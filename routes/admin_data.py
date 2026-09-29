"""Admin "Data sources" API — management of database connections and
registered tables by ladmin, plus scoped self-service by POWER USERS.

Security model:
  - Connection lifecycle (create/edit/delete/test/refresh-all), the GLOBAL
    refresh schedule and the audit tail stay behind `_require_admin`
    (role == "admin" on the local user record; 403 while a forced password
    change is pending). Denials are audited (`admin.denied`).
  - Everything a power user may reach is behind `_require_source_manager`:
    ladmin passes unrestricted (scope None); a user whose PERMISSION is
    "power" (AuthStore profile role, 19e) gets the union of their held
    roles' manage_grants (roles_store.management_scope_for — 19f: the
    SEPARATE management axis; scope_grants are read-only reach) as the
    management scope — every referenced physical table (connection, schema)
    must fall inside it (403 {"code": "OUT_OF_SCOPE"} otherwise), list
    responses are filtered to it, `access_role_ids` must be a subset of the
    power user's held roles minus Base (403 {"code": "ROLE_NOT_HELD"},
    validated before anything registers; their reconcile also preserves
    unheld roles' ladmin-granted membership), and table delete additionally
    requires `registered_by == <the power user>` (403 {"code":
    "NOT_OWNER"}). Power-user writes carry `actor_kind: "power_user"` in
    their audit detail.
  - Connections leave the store ONLY masked (`db_sources._mask_connection`);
    passwords are decrypted into function-locals at the moment of use.
  - The AI description draft reuses `brain_client.schema_autofill` with the
    SAME sampled/truncated context uploaded files send (Article II parity) —
    the payload carries no host, user, password, or connection id, and the
    draft endpoint has NO write path (the mandatory-confirm gate lives in the
    save route).
  - Connectivity/introspection failures return 200 {ok:false, error} (the
    dashboards idiom); HTTP codes are reserved for auth/validation.
"""
from __future__ import annotations

import asyncio
import atexit
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import brain_client
import db_connector
import db_sources
import relation_discovery
from exec_transport import log_safe_text
from local_store import AuthStore
from logger_utils import log_with_sid
from settings import settings

router = APIRouter(prefix="/api/admin", tags=["client-admin"])

# Blocking DB work stays off the event loop (Article VI pool + atexit).
_DB_EXEC = ThreadPoolExecutor(max_workers=4, thread_name_prefix="db_admin")
atexit.register(lambda: _DB_EXEC.shutdown(wait=False, cancel_futures=True))


def _require_admin(request: Request):
    """2-tuple guard (the repo idiom — see routes/dashboards._require_email):
    (email, None) for an admin, (None, JSONResponse) otherwise."""
    email = request.session.get("email")
    if not email:
        return None, JSONResponse({"error": "Not authenticated"}, status_code=401)
    email = email.strip().lower()
    if request.session.get("must_change_password"):
        return None, JSONResponse({"error": "Password change required"}, status_code=403)
    if not AuthStore().is_admin(email):
        db_sources.audit(email, "admin.denied", target=str(request.url.path), ok=False,
                         ip=(request.client.host if request.client else None))
        log_with_sid(email, "warning", f"ADMIN_DENIED path={request.url.path}")
        return None, JSONResponse({"error": "Administrator access required"}, status_code=403)
    return email, None


def _require_source_manager(request: Request):
    """3-tuple guard for the endpoints POWER USERS may reach: (email, scope,
    None) on success — scope is None for ladmin (unrestricted) or the power
    user's management grants (roles_store.management_scope_for, fail-closed) —
    else (None, None, JSONResponse). 401/403/forced-change semantics and the
    `admin.denied` audit row match _require_admin exactly (the guard-coverage
    test counts those rows 1:1), with detail {"reason": "not_power_user"}."""
    email = request.session.get("email")
    if not email:
        return None, None, JSONResponse({"error": "Not authenticated"}, status_code=401)
    email = email.strip().lower()
    if request.session.get("must_change_password"):
        return None, None, JSONResponse({"error": "Password change required"},
                                        status_code=403)
    if AuthStore().is_admin(email):
        return email, None, None
    import roles_store
    scope = roles_store.management_scope_for(email)
    if scope is None:
        db_sources.audit(email, "admin.denied", target=str(request.url.path), ok=False,
                         detail={"reason": "not_power_user"},
                         ip=(request.client.host if request.client else None))
        log_with_sid(email, "warning", f"ADMIN_DENIED path={request.url.path}")
        return None, None, JSONResponse({"error": "Administrator access required"},
                                        status_code=403)
    return email, scope, None


def _kind(scope):
    """Audit actor_kind for this request: ladmin (scope None) keeps rows
    byte-identical; a power user's writes are labeled."""
    return "power_user" if scope is not None else None


def _in_scope(scope, connection_id, schema) -> bool:
    """scope None = ladmin, unrestricted. Otherwise the physical (connection,
    schema) must be covered by a management grant (schema case-insensitive;
    a None-schema grant covers the whole connection)."""
    if scope is None:
        return True
    import roles_store
    return roles_store.scope_covers(scope, connection_id, schema)


def _scoped_tables(scope, tables: list) -> list:
    """Registry rows (CONNECTORS INCLUDED — the management page lists them)
    inside the management scope; everything for ladmin."""
    if scope is None:
        return tables
    return [t for t in tables
            if _in_scope(scope, t.get("connection_id"), t.get("schema"))]


def _out_of_scope(msg: str = "Outside your managed scope."):
    return JSONResponse({"error": msg, "code": "OUT_OF_SCOPE"}, status_code=403)


async def _run(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_DB_EXEC, lambda: fn(*args, **kwargs))


def _safe_int(value):
    """None on absent/garbage — a malformed row_cap must 400-degrade, not 500
    (Article IV)."""
    try:
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _conn_cfg_and_password(store: db_sources.DataSourceStore, body: dict):
    """Resolve (cfg, password, error_response) from either a saved
    connection_id or an unsaved draft (Test-before-Save)."""
    cid = (body.get("connection_id") or "").strip()
    if cid:
        conn = store.get_connection(cid, with_secret=True)
        if conn is None:
            return None, None, JSONResponse({"error": "Unknown connection."}, status_code=404)
        password = db_sources.decrypt_password(conn.get("password_enc"))
        if password is None and conn.get("password_enc"):
            return conn, None, JSONResponse(
                {"ok": False, "error": "Stored credential cannot be read — "
                                       "re-enter the connection password."},
                status_code=200)
        return conn, password or "", None
    # Unsaved draft: the password rides in the body (never persisted here).
    cfg = {k: body.get(k) for k in ("db_type", "host", "port", "database",
                                    "service_name", "user", "ssl",
                                    "trust_server_certificate", "connect_timeout",
                                    "statement_timeout", "url_override")}
    return cfg, body.get("password") or "", None


# ---------------------------------------------------------------------------
# Dialects
# ---------------------------------------------------------------------------

@router.get("/dialects")
async def dialects(request: Request):
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    return {"dialects": db_connector.list_dialects()}


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

@router.get("/connections")
async def list_connections(request: Request):
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    store = db_sources.DataSourceStore()
    conns = store.list_connections()
    if scope is not None:
        # A power user sees only connections their scope references (read-only
        # in the UI), and table counts only for tables they can manage.
        granted = {g.get("connection_id") for g in scope}
        conns = [c for c in conns if c.get("id") in granted]
    tables = _scoped_tables(scope, store.list_tables())
    counts: dict = {}
    for t in tables:
        counts[t.get("connection_id")] = counts.get(t.get("connection_id"), 0) + 1
    for c in conns:
        c["table_count"] = counts.get(c.get("id"), 0)
    return {"connections": conns, "encryption_ready": db_sources.encryption_ready()}


@router.post("/connections")
async def create_connection(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    if not (body.get("name") or "").strip():
        return JSONResponse({"error": "Name is required."}, status_code=400)
    try:
        d = db_connector.get_dialect(body.get("db_type"))
        if d.hidden:
            # Hidden entries (sqlite, allow_url_override) exist for the offline
            # test suite only — the API must not accept them either, or an
            # arbitrary SQLAlchemy URL could ride in via url_override.
            raise ValueError(f"Unknown database type: {body.get('db_type')!r}")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not (body.get("password") or ""):
        return JSONResponse({"error": "Password is required."}, status_code=400)
    try:
        conn = db_sources.DataSourceStore().create_connection(
            body, body.get("password"), actor=email)
    except db_sources.EncryptionUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return JSONResponse({"connection": conn}, status_code=201)


@router.post("/connections/test")
async def test_connection(request: Request):
    """SELECT-1 probe. Accepts {connection_id} OR a full unsaved draft (with
    password) so Test-before-Save works. Connectivity failure → 200 ok:false.
    NOTE: registered before /connections/{cid} — literal beats parameter."""
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    store = db_sources.DataSourceStore()
    cfg, password, resp = _conn_cfg_and_password(store, body)
    if resp is not None:
        return resp
    res = await _run(db_connector.test_connection, cfg, password, sid=f"admin:{email}")
    cid = (body.get("connection_id") or "").strip()
    if cid:
        store.mark_tested(cid, bool(res.get("ok")))
    db_sources.audit(email, "connection.test", target=cid or (cfg.get("host") or ""),
                     ok=bool(res.get("ok")), detail={"error": res.get("error")})
    return res


@router.post("/connections/{cid}")
async def update_connection(request: Request, cid: str):
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    if body.get("db_type"):
        try:
            if db_connector.get_dialect(body["db_type"]).hidden:
                raise ValueError(f"Unknown database type: {body['db_type']!r}")
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    try:
        conn = db_sources.DataSourceStore().update_connection(
            cid, body, body.get("password") or None, actor=email)
    except db_sources.EncryptionUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    if conn is None:
        return JSONResponse({"error": "Unknown connection."}, status_code=404)
    return {"connection": conn}


@router.post("/connections/{cid}/delete")
async def delete_connection(request: Request, cid: str):
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    res = db_sources.DataSourceStore().delete_connection(
        cid, actor=email, cascade=bool(body.get("cascade")))
    if not res.get("ok"):
        return JSONResponse(
            {"error": "Registered tables still use this connection.",
             "tables": res.get("tables") or []},
            status_code=409)
    # Best-effort prune of the connection's scope grants + cascaded table ids
    # from every role (mirror of the single-table delete prune).
    try:
        import roles_store
        roles_store.RolesStore().remove_connection(
            cid, res.get("deleted_tables") or [], actor=email)
    except Exception as e:
        log_with_sid(email, "warning", f"CONN_DELETE_ROLE_PRUNE_FAILED: {e}")
    return res


@router.get("/connections/{cid}/schemas")
async def connection_schemas(request: Request, cid: str):
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    if scope is not None and not any(g.get("connection_id") == cid for g in scope):
        return _out_of_scope()
    store = db_sources.DataSourceStore()
    cfg, password, resp = _conn_cfg_and_password(store, {"connection_id": cid})
    if resp is not None:
        return resp
    res = await _run(db_connector.list_schemas, cfg, password, sid=f"admin:{email}")
    if scope is not None and res.get("ok"):
        # Schema-level grants narrow the browser to the granted schemas; any
        # whole-connection grant (schema None) keeps the full list.
        conn_grants = [g for g in scope if g.get("connection_id") == cid]
        if all(g.get("schema") is not None for g in conn_grants):
            granted = {str(g.get("schema")).lower() for g in conn_grants}
            res["schemas"] = [s for s in (res.get("schemas") or [])
                              if str(s).lower() in granted]
            if str(res.get("default_schema") or "").lower() not in granted:
                res["default_schema"] = (res["schemas"][0]
                                         if res["schemas"] else None)
    return res


@router.get("/connections/{cid}/tables")
async def connection_tables(request: Request, cid: str, schema: str = ""):
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    if not _in_scope(scope, cid, schema or None):
        return _out_of_scope()
    store = db_sources.DataSourceStore()
    cfg, password, resp = _conn_cfg_and_password(store, {"connection_id": cid})
    if resp is not None:
        return resp
    res = await _run(db_connector.list_tables, cfg, password, schema or None,
                     sid=f"admin:{email}")
    if res.get("ok"):
        registered = {(t.get("connection_id"), t.get("schema") or "", t.get("table_name")): t
                      for t in store.list_tables()}
        for row in res["tables"]:
            t = registered.get((cid, schema or "", row["name"]))
            row["registered"] = t is not None
            if t:
                row["table_id"] = t.get("id")
                # Lets the wizard label + disable the option ("already
                # registered as 'X'") — duplicates are blocked on save too.
                row["registered_as"] = t.get("display_name")
    return res


@router.post("/connections/{cid}/refresh")
async def refresh_connection(request: Request, cid: str):
    """Refresh-now for every table on one connection (sequential)."""
    email, err = _require_admin(request)
    if err:
        return err
    import db_scheduler
    store = db_sources.DataSourceStore()
    tables = [t for t in store.list_tables() if t.get("connection_id") == cid]
    results = []
    for t in tables:
        if db_sources.table_mode(t) == "live":
            # A live table takes no snapshot (its profile is refreshed by the
            # per-table Refresh now).
            results.append({"table_id": t.get("id"),
                            "display_name": t.get("display_name"),
                            "ok": True, "rows": None, "error": None,
                            "skipped": "live"})
            continue
        res = await _run(db_scheduler.refresh_one_table, t.get("id"), actor=email)
        results.append({"table_id": t.get("id"),
                        "display_name": t.get("display_name"),
                        "ok": bool(res.get("ok")),
                        "rows": res.get("rows"), "error": res.get("error")})
    return {"ok": True, "results": results}


# ---------------------------------------------------------------------------
# Relation discovery (proposals only — ladmin accepts explicitly)
# ---------------------------------------------------------------------------

def _snapshot_key_loader(tid: str, cols: list):
    """Column-projected snapshot read for verification. None on any failure
    (missing snapshot, missing column, bad id) — the candidate just renders
    as "unverified". Values stay in-process; only aggregates leave."""
    import pandas as pd
    import local_store
    try:
        return pd.read_parquet(local_store.db_snapshot_path(tid), columns=cols)
    except Exception as e:
        log_with_sid("admin", "info",
                     f"REL_SNAPSHOT_UNAVAILABLE table={tid}: {type(e).__name__}")
        return None


def _verify_and_band(candidates: list) -> list:
    verified = relation_discovery.verify_candidates(candidates, _snapshot_key_loader)
    return relation_discovery.band_all(verified)


def _confirmed_relation_count(tables: list) -> int:
    """Total confirmed relation entries across the registry — the UI uses it
    to explain a zero-candidate scan ("N already confirmed, excluded")."""
    return sum(1 for t in tables or []
               for r in (t.get("relations") or []) if isinstance(r, dict))


def _collect_evidence_warnings(recs: list, tables: list, email: str) -> list:
    """Replay-validation warnings for the admin UI ("never silent"): stored
    pasted-SQL evidence naming columns a now-known registration lacks — the
    pairs are excluded from replay by validate_rec_evidence; this surfaces
    them. Deduped on (table, column); identifiers only."""
    out: list = []
    seen: set = set()
    for rec in recs:
        _clean, invalid = relation_discovery.validate_rec_evidence(rec, tables)
        for w in invalid:
            key = (w["table"], w["column"])
            if key in seen:
                continue
            seen.add(key)
            out.append({**w, "source": "sql-evidence"})
            log_with_sid(email, "warning",
                         f"REL_REPLAY_INVALID_COLUMN table={w['table']} "
                         f"col={w['column']}")
    return out


def _persist_sql_recommendations(store, tables: list, stats: dict,
                                 email: str, scope=None, actor_kind=None) -> dict:
    """Turn extraction's unregistered-join evidence into persistent
    "Recommended tables". Only names resolvable to ONE connection (the same
    resolve_unknown_tables rule the register shortcut uses) become
    recommendations — the rest stay ephemeral unknown_tables strings.
    Identifiers and counts only; the SQL text never reaches this function.
    A power user's scope also bounds the WRITE: a resolved recommendation
    whose physical (connection, schema) falls outside it is never persisted
    (the read endpoints filter too, but an out-of-scope row must not exist at
    all). Never raises — a store failure must not discard the already-computed
    candidates of the analyze response (Article IV)."""
    try:
        return _persist_sql_recommendations_inner(store, tables, stats, email,
                                                  scope, actor_kind)
    except Exception as e:
        log_with_sid(email, "error",
                     f"REL_RECOMMEND_PERSIST_FAILED: {type(e).__name__}")
        return {"created": 0, "updated": 0}


def _persist_sql_recommendations_inner(store, tables: list, stats: dict,
                                       email: str, scope=None,
                                       actor_kind=None) -> dict:
    joins = stats.get("unregistered_joins") or []
    if not joins:
        return {"created": 0, "updated": 0}
    names = sorted({str(j.get("name")) for j in joins if j.get("name")})
    resolved = {h["name"]: h for h in relation_discovery.resolve_unknown_tables(
        names, tables, store.list_connections())}
    table_freq = {e.get("name"): int(e.get("count") or 0)
                  for e in stats.get("unregistered_tables") or []}
    by_id = {t.get("id"): t for t in tables}
    batch: dict = {}
    for j in joins:
        name = str(j.get("name") or "")
        hint = resolved.get(name)
        if hint is None:
            continue
        item = batch.get(name)
        if item is None:
            item = batch[name] = {
                "connection_id": hint["connection_id"],
                "schema": hint["schema"], "table": hint["table"],
                "source": "sql", "frequency": int(table_freq.get(name) or 0),
                "evidence": []}
        other = j.get("other") or {}
        if "table_id" in other:
            ot = by_id.get(other.get("table_id"))
            if ot is None:
                # Should be unreachable: table_id evidence is minted and
                # consumed from the SAME tables list within one request.
                log_with_sid(email, "warning",
                             "REL_RECOMMEND_STALE_TABLE_ID dropped")
                continue
            oref = {"connection_id": str(ot.get("connection_id") or ""),
                    "schema": str(ot.get("schema") or ""),
                    "table": str(ot.get("table_name") or "")}
        else:
            oname = str(other.get("name") or "")
            osc, _, otn = oname.rpartition(".")
            oref = {"schema": osc, "table": otn}
            ohint = resolved.get(oname)
            if ohint:
                oref["connection_id"] = ohint["connection_id"]
        item["evidence"].append({"origin": "sql", "other": oref,
                                 "pairs": j.get("pairs") or [],
                                 "count": int(j.get("count") or 0)})
    items = [it for it in batch.values()
             if _in_scope(scope, it.get("connection_id"), it.get("schema"))]
    if not items:
        return {"created": 0, "updated": 0}
    return store.upsert_recommendations(items, actor=email,
                                        actor_kind=actor_kind)


def _rel_ref_in_scope(scope, store, ref: str, tables=None) -> bool:
    """A stored or posted relation's RELATED side is inside the management
    scope. The ref is a table id, or a legacy `related_table` display name —
    a name counts as out of scope when ANY registration of that name is.
    An unknown ref resolves to nothing and passes (it relates nothing)."""
    if scope is None or not ref:
        return True
    if db_sources.DataSourceStore.valid_id(ref):
        parent = store.get_table(ref)
        return parent is None or _in_scope(scope, parent.get("connection_id"),
                                           parent.get("schema"))
    wanted = ref.strip().lower()
    for t in (tables if tables is not None else store.list_tables()):
        names = {str(t.get("display_name") or "").strip().lower(),
                 str(t.get("table_name") or "").strip().lower()}
        if wanted in names and not _in_scope(scope, t.get("connection_id"),
                                             t.get("schema")):
            return False
    return True


def _scoped_relations(scope, store, posted: list, existing: list):
    """Apply the both-sides-in-scope rule to a table save's `relations`.

    For a power user, a posted relation whose related table is OUTSIDE the
    scope is accepted only when it is an unchanged copy of a stored one (the
    wizard posts the stored relations back on every edit); a new or changed
    one is refused. A stored out-of-scope relation the post omits is KEPT —
    the power user cannot remove it any more than add it. Returns
    (relations, None) or (None, the out-of-scope error)."""
    if scope is None:
        return posted, None
    tables = store.list_tables()

    def ref_of(rel):
        return str(rel.get("related_table_id") or rel.get("related_table") or "").strip()

    def pairs_of(rel):
        try:
            return [[str(p[0]), str(p[1])] for p in (rel.get("join_keys") or [])]
        except Exception as e:
            log_with_sid("admin", "warning",
                         f"REL_SCOPE_MALFORMED_ENTRY: {type(e).__name__}")
            return None

    def stored_copy(rel):
        pairs = pairs_of(rel)
        return pairs is not None and any(
            isinstance(r, dict) and _rel_matches(r, ref_of(rel), pairs)
            for r in existing)

    for rel in posted:
        if (not _rel_ref_in_scope(scope, store, ref_of(rel), tables)
                and not stored_copy(rel)):
            return None, _out_of_scope(
                "A relation to a table outside your managed scope cannot be "
                "added or changed.")
    kept = list(posted)
    for r in existing:
        if not isinstance(r, dict) or _rel_ref_in_scope(scope, store, ref_of(r), tables):
            continue
        pairs = pairs_of(r)
        if pairs is not None and not any(_rel_matches(p, ref_of(r), pairs) for p in posted):
            kept.append(r)
    return kept, None


def _rel_matches(rel: dict, ref: str, jk: list) -> bool:
    """Exact match of a stored relation entry: same related ref (id or legacy
    name) AND join_keys equal as ordered [[child, parent], ...] lists."""
    rid = str(rel.get("related_table_id") or rel.get("related_table") or "")
    if rid != ref:
        return False
    try:
        pairs = [[str(p[0]), str(p[1])] for p in (rel.get("join_keys") or [])]
    except Exception as e:
        log_with_sid("admin", "warning",
                     f"REL_MATCH_MALFORMED_ENTRY ref={ref}: {type(e).__name__}")
        return False
    return pairs == jk


@router.post("/relations/scan")
async def scan_relations(request: Request):
    """Run the FK + name/description discovery pipeline over ALL registered
    tables. FKs are fetched by LIVE introspection (they are not persisted in
    the registry); an unreachable connection degrades that source only —
    name/description candidates still come back. Nothing is written. A power
    user's scan runs over their manageable tables only — introspection,
    candidates, recommendations and the confirmed count all stay in scope."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    store = db_sources.DataSourceStore()
    tables = _scoped_tables(scope, store.list_tables())
    degraded: list = []
    fk_map: dict = {}

    by_conn: dict = {}
    for t in tables:
        by_conn.setdefault(t.get("connection_id"), []).append(t)
    for cid, conn_tables in by_conn.items():
        conn = store.get_connection(cid)
        conn_name = (conn or {}).get("name") or cid or "?"
        # A registry row with a missing/unknown connection must degrade, not
        # fall into _conn_cfg_and_password's unsaved-draft branch (which would
        # hand introspect an all-None cfg).
        if not cid or conn is None:
            degraded.append({"connection": conn_name,
                             "error": "connection unavailable — FK evidence skipped"})
            continue
        cfg, password, resp = _conn_cfg_and_password(store, {"connection_id": cid})
        if resp is not None:
            degraded.append({"connection": conn_name,
                             "error": "connection unavailable — FK evidence skipped"})
            continue
        intros = await asyncio.gather(*[
            _run(db_connector.introspect, cfg, password,
                 (t.get("schema") or "").strip() or None, t.get("table_name"),
                 sid=f"admin:{email}")
            for t in conn_tables], return_exceptions=True)
        for t, intro in zip(conn_tables, intros):
            if isinstance(intro, dict) and intro.get("ok"):
                fk_map[t["id"]] = intro
            else:
                if isinstance(intro, BaseException):
                    log_with_sid(email, "warning",
                                 f"REL_SCAN_INTROSPECT_RAISED table={t.get('id')}: "
                                 f"{type(intro).__name__}")
                degraded.append({"connection": conn_name,
                                 "table": t.get("display_name") or t.get("table_name"),
                                 "error": "introspection failed — FK evidence skipped"})

    # FKs pointing at tables the admin never registered — the "forgot the
    # dictionary table" signal (rendered with a Register-as-connector shortcut)
    # — persisted as fk-sourced recommendations so they survive the session.
    unregistered = relation_discovery.unregistered_fk_refs(tables, fk_map)
    by_id = {t.get("id"): t for t in tables}
    fk_batch = []
    for ref in unregistered:
        evidence = []
        for src_id, pairs in zip(ref.get("referenced_by_ids") or [],
                                 ref.get("referenced_pairs") or []):
            src = by_id.get(src_id)
            if src is None:
                continue
            evidence.append({
                "origin": "fk",
                "other": {"connection_id": str(src.get("connection_id") or ""),
                          "schema": str(src.get("schema") or ""),
                          "table": str(src.get("table_name") or "")},
                "pairs": pairs, "count": 0})
        fk_batch.append({"connection_id": ref.get("connection_id"),
                         "schema": ref.get("schema"), "table": ref.get("table"),
                         "source": "fk", "frequency": 0, "evidence": evidence})
    # A power user's scope bounds the WRITE too: an in-scope table's FK may
    # point at a schema/connection outside a schema-limited grant — such refs
    # still render in this response but must never persist a recommendation.
    fk_batch = [b for b in fk_batch
                if _in_scope(scope, b.get("connection_id"), b.get("schema"))]
    if fk_batch:
        await _run(store.upsert_recommendations, fk_batch, email,
                   actor_kind=_kind(scope))

    cands = await _run(relation_discovery.discover, tables, fk_map)
    # Close the loop for registrations made OUTSIDE instant-Accept (wizard,
    # ghost shortcut): stored SQL evidence of now-registered recommendations
    # replays through the same pipeline — no re-pasting. v4.1: evidence pairs
    # naming columns the registration lacks are excluded by the replay
    # validator and surfaced as warnings, never as bogus candidates.
    rec_cands: list = []
    registered_recs = [r for r in await _run(store.list_recommendations)
                       if r.get("status") == "registered"
                       and _in_scope(scope, r.get("connection_id"), r.get("schema"))]
    evidence_warnings = _collect_evidence_warnings(registered_recs, tables, email)
    for rec in registered_recs:
        rec_cands.extend(
            relation_discovery.recommendation_candidates(rec, tables))
    if rec_cands:
        rec_cands = relation_discovery.filter_same_physical(rec_cands, tables)
        rec_cands = relation_discovery.filter_existing_physical(rec_cands, tables)
        rec_cands = relation_discovery.dedupe_physical_targets(rec_cands, tables)
        cands = relation_discovery.merge_candidates(cands, rec_cands)
    cands = await _run(_verify_and_band, cands)
    db_sources.audit(email, "relations.scan",
                     detail={"tables": len(tables), "candidates": len(cands),
                             "degraded": len(degraded),
                             "unregistered_refs": len(unregistered)},
                     actor_kind=_kind(scope))
    return {"ok": True, "candidates": cands, "degraded": degraded,
            "unregistered_refs": unregistered,
            "evidence_warnings": evidence_warnings,
            "confirmed_count": _confirmed_relation_count(tables)}


@router.post("/relations/analyze_sql")
async def analyze_sql(request: Request):
    """Extract join candidates from admin-pasted SELECT statements. The SQL
    TEXT is parsed IN MEMORY on this client and never persisted, logged,
    audited, or sent to the brain (Article II) — the audit rows carry counts
    only, and sqlglot's error text (it embeds the SQL) never leaves the
    parser. Only table/column IDENTIFIERS extracted from it persist, as the
    "Recommended tables" evidence for unregistered join endpoints."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    sql_text = body.get("sql") or ""
    if not sql_text.strip():
        return JSONResponse({"error": "sql is required."}, status_code=400)
    dialect = relation_discovery.SQLGLOT_DIALECT.get(
        (body.get("db_type") or "").strip().lower())
    store = db_sources.DataSourceStore()
    tables = _scoped_tables(scope, store.list_tables())
    cands, stats = await _run(relation_discovery.extract_sql_joins,
                              sql_text, tables, dialect)
    cands = relation_discovery.filter_same_physical(cands, tables)
    cands = relation_discovery.filter_existing_physical(cands, tables)
    cands = relation_discovery.dedupe_physical_targets(cands, tables)
    cands = await _run(_verify_and_band, cands)
    conns = store.list_connections()
    if scope is not None:
        granted = {g.get("connection_id") for g in scope}
        conns = [c for c in conns if c.get("id") in granted]
    hints = relation_discovery.resolve_unknown_tables(
        stats.get("unknown_tables") or [], tables, conns)
    recs = await _run(_persist_sql_recommendations, store, tables, stats, email,
                      scope, _kind(scope))
    db_sources.audit(email, "relations.analyze_sql",
                     detail={"statements": stats.get("statements"),
                             "failed": stats.get("failed"),
                             "candidates": len(cands)},
                     actor_kind=_kind(scope))
    return {"ok": True, "candidates": cands, "stats": stats,
            "unknown_table_hints": hints,
            "recommendations": recs,
            "confirmed_count": _confirmed_relation_count(tables)}


_REL_CARDINALITIES = {"N:1", "1:1", "1:N", "N:M"}
_REL_ORIGINS = {"fk", "sql", "name", "description"}
# How a table may be registered. "connector" = helper/dictionary table hidden
# from the user picker and auto-included via relations; "normal" = pickable.
_TABLE_TYPES = {"connector", "normal"}

# Sentinel child id for a table being registered (no id yet). Deliberately
# NON-hex: it fails DataSourceStore.valid_id, so it can never collide with a
# real id or slip through /relations/accept.
_WIZARD_CHILD_ID = "__wizard__"


def _sample_frame(sample):
    """Wizard preview sample → DataFrame, or None (absent/failed preview →
    every candidate renders "unverified", never an error)."""
    try:
        if not isinstance(sample, dict):
            return None
        cols = [str(c) for c in (sample.get("columns") or [])]
        rows = sample.get("rows") or []
        if not cols or not rows:
            return None
        import pandas as pd
        return pd.DataFrame(rows, columns=cols)
    except Exception as e:
        log_with_sid("admin", "info", f"REL_WIZARD_SAMPLE_INVALID: {type(e).__name__}")
        return None


def _wizard_suggest_candidates(body: dict, registered: list) -> list:
    """Assemble relation suggestions for the wizard's relations step, purely
    from wizard-held state (introspected FKs, preview sample, typed
    descriptions) + registry metadata + parent snapshots. Reuses the discovery
    generators/verification; the wizard table is normalized to ALWAYS be the
    stored child, because the wizard save can only write relations onto the
    table being saved."""
    editing_tid = (body.get("editing_tid") or "").strip()
    child_id = (editing_tid if db_sources.DataSourceStore.valid_id(editing_tid)
                else _WIZARD_CHILD_ID)
    child = {
        "id": child_id,
        "connection_id": (body.get("connection_id") or "").strip(),
        "schema": (body.get("schema") or "").strip(),
        "table_name": (body.get("table_name") or "").strip(),
        "display_name": ((body.get("display_name") or "").strip()
                         or (body.get("table_name") or "").strip()),
        "columns": [{"name": str(c.get("name")), "pk": bool(c.get("pk")),
                     "description": (c.get("description") or "").strip()}
                    for c in (body.get("columns") or [])
                    if isinstance(c, dict) and c.get("name")],
        # Current wizard rows + (for edits) stored relations arrive here so
        # filter_existing dedupes against BOTH, either orientation.
        "relations": [r for r in (body.get("relations") or []) if isinstance(r, dict)],
        # Honest flag: these descriptions are confirmed by the very save the
        # suggestions feed into — lets description_candidates use them.
        "descriptions_confirmed_by": "wizard",
    }
    # Child FIRST: the (0, j) pairs enumerate before any registered-vs-
    # registered pair, so MAX_CANDIDATES_PER_SOURCE can't starve child pairs.
    tables = [child] + [t for t in registered if t.get("id") != child_id]
    fk_map = {child_id: {"ok": True,
                         "foreign_keys": body.get("foreign_keys") or []}}
    cands = relation_discovery.merge_candidates(
        relation_discovery.fk_candidates(tables, fk_map),
        relation_discovery.name_candidates(tables),
        relation_discovery.description_candidates(tables))
    cands = [c for c in cands if child_id in (c["table_id"], c["related_table_id"])]
    cands = relation_discovery.filter_same_physical(cands, tables)
    cands = relation_discovery.filter_existing_physical(cands, tables)
    cands = relation_discovery.dedupe_physical_targets(cands, tables)

    def _swap_orientation(c):
        c["table_id"], c["related_table_id"] = c["related_table_id"], c["table_id"]
        c["table_label"], c["related_label"] = c["related_label"], c["table_label"]
        c["join_keys"] = [[b, a] for a, b in c["join_keys"]]
        if c.get("cardinality") == "N:1":
            c["cardinality"] = "1:N"
        elif c.get("cardinality") == "1:N":
            c["cardinality"] = "N:1"
        c["child_unique"], c["parent_unique"] = \
            c.get("parent_unique"), c.get("child_unique")

    # Pre-normalize BEFORE verification so overlap is measured in the spec's
    # direction — share of SAMPLE key values found in the parent snapshot —
    # even when the generation heuristics oriented the registered table as
    # child. (FK candidates are generated wizard-child already.)
    for c in cands:
        if c["table_id"] != child_id:
            _swap_orientation(c)

    sample_df = _sample_frame(body.get("sample"))

    def loader(tid, wanted):
        if tid == child_id:
            if sample_df is None or any(c not in sample_df.columns for c in wanted):
                return None
            return sample_df[wanted]
        return _snapshot_key_loader(tid, wanted)

    out = []
    for c in relation_discovery.verify_candidates(cands, loader):
        if c["table_id"] != child_id:
            # The data flip put the registered side back as child (wizard side
            # unique, snapshot side not). Restore the wizard as stored child —
            # the wizard save can only write onto the table being saved — and
            # DROP the measured numbers: they describe snapshot→capped-sample,
            # which systematically understates overlap in the stored
            # direction. The structural facts (uniqueness → cardinality) hold.
            _swap_orientation(c)
            c["overlap_pct"] = None
            c["orphans"] = None
            c["child_nonnull"] = None
        if "fk" in c.get("sources", ()):
            # FK direction is ground truth and sample uniqueness is noise —
            # default to N:1 unless the PARENT side was MEASURED non-unique
            # (that is a real data-quality signal worth surfacing).
            if c.get("parent_unique") is not False:
                c["cardinality"] = "N:1"
        c["estimated"] = bool(c.get("verified"))   # child side is a sample
        c["precheck"] = "fk" in c.get("sources", ())
        out.append(c)
    out.sort(key=lambda c: (0 if c["precheck"] else 1,
                            -(c.get("overlap_pct") or 0.0),
                            c.get("related_label") or ""))
    return out


@router.post("/relations/wizard_suggest")
async def wizard_suggest(request: Request):
    """Relation suggestions for the register wizard's relations step. Computed
    ONLY from state the wizard already holds (the table's own introspection
    FKs + preview sample + typed descriptions) plus registry metadata and
    parent snapshots — no live DB access, nothing written. Sample row values
    stay in-process; the audit row carries counts only (Article II)."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    table_name = (body.get("table_name") or "").strip()
    if not table_name:
        return JSONResponse({"error": "table_name is required."}, status_code=400)
    if not _in_scope(scope, (body.get("connection_id") or "").strip(),
                     (body.get("schema") or "").strip() or None):
        return _out_of_scope()
    store = db_sources.DataSourceStore()
    editing_tid = (body.get("editing_tid") or "").strip()
    if editing_tid and db_sources.DataSourceStore.valid_id(editing_tid):
        own = store.get_table(editing_tid)
        if own is not None and not _in_scope(scope, own.get("connection_id"),
                                             own.get("schema")):
            return _out_of_scope()
    registered = _scoped_tables(scope, store.list_tables())
    try:
        cands = await _run(_wizard_suggest_candidates, body, registered)
    except Exception as e:
        # A malformed body field must degrade, not 500 (Article IV). Only the
        # exception TYPE is logged — the body carries sample row values.
        log_with_sid(email, "warning",
                     f"REL_WIZARD_SUGGEST_FAILED table={table_name}: {type(e).__name__}")
        return {"ok": False, "error": "Could not compute suggestions."}
    db_sources.audit(email, "relations.wizard_suggest",
                     target=f"{(body.get('schema') or '').strip()}.{table_name}",
                     detail={"candidates": len(cands),
                             "fk": sum(1 for c in cands if "fk" in c["sources"]),
                             "registered": len(registered)},
                     actor_kind=_kind(scope))
    return {"ok": True, "candidates": cands}


@router.post("/relations/accept")
async def accept_relations(request: Request):
    """Write accepted relation candidates into the registry. Body
    {relations: [{table_id, related_table_id, join_keys, cardinality?, origin?,
    replaces?: {related_table_id|related_table, join_keys}}]} (bulk accept =
    the same endpoint; `replaces` = the overview Edit path — the matching old
    entry is swapped for the new one in the same write, and when the edited
    entry would duplicate ANOTHER existing entry it is skipped WITH the old
    entry left untouched, never a silent delete). Deliberately NO confirm
    gate, NO SCHEMA_DRIFT check, and NO re-snapshot: those locks protect the
    column/description shape, which this endpoint cannot touch — it only
    edits `relations` on the child table's doc."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)

    def _reject(msg: str, status: int = 400, code: str | None = None):
        # A rejected write is an admin action too — leave an ok:false trace
        # (parity with admin.denied; previously a failed accept vanished).
        db_sources.audit(email, "relations.accept", ok=False,
                         detail={"error": msg[:200]}, actor_kind=_kind(scope))
        payload = {"error": msg}
        if code:
            payload["code"] = code
        return JSONResponse(payload, status_code=status)

    items = body.get("relations")
    if not isinstance(items, list) or not items:
        return _reject("relations list is required.")

    store = db_sources.DataSourceStore()
    by_child: dict = {}
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return _reject(f"relations[{idx}] must be an object.")
        child = store.get_table((item.get("table_id") or "").strip())
        parent = store.get_table((item.get("related_table_id") or "").strip())
        if child is None or parent is None:
            return _reject(f"relations[{idx}]: unknown table id.")
        # A power user may relate only tables inside their managed scope —
        # BOTH sides (the child doc is the one mutated).
        for side in (child, parent):
            if not _in_scope(scope, side.get("connection_id"), side.get("schema")):
                return _reject(
                    f"'{side.get('display_name') or side.get('table_name')}' "
                    "is outside your managed scope.",
                    status=403, code="OUT_OF_SCOPE")
        jk = item.get("join_keys")
        ok_shape = (isinstance(jk, list) and jk and all(
            isinstance(p, (list, tuple)) and len(p) == 2
            and str(p[0] or "").strip() and str(p[1] or "").strip() for p in jk))
        if not ok_shape:
            return _reject(f"relations[{idx}]: join_keys must be a non-empty "
                           "list of [child_col, parent_col] pairs.")
        jk = [[str(p[0]).strip(), str(p[1]).strip()] for p in jk]
        jk = list(dict.fromkeys(map(tuple, jk)))       # drop repeated pairs
        jk = [list(p) for p in jk]
        # Per-side, per-column messages naming the table (BUG B was this
        # check formatting the pair as one fused 'a=b' token plus leaking
        # relations[idx] internals — unreadable for the admin).
        child_cols = {c.get("name") for c in (child.get("columns") or [])}
        parent_cols = {c.get("name") for c in (parent.get("columns") or [])}
        for a, b in jk:
            if a not in child_cols:
                return _reject(f"Column '{a}' does not exist on "
                               f"'{child.get('display_name') or child.get('table_name')}'.")
            if b not in parent_cols:
                return _reject(f"Column '{b}' does not exist on "
                               f"'{parent.get('display_name') or parent.get('table_name')}'.")
        cardinality = item.get("cardinality")
        if cardinality is not None and cardinality not in _REL_CARDINALITIES:
            return _reject(f"relations[{idx}]: invalid cardinality.")
        origin = item.get("origin")
        if origin is not None and origin not in _REL_ORIGINS:
            return _reject(f"relations[{idx}]: invalid origin.")
        replaces = item.get("replaces")
        if replaces is not None:
            rep_ref = (str(replaces.get("related_table_id")
                           or replaces.get("related_table") or "").strip()
                       if isinstance(replaces, dict) else "")
            rep_jk = replaces.get("join_keys") if isinstance(replaces, dict) else None
            ok_rep = (rep_ref and isinstance(rep_jk, list) and rep_jk and all(
                isinstance(p, (list, tuple)) and len(p) == 2 for p in rep_jk))
            if not ok_rep:
                return _reject(f"relations[{idx}]: invalid replaces.")
            # The relation being REPLACED is removed: its related table must
            # be in scope as well, like both sides of the new one.
            if not _rel_ref_in_scope(scope, store, rep_ref):
                return _reject("The relation being replaced relates a table "
                               "outside your managed scope.",
                               status=403, code="OUT_OF_SCOPE")
            # No truncation: the ref is compared against stored values, never
            # stored itself — a shortened ref could silently stop matching.
            replaces = {"ref": rep_ref,
                        "join_keys": [[str(p[0]), str(p[1])] for p in rep_jk]}
        by_child.setdefault(child["id"], []).append(
            {"parent": parent, "join_keys": jk,
             "cardinality": cardinality, "origin": origin, "replaces": replaces})

    accepted = skipped = replaced = 0
    # ONE read-modify-write per child table: upsert_table is a full replace,
    # so per-item writes to the same doc would lose all but the last.
    for tid, batch in by_child.items():
        doc = store.get_table(tid)
        if doc is None:
            log_with_sid(email, "warning", f"REL_ACCEPT_TABLE_VANISHED table={tid}")
            continue
        existing = doc.get("relations") or []

        def entry_key(rel):
            rid = rel.get("related_table_id") or rel.get("related_table")
            try:
                pairs = [(str(p[0]), str(p[1])) for p in (rel.get("join_keys") or [])]
            except Exception as e:
                log_with_sid(email, "warning",
                             f"REL_ACCEPT_MALFORMED_EXISTING table={tid}: {type(e).__name__}")
                return None
            if not rid or not pairs:
                return None
            return relation_discovery.candidate_id(
                tid, [p[0] for p in pairs], str(rid), [p[1] for p in pairs])

        changed = False
        for item in batch:
            rep = item.get("replaces")
            is_old = (lambda r: isinstance(r, dict)
                      and _rel_matches(r, rep["ref"], rep["join_keys"])) if rep \
                else (lambda r: False)
            # Dup-check against everything EXCEPT the entries being replaced —
            # so an edit never self-collides, and an edit that duplicates a
            # DIFFERENT entry is skipped with the old entry left untouched
            # (never a silent delete). A stale `replaces` (old entry already
            # gone) degrades to a plain accept.
            other_ids = {entry_key(r) for r in existing
                         if isinstance(r, dict) and not is_old(r)} - {None}
            cand_key = relation_discovery.candidate_id(
                tid, [p[0] for p in item["join_keys"]],
                item["parent"]["id"], [p[1] for p in item["join_keys"]])
            if cand_key in other_ids:
                skipped += 1
                continue
            old_count = sum(1 for r in existing if is_old(r))
            if old_count:
                existing = [r for r in existing if not is_old(r)]
                replaced += old_count
            rel = {"related_table_id": item["parent"]["id"],
                   "join_keys": item["join_keys"]}
            if item["origin"]:
                rel["origin"] = item["origin"]
            if item["cardinality"]:
                rel["cardinality"] = item["cardinality"]
            existing.append(rel)
            accepted += 1
            changed = True
        if changed:
            doc["relations"] = existing
            await _run(store.upsert_table, doc, actor=email,
                       actor_kind=_kind(scope))

    db_sources.audit(email, "relations.accept",
                     detail={"accepted": accepted, "skipped": skipped,
                             "replaced": replaced,
                             "tables": sorted(by_child.keys())},
                     actor_kind=_kind(scope))
    return {"ok": True, "accepted": accepted, "skipped": skipped,
            "replaced": replaced}


@router.post("/relations/dismiss")
async def dismiss_relation(request: Request):
    """Audit trail for a dismissed candidate. Dismissals are session-local by
    design (no persistence) — this endpoint exists so the action is on the
    record like every other ladmin decision."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    tid = (body.get("table_id") or "").strip()
    rid = (body.get("related_table_id") or "").strip()
    if not (db_sources.DataSourceStore.valid_id(tid)
            and db_sources.DataSourceStore.valid_id(rid)):
        return JSONResponse({"error": "Unknown table id."}, status_code=400)
    if scope is not None:
        store = db_sources.DataSourceStore()
        for ref in (tid, rid):
            doc = store.get_table(ref)
            if doc is not None and not _in_scope(scope, doc.get("connection_id"),
                                                 doc.get("schema")):
                return _out_of_scope()
    jk = body.get("join_keys")
    jk = [[str(p[0])[:128], str(p[1])[:128]] for p in jk
          if isinstance(p, (list, tuple)) and len(p) == 2] if isinstance(jk, list) else []
    band = body.get("band")
    db_sources.audit(email, "relations.dismiss", target=f"{tid}->{rid}",
                     detail={"join_keys": jk,
                             "band": band if band in ("confirmed", "suggested",
                                                      "attention") else None},
                     actor_kind=_kind(scope))
    return {"ok": True}


@router.post("/relations/graph")
async def relations_graph(request: Request):
    """Graph-view data for the Relations section: registered tables as nodes,
    confirmed relations as edges, connected components/isolated flags, and
    dashed ghost nodes for OPEN recommended tables (server-side, persistent)
    unioned with any body-passed last-scan refs (kept for compatibility —
    the graph never introspects). Dismissed recommendations never render.
    Read-only."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    store = db_sources.DataSourceStore()
    tables = _scoped_tables(scope, store.list_tables())
    refs: dict = {}
    for r in body.get("unregistered_refs") or []:
        if isinstance(r, dict) and r.get("table"):
            key = (str(r.get("connection_id") or ""),
                   str(r.get("schema") or "").lower(),
                   str(r.get("table") or "").lower())
            refs.setdefault(key, dict(r))
    for rec in await _run(store.list_recommendations):
        if rec.get("status") != "open" or not rec.get("table"):
            continue
        if not _in_scope(scope, rec.get("connection_id"), rec.get("schema")):
            continue
        key = (str(rec.get("connection_id") or ""),
               str(rec.get("schema") or "").lower(),
               str(rec.get("table") or "").lower())
        entry = refs.setdefault(key, {
            "connection_id": rec.get("connection_id"),
            "schema": rec.get("schema"), "table": rec.get("table"),
            "referenced_by": [], "referenced_by_ids": []})
        entry.setdefault("evidence", []).extend(
            ev for ev in (rec.get("evidence") or []) if isinstance(ev, dict))
    graph = await _run(relation_discovery.build_graph, tables,
                       [refs[k] for k in sorted(refs)])
    return {"ok": True, "nodes": graph["nodes"], "edges": graph["edges"]}


@router.get("/relations/recommendations")
async def list_recommendations(request: Request):
    """The persistent "Recommended tables" (SQL + FK evidence), enriched at
    read time with the dynamic role and resolved join partners. Bridge rows
    rank above referenced rows; within a role, by frequency. Dismissed rows
    are included (flagged by status) so the UI can offer show/restore."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    store = db_sources.DataSourceStore()
    tables = _scoped_tables(scope, store.list_tables())
    out = []
    for rec in await _run(store.list_recommendations):
        if not _in_scope(scope, rec.get("connection_id"), rec.get("schema")):
            continue
        summary = relation_discovery.recommendation_summary(rec, tables)
        out.append({**rec, **summary})
    out.sort(key=lambda r: (0 if r.get("role") == "bridge" else 1,
                            -(int(r.get("frequency") or 0)),
                            str(r.get("schema") or ""),
                            str(r.get("table") or "")))
    return {"ok": True, "recommendations": out}


@router.post("/relations/recommendations/status")
async def recommendation_status(request: Request):
    """Persistent dismiss / restore for one recommended table. A dismissed
    recommendation never reappears on later scans/analyzes until restored;
    registered ones are immutable here (the registry owns them)."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    rid = (body.get("id") or "").strip()
    status = (body.get("status") or "").strip()
    if not db_sources.DataSourceStore.valid_id(rid) or \
            status not in ("open", "dismissed"):
        return JSONResponse(
            {"error": "id and status (open|dismissed) are required."},
            status_code=400)
    store = db_sources.DataSourceStore()
    if scope is not None:
        target = next((r for r in await _run(store.list_recommendations)
                       if r.get("id") == rid), None)
        if target is not None and not _in_scope(scope, target.get("connection_id"),
                                                target.get("schema")):
            return _out_of_scope()
    rec = await _run(store.set_recommendation_status,
                     rid, status, email, actor_kind=_kind(scope))
    if rec is None:
        return JSONResponse(
            {"error": "Unknown recommendation (or already registered)."},
            status_code=404)
    return {"ok": True, "recommendation": rec}


def _classify_fallback() -> dict:
    """The classifier's own degraded answer — asked for, never re-spelled
    here, so the wording can't drift between the two."""
    return relation_discovery.classify_table_type([])


@router.post("/relations/recommendations/classify")
async def classify_recommendation(request: Request):
    """Suggest how a recommended table should be registered (connector vs
    normal) so the accept dialog can default the admin's choice instead of
    silently hard-coding connector. Metadata only: one bounded introspection
    of column names + dtypes, no brain call, no data values, no writes.

    A SUGGESTION only — the admin confirms or flips it, and the registration
    uses their choice. Any failure degrades to the historical default rather
    than blocking Accept; the real error surfaces on the accept attempt."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    rid = (body.get("id") or "").strip()
    store = db_sources.DataSourceStore()
    rec = next((r for r in await _run(store.list_recommendations)
                if r.get("id") == rid), None)
    if rec is None:
        return JSONResponse({"error": "Unknown recommendation."},
                            status_code=404)
    if not _in_scope(scope, rec.get("connection_id"), rec.get("schema")):
        return _out_of_scope()
    cfg, password, resp = _conn_cfg_and_password(
        store, {"connection_id": str(rec.get("connection_id") or "")})
    if resp is not None:
        return {"ok": True, "classified": False, **_classify_fallback()}
    schema = (rec.get("schema") or "").strip() or None
    table = str(rec.get("table") or "").strip()
    intro = await _run(db_connector.introspect, cfg, password, schema, table,
                       sid=f"admin:{email}")
    if not intro.get("ok"):
        log_with_sid(email, "warning",
                     f"REC_CLASSIFY_UNAVAILABLE rid={rid} table={table}")
        return {"ok": True, "classified": False, **_classify_fallback()}
    out = relation_discovery.classify_table_type(intro.get("columns") or [])
    log_with_sid(email, "info",
                 f"REC_CLASSIFY rid={rid} table={table} "
                 f"suggested={out['suggested_type']}")
    return {"ok": True, "classified": True, **out}


@router.post("/relations/recommendations/accept")
async def accept_recommendation(request: Request):
    """One-click registration of a recommended table as the type the ADMIN
    chose in the accept dialog (connector or normal — the classifier only
    suggests): introspect → AI-draft descriptions (the existing draft
    mechanism) → register + snapshot (the same doc shape and snapshot path as
    the wizard save), then replay the stored SQL evidence through the NORMAL
    candidate pipeline so relations are proposed instantly — proposed, never
    auto-confirmed. The UI confirm dialog is the review act (it states the
    descriptions are AI-drafted and editable later in the table's settings).
    Any failure leaves NO half-registered state and the recommendation open;
    connectivity-style failures return 200 {ok:false} naming the dependency
    that failed. Every dependency on this path is time-bounded."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    rid = (body.get("id") or "").strip()
    chosen_type = str(body.get("chosen_type") or "connector").strip().lower()
    if chosen_type not in _TABLE_TYPES:
        return JSONResponse(
            {"error": "chosen_type must be 'connector' or 'normal'."},
            status_code=400)
    # Audit metadata only (what the dialog offered vs what the admin picked) —
    # deliberately not recomputed here: it would cost a second introspection
    # and cannot change what gets registered.
    suggested_type = str(body.get("suggested_type") or "").strip().lower()
    if suggested_type not in _TABLE_TYPES:
        suggested_type = None
    store = db_sources.DataSourceStore()
    rec = next((r for r in await _run(store.list_recommendations)
                if r.get("id") == rid), None)
    if rec is None:
        return JSONResponse({"error": "Unknown recommendation."},
                            status_code=404)
    if rec.get("status") != "open":
        return JSONResponse(
            {"error": f"Recommendation is {rec.get('status')}."},
            status_code=400)
    if not _in_scope(scope, rec.get("connection_id"), rec.get("schema")):
        return _out_of_scope()
    cid = str(rec.get("connection_id") or "")
    cfg, password, resp = _conn_cfg_and_password(store, {"connection_id": cid})
    if resp is not None:
        return resp
    schema = (rec.get("schema") or "").strip() or None
    table = str(rec.get("table") or "").strip()

    # Registered by another path since the rec was written? The end-state the
    # admin wants already exists — sync statuses and report success.
    new_key = relation_discovery.physical_key(
        {"connection_id": cid, "schema": schema or "", "table_name": table})
    if any(relation_discovery.physical_key(t) == new_key
           for t in store.list_tables()):
        await _run(store.sync_recommendations)
        db_sources.audit(email, "relations.rec_accept", target=rid, ok=True,
                         detail={"table": f"{schema}.{table}" if schema else table,
                                 "note": "already_registered",
                                 "suggested_type": suggested_type,
                                 "chosen_type": chosen_type},
                         actor_kind=_kind(scope))
        return {"ok": True, "status": "registered",
                "note": "This table is already registered.", "candidates": []}

    phys = f"{schema}.{table}" if schema else table

    def _phase(name: str) -> None:
        # Entry (not completion) logging: a wedged accept used to leave NO log
        # line at all, because every dependency logs only once it returns.
        log_with_sid(email, "info",
                     f"REC_ACCEPT_PHASE phase={name} rid={rid} table={phys}")

    def _register():
        _phase("introspect")
        intro = db_connector.introspect(cfg, password, schema, table,
                                        sid=f"admin:{email}")
        if not intro.get("ok"):
            return {"ok": False, "error": intro.get("error")}
        # A recommendation never registers live: a table at or above the
        # force threshold cannot be snapshotted, so it is refused here,
        # before the (brain) draft and before anything is written.
        _phase("count")
        count_res = db_connector.count_rows(
            cfg, password, intro.get("schema"), intro.get("table") or table,
            sid=f"admin:{email}")
        if _size_verdict(intro, count_res).get("live_required"):
            return {"ok": False, "code": "LIVE_REQUIRED",
                    "error": _LIVE_REQUIRED_TEXT}
        _phase("draft")
        draft = _draft_table_descriptions(cfg, password, schema, table, email,
                                          intro=intro)
        if not draft.get("ok"):
            return {"ok": False,
                    "error": (draft.get("error") or "AI drafting failed.")
                    + " Use “Edit first” to register with manually "
                      "written descriptions."}
        dcols = (draft.get("draft") or {}).get("columns") or {}
        columns = [{
            "name": c.get("name"), "dtype": c.get("dtype") or "",
            "description": str(dcols.get(str(c.get("name")), "") or "").strip(),
            "indexed": bool(c.get("indexed")), "pk": bool(c.get("pk")),
        } for c in (intro.get("columns") or []) if c.get("name")]
        doc = _build_table_doc(
            tid="", connection_id=cid, schema=schema, table=table,
            # Same humanized-name convention the wizard prefills client-side.
            display_name=table.lower().replace("_", " "),
            description=(draft.get("draft") or {}).get("table_description") or "",
            columns=columns, is_connector=(chosen_type == "connector"),
            relations=[],
            intro=intro, where_filter=None, row_cap=None, email=email)
        _phase("register")
        saved = store.upsert_table(doc, actor=email, actor_kind=_kind(scope))
        import db_scheduler
        _phase("snapshot")
        snap = db_scheduler.refresh_one_table(saved["id"], actor=email,
                                              actor_kind=_kind(scope))
        if not snap.get("ok"):
            # Accept is one atomic gesture: unlike the wizard save (which
            # keeps the registration for a manual "Refresh now"), roll the
            # registration back so no half-registered state remains — the
            # store's reconcile hook restores the rec to open. A nightly-
            # scheduler race can at worst leave an orphan parquet, the same
            # window a plain table delete has today.
            store.delete_table(saved["id"], actor=email, actor_kind=_kind(scope))
            return {"ok": False,
                    "error": (snap.get("error") or "Snapshot failed")
                    + " — the table was not registered."}
        return {"ok": True, "table_id": saved["id"], "snapshot": snap}

    t0 = time.monotonic()
    res = await _run(_register)
    log_with_sid(email, "info",
                 f"REC_ACCEPT_DONE rid={rid} table={phys} "
                 f"ok={bool(res.get('ok'))} elapsed_s={time.monotonic() - t0:.2f}")
    db_sources.audit(email, "relations.rec_accept", target=rid,
                     ok=bool(res.get("ok")),
                     detail={"table": phys, "suggested_type": suggested_type,
                             "chosen_type": chosen_type},
                     actor_kind=_kind(scope))
    if not res.get("ok"):
        if res.get("code") == "LIVE_REQUIRED":
            return JSONResponse({"ok": False, "error": res.get("error"),
                                 "code": "LIVE_REQUIRED"}, status_code=400)
        return {"ok": False, "error": res.get("error")}

    # Replay the stored SQL evidence through the NORMAL candidate pipeline
    # (same chain as analyze_sql). FK evidence is not replayed — the next
    # scan's live introspection re-derives it as ground truth. v4.1: invalid
    # evidence pairs are excluded by the replay validator and surfaced.
    tables = _scoped_tables(scope, store.list_tables())
    warnings = _collect_evidence_warnings([rec], tables, email)
    cands = relation_discovery.recommendation_candidates(rec, tables)
    cands = relation_discovery.filter_same_physical(cands, tables)
    cands = relation_discovery.filter_existing_physical(cands, tables)
    cands = relation_discovery.dedupe_physical_targets(cands, tables)
    cands = await _run(_verify_and_band, cands)
    return {"ok": True, "table": store.get_table(res["table_id"]),
            "snapshot": res["snapshot"], "candidates": cands,
            "evidence_warnings": warnings}


@router.post("/relations/delete")
async def delete_relation(request: Request):
    """Remove a confirmed relation from the owning (child) table's doc.
    Matches by related ref (id or legacy `related_table` name) + ORDERED
    join_keys, and removes EVERY exact match — identical duplicates are
    indistinguishable in the overview UI. Distinct from /relations/dismiss,
    which is audit-only and never mutates the registry."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    tid = (body.get("table_id") or "").strip()
    if not db_sources.DataSourceStore.valid_id(tid):
        return JSONResponse({"error": "Unknown table id."}, status_code=400)
    ref = str(body.get("related_table_id")
              or body.get("related_table") or "").strip()
    jk = body.get("join_keys")
    if not ref or not isinstance(jk, list) or not jk or not all(
            isinstance(p, (list, tuple)) and len(p) == 2 for p in jk):
        return JSONResponse(
            {"error": "related table ref and join_keys pairs are required."},
            status_code=400)
    jk = [[str(p[0]), str(p[1])] for p in jk]
    store = db_sources.DataSourceStore()
    doc = store.get_table(tid)
    if doc is None:
        return JSONResponse({"error": "Unknown table."}, status_code=404)
    if not _in_scope(scope, doc.get("connection_id"), doc.get("schema")):
        return _out_of_scope()
    if db_sources.DataSourceStore.valid_id(ref):
        parent = store.get_table(ref)
        if parent is not None and not _in_scope(scope, parent.get("connection_id"),
                                                parent.get("schema")):
            return _out_of_scope()
    kept = [r for r in (doc.get("relations") or [])
            if not (isinstance(r, dict) and _rel_matches(r, ref, jk))]
    removed = len(doc.get("relations") or []) - len(kept)
    if not removed:
        return JSONResponse({"error": "Relation not found on this table."},
                            status_code=404)
    doc["relations"] = kept
    await _run(store.upsert_table, doc, actor=email, actor_kind=_kind(scope))
    db_sources.audit(email, "relations.delete", target=f"{tid}->{ref}",
                     detail={"join_keys": jk, "removed": removed},
                     actor_kind=_kind(scope))
    return {"ok": True, "removed": removed}


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

@router.get("/tables")
async def list_tables(request: Request):
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    rows = _scoped_tables(scope, db_sources.DataSourceStore().list_tables())
    # Additive, derived on every read from the STORED row count (rewritten by
    # every snapshot refresh and every live profile) x the column count, so
    # the list shows the live suggestion after any refresh.
    out = []
    for t in rows:
        row = dict(t)
        v = _stored_verdict(t)
        row["mode"] = db_sources.table_mode(t)
        row["cell_count"] = v["cell_count"]
        row["live_suggested"] = v["live_suggested"]
        row["live_required"] = v["live_required"]
        out.append(row)
    return {"tables": out}


@router.get("/my_roles")
async def my_roles(request: Request):
    """The caller's HELD roles (19f) — what the power-mode wizard's "Share
    with your roles" panel offers and locks against. Held roles only, never
    the whole registry (that stays ladmin's GET /roles), and the built-in
    Base role is excluded even when held: everyone is a member, so sharing
    through it would publish to the whole platform — an administrator
    action (save_table refuses it server-side too)."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    import roles_store
    rows = [{"id": r.get("id"), "name": r.get("name"),
             "is_base": False,
             "table_ids": r.get("table_ids") or [],
             "scope_grants": r.get("scope_grants") or []}
            for r in roles_store.roles_for_email(email)
            if r.get("id") != roles_store.BASE_ROLE_ID]
    return {"roles": rows}


@router.post("/tables/introspect")
async def introspect_table(request: Request):
    """Inspector introspection + first-rows preview for the registration
    wizard. Preview values go only to the admin's browser — never the brain."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    # A power user may introspect only saved connections inside their scope —
    # the unsaved-draft path (no connection_id) is inherently out of scope,
    # since no grant can reference an unsaved connection.
    if not _in_scope(scope, (body.get("connection_id") or "").strip(),
                     (body.get("schema") or "").strip() or None):
        return _out_of_scope()
    store = db_sources.DataSourceStore()
    cfg, password, resp = _conn_cfg_and_password(store, body)
    if resp is not None:
        return resp
    schema = (body.get("schema") or "").strip() or None
    table = (body.get("table") or "").strip()
    if not table:
        return JSONResponse({"error": "table is required."}, status_code=400)
    intro = await _run(db_connector.introspect, cfg, password, schema, table,
                       sid=f"admin:{email}")
    if not intro.get("ok"):
        return intro
    # The RESOLVED identifiers from the introspection — in-process they carry
    # the case-sensitivity flag (quoted_name), so a physically case-sensitive
    # schema/table/column compiles quoted in the preview SELECT.
    preview = await _run(db_connector.preview_rows, cfg, password,
                         intro.get("schema"), intro.get("table") or table,
                         limit=int(body.get("preview_rows") or settings.DB_PREVIEW_ROWS),
                         # Explicit columns → the frame is keyed by the
                         # INTROSPECTED names on every dialect (Oracle's cursor
                         # reports them uppercase, which matches nothing else).
                         columns=[c.get("name") for c in (intro.get("columns") or [])
                                  if c.get("name")],
                         sid=f"admin:{email}")
    # Size verdict for the storage choice (snapshot / live): an exact
    # COUNT(*) under a capped statement timeout, the catalog estimate when
    # the count fails, "unknown" when both are missing.
    count_res = await _run(db_connector.count_rows, cfg, password,
                           intro.get("schema"), intro.get("table") or table,
                           where=(body.get("where_filter") or "").strip() or None,
                           sid=f"admin:{email}")
    size_verdict = _size_verdict(intro, count_res)
    db_sources.audit(email, "table.introspect", target=f"{schema}.{table}",
                     detail={"columns": len(intro.get("columns") or []),
                             "degraded": intro.get("degraded")},
                     actor_kind=_kind(scope))
    return {"ok": True, "introspection": intro, "preview": preview,
            "degraded": intro.get("degraded") or [],
            "size_verdict": size_verdict,
            # Pre-ticks the wizard's connector box from the columns already in
            # hand (no extra round-trip). A suggestion the admin edits freely.
            "classification": relation_discovery.classify_table_type(
                intro.get("columns") or [])}


# ---------------------------------------------------------------------------
# Live mode: size verdict + sampled profile
# ---------------------------------------------------------------------------

# Who/when/why a table went live — carried through an edit-save.
_LIVE_STAMP_KEYS = ("live_reason", "live_set_by", "live_set_at")
# Every live-only key; all removed when a save turns the table to snapshot.
# The last two are re-stamped by each live profile, so an edit-save does not
# carry them.
_LIVE_DOC_KEYS = _LIVE_STAMP_KEYS + ("live_profiled_at", "live_sample_rows")

_LIVE_REQUIRED_TEXT = ("This table is above the snapshot size limit. A "
                       "snapshot is refused; register it in live mode.")


def _verdict_for(row_count, ncols: int) -> tuple:
    """(cell_count, live_suggested, live_required) for a row count and a
    column count; an unknown row count gives no verdict (fail-open). The
    effective force threshold is max(force, cell)."""
    if isinstance(row_count, bool) or not isinstance(row_count, (int, float)):
        return None, False, False
    cell = int(row_count) * int(ncols)
    cell_t = int(settings.LIVE_MODE_CELL_THRESHOLD)
    force_t = max(int(settings.LIVE_MODE_FORCE_THRESHOLD), cell_t)
    return cell, cell >= cell_t, cell >= force_t


def _size_verdict(intro: dict, count_res, row_cap=None) -> dict:
    """The storage verdict from an introspection + a `count_rows` result:
    ok => the exact count; timed out => treated as above the force threshold
    (live required); any other failure => the catalog estimate; both missing
    => unknown (no suggestion, no refusal). A `row_cap` bounds the snapshot,
    so a known row count is taken at most at the cap; a timeout under a cap
    whose cap x columns stays below the force threshold reads as
    `count_source: "cap"` with the cap as the row count (not required),
    otherwise a timeout stays required."""
    cap = _safe_int(row_cap)
    ncols = len([c for c in ((intro or {}).get("columns") or []) if c.get("name")])
    count_res = count_res if isinstance(count_res, dict) else {}
    if count_res.get("ok") and count_res.get("count") is not None:
        row_count = int(count_res["count"])
        if cap:
            row_count = min(row_count, cap)
        cell, suggested, required = _verdict_for(row_count, ncols)
        return {"row_count": row_count, "count_source": "count",
                "timed_out": False, "cell_count": cell,
                "live_suggested": suggested, "live_required": required}
    if count_res.get("timed_out"):
        if cap:
            # The snapshot copies at most `cap` rows: when even that many
            # stay under the force threshold, the count timeout does not
            # make live required.
            cell, suggested, required = _verdict_for(cap, ncols)
            if not required:
                return {"row_count": cap, "count_source": "cap",
                        "timed_out": True, "cell_count": cell,
                        "live_suggested": suggested, "live_required": False}
        return {"row_count": None, "count_source": None, "timed_out": True,
                "cell_count": None, "live_suggested": True,
                "live_required": True}
    est = (intro or {}).get("row_count_estimate")
    if isinstance(est, (int, float)) and not isinstance(est, bool):
        est = min(int(est), cap) if cap else int(est)
        cell, suggested, required = _verdict_for(est, ncols)
        return {"row_count": est, "count_source": "estimate",
                "timed_out": False, "cell_count": cell,
                "live_suggested": suggested, "live_required": required}
    return {"row_count": None, "count_source": None, "timed_out": False,
            "cell_count": None, "live_suggested": False,
            "live_required": False}


def _stored_verdict(doc: dict) -> dict:
    """The verdict derived from a registry row's STORED row count (rewritten
    by every snapshot refresh and live profile) x its column count."""
    ncols = len([c for c in ((doc or {}).get("columns") or [])
                 if isinstance(c, dict) and c.get("name")])
    cell, suggested, required = _verdict_for((doc or {}).get("row_count"), ncols)
    return {"cell_count": cell, "live_suggested": suggested,
            "live_required": required}


def _profile_live_table(tid: str, cfg: dict, password: str, doc: dict,
                        count_res, actor: str, actor_kind=None) -> dict:
    """Sampled profile of a LIVE table — no parquet is written. Sample (the
    dialect's own row limit, LIVE_PROFILE_SAMPLE_ROWS) -> compute_profile with
    the counted rows as `total_rows` (marked sampled) -> the sidecar profile
    at db_profile_path with the live stamp -> technical descriptions from the
    sample -> `mark_live_profiled`. Never raises (Article IV): returns
    {ok, rows, sample_rows, profiled_at} or {ok: False, error}."""
    sid = f"admin:{actor}"
    try:
        import dataset_profile
        import local_store
        schema = db_connector.qname(doc.get("schema") or None,
                                    doc.get("schema_quote"))
        table = db_connector.qname(doc.get("table_name"), doc.get("table_quote"))
        cols = [db_connector.col_ident(c) for c in (doc.get("columns") or [])
                if isinstance(c, dict) and c.get("name")]
        cap = _safe_int(doc.get("row_cap"))
        limit = db_connector.LIVE_PROFILE_SAMPLE_ROWS
        if cap:
            limit = min(limit, cap)
        res = db_connector.sample_rows(cfg, password, schema, table,
                                       columns=cols or None,
                                       where=doc.get("where_filter") or None,
                                       limit=limit, sid=sid)
        if not res.get("ok") or res.get("df") is None:
            return {"ok": False, "error": res.get("error") or "Sample query failed."}
        df = res["df"]
        n_sample = int(len(df))
        count_res = count_res if isinstance(count_res, dict) else {}
        rows = count_res.get("count") if count_res.get("ok") else doc.get("row_count")
        if isinstance(rows, bool) or not isinstance(rows, (int, float)):
            rows = None
        else:
            rows = int(rows)
            if cap:
                rows = min(rows, cap)
        prof = dataset_profile.compute_profile(
            df, total_rows=rows if rows is not None else n_sample)
        profiled_at = datetime.now(timezone.utc).isoformat()
        local_store.write_profile(
            local_store.db_profile_path(tid), prof,
            {"kind": "live", "profiled_at": profiled_at,
             "sample_rows": n_sample})
        tech = {}
        try:
            tech = {str(c): dataset_profile._generate_technical_description(
                        df[c], n_sample) for c in df.columns}
        except Exception as e:
            log_with_sid(sid, "warning",
                         f"LIVE_TECH_DESC_FAILED table={log_safe_text(tid)} "
                         f"error={type(e).__name__}")
        new_columns = []
        for c in (doc.get("columns") or []):
            if not isinstance(c, dict):
                continue
            col = dict(c)
            td = tech.get(str(col.get("name")))
            if td:
                col["technical_description"] = td
            new_columns.append(col)
        db_sources.DataSourceStore().mark_live_profiled(
            tid, row_count=rows, columns=new_columns, profiled_at=profiled_at,
            sample_rows=n_sample)
        log_with_sid(sid, "info",
                     f"LIVE_PROFILE_OK table={log_safe_text(tid)} rows={rows} "
                     f"sample_rows={n_sample}")
        return {"ok": True, "rows": rows, "sample_rows": n_sample,
                "profiled_at": profiled_at}
    except Exception as e:
        log_with_sid(sid, "error",
                     f"LIVE_PROFILE_FAILED table={log_safe_text(tid)} "
                     f"error={log_safe_text(type(e).__name__, 80)}")
        return {"ok": False, "error": "Profiling the live table failed."}


def _count_and_profile_live(tid: str, actor: str, actor_kind=None) -> dict:
    """Re-count and re-sample a registered live table (Refresh now, and the
    switch to live). Never raises."""
    try:
        store = db_sources.DataSourceStore()
        doc = store.get_table(tid)
        if doc is None:
            return {"ok": False, "error": "Unknown table."}
        conn = store.get_connection(doc.get("connection_id"), with_secret=True)
        if conn is None:
            return {"ok": False, "error": "Connection no longer exists."}
        password = db_sources.decrypt_password(conn.get("password_enc"))
        if password is None and conn.get("password_enc"):
            return {"ok": False,
                    "error": "Stored credential cannot be read — re-enter the "
                             "connection password."}
        count_res = db_connector.count_rows(
            conn, password or "",
            db_connector.qname(doc.get("schema") or None, doc.get("schema_quote")),
            db_connector.qname(doc.get("table_name"), doc.get("table_quote")),
            where=doc.get("where_filter") or None, sid=f"admin:{actor}")
        return _profile_live_table(tid, conn, password or "", doc, count_res,
                                   actor, actor_kind)
    except Exception as e:
        log_with_sid(f"admin:{actor}", "error",
                     f"LIVE_PROFILE_FAILED table={log_safe_text(tid)} "
                     f"error={log_safe_text(type(e).__name__, 80)}")
        return {"ok": False, "error": "Profiling the live table failed."}


def _draft_table_descriptions(cfg: dict, password: str, schema, table: str,
                              email: str, intro: Optional[dict] = None,
                              existing_descriptions: Optional[dict] = None) -> dict:
    """The ONE AI-draft mechanism (the existing schema-autofill brain call) —
    used by the draft_descriptions route, the recommendation Accept and the
    refresh's added-column draft (db_scheduler), so they can never fork.
    Sync (run via _run). Pass a fresh `intro` to skip the internal
    introspection (Accept already has one — no second live catalog
    round-trip against the customer DB). `existing_descriptions`
    ({column: text}) pre-fills the fields so `cols_to_fill` holds only the
    columns without one — the refresh passes its surviving columns' stored
    descriptions to draft only the added columns; the wizard and Accept pass
    nothing. Returns {"ok": True, "draft": {...}, "confirmed": False} or
    {"ok": False, "error": ...}."""
    import pandas as pd
    from routes.upload import _prepare_file_context
    if intro is None:
        intro = db_connector.introspect(cfg, password, schema, table,
                                        sid=f"admin:{email}")
    if not intro.get("ok"):
        return {"ok": False, "error": intro.get("error")}
    # Resolved identifiers (quoted_name in-process) — a case-sensitive
    # schema/table compiles quoted in the sample SELECT.
    if "schema" in intro:
        schema = intro.get("schema")
    table = intro.get("table") or table
    # A larger sample than the visual preview so unique_hints are honest
    # (still truncated by the same SCHEMA_AUTOFILL_* rules files use).
    prev = db_connector.preview_rows(cfg, password, schema, table,
                                     limit=200,
                                     # Keyed by the introspected names, so the
                                     # drafted descriptions land on the columns
                                     # the wizard rows actually carry.
                                     columns=[c.get("name") for c in (intro.get("columns") or [])
                                              if c.get("name")],
                                     sid=f"admin:{email}")
    if not prev.get("ok"):
        return {"ok": False, "error": prev.get("error")}
    df = pd.DataFrame(prev.get("rows") or [], columns=prev.get("columns") or [])
    entry = {"file_name": f"{schema}.{table}" if schema else table,
             "file_description": intro.get("table_comment") or "",
             "schema": {"file_name": table, "fields": {}}}
    if existing_descriptions:
        entry["schema"]["fields"] = {
            str(name): {"description": text}
            for name, text in existing_descriptions.items()
            if isinstance(text, str)}
    ctx = _prepare_file_context(entry["file_name"], df, entry, "")
    try:
        rsp = brain_client.schema_autofill(
            sid=f"dbdraft:{email}",
            fname=ctx["fname"],
            cols_to_fill=ctx["cols_to_fill"],
            unique_hints=ctx["unique_hints"],
            dtypes=ctx["dtypes"],
            file_desc=ctx["file_desc"],
            notes_text="",
            # English per the feature spec — NOT the column-language
            # detection uploaded files use.
            lang_name="English",
            desc_word_limit=settings.SCHEMA_AUTOFILL_DESC_WORD_LIMIT,
            user_email=email,
            # Behind an interactive click: a stalled brain must fail fast and
            # by name, not ride the 180s client-wide default.
            timeout=settings.BRAIN_DRAFT_TIMEOUT,
        )
    # Before the generic handler — the admin needs to know WHICH dependency
    # stalled to act on it (Article IV: named fallback, never silent).
    except brain_client.BrainTimeoutError:
        log_with_sid(email, "warning", f"DB_DRAFT_LLM_TIMEOUT table={table}")
        return {"ok": False,
                "error": "The AI description service did not respond within "
                         f"{int(settings.BRAIN_DRAFT_TIMEOUT)}s."}
    except Exception as e:
        log_with_sid(email, "warning", f"DB_DRAFT_LLM_FAIL table={table}: {e}")
        return {"ok": False, "error": "AI drafting is unavailable right now."}
    return {"ok": True,
            "draft": {"table_description": (rsp.get("file_description") or "").strip(),
                      "columns": rsp.get("columns") or {}},
            "confirmed": False}


@router.post("/tables/draft_descriptions")
async def draft_descriptions(request: Request):
    """AI-drafted table + column descriptions in ENGLISH via the EXISTING
    schema-autofill brain call. PERSISTS NOTHING — the ladmin must review,
    edit, and confirm in the save step; this endpoint has no write path at
    all (one of the four mandatory-confirm locks)."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    if not _in_scope(scope, (body.get("connection_id") or "").strip(),
                     (body.get("schema") or "").strip() or None):
        return _out_of_scope()
    store = db_sources.DataSourceStore()
    cfg, password, resp = _conn_cfg_and_password(store, body)
    if resp is not None:
        return resp
    schema = (body.get("schema") or "").strip() or None
    table = (body.get("table") or "").strip()
    if not table:
        return JSONResponse({"error": "table is required."}, status_code=400)
    res = await _run(_draft_table_descriptions, cfg, password, schema, table, email)
    db_sources.audit(email, "table.draft_descriptions",
                     target=f"{schema}.{table}", ok=bool(res.get("ok")),
                     actor_kind=_kind(scope))
    return res


# A save body without a `where_filter` / `row_cap` key: the stored value is
# kept. An explicit null still clears it.
_NOT_POSTED = object()


def _posted_or_stored(value, existing, key: str):
    """`value`, or the existing doc's `key` when `value` is `_NOT_POSTED`."""
    if value is not _NOT_POSTED:
        return value
    return existing.get(key) if isinstance(existing, dict) else None


def _build_table_doc(*, tid: str, connection_id, schema, table: str,
                     display_name: str, description: str, columns: list,
                     is_connector: bool, relations: list, intro: dict,
                     where_filter, row_cap, email: str,
                     existing: dict | None = None) -> dict:
    """The ONE stored table-doc shape — built here for both the wizard save
    and the recommendation Accept, so the two can never drift. The confirm
    stamps come from the SESSION identity + server clock, never a body.
    `existing` carries fields upsert_table's whole-doc replace would drop:
    the per-table schedule override + its fire stamp, and `registered_by`
    (ownership: stamped from the SESSION identity at FIRST save only and
    carried through every edit-save — an edit never changes or introduces
    it; a legacy doc without the field stays without it, meaning
    ladmin-registered / not deletable by any power user). Deliberately NOT
    carried: `last_drift` / `last_fingerprint` — an edit-save is the admin
    reviewing the table (drift review resolved), and the post-save refresh
    stores a fresh fingerprint anyway. `where_filter` / `row_cap` passed as
    `_NOT_POSTED` keep the existing doc's values."""
    where_filter = _posted_or_stored(where_filter, existing, "where_filter")
    row_cap = _posted_or_stored(row_cap, existing, "row_cap")
    now = datetime.now(timezone.utc).isoformat()
    carried = {}
    if isinstance(existing, dict):
        if isinstance(existing.get("schedule"), dict):
            carried["schedule"] = existing["schedule"]
            carried["schedule_last_fired_at"] = existing.get("schedule_last_fired_at")
        if existing.get("registered_by"):
            carried["registered_by"] = existing["registered_by"]
        # Storage mode + who/when/why it went live: an edit-save must never
        # silently turn a live table back into a snapshot table.
        if existing.get("mode") in db_sources.MODES:
            carried["mode"] = existing["mode"]
        for key in _LIVE_STAMP_KEYS:
            if existing.get(key) is not None:
                carried[key] = existing[key]
    else:
        carried["registered_by"] = email
    # Case-sensitivity flags come from the FRESH introspection, never the
    # posted body — the browser round-trips bare strings, and the plain name
    # is ambiguous (physical lowercase vs folded UPPERCASE). Emitted only
    # when true, so ordinary docs stay byte-identical.
    quote_by_name = {}
    for c in (intro.get("columns") or []):
        nm = c.get("name")
        if nm:
            quote_by_name[str(nm)] = bool(c.get("quote")
                                          or getattr(nm, "quote", None))
    doc_columns = []
    for c in (columns or []):
        if not c.get("name"):
            continue
        entry = {
            "name": c.get("name"),
            "dtype": c.get("dtype") or "",
            "description": (c.get("description") or "").strip(),
            "indexed": bool(c.get("indexed")),
            "pk": bool(c.get("pk")),
        }
        if quote_by_name.get(str(c.get("name"))):
            entry["quote"] = True
        doc_columns.append(entry)
    idents = {}
    if intro.get("schema_quote") or getattr(intro.get("schema"), "quote", None):
        idents["schema_quote"] = True
    if intro.get("table_quote") or getattr(intro.get("table"), "quote", None):
        idents["table_quote"] = True
    return {
        **carried,
        **idents,
        "id": tid if db_sources.DataSourceStore.valid_id(tid) else None,
        "connection_id": connection_id,
        "schema": schema or "",
        "table_name": table,
        "display_name": (display_name or "").strip(),
        "description": (description or "").strip(),
        "columns": doc_columns,
        "is_connector": bool(is_connector),
        "relations": [r for r in (relations or []) if isinstance(r, dict)],
        "row_count": intro.get("row_count_estimate"),
        "size_bytes": intro.get("size_bytes_estimate"),
        "where_filter": (where_filter or "").strip() or None,
        "row_cap": _safe_int(row_cap),
        "descriptions_confirmed_by": email,        # session identity — never the body
        "descriptions_confirmed_at": now,          # server clock
    }


def _validate_table_body(body: dict, store: db_sources.DataSourceStore):
    if body.get("confirm") is not True:
        return JSONResponse({"error": "Descriptions must be reviewed and confirmed before saving.",
                             "code": "CONFIRM_REQUIRED"}, status_code=400)
    cid = (body.get("connection_id") or "").strip()
    if store.get_connection(cid) is None:
        return JSONResponse({"error": "Unknown connection."}, status_code=400)
    if not (body.get("table_name") or "").strip():
        return JSONResponse({"error": "table_name is required."}, status_code=400)
    if not (body.get("display_name") or "").strip():
        return JSONResponse({"error": "display_name is required."}, status_code=400)
    return None


@router.post("/tables")
@router.post("/tables/{tid}")
async def save_table(request: Request, tid: str = ""):
    """Register (or edit) a table, then snapshot it. Mandatory-confirm locks:
    (1) confirm:true required; (2) drafts are never persisted elsewhere;
    (3) descriptions_confirmed_by/at stamped from the SESSION + server clock,
    never the body; (4) a fresh introspection must match the posted column
    set (409 SCHEMA_DRIFT) so a stale wizard can't confirm the wrong shape.
    The response carries the counted `size_verdict`; a snapshot is refused
    (LIVE_REQUIRED) only for a new registration or an edit switching mode.
    A body without `where_filter` / `row_cap` keeps the stored values."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    store = db_sources.DataSourceStore()
    verr = _validate_table_body(body, store)
    if verr is not None:
        return verr
    # Storage mode: absent => the existing registration's mode (resolved
    # below), "snapshot" for a new one. live_reason / live_set_by /
    # live_set_at are SERVER-derived; any posted value is ignored.
    posted_mode = body.get("mode")
    if posted_mode is not None and posted_mode not in db_sources.MODES:
        return JSONResponse({"error": "mode must be 'snapshot' or 'live'.",
                             "code": "BAD_MODE"}, status_code=400)

    # 19f publish+share: a power user may share their registration, but only
    # with roles they HOLD — an outside id is rejected up-front (403, never
    # silently dropped) BEFORE anything is registered or snapshotted. The
    # built-in Base role is EXCLUDED even when held (everyone is a member —
    # publishing to the whole platform stays ladmin's call).
    access_role_ids = body.get("access_role_ids")
    if isinstance(access_role_ids, list) and scope is not None:
        import roles_store
        held = ({r["id"] for r in roles_store.roles_for_email(email)}
                - {roles_store.BASE_ROLE_ID})
        outside = [r for r in access_role_ids
                   if isinstance(r, str) and r not in held]
        if outside:
            return JSONResponse(
                {"error": "You can only share a table with roles you hold "
                          "(sharing with everyone via Base is an "
                          "administrator action).",
                 "code": "ROLE_NOT_HELD"}, status_code=403)

    cfg, password, resp = _conn_cfg_and_password(
        store, {"connection_id": body.get("connection_id")})
    if resp is not None:
        return resp
    schema = (body.get("schema") or "").strip() or None
    table = (body.get("table_name") or "").strip()
    if not _in_scope(scope, (body.get("connection_id") or "").strip(), schema):
        return _out_of_scope()

    # A physical table (connection + schema + table) may be registered only
    # ONCE — duplicate registrations are how meaningless self-relations were
    # born. Editing the existing registration (same id) stays allowed; legacy
    # duplicates in stored data keep loading, only NEW saves are blocked.
    own_id = tid if db_sources.DataSourceStore.valid_id(tid) else None
    own = store.get_table(own_id) if own_id else None
    # An edit must also hold the EXISTING doc's physical key — a power user
    # cannot retarget an out-of-scope registration into their scope.
    if own is not None and not _in_scope(scope, own.get("connection_id"),
                                         own.get("schema")):
        return _out_of_scope()
    # Relations saved with the table follow the relation-accept rule: both
    # sides inside the power user's scope (ladmin unrestricted).
    posted_rels = [r for r in (body.get("relations") or []) if isinstance(r, dict)]
    relations, rel_err = _scoped_relations(
        scope, store, posted_rels,
        [r for r in ((own or {}).get("relations") or []) if isinstance(r, dict)])
    if rel_err is not None:
        return rel_err
    own_key = relation_discovery.physical_key(own) if own else None
    new_key = relation_discovery.physical_key(
        {"connection_id": body.get("connection_id"),
         "schema": schema or "", "table_name": table})
    # Check only when the save CREATES a physical mapping (new registration,
    # or an edit retargeting to a different source table). An edit that keeps
    # its stored physical key passes even when a LEGACY duplicate of it
    # exists — otherwise every mutation on a duplicated table would 400.
    if own_key != new_key:
        for t in store.list_tables():
            if t.get("id") != own_id and \
                    relation_discovery.physical_key(t) == new_key:
                return JSONResponse(
                    {"error": f"This table is already registered as "
                              f"'{t.get('display_name')}'. Edit that registration "
                              "instead (the Connector flag can be changed there).",
                     "code": "DUPLICATE_TABLE"}, status_code=400)

    posted_cols = [c.get("name") for c in (body.get("columns") or []) if c.get("name")]
    intro = await _run(db_connector.introspect, cfg, password, schema, table,
                       sid=f"admin:{email}")
    if not intro.get("ok"):
        return {"ok": False, "error": intro.get("error")}
    live_cols = [c.get("name") for c in (intro.get("columns") or [])]
    if sorted(posted_cols) != sorted(live_cols):
        return JSONResponse(
            {"error": "Table structure changed since introspection — re-run introspect.",
             "code": "SCHEMA_DRIFT"}, status_code=409)

    # The size thresholds: counted again after the fresh introspection (with
    # the filter and cap the saved doc will carry). A NEW registration — or
    # an edit that posts a mode different from the stored one — saved as a
    # snapshot at or above the force threshold is refused BEFORE anything is
    # written. On any other edit the verdict is advisory only: returned with
    # the response, the stored mode kept, a count timeout included.
    prev_mode = db_sources.table_mode(own) if own is not None else None
    mode = posted_mode if posted_mode is not None else (prev_mode or "snapshot")
    is_edit = own is not None
    flip = is_edit and posted_mode is not None and posted_mode != prev_mode
    where_in = body["where_filter"] if "where_filter" in body else _NOT_POSTED
    cap_in = body["row_cap"] if "row_cap" in body else _NOT_POSTED
    where_filter = _posted_or_stored(where_in, own, "where_filter")
    row_cap = _posted_or_stored(cap_in, own, "row_cap")
    count_res = await _run(db_connector.count_rows, cfg, password,
                           intro.get("schema"), intro.get("table") or table,
                           where=(where_filter.strip()
                                  if isinstance(where_filter, str) else "") or None,
                           sid=f"admin:{email}")
    verdict = _size_verdict(intro, count_res, row_cap=row_cap)
    if mode == "snapshot" and verdict.get("live_required") and \
            (not is_edit or flip):
        return JSONResponse({"error": _LIVE_REQUIRED_TEXT,
                             "code": "LIVE_REQUIRED"}, status_code=400)

    doc = _build_table_doc(
        tid=tid, connection_id=body.get("connection_id"), schema=schema,
        table=table, display_name=body.get("display_name") or "",
        description=body.get("description") or "",
        columns=body.get("columns") or [],
        is_connector=bool(body.get("is_connector")),
        relations=relations, intro=intro,
        where_filter=where_in, row_cap=cap_in,
        email=email, existing=own)
    doc["mode"] = mode
    if mode == "live":
        if prev_mode != "live":
            # Flipped (or registered) live by THIS save: the stamps come from
            # the session identity, the server clock and the counted size.
            doc["live_reason"] = ("threshold" if verdict.get("live_suggested")
                                  else "manual")
            doc["live_set_by"] = email
            doc["live_set_at"] = datetime.now(timezone.utc).isoformat()
    else:
        for key in _LIVE_DOC_KEYS:
            doc.pop(key, None)
    # Relations are stored verbatim here (frozen contract — rejecting would
    # break payloads this API must keep accepting), but a join key naming a
    # column neither side has is a silent broken hint: make it observable.
    # The structured editor removes the UI-side cause.
    try:
        own_cols = {str(c.get("name")) for c in doc["columns"]}
        for rel in doc["relations"]:
            rid = rel.get("related_table_id")
            parent = store.get_table(rid) if isinstance(rid, str) else None
            parent_cols = ({str(c.get("name")) for c in (parent.get("columns") or [])}
                           if parent else None)
            for p in (rel.get("join_keys") or []):
                if not (isinstance(p, (list, tuple)) and len(p) == 2):
                    continue
                a, b = str(p[0]), str(p[1])
                if a not in own_cols or (parent_cols is not None and b not in parent_cols):
                    log_with_sid(email, "warning",
                                 f"REL_SAVE_UNKNOWN_COLUMN table={table} "
                                 f"target={rid} pair={a}={b}")
    except Exception as e:
        log_with_sid(email, "warning", f"REL_SAVE_COLCHECK_FAILED: {type(e).__name__}")

    saved = store.upsert_table(doc, actor=email, actor_kind=_kind(scope))
    if mode != (prev_mode or "snapshot"):
        db_sources.audit(email, "table.mode", target=saved["id"],
                         detail={"from": prev_mode or "snapshot", "to": mode,
                                 "reason": doc.get("live_reason"),
                                 "cell_count": verdict.get("cell_count")},
                         actor_kind=_kind(scope))

    # Access panel (wizard step 3): canonical storage on the ROLE records,
    # never on the table doc. Absent field ⇒ no role writes (recommendation
    # Accept + pre-feature API payloads). 19f: a power user's list was
    # validated up-front as a subset of their held roles ("Share with your
    # roles" — the panel only offers held roles, and unchecked = only the
    # registerer via the ownership read); their reconcile is limited to that
    # held subset, so a role they do NOT hold keeps its ladmin-granted
    # membership — set_table_roles' exact reconcile would otherwise let a PU
    # edit-save silently strip other roles' read access. Best-effort: a role
    # write failure never fails the save (Article IV).
    if isinstance(access_role_ids, list):
        try:
            import roles_store
            wanted = [r for r in access_role_ids if isinstance(r, str)]
            if scope is not None:
                rs = roles_store.RolesStore()
                held = ({r["id"] for r in roles_store.roles_for_email(email)}
                        - {roles_store.BASE_ROLE_ID})
                wanted += [ro["id"] for ro in rs.list_roles()
                           if ro["id"] not in held
                           and saved["id"] in (ro.get("table_ids") or [])]
            roles_store.RolesStore().set_table_roles(
                saved["id"], wanted, actor=email, actor_kind=_kind(scope))
        except Exception as e:
            log_with_sid(email, "warning", f"TABLE_ACCESS_ROLES_FAILED: {e}")

    status = 201 if not db_sources.DataSourceStore.valid_id(tid) else 200
    if mode == "live":
        # No parquet for a live table: a sampled profile instead. A failure
        # keeps the registration (live_profile.ok false) — "Refresh now"
        # re-counts and re-samples.
        live_profile = await _run(_profile_live_table, saved["id"], cfg,
                                  password, store.get_table(saved["id"]) or saved,
                                  count_res, email, _kind(scope))
        return JSONResponse({"table": store.get_table(saved["id"]),
                             "snapshot": None, "live_profile": live_profile,
                             "size_verdict": verdict},
                            status_code=status)

    # Snapshot (or re-snapshot). A failure keeps the registration saved with
    # last_refresh_error set — "Refresh now" retries.
    import db_scheduler
    snap = await _run(db_scheduler.refresh_one_table, saved["id"], actor=email,
                      actor_kind=_kind(scope))
    return JSONResponse({"table": store.get_table(saved["id"]), "snapshot": snap,
                         "size_verdict": verdict},
                        status_code=status)


@router.post("/tables/{tid}/delete")
async def delete_table(request: Request, tid: str):
    """Ladmin deletes anything. A power user deletes a table ONLY when its
    physical (connection, schema) is in their scope (403 OUT_OF_SCOPE) AND
    they registered it themselves — `registered_by` equals their email; an
    absent field means ladmin-registered (403 NOT_OWNER). The two codes are
    distinct so the UI can say why."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    store = db_sources.DataSourceStore()
    if scope is not None:
        doc = store.get_table(tid)
        if doc is None:
            return JSONResponse({"error": "Unknown table."}, status_code=404)
        if not _in_scope(scope, doc.get("connection_id"), doc.get("schema")):
            return _out_of_scope()
        # Case-insensitive like the ownership read and the UI's canDelete.
        if str(doc.get("registered_by") or "").strip().lower() != email:
            return JSONResponse(
                {"error": "Only tables you registered yourself can be deleted.",
                 "code": "NOT_OWNER"}, status_code=403)
    ok = store.delete_table(
        tid, actor=email, drop_snapshot=body.get("drop_snapshot", True),
        actor_kind=_kind(scope))
    if not ok:
        return JSONResponse({"error": "Unknown table."}, status_code=404)
    # Best-effort prune from every role's table_ids (a stale id grants
    # nothing — the effective set intersects the live registry — but would
    # clutter the roles UI forever).
    try:
        import roles_store
        roles_store.RolesStore().remove_table(tid, actor=email)
    except Exception as e:
        log_with_sid(email, "warning", f"TABLE_DELETE_ROLE_PRUNE_FAILED: {e}")
    return {"ok": True}


@router.post("/tables/{tid}/refresh")
async def refresh_table(request: Request, tid: str):
    """Admin "Refresh now" — always a FULL snapshot (refresh_one_table's
    force default), never the fingerprint skip. A LIVE table takes no
    snapshot: it is re-counted and re-sampled (a fresh profile)."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    doc = db_sources.DataSourceStore().get_table(tid)
    if scope is not None:
        if doc is None or not _in_scope(scope, doc.get("connection_id"),
                                        doc.get("schema")):
            return _out_of_scope()
    if doc is not None and db_sources.table_mode(doc) == "live":
        lp = await _run(_count_and_profile_live, tid, email, _kind(scope))
        return {"ok": bool(lp.get("ok")), "live_profile": lp,
                "rows": lp.get("rows"), "error": lp.get("error")}
    import db_scheduler
    return await _run(db_scheduler.refresh_one_table, tid, actor=email,
                      actor_kind=_kind(scope))


@router.post("/tables/{tid}/dismiss_drift")
async def dismiss_drift(request: Request, tid: str):
    """Acknowledge the schema-drift banner for one table (audited)."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    store = db_sources.DataSourceStore()
    if scope is not None:
        doc = store.get_table(tid)
        if doc is None or not _in_scope(scope, doc.get("connection_id"),
                                        doc.get("schema")):
            return _out_of_scope()
    if not store.dismiss_drift(tid, actor=email, actor_kind=_kind(scope)):
        return JSONResponse({"error": "Unknown table or no drift recorded."},
                            status_code=404)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Refresh schedule + audit
# ---------------------------------------------------------------------------

def _schedule_view(stg: dict) -> dict:
    """The GET/POST response shape: settings + effective schedule object +
    human description + directly-computed next_run_at (correct in tests and
    before the scheduler thread's first tick; thread state is the fallback)."""
    import schedule_utils
    from datetime import datetime
    out = dict(stg)
    sched = schedule_utils.schedule_from_settings(stg)
    out["schedule"] = sched
    out["description"] = schedule_utils.describe_schedule(sched)
    nxt = None
    try:
        import db_scheduler
        after = db_scheduler._parse_iso(stg.get("last_fired_at")) or datetime.now()
        fire = schedule_utils.next_fire(sched, after)
        nxt = fire.isoformat() if fire else None
        if nxt is None and sched.get("enabled"):
            nxt = db_scheduler.next_run_at()
    except Exception:
        pass
    out["next_run_at"] = nxt
    return out


def _schedule_from_body(body: dict) -> dict:
    """Accept BOTH request shapes: the new {schedule: {...}} and the legacy
    {refresh_time, refresh_enabled} pair (mapped to daily) so nothing breaks
    mid-deploy."""
    if isinstance(body.get("schedule"), dict):
        return body["schedule"]
    return {"mode": "daily",
            "time": (body.get("refresh_time") or "00:00").strip() or "00:00",
            "enabled": bool(body.get("refresh_enabled"))}


@router.get("/refresh_settings")
async def get_refresh_settings(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    return _schedule_view(db_sources.DataSourceStore().get_refresh_settings())


@router.post("/refresh_settings")
async def set_refresh_settings(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    try:
        out = db_sources.DataSourceStore().set_refresh_settings(
            schedule=_schedule_from_body(body), actor=email)
    except ValueError as e:
        return JSONResponse({"error": str(e), "code": "BAD_SCHEDULE"},
                            status_code=400)
    return _schedule_view(out)


@router.post("/schedule_preview")
async def schedule_preview(request: Request):
    """Validate a schedule draft and echo it: canonical crons, human
    description, next 3 runs. No writes, no audit — serves the editors'
    live preview."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    import schedule_utils
    from datetime import datetime
    body = await _json_body(request)
    try:
        sched = schedule_utils.validate_schedule(body.get("schedule") or {})
    except ValueError as e:
        return JSONResponse({"error": str(e), "code": "BAD_SCHEDULE"},
                            status_code=400)
    now = datetime.now()
    return {"ok": True,
            "schedule": sched,
            "crons": schedule_utils.to_crons(sched),
            "description": schedule_utils.describe_schedule(sched),
            "next_runs": [d.isoformat() for d in
                          schedule_utils.preview(sched, now, 3)]}


@router.post("/tables/{tid}/schedule")
async def set_table_schedule(request: Request, tid: str):
    """Per-table schedule override; {"schedule": null} (or absent) = inherit
    the global schedule."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    import schedule_utils
    from datetime import datetime
    body = await _json_body(request)
    store = db_sources.DataSourceStore()
    if scope is not None:
        doc = store.get_table(tid)
        if doc is None or not _in_scope(scope, doc.get("connection_id"),
                                        doc.get("schema")):
            return _out_of_scope()
    sched_body = body.get("schedule")
    if sched_body is not None and not isinstance(sched_body, dict):
        return JSONResponse({"error": "Unknown schedule mode.",
                             "code": "BAD_SCHEDULE"}, status_code=400)
    try:
        found = store.set_table_schedule(tid, sched_body, actor=email,
                                         actor_kind=_kind(scope))
    except ValueError as e:
        return JSONResponse({"error": str(e), "code": "BAD_SCHEDULE"},
                            status_code=400)
    if not found:
        return JSONResponse({"error": "Unknown table."}, status_code=404)
    row = store.get_table(tid)
    out = {"ok": True, "table": row, "next_run_at": None, "description": None}
    try:
        if isinstance((row or {}).get("schedule"), dict):
            sched = schedule_utils.schedule_from_settings(
                {"schedule": row["schedule"]})
            out["description"] = schedule_utils.describe_schedule(sched)
            fire = schedule_utils.next_fire(sched, datetime.now())
            out["next_run_at"] = fire.isoformat() if fire else None
    except Exception:
        pass
    return out


@router.post("/tables/{tid}/mode")
async def set_table_mode(request: Request, tid: str):
    """Switch a registered table between "snapshot" and "live". Source
    managers only — administrators unrestricted, power users inside their
    management scope (not owner-only: mode is operational, like the schedule
    override). Same mode => 200, nothing written, no audit. To live: the live
    keys are stamped, an existing parquet is KEPT (no update deletes state)
    and a sampled profile is computed. To snapshot: the rows are counted
    again (the doc's filter and cap; a count failure falls back to the
    stored row count) and the switch is refused (LIVE_REQUIRED) at or above
    the force threshold, a count timeout included, before anything is
    written; otherwise the live keys are cleared and a fresh snapshot is
    taken (a parquet kept from before the live period would be stale). When
    that snapshot fails the table goes back to live and the answer carries
    `reverted: true`. Audited as `table.mode`."""
    email, scope, err = _require_source_manager(request)
    if err:
        return err
    body = await _json_body(request)
    store = db_sources.DataSourceStore()
    doc = store.get_table(tid)
    if scope is not None:
        if doc is None or not _in_scope(scope, doc.get("connection_id"),
                                        doc.get("schema")):
            return _out_of_scope()
    if doc is None:
        return JSONResponse({"error": "Unknown table."}, status_code=404)
    mode = body.get("mode")
    if mode not in db_sources.MODES:
        return JSONResponse({"error": "mode must be 'snapshot' or 'live'.",
                             "code": "BAD_MODE"}, status_code=400)
    if mode == db_sources.table_mode(doc):
        return {"ok": True, "table": doc}
    stored = _stored_verdict(doc)
    if mode == "snapshot":
        cfg, password, resp = _conn_cfg_and_password(
            store, {"connection_id": doc.get("connection_id")})
        if resp is not None:
            return resp
        count_res = await _run(
            db_connector.count_rows, cfg, password,
            db_connector.qname(doc.get("schema") or None, doc.get("schema_quote")),
            db_connector.qname(doc.get("table_name"), doc.get("table_quote")),
            where=doc.get("where_filter") or None, sid=f"admin:{email}")
        verdict = _size_verdict({"columns": doc.get("columns") or [],
                                 "row_count_estimate": doc.get("row_count")},
                                count_res, row_cap=doc.get("row_cap"))
        if verdict["live_required"]:
            return JSONResponse({"error": _LIVE_REQUIRED_TEXT,
                                 "code": "LIVE_REQUIRED"}, status_code=400)
        found = store.set_table_mode(tid, "snapshot", actor=email,
                                     actor_kind=_kind(scope),
                                     cell_count=verdict["cell_count"])
        if not found:
            return JSONResponse({"error": "Unknown table."}, status_code=404)
        # Always a fresh full snapshot: a parquet kept from before the live
        # period would otherwise be served to chats as current data.
        import db_scheduler
        snap = await _run(db_scheduler.refresh_one_table, tid, actor=email,
                          actor_kind=_kind(scope))
        if not (isinstance(snap, dict) and snap.get("ok")):
            # The switch holds only once a snapshot exists: back to live,
            # keeping why it was live before.
            reason = doc.get("live_reason")
            if reason not in db_sources.LIVE_REASONS:
                reason = "manual"
            restored = store.set_table_mode(tid, "live", actor=email,
                                            actor_kind=_kind(scope), reason=reason,
                                            cell_count=verdict["cell_count"])
            if not restored:
                # The table vanished while the snapshot ran: nothing to
                # revert, and nothing to report as reverted.
                return JSONResponse({"error": "Unknown table."}, status_code=404)
            # The flip cleared the profile stamps; a reverted row is not an
            # unprofiled one, so they come back from the pre-flip doc.
            if doc.get("live_profiled_at") and isinstance(
                    doc.get("live_sample_rows"), int):
                store.mark_live_profiled(
                    tid, row_count=None, columns=None,
                    profiled_at=doc["live_profiled_at"],
                    sample_rows=doc["live_sample_rows"])
            log_with_sid(log_safe_text(f"admin:{email}"), "warning",
                         f"LIVE_MODE_REVERTED table={log_safe_text(tid)}")
            return {"ok": True, "table": store.get_table(tid),
                    "snapshot": snap, "reverted": True}
        return {"ok": True, "table": store.get_table(tid), "snapshot": snap}
    reason = "threshold" if stored["live_suggested"] else "manual"
    found = store.set_table_mode(tid, "live", actor=email,
                                 actor_kind=_kind(scope), reason=reason,
                                 cell_count=stored["cell_count"])
    if not found:
        return JSONResponse({"error": "Unknown table."}, status_code=404)
    lp = await _run(_count_and_profile_live, tid, email, _kind(scope))
    return {"ok": True, "table": store.get_table(tid), "live_profile": lp}


@router.get("/audit")
async def audit_tail(request: Request, limit: int = 200):
    email, err = _require_admin(request)
    if err:
        return err
    return {"rows": db_sources.read_audit_tail(limit=min(int(limit), 1000))}
