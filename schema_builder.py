"""Schema-text builder.

PORTED VERBATIM from the B2C `_schema_text` in `agent.py` (lines ~663-780).
The prompt told us to *reuse existing message shapes* — this is exactly the
string the B2C app feeds the planner LLM, just built client-side now so the
brain only receives the resulting text plus df names.

NOTE about sample categorical values: the existing `_schema_text` lists the
distinct values of categorical columns with ≤20 unique values. This is the
agreed-upon B2C shape and is what the user's instruction said to reuse. The
client agreement covers this exposure ("Column names going to the LLM is
acceptable and is covered in the client agreement").

If a stricter mode is ever needed, gate the "CATEGORICAL (n unique values:
...)" portion behind a per-tenant toggle.
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict

import pandas as pd

from settings import settings


def schema_text(schema_docs: Dict[str, Dict], dfs: Dict[str, pd.DataFrame], common_fields: list | None = None) -> str:
    parts: list[str] = []
    for fname, df in dfs.items():
        cols = list(df.columns)
        descs: dict = {}
        tech_descs: dict = {}
        value_descs: dict = {}
        file_desc = None
        if fname in schema_docs:
            f = schema_docs[fname]
            if isinstance(f, dict):
                file_desc = f.get("file_description")
                fields = f.get("fields", {}) or {}
                for c in cols:
                    d = fields.get(c, {})
                    if isinstance(d, dict):
                        descs[c] = d.get("description", "")
                        tech_descs[c] = d.get("technical_description", "")
                        values = d.get("values", {}) or {}
                        if values and isinstance(values, dict):
                            value_descs[c] = values

        dtype_info = {}
        for c in cols:
            dt = str(df[c].dtype)
            if df[c].dtype == object or df[c].dtype.kind == "O" or str(df[c].dtype) in ("str", "string", "object"):
                try:
                    uniq = df[c].dropna().unique()
                    n_unique = len(uniq)
                    if n_unique <= 20:
                        sample = uniq.tolist()
                        dtype_info[c] = f"CATEGORICAL ({n_unique} unique values: {', '.join(str(v) for v in sample)})"
                    else:
                        sample = uniq[:10].tolist()
                        dtype_info[c] = f"object ({n_unique} unique, sample: {', '.join(str(v) for v in sample)})"
                except Exception:
                    dtype_info[c] = "object"
            else:
                dtype_info[c] = dt

        file_info = f"File: {fname}"
        if file_desc and isinstance(file_desc, str) and file_desc.strip():
            file_info += f"\nFile Description: {file_desc.strip()}"
        file_info += f"\nColumns: {', '.join(cols)}\nColumn Descriptions (business): {json.dumps(descs, ensure_ascii=False)}"
        active_tech = {c: v for c, v in tech_descs.items() if v}
        if active_tech:
            file_info += f"\nColumn Technical Info: {json.dumps(active_tech, ensure_ascii=False)}"
        file_info += f"\nColumn Types: {json.dumps(dtype_info, ensure_ascii=False)}"
        if value_descs:
            file_info += f"\nValue Descriptions (categorical): {json.dumps(value_descs, ensure_ascii=False)}"
        parts.append(file_info)

    if common_fields:
        cf_lines = []
        for rel in common_fields:
            if not isinstance(rel, dict):
                continue
            f1, c1 = rel.get("file1"), rel.get("column1")
            f2, c2 = rel.get("file2"), rel.get("column2")
            conf = rel.get("confidence", 0)
            if f1 and c1 and f2 and c2:
                cf_lines.append(f"  - {f1}[{c1}] ⟷ {f2}[{c2}] (confidence: {conf}%)")
        if cf_lines:
            parts.append("\nCommon Fields (potential join columns):\n" + "\n".join(cf_lines))

    return "\n\n".join(parts)


def auto_schema_from_df(df: pd.DataFrame) -> dict:
    """Build a default empty schema_docs entry from a DataFrame.

    Mirrors what the B2C upload route does when the user has not yet filled in
    field descriptions — fields are listed with empty descriptions, and the
    LLM uses the column-type info already in the schema text.
    """
    fields = {c: {"description": "", "values": {}} for c in df.columns}
    return {"file_description": "", "fields": fields}


# ---------------------------------------------------------------------------
# Verbatim ports from global upload.py — used to build the input to the
# brain's /v1/chat_metadata endpoint. NO LLM calls; pure data shaping.
# ---------------------------------------------------------------------------
def columns_to_human_map(meta: dict) -> dict:
    """Build {column_name: human_label} for the brain sanitizer fallback.

    Verbatim from global upload.py._columns_to_human_map.
    """
    out: dict = {}
    for f in (meta.get("files") or []):
        fields = (f.get("schema") or {}).get("fields") or {}
        for col in fields.keys():
            if not isinstance(col, str) or not col:
                continue
            out[col] = col.replace("_", " ").strip()
    return out


def _detect_language(text: str) -> str:
    """Minimal language detector (Georgian / Russian / English)."""
    import re as _re
    if not text:
        return "en"
    if _re.search(r"[Ⴀ-ჿ]", text):
        return "ka"
    if _re.search(r"[Ѐ-ӿ]", text):
        return "ru"
    return "en"


_LANG_NAME_BY_CODE = {
    "en": "English",
    "ka": "Georgian (ქართული)",
    "ru": "Russian (Русский)",
}


def build_context_for_questions(meta: dict) -> tuple[str, str]:
    """Verbatim port of global upload.py._build_context_for_questions.

    Builds (context, lang_instruction) from the chat meta.
    """
    files = meta.get("files", [])
    context_parts = []
    for f in files:
        file_name = f.get("file_name", "")
        file_desc = f.get("file_description", "")
        schema = f.get("schema", {})
        file_context = f"File: {file_name}"
        if file_desc:
            file_context += f"\nDescription: {file_desc}"
        fields = schema.get("fields", {}) if isinstance(schema, dict) else {}
        if fields:
            column_list = []
            for field_name, field_data in list(fields.items())[:15]:
                field_desc = (field_data or {}).get("description", "")
                field_type = (field_data or {}).get("dtype", "")
                if field_desc:
                    column_list.append(f"  - {field_name} ({field_type}): {field_desc}")
                else:
                    column_list.append(f"  - {field_name} ({field_type})")
            file_context += "\nColumns:\n" + "\n".join(column_list)
        context_parts.append(file_context)
    context = "\n\n".join(context_parts) if context_parts else ""
    descriptions = [f.get("file_description", "") for f in files if f.get("file_description")]
    all_desc_text = " ".join(descriptions)
    lang = _detect_language(all_desc_text) if all_desc_text else "en"
    lang_instruction = _LANG_NAME_BY_CODE.get(lang, "English")
    return context, lang_instruction


def file_descriptions_dict(meta: dict) -> dict:
    """Verbatim shape from global generate_chatdata Step 2."""
    out = {}
    for f in (meta.get("files") or []):
        if isinstance(f, dict):
            name = f.get("file_name", "")
            desc = f.get("file_description", "") or f.get("description", "")
            if name:
                out[name] = desc
    return out
