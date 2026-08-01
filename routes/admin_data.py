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
import relation_discovery
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


@router.post("/relations/scan")
async def scan_relations(request: Request):
    """Run the FK + name/description discovery pipeline over ALL registered
    tables. FKs are fetched by LIVE introspection (they are not persisted in
    the registry); an unreachable connection degrades that source only —
    name/description candidates still come back. Nothing is written."""
    email, err = _require_admin(request)
    if err:
        return err
    store = db_sources.DataSourceStore()
    tables = store.list_tables()
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

    cands = await _run(relation_discovery.discover, tables, fk_map)
    cands = await _run(_verify_and_band, cands)
    db_sources.audit(email, "relations.scan",
                     detail={"tables": len(tables), "candidates": len(cands),
                             "degraded": len(degraded)})
    return {"ok": True, "candidates": cands, "degraded": degraded}


@router.post("/relations/analyze_sql")
async def analyze_sql(request: Request):
    """Extract join candidates from admin-pasted SELECT statements. The SQL is
    parsed IN MEMORY on this client and never persisted, logged, audited, or
    sent to the brain (Article II) — the audit row carries counts only, and
    sqlglot's error text (it embeds the SQL) never leaves the parser."""
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    sql_text = body.get("sql") or ""
    if not sql_text.strip():
        return JSONResponse({"error": "sql is required."}, status_code=400)
    dialect = relation_discovery.SQLGLOT_DIALECT.get(
        (body.get("db_type") or "").strip().lower())
    store = db_sources.DataSourceStore()
    tables = store.list_tables()
    cands, stats = await _run(relation_discovery.extract_sql_joins,
                              sql_text, tables, dialect)
    cands = relation_discovery.filter_existing(cands, tables)
    cands = await _run(_verify_and_band, cands)
    db_sources.audit(email, "relations.analyze_sql",
                     detail={"statements": stats.get("statements"),
                             "failed": stats.get("failed"),
                             "candidates": len(cands)})
    return {"ok": True, "candidates": cands, "stats": stats}


_REL_CARDINALITIES = {"N:1", "1:1", "1:N", "N:M"}
_REL_ORIGINS = {"fk", "sql", "name", "description"}


@router.post("/relations/accept")
async def accept_relations(request: Request):
    """Write accepted relation candidates into the registry. Body
    {relations: [{table_id, related_table_id, join_keys, cardinality?, origin?}]}
    (bulk accept = the same endpoint). Deliberately NO confirm gate, NO
    SCHEMA_DRIFT check, and NO re-snapshot: those locks protect the
    column/description shape, which this endpoint cannot touch — it only
    appends to `relations` on the child table's doc."""
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    items = body.get("relations")
    if not isinstance(items, list) or not items:
        return JSONResponse({"error": "relations list is required."}, status_code=400)

    store = db_sources.DataSourceStore()
    by_child: dict = {}
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return JSONResponse({"error": f"relations[{idx}] must be an object."},
                                status_code=400)
        child = store.get_table((item.get("table_id") or "").strip())
        parent = store.get_table((item.get("related_table_id") or "").strip())
        if child is None or parent is None:
            return JSONResponse({"error": f"relations[{idx}]: unknown table id."},
                                status_code=400)
        jk = item.get("join_keys")
        ok_shape = (isinstance(jk, list) and jk and all(
            isinstance(p, (list, tuple)) and len(p) == 2
            and str(p[0] or "").strip() and str(p[1] or "").strip() for p in jk))
        if not ok_shape:
            return JSONResponse(
                {"error": f"relations[{idx}]: join_keys must be a non-empty "
                          "list of [child_col, parent_col] pairs."}, status_code=400)
        jk = [[str(p[0]).strip(), str(p[1]).strip()] for p in jk]
        jk = list(dict.fromkeys(map(tuple, jk)))       # drop repeated pairs
        jk = [list(p) for p in jk]
        child_cols = {c.get("name") for c in (child.get("columns") or [])}
        parent_cols = {c.get("name") for c in (parent.get("columns") or [])}
        for a, b in jk:
            if a not in child_cols or b not in parent_cols:
                return JSONResponse(
                    {"error": f"relations[{idx}]: column '{a}={b}' not found "
                              "on the registered tables."}, status_code=400)
        cardinality = item.get("cardinality")
        if cardinality is not None and cardinality not in _REL_CARDINALITIES:
            return JSONResponse({"error": f"relations[{idx}]: invalid cardinality."},
                                status_code=400)
        origin = item.get("origin")
        if origin is not None and origin not in _REL_ORIGINS:
            return JSONResponse({"error": f"relations[{idx}]: invalid origin."},
                                status_code=400)
        by_child.setdefault(child["id"], []).append(
            {"parent": parent, "join_keys": jk,
             "cardinality": cardinality, "origin": origin})

    accepted = skipped = 0
    # ONE read-modify-write per child table: upsert_table is a full replace,
    # so per-item writes to the same doc would lose all but the last.
    for tid, batch in by_child.items():
        doc = store.get_table(tid)
        if doc is None:
            log_with_sid(email, "warning", f"REL_ACCEPT_TABLE_VANISHED table={tid}")
            continue
        existing = doc.get("relations") or []
        existing_ids = set()
        for rel in existing:
            if not isinstance(rel, dict):
                continue
            rid = rel.get("related_table_id") or rel.get("related_table")
            try:
                pairs = [(str(p[0]), str(p[1])) for p in (rel.get("join_keys") or [])]
            except Exception as e:
                log_with_sid(email, "warning",
                             f"REL_ACCEPT_MALFORMED_EXISTING table={tid}: {type(e).__name__}")
                continue
            if rid and pairs:
                existing_ids.add(relation_discovery.candidate_id(
                    tid, [p[0] for p in pairs], str(rid), [p[1] for p in pairs]))
        changed = False
        for item in batch:
            cand_key = relation_discovery.candidate_id(
                tid, [p[0] for p in item["join_keys"]],
                item["parent"]["id"], [p[1] for p in item["join_keys"]])
            if cand_key in existing_ids:
                skipped += 1
                continue
            rel = {"related_table_id": item["parent"]["id"],
                   "join_keys": item["join_keys"]}
            if item["origin"]:
                rel["origin"] = item["origin"]
            if item["cardinality"]:
                rel["cardinality"] = item["cardinality"]
            existing.append(rel)
            existing_ids.add(cand_key)
            accepted += 1
            changed = True
        if changed:
            doc["relations"] = existing
            await _run(store.upsert_table, doc, actor=email)

    db_sources.audit(email, "relations.accept",
                     detail={"accepted": accepted, "skipped": skipped,
                             "tables": sorted(by_child.keys())})
    return {"ok": True, "accepted": accepted, "skipped": skipped}


@router.post("/relations/dismiss")
async def dismiss_relation(request: Request):
    """Audit trail for a dismissed candidate. Dismissals are session-local by
    design (no persistence) — this endpoint exists so the action is on the
    record like every other ladmin decision."""
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    tid = (body.get("table_id") or "").strip()
    rid = (body.get("related_table_id") or "").strip()
    if not (db_sources.DataSourceStore.valid_id(tid)
            and db_sources.DataSourceStore.valid_id(rid)):
        return JSONResponse({"error": "Unknown table id."}, status_code=400)
    jk = body.get("join_keys")
    jk = [[str(p[0])[:128], str(p[1])[:128]] for p in jk
          if isinstance(p, (list, tuple)) and len(p) == 2] if isinstance(jk, list) else []
    band = body.get("band")
    db_sources.audit(email, "relations.dismiss", target=f"{tid}->{rid}",
                     detail={"join_keys": jk,
                             "band": band if band in ("confirmed", "suggested",
                                                      "attention") else None})
    return {"ok": True}


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
