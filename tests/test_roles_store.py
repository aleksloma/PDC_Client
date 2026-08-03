"""roles_store.RolesStore + effective-access rules: default/old-shape docs,
Base-role seeding, CRUD + audit rows, base-role protections, explicit vs
scope-grant access (case-insensitive schemas, null-schema whole-connection,
later-registered tables), connector exemption, dangling data_role → Base,
set_table_roles exact reconcile. Store-level, offline."""
import json

import pytest
from cryptography.fernet import Fernet

import db_sources
import local_store
import roles_store
from settings import settings

ACTOR = "ladmin"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    yield


@pytest.fixture
def registry():
    """One connection, two normal tables (schemas shop/hr) + one connector."""
    store = db_sources.DataSourceStore()
    conn = store.create_connection(
        {"name": "c", "db_type": "postgresql", "host": "h", "port": 5432,
         "database": "d", "user": "u"}, "pw", actor=ACTOR)
    ids = {"conn": conn["id"]}
    for tname, schema, is_conn in [("cl_info", "shop", False),
                                   ("payroll", "hr", False),
                                   ("city_dict", "shop", True)]:
        t = store.upsert_table({
            "connection_id": conn["id"], "schema": schema, "table_name": tname,
            "display_name": tname, "description": "", "is_connector": is_conn,
            "relations": [], "columns": [],
        }, actor=ACTOR)
        ids[tname] = t["id"]
    return ids


def _tables():
    return db_sources.DataSourceStore().list_tables()


# ── doc shapes ─────────────────────────────────────────────────────────────

def test_absent_roles_json_reads_default(tmp_path):
    store = roles_store.RolesStore()
    assert store.read_doc() == {"version": 1, "updated_at": None, "roles": []}
    assert roles_store.allowed_table_ids_for("u@x.com") == set()


def test_ensure_base_role_seeds_and_is_idempotent():
    store = roles_store.RolesStore()
    store.ensure_base_role()
    store.ensure_base_role()
    roles = store.list_roles()
    assert [r["id"] for r in roles] == ["base"]
    assert roles[0]["name"] == "Base"
    assert roles[0]["table_ids"] == [] and roles[0]["scope_grants"] == []


def test_old_shape_roles_json_keeps_loading(tmp_path):
    """A stored doc with foreign top-level keys and roles missing fields must
    load with defaults (stored-shape backward compatibility)."""
    doc = {"version": 1, "some_future_section": {"x": 1},
           "roles": [{"id": "aa11bb22cc33dd44", "name": "Old"},
                     "garbage-row", {"name": "no-id-dropped"}]}
    (tmp_path / "roles.json").write_text(json.dumps(doc), encoding="utf-8")
    store = roles_store.RolesStore()
    roles = store.list_roles()
    assert len(roles) == 1
    r = roles[0]
    assert r["name"] == "Old"
    assert r["table_ids"] == [] and r["scope_grants"] == []
    assert r["description"] == ""
    # Unparseable file → default doc, never a crash.
    (tmp_path / "roles.json").write_text("{not json", encoding="utf-8")
    assert store.read_doc()["roles"] == []


# ── CRUD + audit ───────────────────────────────────────────────────────────

def test_create_update_delete_role_and_audit_rows():
    store = roles_store.RolesStore()
    role = store.create_role({"name": "Finance", "description": "d"}, actor=ACTOR)
    assert len(role["id"]) == 16 and store.valid_id(role["id"])
    assert store.get_role(role["id"])["name"] == "Finance"

    updated = store.update_role(role["id"], {"name": "Fin2"}, actor=ACTOR)
    assert updated["name"] == "Fin2"
    # Absent fields are kept on update.
    assert updated["description"] == "d"

    assert store.delete_role(role["id"], actor=ACTOR) is True
    assert store.get_role(role["id"]) is None

    actions = [r["action"] for r in db_sources.read_audit_tail(50)]
    assert {"role.create", "role.update", "role.delete"} <= set(actions)


def test_delete_base_refused_and_base_rename_ignored():
    store = roles_store.RolesStore()
    store.ensure_base_role()
    assert store.delete_role("base", actor=ACTOR) is False
    out = store.update_role("base", {"name": "Hacked", "description": "ok"},
                            actor=ACTOR)
    assert out["name"] == "Base"          # rename silently ignored (route 400s)
    assert out["description"] == "ok"     # description stays editable
    assert store.get_role("base")["name"] == "Base"


def test_unknown_role_update_delete():
    store = roles_store.RolesStore()
    assert store.update_role("ffffffffffffffff", {"name": "x"}, actor=ACTOR) is None
    assert store.delete_role("ffffffffffffffff", actor=ACTOR) is False


# ── effective access ───────────────────────────────────────────────────────

def test_effective_explicit_ids_intersect_live_registry(registry):
    role = {"id": "r", "table_ids": [registry["cl_info"], "ffffffffffffffff"],
            "scope_grants": []}
    assert roles_store.effective_table_ids(role, _tables()) == {registry["cl_info"]}


def test_effective_scope_grant_schema_case_insensitive(registry):
    role = {"id": "r", "table_ids": [],
            "scope_grants": [{"connection_id": registry["conn"], "schema": "SHOP"}]}
    assert roles_store.effective_table_ids(role, _tables()) == {registry["cl_info"]}


def test_effective_null_schema_covers_whole_connection(registry):
    role = {"id": "r", "table_ids": [],
            "scope_grants": [{"connection_id": registry["conn"], "schema": None}]}
    assert roles_store.effective_table_ids(role, _tables()) == {
        registry["cl_info"], registry["payroll"]}


def test_scope_grant_covers_later_registered_table(registry):
    """The dynamic-access core: a table registered AFTER the grant is covered
    without any role edit."""
    store = roles_store.RolesStore()
    role = store.create_role(
        {"name": "Shop", "scope_grants": [
            {"connection_id": registry["conn"], "schema": "shop"}]}, actor=ACTOR)
    t_new = db_sources.DataSourceStore().upsert_table({
        "connection_id": registry["conn"], "schema": "shop",
        "table_name": "orders", "display_name": "orders", "description": "",
        "is_connector": False, "relations": [], "columns": [],
    }, actor=ACTOR)
    eff = roles_store.effective_table_ids(store.get_role(role["id"]), _tables())
    assert t_new["id"] in eff


def test_effective_excludes_connectors(registry):
    """Connectors are exempt by design — even an explicit grant is a no-op."""
    role = {"id": "r", "table_ids": [registry["city_dict"]],
            "scope_grants": [{"connection_id": registry["conn"], "schema": None}]}
    eff = roles_store.effective_table_ids(role, _tables())
    assert registry["city_dict"] not in eff


def test_effective_none_role_is_empty():
    assert roles_store.effective_table_ids(None, []) == set()


# ── role_for_email resolution ──────────────────────────────────────────────

def test_role_for_email_missing_and_dangling_resolve_to_base(registry):
    store = roles_store.RolesStore()
    store.ensure_base_role()
    auth = local_store.AuthStore()
    auth.ensure_user("u@x.com")
    # Missing data_role → Base.
    assert roles_store.role_for_email("u@x.com")["id"] == "base"
    # Dangling data_role (role deleted) → Base, no profile rewrite.
    role = store.create_role({"name": "Temp"}, actor=ACTOR)
    auth.set_data_role("u@x.com", role["id"])
    store.delete_role(role["id"], actor=ACTOR)
    assert roles_store.role_for_email("u@x.com")["id"] == "base"
    assert (auth.get_profile("u@x.com") or {}).get("data_role") == role["id"]


def test_role_for_email_stub_when_base_missing():
    """Even with no roles.json at all the resolver returns an empty Base stub
    — genuine denials fail closed through defaults."""
    role = roles_store.role_for_email("nobody@x.com")
    assert role["id"] == "base"
    assert role["table_ids"] == [] and role["scope_grants"] == []


# ── set_table_roles reconcile ──────────────────────────────────────────────

def test_set_table_roles_exact_reconcile(registry):
    store = roles_store.RolesStore()
    a = store.create_role({"name": "A"}, actor=ACTOR)
    b = store.create_role({"name": "B", "table_ids": [registry["cl_info"]]},
                          actor=ACTOR)
    tid = registry["cl_info"]
    # tid ends up in A (added) and out of B (removed).
    store.set_table_roles(tid, [a["id"]], actor=ACTOR)
    assert tid in store.get_role(a["id"])["table_ids"]
    assert tid not in store.get_role(b["id"])["table_ids"]
    # remove_table strips it everywhere.
    store.set_table_roles(tid, [a["id"], b["id"]], actor=ACTOR)
    store.remove_table(tid, actor=ACTOR)
    assert tid not in store.get_role(a["id"])["table_ids"]
    assert tid not in store.get_role(b["id"])["table_ids"]


def test_remove_connection_prunes_grants_and_cascaded_tables(registry):
    """Connection (cascade) delete cleanup: its scope grants AND the cascaded
    tables' ids leave every role; unrelated grants/ids survive."""
    store = roles_store.RolesStore()
    other_grant = {"connection_id": "ffffffffffffffff", "schema": None}
    role = store.create_role({
        "name": "Mixed",
        "table_ids": [registry["cl_info"], "aa11bb22cc33dd44"],
        "scope_grants": [{"connection_id": registry["conn"], "schema": "shop"},
                         other_grant]}, actor=ACTOR)
    store.remove_connection(registry["conn"], [registry["cl_info"]], actor=ACTOR)
    out = store.get_role(role["id"])
    assert out["scope_grants"] == [other_grant]
    assert out["table_ids"] == ["aa11bb22cc33dd44"]
    assert any(r["action"] == "role.prune_connection"
               for r in db_sources.read_audit_tail(50))
