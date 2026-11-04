"""Personal (self-service) database connections — store level, offline.

Pins the storage half of the feature: the per-user `self_service_sources`
profile flag (absent = on), the connection doc's optional `owner`, the
capability `roles_store.self_service_allowed`, the ONE scope resolver
`roles_store.manager_scope_for` (power manage grants ∪ owned connections),
the defensive exclusion of personal tables from role access / management /
the connector closure, the removal cascade `delete_personal_sources`, and
the old-shape regression (a registry written before `owner` existed loads
and lists exactly as before)."""
import json

import pytest
from cryptography.fernet import Fernet

import db_sources
import local_store
import roles_store
from settings import settings

ACTOR = "ladmin"
OWNER = "owner@x.com"
OTHER = "other@x.com"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin", raising=False)
    yield


def _conn(name, owner=None):
    store = db_sources.DataSourceStore()
    kw = {"owner": owner} if owner is not None else {}
    return store.create_connection(
        {"name": name, "db_type": "postgresql", "host": "h", "port": 5432,
         "database": "d", "user": "u"}, "pw", actor=ACTOR, **kw)


def _table(cid, name, *, schema="shop", connector=False, registered_by=None,
           relations=None):
    doc = {"connection_id": cid, "schema": schema, "table_name": name,
           "display_name": name, "description": "", "is_connector": connector,
           "relations": relations or [], "columns": []}
    if registered_by:
        doc["registered_by"] = registered_by
    return db_sources.DataSourceStore().upsert_table(doc, actor=ACTOR)


def _user(email, role=None):
    auth = local_store.AuthStore()
    auth.ensure_user(email)
    if role:
        auth.set_role(email, role)


def _audit(action):
    return [r for r in db_sources.read_audit_tail(500) if r["action"] == action]


# ── profile flag ────────────────────────────────────────────────────────────

def test_self_service_flag_absent_reads_true_and_set_false_persists():
    _user(OWNER)
    auth = local_store.AuthStore()
    assert auth.get_self_service(OWNER) is True
    assert "self_service_sources" not in (auth.get_profile(OWNER) or {})
    assert auth.set_self_service(OWNER, False) is True
    assert auth.get_self_service(OWNER) is False
    assert auth.get_profile(OWNER)["self_service_sources"] is False
    assert auth.set_self_service(OWNER, True) is True
    assert auth.get_self_service(OWNER) is True


def test_set_self_service_missing_account_writes_nothing(tmp_path):
    auth = local_store.AuthStore()
    assert auth.set_self_service("ghost@x.com", False) is False
    assert not (tmp_path / "users" / "ghost@x.com").exists()
    # An absent account still reads as "on" (the default), like an absent flag.
    assert auth.get_self_service("ghost@x.com") is True


def test_get_self_service_fails_open_on_exception(monkeypatch):
    _user(OWNER)
    auth = local_store.AuthStore()
    monkeypatch.setattr(local_store.AuthStore, "get_profile",
                        lambda self, e: (_ for _ in ()).throw(RuntimeError("x")))
    assert auth.get_self_service(OWNER) is True


# ── connection owner ────────────────────────────────────────────────────────

def test_create_connection_owner_lower_cased_and_admin_has_none():
    mine = _conn("mine", owner="Owner@X.com")
    theirs = _conn("ladmins")
    assert mine["owner"] == OWNER
    assert "owner" not in theirs
    store = db_sources.DataSourceStore()
    listed = {c["id"]: c for c in store.list_connections()}
    assert listed[mine["id"]]["owner"] == OWNER         # masked shape keeps it
    assert "owner" not in listed[theirs["id"]]
    assert "password_enc" not in listed[mine["id"]]
    raw = store.get_connection(mine["id"], with_secret=True)
    assert raw["owner"] == OWNER


def test_personal_grants_for_one_null_schema_grant_per_owned_connection():
    a = _conn("a", owner=OWNER)
    b = _conn("b", owner="OWNER@x.com")
    _conn("c", owner=OTHER)
    _conn("d")
    store = db_sources.DataSourceStore()
    grants = store.personal_grants_for("Owner@X.com")
    assert sorted(g["connection_id"] for g in grants) == sorted([a["id"], b["id"]])
    assert all(g["schema"] is None for g in grants)
    assert store.personal_grants_for(OTHER) == [{"connection_id": store.list_connections()[2]["id"], "schema": None}]
    assert store.personal_grants_for("nobody@x.com") == []
    assert store.personal_grants_for("") == []


def test_personal_grants_for_unreadable_registry_is_empty(monkeypatch):
    _conn("a", owner=OWNER)
    monkeypatch.setattr(db_sources.DataSourceStore, "read_doc",
                        lambda self: (_ for _ in ()).throw(OSError("disk")))
    assert db_sources.DataSourceStore().personal_grants_for(OWNER) == []
    assert db_sources.DataSourceStore().personal_connection_ids() == set()


# ── capability ──────────────────────────────────────────────────────────────

def test_self_service_allowed_rules(monkeypatch):
    _user(OWNER)
    _user("adm@x.com", "admin")
    _user("pow@x.com", "power")
    assert roles_store.self_service_allowed(OWNER) is True
    assert roles_store.self_service_allowed("pow@x.com") is True
    local_store.AuthStore().set_self_service(OWNER, False)
    assert roles_store.self_service_allowed(OWNER) is False
    assert roles_store.self_service_allowed("adm@x.com") is False   # admin permission
    assert roles_store.self_service_allowed("ladmin") is False       # bootstrap
    assert roles_store.self_service_allowed("ghost@x.com") is False  # no account
    monkeypatch.setattr(local_store.AuthStore, "get_self_service",
                        lambda self, e: (_ for _ in ()).throw(RuntimeError("x")))
    assert roles_store.self_service_allowed("pow@x.com") is False    # fail closed


# ── scope resolver ──────────────────────────────────────────────────────────

def _grant(cid, schema=None):
    return {"connection_id": cid, "schema": schema}


def test_manager_scope_for_self_service_user():
    _user(OWNER)
    assert roles_store.manager_scope_for(OWNER) == []
    a = _conn("a", owner=OWNER)
    b = _conn("b", owner=OWNER)
    _conn("c", owner=OTHER)
    scope = roles_store.manager_scope_for(OWNER)
    assert sorted(g["connection_id"] for g in scope) == sorted([a["id"], b["id"]])
    assert all(g["schema"] is None for g in scope)
    # An unknown or admin account is neither power nor self-service.
    assert roles_store.manager_scope_for("ghost@x.com") is None
    _user("adm@x.com", "admin")
    _conn("x", owner="adm@x.com")
    assert roles_store.manager_scope_for("adm@x.com") is None


def test_manager_scope_for_power_user_union_and_switch_off():
    ladmin_conn = _conn("l")
    own = _conn("o", owner="pow@x.com")
    role = roles_store.RolesStore().create_role(
        {"name": "PU", "manage_grants": [_grant(ladmin_conn["id"], "shop"),
                                         _grant(own["id"])]}, actor=ACTOR)
    _user("pow@x.com", "power")
    local_store.AuthStore().set_data_role("pow@x.com", role["id"])
    scope = roles_store.manager_scope_for("pow@x.com")
    assert scope == [_grant(ladmin_conn["id"], "shop"), _grant(own["id"])]   # deduped
    # Switched off: the owned connection no longer contributes, grants stay.
    local_store.AuthStore().set_self_service("pow@x.com", False)
    assert roles_store.manager_scope_for("pow@x.com") == [_grant(ladmin_conn["id"], "shop"),
                                                          _grant(own["id"])]
    assert roles_store.management_scope_for("pow@x.com") == roles_store.manager_scope_for("pow@x.com")
    # Neither power nor self-service → None (the guard's denial value).
    _user(OTHER)
    local_store.AuthStore().set_self_service(OTHER, False)
    assert roles_store.manager_scope_for(OTHER) is None


def test_manager_scope_for_fails_closed(monkeypatch):
    _user(OWNER)
    monkeypatch.setattr(db_sources.DataSourceStore, "personal_grants_for",
                        lambda self, e: (_ for _ in ()).throw(RuntimeError("x")))
    assert roles_store.manager_scope_for(OWNER) is None


# ── personal tables never reach roles or the closure ────────────────────────

def test_effective_and_manageable_exclude_personal_tables():
    pc = _conn("p", owner=OWNER)
    lc = _conn("l")
    pt = _table(pc["id"], "pt", registered_by=OWNER)
    lt = _table(lc["id"], "lt")
    store = roles_store.RolesStore()
    role = store.create_role({"name": "R", "table_ids": [pt["id"], lt["id"]],
                              "scope_grants": [_grant(pc["id"]), _grant(lc["id"])],
                              "manage_grants": [_grant(pc["id"]), _grant(lc["id"])]},
                             actor=ACTOR)
    tables = db_sources.DataSourceStore().list_tables()
    assert roles_store.effective_table_ids(role, tables) == {lt["id"]}
    _user("pow@x.com", "power")
    local_store.AuthStore().set_data_role("pow@x.com", role["id"])
    assert roles_store.manageable_table_ids_for("pow@x.com") == {lt["id"]}
    # The member reads only ladmin's table through the role...
    _user(OTHER)
    local_store.AuthStore().set_data_role(OTHER, role["id"])
    assert roles_store.allowed_table_ids_for(OTHER) == {lt["id"]}
    # ...while the owner reads their own through the ownership read.
    _user(OWNER)
    assert pt["id"] in roles_store.allowed_table_ids_for(OWNER)


def test_expand_with_connectors_never_walks_a_personal_table():
    pc = _conn("p", owner=OWNER)
    lc = _conn("l")
    seed = _table(lc["id"], "seed")
    good = _table(lc["id"], "dict", connector=True,
                  relations=[{"related_table_id": seed["id"], "join_keys": []}])
    bad = _table(pc["id"], "pdict", connector=True,   # hand-edited doc shape
                 relations=[{"related_table_id": seed["id"], "join_keys": []}])
    closure = db_sources.expand_with_connectors([seed["id"]])
    assert good["id"] in closure
    assert bad["id"] not in closure
    # A personal seed never pulls a connector through either.
    pseed = _table(pc["id"], "pseed", registered_by=OWNER,
                   relations=[{"related_table_id": good["id"], "join_keys": []}])
    assert db_sources.expand_with_connectors([pseed["id"]]) == [pseed["id"]]


def test_personal_grant_refusal():
    pc = _conn("p", owner=OWNER)
    lc = _conn("l")
    pt = _table(pc["id"], "pt", registered_by=OWNER)
    lt = _table(lc["id"], "lt")
    store = db_sources.DataSourceStore()
    msg = "Personal connections cannot be granted to roles."
    assert roles_store.personal_grant_refusal({"scope_grants": [_grant(pc["id"])]}, store) == msg
    assert roles_store.personal_grant_refusal({"manage_grants": [_grant(pc["id"], "s")]}, store) == msg
    assert roles_store.personal_grant_refusal({"table_ids": [pt["id"]]}, store) == msg
    assert roles_store.personal_grant_refusal(
        {"table_ids": [lt["id"]], "scope_grants": [_grant(lc["id"])],
         "manage_grants": [_grant(lc["id"])]}, store) is None
    assert roles_store.personal_grant_refusal({}, store) is None


# ── removal cascade ─────────────────────────────────────────────────────────

def test_delete_personal_sources_removes_owned_connections_and_tables(tmp_path):
    a = _conn("a", owner=OWNER)
    b = _conn("b", owner="Owner@X.com")
    l = _conn("l")
    t1 = _table(a["id"], "t1", registered_by=OWNER)
    t2 = _table(a["id"], "t2", registered_by=OWNER, connector=True)
    t3 = _table(b["id"], "t3", registered_by=OWNER)
    lt = _table(l["id"], "lt")
    for t in (t1, t2, t3, lt):
        local_store.db_snapshot_path(t["id"]).write_bytes(b"parquet")
        local_store.db_profile_path(t["id"]).write_text("{}", encoding="utf-8")
    rstore = roles_store.RolesStore()
    role = rstore.create_role({"name": "R", "table_ids": [t1["id"], lt["id"]],
                               "scope_grants": [_grant(a["id"]), _grant(l["id"])],
                               "manage_grants": [_grant(b["id"])]}, actor=ACTOR)
    store = db_sources.DataSourceStore()
    res = store.delete_personal_sources("Owner@X.com", actor=ACTOR)
    assert res == {"connections": 2, "tables": 3}
    assert [c["id"] for c in store.list_connections()] == [l["id"]]
    assert [t["id"] for t in store.list_tables()] == [lt["id"]]
    for t in (t1, t2, t3):
        assert not local_store.db_snapshot_path(t["id"]).exists()
        assert not local_store.db_profile_path(t["id"]).exists()
    assert local_store.db_snapshot_path(lt["id"]).exists()
    assert local_store.db_profile_path(lt["id"]).exists()
    r = rstore.get_role(role["id"])
    assert r["table_ids"] == [lt["id"]]
    assert r["scope_grants"] == [_grant(l["id"])]
    assert r["manage_grants"] == []
    rows = _audit("personal_sources.deleted")
    assert len(rows) == 1
    assert rows[0]["actor"] == ACTOR and rows[0]["target"] == OWNER
    assert rows[0]["detail"]["connections"] == 2
    assert rows[0]["detail"]["tables"] == 3
    assert sorted(rows[0]["detail"]["connection_ids"]) == sorted([a["id"], b["id"]])
    assert {r["detail"].get("actor_kind") for r in _audit("table.delete")} == {"personal"}
    assert {r["detail"].get("actor_kind") for r in _audit("connection.delete")} == {"personal"}
    # Nothing to do → zeros, no audit row.
    assert store.delete_personal_sources(OWNER, actor=ACTOR) == {"connections": 0, "tables": 0}
    assert len(_audit("personal_sources.deleted")) == 1
    assert store.delete_personal_sources("", actor=ACTOR) == {"connections": 0, "tables": 0}


def test_delete_personal_sources_never_raises(monkeypatch):
    _conn("a", owner=OWNER)
    monkeypatch.setattr(db_sources.DataSourceStore, "read_doc",
                        lambda self: (_ for _ in ()).throw(OSError("disk")))
    assert db_sources.DataSourceStore().delete_personal_sources(OWNER, actor=ACTOR) == {
        "connections": 0, "tables": 0}


# ── settings ────────────────────────────────────────────────────────────────

def test_personal_tables_max_setting_default_and_floor(monkeypatch):
    assert settings.PERSONAL_TABLES_MAX == 15
    import settings as settings_mod
    monkeypatch.setenv("PERSONAL_TABLES_MAX", "0")
    assert settings_mod._int_env("PERSONAL_TABLES_MAX", 15, 1) == 1
    monkeypatch.setenv("PERSONAL_TABLES_MAX", "junk")
    assert settings_mod._int_env("PERSONAL_TABLES_MAX", 15, 1) == 15


# ── old-shape regression ────────────────────────────────────────────────────

def test_old_shape_registry_without_owner_loads_byte_identically(tmp_path):
    """A data_sources.json written before `owner` existed: every row lists
    exactly as stored (no key added), nothing is personal, the connector
    closure and role access behave as before."""
    doc = {"version": 1, "updated_at": None,
           "settings": {"refresh_time": "02:00", "timezone": "UTC"},
           "connections": [{
               "id": "aa11bb22cc33dd44", "name": "old", "db_type": "postgresql",
               "host": "h", "port": 5432, "database": "d", "service_name": None,
               "user": "u", "password_enc": "", "password_set_at": None,
               "ssl": False, "trust_server_certificate": False,
               "connect_timeout": 10, "statement_timeout": 60, "url_override": None,
               "created_at": "2026-01-01T00:00:00Z", "created_by": "ladmin",
               "updated_at": "2026-01-01T00:00:00Z", "updated_by": "ladmin",
               "last_test_at": None, "last_test_ok": None}],
           "tables": [
               {"id": "bb11bb22cc33dd44", "connection_id": "aa11bb22cc33dd44",
                "schema": "shop", "table_name": "seed", "display_name": "seed",
                "description": "", "is_connector": False, "columns": [],
                "relations": [{"related_table_id": "cc11bb22cc33dd44", "join_keys": []}],
                "registered_by": "u@x.com"},
               {"id": "cc11bb22cc33dd44", "connection_id": "aa11bb22cc33dd44",
                "schema": "shop", "table_name": "dict", "display_name": "dict",
                "description": "", "is_connector": True, "columns": [],
                "relations": []}],
           "recommendations": []}
    (tmp_path / "data_sources.json").write_text(json.dumps(doc), encoding="utf-8")
    store = db_sources.DataSourceStore()
    conns = store.list_connections()
    assert len(conns) == 1 and "owner" not in conns[0]
    expected = {k: v for k, v in doc["connections"][0].items()
                if k not in ("password_enc", "url_override")}
    expected.update({"password_set": False, "password_readable": False,
                     "password_masked": ""})
    assert conns[0] == expected
    assert store.list_tables() == doc["tables"]
    assert store.personal_connection_ids() == set()
    assert store.personal_grants_for("ladmin") == []
    assert db_sources.expand_with_connectors(["bb11bb22cc33dd44"]) == [
        "bb11bb22cc33dd44", "cc11bb22cc33dd44"]
    role = {"id": "r", "table_ids": [], "scope_grants": [_grant("aa11bb22cc33dd44")]}
    assert roles_store.effective_table_ids(role, store.list_tables()) == {"bb11bb22cc33dd44"}
    _user("u@x.com")
    assert roles_store.manager_scope_for("u@x.com") == []
    assert store.delete_personal_sources("u@x.com", actor=ACTOR) == {"connections": 0, "tables": 0}
    # The file itself was never rewritten by any of the reads above.
    assert json.loads((tmp_path / "data_sources.json").read_text(encoding="utf-8")) == doc
