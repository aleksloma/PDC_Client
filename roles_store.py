"""Roles registry — which registered DB tables each user role may use.

Storage: DATA_ROOT/roles.json — one JSON document holding `roles`:
  {id, name, description, table_ids: [], scope_grants: [{connection_id,
   schema|null}], created_at, updated_at}

One role per user (profile.json `data_role`, default the built-in Base role).
A scope grant with schema=null covers EVERY table on that connection, present
and future; a schema grant covers that schema's tables the same way. Effective
access is computed dynamically at request time (`allowed_table_ids_for`), so a
table registered later under a granted schema is covered without a role edit
and grant changes propagate instantly. Connector tables are exempt from role
checks by design — they are invisible to users and auto-included for joins
(db_sources.expand_with_connectors); gating them would silently break allowed
joins.

Discipline mirrors db_sources.DataSourceStore: every mutation under _LOCK with
an atomic whole-file replace, section-whitelisting read_doc so an old or
foreign-shaped doc keeps loading after an upgrade, 16-hex ids. The built-in
Base role has the literal id "base" (seeded at boot, undeletable, unrenamable).
Missing roles.json / missing data_role / a dangling data_role all resolve to
Base — deleting a role reverts its members with NO profile rewrites.

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


def _roles_path() -> Path:
    return _data_root() / _DOC_NAME


def _default_doc() -> dict:
    return {"version": 1, "updated_at": None, "roles": []}


def _base_role_doc() -> dict:
    now = _now()
    return {
        "id": BASE_ROLE_ID,
        "name": "Base",
        "description": "Default role for all users.",
        "table_ids": [],
        "scope_grants": [],
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
    """Defaults for every field so an old-shape stored role keeps loading."""
    return {
        "id": str(r.get("id") or ""),
        "name": str(r.get("name") or ""),
        "description": str(r.get("description") or ""),
        "table_ids": [t for t in (r.get("table_ids") or []) if isinstance(t, str)],
        "scope_grants": [g for g in map(_normalize_grant, r.get("scope_grants") or [])
                         if g is not None],
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


class RolesStore:
    """roles.json registry — DataSourceStore discipline (locked atomic writes,
    whitelisting reads, .get() defaults)."""

    @staticmethod
    def valid_id(value) -> bool:
        return value == BASE_ROLE_ID or bool(isinstance(value, str) and _ID_RE.match(value))

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
                      "grants": len(role["scope_grants"])})
        return dict(role)

    def update_role(self, rid: str, fields: dict, actor: str) -> Optional[dict]:
        """Fields present in `fields` replace the stored value; absent fields
        are kept. The Base role's name is immutable — a rename is ignored here
        (the route also 400s it); description and grants stay editable."""
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
            role["updated_at"] = _now()
            self._write_doc(doc)
            out = dict(role)
        audit(actor, "role.update", target=rid,
              detail={"name": out["name"], "tables": len(out["table_ids"]),
                      "grants": len(out["scope_grants"])})
        return out

    def delete_role(self, rid: str, actor: str) -> bool:
        """False for the Base role or an unknown id. Members revert to Base
        dynamically (role_for_email) — no profile rewrites."""
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

    def set_table_roles(self, table_id: str, role_ids: list[str], actor: str) -> None:
        """Exact reconcile for one table: after this call, `table_id` is in a
        role's table_ids iff the role is listed in `role_ids`. One locked
        write. Used by the register/edit wizard's Access panel (canonical
        storage lives HERE, never on the table doc)."""
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
                  detail={"roles": sorted(wanted)})

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
                tables = [t for t in role["table_ids"] if t not in gone]
                if (len(grants) == len(role["scope_grants"])
                        and len(tables) == len(role["table_ids"])):
                    continue
                role["scope_grants"] = grants
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
        t_conn = t.get("connection_id")
        t_schema = str(t.get("schema") or "").lower()
        for g in grants:
            if g["connection_id"] != t_conn:
                continue
            if g["schema"] is None or str(g["schema"]).lower() == t_schema:
                out.add(tid)
                break
    return out


def role_for_email(email: str) -> dict:
    """The user's effective role doc. Missing data_role, a dangling id, or an
    unreadable store all resolve to Base; a missing Base role resolves to an
    empty stub — genuine denials always fail closed through defaults."""
    from local_store import AuthStore
    store = RolesStore()
    rid = AuthStore().get_data_role(email)
    role = store.get_role(rid)
    if role is None and rid != BASE_ROLE_ID:
        role = store.get_role(BASE_ROLE_ID)
    if role is None:
        role = {"id": BASE_ROLE_ID, "name": "Base", "description": "",
                "table_ids": [], "scope_grants": []}
    return role


def allowed_table_ids_for(email: str) -> set:
    """Non-connector registry table ids the user may pick/refresh right now."""
    from db_sources import DataSourceStore
    return effective_table_ids(role_for_email(email), DataSourceStore().list_tables())
