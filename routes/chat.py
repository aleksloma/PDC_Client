"""Client-side chat endpoints — SSE stream + sidebar helpers + B2C-only stubs.

The chat-stream endpoint matches the B2C `/api/chat/{chat_id}/chat/stream`
shape that `dashboard.js` consumes:
  - emits `data: {...}\\n\\n` JSON events
  - `{progress: true, message: "..."}` for status text in the loading bubble
  - `{partial: true, answer, image_base64, chart_n, chart_total, conv_id}`
    for per-chart partial results (not used here — enterprise build emits
    a single final event), kept in the contract for parity
  - `{done: true, answer, image_base64, table, conv_id, tokens}` final event
  - `{error: "..."}` on failure (kill-switch surfaces here)

LLM work is done in a worker thread; results come back through an
`asyncio.Queue` (same pattern as the B2C `chat_stream_api`).
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import local_store
import run_chat_local
import brain_client
from brain_client import TenantRevokedError, BrainError
from logger_utils import log_with_sid
from schema_builder import _detect_language as _detect_lang_for_title


# Token-keyed in-memory cache of full result tables (the chat stream returns
# only a preview when `full_table_key` is set; the frontend fetches the full
# table via /api/chat/{chat_id}/full_table/{key}). Bounded LRU so very long
# sessions don't blow memory.
_FULL_TABLE_CACHE: dict[str, dict] = {}
_FULL_TABLE_ORDER: list[str] = []
_FULL_TABLE_MAX = 256

def _cache_full_table(table: dict) -> str:
    key = secrets.token_hex(8)
    _FULL_TABLE_CACHE[key] = table
    _FULL_TABLE_ORDER.append(key)
    while len(_FULL_TABLE_ORDER) > _FULL_TABLE_MAX:
        old = _FULL_TABLE_ORDER.pop(0)
        _FULL_TABLE_CACHE.pop(old, None)
    return key


_XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def _safe_xlsx_filename(filename: str) -> str:
    """Sanitize the client-supplied filename stem (no extension, no path/header
    injection). Mirrors the frontend's `table_<timestamp>` convention."""
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", (filename or "").strip())[:60]
    return stem or "table"


def _build_xlsx_response(columns: list, rows: list, filename: str) -> Response:
    """Build an in-memory .xlsx from columns + row dicts and return it as a
    download. 100% client-local — no brain call, raw data never leaves the
    client (Constitution Art. II / Art. V)."""
    import pandas as pd

    cols = list(columns or [])
    df = pd.DataFrame(rows or [], columns=cols or None)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Data")
    buf.seek(0)
    safe = _safe_xlsx_filename(filename)
    return Response(
        content=buf.getvalue(),
        media_type=_XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{safe}.xlsx"'},
    )


router = APIRouter(prefix="/api/chat", tags=["client-chat"])

# Worker pool for the SSE per-request thread (Article VI)
_EXEC = ThreadPoolExecutor(max_workers=4, thread_name_prefix="client_chat")

import atexit
atexit.register(lambda: _EXEC.shutdown(wait=False, cancel_futures=True))


def _json_safe(obj):
    """Best-effort safe JSON serialization for the SSE payload."""
    try:
        json.dumps(obj, ensure_ascii=False)
        return obj
    except Exception:
        # Fall back to str() for anything pandas/numpy that snuck through
        import pandas as pd
        import numpy as np
        if isinstance(obj, dict):
            return {k: _json_safe(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_json_safe(v) for v in obj]
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, pd.Series):
            return obj.to_list()
        if isinstance(obj, pd.DataFrame):
            return obj.to_dict(orient="records")
        return str(obj)


def _require_chat(request: Request, chat_id: str):
    email = request.session.get("email")
    if not email:
        return None, JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not local_store.chat_exists(chat_id):
        return None, JSONResponse({"error": "Chat not found"}, status_code=404)
    owner = local_store.get_chat_meta_owner(chat_id)
    if owner == email:
        return email, None
    # Allow shared recipients to access the chat (chat-level + conversation-level
    # sharing both populate meta.json["sharing"]["shared_with"]).
    try:
        store = local_store.ChatDataStore(chat_id)
        sharing = (store.read_meta().get("sharing") or {}).get("shared_with") or []
        if email in [s.lower() for s in sharing]:
            return email, None
    except Exception:
        pass
    return None, JSONResponse({"error": "Access denied"}, status_code=403)


def _conv_in_scope(email: str, chat_id: str, conv_id: str) -> bool:
    """Is `conv_id` accessible to `email` within `chat_id`?

    The chat OWNER has full access to every conversation in their chat. A shared
    (non-owner) recipient may ONLY touch conversations recorded in their own
    per-user index — never the owner's other conversations in the same shared
    chat. Keeps conversation isolation consistent across every conv-scoped
    endpoint (history, chat stream, edit-regenerate, report download)."""
    if local_store.get_chat_meta_owner(chat_id) == email:
        return True
    return local_store.user_owns_conversation(email, chat_id, conv_id)


def _require_conv(request: Request, chat_id: str, conv_id: str):
    """Chat-level membership (`_require_chat`) + conversation-level scope.

    Returns (email, None) on success, or (None, JSONResponse) on failure. A
    non-owner asking for a conversation outside their own index gets a 404 (we
    do not reveal that the owner's other conversations exist)."""
    email, err = _require_chat(request, chat_id)
    if err:
        return None, err
    if not _conv_in_scope(email, chat_id, conv_id):
        return None, JSONResponse({"error": "Conversation not found"}, status_code=404)
    return email, None


# ---------------------------------------------------------------------------
# Welcome + schema + history
# ---------------------------------------------------------------------------
@router.get("/{chat_id}/welcome")
async def welcome(request: Request, chat_id: str):
    """Same response shape as the B2C `/api/chat/{chat_id}/welcome`:
    {message, language, suggested_questions} — `dashboard.js` reads
    `welcomeData.message` and `welcomeData.suggested_questions`.
    """
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    store = local_store.ChatDataStore(chat_id)
    meta = store.read_meta()
    msg = meta.get("welcome_message") or store.get_welcome() or ""
    questions = meta.get("suggested_questions") or store.get_suggested_questions() or []
    # Detect language from file descriptions (same logic as global welcome handler)
    from schema_builder import _detect_language
    descs = [f.get("file_description", "") for f in meta.get("files", []) if f.get("file_description")]
    lang = _detect_language(" ".join(descs)) if descs else "en"
    return {
        "message": msg,
        "language": lang,
        "suggested_questions": questions,
    }


@router.get("/{chat_id}/schema")
async def get_schema(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    store = local_store.ChatDataStore(chat_id)
    meta = store.read_meta()
    return {
        "chat_id": chat_id,
        "files": meta.get("files", []),
        "common_fields": meta.get("common_fields", []),
    }


@router.post("/{chat_id}/schema")
async def save_schema(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    body = await request.json()
    store = local_store.ChatDataStore(chat_id)
    meta = store.read_meta()
    # Body shape from dashboard.js: {files: [{file_name, fields:{...}, file_description}]}
    posted = {f.get("file_name"): f for f in (body.get("files") or []) if f.get("file_name")}
    for entry in meta.get("files", []):
        name = entry.get("file_name")
        if name in posted:
            p = posted[name]
            if "fields" in p:
                entry.setdefault("schema", {})["fields"] = p["fields"]
            if "file_description" in p:
                entry["file_description"] = p["file_description"]
    store.write_meta(meta)
    return {"ok": True}


@router.get("/{chat_id}/conversation/{conv_id}/history")
async def history(request: Request, chat_id: str, conv_id: str):
    email, err = _require_conv(request, chat_id, conv_id)
    if err:
        return err
    store = local_store.ChatDataStore(chat_id)
    return {"history": store.get_history(conv_id)}


@router.get("/{chat_id}/history")
async def chat_history(request: Request, chat_id: str):
    """Legacy single-conversation history — return the most recent conv.

    The OWNER gets the newest conversation in the whole chat. A non-owner
    (shared recipient) is scoped to THEIR OWN conversations in this chat (newest
    first via the per-user index); it never exposes the owner's other convs.
    """
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    store = local_store.ChatDataStore(chat_id)
    try:
        if local_store.get_chat_meta_owner(chat_id) == email:
            # Owner: newest conversation file in the chat directory.
            convs = sorted(store.conversations_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not convs:
                return {"history": []}
            conv_id = convs[0].stem
            return {"history": store.get_history(conv_id), "conv_id": conv_id}
        # Non-owner: only their own conversations in this chat. list_conversations
        # is already sorted newest-first, so the first match is the most recent.
        own = [c for c in local_store.AuthStore().list_conversations(email)
               if c.get("chat_id") == chat_id and c.get("conv_id")]
        if not own:
            return {"history": []}
        conv_id = own[0]["conv_id"]
        return {"history": store.get_history(conv_id), "conv_id": conv_id}
    except Exception:
        return {"history": []}


@router.post("/{chat_id}/deactivate")
async def deactivate(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    local_store.AuthStore().deactivate_chat(email, chat_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# SSE chat stream  — the working chat path
# ---------------------------------------------------------------------------
@router.post("/{chat_id}/chat/stream")
async def chat_stream(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    body = await request.json()
    question = (body.get("question") or "").strip()
    conv_id = body.get("conv_id")
    if not question:
        return JSONResponse({"error": "Question cannot be empty."}, status_code=400)
    # Continuing an EXISTING conversation: a non-owner may only continue one in
    # their own index (a fresh conv with no conv_id is created + recorded under
    # the caller below, so it stays theirs).
    if conv_id and not _conv_in_scope(email, chat_id, conv_id):
        return JSONResponse({"error": "Conversation not found"}, status_code=404)

    store = local_store.ChatDataStore(chat_id)
    dfs = store.load_dataframes()
    if not dfs:
        return JSONResponse({"error": "Chat dataset is empty."}, status_code=400)
    schema_docs = store.schema_docs()
    # User-confirmed join columns (same source as GET /{chat_id}/schema): the
    # planner needs them so build_schema_text includes the join relationships,
    # exactly as Auto Analytics already does.
    common_fields = store.read_meta().get("common_fields") or []

    sid = secrets.token_hex(8)
    if not conv_id:
        conv_id = store.new_conversation(title=question[:80])
        local_store.AuthStore().record_conversation(email, chat_id, conv_id, title=question[:80])
        # Seed the conversation with the welcome message (matches B2C)
        welcome = store.get_welcome()
        if welcome:
            store.append_history(conv_id, {"role": "ai", "content": welcome, "ts": time.time()})

    history_rows = store.get_history(conv_id)
    store.append_history(conv_id, {"role": "human", "content": question, "ts": time.time()})

    log_with_sid(sid, "info", "CHAT_STREAM_REQ", user=email, chat_id=chat_id, conv_id=conv_id, q=question[:120])

    async def _sse_generator():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        # Multi-plot accumulator — partials are streamed live to the browser,
        # but the chart images must also be persisted so they reappear after
        # refresh/reopen. Matches global's `all_images` / `all_answers` pattern
        # in backend/routes/chat.py (final append_conv_history with images=[...]).
        all_images: list[str] = []
        all_answers: list[str] = []

        def _worker():
            try:
                # Use the multi-plot generator: it yields per-chart partials when
                # the planner emitted ###NEXT_PLOT### blocks, and a single
                # {single_response, result} for normal one-shot answers.
                gen = run_chat_local.run_chat_multi_plot(
                    sid=sid, dfs=dfs, schema_docs=schema_docs,
                    question=question, history_rows=history_rows,
                    user_email=email, common_fields=common_fields,
                )
                for event in gen:
                    loop.call_soon_threadsafe(queue.put_nowait, ("event", event))
            except TenantRevokedError as e:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", "Service unavailable. Please contact your administrator."))
            except BrainError as e:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", "Analysis service is temporarily unavailable."))
            except Exception as e:
                log_with_sid(sid, "error", f"CHAT_THREAD_ERROR: {type(e).__name__}: {e}")
                loop.call_soon_threadsafe(queue.put_nowait, ("error", "Something went wrong while processing your request."))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=_worker, daemon=True).start()

        # First event — empty progress + conv_id so the frontend can capture the conv_id
        yield f'data: {json.dumps({"progress": True, "message": "Working...", "conv_id": conv_id})}\n\n'

        while True:
            event = await queue.get()
            if event is None:
                break
            tag, payload = event
            if tag == "error":
                err_event = {"error": payload, "conv_id": conv_id, "done": True}
                yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n"
                # Persist the error as the AI turn so history reflects the failure
                store.append_history(conv_id, {"role": "ai", "content": payload, "ts": time.time()})
                continue
            if tag == "event":
                ev = payload
                # ── Multi-plot: per-chart partial event ─────────────────
                if ev.get("partial"):
                    img = ev.get("image_base64")
                    chart_answer = ev.get("answer", "")
                    # Accumulate for persistence on the final done event.
                    if img:
                        all_images.append(img)
                        all_answers.append(chart_answer)
                    partial_payload = {
                        "partial": True,
                        "answer": chart_answer,
                        "image_base64": img,
                        "chart_n": ev.get("chart_n"),
                        "chart_total": ev.get("chart_total"),
                        "conv_id": conv_id,
                        "tokens": ev.get("usage") or {},
                    }
                    yield f"data: {json.dumps(_json_safe(partial_payload), ensure_ascii=False)}\n\n"
                    if img:
                        try:
                            brain_client.post_activity("plot_generated", email,
                                                       {"chat_id": chat_id, "conv_id": conv_id,
                                                        "chart_n": ev.get("chart_n")})
                        except Exception:
                            pass
                    continue

                # ── Multi-plot: final done event ───────────────────────
                if ev.get("done") and not ev.get("single_response"):
                    answer = ev.get("combined_answer", "")
                    codes = ev.get("combined_codes") or []
                    usage = ev.get("total_usage") or {}
                    # Persist the chart images alongside the combined answer so
                    # the conversation re-renders after refresh / reopen. Matches
                    # global's persist shape (backend/routes/chat.py L1088-1105):
                    #   - 0 imgs  → image_base64=None, no images key
                    #   - 1 img   → image_base64=<the image>, no images key
                    #   - 2+ imgs → image_base64=None, images=[{image_base64, answer}, ...]
                    history_obj: dict = {
                        "role": "ai", "content": answer,
                        "image_base64": None, "table": None,
                        "code": "\n\n###NEXT_PLOT###\n\n".join(codes),
                        "usage": usage, "ts": time.time(),
                    }
                    if len(all_images) >= 2:
                        history_obj["images"] = [
                            {"image_base64": img, "answer": ans}
                            for img, ans in zip(all_images, all_answers) if img
                        ]
                    elif len(all_images) == 1:
                        history_obj["image_base64"] = all_images[0]
                    store.append_history(conv_id, history_obj)
                    out = {
                        "done": True, "partial": False,
                        "conv_id": conv_id,
                        "answer": answer,
                        "image_base64": None, "table": None,
                        "tokens": usage,
                    }
                    yield f"data: {json.dumps(_json_safe(out), ensure_ascii=False)}\n\n"
                    try:
                        human_count = sum(1 for m in store.get_history(conv_id) if m.get("role") == "human")
                        if human_count == 2:
                            _start_title_generation(email, chat_id, conv_id, question, answer)
                    except Exception:
                        pass
                    continue

                # ── Single-shot path: {single_response: True, result: ...} ─
                result = ev.get("result") or {}
                # Persist the AI turn (Article V)
                store.append_history(conv_id, {
                    "role": "ai",
                    "content": result.get("text", ""),
                    "image_base64": result.get("image_base64"),
                    "table": result.get("table"),
                    "code": result.get("code"),
                    "usage": result.get("usage"),
                    "ts": time.time(),
                })
                if result.get("image_base64"):
                    try:
                        brain_client.post_activity("plot_generated", email,
                                                   {"chat_id": chat_id, "conv_id": conv_id})
                    except Exception:
                        pass
                full_table_key = None
                tbl = result.get("table")
                if isinstance(tbl, dict) and tbl.get("rows") and len(tbl.get("rows") or []) > 0:
                    full_table_key = _cache_full_table(tbl)
                out = {
                    "done": True,
                    "partial": False,
                    "conv_id": conv_id,
                    "answer": result.get("text", ""),
                    "image_base64": result.get("image_base64"),
                    "table": result.get("table"),
                    "full_table_key": full_table_key,
                    "tokens": result.get("usage") or {},
                }
                yield f"data: {json.dumps(_json_safe(out), ensure_ascii=False)}\n\n"
                try:
                    human_count = sum(1 for m in store.get_history(conv_id) if m.get("role") == "human")
                    if human_count == 2:
                        _start_title_generation(email, chat_id, conv_id, question, result.get("text", ""))
                except Exception:
                    pass

    return StreamingResponse(_sse_generator(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------------------
# Edit-regenerate — verbatim port of global backend/routes/chat.py
# `edit_regenerate_api`, adapted for the brain/client split (LLM via brain,
# raw data + execution on client). dashboard.js triggers this from the
# pencil-edit affordance on a past user message.
# ---------------------------------------------------------------------------
@router.post("/{chat_id}/edit-regenerate")
async def edit_regenerate(request: Request, chat_id: str):
    """Edit the last user message and regenerate the AI response.

    Body: {edited_question: str, conv_id: str}
    Returns: {answer, image_base64?, table?, full_table_key?, conv_id, tokens}
    (or, for multi-chart responses, {answer, images: [...], conv_id, tokens}).

    Mirrors global's truncate-then-rerun behavior:
      1. Truncate the conv history to drop the last human turn (and everything
         after it).
      2. Append the edited question as the new human turn.
      3. Run `run_chat_multi_plot` against the local dfs (matches the normal
         chat-stream path), persisting the AI turn in the same JSONL shape.
    """
    email, err = _require_chat(request, chat_id)
    if err:
        return err

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    edited_question = (body.get("edited_question") or "").strip()
    conv_id = (body.get("conv_id") or "").strip()
    if not edited_question:
        return JSONResponse({"error": "Question cannot be empty."}, status_code=400)
    if not conv_id:
        return JSONResponse({"error": "Conversation ID is required."}, status_code=400)
    # A non-owner may only edit-regenerate a conversation in their own index.
    if not _conv_in_scope(email, chat_id, conv_id):
        return JSONResponse({"error": "Conversation not found"}, status_code=404)

    store = local_store.ChatDataStore(chat_id)
    if not store.root.exists():
        return JSONResponse({"error": "Chat not found."}, status_code=404)

    history = store.get_history(conv_id)
    if len(history) < 2:
        return JSONResponse(
            {"error": "Not enough messages to edit. Need at least one exchange."},
            status_code=400,
        )

    # Find the last human turn → keep everything *before* it.
    last_human_idx = None
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "human":
            last_human_idx = i
            break
    if last_human_idx is None:
        return JSONResponse({"error": "No user message found to edit."}, status_code=400)

    store.truncate_conv_history(conv_id, last_human_idx)
    log_with_sid(chat_id, "info",
                 f"EDIT_REGENERATE user={email} conv_id={conv_id} truncated_to={last_human_idx}")

    try:
        dfs = store.load_dataframes()
        if not dfs:
            return JSONResponse({"error": "Chat dataset is empty."}, status_code=400)
        schema_docs = store.schema_docs()
        # User-confirmed join columns — pass to the planner so the regenerated
        # schema text carries the join relationships (same as the chat stream).
        common_fields = store.read_meta().get("common_fields") or []

        sid = secrets.token_hex(8)
        history_rows = store.get_history(conv_id)
        # Append the edited human turn after truncation, before running.
        store.append_history(conv_id, {
            "role": "human", "content": edited_question, "ts": time.time(),
        })

        loop = asyncio.get_running_loop()

        def _run_blocking():
            return list(run_chat_local.run_chat_multi_plot(
                sid=sid, dfs=dfs, schema_docs=schema_docs,
                question=edited_question, history_rows=history_rows,
                user_email=email, common_fields=common_fields,
            ))

        events = await loop.run_in_executor(_EXEC, _run_blocking)
    except TenantRevokedError:
        msg = "Service unavailable. Please contact your administrator."
        store.append_history(conv_id, {"role": "ai", "content": msg, "ts": time.time()})
        return JSONResponse({"error": msg, "conv_id": conv_id}, status_code=503)
    except BrainError as e:
        msg = "Analysis service is temporarily unavailable."
        log_with_sid(chat_id, "warning", f"EDIT_REGENERATE_BRAIN_ERROR: {e}")
        store.append_history(conv_id, {"role": "ai", "content": msg, "ts": time.time()})
        return JSONResponse({"error": msg, "conv_id": conv_id}, status_code=503)
    except Exception as e:
        log_with_sid(chat_id, "error", f"EDIT_REGENERATE_ERROR: {type(e).__name__}: {e}")
        return JSONResponse(
            {"error": "Something went wrong while processing your request."},
            status_code=500,
        )

    # Accumulate multi-plot images (same shape used in the normal SSE path so
    # persistence + frontend rendering match exactly).
    all_images: list[str] = []
    all_answers: list[str] = []
    single_result: dict | None = None
    combined_codes: list[str] = []
    final_usage: dict = {}

    for ev in events:
        if ev.get("partial"):
            img = ev.get("image_base64")
            if img:
                all_images.append(img)
                all_answers.append(ev.get("answer", ""))
            continue
        if ev.get("done") and not ev.get("single_response"):
            combined_codes = ev.get("combined_codes") or []
            final_usage = ev.get("total_usage") or {}
            combined_answer = ev.get("combined_answer", "")
            history_obj: dict = {
                "role": "ai", "content": combined_answer,
                "image_base64": None, "table": None,
                "code": "\n\n###NEXT_PLOT###\n\n".join(combined_codes),
                "usage": final_usage, "ts": time.time(),
            }
            if len(all_images) >= 2:
                history_obj["images"] = [
                    {"image_base64": img, "answer": ans}
                    for img, ans in zip(all_images, all_answers) if img
                ]
            elif len(all_images) == 1:
                history_obj["image_base64"] = all_images[0]
            store.append_history(conv_id, history_obj)
            out: dict = {
                "ok": True, "done": True, "conv_id": conv_id,
                "answer": combined_answer,
                "image_base64": None, "table": None,
                "tokens": final_usage,
            }
            if "images" in history_obj:
                out["images"] = history_obj["images"]
            elif history_obj.get("image_base64"):
                out["image_base64"] = history_obj["image_base64"]
            return JSONResponse(_json_safe(out))

        # Single-shot path
        if ev.get("single_response"):
            single_result = ev.get("result") or {}
            store.append_history(conv_id, {
                "role": "ai",
                "content": single_result.get("text", ""),
                "image_base64": single_result.get("image_base64"),
                "table": single_result.get("table"),
                "code": single_result.get("code"),
                "usage": single_result.get("usage"),
                "ts": time.time(),
            })
            full_table_key = None
            tbl = single_result.get("table")
            if isinstance(tbl, dict) and tbl.get("rows"):
                full_table_key = _cache_full_table(tbl)
            out = {
                "ok": True, "done": True, "conv_id": conv_id,
                "answer": single_result.get("text", ""),
                "image_base64": single_result.get("image_base64"),
                "table": single_result.get("table"),
                "full_table_key": full_table_key,
                "tokens": single_result.get("usage") or {},
            }
            return JSONResponse(_json_safe(out))

    # Should not reach here, but degrade safely.
    return JSONResponse(
        {"error": "No response was produced.", "conv_id": conv_id},
        status_code=500,
    )


# ---------------------------------------------------------------------------
# Filename generator (used by the report download button)
# ---------------------------------------------------------------------------
@router.post("/{chat_id}/generate_filename")
async def generate_filename(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    title = (body.get("title") or "analysis_report").strip()
    import re as _re
    safe = _re.sub(r"[^a-zA-Z0-9_-]", "_", title)[:40] or "analysis_report"
    return {"filename": safe}


# ---------------------------------------------------------------------------
# Title generation — background, fired after 2nd human message
# ---------------------------------------------------------------------------
def _start_title_generation(email: str, chat_id: str, conv_id: str,
                             question: str, answer: str) -> None:
    """Spin a daemon thread to call brain /v1/title and rename the conversation."""
    def _worker():
        try:
            lang_code = _detect_lang_for_title((question + " " + answer)[:300])
            lang_name = {"ka": "Georgian", "ru": "Russian"}.get(lang_code, "English")
            rsp = brain_client.title(
                sid=f"title:{conv_id}", question=question, answer=answer,
                lang=lang_name, user_email=email,
            )
            new_title = (rsp.get("title") or "").strip()
            if new_title:
                local_store.AuthStore().rename_conversation(email, conv_id, new_title)
                log_with_sid(email, "info", f"CONV_TITLE_UPDATED conv_id={conv_id} title={new_title!r}")
        except Exception as e:
            log_with_sid(email, "warning", f"TITLE_GEN_FAILED: {e}")
    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Chat sharing — implemented (brain SMTP relay sends invites)
# ---------------------------------------------------------------------------
@router.get("/{chat_id}/share")
async def share_get(request: Request, chat_id: str):
    """Return the current chat-level sharing record."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    store = local_store.ChatDataStore(chat_id)
    meta = store.read_meta()
    sharing = meta.get("sharing") or {}
    return {
        "shared_with": sharing.get("shared_with") or [],
        "owner": email,
    }


@router.post("/{chat_id}/share")
async def share_post(request: Request, chat_id: str):
    """Body: {emails: ["a@x.com", ...], message?: "..."}.

    Adds the recipients to this chat's sharing list and asks the brain to
    SMTP-relay an invite email to each. The brain's SMTP relay uses this
    tenant's smtp_* config (set in the per-tenant admin page).
    """
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    body = await request.json()
    raw_emails = body.get("emails") or []
    if isinstance(raw_emails, str):
        raw_emails = [e.strip() for e in raw_emails.replace(",", "\n").splitlines() if e.strip()]
    recipients = []
    import re as _re
    _email_re = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    for e in raw_emails:
        e = (e or "").strip().lower()
        if _email_re.match(e) and e != email:
            recipients.append(e)
    if not recipients:
        return JSONResponse({"error": "Provide at least one valid recipient email."}, status_code=400)
    message_text = (body.get("message") or "").strip()

    store = local_store.ChatDataStore(chat_id)
    meta = store.read_meta()
    sharing = meta.get("sharing") or {"shared_with": []}
    existing = set(sharing.get("shared_with") or [])
    new_recipients = [r for r in recipients if r not in existing]
    existing.update(new_recipients)
    sharing["shared_with"] = sorted(existing)
    meta["sharing"] = sharing
    store.write_meta(meta)

    chat_title = meta.get("title", "")
    # Register the chat in each recipient's sidebar as an EMPTY chat: same
    # uploaded data/files/schema, but NONE of the owner's conversations. The
    # recipient starts fresh and creates their own conversations (recorded under
    # their own conversations.jsonl by the chat-stream endpoint). We deliberately
    # do NOT copy or record any conversation here — that is the conversation-share
    # path (routes/auth.py). `record_shared_chat` is idempotent per chat_id.
    files = [f.get("file_name") for f in meta.get("files", []) if f.get("file_name")]
    for rec in recipients:
        local_store.AuthStore().record_shared_chat(
            rec, chat_id, chat_title or "Chat", files, shared_by=email)

    smtp_result = {"smtp_configured": False, "sent": [], "failed": []}
    if new_recipients:
        try:
            smtp_result = brain_client.send_share_email(
                to=new_recipients, subject=f"{email} shared an analysis with you",
                sender_email=email, chat_title=chat_title, message=message_text,
            ) or smtp_result
        except (TenantRevokedError, BrainError) as e:
            log_with_sid(email, "warning", f"SHARE_EMAIL_BRAIN_ERROR: {e}")
        except Exception as e:
            log_with_sid(email, "warning", f"SHARE_EMAIL_ERROR: {e}")

    return {
        "ok": True,
        "shared_with": sharing["shared_with"],
        "added": new_recipients,
        "email_sent": bool(smtp_result.get("sent")),
        "smtp_configured": bool(smtp_result.get("smtp_configured")),
        "failed": smtp_result.get("failed") or [],
    }


# ---------------------------------------------------------------------------
# B2C-only features — disabled in enterprise (clean errors so the UI handles gracefully)
# ---------------------------------------------------------------------------
def _disabled(message: str, code: int = 400):
    return JSONResponse({"error": message}, status_code=code)


@router.post("/{chat_id}/publish")
@router.post("/{chat_id}/unpublish")
async def publish_disabled(request: Request, chat_id: str):
    return _disabled("Public publish is not available in the on-prem build.")


@router.get("/{chat_id}/publish-status")
async def publish_status_disabled(request: Request, chat_id: str):
    return {"public": False}


@router.post("/{chat_id}/auto_analysis/start")
async def auto_analysis_start(request: Request, chat_id: str):
    """Kick off the auto-analysis background job. Returns immediately; poll
    `/auto_analysis/status` for state and finally fetch the PPTX from
    `/auto_analysis/download` when status is `done`."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    import auto_analytics as aa
    store = local_store.ChatDataStore(chat_id)
    state = aa.get_auto_analysis_state(store.read_meta())
    if state.get("status") == "processing":
        return {"ok": True, "status": "processing", "message": "Already running."}
    from datetime import datetime, timezone as _tz
    aa._update_state(
        store, status="processing",
        started_at=datetime.now(_tz.utc).isoformat(),
        finished_at=None, error=None, progress="Starting…", pptx_path=None,
    )
    aa.start_job(chat_id, email)
    log_with_sid(email, "info", f"AUTO_ANALYTICS_START chat={chat_id}")
    return {"ok": True, "status": "processing"}


@router.get("/{chat_id}/auto_analysis/status")
async def auto_analysis_status(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    import auto_analytics as aa
    store = local_store.ChatDataStore(chat_id)
    return aa.get_auto_analysis_state(store.read_meta())


@router.get("/{chat_id}/auto_analysis/download")
async def auto_analysis_download(request: Request, chat_id: str):
    """Stream the auto-analysis PPTX. 404 until the job is `done`."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    import auto_analytics as aa
    store = local_store.ChatDataStore(chat_id)
    state = aa.get_auto_analysis_state(store.read_meta())
    if state.get("status") != "done":
        return JSONResponse(
            {"error": "Auto-analysis is not ready", "status": state.get("status")},
            status_code=404,
        )
    pptx_path = store.root / aa.AUTO_ANALYSIS_PPTX_NAME
    if not pptx_path.exists():
        return JSONResponse({"error": "Deck file is missing"}, status_code=404)
    from fastapi.responses import FileResponse
    return FileResponse(
        str(pptx_path),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"auto_analysis_{chat_id[:8]}.pptx",
    )


@router.get("/{chat_id}/suggested-questions")
async def suggested_questions_stub(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    store = local_store.ChatDataStore(chat_id)
    return {"questions": store.get_suggested_questions()}


@router.get("/{chat_id}/full_table/{key}")
async def full_table_get(request: Request, chat_id: str, key: str):
    """Return the full table that was cached for this key. The chat stream
    sets `full_table_key` on responses that contain a tabular result; the
    frontend fetches the full rows via this endpoint."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    tbl = _FULL_TABLE_CACHE.get(key)
    if not tbl:
        return JSONResponse({"error": "Full table not found or expired."}, status_code=404)
    return tbl


# ---------------------------------------------------------------------------
# Excel download — 100% client-local (no brain call). dashboard.js / chat.js
# POST these from the "Download Excel" button under a result table:
#   - export_excel: the preview table the frontend already holds (columns/rows).
#   - download_excel/{full_key}: the full cached table minted by the chat stream.
# Raw data never leaves the client (Constitution Art. II / Art. V).
# ---------------------------------------------------------------------------
@router.post("/{chat_id}/export_excel")
async def export_excel(request: Request, chat_id: str):
    """Body: {columns, rows, filename}. Returns an .xlsx of the posted table."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    columns = body.get("columns") or []
    rows = body.get("rows") or []
    filename = body.get("filename") or "table"
    try:
        return _build_xlsx_response(columns, rows, filename)
    except Exception as e:
        log_with_sid(email, "error", f"EXPORT_EXCEL_ERROR chat_id={chat_id}: {type(e).__name__}: {e}")
        return JSONResponse({"error": "Failed to build Excel file."}, status_code=500)


@router.post("/{chat_id}/download_excel/{full_key}")
async def download_excel(request: Request, chat_id: str, full_key: str):
    """Serve the full table cached under `full_key` (minted by the chat stream)
    as an .xlsx. Clean JSON 404 if the key has expired out of the LRU."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    tbl = _FULL_TABLE_CACHE.get(full_key)
    if not tbl:
        return JSONResponse({"error": "Full table not found or expired."}, status_code=404)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    filename = body.get("filename") or "table"
    try:
        return _build_xlsx_response(tbl.get("columns") or [], tbl.get("rows") or [], filename)
    except Exception as e:
        log_with_sid(email, "error", f"DOWNLOAD_EXCEL_ERROR chat_id={chat_id}: {type(e).__name__}: {e}")
        return JSONResponse({"error": "Failed to build Excel file."}, status_code=500)
