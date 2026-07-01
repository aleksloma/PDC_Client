"""Client-side upload + chat-creation flow.

Mirrors the B2C dashboard.js "frictionless drop" pipeline:
  1. POST /new_session            → reset the per-session temp area
  2. POST /upload                 → save files to temp area, return {ok, saved, dataframes}
  3. POST /schema_autofill_full   → (optional) brain fills empty descriptions
  4. POST /generate_chatdata      → clone temp → permanent ChatDataStore,
                                    record chat for the user, return chat_id

Raw data NEVER leaves the client container. The brain only receives column
names + schema descriptions, never row values.
"""
from __future__ import annotations

import json
import secrets
from typing import List, Optional

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse

import brain_client
from brain_client import BrainError, TenantRevokedError
from local_store import AuthStore, UserStore, ChatDataStore
from excel_table_detector import _EXTRACTED_TEXT_ABOVE_TABLE
from logger_utils import log_with_sid
from settings import settings
from schema_builder import (
    columns_to_human_map,
    build_context_for_questions,
    file_descriptions_dict,
)

router = APIRouter(tags=["upload"])


def _require_session(request: Request) -> tuple[Optional[str], Optional[str]]:
    """Return (email, sid). Either may be None on auth failure."""
    return request.session.get("email"), request.session.get("sid")


# ---------------------------------------------------------------------------
# 1. /new_session  — reset temp area
# ---------------------------------------------------------------------------
@router.post("/new_session")
async def new_session(request: Request):
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    # Rotate sid → cleanest reset
    new_sid = "s_" + secrets.token_hex(8)
    request.session["sid"] = new_sid
    # Also wipe any stale UserStore folder at the previous sid
    UserStore(new_sid).reset_all()
    log_with_sid(email, "info", "NEW_SESSION_RESET", sid=new_sid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 2. /upload  — save files to per-session temp area
# ---------------------------------------------------------------------------
@router.post("/upload")
async def upload(request: Request, files: List[UploadFile] = File(...), file_descriptions: str = Form("")):
    email, sid = _require_session(request)
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not sid:
        # Auto-issue if missing (defensive — login should have done this)
        sid = "s_" + secrets.token_hex(8)
        request.session["sid"] = sid

    if not files:
        return JSONResponse({"error": "No files"}, status_code=400)
    if len(files) > settings.MAX_FILES:
        return JSONResponse({"error": f"You can upload up to {settings.MAX_FILES} files."}, status_code=400)

    descriptions: dict = {}
    if file_descriptions:
        try:
            descriptions = json.loads(file_descriptions)
        except Exception:
            return JSONResponse({"error": "Invalid file descriptions format."}, status_code=400)

    store = UserStore(sid)
    store.reset_all()  # Fresh temp area for this upload batch (matches B2C)

    saved = []
    try:
        for f in files:
            content = await f.read()
            store.save_upload(f.filename, content)
            saved.append(f.filename)
            log_with_sid(email, "info", "FILE_SAVED", file=f.filename, size_kb=int(len(content) / 1024))
            try:
                brain_client.post_activity("file_uploaded", email, {
                    "filename": f.filename, "size_bytes": len(content),
                })
            except Exception:
                pass
    except Exception as e:
        log_with_sid(email, "error", f"UPLOAD_ERROR: {e}")
        return JSONResponse({"error": f"Upload failed: {e}"}, status_code=500)

    # Try loading them to detect parse failures and to populate the dataframe-key list
    dfs = store.load_dataframes()
    df_names = list(dfs.keys())

    # Initialize empty schema entries (filled later by /schema_autofill_full)
    meta = store.read_meta()
    by_name = {f.get("file_name"): f for f in meta.get("files", [])}
    for name in df_names:
        # Excel sheets show up as "file.xlsx::Sheet" — keep them as separate entries
        base = name.split("::")[0] if "::" in name else name
        df = dfs[name]
        entry = by_name.get(name)
        if entry is None:
            entry = {"file_name": name}
            meta.setdefault("files", []).append(entry)
            by_name[name] = entry
        # Preserve any user-supplied description, otherwise empty
        if base in descriptions and descriptions[base]:
            entry["file_description"] = descriptions[base]
        else:
            entry.setdefault("file_description", "")
        if not entry.get("schema"):
            entry["schema"] = {
                "file_name": name,
                "fields": {c: {"description": "", "values": None} for c in df.columns},
            }
    # If multi-sheet replaced the base, drop the base-only entries
    seen = {n.split("::")[0] for n in df_names if "::" in n}
    if seen:
        meta["files"] = [f for f in meta.get("files", []) if f.get("file_name") not in seen or "::" in f.get("file_name", "")]

    # Step 4b (verbatim port of global upload.py): enrich descriptions using
    # text extracted above Excel tables by the 6-stage detection pipeline. The
    # Excel detector publishes that text into `_EXTRACTED_TEXT_ABOVE_TABLE`
    # keyed by df_key. Pop + send to the brain to summarize into a 2-3
    # sentence file_description. If summarization fails, fall back to the raw
    # extracted text (truncated). Raw row values are NEVER sent — only the
    # text rendered in the header band of the spreadsheet (titles / report
    # blurbs the spreadsheet's author typed above the table).
    for entry in meta.get("files", []):
        fn_key = entry.get("file_name", "")
        extracted = _EXTRACTED_TEXT_ABOVE_TABLE.pop(fn_key, "")
        if not extracted or entry.get("file_description"):
            continue
        try:
            rsp = brain_client.file_description(
                sid=f"upload:{sid}",
                extracted_text=extracted[:2000],
                user_email=email,
            )
            desc = (rsp.get("description") or "").strip()
            if desc:
                entry["file_description"] = desc[:500]
                log_with_sid(email, "info", f"UPLOAD_AUTO_DESC file={fn_key} chars={len(desc)}")
            else:
                entry["file_description"] = extracted[:500]
        except (TenantRevokedError, BrainError) as e:
            log_with_sid(email, "warning", f"UPLOAD_AUTO_DESC_BRAIN_ERROR file={fn_key}: {e}")
            entry["file_description"] = extracted[:500]
        except Exception as e:
            log_with_sid(email, "warning", f"UPLOAD_AUTO_DESC_ERROR file={fn_key}: {e}")
            entry["file_description"] = extracted[:500]

    store.write_meta(meta)

    log_with_sid(email, "info", "UPLOAD_OK", saved=",".join(saved), dataframes=",".join(df_names))
    return {"ok": True, "saved": saved, "dataframes": df_names}


# ---------------------------------------------------------------------------
# 3. /schema_autofill_full  — combined autofill (file desc + per-column descs)
# ---------------------------------------------------------------------------
#
# Verbatim port of global `backend/routes/schema.py:schema_autofill_full`,
# split for the brain/client boundary:
#   - Client builds per-file context (dtypes, sampled unique values, language
#     hint, columns needing fill). This is identical to global's
#     `_prepare_file_context`. Raw row data NEVER leaves the client beyond the
#     same sampled / truncated values global itself sends to its LLM.
#   - Brain runs the combined LLM call (`POST /v1/schema_autofill`) that asks
#     for `{file_description, columns: {col: desc}}` in one round-trip.
#   - Client persists both into meta.json AND auto-generates
#     `technical_description` for every column from local pandas stats (no LLM,
#     no network).

import re as _re_autofill


def _language_name_from_cols(cols: list) -> tuple[str, str]:
    """Verbatim port of global `_language_name_from_cols`."""
    try:
        txt = " ".join([str(c) for c in cols if isinstance(c, str)])
    except Exception:
        txt = ""
    if _re_autofill.search(r"[Ⴀ-ჿ]", txt):
        return "ka", "Georgian"
    if _re_autofill.search(r"[Ѐ-ӿ]", txt):
        return "ru", "Russian"
    return "en", "English"


def _prepare_file_context(fname: str, df, entry: dict, notes_text: str) -> dict:
    """Verbatim port of global `_prepare_file_context` (CPU-bound, no LLM).

    Builds the same payload global feeds its own LLM in-process. This runs on
    the client so raw row data never leaves; the brain only sees the sampled
    / truncated `unique_hints`.
    """
    import pandas as pd

    if not isinstance(entry.get("schema"), dict):
        entry["schema"] = {"file_name": fname, "fields": {}}
    fields = entry["schema"].setdefault("fields", {})

    SAMPLE_SIZE = 10000  # Same constant as global

    try:
        dtypes = {col: str(df[col].dtype) for col in list(df.columns)}
    except Exception:
        dtypes = {col: "unknown" for col in list(df.columns)}

    unique_hints: dict = {}
    columns = list(df.columns)[:settings.SCHEMA_AUTOFILL_MAX_COLS]

    for col in columns:
        try:
            ser = df[col].dropna()
            if len(ser) > SAMPLE_SIZE:
                sample = ser.sample(n=SAMPLE_SIZE, random_state=42)
                nun = sample.nunique()
            else:
                sample = ser
                nun = ser.nunique()

            if nun is not None and 0 < nun <= settings.SCHEMA_AUTOFILL_UNIQUE_THRESHOLD:
                if len(ser) > SAMPLE_SIZE:
                    uniq = sample.unique().tolist()
                else:
                    uniq = ser.unique().tolist()
                unique_hints[col] = [
                    str(v)[: settings.SCHEMA_AUTOFILL_VALUE_TRUNC] for v in uniq
                ][: settings.SCHEMA_AUTOFILL_UNIQUE_THRESHOLD]
            else:
                k = min(settings.SCHEMA_AUTOFILL_SAMPLE_VALUES, len(ser))
                if k > 0:
                    smp = ser.sample(n=k, replace=False).astype(str).tolist()
                    truncated = [s[: settings.SCHEMA_AUTOFILL_VALUE_TRUNC] for s in smp]
                    dtype = str(df[col].dtype)
                    if nun > 20 and dtype in ("object", "str", "string"):
                        try:
                            first_words = (
                                ser.dropna().astype(str).str.split().str[0].value_counts().head(5)
                            )
                            prefix_summary = ", ".join(
                                [f"'{w}'({c})" for w, c in first_words.items()]
                            )
                            avg_commas = ser.dropna().astype(str).str.count(",").mean()
                            pattern_note = (
                                f"[Patterns: prefixes={prefix_summary}, avg_commas={avg_commas:.1f}]"
                            )
                            unique_hints[col] = truncated + [pattern_note]
                        except Exception:
                            unique_hints[col] = truncated
                    else:
                        unique_hints[col] = truncated
                else:
                    unique_hints[col] = []
        except Exception:
            unique_hints[col] = []

    lang_code, lang_name = _language_name_from_cols(columns)

    cols_to_fill = [
        c for c in columns
        if not (
            isinstance(fields.get(c, {}).get("description"), str)
            and fields.get(c, {}).get("description").strip()
        )
    ]

    file_desc = entry.get("file_description", "") or ""
    if not file_desc and notes_text:
        for line in notes_text.split("\n"):
            if line.strip().startswith(f"{fname}:"):
                file_desc = line.split(":", 1)[1].strip()
                break

    return {
        "fname": fname,
        "entry": entry,
        "fields": fields,
        "dtypes": dtypes,
        "unique_hints": unique_hints,
        "lang_code": lang_code,
        "lang_name": lang_name,
        "cols_to_fill": cols_to_fill,
        "file_desc": file_desc,
        "df_columns": list(df.columns),
    }


def _generate_technical_description(series, total: int) -> str:
    """Verbatim port of global `_generate_technical_description`.

    Pure-pandas; runs on the client. Stored alongside the LLM-generated
    `description` so the prompt-builder can include both when the chat runs.
    """
    dtype = str(series.dtype)
    non_null = int(series.count())
    tech_parts = [dtype, f"{non_null}/{total} filled"]

    is_text = dtype in ("object", "str", "string", "category") or series.dtype.kind == "O"

    if is_text:
        try:
            uniq = series.dropna().unique()
            n_unique = len(uniq)
            if n_unique <= 20:
                tech_parts.append(
                    f"CATEGORICAL ({n_unique} unique: {', '.join(str(v) for v in uniq)})"
                )
            else:
                sample = uniq[:5].tolist()
                tech_parts.append(
                    f"{n_unique} unique, sample: {', '.join(str(v) for v in sample)}"
                )
        except Exception:
            pass
    elif dtype == "bool":
        tech_parts.append("boolean")
    else:
        try:
            sample = series.dropna().head(3).tolist()
            if sample:
                tech_parts.append(f"sample: {', '.join(str(v) for v in sample)}")
        except Exception:
            pass

    return ", ".join(tech_parts)


@router.post("/schema_autofill_full")
async def schema_autofill_full(request: Request):
    """Combined autofill — file description + per-column descriptions, in one
    LLM call per file. Mirrors global's `/schema_autofill_full`.

    Pipeline:
      1. Build per-file context locally (sampling, language detect, cols
         needing fill) — verbatim port of global's `_prepare_file_context`.
      2. POST `/v1/schema_autofill` to the brain (one call per file, ran in
         parallel via `asyncio.gather`).
      3. Merge `file_description` + `columns` into meta.json.
      4. Auto-generate `technical_description` for every column from pandas
         stats (no LLM) — matches global's step 4.

    Non-fatal: any per-file brain failure falls back to leaving descriptions
    blank (just like global) and proceeds to the next file. The technical
    description step still runs.
    """
    email, sid = _require_session(request)
    if not email or not sid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    store = UserStore(sid)
    meta = store.read_meta()
    dfs = store.load_dataframes()
    if not dfs:
        return JSONResponse({"error": "Upload files first"}, status_code=400)

    # User notes (same source as global: `text_path`)
    notes_text = ""
    try:
        text_path = getattr(store, "text_path", None)
        if text_path is not None and text_path.exists():
            notes_text = (text_path.read_text(encoding="utf-8") or "").strip()
    except Exception:
        notes_text = ""
    if notes_text:
        notes_text = notes_text[: settings.SCHEMA_AUTOFILL_NOTES_MAX_CHARS]

    # Step 1 — per-file context
    by_name = {f.get("file_name"): f for f in meta.get("files", [])}
    file_contexts: list[dict] = []
    for fname, df in dfs.items():
        entry = by_name.get(fname)
        if entry is None:
            entry = {
                "file_name": fname,
                "schema": {"file_name": fname, "fields": {}},
                "file_description": "",
            }
            meta.setdefault("files", []).append(entry)
            by_name[fname] = entry
        try:
            ctx = _prepare_file_context(fname, df, entry, notes_text)
            file_contexts.append(ctx)
        except Exception as e:
            log_with_sid(email, "warning", f"AUTOFILL_FULL_CTX_FAIL file={fname}: {e}")

    # Step 2 — brain calls (one per file, run in parallel)
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()

    def _call_one(ctx: dict) -> dict:
        try:
            return brain_client.schema_autofill(
                sid=f"autofill:{sid}",
                fname=ctx["fname"],
                cols_to_fill=ctx["cols_to_fill"],
                unique_hints=ctx["unique_hints"],
                dtypes=ctx["dtypes"],
                file_desc=ctx["file_desc"],
                notes_text=notes_text,
                lang_name=ctx["lang_name"],
                desc_word_limit=settings.SCHEMA_AUTOFILL_DESC_WORD_LIMIT,
                user_email=email,
            )
        except TenantRevokedError:
            raise
        except (BrainError, Exception) as e:
            log_with_sid(email, "warning",
                         f"AUTOFILL_FULL_LLM_FAIL file={ctx['fname']}: {e}")
            return {"file_description": "", "columns": {}}

    parsed_results: list[tuple[dict, dict]] = []
    if file_contexts:
        try:
            with ThreadPoolExecutor(max_workers=min(len(file_contexts), 4)) as ex:
                futures = [loop.run_in_executor(ex, _call_one, c) for c in file_contexts]
                parsed_list = await asyncio.gather(*futures, return_exceptions=False)
            for ctx, parsed in zip(file_contexts, parsed_list):
                parsed_results.append((ctx, parsed or {"file_description": "", "columns": {}}))
        except TenantRevokedError:
            log_with_sid(email, "warning", "AUTOFILL_FULL_TENANT_REVOKED")
            parsed_results = [(c, {"file_description": "", "columns": {}}) for c in file_contexts]

    # Step 3 — merge results into meta
    filled_total = 0
    file_results = []
    for ctx, parsed in parsed_results:
        fname = ctx["fname"]
        fields = ctx["fields"]
        df_columns = ctx["df_columns"]
        entry = ctx["entry"]

        suggestions = parsed.get("columns") or {}
        filled_here = 0
        for col, desc in suggestions.items():
            if (
                col in df_columns
                and isinstance(desc, str)
                and desc.strip()
            ):
                ex = fields.get(col, {}) if isinstance(fields.get(col), dict) else {}
                if not (
                    isinstance(ex.get("description"), str)
                    and ex.get("description").strip()
                ):
                    fields[col] = {**ex, "description": desc.strip()}
                    filled_here += 1

        new_file_desc = (parsed.get("file_description") or "").strip()
        existing_file_desc = (entry.get("file_description") or "").strip()
        if new_file_desc:
            entry["file_description"] = new_file_desc[:500]
        elif not existing_file_desc:
            entry["file_description"] = f"Dataset: {fname} with {len(df_columns)} columns"

        filled_total += filled_here
        file_results.append({
            "file_name": fname,
            "filled": filled_here,
            "file_description": entry.get("file_description", ""),
        })

    # Step 4 — auto-generate technical_description for every column (no LLM)
    for fname, df in dfs.items():
        entry = by_name.get(fname)
        if entry is None or df is None:
            continue
        if not isinstance(entry.get("schema"), dict):
            entry["schema"] = {"file_name": fname, "fields": {}}
        fields = entry["schema"].setdefault("fields", {})
        total = len(df)
        for col in df.columns:
            fields.setdefault(col, {})
            if isinstance(fields[col], str):
                fields[col] = {"description": fields[col]}
            try:
                fields[col]["technical_description"] = _generate_technical_description(df[col], total)
            except Exception as e:
                log_with_sid(email, "warning",
                             f"AUTOFILL_TECH_DESC_FAIL file={fname} col={col}: {e}")

    store.write_meta(meta)
    log_with_sid(email, "info",
                 f"SCHEMA_AUTOFILL_FULL filled_total={filled_total} files={len(file_results)}")
    return {"ok": True, "filled": filled_total, "files": file_results, "updated": filled_total}


# ---------------------------------------------------------------------------
# 4. /generate_chatdata  — promote temp UserStore to a new ChatDataStore
# ---------------------------------------------------------------------------
@router.post("/generate_chatdata")
async def generate_chatdata(request: Request):
    """Promote the per-session temp UserStore into a permanent ChatDataStore.

    Mirrors the global Step 1-6 flow:
      1. Load dataframes from the temp area.
      2. Read file_descriptions from meta.
      3. Call brain `/v1/chat_metadata` (verbatim port of global
         `_generate_all_parallel`) → name + welcome + suggested questions.
      4. De-duplicate the chosen name against existing user chats.
      5. Clone the temp UserStore into a new permanent ChatDataStore.
      6. Persist welcome + suggested_questions on the chat meta.
    """
    email, sid = _require_session(request)
    if not email or not sid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    provided_name = (body.get("name") or "").strip()

    user_store = UserStore(sid)
    dfs = user_store.load_dataframes()
    if not dfs:
        return JSONResponse({"error": "Upload at least one file before generating chat data."}, status_code=400)

    user_meta = user_store.read_meta()
    file_descs = file_descriptions_dict(user_meta)
    context, lang_instruction = build_context_for_questions(user_meta)
    cth = columns_to_human_map(user_meta)
    # The client only sends the DETECTED language as a hint. The welcome /
    # suggested-questions language is decided on the brain: the per-tenant
    # `welcome_language` override takes precedence over this hint (see
    # /v1/chat_metadata in PROTOCOL.md).

    # Brain LLM step (3 parallel sub-calls under the hood — matches global)
    llm_name, welcome_message, suggested_questions = "", "", []
    if not provided_name:
        try:
            rsp = brain_client.chat_metadata(
                sid=f"chatmeta:{sid}",
                files_info=list(dfs.keys()),
                file_descriptions=file_descs,
                context=context,
                lang_instruction=lang_instruction,
                columns_to_human=cth,
                user_email=email,
            )
            llm_name = (rsp.get("name") or "").strip() if rsp.get("name") else ""
            welcome_message = (rsp.get("welcome_message") or "").strip()
            suggested_questions = rsp.get("suggested_questions") or []
        except (TenantRevokedError, BrainError) as e:
            log_with_sid(email, "warning", f"CHAT_METADATA_BRAIN_ERROR: {e}")
        except Exception as e:
            log_with_sid(email, "warning", f"CHAT_METADATA_ERROR: {e}")
    else:
        # Name provided by user — only generate welcome + questions
        try:
            rsp = brain_client.chat_metadata(
                sid=f"chatmeta:{sid}",
                files_info=list(dfs.keys()),
                file_descriptions=file_descs,
                context=context,
                lang_instruction=lang_instruction,
                columns_to_human=cth,
                user_email=email,
            )
            welcome_message = (rsp.get("welcome_message") or "").strip()
            suggested_questions = rsp.get("suggested_questions") or []
        except (TenantRevokedError, BrainError) as e:
            log_with_sid(email, "warning", f"CHAT_METADATA_BRAIN_ERROR: {e}")
        except Exception as e:
            log_with_sid(email, "warning", f"CHAT_METADATA_ERROR: {e}")

    name = provided_name or llm_name or _name_from_files(list(dfs.keys()))

    existing = AuthStore().get_active_chat_names(email)
    title = name
    n = 1
    while title in existing:
        n += 1
        title = f"{name}_{n}"

    chat_id = "c_" + secrets.token_hex(8)
    chat_store = ChatDataStore(chat_id)
    chat_store.clone_from_user_store(user_store)

    meta = chat_store.read_meta()
    meta["owner"] = email
    meta["title"] = title
    meta["chat_id"] = chat_id
    if welcome_message:
        meta["welcome_message"] = welcome_message
    if suggested_questions:
        meta["suggested_questions"] = suggested_questions
    chat_store.write_meta(meta)

    # Keep legacy welcome.txt + suggested_questions.json for the existing
    # ChatDataStore.get_welcome() / get_suggested_questions() helpers.
    chat_store.set_welcome(welcome_message or "")
    chat_store.set_suggested_questions(suggested_questions or [])

    AuthStore().record_active_chat(email, chat_id, title, list(dfs.keys()))
    log_with_sid(
        email, "info", "CHAT_CREATED",
        chat_id=chat_id, title=title, files=len(dfs),
        questions=len(suggested_questions),
        welcome_chars=len(welcome_message),
    )

    return {
        "ok": True,
        "chat_id": chat_id,
        "name": title,
        "welcome_message": welcome_message,
        "suggested_questions": suggested_questions,
    }


def _name_from_files(filenames: list[str]) -> str:
    if not filenames:
        return "Data Analysis"
    first = filenames[0]
    # Strip extension + sheet suffix
    for sep in ("::", "."):
        if sep in first:
            first = first.split(sep)[0]
    return first[:60] or "Data Analysis"


# ---------------------------------------------------------------------------
# Disabled-in-enterprise upload paths
# ---------------------------------------------------------------------------
@router.post("/upload/init")
async def upload_init_disabled(request: Request):
    return JSONResponse(
        {"error": "Direct-to-GCS upload is not available in the on-prem build. The standard /upload path is used for all sizes."},
        status_code=400,
    )


@router.post("/upload/finalize")
async def upload_finalize_disabled(request: Request):
    return JSONResponse({"error": "Disabled in the on-prem build."}, status_code=400)


@router.post("/upload_from_url")
async def upload_from_url_disabled(request: Request):
    return JSONResponse(
        {"error": "Google Sheets / Drive import is not available in the on-prem build."},
        status_code=400,
    )
