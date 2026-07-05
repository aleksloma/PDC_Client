"""Session-level schema endpoints — verbatim ports of global
`backend/routes/schema.py` /schema_details and /schema_common_fields.

These run on the client (raw data lives here) and are called by `dashboard.js`
between /upload and /generate_chatdata to populate the schema-edit form and
auto-detect potential join columns.

ENTERPRISE NOTE: no LLM calls live in this file. The LLM-driven
column-description generation (the old global /schema_autofill_full) is in
`routes/upload.py` and goes through the brain.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from local_store import UserStore
from logger_utils import log_with_sid
from routes.chat import _EXEC
from settings import settings

router = APIRouter(tags=["schema"])


def _require_session(request: Request):
    email = request.session.get("email")
    sid = request.session.get("sid")
    if not email or not sid:
        return None, None
    return email, sid


def _compute_column_stats_optimized(col_name: str, series, total: int, existing_fields: dict,
                                     max_list: int, cat_limit: int) -> dict:
    """Compute column stats with sampling optimization for large datasets.

    Verbatim port of global `schema.py::_compute_column_stats_optimized`.
    """
    dtype = str(series.dtype)
    SAMPLE_THRESHOLD = 50000

    try:
        if total > SAMPLE_THRESHOLD:
            sample_size = min(10000, total)
            sample = series.dropna().sample(n=min(sample_size, len(series.dropna())), random_state=42)
            sample_nunique = sample.nunique()
            if sample_nunique > cat_limit:
                nunique = sample_nunique
            else:
                nunique = int(series.nunique(dropna=True))
        else:
            nunique = int(series.nunique(dropna=True))
    except Exception:
        nunique = None

    unique_values: list = []
    categorical = False

    if nunique is not None:
        categorical = (
            (nunique > 0 and nunique <= cat_limit and dtype in ["object", "category", "bool", "str", "string"])
            or (dtype == "bool")
        )
        if nunique <= max_list:
            try:
                uniq = series.dropna().unique().tolist()
                unique_values = [str(x) for x in uniq][:max_list]
            except Exception:
                unique_values = []

    ex = existing_fields.get(col_name, {}) if isinstance(existing_fields, dict) else {}
    desc = (ex.get("description") or "") if isinstance(ex, dict) else ""
    values_map = ex.get("values") if isinstance(ex, dict) else None
    if values_map is not None and not isinstance(values_map, dict):
        values_map = None

    missing_value_desc: list = []
    if categorical and unique_values:
        for v in unique_values:
            if not values_map or not isinstance(values_map.get(v), str) or not values_map.get(v).strip():
                missing_value_desc.append(v)

    non_null_count = int(series.count())

    return {
        "dtype": dtype,
        "nunique": nunique,
        "total_rows": total,
        "non_null_count": non_null_count,
        "unique_values": unique_values,
        "categorical": bool(categorical),
        "needs_description": True if not (isinstance(desc, str) and desc.strip()) else False,
        "missing_value_descriptions": missing_value_desc,
    }


@router.get("/schema_details")
async def schema_details(request: Request):
    """Return per-column stats for the UI form (which fields need descriptions,
    unique values, dtype, cardinality).

    Verbatim port of global /schema_details (storage→UserStore swap, no LLM).
    """
    total_start = time.time()
    email, sid = _require_session(request)
    if not email or not sid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    store = UserStore(sid)
    loop = asyncio.get_running_loop()
    dfs = await loop.run_in_executor(_EXEC, store.load_dataframes)
    meta = store.read_meta()

    MAX_LIST = getattr(settings, "SCHEMA_MAX_UNIQUE_LIST", 50)
    CAT_LIMIT = getattr(settings, "SCHEMA_CAT_LIMIT", 50)

    def process_file(entry) -> dict:
        fn = entry.get("file_name")
        df = dfs.get(fn)
        cols_out: dict = {}
        if df is not None:
            total = len(df)
            existing_fields: dict = {}
            if isinstance(entry.get("schema"), dict):
                existing_fields = entry.get("schema", {}).get("fields", {}) or {}
            columns = list(df.columns)
            if total > 50000 and len(columns) > 5:
                with ThreadPoolExecutor(max_workers=min(len(columns), 8)) as ex:
                    futures = {
                        ex.submit(_compute_column_stats_optimized,
                                  col, df[col], total, existing_fields, MAX_LIST, CAT_LIMIT): col
                        for col in columns
                    }
                    for future in futures:
                        col = futures[future]
                        try:
                            cols_out[col] = future.result()
                        except Exception:
                            cols_out[col] = {
                                "dtype": "unknown", "nunique": None, "total_rows": total,
                                "unique_values": [], "categorical": False,
                                "needs_description": True, "missing_value_descriptions": [],
                            }
            else:
                for col in columns:
                    cols_out[col] = _compute_column_stats_optimized(
                        col, df[col], total, existing_fields, MAX_LIST, CAT_LIMIT
                    )
        return {"file_name": fn, "columns": cols_out}

    file_entries = meta.get("files", [])

    # Column-stats computation is CPU-heavy — keep it off the event loop.
    def _compute_files_out() -> list:
        if len(file_entries) > 1:
            with ThreadPoolExecutor(max_workers=min(len(file_entries), 4)) as ex:
                return list(ex.map(process_file, file_entries))
        return [process_file(entry) for entry in file_entries]

    files_out = await loop.run_in_executor(_EXEC, _compute_files_out)

    log_with_sid(email, "info", f"SCHEMA_DETAILS time={time.time() - total_start:.2f}s files={len(files_out)}")
    return {"files": files_out}


def _normalize_col_name(name) -> str:
    s = str(name).lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return s.replace(" ", "").replace("_", "").replace("-", "")


@router.get("/schema_common_fields")
async def get_common_fields(request: Request):
    """Auto-detect potential join columns across multiple uploaded files.

    Verbatim port of global /schema_common_fields GET — fuzzy column-name
    matching, dtype agreement, cardinality similarity.
    """
    email, sid = _require_session(request)
    if not email or not sid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    store = UserStore(sid)
    loop = asyncio.get_running_loop()
    dfs = await loop.run_in_executor(_EXEC, store.load_dataframes)
    meta = store.read_meta()
    if len(dfs) < 2:
        return {"relationships": [], "user_defined": []}

    user_defined = meta.get("common_fields", []) or []
    file_list = list(dfs.items())

    # nunique() over every column pair is CPU-heavy — off the event loop.
    def _detect_relationships() -> list:
        detected: list = []
        for i, (file1, df1) in enumerate(file_list):
            for file2, df2 in file_list[i + 1:]:
                for col1 in df1.columns:
                    for col2 in df2.columns:
                        score = 0
                        reasons: list = []
                        col1_str = str(col1)
                        col2_str = str(col2)
                        if col1_str == col2_str:
                            score += 10
                            reasons.append("Exact name match")
                        else:
                            n1 = _normalize_col_name(col1_str)
                            n2 = _normalize_col_name(col2_str)
                            if n1 == n2:
                                score += 8
                                reasons.append("Similar names")
                            elif len(n1) > 3 and len(n2) > 3:
                                if n1 in n2 or n2 in n1:
                                    score += 5
                                    reasons.append("Name similarity")
                            for suffix in ("id", "code", "number", "num", "no", "key"):
                                if n1.endswith(suffix) and n2.endswith(suffix):
                                    score += 3
                                    reasons.append(f"Both end with '{suffix}'")
                                    break
                        try:
                            if str(df1[col1].dtype) == str(df2[col2].dtype):
                                score += 2
                                reasons.append(f"Same type ({df1[col1].dtype})")
                        except Exception:
                            pass
                        try:
                            u1 = df1[col1].nunique()
                            u2 = df2[col2].nunique()
                            if u1 > 0 and u2 > 0:
                                ratio = min(u1, u2) / max(u1, u2)
                                if ratio > 0.8:
                                    score += 3
                                    reasons.append(f"Similar unique count ({u1} vs {u2})")
                        except Exception:
                            pass
                        if score >= 5:
                            detected.append({
                                "file1": file1, "column1": col1_str,
                                "file2": file2, "column2": col2_str,
                                "confidence": min(100, int(score * 10)),
                                "reasons": reasons,
                                "auto_detected": True,
                            })
        return detected

    detected = await loop.run_in_executor(_EXEC, _detect_relationships)
    detected.sort(key=lambda x: x["confidence"], reverse=True)
    log_with_sid(email, "info", f"COMMON_FIELDS_DETECTED count={len(detected)}")
    return {"relationships": detected, "user_defined": user_defined}


@router.post("/schema_common_fields")
async def save_common_fields(request: Request):
    """Persist user-confirmed join relationships into meta.json."""
    email, sid = _require_session(request)
    if not email or not sid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
        relationships = body.get("relationships", [])
    except Exception as e:
        return JSONResponse({"error": f"Invalid request: {e}"}, status_code=400)

    store = UserStore(sid)
    meta = store.read_meta()
    meta["common_fields"] = relationships
    store.write_meta(meta)
    log_with_sid(email, "info", f"COMMON_FIELDS_SAVED count={len(relationships)}")
    return {"ok": True}


@router.get("/schema")
async def schema_get(request: Request):
    """Return current session schema (the `meta.json` shape global returns)."""
    email, sid = _require_session(request)
    if not email or not sid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return UserStore(sid).read_meta()


@router.post("/schema")
async def schema_save(request: Request):
    """Persist schema edits (descriptions + value mappings) for uploaded files.

    Body shape (from dashboard.js):
      {files: [{file_name, fields:{...}, file_description}]}
    """
    email, sid = _require_session(request)
    if not email or not sid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    store = UserStore(sid)
    meta = store.read_meta()
    posted = {f.get("file_name"): f for f in (body.get("files") or []) if f.get("file_name")}
    for entry in meta.get("files", []):
        name = entry.get("file_name")
        if name in posted:
            p = posted[name]
            file_desc = p.pop("file_description", None)
            if file_desc is not None:
                entry["file_description"] = file_desc
            old_fields = entry.get("schema", {}).get("fields", {}) if isinstance(entry.get("schema"), dict) else {}
            new_fields = (p.get("fields") or {})
            for col, field_data in new_fields.items():
                if isinstance(field_data, dict) and not field_data.get("technical_description"):
                    old_tech = (old_fields.get(col) or {}).get("technical_description") if isinstance(old_fields.get(col), dict) else None
                    if old_tech:
                        field_data["technical_description"] = old_tech
            if "fields" in p:
                entry.setdefault("schema", {})["fields"] = new_fields
    store.write_meta(meta)
    log_with_sid(email, "info", f"SAVE_SCHEMA files={list(posted.keys())}")
    return {"ok": True}
