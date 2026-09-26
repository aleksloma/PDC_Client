"""Live-mode registry for database tables.

A registered table carries a storage `mode`: "snapshot" (the default, a local
parquet refreshed on a schedule) or "live" (no parquet; queried at question
time once that path ships). This file pins the REGISTRY half:

* the two size thresholds in settings (`LIVE_MODE_CELL_THRESHOLD`,
  `LIVE_MODE_FORCE_THRESHOLD`, both through `_int_env`; the effective force
  threshold is max(force, cell));
* the store: `MODES`, `LIVE_REASONS`, `table_mode`, `validate_mode_fields`,
  `upsert_table`'s snapshot default, `set_table_mode` (audited `table.mode`,
  same mode writes nothing), `mark_live_profiled`, `list_tables(include_live=)`
  and the connector closure skipping a live connector;
* old-shape documents (no `mode` key) reading as snapshot without a rewrite;
* the admin routes: the introspect `size_verdict`, the save body's `mode`
  (server-derived `live_reason`, `LIVE_REQUIRED`, `BAD_MODE`, the sampled live
  profile, no parquet), the edit-save carry-over, `POST /tables/{tid}/mode`
  (guards, scope, audit, parquet kept on → live, re-snapshot on → snapshot),
  the derived list fields, refresh-now re-profiling a live table, the
  connection refresh skipping it and the recommendation accept refusing a
  table above the force threshold.

Offline: every database is a tmp-file sqlite reached through the hidden
sqlite dialect (the same idiom as tests/test_admin_data_routes.py). The
seeded table `t` has 2 rows x 2 columns = 4 cells, so the thresholds are
monkeypatched around that number.
"""
import json

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import db_sources
import local_store
import roles_store
from settings import settings

ADMIN = "ladmin"
USER = "user@x.com"
POWER = "power@x.com"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _set_thresholds(monkeypatch, cell=None, force=None):
    """Set the two live-mode thresholds for one test. Fails with a clear
    message (not a pydantic error) while the settings do not exist."""
    for name, value in (("LIVE_MODE_CELL_THRESHOLD", cell),
                        ("LIVE_MODE_FORCE_THRESHOLD", force)):
        if value is None:
            continue
        if not hasattr(settings, name):
            pytest.fail(f"settings.{name} is missing")
        monkeypatch.setattr(settings, name, value)


def _mode_rows():
    return [r for r in db_sources.read_audit_tail(1000)
            if r.get("action") == "table.mode"]


def _table_body(cid, table="t", display=None, **extra):
    body = {"connection_id": cid, "schema": "", "table_name": table,
            "display_name": display or f"{table} table", "description": "desc",
            "columns": [
                {"name": "a", "dtype": "INTEGER", "description": "col a",
                 "indexed": True, "pk": True},
                {"name": "b", "dtype": "TEXT", "description": "col b",
                 "indexed": False}],
            "is_connector": False, "relations": [], "confirm": True}
    body.update(extra)
    return body


def _mk_sqlite_db(tmp_path, dbfile, tables=("t",)):
    from sqlalchemy import create_engine, text
    db = tmp_path / dbfile
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        for t in tables:
            conn.execute(text(f"CREATE TABLE {t} (a INTEGER PRIMARY KEY, b TEXT)"))
            conn.execute(text(f"INSERT INTO {t} VALUES (1, 'x'), (2, 'y')"))
    eng.dispose()
    return db


def _mk_sqlite_conn(tmp_path, name, dbfile, tables=("t",)):
    db = _mk_sqlite_db(tmp_path, dbfile, tables)
    masked = db_sources.DataSourceStore().create_connection(
        {"name": name, "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{db}"}, "pw", actor=ADMIN)
    return masked["id"]


def _grant_power(email, grants):
    role = roles_store.RolesStore().create_role(
        {"name": f"PU {email}", "manage_grants": grants}, actor=ADMIN)
    auth = local_store.AuthStore()
    auth.ensure_user(email)
    auth.set_role(email, "power")
    auth.set_data_role(email, role["id"])
    return role


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    local_store.AuthStore().ensure_user(ADMIN)
    local_store.AuthStore().set_role(ADMIN, "admin")
    local_store.AuthStore().ensure_user(USER)

    import routes.admin_data as admin_mod

    def fake_autofill(**kw):
        return {"file_description": "AI table desc",
                "columns": {"a": "AI col a", "b": "AI col b"}}

    monkeypatch.setattr(admin_mod.brain_client, "schema_autofill", fake_autofill)

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
def conn(client, tmp_path):
    """One saved sqlite connection holding tables t and u (2 rows x 2 cols)."""
    return _mk_sqlite_conn(tmp_path, "S", "src.db", tables=("t", "u"))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    return db_sources.DataSourceStore()


def _store_table(store, cid="aa11bb22cc33dd44", **over):
    doc = {"connection_id": cid, "schema": "s", "table_name": "t",
           "display_name": "t table", "description": "",
           "columns": [{"name": "a"}, {"name": "b"}],
           "is_connector": False, "relations": []}
    doc.update(over)
    return store.upsert_table(doc, actor=ADMIN)


def _register_live(client, cid, table="t"):
    r = client.post("/api/admin/tables", json=_table_body(cid, table=table,
                                                         mode="live"))
    assert r.status_code == 201, r.json()
    return r.json()["table"]["id"]


# ===========================================================================
# settings
# ===========================================================================
def test_threshold_defaults(monkeypatch):
    from settings import Settings
    monkeypatch.delenv("LIVE_MODE_CELL_THRESHOLD", raising=False)
    monkeypatch.delenv("LIVE_MODE_FORCE_THRESHOLD", raising=False)
    s = Settings()
    assert getattr(s, "LIVE_MODE_CELL_THRESHOLD", None) == 50_000_000
    assert getattr(s, "LIVE_MODE_FORCE_THRESHOLD", None) == 500_000_000


@pytest.mark.parametrize("raw", ["abc", "", "1e6"])
def test_threshold_env_garbage_falls_back_to_default(monkeypatch, raw):
    """A typo in the environment never crashes Settings() (the _int_env idiom)."""
    from settings import Settings
    monkeypatch.setenv("LIVE_MODE_CELL_THRESHOLD", raw)
    monkeypatch.setenv("LIVE_MODE_FORCE_THRESHOLD", raw)
    s = Settings()
    assert getattr(s, "LIVE_MODE_CELL_THRESHOLD", None) == 50_000_000
    assert getattr(s, "LIVE_MODE_FORCE_THRESHOLD", None) == 500_000_000


def test_threshold_env_values_are_read(monkeypatch):
    from settings import Settings
    monkeypatch.setenv("LIVE_MODE_CELL_THRESHOLD", "4")
    monkeypatch.setenv("LIVE_MODE_FORCE_THRESHOLD", "8")
    s = Settings()
    assert getattr(s, "LIVE_MODE_CELL_THRESHOLD", None) == 4
    assert getattr(s, "LIVE_MODE_FORCE_THRESHOLD", None) == 8


# ===========================================================================
# store
# ===========================================================================
def test_modes_and_reasons_constants():
    assert getattr(db_sources, "MODES", None) == ("snapshot", "live")
    assert getattr(db_sources, "LIVE_REASONS", None) == ("threshold", "manual")


def test_table_mode_reads_absent_and_none_as_snapshot():
    assert db_sources.table_mode({}) == "snapshot"
    assert db_sources.table_mode({"mode": None}) == "snapshot"
    assert db_sources.table_mode({"mode": "snapshot"}) == "snapshot"
    assert db_sources.table_mode({"mode": "live"}) == "live"


@pytest.mark.parametrize("doc", [
    {"mode": "turbo"},
    {"mode": "LIVE"},
    {"mode": "live", "live_reason": "because"},
], ids=["unknown-mode", "wrong-case", "unknown-reason"])
def test_validate_mode_fields_rejects_bad_values(doc):
    with pytest.raises(ValueError):
        db_sources.validate_mode_fields(doc)


@pytest.mark.parametrize("doc", [
    {},
    {"mode": "snapshot"},
    {"mode": "live", "live_reason": "threshold"},
    {"mode": "live", "live_reason": "manual"},
    {"mode": "live", "live_reason": None},
], ids=["absent", "snapshot", "threshold", "manual", "reason-none"])
def test_validate_mode_fields_accepts_good_values(doc):
    db_sources.validate_mode_fields(doc)


def test_upsert_table_defaults_mode_to_snapshot(store):
    t = _store_table(store)
    assert t.get("mode") == "snapshot"
    assert store.get_table(t["id"]).get("mode") == "snapshot"


def test_upsert_table_rejects_a_bad_mode_and_writes_nothing(store):
    with pytest.raises(ValueError):
        _store_table(store, mode="turbo")
    assert store.list_tables() == []


def test_set_table_mode_unknown_table_is_false(store):
    assert store.set_table_mode("aa11bb22cc33dd44", "live", actor=ADMIN) is False


def test_set_table_mode_bad_mode_raises(store):
    tid = _store_table(store)["id"]
    with pytest.raises(ValueError):
        store.set_table_mode(tid, "turbo", actor=ADMIN)
    assert db_sources.table_mode(store.get_table(tid)) == "snapshot"


def test_set_table_mode_to_live_stamps_and_audits(store):
    tid = _store_table(store)["id"]
    ok = store.set_table_mode(tid, "live", actor=ADMIN, reason="manual",
                              cell_count=4)
    assert ok is True
    doc = store.get_table(tid)
    assert doc["mode"] == "live"
    assert doc["live_reason"] == "manual"
    assert doc["live_set_by"] == ADMIN
    assert isinstance(doc["live_set_at"], str) and doc["live_set_at"]
    rows = _mode_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == ADMIN and row["target"] == tid
    assert row["detail"]["from"] == "snapshot"
    assert row["detail"]["to"] == "live"
    assert row["detail"]["reason"] == "manual"
    assert row["detail"]["cell_count"] == 4
    assert "actor_kind" not in row["detail"]


def test_set_table_mode_actor_kind_is_merged(store):
    tid = _store_table(store)["id"]
    store.set_table_mode(tid, "live", actor=POWER, actor_kind="power_user",
                         reason="manual", cell_count=4)
    assert _mode_rows()[0]["detail"]["actor_kind"] == "power_user"


def test_set_table_mode_back_to_snapshot_clears_the_live_keys(store):
    tid = _store_table(store)["id"]
    store.set_table_mode(tid, "live", actor=ADMIN, reason="threshold",
                         cell_count=9)
    assert store.set_table_mode(tid, "snapshot", actor=ADMIN) is True
    doc = store.get_table(tid)
    assert doc["mode"] == "snapshot"
    assert doc.get("live_reason") is None
    assert doc.get("live_set_by") is None
    assert doc.get("live_set_at") is None
    assert [r["detail"]["to"] for r in _mode_rows()] == ["snapshot", "live"]


def test_set_table_mode_same_mode_writes_and_audits_nothing(store, tmp_path):
    tid = _store_table(store)["id"]
    path = tmp_path / "data_sources.json"
    before = path.read_bytes()
    assert store.set_table_mode(tid, "snapshot", actor=ADMIN) is True
    assert path.read_bytes() == before
    assert _mode_rows() == []


def test_mark_live_profiled_is_field_level(store):
    tid = _store_table(store, description="keep me")["id"]
    cols = [{"name": "a", "dtype": "INTEGER", "description": "A"},
            {"name": "b", "dtype": "TEXT", "description": "B"}]
    store.mark_live_profiled(tid, row_count=123, columns=cols,
                             profiled_at="2026-09-26T00:00:00+00:00",
                             sample_rows=100)
    doc = store.get_table(tid)
    assert doc["row_count"] == 123
    assert doc["columns"] == cols
    assert doc["live_profiled_at"] == "2026-09-26T00:00:00+00:00"
    assert doc["live_sample_rows"] == 100
    assert doc["description"] == "keep me"          # the rest is untouched


def test_list_tables_include_live_flag(store):
    snap = _store_table(store, table_name="s1")["id"]
    live = _store_table(store, table_name="l1", mode="live")["id"]
    live_conn = _store_table(store, table_name="l2", mode="live",
                             is_connector=True)["id"]
    snap_conn = _store_table(store, table_name="s2", is_connector=True)["id"]
    all_ids = {t["id"] for t in store.list_tables()}
    assert all_ids == {snap, live, live_conn, snap_conn}
    assert {t["id"] for t in store.list_tables(include_live=False)} == \
        {snap, snap_conn}
    assert {t["id"] for t in store.list_tables(include_connector=False,
                                               include_live=False)} == {snap}
    assert {t["id"] for t in store.list_tables(include_connector=False)} == \
        {snap, live}


def test_connector_closure_skips_a_live_connector(store):
    live_dict = _store_table(store, table_name="city_dict", mode="live",
                             is_connector=True)["id"]
    snap_dict = _store_table(store, table_name="region_dict",
                             is_connector=True)["id"]
    seed = _store_table(store, table_name="clients", relations=[
        {"related_table_id": live_dict, "join_keys": [["a", "a"]]},
        {"related_table_id": snap_dict, "join_keys": [["b", "b"]]}])["id"]
    closure = db_sources.expand_with_connectors([seed], store)
    assert snap_dict in closure
    assert live_dict not in closure


def test_old_shape_table_reads_as_snapshot_without_a_rewrite(tmp_path, monkeypatch):
    """A registry written before the mode field existed: the table has no
    `mode` key. It reads as snapshot, stays in the picker list, and reading it
    never writes the key back to disk (no boot migration)."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    tid = "aa11bb22cc33dd44"
    raw = json.dumps({
        "version": 1,
        "connections": [{"id": "bb11bb22cc33dd44", "name": "legacy"}],
        "tables": [{"id": tid, "connection_id": "bb11bb22cc33dd44",
                    "schema": "s", "table_name": "t", "display_name": "T",
                    "columns": [{"name": "a"}], "is_connector": False,
                    "relations": []}]})
    path = tmp_path / "data_sources.json"
    path.write_text(raw, encoding="utf-8")
    store = db_sources.DataSourceStore()
    doc = store.get_table(tid)
    assert db_sources.table_mode(doc) == "snapshot"
    assert [t["id"] for t in store.list_tables(include_live=False)] == [tid]
    assert [t["id"] for t in store.list_tables(include_connector=False,
                                               include_live=False)] == [tid]
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "mode" not in on_disk["tables"][0]
    assert path.read_text(encoding="utf-8") == raw


# ===========================================================================
# POST /api/admin/tables/introspect — size_verdict
# ===========================================================================
def _verdict(client, cid, table="t"):
    r = client.post("/api/admin/tables/introspect",
                    json={"connection_id": cid, "table": table})
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["ok"] is True
    assert "size_verdict" in body, "introspect carries no size_verdict"
    return body["size_verdict"]


def test_verdict_counts_rows_and_cells(client, conn):
    v = _verdict(client, conn)
    assert v["row_count"] == 2
    assert v["count_source"] == "count"
    assert v["timed_out"] is False
    assert v["cell_count"] == 4
    assert v["live_suggested"] is False
    assert v["live_required"] is False


def test_verdict_suggests_live_at_the_cell_threshold(client, conn, monkeypatch):
    _set_thresholds(monkeypatch, cell=3)
    v = _verdict(client, conn)
    assert v["cell_count"] == 4
    assert v["live_suggested"] is True
    assert v["live_required"] is False


def test_verdict_requires_live_at_the_force_threshold(client, conn, monkeypatch):
    _set_thresholds(monkeypatch, cell=3, force=4)
    v = _verdict(client, conn)
    assert v["live_suggested"] is True
    assert v["live_required"] is True


def test_verdict_effective_force_is_never_below_cell(client, conn, monkeypatch):
    """force=2 < cell=5: the effective force threshold is 5, so 4 cells are
    neither suggested nor required."""
    _set_thresholds(monkeypatch, cell=5, force=2)
    v = _verdict(client, conn)
    assert v["cell_count"] == 4
    assert v["live_suggested"] is False
    assert v["live_required"] is False


def test_verdict_count_failure_falls_back_to_the_estimate(client, conn, monkeypatch):
    import routes.admin_data as admin_mod
    monkeypatch.setattr(admin_mod.db_connector, "count_rows",
                        lambda *a, **k: {"ok": False, "count": None,
                                         "timed_out": False, "error": "boom"},
                        raising=False)
    v = _verdict(client, conn)
    assert v["count_source"] == "estimate"
    assert v["row_count"] == 2          # sqlite's catalog estimate is exact
    assert v["cell_count"] == 4
    assert v["timed_out"] is False


def test_verdict_count_timeout_requires_live(client, conn, monkeypatch):
    import routes.admin_data as admin_mod
    monkeypatch.setattr(admin_mod.db_connector, "count_rows",
                        lambda *a, **k: {"ok": False, "count": None,
                                         "timed_out": True, "error": "x"},
                        raising=False)
    v = _verdict(client, conn)
    assert v["timed_out"] is True
    assert v["live_required"] is True


def test_verdict_unknown_when_count_and_estimate_are_missing(client, conn,
                                                             monkeypatch):
    import routes.admin_data as admin_mod
    import db_connector
    real_introspect = db_connector.introspect

    def introspect_without_estimate(*a, **k):
        out = real_introspect(*a, **k)
        out["row_count_estimate"] = None
        return out

    monkeypatch.setattr(admin_mod.db_connector, "introspect",
                        introspect_without_estimate)
    monkeypatch.setattr(admin_mod.db_connector, "count_rows",
                        lambda *a, **k: {"ok": False, "count": None,
                                         "timed_out": False, "error": "boom"},
                        raising=False)
    _set_thresholds(monkeypatch, cell=1, force=1)
    v = _verdict(client, conn)
    assert v["row_count"] is None
    assert v["count_source"] is None
    assert v["cell_count"] is None
    assert v["live_suggested"] is False
    assert v["live_required"] is False


# ===========================================================================
# POST /api/admin/tables — mode in the save body
# ===========================================================================
def test_save_live_writes_no_parquet_and_a_sampled_profile(client, conn):
    r = client.post("/api/admin/tables",
                    json=_table_body(conn, mode="live", live_reason="threshold"))
    assert r.status_code == 201, r.json()
    out = r.json()
    assert out["snapshot"] is None
    lp = out["live_profile"]
    assert lp["ok"] is True
    assert lp["rows"] == 2
    assert lp["sample_rows"] == 2
    tid = out["table"]["id"]
    assert not local_store.db_snapshot_path(tid).exists()
    prof = json.loads(local_store.db_profile_path(tid).read_text(encoding="utf-8"))
    assert prof["sampled"] is True
    assert prof["rows"] == 2
    src = prof["src"]
    assert set(src) == {"kind", "profiled_at", "sample_rows"}
    assert src["kind"] == "live"
    assert isinstance(src["profiled_at"], str) and src["profiled_at"]
    assert isinstance(src["sample_rows"], int)
    doc = db_sources.DataSourceStore().get_table(tid)
    assert doc["mode"] == "live"
    assert doc["live_set_by"] == ADMIN
    assert doc["live_set_at"]
    # Server-derived: 4 cells < the default cell threshold ⇒ "manual", even
    # though the body claimed "threshold".
    assert doc["live_reason"] == "manual"
    assert doc["live_profiled_at"]
    assert not doc.get("refreshed_at")


def test_save_live_above_the_cell_threshold_is_reason_threshold(client, conn,
                                                                monkeypatch):
    _set_thresholds(monkeypatch, cell=3)
    r = client.post("/api/admin/tables",
                    json=_table_body(conn, mode="live", live_reason="manual"))
    assert r.status_code == 201, r.json()
    doc = db_sources.DataSourceStore().get_table(r.json()["table"]["id"])
    assert doc["live_reason"] == "threshold"


def test_save_live_ignores_a_client_asserted_setter(client, conn):
    r = client.post("/api/admin/tables",
                    json=_table_body(conn, mode="live",
                                     live_set_by="attacker@evil",
                                     live_set_at="1999-01-01T00:00:00+00:00"))
    assert r.status_code == 201, r.json()
    doc = db_sources.DataSourceStore().get_table(r.json()["table"]["id"])
    assert doc["live_set_by"] == ADMIN
    assert doc["live_set_at"] != "1999-01-01T00:00:00+00:00"


def test_save_snapshot_when_required_is_refused(client, conn, monkeypatch):
    _set_thresholds(monkeypatch, cell=3, force=4)
    r = client.post("/api/admin/tables", json=_table_body(conn, mode="snapshot"))
    assert r.status_code == 400
    assert r.json()["code"] == "LIVE_REQUIRED"
    assert db_sources.DataSourceStore().list_tables() == []


def test_save_without_mode_when_required_is_refused(client, conn, monkeypatch):
    """Absent mode on a NEW registration means snapshot — refused the same."""
    _set_thresholds(monkeypatch, cell=3, force=4)
    r = client.post("/api/admin/tables", json=_table_body(conn))
    assert r.status_code == 400
    assert r.json()["code"] == "LIVE_REQUIRED"
    assert db_sources.DataSourceStore().list_tables() == []


def test_a_row_cap_bounds_the_verdict_at_save(client, conn, monkeypatch):
    """The snapshot copies at most `row_cap` rows, so the verdict counts at
    most that many: 1 row x 2 columns stays under a force threshold of 4 that
    the uncapped 2 x 2 table would reach."""
    _set_thresholds(monkeypatch, cell=3, force=4)
    r = client.post("/api/admin/tables",
                    json=_table_body(conn, mode="snapshot", row_cap=1))
    assert r.status_code == 201, (r.status_code, r.text[:300])
    assert r.json()["snapshot"]["ok"] is True


def test_save_with_a_bad_mode_is_400(client, conn):
    r = client.post("/api/admin/tables", json=_table_body(conn, mode="turbo"))
    assert r.status_code == 400
    assert r.json()["code"] == "BAD_MODE"
    assert db_sources.DataSourceStore().list_tables() == []


def test_save_without_mode_is_a_snapshot_table(client, conn):
    r = client.post("/api/admin/tables", json=_table_body(conn))
    assert r.status_code == 201
    doc = db_sources.DataSourceStore().get_table(r.json()["table"]["id"])
    assert doc["mode"] == "snapshot"


def test_edit_save_without_mode_keeps_live_and_its_stamps(client, conn):
    tid = _register_live(client, conn)
    before = db_sources.DataSourceStore().get_table(tid)
    body = _table_body(conn, description="edited")
    r = client.post(f"/api/admin/tables/{tid}", json=body)
    assert r.status_code == 200, r.json()
    after = db_sources.DataSourceStore().get_table(tid)
    assert after["description"] == "edited"
    assert after["mode"] == "live"
    for key in ("live_reason", "live_set_by", "live_set_at"):
        assert after[key] == before[key], key
    assert not local_store.db_snapshot_path(tid).exists()


# ===========================================================================
# POST /api/admin/tables/{tid}/mode
# ===========================================================================
def _register_snapshot(client, cid, table="t"):
    r = client.post("/api/admin/tables", json=_table_body(cid, table=table))
    assert r.status_code == 201, r.json()
    return r.json()["table"]["id"]


def test_mode_route_unauthenticated_is_401(client, conn):
    tid = _register_snapshot(client, conn)
    client.cookies.clear()
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "live"})
    assert r.status_code == 401


def test_mode_route_plain_user_is_403(client, conn):
    tid = _register_snapshot(client, conn)
    client.post(f"/_login/{USER}")
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "live"})
    assert r.status_code == 403
    assert db_sources.table_mode(db_sources.DataSourceStore().get_table(tid)) \
        == "snapshot"


def test_mode_route_power_user_out_of_scope_is_403(client, tmp_path):
    cid1 = _mk_sqlite_conn(tmp_path, "S1", "src1.db")
    cid2 = _mk_sqlite_conn(tmp_path, "S2", "src2.db", tables=("v",))
    tid2 = _register_snapshot(client, cid2, table="v")
    _grant_power(POWER, [{"connection_id": cid1}])
    client.post(f"/_login/{POWER}")
    r = client.post(f"/api/admin/tables/{tid2}/mode", json={"mode": "live"})
    assert r.status_code == 403
    assert r.json()["code"] == "OUT_OF_SCOPE"
    assert _mode_rows() == []


def test_mode_route_power_user_in_scope_is_audited_with_actor_kind(client, conn):
    tid = _register_snapshot(client, conn)
    _grant_power(POWER, [{"connection_id": conn}])
    client.post(f"/_login/{POWER}")
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "live"})
    assert r.status_code == 200, r.json()
    rows = _mode_rows()
    assert len(rows) == 1
    assert rows[0]["actor"] == POWER
    assert rows[0]["detail"]["actor_kind"] == "power_user"
    assert rows[0]["detail"]["from"] == "snapshot"
    assert rows[0]["detail"]["to"] == "live"
    for key in ("reason", "cell_count"):
        assert key in rows[0]["detail"], key


def test_mode_route_admin_to_live_keeps_the_parquet(client, conn):
    tid = _register_snapshot(client, conn)
    snap = local_store.db_snapshot_path(tid)
    assert snap.exists()
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "live"})
    assert r.status_code == 200, r.json()
    out = r.json()
    assert out["ok"] is True
    assert out["table"]["mode"] == "live"
    assert "live_profile" in out
    assert snap.exists(), "switching to live must not delete the snapshot"
    doc = db_sources.DataSourceStore().get_table(tid)
    assert doc["mode"] == "live" and doc["live_set_by"] == ADMIN
    rows = _mode_rows()
    assert len(rows) == 1 and "actor_kind" not in rows[0]["detail"]


def test_mode_route_unknown_table_is_404(client, conn):
    r = client.post("/api/admin/tables/aa11bb22cc33dd44/mode",
                    json={"mode": "live"})
    assert r.status_code == 404


def test_mode_route_bad_mode_is_400(client, conn):
    tid = _register_snapshot(client, conn)
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "turbo"})
    assert r.status_code == 400
    assert r.json()["code"] == "BAD_MODE"
    assert _mode_rows() == []


def test_mode_route_to_snapshot_takes_a_snapshot_when_none_exists(client, conn):
    tid = _register_live(client, conn)
    snap = local_store.db_snapshot_path(tid)
    assert not snap.exists()
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "snapshot"})
    assert r.status_code == 200, r.json()
    out = r.json()
    assert out["ok"] is True
    assert out["snapshot"]["ok"] is True
    assert out["table"]["mode"] == "snapshot"
    assert snap.exists()
    doc = db_sources.DataSourceStore().get_table(tid)
    assert doc["mode"] == "snapshot"
    for key in ("live_reason", "live_set_by", "live_set_at"):
        assert doc.get(key) is None, key


def test_mode_route_to_snapshot_replaces_a_parquet_kept_from_before(client, conn):
    """A parquet kept through the live period is stale: switching back takes
    a fresh snapshot instead of serving the old copy as current data."""
    tid = _register_snapshot(client, conn)
    snap = local_store.db_snapshot_path(tid)
    old_mtime = snap.stat().st_mtime_ns
    assert client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "live"}).status_code == 200
    import os, time
    os.utime(snap, ns=(old_mtime - 10**9, old_mtime - 10**9))
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "snapshot"})
    assert r.status_code == 200, r.json()
    assert r.json()["snapshot"]["ok"] is True
    assert snap.stat().st_mtime_ns > old_mtime - 10**9
    assert db_sources.DataSourceStore().get_table(tid).get("refreshed_at")


def test_mode_route_to_snapshot_refused_above_the_force_threshold(client, conn,
                                                                  monkeypatch):
    tid = _register_live(client, conn)          # stored: 2 rows x 2 columns
    _set_thresholds(monkeypatch, cell=3, force=4)
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "snapshot"})
    assert r.status_code == 400
    assert r.json()["code"] == "LIVE_REQUIRED"
    assert db_sources.DataSourceStore().get_table(tid)["mode"] == "live"
    assert not local_store.db_snapshot_path(tid).exists()


def test_mode_route_same_mode_is_200_and_audits_nothing(client, conn):
    tid = _register_snapshot(client, conn)
    before = db_sources.DataSourceStore().get_table(tid)
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "snapshot"})
    assert r.status_code == 200, r.json()
    assert _mode_rows() == []
    assert db_sources.DataSourceStore().get_table(tid) == before


# ===========================================================================
# GET /api/admin/tables — derived fields
# ===========================================================================
def test_table_list_rows_carry_the_derived_verdict(client, conn, monkeypatch):
    tid = _register_snapshot(client, conn)      # row_count 2, 2 columns
    _set_thresholds(monkeypatch, cell=3)
    rows = client.get("/api/admin/tables").json()["tables"]
    row = next(r for r in rows if r["id"] == tid)
    assert row["mode"] == "snapshot"
    assert row["cell_count"] == 4
    assert row["live_suggested"] is True
    assert row["live_required"] is False
    _set_thresholds(monkeypatch, force=4)
    row = next(r for r in client.get("/api/admin/tables").json()["tables"]
               if r["id"] == tid)
    assert row["live_required"] is True


def test_table_list_live_row_reports_live(client, conn):
    tid = _register_live(client, conn)
    row = next(r for r in client.get("/api/admin/tables").json()["tables"]
               if r["id"] == tid)
    assert row["mode"] == "live"
    assert row["cell_count"] == 4


# ===========================================================================
# refresh routes
# ===========================================================================
def test_refresh_now_on_a_live_table_reprofiles_without_a_parquet(client, conn):
    tid = _register_live(client, conn)
    store = db_sources.DataSourceStore()
    doc = store.get_table(tid)
    doc["live_profiled_at"] = "2000-01-01T00:00:00+00:00"
    store.upsert_table(doc, actor=ADMIN)
    r = client.post(f"/api/admin/tables/{tid}/refresh", json={})
    assert r.status_code == 200, r.json()
    out = r.json()
    assert out["ok"] is True
    assert out["live_profile"]["ok"] is True
    assert not local_store.db_snapshot_path(tid).exists()
    after = store.get_table(tid)
    assert after["live_profiled_at"] != "2000-01-01T00:00:00+00:00"
    assert after["mode"] == "live"


def test_connection_refresh_skips_live_tables(client, conn):
    snap_tid = _register_snapshot(client, conn, table="t")
    live_tid = _register_live(client, conn, table="u")
    r = client.post(f"/api/admin/connections/{conn}/refresh", json={})
    assert r.status_code == 200, r.json()
    results = {x["table_id"]: x for x in r.json()["results"]}
    assert results[live_tid].get("skipped") == "live"
    assert not local_store.db_snapshot_path(live_tid).exists()
    assert results[snap_tid]["ok"] is True
    assert results[snap_tid].get("skipped") in (None, "")


# ===========================================================================
# POST /api/admin/relations/recommendations/accept — force refusal
# ===========================================================================
def test_recommendation_accept_refuses_a_table_above_the_force_threshold(
        client, tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    db = _mk_sqlite_db(tmp_path, "rec.db", tables=("t",))
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE regions (region_id INTEGER PRIMARY KEY, "
                       "rname TEXT)"))
        for i in range(1, 4):
            c.execute(text(f"INSERT INTO regions VALUES ({i}, 'R{i}')"))
    eng.dispose()
    store = db_sources.DataSourceStore()
    cid = store.create_connection(
        {"name": "R", "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{db}"}, "pw", actor=ADMIN)["id"]
    store.upsert_table({
        "connection_id": cid, "schema": "", "table_name": "t",
        "display_name": "T", "description": "",
        "columns": [{"name": "a", "dtype": "", "description": "a"},
                    {"name": "b", "dtype": "", "description": "b"}],
        "is_connector": False, "relations": [],
        "descriptions_confirmed_by": ADMIN,
        "descriptions_confirmed_at": "2026-08-01T00:00:00+00:00"}, actor=ADMIN)
    client.post("/api/admin/relations/analyze_sql", json={
        "db_type": "sqlite",
        "sql": "SELECT 1 FROM t JOIN regions r ON t.a = r.region_id"})
    recs = store.list_recommendations()
    assert len(recs) == 1 and recs[0]["table"] == "regions"
    rid = recs[0]["id"]
    tables_before = {t["id"] for t in store.list_tables()}
    _set_thresholds(monkeypatch, cell=1, force=1)       # regions: 3 x 2 cells
    r = client.post("/api/admin/relations/recommendations/accept",
                    json={"id": rid})
    assert r.status_code == 400
    assert r.json()["code"] == "LIVE_REQUIRED"
    assert {t["id"] for t in store.list_tables()} == tables_before
    assert store.list_recommendations()[0]["status"] == "open"


# ===========================================================================
# editing an existing table: the size verdict is advisory, the mode is kept
# ===========================================================================
def _timed_out_count(monkeypatch):
    import routes.admin_data as admin_mod
    monkeypatch.setattr(admin_mod.db_connector, "count_rows",
                        lambda *a, **k: {"ok": False, "count": None,
                                         "timed_out": True, "error": "x"},
                        raising=False)


def _edit(client, tid, cid, **extra):
    return client.post(f"/api/admin/tables/{tid}",
                       json=_table_body(cid, **extra))


def test_edit_of_a_snapshot_table_above_force_keeps_snapshot(client, conn,
                                                            monkeypatch):
    tid = _register_snapshot(client, conn)
    _set_thresholds(monkeypatch, cell=3, force=4)
    r = _edit(client, tid, conn, mode="snapshot", description="edited")
    assert r.status_code == 200, r.json()
    out = r.json()
    assert "size_verdict" in out, "the save response carries no size_verdict"
    assert out["size_verdict"]["live_required"] is True
    doc = db_sources.DataSourceStore().get_table(tid)
    assert doc["mode"] == "snapshot"
    assert doc["description"] == "edited"
    assert _mode_rows() == []


def test_edit_without_mode_above_force_keeps_snapshot(client, conn, monkeypatch):
    tid = _register_snapshot(client, conn)
    _set_thresholds(monkeypatch, cell=3, force=4)
    r = _edit(client, tid, conn, description="edited")
    assert r.status_code == 200, r.json()
    assert db_sources.DataSourceStore().get_table(tid)["mode"] == "snapshot"


def test_edit_with_a_count_timeout_keeps_the_stored_mode(client, conn,
                                                         monkeypatch):
    tid = _register_snapshot(client, conn)
    _timed_out_count(monkeypatch)
    r = _edit(client, tid, conn, description="edited")
    assert r.status_code == 200, r.json()
    assert db_sources.DataSourceStore().get_table(tid)["mode"] == "snapshot"
    assert r.json().get("size_verdict", {}).get("timed_out") is True


def test_edit_posting_live_on_a_snapshot_table_flips_and_stamps(client, conn,
                                                                monkeypatch):
    tid = _register_snapshot(client, conn)
    _set_thresholds(monkeypatch, cell=3, force=4)
    r = _edit(client, tid, conn, mode="live")
    assert r.status_code == 200, r.json()
    doc = db_sources.DataSourceStore().get_table(tid)
    assert doc["mode"] == "live"
    assert doc["live_set_by"] == ADMIN
    assert doc["live_set_at"]


def test_edit_posting_snapshot_on_a_live_table_above_force_is_refused(
        client, conn, monkeypatch):
    tid = _register_live(client, conn)
    _set_thresholds(monkeypatch, cell=3, force=4)
    r = _edit(client, tid, conn, mode="snapshot")
    assert r.status_code == 400
    assert r.json()["code"] == "LIVE_REQUIRED"
    assert db_sources.DataSourceStore().get_table(tid)["mode"] == "live"


def test_new_registration_response_carries_the_size_verdict(client, conn):
    r = client.post("/api/admin/tables", json=_table_body(conn))
    assert r.status_code == 201, r.json()
    v = r.json().get("size_verdict")
    assert isinstance(v, dict), "the save response carries no size_verdict"
    assert v["row_count"] == 2
    assert v["cell_count"] == 4
    assert v["live_required"] is False


# ===========================================================================
# POST /tables/{tid}/mode -> snapshot re-counts before switching
# ===========================================================================
def _forget_stored_row_count(tid):
    store = db_sources.DataSourceStore()
    doc = store.get_table(tid)
    doc["row_count"] = None
    store.upsert_table(doc, actor=ADMIN)
    assert store.get_table(tid).get("row_count") is None
    return store


def test_mode_route_to_snapshot_recounts_and_refuses_a_timeout(client, conn,
                                                              monkeypatch):
    tid = _register_live(client, conn)
    store = _forget_stored_row_count(tid)
    _timed_out_count(monkeypatch)
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "snapshot"})
    assert r.status_code == 400, r.json()
    assert r.json()["code"] == "LIVE_REQUIRED"
    assert store.get_table(tid)["mode"] == "live"
    assert not local_store.db_snapshot_path(tid).exists()


def test_mode_route_to_snapshot_with_a_healthy_count_succeeds(client, conn):
    tid = _register_live(client, conn)
    store = _forget_stored_row_count(tid)
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "snapshot"})
    assert r.status_code == 200, r.json()
    assert store.get_table(tid)["mode"] == "snapshot"


# ===========================================================================
# POST /tables/{tid}/mode -> snapshot reverts to live when the snapshot fails
# ===========================================================================
def test_mode_route_reverts_to_live_when_the_snapshot_fails(client, conn,
                                                           monkeypatch):
    import db_scheduler
    tid = _register_live(client, conn)
    monkeypatch.setattr(db_scheduler, "refresh_one_table",
                        lambda *a, **k: {"ok": False, "error": "boom"})
    before = len(_mode_rows())
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "snapshot"})
    assert r.status_code == 200, r.json()
    out = r.json()
    assert out.get("reverted") is True
    assert out["snapshot"]["ok"] is False
    store = db_sources.DataSourceStore()
    assert store.get_table(tid)["mode"] == "live"
    assert out["table"]["mode"] == "live"
    rows = _mode_rows()
    new = rows[:len(rows) - before]
    assert len(new) == 2, new
    assert sorted((x["detail"]["from"], x["detail"]["to"]) for x in new) == \
        [("live", "snapshot"), ("snapshot", "live")]
    assert tid not in {t["id"] for t in store.list_tables(include_live=False)}


# ===========================================================================
# edit-save keeps where_filter / row_cap unless the body names them
# ===========================================================================
def test_edit_without_filter_keys_keeps_where_and_row_cap(client, conn):
    r = client.post("/api/admin/tables",
                    json=_table_body(conn, where_filter="a > 0", row_cap=1))
    assert r.status_code == 201, r.json()
    tid = r.json()["table"]["id"]
    r = _edit(client, tid, conn, description="edited")
    assert r.status_code == 200, r.json()
    doc = db_sources.DataSourceStore().get_table(tid)
    assert doc["where_filter"] == "a > 0"
    assert doc["row_cap"] == 1
    assert doc["description"] == "edited"


def test_edit_with_explicit_null_clears_where_and_row_cap(client, conn):
    r = client.post("/api/admin/tables",
                    json=_table_body(conn, where_filter="a > 0", row_cap=1))
    assert r.status_code == 201, r.json()
    tid = r.json()["table"]["id"]
    r = _edit(client, tid, conn, where_filter=None, row_cap=None)
    assert r.status_code == 200, r.json()
    doc = db_sources.DataSourceStore().get_table(tid)
    assert doc.get("where_filter") is None
    assert doc.get("row_cap") is None


# ===========================================================================
# _size_verdict: a count timeout under a row cap
# ===========================================================================
_TWO_COLS = {"columns": [{"name": "a"}, {"name": "b"}]}


def test_verdict_timeout_under_a_small_cap_is_not_required(monkeypatch):
    import routes.admin_data as admin_mod
    _set_thresholds(monkeypatch, cell=3, force=10)
    v = admin_mod._size_verdict(_TWO_COLS, {"ok": False, "timed_out": True},
                                row_cap=1)
    assert v["live_required"] is False
    assert v["live_suggested"] is False
    assert v["count_source"] == "cap"
    assert v["row_count"] == 1
    assert v["timed_out"] is True


def test_verdict_timeout_under_a_large_cap_is_required(monkeypatch):
    import routes.admin_data as admin_mod
    _set_thresholds(monkeypatch, cell=3, force=10)
    v = admin_mod._size_verdict(_TWO_COLS, {"ok": False, "timed_out": True},
                                row_cap=100)
    assert v["live_required"] is True


def test_verdict_timeout_without_a_cap_is_required(monkeypatch):
    import routes.admin_data as admin_mod
    _set_thresholds(monkeypatch, cell=3, force=10)
    v = admin_mod._size_verdict(_TWO_COLS, {"ok": False, "timed_out": True})
    assert v["live_required"] is True
    assert v["timed_out"] is True


# ===========================================================================
# the flip back to snapshot clears every live bookkeeping key
# ===========================================================================
_FIVE_LIVE_KEYS = ("live_reason", "live_set_by", "live_set_at",
                   "live_profiled_at", "live_sample_rows")


def test_mode_route_back_to_snapshot_clears_the_five_live_keys(client, conn):
    tid = _register_live(client, conn)
    store = db_sources.DataSourceStore()
    doc = store.get_table(tid)
    assert doc.get("live_profiled_at") and doc.get("live_sample_rows")
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "snapshot"})
    assert r.status_code == 200, r.json()
    doc = store.get_table(tid)
    assert doc["mode"] == "snapshot"
    for key in _FIVE_LIVE_KEYS:
        assert key not in doc, key


def test_store_flip_back_to_snapshot_clears_the_five_live_keys(store):
    tid = _store_table(store)["id"]
    store.set_table_mode(tid, "live", actor=ADMIN, reason="manual", cell_count=4)
    store.mark_live_profiled(tid, row_count=2,
                             columns=[{"name": "a"}, {"name": "b"}],
                             profiled_at="2026-09-26T00:00:00+00:00",
                             sample_rows=2)
    assert store.set_table_mode(tid, "snapshot", actor=ADMIN) is True
    doc = store.get_table(tid)
    for key in _FIVE_LIVE_KEYS:
        assert key not in doc, key


# ===========================================================================
# a failed live sample keeps the registration (three routes)
# ===========================================================================
def _failing_sample(monkeypatch):
    import db_connector
    monkeypatch.setattr(db_connector, "sample_rows",
                        lambda *a, **k: {"ok": False, "df": None, "error": "x"})


def test_save_live_keeps_the_registration_when_the_sample_fails(client, conn,
                                                                monkeypatch):
    _failing_sample(monkeypatch)
    r = client.post("/api/admin/tables", json=_table_body(conn, mode="live"))
    assert r.status_code == 201, r.json()
    out = r.json()
    assert out["live_profile"]["ok"] is False
    tid = out["table"]["id"]
    assert db_sources.DataSourceStore().get_table(tid)["mode"] == "live"
    assert not local_store.db_snapshot_path(tid).exists()
    assert not local_store.db_profile_path(tid).exists()


def test_mode_route_to_live_keeps_live_when_the_sample_fails(client, conn,
                                                             monkeypatch):
    tid = _register_snapshot(client, conn)
    _failing_sample(monkeypatch)
    r = client.post(f"/api/admin/tables/{tid}/mode", json={"mode": "live"})
    assert r.status_code == 200, r.json()
    assert r.json()["live_profile"]["ok"] is False
    assert db_sources.DataSourceStore().get_table(tid)["mode"] == "live"


def test_refresh_now_on_a_live_table_reports_a_failed_sample(client, conn,
                                                             monkeypatch):
    tid = _register_live(client, conn)
    _failing_sample(monkeypatch)
    r = client.post(f"/api/admin/tables/{tid}/refresh", json={})
    assert r.status_code == 200, r.json()
    assert r.json()["live_profile"]["ok"] is False
    assert db_sources.DataSourceStore().get_table(tid)["mode"] == "live"
    assert not local_store.db_snapshot_path(tid).exists()
