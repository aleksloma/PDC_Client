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
    # version 2 = the 19f manage_grants split (fresh installs skip migration).
    assert store.read_doc() == {"version": 2, "updated_at": None, "roles": []}
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


# ── power user (prompts 19 + 19e: permission on the USER) ──────────────────

def _mk_power_user(email, grants, name="PU"):
    """Grant `email` the power PERMISSION plus a role carrying the given
    MANAGE grants (19f: capability from AuthStore, WHERE from the roles'
    manage_grants — scope_grants are the read axis)."""
    store = roles_store.RolesStore()
    role = store.create_role({"name": name, "manage_grants": grants}, actor=ACTOR)
    auth = local_store.AuthStore()
    auth.ensure_user(email)
    auth.set_role(email, "power")
    auth.set_data_role(email, role["id"])
    return role


def test_remove_poweruser_role_boot_cleanup(tmp_path):
    """A roles.json still carrying the 19c-era seeded poweruser role loses it
    at boot (idempotent); members holding the id resolve to Base."""
    doc = {"version": 1, "roles": [
        {"id": "base", "name": "Base", "power_user": False},
        {"id": "poweruser", "name": "PowerUser", "power_user": True},
        {"id": "aa11bb22cc33dd44", "name": "Custom"}]}
    (tmp_path / "roles.json").write_text(json.dumps(doc), encoding="utf-8")
    store = roles_store.RolesStore()
    auth = local_store.AuthStore()
    auth.ensure_user("held@x.com")
    auth.set_data_roles("held@x.com", ["poweruser"])
    store.remove_poweruser_role()
    store.remove_poweruser_role()          # idempotent
    assert [r["id"] for r in store.list_roles()] == ["base", "aa11bb22cc33dd44"]
    # The dangling held id resolves to Base — no capability, no crash.
    assert [r["id"] for r in roles_store.roles_for_email("held@x.com")] == ["base"]
    assert roles_store.is_power_user("held@x.com") is False


def test_power_user_flag_dropped_on_old_docs(tmp_path):
    """A 19c-era doc storing power_user loads with the key silently absent
    (the capability lives on the user now)."""
    doc = {"version": 1, "roles": [
        {"id": "aa11bb22cc33dd44", "name": "Old", "power_user": True}]}
    (tmp_path / "roles.json").write_text(json.dumps(doc), encoding="utf-8")
    r = roles_store.RolesStore().list_roles()[0]
    assert "power_user" not in r


def test_management_scope_none_without_power_permission(registry):
    """Grants alone never grant management — the permission decides WHETHER.
    Admin is NOT power (config-only)."""
    store = roles_store.RolesStore()
    store.ensure_base_role()
    auth = local_store.AuthStore()
    auth.ensure_user("plain@x.com")
    assert roles_store.management_scope_for("plain@x.com") is None
    role = store.create_role(
        {"name": "Read", "scope_grants": [
            {"connection_id": registry["conn"], "schema": None}]}, actor=ACTOR)
    auth.set_data_role("plain@x.com", role["id"])
    assert roles_store.management_scope_for("plain@x.com") is None
    assert roles_store.can_manage_physical("plain@x.com",
                                           registry["conn"], "shop") is False
    assert roles_store.manageable_table_ids_for("plain@x.com") == set()
    # Admin permission is not the power capability.
    auth.set_role("plain@x.com", "admin")
    assert roles_store.is_power_user("plain@x.com") is False
    assert roles_store.management_scope_for("plain@x.com") is None


def test_management_scope_connection_grant(registry):
    _mk_power_user("pu@x.com", [{"connection_id": registry["conn"]}])
    scope = roles_store.management_scope_for("pu@x.com")
    assert scope == [{"connection_id": registry["conn"], "schema": None}]
    assert roles_store.can_manage_physical("pu@x.com", registry["conn"], "shop")
    assert roles_store.can_manage_physical("pu@x.com", registry["conn"], None)
    assert not roles_store.can_manage_physical("pu@x.com",
                                               "ffffffffffffffff", "shop")
    # Connectors are INCLUDED in the manageable set (unlike read access).
    assert roles_store.manageable_table_ids_for("pu@x.com") == {
        registry["cl_info"], registry["payroll"], registry["city_dict"]}


def test_management_scope_schema_grant_case_insensitive(registry):
    _mk_power_user("pu2@x.com",
                   [{"connection_id": registry["conn"], "schema": "SHOP"}])
    assert roles_store.can_manage_physical("pu2@x.com", registry["conn"], "shop")
    assert roles_store.can_manage_physical("pu2@x.com", registry["conn"], "Shop")
    assert not roles_store.can_manage_physical("pu2@x.com",
                                               registry["conn"], "hr")
    assert roles_store.manageable_table_ids_for("pu2@x.com") == {
        registry["cl_info"], registry["city_dict"]}


def test_management_scope_empty_grants_is_empty_not_none():
    """A power role with no grants IS a power user (scope []) — the page
    opens, everything is out of scope."""
    _mk_power_user("pu3@x.com", [])
    assert roles_store.management_scope_for("pu3@x.com") == []
    assert roles_store.manageable_table_ids_for("pu3@x.com") == set()


def test_roles_for_email_multi_legacy_and_dangling(registry):
    store = roles_store.RolesStore()
    store.ensure_base_role()
    auth = local_store.AuthStore()
    a = store.create_role({"name": "A"}, actor=ACTOR)
    b = store.create_role({"name": "B"}, actor=ACTOR)
    auth.ensure_user("multi@x.com")
    auth.set_data_roles("multi@x.com", [a["id"], b["id"], "ffffffffffffffff"])
    held = roles_store.roles_for_email("multi@x.com")
    assert [r["id"] for r in held] == [a["id"], b["id"]]   # dangling dropped
    assert roles_store.role_for_email("multi@x.com")["id"] == a["id"]  # shim
    # Nothing held → [Base]; every id dangling → [Base] too.
    auth.ensure_user("none@x.com")
    assert [r["id"] for r in roles_store.roles_for_email("none@x.com")] == ["base"]
    auth.set_data_roles("none@x.com", ["ffffffffffffffff"])
    assert [r["id"] for r in roles_store.roles_for_email("none@x.com")] == ["base"]


def test_allowed_table_ids_union_across_roles(registry):
    """19c: read access is the UNION of the held roles' effective sets."""
    store = roles_store.RolesStore()
    shop = store.create_role({"name": "Shop", "scope_grants": [
        {"connection_id": registry["conn"], "schema": "shop"}]}, actor=ACTOR)
    hr = store.create_role({"name": "HR",
                            "table_ids": [registry["payroll"]]}, actor=ACTOR)
    auth = local_store.AuthStore()
    auth.ensure_user("both@x.com")
    auth.set_data_roles("both@x.com", [shop["id"], hr["id"]])
    assert roles_store.allowed_table_ids_for("both@x.com") == {
        registry["cl_info"], registry["payroll"]}


def test_promoted_admin_reads_via_roles_like_any_user(registry):
    """19g: an admin-PERMISSION user's chat read access follows their held
    roles — no implicit all-tables read, no admin special case."""
    store = roles_store.RolesStore()
    hr = store.create_role({"name": "HRonly",
                            "table_ids": [registry["payroll"]]}, actor=ACTOR)
    auth = local_store.AuthStore()
    auth.ensure_user("padmin@x.com")
    auth.set_role("padmin@x.com", "admin")
    auth.set_data_roles("padmin@x.com", [hr["id"]])
    assert roles_store.allowed_table_ids_for("padmin@x.com") == {
        registry["payroll"]}
    # No roles held → Base → empty, admin permission notwithstanding.
    auth.ensure_user("padmin2@x.com")
    auth.set_role("padmin2@x.com", "admin")
    assert roles_store.allowed_table_ids_for("padmin2@x.com") == set()


def test_capability_from_permission_scope_from_role_union(registry):
    """The 19e/19f separation: the power PERMISSION is the capability; WHERE
    is the union of manage_grants across ALL held roles."""
    store = roles_store.RolesStore()
    shop = store.create_role({"name": "ShopAccess", "manage_grants": [
        {"connection_id": registry["conn"], "schema": "shop"}]}, actor=ACTOR)
    auth = local_store.AuthStore()
    auth.ensure_user("stack@x.com")
    auth.set_role("stack@x.com", "power")
    auth.set_data_roles("stack@x.com", [shop["id"]])
    assert roles_store.is_power_user("stack@x.com") is True
    scope = roles_store.management_scope_for("stack@x.com")
    assert scope == [{"connection_id": registry["conn"], "schema": "shop"}]
    assert roles_store.can_manage_physical("stack@x.com",
                                           registry["conn"], "SHOP")
    assert roles_store.manageable_table_ids_for("stack@x.com") == {
        registry["cl_info"], registry["city_dict"]}
    # Same roles at STANDARD permission → no capability, no scope.
    auth.ensure_user("nopower@x.com")
    auth.set_data_roles("nopower@x.com", [shop["id"]])
    assert roles_store.is_power_user("nopower@x.com") is False
    assert roles_store.management_scope_for("nopower@x.com") is None
    # Union dedupes identical grants across roles.
    shop2 = store.create_role({"name": "Shop2", "manage_grants": [
        {"connection_id": registry["conn"], "schema": "SHOP"},
        {"connection_id": registry["conn"]}]}, actor=ACTOR)
    auth.set_data_roles("stack@x.com", [shop["id"], shop2["id"]])
    scope = roles_store.management_scope_for("stack@x.com")
    assert scope == [{"connection_id": registry["conn"], "schema": "shop"},
                     {"connection_id": registry["conn"], "schema": None}]


# ── 19f: read vs manage separation ─────────────────────────────────────────

def test_scope_grants_no_longer_grant_management(registry):
    """A read schema grant alone gives a POWER user no management scope —
    the two axes are separate (19f)."""
    store = roles_store.RolesStore()
    role = store.create_role({"name": "ReadOnly", "scope_grants": [
        {"connection_id": registry["conn"], "schema": "shop"}]}, actor=ACTOR)
    auth = local_store.AuthStore()
    auth.ensure_user("ro@x.com")
    auth.set_role("ro@x.com", "power")
    auth.set_data_role("ro@x.com", role["id"])
    assert roles_store.management_scope_for("ro@x.com") == []
    assert roles_store.manageable_table_ids_for("ro@x.com") == set()
    # Read access via the scope grant is untouched.
    assert registry["cl_info"] in roles_store.allowed_table_ids_for("ro@x.com")


def test_manage_grants_never_grant_read(registry):
    """A manage grant alone exposes nothing in the chat picker."""
    _mk_power_user("mg@x.com", [{"connection_id": registry["conn"],
                                 "schema": "shop"}])
    assert roles_store.allowed_table_ids_for("mg@x.com") == set()
    # ...while the management scope covers the schema.
    assert roles_store.can_manage_physical("mg@x.com", registry["conn"], "shop")


def test_ownership_read_included(registry):
    """19f ownership read: a user's own registrations are always readable
    (case-insensitive registered_by), other users' are not, connectors never."""
    store = db_sources.DataSourceStore()
    mine = store.upsert_table({
        "connection_id": registry["conn"], "schema": "shop",
        "table_name": "my_tbl", "display_name": "my tbl", "description": "",
        "is_connector": False, "relations": [], "columns": [],
        "registered_by": "Owner@X.com"}, actor=ACTOR)
    conn_own = store.upsert_table({
        "connection_id": registry["conn"], "schema": "shop",
        "table_name": "my_conn", "display_name": "my conn", "description": "",
        "is_connector": True, "relations": [], "columns": [],
        "registered_by": "owner@x.com"}, actor=ACTOR)
    local_store.AuthStore().ensure_user("owner@x.com")
    allowed = roles_store.allowed_table_ids_for("owner@x.com")
    assert mine["id"] in allowed                    # own registration
    assert conn_own["id"] not in allowed            # connectors stay exempt
    # Another user without any grant sees neither.
    local_store.AuthStore().ensure_user("other@x.com")
    assert mine["id"] not in roles_store.allowed_table_ids_for("other@x.com")


def test_manage_grants_normalize_and_old_doc(tmp_path):
    """Old-shape docs load with manage_grants defaulting []; malformed
    entries are dropped like scope grants."""
    doc = {"version": 2, "roles": [
        {"id": "aa11bb22cc33dd44", "name": "Old"},
        {"id": "bb22cc33dd44ee55", "name": "M",
         "manage_grants": [{"connection_id": "cc33dd44ee55ff66", "schema": "s"},
                           "garbage", {"connection_id": "short"}]}]}
    (tmp_path / "roles.json").write_text(json.dumps(doc), encoding="utf-8")
    roles = roles_store.RolesStore().list_roles()
    assert roles[0]["manage_grants"] == []
    assert roles[1]["manage_grants"] == [
        {"connection_id": "cc33dd44ee55ff66", "schema": "s"}]


def test_migrate_manage_grants_once(tmp_path):
    """The v1→v2 boot migration copies scope_grants into manage_grants ONCE;
    a v2 doc is never touched, so later divergence survives."""
    g = {"connection_id": "aa11bb22cc33dd44", "schema": "shop"}
    doc = {"version": 1, "roles": [
        {"id": "base", "name": "Base"},
        {"id": "bb22cc33dd44ee55", "name": "R", "scope_grants": [g]}]}
    (tmp_path / "roles.json").write_text(json.dumps(doc), encoding="utf-8")
    store = roles_store.RolesStore()
    store.migrate_manage_grants()
    out = store.read_doc()
    assert out["version"] == 2
    by_id = {r["id"]: r for r in out["roles"]}
    assert by_id["bb22cc33dd44ee55"]["manage_grants"] == [g]
    assert by_id["bb22cc33dd44ee55"]["scope_grants"] == [g]   # read kept
    assert by_id["base"]["manage_grants"] == []
    # Diverge, re-run: the migration must NOT re-copy.
    store.update_role("bb22cc33dd44ee55", {"manage_grants": []}, actor=ACTOR)
    store.migrate_manage_grants()
    assert store.get_role("bb22cc33dd44ee55")["manage_grants"] == []
    # A fresh install (no file) starts at v2 — nothing to migrate.
    assert roles_store.RolesStore().read_doc()["version"] == 2


def test_explicit_table_ids_grant_no_management(registry):
    """table_ids = read access ONLY — never management rights, even with the
    power permission."""
    store = roles_store.RolesStore()
    role = store.create_role({"name": "R",
                              "table_ids": [registry["cl_info"]]}, actor=ACTOR)
    auth = local_store.AuthStore()
    auth.ensure_user("pu4@x.com")
    auth.set_role("pu4@x.com", "power")
    auth.set_data_role("pu4@x.com", role["id"])
    assert roles_store.management_scope_for("pu4@x.com") == []
    assert roles_store.manageable_table_ids_for("pu4@x.com") == set()
