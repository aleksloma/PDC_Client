"""/api/admin/relations/* routes: scan end-to-end over the hidden sqlite
dialect (live FK introspection -> confirmed candidate with cardinality +
overlap; broken connection -> degraded, name evidence still returned),
analyze_sql (candidates + the pasted SQL never reaching the audit log),
accept (persists cardinality/origin, batches to one child table without lost
updates, duplicate/flipped skip, validation 400s, audit rows, NO re-snapshot),
and dismiss (audit only, registry unchanged).

Auth: every route here is a literal path under the admin router, so the
existing test_all_admin_routes_reject_non_admin iterator in
test_admin_data_routes.py covers 401/403 for them automatically."""
import json

import pandas as pd
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import db_sources
import local_store
from settings import settings

ADMIN = "ladmin"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    local_store.AuthStore().ensure_user(ADMIN)
    local_store.AuthStore().set_role(ADMIN, "admin")

    import routes.admin_data as admin_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(admin_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session.pop("must_change_password", None)
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{ADMIN}")
    return tc


@pytest.fixture
def fk_setup(client, tmp_path):
    """Two registered tables over one sqlite connection with a real FK
    (orders.customer_id -> customers.id) and snapshots on disk."""
    from sqlalchemy import create_engine, text
    db = tmp_path / "src.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE customers (id INTEGER PRIMARY KEY, city TEXT)"))
        conn.execute(text(
            "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, "
            "customer_id INTEGER REFERENCES customers(id), amount REAL)"))
        conn.execute(text("INSERT INTO customers VALUES (1,'A'),(2,'B'),(3,'C')"))
        conn.execute(text(
            "INSERT INTO orders VALUES (10,1,5.0),(11,1,6.0),(12,2,7.0)"))
    eng.dispose()
    store = db_sources.DataSourceStore()
    masked = store.create_connection(
        {"name": "S", "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{db}"}, "pw", actor=ADMIN)
    cid = masked["id"]

    def register(name, display, cols):
        doc = store.upsert_table({
            "connection_id": cid, "schema": "", "table_name": name,
            "display_name": display, "description": "",
            "columns": [{"name": c, "dtype": "", "description": f"{c} desc",
                         "indexed": False, "pk": c.endswith("id") and name == "customers"}
                        for c in cols],
            "is_connector": False, "relations": [],
            "descriptions_confirmed_by": ADMIN,
            "descriptions_confirmed_at": "2026-08-01T00:00:00+00:00",
        }, actor=ADMIN)
        return doc["id"]

    cust_tid = register("customers", "Customers", ["id", "city"])
    ord_tid = register("orders", "Orders", ["order_id", "customer_id", "amount"])
    pd.DataFrame({"id": [1, 2, 3], "city": list("ABC")}).to_parquet(
        local_store.db_snapshot_path(cust_tid))
    pd.DataFrame({"order_id": [10, 11, 12], "customer_id": [1, 1, 2],
                  "amount": [5.0, 6.0, 7.0]}).to_parquet(
        local_store.db_snapshot_path(ord_tid))
    return {"cid": cid, "customers": cust_tid, "orders": ord_tid, "db": db}


def _audit_actions():
    return [r["action"] for r in db_sources.read_audit_tail(500)]


# ── scan ───────────────────────────────────────────────────────────────────

def test_scan_finds_fk_candidate_verified_and_confirmed(client, fk_setup):
    r = client.post("/api/admin/relations/scan", json={})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["degraded"] == []
    cands = data["candidates"]
    fk_cands = [c for c in cands if "fk" in c["sources"]]
    assert len(fk_cands) == 1
    c = fk_cands[0]
    assert c["table_id"] == fk_setup["orders"]
    assert c["related_table_id"] == fk_setup["customers"]
    assert c["join_keys"] == [["customer_id", "id"]]
    assert c["verified"] is True
    assert c["cardinality"] == "N:1"
    assert c["overlap_pct"] == 100.0 and c["orphans"] == 0
    assert c["band"] == "confirmed"
    assert "relations.scan" in _audit_actions()


def test_scan_broken_connection_degrades_but_name_evidence_survives(client, fk_setup):
    # Point the connection at a nonexistent file: introspection fails, FK
    # evidence degrades, but name/description candidates still come back.
    store = db_sources.DataSourceStore()
    store.update_connection(fk_setup["cid"],
                            {"url_override": "sqlite+pysqlite:///nope/missing.db"},
                            None, actor=ADMIN)
    r = client.post("/api/admin/relations/scan", json={})
    data = r.json()
    assert data["ok"] is True
    assert data["degraded"]
    assert any("name" in c["sources"] for c in data["candidates"])


def test_scan_excludes_already_declared(client, fk_setup):
    store = db_sources.DataSourceStore()
    doc = store.get_table(fk_setup["orders"])
    doc["relations"] = [{"related_table_id": fk_setup["customers"],
                         "join_keys": [["customer_id", "id"]]}]
    store.upsert_table(doc, actor=ADMIN)
    data = client.post("/api/admin/relations/scan", json={}).json()
    assert all(c["join_keys"] != [["customer_id", "id"]]
               for c in data["candidates"])


# ── analyze_sql ────────────────────────────────────────────────────────────

def test_analyze_sql_returns_candidates_and_never_persists_sql(client, fk_setup, tmp_path):
    marker = "zz_secret_column_marker"
    sql = (f"SELECT o.amount AS {marker} FROM orders o "
           "JOIN customers c ON o.customer_id = c.id;"
           "SELECT 1 FROM orders o JOIN customers c ON o.customer_id = c.id")
    r = client.post("/api/admin/relations/analyze_sql",
                    json={"sql": sql, "db_type": "sqlite"})
    data = r.json()
    assert data["ok"] is True
    assert data["stats"]["statements"] == 2
    assert len(data["candidates"]) == 1
    c = data["candidates"][0]
    assert c["sql_frequency"] == 2
    assert c["band"] == "confirmed"              # freq >= 2 with good overlap
    assert c["cardinality"] == "N:1"
    # The pasted SQL must appear NOWHERE in the audit trail (Article II/VII).
    audit_text = (tmp_path / "admin_audit.jsonl").read_text(encoding="utf-8")
    assert marker not in audit_text and "customer_id" not in audit_text
    assert "relations.analyze_sql" in _audit_actions()


def test_analyze_sql_requires_sql(client, fk_setup):
    assert client.post("/api/admin/relations/analyze_sql",
                       json={"sql": "  "}).status_code == 400


# ── accept ─────────────────────────────────────────────────────────────────

def test_accept_persists_extended_fields_no_resnapshot(client, fk_setup):
    snap = local_store.db_snapshot_path(fk_setup["orders"])
    mtime_before = snap.stat().st_mtime_ns
    r = client.post("/api/admin/relations/accept", json={"relations": [{
        "table_id": fk_setup["orders"],
        "related_table_id": fk_setup["customers"],
        "join_keys": [["customer_id", "id"]],
        "cardinality": "N:1", "origin": "fk"}]})
    data = r.json()
    assert data == {"ok": True, "accepted": 1, "skipped": 0, "replaced": 0}
    doc = db_sources.DataSourceStore().get_table(fk_setup["orders"])
    assert doc["relations"] == [{"related_table_id": fk_setup["customers"],
                                 "join_keys": [["customer_id", "id"]],
                                 "origin": "fk", "cardinality": "N:1"}]
    assert snap.stat().st_mtime_ns == mtime_before   # accept never re-snapshots
    assert "relations.accept" in _audit_actions()


def test_accept_batch_same_child_table_no_lost_update(client, fk_setup):
    r = client.post("/api/admin/relations/accept", json={"relations": [
        {"table_id": fk_setup["orders"], "related_table_id": fk_setup["customers"],
         "join_keys": [["customer_id", "id"]], "cardinality": "N:1", "origin": "fk"},
        {"table_id": fk_setup["orders"], "related_table_id": fk_setup["customers"],
         "join_keys": [["amount", "city"]], "origin": "name"},
    ]})
    assert r.json()["accepted"] == 2
    doc = db_sources.DataSourceStore().get_table(fk_setup["orders"])
    assert len(doc["relations"]) == 2


def test_accept_skips_duplicates_including_flipped(client, fk_setup):
    body = {"relations": [{"table_id": fk_setup["orders"],
                           "related_table_id": fk_setup["customers"],
                           "join_keys": [["customer_id", "id"]], "origin": "fk"}]}
    assert client.post("/api/admin/relations/accept", json=body).json()["accepted"] == 1
    # exact duplicate
    assert client.post("/api/admin/relations/accept", json=body).json() == \
        {"ok": True, "accepted": 0, "skipped": 1, "replaced": 0}
    # flipped orientation, declared from the parent side
    flipped = {"relations": [{"table_id": fk_setup["customers"],
                              "related_table_id": fk_setup["orders"],
                              "join_keys": [["id", "customer_id"]], "origin": "name"}]}
    r = client.post("/api/admin/relations/accept", json=flipped).json()
    assert r["accepted"] == 1 and r["skipped"] == 0   # parent-side doc has no dup...
    # ...but re-sending it now skips
    assert client.post("/api/admin/relations/accept", json=flipped).json()["skipped"] == 1


def test_accept_validation_errors(client, fk_setup):
    ok = {"table_id": fk_setup["orders"], "related_table_id": fk_setup["customers"]}
    cases = [
        ({}, 400),                                               # no list
        ({"relations": []}, 400),
        ({"relations": [dict(ok, join_keys=[])]}, 400),          # empty keys
        ({"relations": [dict(ok, join_keys=[["customer_id"]])]}, 400),
        ({"relations": [dict(ok, join_keys=[["nope", "id"]])]}, 400),   # unknown col
        ({"relations": [dict(ok, join_keys=[["customer_id", "id"]],
                             cardinality="X:Y")]}, 400),
        ({"relations": [dict(ok, join_keys=[["customer_id", "id"]],
                             origin="manual")]}, 400),           # manual never via accept
        ({"relations": [{"table_id": "aa11bb22cc33dd44",
                         "related_table_id": fk_setup["customers"],
                         "join_keys": [["a", "b"]]}]}, 400),     # unknown table
    ]
    for body, code in cases:
        assert client.post("/api/admin/relations/accept",
                           json=body).status_code == code, body
    # nothing was written by any failed call
    assert db_sources.DataSourceStore().get_table(fk_setup["orders"])["relations"] == []


def test_accepted_relation_feeds_connector_closure(client, fk_setup):
    """The accepted extended-format relation must keep working with the
    existing expand_with_connectors consumer (extra keys ignored)."""
    store = db_sources.DataSourceStore()
    doc = store.get_table(fk_setup["customers"])
    doc["is_connector"] = True
    store.upsert_table(doc, actor=ADMIN)
    client.post("/api/admin/relations/accept", json={"relations": [{
        "table_id": fk_setup["orders"], "related_table_id": fk_setup["customers"],
        "join_keys": [["customer_id", "id"]], "cardinality": "N:1", "origin": "fk"}]})
    closure = db_sources.expand_with_connectors([fk_setup["orders"]])
    assert set(closure) == {fk_setup["orders"], fk_setup["customers"]}


# ── wizard_suggest ─────────────────────────────────────────────────────────

_SENTINEL = "__wizard__"


def _wizard_body(fk_setup, **over):
    """A new table `invoices` with an FK to customers and a matching sample."""
    body = {
        "editing_tid": "",
        "schema": "", "table_name": "invoices", "display_name": "Invoices",
        "columns": [{"name": "invoice_id", "pk": True},
                    {"name": "customer_id"}, {"name": "total"}],
        "foreign_keys": [{"constrained_columns": ["customer_id"],
                          "referred_schema": None,
                          "referred_table": "customers",
                          "referred_columns": ["id"]}],
        "relations": [],
        "sample": {"columns": ["invoice_id", "customer_id", "total"],
                   "rows": [[1, 1, 10.0], [2, 1, 11.0], [3, 2, 12.0]]},
    }
    body.update(over)
    return body


def test_wizard_suggest_fk_prechecked_n1_correct_direction(client, fk_setup):
    r = client.post("/api/admin/relations/wizard_suggest",
                    json=_wizard_body(fk_setup))
    data = r.json()
    assert data["ok"] is True
    fk = [c for c in data["candidates"] if "fk" in c["sources"]]
    assert len(fk) == 1
    c = fk[0]
    assert c["precheck"] is True
    assert c["table_id"] == _SENTINEL                      # wizard table = child
    assert c["related_table_id"] == fk_setup["customers"]
    assert c["join_keys"] == [["customer_id", "id"]]
    assert c["cardinality"] == "N:1"
    assert c["estimated"] is True
    assert c["overlap_pct"] == 100.0                       # sample ids 1,1,2 all exist
    assert "relations.wizard_suggest" in _audit_actions()


def test_wizard_suggest_name_candidates_unchecked_and_estimated(client, fk_setup):
    data = client.post("/api/admin/relations/wizard_suggest",
                       json=_wizard_body(fk_setup)).json()
    # invoices.customer_id also matches orders.customer_id by name
    name_only = [c for c in data["candidates"]
                 if c["related_table_id"] == fk_setup["orders"]]
    assert name_only, data["candidates"]
    c = name_only[0]
    assert c["precheck"] is False and "fk" not in c["sources"]
    assert c["estimated"] is True                          # orders snapshot exists


def test_wizard_suggest_orientation_normalized_to_wizard_child(client, fk_setup):
    """A wizard pk column matching a registered non-pk column makes the
    generator orient the REGISTERED table as child — the endpoint must flip it
    back (wizard save can only write onto the wizard table) and invert the
    cardinality."""
    body = _wizard_body(
        fk_setup,
        table_name="custmaster", display_name="Cust Master",
        columns=[{"name": "customer_id", "pk": True}],
        foreign_keys=[],
        sample={"columns": ["customer_id"], "rows": [[1], [2], [3]]})
    data = client.post("/api/admin/relations/wizard_suggest", json=body).json()
    to_orders = [c for c in data["candidates"]
                 if c["related_table_id"] == fk_setup["orders"]]
    assert to_orders, data["candidates"]
    c = to_orders[0]
    assert c["table_id"] == _SENTINEL
    assert c["join_keys"] == [["customer_id", "customer_id"]]
    # pre-normalization: orders (non-unique) -> custmaster (unique) was N:1;
    # stored from the wizard side it is one-to-many.
    assert c["cardinality"] == "1:N"
    # Reviewer regression pin: a data-flipped candidate's overlap was measured
    # snapshot->capped-sample (systematically understated) — the numbers are
    # DROPPED rather than shown; the structural facts remain.
    assert c["overlap_pct"] is None and c["orphans"] is None
    assert c["verified"] is True


def test_wizard_suggest_dedupes_existing_declared_flipped_and_body(client, fk_setup):
    store = db_sources.DataSourceStore()
    # (a) declared on the OTHER side: customers -> orders (flipped orientation)
    doc = store.get_table(fk_setup["customers"])
    doc["relations"] = [{"related_table_id": fk_setup["orders"],
                         "join_keys": [["id", "customer_id"]]}]
    store.upsert_table(doc, actor=ADMIN)
    body = _wizard_body(fk_setup, editing_tid=fk_setup["orders"],
                        table_name="orders", display_name="Orders",
                        columns=[{"name": "order_id", "pk": True},
                                 {"name": "customer_id"}, {"name": "amount"}])
    data = client.post("/api/admin/relations/wizard_suggest", json=body).json()
    assert all(c["related_table_id"] != fk_setup["customers"]
               for c in data["candidates"]), data["candidates"]
    # (b) same dedupe from rows typed in the wizard (body relations)
    doc = store.get_table(fk_setup["customers"])
    doc["relations"] = []
    store.upsert_table(doc, actor=ADMIN)
    body["relations"] = [{"related_table_id": fk_setup["customers"],
                          "join_keys": [["customer_id", "id"]]}]
    data = client.post("/api/admin/relations/wizard_suggest", json=body).json()
    assert all(c["related_table_id"] != fk_setup["customers"]
               for c in data["candidates"]), data["candidates"]


def test_wizard_suggest_sample_overlap_math_and_missing_snapshot(client, fk_setup):
    body = _wizard_body(fk_setup, sample={
        "columns": ["customer_id"], "rows": [[1], [1], [2], [9]]})
    data = client.post("/api/admin/relations/wizard_suggest", json=body).json()
    fk = [c for c in data["candidates"] if "fk" in c["sources"]][0]
    assert fk["overlap_pct"] == 75.0 and fk["orphans"] == 1   # 9 has no parent
    # parent snapshot gone -> unverified, still N:1 pre-checked
    local_store.db_snapshot_path(fk_setup["customers"]).unlink()
    data = client.post("/api/admin/relations/wizard_suggest", json=body).json()
    fk = [c for c in data["candidates"] if "fk" in c["sources"]][0]
    assert fk["verified"] is False and fk["estimated"] is False
    assert fk["precheck"] is True and fk["cardinality"] == "N:1"


def test_wizard_suggest_descriptions_feed_description_source(client, fk_setup):
    desc = "unique customer identifier assigned by billing system"
    store = db_sources.DataSourceStore()
    doc = store.get_table(fk_setup["customers"])
    doc["columns"][0]["description"] = desc
    store.upsert_table(doc, actor=ADMIN)
    body = _wizard_body(fk_setup,
                        columns=[{"name": "cust_ref", "description": desc}],
                        foreign_keys=[],
                        sample={"columns": ["cust_ref"], "rows": [[1], [2]]})
    data = client.post("/api/admin/relations/wizard_suggest", json=body).json()
    descs = [c for c in data["candidates"] if "description" in c["sources"]]
    assert descs, data["candidates"]
    assert descs[0]["table_id"] == _SENTINEL
    assert descs[0]["precheck"] is False


def test_wizard_suggest_no_sample_all_unverified(client, fk_setup):
    for sample in (None, {"columns": [], "rows": []}):
        data = client.post("/api/admin/relations/wizard_suggest",
                           json=_wizard_body(fk_setup, sample=sample)).json()
        assert data["ok"] is True and data["candidates"]
        assert all(c["verified"] is False for c in data["candidates"])


def test_wizard_suggest_audit_counts_only_no_sample_values(client, fk_setup, tmp_path):
    marker = "zz_wizard_secret_cell_774"
    body = _wizard_body(fk_setup, sample={
        "columns": ["invoice_id", "customer_id", "total"],
        "rows": [[1, 1, marker]]})
    assert client.post("/api/admin/relations/wizard_suggest",
                       json=body).json()["ok"] is True
    audit_text = (tmp_path / "admin_audit.jsonl").read_text(encoding="utf-8")
    assert marker not in audit_text


def test_wizard_suggest_requires_table_name(client, fk_setup):
    assert client.post("/api/admin/relations/wizard_suggest",
                       json={"table_name": " "}).status_code == 400


# ── delete ─────────────────────────────────────────────────────────────────

def test_delete_removes_matching_entries_neighbors_untouched(client, fk_setup):
    store = db_sources.DataSourceStore()
    doc = store.get_table(fk_setup["orders"])
    legacy = {"related_table": "customers", "join_keys": [["amount", "city"]]}
    dup = {"related_table_id": fk_setup["customers"],
           "join_keys": [["customer_id", "id"]], "origin": "fk"}
    doc["relations"] = [dict(dup), dict(dup), legacy]      # identical duplicates
    store.upsert_table(doc, actor=ADMIN)

    r = client.post("/api/admin/relations/delete", json={
        "table_id": fk_setup["orders"],
        "related_table_id": fk_setup["customers"],
        "join_keys": [["customer_id", "id"]]})
    assert r.json() == {"ok": True, "removed": 2}          # BOTH duplicates go
    left = store.get_table(fk_setup["orders"])["relations"]
    assert left == [legacy]                                # legacy neighbor intact
    assert "relations.delete" in _audit_actions()

    # legacy name-based entries are deletable too
    r = client.post("/api/admin/relations/delete", json={
        "table_id": fk_setup["orders"], "related_table": "customers",
        "join_keys": [["amount", "city"]]})
    assert r.json() == {"ok": True, "removed": 1}
    assert store.get_table(fk_setup["orders"])["relations"] == []


def test_delete_404_on_no_match_and_bad_input(client, fk_setup):
    assert client.post("/api/admin/relations/delete", json={
        "table_id": fk_setup["orders"],
        "related_table_id": fk_setup["customers"],
        "join_keys": [["nope", "nope"]]}).status_code == 404
    assert client.post("/api/admin/relations/delete", json={
        "table_id": "zz", "related_table_id": fk_setup["customers"],
        "join_keys": [["a", "b"]]}).status_code == 400
    assert client.post("/api/admin/relations/delete", json={
        "table_id": fk_setup["orders"], "related_table_id": fk_setup["customers"],
        "join_keys": []}).status_code == 400


# ── accept + replaces (overview Edit) ──────────────────────────────────────

def _seed_orders_relation(fk_setup, jk=(("customer_id", "id"),), origin="fk"):
    store = db_sources.DataSourceStore()
    doc = store.get_table(fk_setup["orders"])
    doc["relations"] = [{"related_table_id": fk_setup["customers"],
                         "join_keys": [list(p) for p in jk], "origin": origin}]
    store.upsert_table(doc, actor=ADMIN)


def test_accept_replaces_swaps_in_one_write(client, fk_setup):
    _seed_orders_relation(fk_setup)
    r = client.post("/api/admin/relations/accept", json={"relations": [{
        "table_id": fk_setup["orders"],
        "related_table_id": fk_setup["customers"],
        "join_keys": [["amount", "city"]], "origin": "name",
        "replaces": {"related_table_id": fk_setup["customers"],
                     "join_keys": [["customer_id", "id"]]}}]})
    assert r.json() == {"ok": True, "accepted": 1, "skipped": 0, "replaced": 1}
    rels = db_sources.DataSourceStore().get_table(fk_setup["orders"])["relations"]
    assert rels == [{"related_table_id": fk_setup["customers"],
                     "join_keys": [["amount", "city"]], "origin": "name"}]


def test_accept_replaces_duplicate_of_other_entry_preserves_old(client, fk_setup):
    store = db_sources.DataSourceStore()
    doc = store.get_table(fk_setup["orders"])
    doc["relations"] = [
        {"related_table_id": fk_setup["customers"],
         "join_keys": [["customer_id", "id"]], "origin": "fk"},
        {"related_table_id": fk_setup["customers"],
         "join_keys": [["amount", "city"]], "origin": "name"},
    ]
    store.upsert_table(doc, actor=ADMIN)
    # Edit the second entry INTO a duplicate of the first -> skipped, old kept.
    r = client.post("/api/admin/relations/accept", json={"relations": [{
        "table_id": fk_setup["orders"],
        "related_table_id": fk_setup["customers"],
        "join_keys": [["customer_id", "id"]],
        "replaces": {"related_table_id": fk_setup["customers"],
                     "join_keys": [["amount", "city"]]}}]})
    assert r.json() == {"ok": True, "accepted": 0, "skipped": 1, "replaced": 0}
    rels = store.get_table(fk_setup["orders"])["relations"]
    assert len(rels) == 2                                  # nothing silently deleted
    assert {"related_table_id": fk_setup["customers"],
            "join_keys": [["amount", "city"]], "origin": "name"} in rels


def test_accept_replaces_stale_degrades_to_plain_accept(client, fk_setup):
    r = client.post("/api/admin/relations/accept", json={"relations": [{
        "table_id": fk_setup["orders"],
        "related_table_id": fk_setup["customers"],
        "join_keys": [["customer_id", "id"]], "origin": "fk",
        "replaces": {"related_table_id": fk_setup["customers"],
                     "join_keys": [["long", "gone"]]}}]})
    assert r.json() == {"ok": True, "accepted": 1, "skipped": 0, "replaced": 0}
    rels = db_sources.DataSourceStore().get_table(fk_setup["orders"])["relations"]
    assert len(rels) == 1


def test_accept_replaces_invalid_shape_400(client, fk_setup):
    r = client.post("/api/admin/relations/accept", json={"relations": [{
        "table_id": fk_setup["orders"],
        "related_table_id": fk_setup["customers"],
        "join_keys": [["customer_id", "id"]],
        "replaces": {"join_keys": []}}]})
    assert r.status_code == 400
    assert db_sources.DataSourceStore().get_table(fk_setup["orders"])["relations"] == []


# ── confirmed_count in scan / analyze_sql ──────────────────────────────────

def test_scan_and_analyze_report_confirmed_count(client, fk_setup):
    assert client.post("/api/admin/relations/scan",
                       json={}).json()["confirmed_count"] == 0
    _seed_orders_relation(fk_setup)
    assert client.post("/api/admin/relations/scan",
                       json={}).json()["confirmed_count"] == 1
    r = client.post("/api/admin/relations/analyze_sql", json={
        "sql": "SELECT 1 FROM orders o JOIN customers c ON o.customer_id = c.id",
        "db_type": "sqlite"})
    data = r.json()
    assert data["confirmed_count"] == 1
    assert data["candidates"] == []                        # already declared


# ── dismiss ────────────────────────────────────────────────────────────────

def test_dismiss_audits_and_persists_nothing(client, fk_setup):
    before = json.dumps(db_sources.DataSourceStore().read_doc()["tables"])
    r = client.post("/api/admin/relations/dismiss", json={
        "table_id": fk_setup["orders"], "related_table_id": fk_setup["customers"],
        "join_keys": [["customer_id", "id"]], "band": "suggested"})
    assert r.json() == {"ok": True}
    after = json.dumps(db_sources.DataSourceStore().read_doc()["tables"])
    assert before == after
    assert "relations.dismiss" in _audit_actions()
