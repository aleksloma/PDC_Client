"""Admin API — Users & Roles (ladmin's "User management").

Same security model as routes/admin_data.py: every route behind
`_require_admin` (401 / 403 + admin.denied audit), validation failures use
HTTP status codes, mutations audited via db_sources.audit.

Design (docs/ENTERPRISE_ARCHITECTURE.md — roles):
  - One role per user (`data_role` on profile.json, default the built-in Base
    role, id "base"). Roles live in roles_store.RolesStore (roles.json).
  - Effective table access is computed dynamically at request time, so a role
    edit propagates instantly and deleting a role reverts its members to Base
    with NO profile rewrites (`reverted_members` is a count, not a write).
  - Emails are BODY-carried, never path params (they contain '@' and dots;
    the guard-coverage test enumerates path templates). Role ids are path
    params like /tables/{tid}.
  - The config-only local admin is invisible here: excluded from listings and
    un-assignable (it never uses /lab, so a data role would be meaningless).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import db_sources
import roles_store
from local_store import AuthStore
from logger_utils import log_with_sid
from routes.admin_data import _require_admin, _json_body
from settings import settings

router = APIRouter(prefix="/api/admin", tags=["client-admin-users"])


def _resolved_role_id(data_role: str, roles_by_id: dict) -> str:
    """A dangling/unknown data_role resolves to Base (dynamic revert)."""
    return data_role if data_role in roles_by_id else roles_store.BASE_ROLE_ID


def _user_row(u: dict, roles_by_id: dict) -> dict:
    rid = _resolved_role_id(u.get("data_role") or "base", roles_by_id)
    role = roles_by_id.get(rid) or {}
    return {
        "email": u.get("email"),
        "role_id": rid,
        "role_name": role.get("name") or "Base",
        "created_at": u.get("created_at"),
        "last_login_at": u.get("last_login_at"),
    }


def _role_out(role: dict, member_counts: dict) -> dict:
    out = dict(role)
    out["is_base"] = role["id"] == roles_store.BASE_ROLE_ID
    out["member_count"] = member_counts.get(role["id"], 0)
    return out


def _member_counts(roles_by_id: dict) -> dict:
    counts: dict = {}
    for u in AuthStore().list_users():
        rid = _resolved_role_id(u.get("data_role") or "base", roles_by_id)
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
            return JSONResponse({"error": "The Base role cannot be renamed."},
                                status_code=400)
        if not name:
            return JSONResponse({"error": "Role name is required."}, status_code=400)
        taken = {r["name"].strip().lower() for r in store.list_roles()
                 if r["id"] != editing_rid}
        taken.add("base")
        if name.lower() in taken:
            return JSONResponse({"error": f"A role named '{name}' already exists."},
                                status_code=400)
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
    if "scope_grants" in body:
        grants = body.get("scope_grants") or []
        if not isinstance(grants, list):
            return JSONResponse({"error": "scope_grants must be a list."}, status_code=400)
        conns = {c.get("id") for c in reg.list_connections()}
        for g in grants:
            norm = roles_store._normalize_grant(g)
            if norm is None:
                return JSONResponse({"error": "Malformed scope grant."}, status_code=400)
            if norm["connection_id"] not in conns:
                return JSONResponse({"error": "Unknown connection in scope grant."},
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
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    target = str(body.get("email") or "").strip().lower()
    role_id = str(body.get("role_id") or "").strip()
    if not target:
        return JSONResponse({"error": "email is required."}, status_code=400)
    if target == (settings.LOCAL_ADMIN_USERNAME or "").strip().lower():
        return JSONResponse({"error": "The local admin has no data role."},
                            status_code=400)
    store = roles_store.RolesStore()
    role = store.get_role(role_id)
    if role is None:
        return JSONResponse({"error": "Unknown role."}, status_code=400)
    auth = AuthStore()
    if auth.get_profile(target) is None:
        return JSONResponse({"error": "Unknown user."}, status_code=404)
    if auth.is_admin(target):
        return JSONResponse({"error": "Admin accounts have no data role."},
                            status_code=400)
    auth.set_data_role(target, role_id)
    db_sources.audit(email, "user.set_role", target=target,
                     detail={"role_id": role_id, "role_name": role.get("name")},
                     ip=(request.client.host if request.client else None))
    log_with_sid(email, "info", f"ADMIN_USER_SET_ROLE user={target} role_id={role_id}")
    roles_by_id = {r["id"]: r for r in store.list_roles()}
    prof = auth.get_profile(target) or {}
    return {"ok": True, "user": _user_row({
        "email": target,
        "data_role": prof.get("data_role") or "base",
        "created_at": prof.get("created_at"),
        "last_login_at": prof.get("last_login_at"),
    }, roles_by_id)}


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def _sorted_roles(roles: list) -> list:
    return sorted(roles, key=lambda r: (r["id"] != roles_store.BASE_ROLE_ID,
                                        r["name"].lower()))


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
    fields = {k: body[k] for k in ("name", "description", "table_ids", "scope_grants")
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
    # Count BEFORE deleting so the response can report the dynamic revert.
    reverted = sum(1 for u in AuthStore().list_users()
                   if (u.get("data_role") or "base") == rid)
    if not store.delete_role(rid, actor=email):
        return JSONResponse({"error": "Unknown role."}, status_code=404)
    log_with_sid(email, "info",
                 f"ADMIN_ROLE_DELETED rid={rid} reverted_members={reverted}")
    return {"ok": True, "reverted_members": reverted}
