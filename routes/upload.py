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

import asyncio
import json
import secrets
from typing import List, Optional

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse

import brain_client
from brain_client import BrainError, TenantRevokedError
from local_store import (AuthStore, UserStore, ChatDataStore,
                         db_entries_from_meta, merge_schema_entry, unique_df_key)
from excel_table_detector import _EXTRACTED_TEXT_ABOVE_TABLE
from logger_utils import log_with_sid
from routes.chat import _EXEC
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
    # Also wipe any stale UserStore folder at the previous sid — off the event
    # loop, INCLUDING the constructor (its _ensure_layout does mkdirs + a
    # write): these syscalls block every concurrent request on slow storage
    # (each one is a network RPC on the GCS-fuse-mounted demo volume).
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_EXEC, lambda: UserStore(new_sid).reset_all())
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
    # Preserve database-table selections across the reset: the wizard may run
    # /session/db_tables before /upload (or the user adds files after picking
    # tables), and reset_all() would silently drop those meta-only entries.
    prior_meta = store.read_meta()
    preserved_db = db_entries_from_meta(prior_meta)
    preserved_db_ids = prior_meta.get("db_table_ids") or []
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

    # Try loading them to detect parse failures and to populate the dataframe-key
    # list — off the event loop, the detection pipeline blocks it otherwise.
    # The report variant returns per-file parse warnings/errors so a saved file
    # that produced no dataframe FAILS LOUDLY instead of ok:true (QA 2.1).
    loop = asyncio.get_running_loop()
    dfs, load_report = await loop.run_in_executor(_EXEC, store.load_dataframes_with_report)
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

    # Re-append the preserved database-table entries (deduping their df keys
    # against the fresh upload keys — display-name collisions re-key here, at
    # meta-write time, never at load time).
    if preserved_db:
        existing = {f.get("file_name") for f in meta.get("files", []) if f.get("file_name")}
        for entry in preserved_db:
            key = entry.get("file_name")
            if key in existing:
                entry = dict(entry)
                display = (entry.get("db") or {}).get("display_name") or key
                key = unique_df_key(existing, display)
                entry["file_name"] = key
                if isinstance(entry.get("schema"), dict):
                    entry["schema"] = {**entry["schema"], "file_name": key}
            meta.setdefault("files", []).append(entry)
            existing.add(key)
    if preserved_db_ids:
        meta["db_table_ids"] = preserved_db_ids
    store.write_meta(meta)

    # Per-file results: every saved file is either ok, parsed-with-warning, or
    # failed. /upload must NEVER answer ok:true when a saved file produced no
    # dataframe — that was the silent no-op the user saw (QA 2.1).
    produced = {n.split("::")[0] for n in df_names} | set(df_names)
    by_file = {r.get("file"): r for r in load_report}
    file_results: list[dict] = []
    failed: list[str] = []
    for name in saved:
        row = by_file.get(name)
        if row is not None:
            file_results.append(row)
            if row.get("status") == "error":
                failed.append(name)
        elif name in produced:
            file_results.append({"file": name, "status": "ok"})
        else:
            # saved but no dataframe and no report row (e.g. unsupported extension)
            file_results.append({"file": name, "status": "error",
                                 "message": "This file type could not be read as a table."})
            failed.append(name)

    log_with_sid(email, "info", "UPLOAD_OK", saved=",".join(saved), dataframes=",".join(df_names),
                 failed=",".join(failed))
    if failed:
        payload = {
            "ok": False, "saved": saved, "dataframes": df_names, "files": file_results,
            "error": (f"{len(failed)} of {len(saved)} file(s) could not be read: "
                      f"{', '.join(failed)}. Please fix or remove them and try again."),
        }
        # every file failed → hard 400; partial failure → 200 with ok:false so
        # the frontend's existing error branch aborts the dialog with the message
        return JSONResponse(payload, status_code=400) if len(failed) == len(saved) else payload
    return {"ok": True, "saved": saved, "dataframes": df_names, "files": file_results}


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
    loop = asyncio.get_running_loop()
    # include_db=False: autofill operates on uploaded files only. Database
    # entries carry ladmin-CONFIRMED descriptions from the registry — autofill
    # must never overwrite them, and listing keys must not read snapshots.
    dfs = await loop.run_in_executor(_EXEC, lambda: store.load_dataframes(include_db=False))
    if not dfs:
        if db_entries_from_meta(meta):
            # DB-tables-only session: nothing to autofill, not an error.
            return {"ok": True, "filled": 0, "files": [], "updated": 0}
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

    # Step 1 — per-file context (sampling + value_counts are CPU-heavy — off
    # the event loop)
    by_name = {f.get("file_name"): f for f in meta.get("files", [])}

    def _build_contexts() -> list[dict]:
        contexts: list[dict] = []
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
                contexts.append(ctx)
            except Exception as e:
                log_with_sid(email, "warning", f"AUTOFILL_FULL_CTX_FAIL file={fname}: {e}")
        return contexts

    file_contexts: list[dict] = await loop.run_in_executor(_EXEC, _build_contexts)

    # Step 2 — brain calls (one per file, run in parallel)
    from concurrent.futures import ThreadPoolExecutor

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

    # Step 4 — auto-generate technical_description for every column (no LLM;
    # per-column pandas stats are CPU-heavy — off the event loop)
    def _fill_technical_descriptions() -> None:
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

    await loop.run_in_executor(_EXEC, _fill_technical_descriptions)

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
    loop = asyncio.get_running_loop()
    # include_db=False: never read a multi-million-row snapshot just to list
    # keys; DB display names join via db_keys below.
    dfs = await loop.run_in_executor(_EXEC, lambda: user_store.load_dataframes(include_db=False))
    user_meta = user_store.read_meta()
    db_keys = [e.get("file_name") for e in db_entries_from_meta(user_meta) if e.get("file_name")]
    if not dfs and not db_keys:
        return JSONResponse({"error": "Upload a file or select a database table before generating chat data."}, status_code=400)
    all_keys = list(dfs.keys()) + db_keys
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
                files_info=all_keys,
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
                files_info=all_keys,
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

    name = provided_name or llm_name or _name_from_files(all_keys)

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

    AuthStore().record_active_chat(email, chat_id, title, all_keys)
    log_with_sid(
        email, "info", "CHAT_CREATED",
        chat_id=chat_id, title=title, files=len(all_keys),
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


# ---------------------------------------------------------------------------
# 4b. Database tables — client picker + session selection
# ---------------------------------------------------------------------------
# Registered database tables enter a chat as META-ONLY entries (source ==
# "database") referencing the central DATA_ROOT/db_snapshots/{table_id}.parquet
# by table_id. Descriptions come from ladmin's CONFIRMED registration — no
# brain call, no snapshot read happens here.

@router.get("/api/db_tables")
async def api_db_tables(request: Request):
    """Non-connector registered tables for the Create-New / Add Data picker.
    Connector (helper/join) tables are never listed to users — they are
    auto-included through the relations graph."""
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        from db_sources import DataSourceStore
        import roles_store

        # Off the event loop: the registry read is disk I/O (a network round
        # trip per syscall on the GCS-fuse-mounted demo volume).
        def _visible():
            rows = DataSourceStore().list_tables(include_connector=False)
            # Role gate: only tables the user's role covers right now (a
            # helper failure resolves to Base → empty list, fail-closed).
            allowed = roles_store.allowed_table_ids_for(email)
            return [r for r in rows if r.get("id") in allowed]

        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(_EXEC, _visible)
    except Exception as e:
        log_with_sid(email, "warning", f"DB_TABLES_LIST_FAILED: {e}")
        rows = []
    return {"tables": [{
        "table_id": r.get("id"),
        "display_name": r.get("display_name") or r.get("table_name"),
        "description": (r.get("description") or "")[:300],
        "row_count": r.get("row_count"),
        "refreshed_at": r.get("refreshed_at"),
    } for r in rows if r.get("id")]}


def _db_meta_entry(row: dict, df_key: str, auto_included: bool,
                   name_of: dict) -> dict:
    """Chat-meta entry for a registered table, built from its registry row.
    `name_of` maps table_id → df key (chat-local, deduped) or registry display
    name, so relations always carry a resolvable `related_df_key`."""
    fields = {}
    for col in row.get("columns") or []:
        cname = col.get("name")
        if not cname:
            continue
        fields[str(cname)] = {
            "description": col.get("description") or "",
            "technical_description": col.get("technical_description") or "",
            "values": None,
            "indexed": bool(col.get("indexed")),
        }
    relations = []
    for rel in row.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        rid = rel.get("related_table_id") or rel.get("related_table")
        related_key = (name_of.get(rid) or name_of.get(str(rid).lower())
                       or rel.get("related_df_key"))
        entry = {
            "related_table_id": rid,
            "related_df_key": related_key,
            "join_keys": rel.get("join_keys") or [],
        }
        # Discovered-relation extra (additive): schema_builder renders the
        # cardinality suffix when present; old registry rows stay byte-identical.
        if rel.get("cardinality") in ("N:1", "1:1", "1:N", "N:M"):
            entry["cardinality"] = rel["cardinality"]
        relations.append(entry)
    return {
        "file_name": df_key,
        "file_description": row.get("description") or "",
        "source": "database",
        "db": {
            "table_id": row.get("id"),
            "connection_id": row.get("connection_id"),
            "schema": row.get("schema"),
            "table_name": row.get("table_name"),
            "display_name": row.get("display_name") or row.get("table_name"),
            "is_connector": bool(row.get("is_connector")),
            "auto_included": bool(auto_included),
            "row_count": row.get("row_count"),
            "refreshed_at": row.get("refreshed_at"),
            "relations": relations,
        },
        "schema": {"file_name": df_key, "fields": fields},
    }


@router.post("/session/db_tables")
async def session_db_tables(request: Request):
    """Set the session's database-table selection. Body {table_ids: [...]}.

    Validates the ids, REJECTS a directly-selected connector table (400 —
    connectors are auto-only), expands the selection through the connector
    relations graph (frozen into the meta here — a later admin edit never
    silently changes an existing chat), and REPLACES any previous DB
    selection in the session meta."""
    email, sid = _require_session(request)
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not sid:
        sid = "s_" + secrets.token_hex(8)
        request.session["sid"] = sid

    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_ids = (body or {}).get("table_ids") or []
    ids = [t for t in raw_ids if isinstance(t, str)]

    from db_sources import DataSourceStore, expand_with_connectors
    reg = DataSourceStore()
    tables = {t.get("id"): t for t in reg.list_tables() if t.get("id")}
    unknown = [t for t in ids if t not in tables]
    if unknown:
        return JSONResponse({"error": "Unknown table selection."}, status_code=400)
    if any(tables[t].get("is_connector") for t in ids):
        return JSONResponse(
            {"error": "Connector tables are included automatically and cannot be selected directly."},
            status_code=400)
    # Role gate on the SEEDS only — the connector closure below is exempt by
    # design (connectors are invisible to users; gating them would silently
    # break allowed joins).
    import roles_store
    allowed = roles_store.allowed_table_ids_for(email)
    denied = [t for t in ids if t not in allowed]
    if denied:
        names = sorted((tables[t].get("display_name") or tables[t].get("table_name") or t)
                       for t in denied)
        log_with_sid(email, "warning",
                     f"SESSION_DB_TABLES_ROLE_DENIED sid={sid} tables={names}")
        return JSONResponse({"error": "Your role does not include: " + ", ".join(names),
                             "code": "ROLE_DENIED"}, status_code=403)

    closure = expand_with_connectors(ids, reg) if ids else []

    user_store = UserStore(sid)
    meta = user_store.read_meta()
    meta["files"] = [f for f in (meta.get("files") or [])
                     if not (isinstance(f, dict) and f.get("source") == "database")]
    existing = {f.get("file_name") for f in meta["files"] if f.get("file_name")}

    keymap: dict = {}
    for tid in closure:
        row = tables[tid]
        key = unique_df_key(existing, row.get("display_name") or row.get("table_name") or "table")
        keymap[tid] = key
        existing.add(key)
    # Relations may reference the related table by id OR by name (the same
    # forms expand_with_connectors resolves). Map every form to the df key
    # (chat-local, deduped) or, for non-included tables, the registry display
    # name (schema_builder drops relations whose endpoint isn't loaded).
    name_of: dict = {}
    for tid, t in tables.items():
        label = keymap.get(tid) or t.get("display_name") or t.get("table_name")
        name_of[tid] = label
        name_of[(t.get("table_name") or "").lower()] = label
        name_of[f"{t.get('schema') or ''}.{t.get('table_name') or ''}".lower()] = label

    out_rows = []
    for tid in closure:
        row = tables[tid]
        auto = tid not in ids
        meta["files"].append(_db_meta_entry(row, keymap[tid], auto, name_of))
        out_rows.append({"table_id": tid, "df_key": keymap[tid],
                         "display_name": row.get("display_name") or row.get("table_name"),
                         "is_connector": bool(row.get("is_connector")),
                         "auto_included": auto,
                         "row_count": row.get("row_count"),
                         "refreshed_at": row.get("refreshed_at")})
    meta["db_table_ids"] = ids
    user_store.write_meta(meta)
    log_with_sid(email, "info", "SESSION_DB_TABLES_SET", sid=sid,
                 selected=len(ids), included=len(closure))
    return {"ok": True, "tables": out_rows}


# ---------------------------------------------------------------------------
# 5. /add_data_to_chat  — merge the temp UserStore into an EXISTING chat
# ---------------------------------------------------------------------------
def _source_of_key(df_key: str) -> str:
    """Bare source filename of a dataframe key ("file.xlsx::Sheet" → "file.xlsx")."""
    return df_key.split("::", 1)[0] if "::" in df_key else df_key


def _resync_meta_after_add(old_files: list, new_entries: dict,
                           overwritten_sources: set,
                           added: list, updated: list, removed: list) -> list:
    """Merge the temp-session autofill entries into the chat's meta files.

    Non-overwritten files keep the previous semantics: brand-new keys are
    appended with their autofilled entries, untouched entries stay as-is.

    For an OVERWRITTEN source file the new upload is authoritative:
      - old entries whose key is NOT among the new upload's keys are DELETED
        (the stale-entry bug: vanished sheets no longer pollute the
        descriptions modal or schema_text);
      - a surviving key takes the FRESH autofilled entry (new columns keep
        fresh autofill, new technical_descriptions reflect the new data), but
        the old file_description is kept and every column that also existed
        before keeps its old description + value mappings (both are
        user-editable in the descriptions modal — edits must survive);
      - keys that are new in the upload are appended with fresh autofill.
    """
    merged: list = []
    for entry in old_files:
        key = entry.get("file_name")
        if isinstance(entry, dict) and entry.get("source") == "database":
            # DB entries are merged upstream by table_id — the filename-keyed
            # overwrite logic below must never touch (or drop) them.
            merged.append(entry)
            continue
        if not key or _source_of_key(key) not in overwritten_sources:
            merged.append(entry)
            continue
        if key not in new_entries:
            removed.append(key)
            continue
        # Shared carry-over (also used by the DB drift resync): old
        # file_description + per-column description/values survive; new
        # columns / technical_descriptions come from the fresh autofill.
        merged.append(merge_schema_entry(entry, new_entries[key]))
        updated.append(key)

    existing_keys = {e.get("file_name") for e in merged if e.get("file_name")}
    for key, entry in new_entries.items():
        if key in existing_keys:
            if key not in updated:
                updated.append(key)
            continue
        merged.append(entry)
        existing_keys.add(key)
        added.append(key)
    return merged


@router.post("/add_data_to_chat")
async def add_data_to_chat(request: Request):
    """Add uploaded files to an EXISTING chat.

    The frontend runs the SAME preprocessing pipeline as chat creation
    (/new_session → /upload → /schema_autofill_full — table detection, schema
    init, autofill all happen there, unchanged), then calls this instead of
    /generate_chatdata. Body: {chat_id}.

    Merge semantics:
      - Every uploaded file is copied into the chat's files dir; a filename
        that already exists in the chat overwrites it (a data update — the
        frontend's Add Data collision dialog has already made the user choose
        Overwrite vs upload-as-_vN, so this is never silent).
      - meta.json: entries for NEW keys are appended (with their autofilled
        schema/descriptions). For an OVERWRITTEN source file the entries are
        RE-SYNCED (`_resync_meta_after_add`): vanished keys deleted, surviving
        keys rebuilt from the fresh autofill with old file/column descriptions
        and value mappings carried over, new keys appended.
    schema_text is rebuilt from all loaded dataframes on every question, so
    the added files are picked up with no further work.
    """
    from routes.chat import _require_chat

    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    chat_id = ((body or {}).get("chat_id") or "").strip()
    if not chat_id:
        return JSONResponse({"error": "chat_id is required."}, status_code=400)

    email, err = _require_chat(request, chat_id)
    if err:
        return err
    sid = request.session.get("sid")
    if not sid:
        return JSONResponse({"error": "No upload session. Upload files first."}, status_code=400)

    user_store = UserStore(sid)
    loop = asyncio.get_running_loop()
    dfs = await loop.run_in_executor(
        _EXEC, lambda: user_store.load_dataframes(include_db=False))
    session_db_entries = db_entries_from_meta(user_store.read_meta())
    if not dfs and not session_db_entries:
        return JSONResponse({"error": "Upload a file or select a database table before adding data."}, status_code=400)

    try:
        import shutil

        chat_store = ChatDataStore(chat_id)
        chat_meta = chat_store.read_meta()

        # Copy the raw files. An overwrite is always user-confirmed upstream
        # (the Add Data collision dialog; rename is the default), so a name
        # match here is an intentional data update. Track which SOURCE files
        # were overwritten — their meta entries are re-synced below. The byte
        # overwrite bumps the file's size/mtime_ns, so the parquet cache
        # manifest mismatches and rebuilds on the next load (unchanged).
        overwritten_sources: set = set()
        for fp in sorted(user_store.files_dir.iterdir()):
            if fp.is_file() and not fp.name.startswith("."):
                if (chat_store.files_dir / fp.name).exists():
                    overwritten_sources.add(fp.name)
                shutil.copy2(fp, chat_store.files_dir / fp.name)

        user_meta = user_store.read_meta()
        new_entries: dict = {}
        for entry in user_meta.get("files", []):
            name = entry.get("file_name")
            # DB entries are merged separately below — keep them out of the
            # filename-keyed resync (its _source_of_key logic is file-shaped).
            if name and entry.get("source") != "database":
                new_entries[name] = entry

        # Merge the session's DATABASE selections first: same table already in
        # the chat → refresh its db block but keep the user's edited
        # descriptions (merge_schema_entry); new table → append with a df key
        # deduped against the chat's existing keys.
        added, updated, removed = [], [], []
        if session_db_entries:
            chat_files = chat_meta.get("files", []) or []
            by_tid = {(e.get("db") or {}).get("table_id"): i
                      for i, e in enumerate(chat_files)
                      if isinstance(e, dict) and e.get("source") == "database"}
            existing_keys = {e.get("file_name") for e in chat_files if e.get("file_name")}
            for entry in session_db_entries:
                tid = (entry.get("db") or {}).get("table_id")
                if tid in by_tid:
                    idx = by_tid[tid]
                    old = chat_files[idx]
                    fresh = dict(entry)
                    fresh["file_name"] = old.get("file_name")  # keep the chat's key
                    if isinstance(fresh.get("schema"), dict):
                        fresh["schema"] = {**fresh["schema"], "file_name": old.get("file_name")}
                    chat_files[idx] = merge_schema_entry(old, fresh)
                    updated.append(old.get("file_name"))
                else:
                    ent = dict(entry)
                    key = ent.get("file_name")
                    if key in existing_keys:
                        display = (ent.get("db") or {}).get("display_name") or key
                        key = unique_df_key(existing_keys, display)
                        ent["file_name"] = key
                        if isinstance(ent.get("schema"), dict):
                            ent["schema"] = {**ent["schema"], "file_name": key}
                    chat_files.append(ent)
                    existing_keys.add(key)
                    added.append(key)
            chat_meta["files"] = chat_files

        # Merge meta. On any resync failure fall back to the previous
        # append-behavior — never corrupt meta (Article IV).
        try:
            merged_files = _resync_meta_after_add(
                chat_meta.get("files", []) or [], new_entries,
                overwritten_sources, added, updated, removed)
        except Exception as e:
            log_with_sid(email, "error",
                         f"ADD_DATA_RESYNC_FAILED (falling back to append-merge): {e}",
                         chat_id=chat_id)
            added, updated, removed = [], [], []
            merged_files = list(chat_meta.get("files", []) or [])
            existing_names = {f.get("file_name") for f in merged_files if f.get("file_name")}
            for name, entry in new_entries.items():
                if name in existing_names:
                    updated.append(name)
                    continue
                merged_files.append(entry)
                existing_names.add(name)
                added.append(name)
        chat_meta["files"] = merged_files
        chat_store.write_meta(chat_meta)

        # Keep the sidebar's file list for this chat current (non-fatal).
        all_names = [f.get("file_name") for f in chat_meta.get("files", []) if f.get("file_name")]
        try:
            AuthStore().update_active_chat_files(email, chat_id, all_names)
        except Exception as e:
            log_with_sid(email, "warning", f"ADD_DATA_FILELIST_UPDATE_FAILED: {e}", chat_id=chat_id)

        log_with_sid(email, "info", "CHAT_DATA_ADDED", chat_id=chat_id,
                     added=",".join(added), updated=",".join(updated),
                     removed=",".join(removed))
        return {"ok": True, "chat_id": chat_id, "added": added,
                "updated": updated, "removed": removed, "files": all_names}
    except Exception as e:
        log_with_sid(email, "error", f"ADD_DATA_ERROR: {type(e).__name__}: {e}", chat_id=chat_id)
        return JSONResponse({"error": "Failed to add data to the chat."}, status_code=500)


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
