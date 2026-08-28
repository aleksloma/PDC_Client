"""Admin API — Users & Roles (ladmin's "User management").

Same security model as routes/admin_data.py: every route behind
`_require_admin` (401 / 403 + admin.denied audit), validation failures use
HTTP status codes, mutations audited via db_sources.audit.

Design (docs/ENTERPRISE_ARCHITECTURE.md — roles & permissions):
  - Roles = TABLE ACCESS only. A user holds SEVERAL roles (19c: `data_roles`
    on profile.json, legacy single `data_role` still read; empty ⇒ the
    built-in Base role, id "base"). Read access is the UNION across held
    roles. Roles live in roles_store.RolesStore (roles.json).
  - PERMISSION is per USER (19e): standard | power | admin, stored in the
    profile `role` field as "user"/"power"/"admin". `set_permission` changes
    it (admin-guarded; refuses the bootstrap ladmin account and the caller's
    own account). Power users manage data sources inside the union of their
    roles' manage_grants; a PROMOTED admin (19g) is a FULL analysis user
    (keeps /lab, chats and roles) plus unrestricted Data-sources admin —
    only the bootstrap ladmin account stays config-only and roleless.
  - Effective table access is computed dynamically at request time, so a role
    edit propagates instantly and deleting a role reverts its members to Base
    with NO profile rewrites (`reverted_members` counts users who HELD it).
  - Emails are BODY-carried, never path params (they contain '@' and dots;
    the guard-coverage test enumerates path templates). Role ids are path
    params like /tables/{tid}.
  - Listings include admin-permission users (they must stay demotable); only
    the bootstrap local-admin account itself is excluded, and it never takes
    a data role or a permission change.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import db_sources
import roles_store
from local_store import AuthStore
from logger_utils import log_with_sid
from routes.admin_data import _require_admin, _json_body

router = APIRouter(prefix="/api/admin", tags=["client-admin-users"])


def _raw_role_ids(u: dict) -> list:
    """The RAW held ids off a profile/list_users row (tolerant of both the
    19c `data_roles` list and the legacy single `data_role`)."""
    raw = u.get("data_roles")
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, str) and r]
    legacy = u.get("data_role")
    return [legacy] if isinstance(legacy, str) and legacy else []


def _resolved_role_ids(u: dict, roles_by_id: dict) -> list:
    """Held ids with dangling/unknown ones dropped (dynamic revert); no
    surviving roles ⇒ the built-in Base."""
    ids = [r for r in _raw_role_ids(u) if r in roles_by_id]
    return ids or [roles_store.BASE_ROLE_ID]


# Stored permission value -> API vocabulary (the UI dropdown's values).
_PERM_OUT = {"user": "standard", "power": "power", "admin": "admin"}
_PERM_IN = {"standard": "user", "power": "power", "admin": "admin"}


def _user_row(u: dict, roles_by_id: dict) -> dict:
    rids = _resolved_role_ids(u, roles_by_id)
    names = [(roles_by_id.get(r) or {}).get("name") or "Base" for r in rids]
    return {
        "email": u.get("email"),
        "permission": _PERM_OUT.get(u.get("role"), "standard"),
        "role_ids": rids,
        "role_names": names,
        # Legacy single-role keys (first held role) — tolerant consumers.
        "role_id": rids[0],
        "role_name": names[0],
        "created_at": u.get("created_at"),
        "last_login_at": u.get("last_login_at"),
    }


def _role_out(role: dict, member_counts: dict) -> dict:
    out = dict(role)
    out["is_base"] = role["id"] == roles_store.BASE_ROLE_ID
    out["is_builtin"] = out["is_base"]   # Base is the only built-in (19e)
    out["member_count"] = member_counts.get(role["id"], 0)
    return out


def _member_counts(roles_by_id: dict) -> dict:
    """Users HOLDING each role — a user with 3 roles counts in all 3."""
    counts: dict = {}
    for u in AuthStore().list_users():
        for rid in _resolved_role_ids(u, roles_by_id):
            counts[rid] = counts.get(rid, 0) + 1
    return counts


def _validate_role_fields(body: dict, store: roles_store.RolesStore,
                          reg: db_sources.DataSourceStore,
                          *, editing_rid: str = ""):
    """Shared create/update validation. Returns an error JSONResponse or None.
    Only validates fields PRESENT in the body (update semantics)."""
    if "name" in body:
        name = str(body.get("name") or "").strip()
        if editing_rid == roles_store.BASE_ROLE_ID:
            # A RESTATED identical name is ignored, not a 400 — the UI's
            # save payload used to carry the unchanged name and every
            # built-in edit failed here (the 19c bug). Only an actual
            # rename is rejected.
            stored = (store.get_role(editing_rid) or {}).get("name") or ""
            if name != stored:
                return JSONResponse(
                    {"error": "The Base role cannot be renamed."},
                    status_code=400)
        else:
            if not name:
                return JSONResponse({"error": "Role name is required."},
                                    status_code=400)
            taken = {r["name"].strip().lower() for r in store.list_roles()
                     if r["id"] != editing_rid}
            taken.add("base")
            if name.lower() in taken:
                return JSONResponse(
                    {"error": f"A role named '{name}' already exists."},
                    status_code=400)
    # A stray 19c-era `power_user` body key is deliberately IGNORED (the
    # capability moved to the per-user permission; old UI payloads must not
    # start failing).
    if "table_ids" in body:
        ids = body.get("table_ids") or []
        if not isinstance(ids, list):
            return JSONResponse({"error": "table_ids must be a list."}, status_code=400)
        known = {t["id"]: t for t in reg.list_tables() if t.get("id")}
        for tid in ids:
            t = known.get(tid) if isinstance(tid, str) else None
            if t is None:
                return JSONResponse({"error": "Unknown table in role grants."},
                                    status_code=400)
            if t.get("is_connector"):
                return JSONResponse(
                    {"error": "Connector tables are exempt from role checks and "
                              "cannot be granted."}, status_code=400)
    for field in ("scope_grants", "manage_grants"):
        if field not in body:
            continue
        grants = body.get(field) or []
        if not isinstance(grants, list):
            return JSONResponse({"error": f"{field} must be a list."},
                                status_code=400)
        conns = {c.get("id") for c in reg.list_connections()}
        for g in grants:
            norm = roles_store._normalize_grant(g)
            if norm is None:
                return JSONResponse({"error": f"Malformed {field} entry."},
                                    status_code=400)
            if norm["connection_id"] not in conns:
                return JSONResponse({"error": f"Unknown connection in {field}."},
                                    status_code=400)
    return None


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get("/users")
async def list_users(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    roles_by_id = {r["id"]: r for r in roles_store.RolesStore().list_roles()}
    users = [_user_row(u, roles_by_id) for u in AuthStore().list_users()]
    return {"users": users}


@router.post("/users/set_role")
async def set_user_role(request: Request):
    """Set the user's HELD ROLE LIST (19c). Body {email, role_ids: [...]};
    the legacy {email, role_id} shape is still accepted (→ one-element
    list). An empty list reverts the user to Base."""
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    target = str(body.get("email") or "").strip().lower()
    if not target:
        return JSONResponse({"error": "email is required."}, status_code=400)
    if AuthStore().is_bootstrap_admin(target):
        return JSONResponse({"error": "The local admin has no data role."},
                            status_code=400)
    if "role_ids" in body:
        role_ids = body.get("role_ids")
        if not isinstance(role_ids, list) or not all(
                isinstance(r, str) for r in role_ids):
            return JSONResponse({"error": "role_ids must be a list of role ids."},
                                status_code=400)
    else:
        # Legacy single-role shape: a missing/empty role_id keeps its
        # pre-19c 400 — it must NOT silently clear the user's held roles
        # (only an EXPLICIT empty role_ids list means "revert to Base").
        legacy = str(body.get("role_id") or "").strip()
        if not legacy:
            return JSONResponse({"error": "Unknown role."}, status_code=400)
        role_ids = [legacy]
    role_ids = list(dict.fromkeys(r.strip() for r in role_ids if r.strip()))
    store = roles_store.RolesStore()
    roles = []
    for rid in role_ids:
        role = store.get_role(rid)
        if role is None:
            return JSONResponse({"error": "Unknown role."}, status_code=400)
        roles.append(role)
    auth = AuthStore()
    if auth.get_profile(target) is None:
        return JSONResponse({"error": "Unknown user."}, status_code=404)
    # 19g: no permission check here — PROMOTED admins take roles through the
    # same machinery as everyone else (only the bootstrap account is
    # roleless, refused by the identity check above).
    auth.set_data_roles(target, role_ids)
    db_sources.audit(email, "user.set_roles", target=target,
                     detail={"role_ids": role_ids,
                             "role_names": [r.get("name") for r in roles]},
                     ip=(request.client.host if request.client else None))
    log_with_sid(email, "info",
                 f"ADMIN_USER_SET_ROLES user={target} role_ids={role_ids}")
    roles_by_id = {r["id"]: r for r in store.list_roles()}
    prof = auth.get_profile(target) or {}
    return {"ok": True, "user": _user_row({
        "email": target,
        "role": auth.get_role(target),
        "data_roles": prof.get("data_roles"),
        "data_role": prof.get("data_role"),
        "created_at": prof.get("created_at"),
        "last_login_at": prof.get("last_login_at"),
    }, roles_by_id)}


@router.post("/users/set_permission")
async def set_user_permission(request: Request):
    """Set the user's per-account PERMISSION (19e): body {email, permission:
    "standard"|"power"|"admin"} ("standard" is stored as "user"). Refuses the
    bootstrap ladmin account and the caller's own account (no self-demotion).
    Never touches `data_roles` — an admin keeps them stored but inert, so a
    later demote restores their access for free."""
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    target = str(body.get("email") or "").strip().lower()
    if not target:
        return JSONResponse({"error": "email is required."}, status_code=400)
    if AuthStore().is_bootstrap_admin(target):
        return JSONResponse(
            {"error": "The local admin account's permission cannot be changed."},
            status_code=400)
    if target == email:
        return JSONResponse(
            {"error": "You cannot change your own permission."},
            status_code=400)
    perm = body.get("permission")
    if perm not in _PERM_IN:
        return JSONResponse(
            {"error": "permission must be one of: standard, power, admin."},
            status_code=400)
    auth = AuthStore()
    if auth.get_profile(target) is None:
        return JSONResponse({"error": "Unknown user."}, status_code=404)
    old = _PERM_OUT.get(auth.get_role(target), "standard")
    auth.set_role(target, _PERM_IN[perm])
    db_sources.audit(email, "user.set_permission", target=target,
                     detail={"old": old, "new": perm},
                     ip=(request.client.host if request.client else None))
    log_with_sid(email, "info",
                 f"ADMIN_USER_SET_PERMISSION user={target} {old}->{perm}")
    roles_by_id = {r["id"]: r for r in roles_store.RolesStore().list_roles()}
    prof = auth.get_profile(target) or {}
    return {"ok": True, "user": _user_row({
        "email": target,
        "role": auth.get_role(target),
        "data_roles": prof.get("data_roles"),
        "data_role": prof.get("data_role"),
        "created_at": prof.get("created_at"),
        "last_login_at": prof.get("last_login_at"),
    }, roles_by_id)}


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def _sorted_roles(roles: list) -> list:
    """The built-in Base first, the rest by name."""
    order = {roles_store.BASE_ROLE_ID: 0}
    return sorted(roles, key=lambda r: (order.get(r["id"], 1), r["name"].lower()))


@router.get("/roles")
async def list_roles(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    roles = roles_store.RolesStore().list_roles()
    roles_by_id = {r["id"]: r for r in roles}
    counts = _member_counts(roles_by_id)
    return {"roles": [_role_out(r, counts) for r in _sorted_roles(roles)]}


@router.post("/roles")
async def create_role(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    body.setdefault("name", "")  # name is mandatory on create
    store = roles_store.RolesStore()
    verr = _validate_role_fields(body, store, db_sources.DataSourceStore())
    if verr:
        return verr
    role = store.create_role({
        "name": str(body.get("name") or "").strip(),
        "description": str(body.get("description") or ""),
        "table_ids": body.get("table_ids") or [],
        "scope_grants": body.get("scope_grants") or [],
        "manage_grants": body.get("manage_grants") or [],
    }, actor=email)
    return JSONResponse({"role": _role_out(role, {})}, status_code=201)


@router.post("/roles/{rid}")
async def update_role(request: Request, rid: str):
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    store = roles_store.RolesStore()
    if store.get_role(rid) is None:
        return JSONResponse({"error": "Unknown role."}, status_code=404)
    verr = _validate_role_fields(body, store, db_sources.DataSourceStore(),
                                 editing_rid=rid)
    if verr:
        return verr
    fields = {k: body[k] for k in ("name", "description", "table_ids",
                                   "scope_grants", "manage_grants")
              if k in body}
    role = store.update_role(rid, fields, actor=email)
    roles_by_id = {r["id"]: r for r in store.list_roles()}
    return {"role": _role_out(role, _member_counts(roles_by_id))}


@router.post("/roles/{rid}/delete")
async def delete_role(request: Request, rid: str):
    email, err = _require_admin(request)
    if err:
        return err
    if rid == roles_store.BASE_ROLE_ID:
        return JSONResponse({"error": "The Base role cannot be deleted."},
                            status_code=400)
    store = roles_store.RolesStore()
    role = store.get_role(rid)
    if role is None:
        return JSONResponse({"error": "Unknown role."}, status_code=404)
    # Count BEFORE deleting so the response can report the dynamic revert:
    # every user HOLDING the role (once each, however many roles they hold).
    reverted = sum(1 for u in AuthStore().list_users()
                   if rid in _raw_role_ids(u))
    if not store.delete_role(rid, actor=email):
        return JSONResponse({"error": "Unknown role."}, status_code=404)
    log_with_sid(email, "info",
                 f"ADMIN_ROLE_DELETED rid={rid} reverted_members={reverted}")
    return {"ok": True, "reverted_members": reverted}
