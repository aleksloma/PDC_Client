"""Admin "Data sources" API — ladmin-only management of database connections
and registered tables.

Security model:
  - Every route is behind `_require_admin` (role == "admin" on the local user
    record; 403 while a forced password change is pending). Denials are
    audited (`admin.denied`).
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import brain_client
import db_connector
import db_sources
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
    email, err = _require_admin(request)
    if err:
        return err
    return {"dialects": db_connector.list_dialects()}


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

@router.get("/connections")
async def list_connections(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    store = db_sources.DataSourceStore()
    conns = store.list_connections()
    tables = store.list_tables()
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
    return res


@router.get("/connections/{cid}/schemas")
async def connection_schemas(request: Request, cid: str):
    email, err = _require_admin(request)
    if err:
        return err
    store = db_sources.DataSourceStore()
    cfg, password, resp = _conn_cfg_and_password(store, {"connection_id": cid})
    if resp is not None:
        return resp
    return await _run(db_connector.list_schemas, cfg, password, sid=f"admin:{email}")


@router.get("/connections/{cid}/tables")
async def connection_tables(request: Request, cid: str, schema: str = ""):
    email, err = _require_admin(request)
    if err:
        return err
    store = db_sources.DataSourceStore()
    cfg, password, resp = _conn_cfg_and_password(store, {"connection_id": cid})
    if resp is not None:
        return resp
    res = await _run(db_connector.list_tables, cfg, password, schema or None,
                     sid=f"admin:{email}")
    if res.get("ok"):
        registered = {(t.get("connection_id"), t.get("schema") or "", t.get("table_name")): t.get("id")
                      for t in store.list_tables()}
        for row in res["tables"]:
            tid = registered.get((cid, schema or "", row["name"]))
            row["registered"] = tid is not None
            if tid:
                row["table_id"] = tid
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
        res = await _run(db_scheduler.refresh_one_table, t.get("id"), actor=email)
        results.append({"table_id": t.get("id"),
                        "display_name": t.get("display_name"),
                        "ok": bool(res.get("ok")),
                        "rows": res.get("rows"), "error": res.get("error")})
    return {"ok": True, "results": results}


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

@router.get("/tables")
async def list_tables(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    return {"tables": db_sources.DataSourceStore().list_tables()}


@router.post("/tables/introspect")
async def introspect_table(request: Request):
    """Inspector introspection + first-rows preview for the registration
    wizard. Preview values go only to the admin's browser — never the brain."""
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
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
    preview = await _run(db_connector.preview_rows, cfg, password, schema, table,
                         limit=int(body.get("preview_rows") or settings.DB_PREVIEW_ROWS),
                         sid=f"admin:{email}")
    db_sources.audit(email, "table.introspect", target=f"{schema}.{table}",
                     detail={"columns": len(intro.get("columns") or []),
                             "degraded": intro.get("degraded")})
    return {"ok": True, "introspection": intro, "preview": preview,
            "degraded": intro.get("degraded") or []}


@router.post("/tables/draft_descriptions")
async def draft_descriptions(request: Request):
    """AI-drafted table + column descriptions in ENGLISH via the EXISTING
    schema-autofill brain call. PERSISTS NOTHING — the ladmin must review,
    edit, and confirm in the save step; this endpoint has no write path at
    all (one of the four mandatory-confirm locks)."""
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    store = db_sources.DataSourceStore()
    cfg, password, resp = _conn_cfg_and_password(store, body)
    if resp is not None:
        return resp
    schema = (body.get("schema") or "").strip() or None
    table = (body.get("table") or "").strip()
    if not table:
        return JSONResponse({"error": "table is required."}, status_code=400)

    def _build_and_call():
        import pandas as pd
        from routes.upload import _prepare_file_context
        intro = db_connector.introspect(cfg, password, schema, table,
                                        sid=f"admin:{email}")
        if not intro.get("ok"):
            return {"ok": False, "error": intro.get("error")}
        # A larger sample than the visual preview so unique_hints are honest
        # (still truncated by the same SCHEMA_AUTOFILL_* rules files use).
        prev = db_connector.preview_rows(cfg, password, schema, table,
                                         limit=200, sid=f"admin:{email}")
        if not prev.get("ok"):
            return {"ok": False, "error": prev.get("error")}
        df = pd.DataFrame(prev.get("rows") or [], columns=prev.get("columns") or [])
        entry = {"file_name": f"{schema}.{table}" if schema else table,
                 "file_description": intro.get("table_comment") or "",
                 "schema": {"file_name": table, "fields": {}}}
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
            )
        except Exception as e:
            log_with_sid(email, "warning", f"DB_DRAFT_LLM_FAIL table={table}: {e}")
            return {"ok": False, "error": "AI drafting is unavailable right now."}
        return {"ok": True,
                "draft": {"table_description": (rsp.get("file_description") or "").strip(),
                          "columns": rsp.get("columns") or {}},
                "confirmed": False}

    res = await _run(_build_and_call)
    db_sources.audit(email, "table.draft_descriptions",
                     target=f"{schema}.{table}", ok=bool(res.get("ok")))
    return res


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
    set (409 SCHEMA_DRIFT) so a stale wizard can't confirm the wrong shape."""
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    store = db_sources.DataSourceStore()
    verr = _validate_table_body(body, store)
    if verr is not None:
        return verr

    cfg, password, resp = _conn_cfg_and_password(
        store, {"connection_id": body.get("connection_id")})
    if resp is not None:
        return resp
    schema = (body.get("schema") or "").strip() or None
    table = (body.get("table_name") or "").strip()

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

    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": tid if db_sources.DataSourceStore.valid_id(tid) else None,
        "connection_id": body.get("connection_id"),
        "schema": schema or "",
        "table_name": table,
        "display_name": (body.get("display_name") or "").strip(),
        "description": (body.get("description") or "").strip(),
        "columns": [{
            "name": c.get("name"),
            "dtype": c.get("dtype") or "",
            "description": (c.get("description") or "").strip(),
            "indexed": bool(c.get("indexed")),
            "pk": bool(c.get("pk")),
        } for c in (body.get("columns") or []) if c.get("name")],
        "is_connector": bool(body.get("is_connector")),
        "relations": [r for r in (body.get("relations") or []) if isinstance(r, dict)],
        "row_count": intro.get("row_count_estimate"),
        "size_bytes": intro.get("size_bytes_estimate"),
        "where_filter": (body.get("where_filter") or "").strip() or None,
        "row_cap": _safe_int(body.get("row_cap")),
        "descriptions_confirmed_by": email,        # session identity — never the body
        "descriptions_confirmed_at": now,          # server clock
    }
    saved = store.upsert_table(doc, actor=email)

    # Snapshot (or re-snapshot). A failure keeps the registration saved with
    # last_refresh_error set — "Refresh now" retries.
    import db_scheduler
    snap = await _run(db_scheduler.refresh_one_table, saved["id"], actor=email)
    status = 201 if not db_sources.DataSourceStore.valid_id(tid) else 200
    return JSONResponse({"table": store.get_table(saved["id"]), "snapshot": snap},
                        status_code=status)


@router.post("/tables/{tid}/delete")
async def delete_table(request: Request, tid: str):
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    ok = db_sources.DataSourceStore().delete_table(
        tid, actor=email, drop_snapshot=body.get("drop_snapshot", True))
    if not ok:
        return JSONResponse({"error": "Unknown table."}, status_code=404)
    return {"ok": True}


@router.post("/tables/{tid}/refresh")
async def refresh_table(request: Request, tid: str):
    email, err = _require_admin(request)
    if err:
        return err
    import db_scheduler
    return await _run(db_scheduler.refresh_one_table, tid, actor=email)


# ---------------------------------------------------------------------------
# Refresh schedule + audit
# ---------------------------------------------------------------------------

@router.get("/refresh_settings")
async def get_refresh_settings(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    import db_scheduler
    stg = db_sources.DataSourceStore().get_refresh_settings()
    stg["next_run_at"] = db_scheduler.next_run_at()
    return stg


@router.post("/refresh_settings")
async def set_refresh_settings(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    try:
        out = db_sources.DataSourceStore().set_refresh_settings(
            refresh_time=(body.get("refresh_time") or "").strip(),
            refresh_enabled=bool(body.get("refresh_enabled")),
            actor=email)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return out


@router.get("/audit")
async def audit_tail(request: Request, limit: int = 200):
    email, err = _require_admin(request)
    if err:
        return err
    return {"rows": db_sources.read_audit_tail(limit=min(int(limit), 1000))}
