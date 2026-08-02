"""data_sources.json registry: Fernet encryption at rest, masking in every
API-facing shape, key-missing/rotation behavior, atomic writes, old-shape
backward compatibility, audit scrubbing, and the connector closure."""
import json

import pytest
from cryptography.fernet import Fernet

import db_sources
from settings import settings


KEY = Fernet.generate_key().decode()
KEY2 = Fernet.generate_key().decode()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db_sources.settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(db_sources.settings, "CLIENT_ENCRYPTION_KEY", KEY)
    monkeypatch.setattr(db_sources.settings, "CLIENT_ENCRYPTION_KEY_OLD", "")
    return db_sources.DataSourceStore()


def _mk_conn(store, password="s3cret-pw"):
    return store.create_connection(
        {"name": "DWH", "db_type": "postgresql", "host": "db.local",
         "port": 5432, "database": "dw", "user": "ro"},
        password, actor="ladmin")


def test_create_connection_encrypts_and_masks(store, tmp_path):
    masked = _mk_conn(store)
    # Response shape: masked, no ciphertext, no plaintext.
    assert "password_enc" not in masked
    assert masked["password_set"] is True
    assert masked["password_readable"] is True
    assert masked["password_masked"] == "••••••••"
    # On-disk: ciphertext only — the plaintext never appears in the file bytes.
    raw = (tmp_path / "data_sources.json").read_text(encoding="utf-8")
    assert "s3cret-pw" not in raw
    assert "password_enc" in raw


def test_decrypt_roundtrip(store):
    _mk_conn(store, password="round-trip-pw")
    cid = store.list_connections()[0]["id"]
    full = store.get_connection(cid, with_secret=True)
    assert db_sources.decrypt_password(full["password_enc"]) == "round-trip-pw"


def test_missing_encryption_key_refuses_write(tmp_path, monkeypatch):
    monkeypatch.setattr(db_sources.settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(db_sources.settings, "CLIENT_ENCRYPTION_KEY", "")
    store = db_sources.DataSourceStore()
    with pytest.raises(db_sources.EncryptionUnavailable):
        _mk_conn(store)
    # Nothing persisted.
    assert store.list_connections() == []


def test_rotated_key_marks_password_unreadable(store, monkeypatch):
    _mk_conn(store)
    monkeypatch.setattr(db_sources.settings, "CLIENT_ENCRYPTION_KEY", KEY2)
    rows = store.list_connections()
    assert rows[0]["password_set"] is True
    assert rows[0]["password_readable"] is False  # never deleted, just unreadable


def test_old_key_fallback_decrypts(store, monkeypatch):
    _mk_conn(store, password="old-key-pw")
    monkeypatch.setattr(db_sources.settings, "CLIENT_ENCRYPTION_KEY", KEY2)
    monkeypatch.setattr(db_sources.settings, "CLIENT_ENCRYPTION_KEY_OLD", KEY)
    cid = store.list_connections()[0]["id"]
    full = store.get_connection(cid, with_secret=True)
    assert db_sources.decrypt_password(full["password_enc"]) == "old-key-pw"


def test_atomic_write_leaves_no_tmp(store, tmp_path):
    _mk_conn(store)
    leftovers = [p for p in tmp_path.iterdir() if ".tmp-" in p.name]
    assert leftovers == []


def test_old_shape_doc_loads_with_defaults(tmp_path, monkeypatch):
    """A doc written by an older release (minimal keys) must load with
    defaults — stored-format backward compatibility (CLAUDE.md data safety)."""
    monkeypatch.setattr(db_sources.settings, "DATA_ROOT", str(tmp_path))
    (tmp_path / "data_sources.json").write_text(
        json.dumps({"connections": [{"id": "aa11bb22cc33dd44", "name": "legacy"}]}),
        encoding="utf-8")
    store = db_sources.DataSourceStore()
    doc = store.read_doc()
    assert doc["version"] == 1
    assert doc["settings"]["refresh_time"]
    assert doc["tables"] == []
    rows = store.list_connections()
    assert rows[0]["name"] == "legacy"
    assert rows[0]["password_set"] is False


def test_audit_append_scrubs_secrets(store, tmp_path):
    db_sources.audit("ladmin", "connection.test", target="x",
                     detail={"nested": {"password": "TOPSECRET", "host": "db"},
                             "url": "postgres://u:TOPSECRET@h/db"})
    lines = (tmp_path / "admin_audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert "TOPSECRET" not in lines[0]
    assert row["detail"]["nested"]["password"] == "***"
    assert row["detail"]["url"] == "***"
    assert row["detail"]["nested"]["host"] == "db"


def test_delete_connection_blocked_by_dependent_tables(store):
    masked = _mk_conn(store)
    cid = masked["id"]
    store.upsert_table({"connection_id": cid, "schema": "s", "table_name": "t",
                        "display_name": "t", "columns": []}, actor="ladmin")
    res = store.delete_connection(cid, actor="ladmin")
    assert res["ok"] is False and res["tables"]
    res2 = store.delete_connection(cid, actor="ladmin", cascade=True)
    assert res2["ok"] is True and len(res2["deleted_tables"]) == 1
    assert store.list_connections() == []
    assert store.list_tables() == []


def test_refresh_settings_validation(store):
    with pytest.raises(ValueError):
        store.set_refresh_settings(refresh_time="25:00", refresh_enabled=True, actor="ladmin")
    out = store.set_refresh_settings(refresh_time="02:30", refresh_enabled=False, actor="ladmin")
    assert out["refresh_time"] == "02:30" and out["refresh_enabled"] is False
    assert store.get_refresh_settings()["refresh_time"] == "02:30"


def test_mark_refreshed_error_keeps_previous_refreshed_at(store):
    masked = _mk_conn(store)
    t = store.upsert_table({"connection_id": masked["id"], "schema": "s",
                            "table_name": "t", "display_name": "t", "columns": []},
                           actor="ladmin")
    store.mark_refreshed(t["id"], rows=10, snapshot_bytes=100)
    first = store.get_table(t["id"])["refreshed_at"]
    assert first
    store.mark_refreshed(t["id"], error="boom")
    after = store.get_table(t["id"])
    assert after["refreshed_at"] == first          # last good timestamp kept
    assert after["last_refresh_error"] == "boom"


# ---------------------------------------------------------------------------
# Connector transitive closure
# ---------------------------------------------------------------------------

def _seed_tables(store, cid):
    """A(normal) -rel-> C1(connector) -rel-> C2(connector); A -rel-> B(normal);
    D(normal) declares nothing but C3(connector) declares a relation TO D."""
    ids = {}
    for name, is_conn, rels in [
        ("A", False, [{"related_table": "C1"}, {"related_table": "B"}]),
        ("B", False, []),
        ("C1", True, [{"related_table": "C2"}]),
        ("C2", True, [{"related_table": "C1"}]),   # cycle back
        ("D", False, []),
        ("C3", True, [{"related_table": "D"}]),    # reverse-declared
    ]:
        t = store.upsert_table({"connection_id": cid, "schema": "s",
                                "table_name": name, "display_name": name,
                                "is_connector": is_conn, "relations": rels,
                                "columns": []}, actor="ladmin")
        ids[name] = t["id"]
    return ids


def test_connector_closure(store):
    cid = _mk_conn(store)["id"]
    ids = _seed_tables(store, cid)

    # Transitive: A pulls C1, then C1 pulls C2. B is a NORMAL table — never
    # auto-included. Cycle C1<->C2 terminates.
    out = db_sources.expand_with_connectors([ids["A"]], store)
    assert set(out) == {ids["A"], ids["C1"], ids["C2"]}
    assert out == sorted(out)  # deterministic

    # Reverse-declared relation: selecting D finds C3 through the undirected index.
    out_d = db_sources.expand_with_connectors([ids["D"]], store)
    assert set(out_d) == {ids["D"], ids["C3"]}


def test_connector_closure_dangling_and_cap(store, monkeypatch):
    cid = _mk_conn(store)["id"]
    t = store.upsert_table({"connection_id": cid, "schema": "s",
                            "table_name": "X", "display_name": "X",
                            "relations": [{"related_table": "does_not_exist"}],
                            "columns": []}, actor="ladmin")
    # Dangling relation is skipped, not fatal.
    assert db_sources.expand_with_connectors([t["id"]], store) == [t["id"]]

    ids = _seed_tables(store, cid)
    monkeypatch.setattr(db_sources.settings, "DB_MAX_AUTO_CONNECTORS", 1)
    out = db_sources.expand_with_connectors([ids["A"]], store)
    # Cap 1: only one connector auto-added.
    assert len([i for i in out if i != ids["A"]]) == 1


# ---------------------------------------------------------------------------
# Recommended tables (v4)
# ---------------------------------------------------------------------------

def _rec_item(cid, table="city_dict", source="sql", frequency=2, count=2,
              other=None, pairs=None):
    return {"connection_id": cid, "schema": "shop", "table": table,
            "source": source, "frequency": frequency,
            "evidence": [{"origin": source,
                          "other": other or {"schema": "shop", "table": "cl_info"},
                          "pairs": pairs or [["city_code", "city_code"]],
                          "count": count}]}


def test_recommendations_merge_accumulate_and_union(store):
    cid = _mk_conn(store)["id"]
    r1 = store.upsert_recommendations([_rec_item(cid)], actor="ladmin")
    assert r1 == {"created": 1, "updated": 0}
    # Same physical table, different case + fk source + one identical and one
    # new evidence entry: frequencies accumulate, evidence unions, counts add.
    item2 = {"connection_id": cid, "schema": "SHOP", "table": "City_Dict",
             "source": "fk", "frequency": 1,
             "evidence": [
                 {"origin": "sql", "other": {"schema": "shop", "table": "cl_info"},
                  "pairs": [["city_code", "city_code"]], "count": 3},
                 {"origin": "fk", "other": {"schema": "shop", "table": "tr_data"},
                  "pairs": [["city_code", "city_code"]], "count": 0}]}
    r2 = store.upsert_recommendations([item2], actor="ladmin")
    assert r2 == {"created": 0, "updated": 1}
    recs = store.list_recommendations()
    assert len(recs) == 1
    rec = recs[0]
    assert rec["status"] == "open"
    assert rec["sources"] == ["sql", "fk"]
    assert rec["frequency"] == 3
    sql_ev = [e for e in rec["evidence"] if e["origin"] == "sql"]
    assert len(sql_ev) == 1 and sql_ev[0]["count"] == 5      # 2 + 3
    assert len(rec["evidence"]) == 2


def test_recommendations_dismiss_sticky_and_restore(store):
    cid = _mk_conn(store)["id"]
    store.upsert_recommendations([_rec_item(cid)], actor="ladmin")
    rid = store.list_recommendations()[0]["id"]
    assert store.set_recommendation_status(rid, "dismissed", "ladmin")["status"] == "dismissed"
    # Re-analyze: evidence still merges, status stays dismissed.
    store.upsert_recommendations([_rec_item(cid, frequency=4)], actor="ladmin")
    rec = store.list_recommendations()[0]
    assert rec["status"] == "dismissed" and rec["frequency"] == 6
    # Restore.
    assert store.set_recommendation_status(rid, "open", "ladmin")["status"] == "open"
    # Bogus status refused.
    assert store.set_recommendation_status(rid, "registered", "ladmin") is None


def test_recommendations_skip_creation_for_registered_physical(store):
    cid = _mk_conn(store)["id"]
    store.upsert_table({"connection_id": cid, "schema": "shop",
                        "table_name": "city_dict", "display_name": "cd",
                        "columns": []}, actor="ladmin")
    out = store.upsert_recommendations([_rec_item(cid)], actor="ladmin")
    assert out == {"created": 0, "updated": 0}
    assert store.list_recommendations() == []


def test_recommendations_transition_and_prior_status_revert(store):
    cid = _mk_conn(store)["id"]
    store.upsert_recommendations([_rec_item(cid)], actor="ladmin")
    rid = store.list_recommendations()[0]["id"]
    store.set_recommendation_status(rid, "dismissed", "ladmin")
    # ANY registration path (store-level hook) flips it to registered.
    t = store.upsert_table({"connection_id": cid, "schema": "Shop",
                            "table_name": "CITY_DICT", "display_name": "cd",
                            "columns": []}, actor="ladmin")
    rec = store.list_recommendations()[0]
    assert rec["status"] == "registered"
    assert rec["registered_table_id"] == t["id"]
    # Registered recs are untouched by further upserts.
    assert store.upsert_recommendations([_rec_item(cid)], actor="ladmin") == \
        {"created": 0, "updated": 0}
    # Table deleted -> reverts to PRIOR status (dismissed, not open).
    store.delete_table(t["id"], actor="ladmin")
    rec = store.list_recommendations()[0]
    assert rec["status"] == "dismissed"
    assert "prior_status" not in rec and "registered_table_id" not in rec


def test_recommendations_connection_cascade_drops_recs(store):
    cid = _mk_conn(store)["id"]
    store.upsert_recommendations([_rec_item(cid)], actor="ladmin")
    assert store.list_recommendations()
    store.delete_connection(cid, actor="ladmin", cascade=True)
    assert store.list_recommendations() == []


def test_recommendations_old_shape_doc_defaults_section(tmp_path, monkeypatch):
    monkeypatch.setattr(db_sources.settings, "DATA_ROOT", str(tmp_path))
    (tmp_path / "data_sources.json").write_text(
        json.dumps({"connections": [], "tables": []}), encoding="utf-8")
    store = db_sources.DataSourceStore()
    assert store.read_doc()["recommendations"] == []
    assert store.list_recommendations() == []


def test_recommendations_audit_counts_only(store, tmp_path):
    cid = _mk_conn(store)["id"]
    store.upsert_recommendations([_rec_item(cid)], actor="ladmin")
    rows = [r for r in db_sources.read_audit_tail(50)
            if r["action"] == "relations.recommend"]
    assert rows and rows[0]["detail"] == {"created": 1, "updated": 0}

