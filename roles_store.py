"""Roles registry — which registered DB tables each user role may use.

Storage: DATA_ROOT/roles.json — one JSON document holding `roles`:
  {id, name, description, table_ids: [], scope_grants: [{connection_id,
   schema|null}], manage_grants: [{connection_id, schema|null}],
   created_at, updated_at}

A user holds SEVERAL roles (19c: profile.json `data_roles`, legacy single
`data_role` reads as a one-element list; empty resolves to the built-in Base
role). READ ACCESS is the union of `effective_table_ids` across the held
roles — explicit `table_ids` plus `scope_grants` (the deliberate opt-in
"every table on this connection/schema, present AND future" read choice: a
grant with schema=null covers the whole connection). Effective access is
computed dynamically at request time (`allowed_table_ids_for`), so a table
registered later under a granted schema is covered without a role edit and
grant changes propagate instantly. `allowed_table_ids_for` ALSO includes the
user's OWN registrations (`registered_by` == email, non-connector) — a power
user always reads what they registered, before any role share. Connector
tables are exempt from role checks by design — they are invisible to users
and auto-included for joins (db_sources.expand_with_connectors); gating them
would silently break allowed joins.

Discipline mirrors db_sources.DataSourceStore: every mutation under _LOCK with
an atomic whole-file replace, section-whitelisting read_doc so an old or
foreign-shaped doc keeps loading after an upgrade, 16-hex ids. The built-in
Base role has the literal id "base" (seeded at boot, undeletable, unrenamable).
Missing roles.json / missing data_role / a dangling data_role all resolve to
Base — deleting a role reverts its members with NO profile rewrites.

Power users (19e/19f): the capability is a per-USER PERMISSION, not a role
property — AuthStore's profile `role` field holds "user" | "power" | "admin",
and `is_power_user` here delegates to `AuthStore.is_power` (admin is NOT
power: the local admin is config-only and uses the admin page). The
permission decides WHETHER the user may manage data sources (register tables,
define relations, set per-table refresh schedules — routes/admin_data.py's
_require_source_manager gate); WHERE they may manage is the union of
`manage_grants` across ALL their held roles (`management_scope_for`) — a
SEPARATE axis from read since 19f, so opening a schema for management no
longer force-exposes its tables to the whole role. `migrate_manage_grants`
(boot, doc version 1 → 2) copies each role's scope_grants into manage_grants
ONCE so an upgrade preserves behavior exactly; afterwards the two lists
diverge freely. Explicit table_ids grant READ access only, never management.
The 19c-era `power_user` role flag is dropped silently on read and the
once-seeded built-in "poweruser" role is removed at boot
(`remove_poweruser_role`).

Leaf-ish module: imports only settings/logger/local_store/db_sources helpers;
nothing imports it back at module scope.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from pathlib import Path
from typing import Optional

from settings import settings
from logger_utils import log_with_sid
from local_store import _data_root, _now, _write_json_atomic

_LOCK = threading.RLock()

_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")

_DOC_NAME = "roles.json"

BASE_ROLE_ID = "base"
# The 19c-era built-in capability role, removed in 19e — kept only so the
# boot cleanup can find and delete a doc seeded by an older build.
_LEGACY_POWERUSER_ID = "poweruser"


def _roles_path() -> Path:
    return _data_root() / _DOC_NAME


def _default_doc() -> dict:
    # version 2 = manage_grants split from scope_grants (19f). Fresh docs
    # start at 2 so migrate_manage_grants never touches them.
    return {"version": 2, "updated_at": None, "roles": []}


def _base_role_doc() -> dict:
    now = _now()
    return {
        "id": BASE_ROLE_ID,
        "name": "Base",
        "description": "Default role for all users.",
        "table_ids": [],
        "scope_grants": [],
        "manage_grants": [],
        "created_at": now,
        "updated_at": now,
    }


def _normalize_grant(g) -> Optional[dict]:
    """One scope grant -> {"connection_id": str, "schema": str|None}, or None
    when malformed. schema=None means the whole connection; "" is a legal
    literal schema (sqlite) and stays distinct from None."""
    if not isinstance(g, dict):
        return None
    cid = g.get("connection_id")
    if not isinstance(cid, str) or not _ID_RE.match(cid):
        return None
    schema = g.get("schema", None)
    if schema is not None and not isinstance(schema, str):
        return None
    return {"connection_id": cid, "schema": schema}


def _normalize_role(r: dict) -> dict:
    """Defaults for every field so an old-shape stored role keeps loading.
    The 19c-era `power_user` flag is deliberately NOT carried — a doc that
    still stores it loads with the key silently absent (19e moved the
    capability to the per-user permission)."""
    return {
        "id": str(r.get("id") or ""),
        "name": str(r.get("name") or ""),
        "description": str(r.get("description") or ""),
        "table_ids": [t for t in (r.get("table_ids") or []) if isinstance(t, str)],
        "scope_grants": [g for g in map(_normalize_grant, r.get("scope_grants") or [])
                         if g is not None],
        "manage_grants": [g for g in map(_normalize_grant, r.get("manage_grants") or [])
                          if g is not None],
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


class RolesStore:
    """roles.json registry — DataSourceStore discipline (locked atomic writes,
    whitelisting reads, .get() defaults)."""

    @staticmethod
    def valid_id(value) -> bool:
        return (value == BASE_ROLE_ID
                or bool(isinstance(value, str) and _ID_RE.match(value)))

    # ---- doc ---------------------------------------------------------------
    def read_doc(self) -> dict:
        p = _roles_path()
        if not p.exists():
            return _default_doc()
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                return _default_doc()
        except Exception as e:
            log_with_sid("roles", "error", f"ROLES_READ_FAILED: {e}")
            return _default_doc()
        base = _default_doc()
        base["version"] = doc.get("version", 1)
        base["updated_at"] = doc.get("updated_at")
        base["roles"] = [_normalize_role(r) for r in (doc.get("roles") or [])
                         if isinstance(r, dict) and r.get("id")]
        return base

    def _write_doc(self, doc: dict) -> None:
        doc["updated_at"] = _now()
        _write_json_atomic(_roles_path(), doc)

    # ---- boot seed ---------------------------------------------------------
    def ensure_base_role(self) -> None:
        """Idempotent boot-time seed of the built-in Base role. Never raises
        (Article IV) — a failed seed still resolves to Base via the
        role_for_email stub fallback."""
        try:
            with _LOCK:
                doc = self.read_doc()
                if any(r["id"] == BASE_ROLE_ID for r in doc["roles"]):
                    return
                doc["roles"].insert(0, _base_role_doc())
                self._write_doc(doc)
            log_with_sid("startup", "info", "ROLES_BASE_SEEDED")
        except Exception as e:
            log_with_sid("startup", "error", f"ROLES_BASE_SEED_FAILED: {e}")

    def remove_poweruser_role(self) -> None:
        """Idempotent boot-time cleanup mirroring the 19c seed it replaces:
        a roles.json still carrying the built-in "poweruser" role (written by
        an older build) loses it. Members holding the id go dangling and are
        dropped at read time (roles_for_email) — no profile rewrites. Never
        raises (Article IV)."""
        try:
            with _LOCK:
                doc = self.read_doc()
                if not any(r["id"] == _LEGACY_POWERUSER_ID for r in doc["roles"]):
                    return
                doc["roles"] = [r for r in doc["roles"]
                                if r["id"] != _LEGACY_POWERUSER_ID]
                self._write_doc(doc)
            log_with_sid("startup", "info", "POWERUSER_ROLE_REMOVED")
        except Exception as e:
            log_with_sid("startup", "error", f"POWERUSER_ROLE_REMOVE_FAILED: {e}")

    def migrate_manage_grants(self) -> None:
        """One-time versioned upgrade (doc version 1 → 2, 19f): copy every
        role's scope_grants into the new manage_grants list, preserving the
        pre-split behavior exactly — a schema grant used to mean read AND
        manage, so on upgrade it still does; afterwards ladmin edits the two
        lists independently. Idempotent via the version stamp (a v2 doc is
        never touched, so later divergence is never re-overwritten). Never
        raises (Article IV) — a failed migration just means empty manage
        scopes (fail-closed) until the next boot."""
        try:
            with _LOCK:
                doc = self.read_doc()
                if doc.get("version", 1) >= 2:
                    return
                for role in doc["roles"]:
                    role["manage_grants"] = [dict(g) for g in role["scope_grants"]]
                doc["version"] = 2
                self._write_doc(doc)
            log_with_sid("startup", "info",
                         f"ROLES_MANAGE_GRANTS_MIGRATED roles={len(doc['roles'])}")
        except Exception as e:
            log_with_sid("startup", "error", f"ROLES_MANAGE_MIGRATE_FAILED: {e}")

    # ---- reads -------------------------------------------------------------
    def list_roles(self) -> list[dict]:
        return [dict(r) for r in self.read_doc()["roles"]]

    def get_role(self, rid) -> Optional[dict]:
        if not self.valid_id(rid):
            return None
        for r in self.read_doc()["roles"]:
            if r["id"] == rid:
                return dict(r)
        return None

    # ---- mutations (all audited via db_sources.audit) ----------------------
    def create_role(self, fields: dict, actor: str) -> dict:
        from db_sources import audit
        role = _normalize_role(fields or {})
        role["id"] = secrets.token_hex(8)
        role["created_at"] = role["updated_at"] = _now()
        with _LOCK:
            doc = self.read_doc()
            doc["roles"].append(role)
            self._write_doc(doc)
        audit(actor, "role.create", target=role["id"],
              detail={"name": role["name"], "tables": len(role["table_ids"]),
                      "grants": len(role["scope_grants"]),
                      "manage_grants": len(role["manage_grants"])})
        return dict(role)

    def update_role(self, rid: str, fields: dict, actor: str) -> Optional[dict]:
        """Fields present in `fields` replace the stored value; absent fields
        are kept. The built-in Base role's name is immutable — a rename is
        ignored here (the route also 400s it); description and grants stay
        editable."""
        from db_sources import audit
        fields = fields or {}
        with _LOCK:
            doc = self.read_doc()
            role = next((r for r in doc["roles"] if r["id"] == rid), None)
            if role is None:
                return None
            if "name" in fields and rid != BASE_ROLE_ID:
                role["name"] = str(fields.get("name") or "")
            if "description" in fields:
                role["description"] = str(fields.get("description") or "")
            if "table_ids" in fields:
                role["table_ids"] = [t for t in (fields.get("table_ids") or [])
                                     if isinstance(t, str)]
            if "scope_grants" in fields:
                role["scope_grants"] = [g for g in map(_normalize_grant,
                                                       fields.get("scope_grants") or [])
                                        if g is not None]
            if "manage_grants" in fields:
                role["manage_grants"] = [g for g in map(_normalize_grant,
                                                        fields.get("manage_grants") or [])
                                         if g is not None]
            role["updated_at"] = _now()
            self._write_doc(doc)
            out = dict(role)
        audit(actor, "role.update", target=rid,
              detail={"name": out["name"], "tables": len(out["table_ids"]),
                      "grants": len(out["scope_grants"]),
                      "manage_grants": len(out["manage_grants"])})
        return out

    def delete_role(self, rid: str, actor: str) -> bool:
        """False for the built-in Base role or an unknown id. Members revert
        to Base dynamically (role_for_email) — no profile rewrites."""
        from db_sources import audit
        if rid == BASE_ROLE_ID:
            return False
        name = ""
        with _LOCK:
            doc = self.read_doc()
            role = next((r for r in doc["roles"] if r["id"] == rid), None)
            if role is None:
                return False
            name = role["name"]
            doc["roles"] = [r for r in doc["roles"] if r["id"] != rid]
            self._write_doc(doc)
        audit(actor, "role.delete", target=rid, detail={"name": name})
        return True

    def set_table_roles(self, table_id: str, role_ids: list[str], actor: str,
                        actor_kind: Optional[str] = None) -> None:
        """Exact reconcile for one table: after this call, `table_id` is in a
        role's table_ids iff the role is listed in `role_ids`. One locked
        write. Used by the register/edit wizard's Access panel (canonical
        storage lives HERE, never on the table doc). `actor_kind` labels
        delegated power-user writes in the audit row (None ⇒ ladmin rows stay
        byte-identical)."""
        from db_sources import audit
        wanted = {r for r in (role_ids or []) if isinstance(r, str)}
        with _LOCK:
            doc = self.read_doc()
            changed = False
            for role in doc["roles"]:
                has = table_id in role["table_ids"]
                want = role["id"] in wanted
                if want and not has:
                    role["table_ids"].append(table_id)
                elif has and not want:
                    role["table_ids"] = [t for t in role["table_ids"] if t != table_id]
                else:
                    continue
                role["updated_at"] = _now()
                changed = True
            if changed:
                self._write_doc(doc)
        if changed:
            audit(actor, "role.set_tables", target=table_id,
                  detail={"roles": sorted(wanted)}, actor_kind=actor_kind)

    def remove_table(self, table_id: str, actor: str) -> None:
        """Strip a deleted table's id from every role (best-effort cleanup —
        a stale id grants nothing, but would clutter the roles UI)."""
        self.set_table_roles(table_id, [], actor)

    def remove_connection(self, connection_id: str, table_ids, actor: str) -> None:
        """Cleanup for a deleted connection (incl. cascade): strip its scope
        grants and the cascaded tables' ids from every role in ONE locked
        write. Best-effort like remove_table — stale entries grant nothing
        (effective access intersects the live registry) but would clutter the
        roles UI forever."""
        from db_sources import audit
        gone = {t for t in (table_ids or []) if isinstance(t, str)}
        changed = False
        with _LOCK:
            doc = self.read_doc()
            for role in doc["roles"]:
                grants = [g for g in role["scope_grants"]
                          if g["connection_id"] != connection_id]
                manage = [g for g in role["manage_grants"]
                          if g["connection_id"] != connection_id]
                tables = [t for t in role["table_ids"] if t not in gone]
                if (len(grants) == len(role["scope_grants"])
                        and len(manage) == len(role["manage_grants"])
                        and len(tables) == len(role["table_ids"])):
                    continue
                role["scope_grants"] = grants
                role["manage_grants"] = manage
                role["table_ids"] = tables
                role["updated_at"] = _now()
                changed = True
            if changed:
                self._write_doc(doc)
        if changed:
            audit(actor, "role.prune_connection", target=connection_id,
                  detail={"tables": sorted(gone)})


# ---------------------------------------------------------------------------
# Effective access — computed dynamically at request time
# ---------------------------------------------------------------------------

def _grant_matches(grant: dict, connection_id, schema) -> bool:
    """One normalized scope grant vs a physical (connection, schema): schema
    compare is case-insensitive, a table schema of None/"" reads as "", and a
    None-schema grant covers the whole connection."""
    if grant.get("connection_id") != connection_id:
        return False
    g_schema = grant.get("schema")
    return g_schema is None or str(g_schema).lower() == str(schema or "").lower()


def effective_table_ids(role: Optional[dict], tables: list[dict]) -> set:
    """The set of NON-CONNECTOR registry table ids this role may use: explicit
    table_ids intersected with the live registry, plus every table matched by
    a scope grant (connection match AND schema match, case-insensitive; a
    None-schema grant covers the whole connection)."""
    if not isinstance(role, dict):
        return set()
    explicit = {t for t in (role.get("table_ids") or []) if isinstance(t, str)}
    grants = [g for g in map(_normalize_grant, role.get("scope_grants") or [])
              if g is not None]
    out: set = set()
    for t in tables or []:
        tid = t.get("id")
        if not tid or t.get("is_connector"):
            continue
        if tid in explicit:
            out.add(tid)
            continue
        if any(_grant_matches(g, t.get("connection_id"), t.get("schema"))
               for g in grants):
            out.add(tid)
    return out


def roles_for_email(email: str) -> list:
    """ALL of the user's held role docs (19c multi-role model). Dangling ids
    are dropped at read time (role delete rewrites no profiles); no held
    roles / an unreadable store resolve to [Base]; a missing Base role
    resolves to an empty stub — genuine denials always fail closed through
    defaults."""
    from local_store import AuthStore
    store = RolesStore()
    by_id = {r["id"]: r for r in store.read_doc()["roles"]}
    out = [dict(by_id[rid]) for rid in AuthStore().get_data_roles(email)
           if rid in by_id]
    if not out:
        base = by_id.get(BASE_ROLE_ID)
        out = [dict(base) if base else
               {"id": BASE_ROLE_ID, "name": "Base", "description": "",
                "table_ids": [], "scope_grants": [], "manage_grants": []}]
    return out


def role_for_email(email: str) -> dict:
    """Single-role view kept for legacy call sites/tests: the FIRST held role
    (or the Base fallback). New code wants roles_for_email."""
    return roles_for_email(email)[0]


def allowed_table_ids_for(email: str) -> set:
    """Non-connector registry table ids the user may pick/refresh right now —
    the UNION of effective access across all their held roles, PLUS the
    user's own registrations (19f ownership read: `registered_by` == email,
    case-insensitive) so a power user always sees what they registered even
    before sharing it with any role. Connectors stay excluded either way."""
    from db_sources import DataSourceStore
    tables = DataSourceStore().list_tables()
    out: set = set()
    for role in roles_for_email(email):
        out |= effective_table_ids(role, tables)
    me = str(email or "").strip().lower()
    if me:
        out |= {t["id"] for t in tables
                if t.get("id") and not t.get("is_connector")
                and str(t.get("registered_by") or "").strip().lower() == me}
    return out


# ---------------------------------------------------------------------------
# Management scope — what a POWER USER may register/manage (prompts 19 + 19e)
# ---------------------------------------------------------------------------

def is_power_user(email: str) -> bool:
    """The pure CAPABILITY: the user's per-account PERMISSION is "power"
    (AuthStore profile `role`; admin is NOT power — config-only). Fails
    closed (Article IV) — an unreadable profile means no capability."""
    try:
        from local_store import AuthStore
        return AuthStore().is_power(email)
    except Exception as e:
        log_with_sid(email, "error", f"POWER_CAPABILITY_FAILED: {e}")
        return False


def management_scope_for(email: str) -> Optional[list]:
    """The normalized grants this user may MANAGE data sources inside —
    None unless the user's permission is "power" (incl. every failure path:
    this gate fails closed, Article IV). The permission decides WHETHER;
    WHERE is the union of `manage_grants` across ALL held roles (deduped,
    order-preserving) — since 19f a SEPARATE axis from the read
    `scope_grants`, which no longer contribute here. Explicit table_ids
    grant READ access only, never management rights."""
    try:
        if not is_power_user(email):
            return None
        out: list = []
        seen: set = set()
        for role in roles_for_email(email):
            for g in map(_normalize_grant, role.get("manage_grants") or []):
                if g is None:
                    continue
                key = (g["connection_id"],
                       None if g["schema"] is None else str(g["schema"]).lower())
                if key in seen:
                    continue
                seen.add(key)
                out.append(g)
        return out
    except Exception as e:
        log_with_sid(email, "error", f"MANAGEMENT_SCOPE_FAILED: {e}")
        return None


def scope_covers(scope, connection_id, schema) -> bool:
    """True iff any grant in an already-resolved management scope covers the
    physical (connection, schema). For callers holding the guard's scope —
    can_manage_physical is the email-resolving variant."""
    return any(_grant_matches(g, connection_id, schema) for g in scope or [])


def can_manage_physical(email: str, connection_id, schema) -> bool:
    """True iff the user is a power user AND (connection_id, schema) falls
    inside their management scope (schema case-insensitive; a None-schema
    grant covers the connection). Always False without the power permission —
    ladmin is handled by the route guards, never here."""
    scope = management_scope_for(email)
    if scope is None:
        return False
    return scope_covers(scope, connection_id, schema)


def manageable_table_ids_for(email: str) -> set:
    """Registry table ids — CONNECTORS INCLUDED, unlike effective_table_ids
    (the management page lists connector registrations too) — whose
    (connection, schema) fall inside the user's management scope. Empty when
    the user is not a power user."""
    from db_sources import DataSourceStore
    scope = management_scope_for(email)
    if scope is None:
        return set()
    out: set = set()
    for t in DataSourceStore().list_tables():
        tid = t.get("id")
        if tid and any(_grant_matches(g, t.get("connection_id"), t.get("schema"))
                       for g in scope):
            out.add(tid)
    return out
