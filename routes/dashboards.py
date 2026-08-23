"""Dashboards — per-user curated pages of pinned charts/tables.

A dashboard is a named grid of TILES. Each tile is a snapshot of one chart or
table pinned from a conversation response, plus the stored code that produced
it. Opening a dashboard renders the stored snapshots instantly (zero
execution); per-tile refresh re-runs the tile's code against the source chat's
CURRENT dataframes via `routes.chat.run_item_refresh` — purely local, no brain
call (Article II) — and persists the new snapshot.

Storage: `local_store.DashboardStore` under DATA_ROOT/users/{email}/dashboards/
(Article V). Sharing mirrors chat sharing: the owner's doc carries
`sharing.shared_with`; each recipient's index gets a `shared_by` pointer row.
Recipients get view + refresh; every structural mutation is owner-only (403).

Error handling per Article IV: auth/validation failures use HTTP status codes;
EXECUTION failures return 200 {ok: false, error} so the UI keeps the previous
render.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import local_store
import brain_client
from brain_client import TenantRevokedError, BrainError
from logger_utils import log_with_sid
from routes.chat import (_FULL_KEY_RE, _json_safe, _load_full_table_record,
                         _persist_full_table, _persistable_chart_data,
                         _reexecute_full_df, _require_chat, _role_refresh_block,
                         run_item_refresh)

router = APIRouter(prefix="/api/dashboards", tags=["client-dashboards"])

_dash_store = local_store.DashboardStore()

# A chart snapshot is the same content history already persists inline
# (base64 PNG or Plotly HTML); this cap only rejects pathological payloads.
_MAX_SNAPSHOT_CHARS = 5_000_000
# Tables store only a preview on the tile; the full table goes through the
# source chat's /full_table/{key} endpoint.
_MAX_TILE_TABLE_ROWS = 50
# styled_html (pandas-Styler HTML — carries the conditional formatting) is
# kept on the tile so colors survive; only a pathological one is dropped
# (the plain rows remain as fallback).
_MAX_STYLED_HTML_CHARS = 2_000_000
_MAX_DESCRIPTION_CHARS = 20_000
_MAX_NAME_CHARS = 100


def _require_email(request: Request):
    email = request.session.get("email")
    if not email:
        return None, JSONResponse({"error": "Not authenticated"}, status_code=401)
    return email.strip().lower(), None


def _require_owned(email: str, dash_id: str):
    """Owner-only guard: (doc, None) when the caller owns the dashboard,
    else (None, 403) when it is merely shared with them, (None, 404) otherwise."""
    doc = _dash_store.get_dashboard(email, dash_id)
    if doc is not None:
        return doc, None
    shared_doc, _ = _dash_store.resolve_dashboard(email, dash_id)
    if shared_doc is not None:
        return None, JSONResponse({"error": "Only the dashboard owner can do this."},
                                  status_code=403)
    return None, JSONResponse({"error": "Dashboard not found"}, status_code=404)


def _resolve_table_record_code(chat_id: str, full_table_key):
    """The durable full-table record (`conversations/full/{key}.json`) carries
    the AUTHORITATIVE code that produced the table — the same code Download
    Excel re-executes. The client-sent code can be the CHART's code when the
    response mixed a chart with a table, so the record wins whenever it has a
    clean code. Returns (code, result_key) or (None, None)."""
    try:
        if not (isinstance(full_table_key, str) and _FULL_KEY_RE.fullmatch(full_table_key)):
            return None, None
        rec = _load_full_table_record(local_store.ChatDataStore(chat_id), full_table_key)
        if not isinstance(rec, dict):
            return None, None
        code = (rec.get("code") or "").strip()
        if not code or "###NEXT_PLOT###" in code:
            return None, None
        return code, rec.get("result_key")
    except Exception as e:
        log_with_sid(chat_id, "warning", f"DASH_TABLE_RECORD_RESOLVE_FAILED: {e}")
        return None, None


def _tile_title(description: str, kind: str) -> str:
    first_line = (description or "").strip().splitlines()[0] if (description or "").strip() else ""
    first_line = first_line.strip().lstrip("#*").strip()
    return first_line[:80] if first_line else ("Chart" if kind == "chart" else "Table")


def _capped_table(table) -> dict | None:
    if not (isinstance(table, dict) and table.get("columns") is not None
            and isinstance(table.get("rows"), list)):
        return None
    out = {
        "columns": list(table.get("columns") or []),
        "rows": list(table.get("rows") or [])[:_MAX_TILE_TABLE_ROWS],
        "total_rows": table.get("total_rows"),
    }
    styled = table.get("styled_html")
    if isinstance(styled, str) and styled and len(styled) <= _MAX_STYLED_HTML_CHARS:
        out["styled_html"] = styled
    if table.get("dtype"):
        out["dtype"] = table["dtype"]
    if table.get("title"):
        out["title"] = table["title"]
    return out


# ---------------------------------------------------------------------------
# Dashboard CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def list_dashboards(request: Request):
    email, err = _require_email(request)
    if err:
        return err
    try:
        rows = _dash_store.list_dashboards(email)
    except Exception as e:
        log_with_sid(email, "error", f"DASH_LIST_FAILED: {e}")
        rows = []
    return {"dashboards": _json_safe(rows)}


@router.post("")
async def create_dashboard(request: Request):
    email, err = _require_email(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str((body or {}).get("name") or "").strip()
    if not name or len(name) > _MAX_NAME_CHARS:
        return JSONResponse({"error": "Dashboard name must be 1-100 characters."},
                            status_code=400)
    try:
        row = _dash_store.create_dashboard(email, name)
    except Exception as e:
        log_with_sid(email, "error", f"DASH_CREATE_FAILED: {e}")
        return JSONResponse({"error": "Could not create dashboard."}, status_code=500)
    return _json_safe(row)


def _with_default_layouts(doc: dict) -> dict:
    """Fill layouts for legacy tiles that predate the layout field — in the
    RESPONSE only, never persisted (non-destructive; the user's first drag
    persists real positions via /layout). Layoutless tiles are placed below
    every positioned tile, half-width, two per row (QA 3.1)."""
    try:
        tiles = doc.get("tiles") or []
        if all(isinstance(t.get("layout"), dict) for t in tiles):
            return doc
        bottom = 0
        for t in tiles:
            lay = t.get("layout")
            if isinstance(lay, dict):
                try:
                    bottom = max(bottom, int(lay.get("y", 0)) + int(lay.get("h", 5)))
                except Exception:
                    continue
        out_tiles = []
        n = 0
        for t in tiles:
            if isinstance(t.get("layout"), dict):
                out_tiles.append(t)
                continue
            t = dict(t)
            t["layout"] = {"x": (n % 2) * 6, "y": bottom + (n // 2) * 5, "w": 6, "h": 5}
            n += 1
            out_tiles.append(t)
        return {**doc, "tiles": out_tiles}
    except Exception as e:
        log_with_sid(doc.get("dash_id") or "?", "warning", f"DASH_DEFAULT_LAYOUT_FAILED: {e}")
        return doc


@router.get("/{dash_id}")
async def get_dashboard(request: Request, dash_id: str):
    email, err = _require_email(request)
    if err:
        return err
    doc, is_owner = _dash_store.resolve_dashboard(email, dash_id)
    if doc is None:
        return JSONResponse({"error": "Dashboard not found"}, status_code=404)
    _dash_store.touch_last_used(email, dash_id)
    return _json_safe({**_with_default_layouts(doc), "is_owner": is_owner})


@router.post("/{dash_id}/rename")
async def rename_dashboard(request: Request, dash_id: str):
    email, err = _require_email(request)
    if err:
        return err
    doc, err = _require_owned(email, dash_id)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str((body or {}).get("name") or "").strip()
    if not name or len(name) > _MAX_NAME_CHARS:
        return JSONResponse({"error": "Dashboard name must be 1-100 characters."},
                            status_code=400)
    ok = _dash_store.rename_dashboard(email, dash_id, name)
    return {"ok": bool(ok)}


@router.post("/{dash_id}/delete")
async def delete_dashboard(request: Request, dash_id: str):
    """Owner → delete the dashboard. Shared recipient → remove it from their
    own list only (the owner's dashboard is untouched). Idempotent."""
    email, err = _require_email(request)
    if err:
        return err
    if _dash_store.get_dashboard(email, dash_id) is not None:
        _dash_store.delete_dashboard(email, dash_id)
        return {"ok": True, "deleted": True}
    _dash_store.remove_from_index(email, dash_id)
    return {"ok": True, "deleted": False}


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------

# Text tiles (QA 3.1): headers/comments between visuals. Fixed enums so the
# renderer's CSS classes are the whole styling surface.
_TEXT_STYLES = ("header1", "header2", "paragraph")
_TEXT_COLORS = ("default", "gray", "red", "orange", "green", "blue", "purple")
_TEXT_SIZES = ("S", "M", "L")
# Word-style alignment. Defaults reproduce the pre-alignment rendering, and
# tiles stored without the keys render the same way (the frontend falls back to
# left/top) — no migration, no shifted dashboards.
_TEXT_ALIGNS = ("left", "center", "right")
_TEXT_VALIGNS = ("top", "middle", "bottom")
_MAX_TEXT_CHARS = 2000


def _validate_text_fields(body: dict, *, require_text: bool):
    """Validate {text, style, color, size, align, valign} for a text tile. Returns
    (fields_dict, error_response). Absent enum fields get defaults when
    require_text (create); on update only supplied fields are validated."""
    out: dict = {}
    text = body.get("text")
    if text is not None or require_text:
        if not (isinstance(text, str) and text.strip()):
            return None, JSONResponse({"error": "A text tile needs non-empty text."},
                                      status_code=400)
        if len(text) > _MAX_TEXT_CHARS:
            return None, JSONResponse({"error": f"Text is limited to {_MAX_TEXT_CHARS} characters."},
                                      status_code=400)
        out["text"] = text
    for field, allowed, default in (("style", _TEXT_STYLES, "paragraph"),
                                    ("color", _TEXT_COLORS, "default"),
                                    ("size", _TEXT_SIZES, "M"),
                                    ("align", _TEXT_ALIGNS, "left"),
                                    ("valign", _TEXT_VALIGNS, "top")):
        val = body.get(field)
        if val is None:
            if require_text:
                out[field] = default
            continue
        if val not in allowed:
            return None, JSONResponse(
                {"error": f"{field} must be one of {', '.join(allowed)}."}, status_code=400)
        out[field] = val
    return out, None


@router.post("/{dash_id}/tiles")
async def add_tile(request: Request, dash_id: str):
    """Pin one chart/table onto a dashboard, or add a text block. Body (from
    the /lab pin button, or the dashboard page's Add header / Add text):
    {chat_id, kind: "chart"|"table", description?, code?,
     image_base64?, is_plotly?,            # charts
     table?, full_table_key?,              # tables
     chart_data? | chart_data_key?}        # charts' "Show data" source
    {kind: "text", text, style?, color?, size?, align?, valign?}  # text blocks (no chat)
    """
    email, err = _require_email(request)
    if err:
        return err
    doc, err = _require_owned(email, dash_id)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}

    kind = str(body.get("kind") or "").strip().lower()
    if kind not in ("chart", "table", "text"):
        return JSONResponse({"error": "kind must be 'chart', 'table', or 'text'."},
                            status_code=400)

    if kind == "text":
        fields, terr = _validate_text_fields(body, require_text=True)
        if terr:
            return terr
        tile = {"kind": "text", **fields}
        saved = _dash_store.add_tile(email, dash_id, tile)
        if saved is None:
            return JSONResponse({"error": "Dashboard not found"}, status_code=404)
        return _json_safe({"ok": True, "tile": saved})

    chat_id = str(body.get("chat_id") or "").strip()
    _, chat_err = _require_chat(request, chat_id)
    if chat_err:
        return chat_err

    description = str(body.get("description") or "")[:_MAX_DESCRIPTION_CHARS]
    code = str(body.get("code") or "").strip()
    if "###NEXT_PLOT###" in code:
        # Legacy joined multi-chart record — not a single executable block; the
        # tile still renders but cannot be refreshed.
        code = ""

    snapshot: dict = {"rendered_at": local_store._now()}
    chart_data = None
    full_table_key = None
    result_key = None

    if kind == "chart":
        image = body.get("image_base64")
        if not (isinstance(image, str) and image.strip()):
            return JSONResponse({"error": "A chart tile needs image_base64."}, status_code=400)
        if len(image) > _MAX_SNAPSHOT_CHARS:
            log_with_sid(email, "warning", f"DASH_TILE_SNAPSHOT_TOO_BIG chat={chat_id} "
                                           f"chars={len(image)}")
            return JSONResponse({"error": "Chart snapshot is too large to pin."}, status_code=400)
        snapshot["image_base64"] = image
        snapshot["is_plotly"] = bool(body.get("is_plotly"))
        # Resolve the volatile "Show data" cache key into durable inline data
        # at pin time so it survives restarts (capped by _persistable_chart_data).
        chart_data = _persistable_chart_data(body.get("chart_data"))
        if chart_data is None:
            cd_key = body.get("chart_data_key")
            if isinstance(cd_key, str) and _FULL_KEY_RE.fullmatch(cd_key):
                rec = _load_full_table_record(local_store.ChatDataStore(chat_id), cd_key)
                chart_data = _persistable_chart_data(rec)
    else:
        table = _capped_table(body.get("table"))
        if not table or not table["rows"]:
            return JSONResponse({"error": "A table tile needs a table with rows."}, status_code=400)
        snapshot["table"] = table
        ftk = body.get("full_table_key")
        if isinstance(ftk, str) and _FULL_KEY_RE.fullmatch(ftk):
            full_table_key = ftk
        # The durable record's code is authoritative for table tiles (the
        # client-sent code may belong to the response's CHART).
        rec_code, rec_result_key = _resolve_table_record_code(chat_id, full_table_key)
        if rec_code:
            code = rec_code
            result_key = rec_result_key

    tile = {
        "chat_id": chat_id,
        "kind": kind,
        "title": _tile_title(description, kind),
        "description": description,
        "code": code or None,
        "snapshot": snapshot,
        "chart_data": chart_data,
        "full_table_key": full_table_key,
    }
    if kind == "table" and result_key is not None:
        tile["result_key"] = result_key
    saved = _dash_store.add_tile(email, dash_id, tile)
    if saved is None:
        return JSONResponse({"error": "Dashboard not found"}, status_code=404)
    return _json_safe({"ok": True, "tile": saved})


@router.post("/{dash_id}/tiles/{tile_id}/remove")
async def remove_tile(request: Request, dash_id: str, tile_id: str):
    email, err = _require_email(request)
    if err:
        return err
    doc, err = _require_owned(email, dash_id)
    if err:
        return err
    _dash_store.remove_tile(email, dash_id, tile_id)
    return {"ok": True}


@router.post("/{dash_id}/tiles/{tile_id}/update")
async def update_text_tile(request: Request, dash_id: str, tile_id: str):
    """Edit a TEXT tile's content/style (owner-only). Body: any subset of
    {text, style, color, size, align, valign}. Chart/table tiles are immutable through this
    endpoint (their content comes from refresh, never free edits)."""
    email, err = _require_email(request)
    if err:
        return err
    doc, err = _require_owned(email, dash_id)
    if err:
        return err
    tile = next((t for t in (doc.get("tiles") or []) if t.get("tile_id") == tile_id), None)
    if tile is None:
        return JSONResponse({"error": "Tile not found"}, status_code=404)
    if (tile.get("kind") or "").lower() != "text":
        return JSONResponse({"error": "Only text tiles can be edited."}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    fields, terr = _validate_text_fields(body or {}, require_text=False)
    if terr:
        return terr
    if not fields:
        return JSONResponse({"error": "Nothing to update."}, status_code=400)
    ok = _dash_store.update_tile(email, dash_id, tile_id, fields)
    if not ok:
        return JSONResponse({"error": "Tile not found"}, status_code=404)
    return _json_safe({"ok": True, "tile": {**tile, **fields}})


@router.post("/{dash_id}/layout")
async def update_layout(request: Request, dash_id: str):
    email, err = _require_email(request)
    if err:
        return err
    doc, err = _require_owned(email, dash_id)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    tiles = (body or {}).get("tiles")
    if not isinstance(tiles, list):
        return JSONResponse({"error": "Body must be {tiles: [{tile_id,x,y,w,h}]}."},
                            status_code=400)
    ok = _dash_store.update_layout(email, dash_id, tiles)
    return {"ok": bool(ok)}


@router.post("/{dash_id}/tiles/{tile_id}/refresh")
async def refresh_tile(request: Request, dash_id: str, tile_id: str):
    """Re-run one tile's stored code against the source chat's current data and
    persist the fresh snapshot into the (owner's) dashboard doc. Allowed for
    the owner AND shared recipients. Execution failures → 200 {ok:false,error}
    with the stored snapshot untouched (the dashboard keeps its last good
    render — exact mirror of the chat refresh contract)."""
    email, err = _require_email(request)
    if err:
        return err
    doc, is_owner = _dash_store.resolve_dashboard(email, dash_id)
    if doc is None:
        return JSONResponse({"error": "Dashboard not found"}, status_code=404)
    owner = doc.get("owner") or email
    tile = next((t for t in (doc.get("tiles") or []) if t.get("tile_id") == tile_id), None)
    if tile is None:
        return JSONResponse({"error": "Tile not found"}, status_code=404)
    if (tile.get("kind") or "").lower() == "text":
        # Text blocks have no source chat/code — refreshing is a no-op, and the
        # chat-existence check below must never freeze them.
        return {"ok": False, "error": "Text tiles have nothing to refresh."}

    chat_id = str(tile.get("chat_id") or "")
    if not chat_id or not local_store.chat_exists(chat_id):
        # Global fact — persist so every viewer sees the freeze.
        _dash_store.update_tile(owner, dash_id, tile_id,
                                {"frozen": True, "frozen_reason": "source_deleted"})
        return {"ok": False, "frozen": True, "reason": "source_deleted"}
    _, chat_err = _require_chat(request, chat_id)
    if chat_err:
        # Caller-specific (this viewer lacks source-chat access) — do NOT
        # persist into the shared doc; the tile stays live for everyone else.
        return {"ok": False, "frozen": True, "reason": "access_revoked"}

    code = (tile.get("code") or "").strip()
    kind = (tile.get("kind") or "chart").strip().lower()
    result_key = tile.get("result_key")
    if kind == "table":
        # Re-resolve the authoritative table code from the durable record on
        # every refresh: tiles pinned before this fix stored the response's
        # CHART code (mixed chart+table answers) — heal them in place.
        rec_code, rec_result_key = _resolve_table_record_code(chat_id, tile.get("full_table_key"))
        if rec_code and (rec_code != code or rec_result_key != result_key):
            code, result_key = rec_code, rec_result_key
            tile["code"], tile["result_key"] = code, result_key
            _dash_store.update_tile(owner, dash_id, tile_id,
                                    {"code": code, "result_key": result_key})
            log_with_sid(email, "info", f"DASH_TILE_CODE_HEALED dash={dash_id} tile={tile_id}")
    if not code:
        return {"ok": False, "error": "This tile cannot be refreshed."}

    # Role gate — BEFORE the branch split so both execution paths are covered.
    # Caller-specific like access_revoked above: never persisted, the tile
    # stays live for viewers whose role covers the table.
    drop, blocked = _role_refresh_block(email, chat_id, code)
    if blocked:
        log_with_sid(email, "info",
                     f"DASH_TILE_ROLE_DENIED dash={dash_id} tile={tile_id}")
        return {"ok": False, "frozen": True, "reason": "role_denied",
                "blocked_tables": blocked}

    if kind == "table" and result_key is not None:
        # Multi-table answers: the code's RESULT is a dict of tables; the
        # record's result_key names this tile's entry — _reexecute_full_df
        # (the Download-Excel path) handles the selection.
        df = await _reexecute_full_df(chat_id, code, result_key, drop_df_keys=drop)
        if df is None or getattr(df, "empty", True):
            result = {"ok": False, "error": "Could not re-run this table with the current data."}
        else:
            table = _json_safe({
                "columns": list(df.columns),
                "rows": df.head(_MAX_TILE_TABLE_ROWS).to_dict(orient="records"),
                "total_rows": int(len(df)),
            })
            result = {"ok": True, "kind": "table", "table": table}
            new_key = _persist_full_table(local_store.ChatDataStore(chat_id), table,
                                          code, result_key=result_key)
            if new_key:
                result["full_table_key"] = new_key
    else:
        result = await run_item_refresh(chat_id, code, kind, secrets.token_hex(8),
                                        drop_df_keys=drop)
    if not isinstance(result, dict) or not result.get("ok"):
        return _json_safe(result if isinstance(result, dict)
                          else {"ok": False, "error": "Refresh failed."})

    now = local_store._now()
    patch: dict = {"frozen": False, "frozen_reason": None}
    if kind == "table":
        table = _capped_table(result.get("table"))
        patch["snapshot"] = {"table": table, "rendered_at": now}
        if result.get("full_table_key"):
            patch["full_table_key"] = result["full_table_key"]
    else:
        patch["snapshot"] = {"image_base64": result.get("image_base64"),
                             "is_plotly": bool(result.get("is_plotly")),
                             "rendered_at": now}
        cd_key = result.get("chart_data_key")
        if isinstance(cd_key, str) and _FULL_KEY_RE.fullmatch(cd_key):
            rec = _load_full_table_record(local_store.ChatDataStore(chat_id), cd_key)
            fresh_cd = _persistable_chart_data(rec)
            if fresh_cd is not None:
                patch["chart_data"] = fresh_cd
    if not _dash_store.update_tile(owner, dash_id, tile_id, patch):
        log_with_sid(email, "warning", f"DASH_TILE_PATCH_LOST dash={dash_id} tile={tile_id}")
    updated_tile = {**tile, **patch}
    return _json_safe({**result, "tile": updated_tile})


# ---------------------------------------------------------------------------
# Sharing (mirrors POST /api/chat/{chat_id}/share)
# ---------------------------------------------------------------------------

@router.post("/{dash_id}/share")
async def share_dashboard(request: Request, dash_id: str):
    """Body: {emails: [...] | "a@x, b@y", message?}. Owner-only. Adds the
    recipients to the dashboard's shared_with + writes pointer rows into their
    dashboard lists, AND adds them to every tile's source chat sharing (same
    grant conversation-level sharing performs) so their Show-data / refresh
    work. Only dashboard name + comment go to the brain's SMTP relay — never
    tile content (Article II)."""
    email, err = _require_email(request)
    if err:
        return err
    doc, err = _require_owned(email, dash_id)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    raw_emails = body.get("emails") or []
    if isinstance(raw_emails, str):
        raw_emails = [e.strip() for e in raw_emails.replace(",", "\n").splitlines() if e.strip()]
    import re as _re
    _email_re = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    recipients = []
    for e in raw_emails:
        e = (e or "").strip().lower()
        if _email_re.match(e) and e != email:
            recipients.append(e)
    if not recipients:
        return JSONResponse({"error": "Provide at least one valid recipient email."},
                            status_code=400)
    message_text = (body.get("message") or "").strip()

    new_recipients = _dash_store.add_dashboard_share(email, dash_id, recipients)

    # Grant the recipients access to every tile's source chat so tiles are
    # live for them, not just the stored snapshots. Best-effort per chat.
    chat_ids = {t.get("chat_id") for t in (doc.get("tiles") or []) if t.get("chat_id")}
    for cid in chat_ids:
        try:
            if local_store.chat_exists(cid):
                local_store.ChatDataStore(cid).add_share_recipients(recipients)
        except Exception as e:
            log_with_sid(email, "warning", f"DASH_SHARE_CHAT_GRANT_FAILED chat={cid}: {e}")

    smtp_result = {"smtp_configured": False, "sent": [], "failed": []}
    if new_recipients:
        try:
            smtp_result = brain_client.send_share_email(
                to=new_recipients, subject=f"{email} shared a dashboard with you",
                sender_email=email, chat_title=doc.get("name") or "",
                message=message_text,
            ) or smtp_result
        except (TenantRevokedError, BrainError) as e:
            log_with_sid(email, "warning", f"DASH_SHARE_EMAIL_BRAIN_ERROR: {e}")
        except Exception as e:
            log_with_sid(email, "warning", f"DASH_SHARE_EMAIL_ERROR: {e}")

    fresh = _dash_store.get_dashboard(email, dash_id) or doc
    return {
        "ok": True,
        "shared_with": (fresh.get("sharing") or {}).get("shared_with") or [],
        "added": new_recipients,
        "email_sent": bool(smtp_result.get("sent")),
        "smtp_configured": bool(smtp_result.get("smtp_configured")),
        "failed": smtp_result.get("failed") or [],
    }
