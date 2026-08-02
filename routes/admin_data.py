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
import time
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
                                 email: str) -> dict:
    """Turn extraction's unregistered-join evidence into persistent
    "Recommended tables". Only names resolvable to ONE connection (the same
    resolve_unknown_tables rule the register shortcut uses) become
    recommendations — the rest stay ephemeral unknown_tables strings.
    Identifiers and counts only; the SQL text never reaches this function.
    Never raises — a store failure must not discard the already-computed
    candidates of the analyze response (Article IV)."""
    try:
        return _persist_sql_recommendations_inner(store, tables, stats, email)
    except Exception as e:
        log_with_sid(email, "error",
                     f"REL_RECOMMEND_PERSIST_FAILED: {type(e).__name__}")
        return {"created": 0, "updated": 0}


def _persist_sql_recommendations_inner(store, tables: list, stats: dict,
                                       email: str) -> dict:
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
    if not batch:
        return {"created": 0, "updated": 0}
    return store.upsert_recommendations(list(batch.values()), actor=email)


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
    if fk_batch:
        await _run(store.upsert_recommendations, fk_batch, email)

    cands = await _run(relation_discovery.discover, tables, fk_map)
    # Close the loop for registrations made OUTSIDE instant-Accept (wizard,
    # ghost shortcut): stored SQL evidence of now-registered recommendations
    # replays through the same pipeline — no re-pasting. v4.1: evidence pairs
    # naming columns the registration lacks are excluded by the replay
    # validator and surfaced as warnings, never as bogus candidates.
    rec_cands: list = []
    registered_recs = [r for r in await _run(store.list_recommendations)
                       if r.get("status") == "registered"]
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
                             "unregistered_refs": len(unregistered)})
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
    cands = relation_discovery.filter_same_physical(cands, tables)
    cands = relation_discovery.filter_existing_physical(cands, tables)
    cands = relation_discovery.dedupe_physical_targets(cands, tables)
    cands = await _run(_verify_and_band, cands)
    hints = relation_discovery.resolve_unknown_tables(
        stats.get("unknown_tables") or [], tables, store.list_connections())
    recs = await _run(_persist_sql_recommendations, store, tables, stats, email)
    db_sources.audit(email, "relations.analyze_sql",
                     detail={"statements": stats.get("statements"),
                             "failed": stats.get("failed"),
                             "candidates": len(cands)})
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
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    table_name = (body.get("table_name") or "").strip()
    if not table_name:
        return JSONResponse({"error": "table_name is required."}, status_code=400)
    registered = db_sources.DataSourceStore().list_tables()
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
                             "registered": len(registered)})
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
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)

    def _reject(msg: str):
        # A rejected write is an admin action too — leave an ok:false trace
        # (parity with admin.denied; previously a failed accept vanished).
        db_sources.audit(email, "relations.accept", ok=False,
                         detail={"error": msg[:200]})
        return JSONResponse({"error": msg}, status_code=400)

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
            await _run(store.upsert_table, doc, actor=email)

    db_sources.audit(email, "relations.accept",
                     detail={"accepted": accepted, "skipped": skipped,
                             "replaced": replaced,
                             "tables": sorted(by_child.keys())})
    return {"ok": True, "accepted": accepted, "skipped": skipped,
            "replaced": replaced}


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


@router.post("/relations/graph")
async def relations_graph(request: Request):
    """Graph-view data for the Relations section: registered tables as nodes,
    confirmed relations as edges, connected components/isolated flags, and
    dashed ghost nodes for OPEN recommended tables (server-side, persistent)
    unioned with any body-passed last-scan refs (kept for compatibility —
    the graph never introspects). Dismissed recommendations never render.
    Read-only."""
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    store = db_sources.DataSourceStore()
    tables = store.list_tables()
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
    email, err = _require_admin(request)
    if err:
        return err
    store = db_sources.DataSourceStore()
    tables = store.list_tables()
    out = []
    for rec in await _run(store.list_recommendations):
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
    email, err = _require_admin(request)
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
    rec = await _run(db_sources.DataSourceStore().set_recommendation_status,
                     rid, status, email)
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
    email, err = _require_admin(request)
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
    email, err = _require_admin(request)
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
                                 "chosen_type": chosen_type})
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
        saved = store.upsert_table(doc, actor=email)
        import db_scheduler
        _phase("snapshot")
        snap = db_scheduler.refresh_one_table(saved["id"], actor=email)
        if not snap.get("ok"):
            # Accept is one atomic gesture: unlike the wizard save (which
            # keeps the registration for a manual "Refresh now"), roll the
            # registration back so no half-registered state remains — the
            # store's reconcile hook restores the rec to open. A nightly-
            # scheduler race can at worst leave an orphan parquet, the same
            # window a plain table delete has today.
            store.delete_table(saved["id"], actor=email)
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
                             "chosen_type": chosen_type})
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}

    # Replay the stored SQL evidence through the NORMAL candidate pipeline
    # (same chain as analyze_sql). FK evidence is not replayed — the next
    # scan's live introspection re-derives it as ground truth. v4.1: invalid
    # evidence pairs are excluded by the replay validator and surfaced.
    tables = store.list_tables()
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
    email, err = _require_admin(request)
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
    kept = [r for r in (doc.get("relations") or [])
            if not (isinstance(r, dict) and _rel_matches(r, ref, jk))]
    removed = len(doc.get("relations") or []) - len(kept)
    if not removed:
        return JSONResponse({"error": "Relation not found on this table."},
                            status_code=404)
    doc["relations"] = kept
    await _run(store.upsert_table, doc, actor=email)
    db_sources.audit(email, "relations.delete", target=f"{tid}->{ref}",
                     detail={"join_keys": jk, "removed": removed})
    return {"ok": True, "removed": removed}


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
            "degraded": intro.get("degraded") or [],
            # Pre-ticks the wizard's connector box from the columns already in
            # hand (no extra round-trip). A suggestion the admin edits freely.
            "classification": relation_discovery.classify_table_type(
                intro.get("columns") or [])}


def _draft_table_descriptions(cfg: dict, password: str, schema, table: str,
                              email: str, intro: Optional[dict] = None) -> dict:
    """The ONE AI-draft mechanism (the existing schema-autofill brain call) —
    used by the draft_descriptions route and the recommendation Accept, so
    the two can never fork. Sync (run via _run). Pass a fresh `intro` to skip
    the internal introspection (Accept already has one — no second live
    catalog round-trip against the customer DB). Returns {"ok": True,
    "draft": {...}, "confirmed": False} or {"ok": False, "error": ...}."""
    import pandas as pd
    from routes.upload import _prepare_file_context
    if intro is None:
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
    res = await _run(_draft_table_descriptions, cfg, password, schema, table, email)
    db_sources.audit(email, "table.draft_descriptions",
                     target=f"{schema}.{table}", ok=bool(res.get("ok")))
    return res


def _build_table_doc(*, tid: str, connection_id, schema, table: str,
                     display_name: str, description: str, columns: list,
                     is_connector: bool, relations: list, intro: dict,
                     where_filter, row_cap, email: str) -> dict:
    """The ONE stored table-doc shape — built here for both the wizard save
    and the recommendation Accept, so the two can never drift. The confirm
    stamps come from the SESSION identity + server clock, never a body."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": tid if db_sources.DataSourceStore.valid_id(tid) else None,
        "connection_id": connection_id,
        "schema": schema or "",
        "table_name": table,
        "display_name": (display_name or "").strip(),
        "description": (description or "").strip(),
        "columns": [{
            "name": c.get("name"),
            "dtype": c.get("dtype") or "",
            "description": (c.get("description") or "").strip(),
            "indexed": bool(c.get("indexed")),
            "pk": bool(c.get("pk")),
        } for c in (columns or []) if c.get("name")],
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

    # A physical table (connection + schema + table) may be registered only
    # ONCE — duplicate registrations are how meaningless self-relations were
    # born. Editing the existing registration (same id) stays allowed; legacy
    # duplicates in stored data keep loading, only NEW saves are blocked.
    own_id = tid if db_sources.DataSourceStore.valid_id(tid) else None
    own = store.get_table(own_id) if own_id else None
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

    doc = _build_table_doc(
        tid=tid, connection_id=body.get("connection_id"), schema=schema,
        table=table, display_name=body.get("display_name") or "",
        description=body.get("description") or "",
        columns=body.get("columns") or [],
        is_connector=bool(body.get("is_connector")),
        relations=body.get("relations") or [], intro=intro,
        where_filter=body.get("where_filter"), row_cap=body.get("row_cap"),
        email=email)
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
