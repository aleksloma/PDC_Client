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
from logger_utils import log_with_sid
from local_store import LRUTTLCache

# Memoizes the serialized schema string (mirrors the B2C `_schema_text_cache`,
# with a stronger key). The key covers schema_docs + common_fields + a per-df
# identity (name, shape, columns, dtypes, and a hash of the first rows), so
# neither a data update that keeps the file names nor a DIFFERENT chat whose
# files merely share names/columns can be served another dataset's schema
# string. Deep value changes past the hashed head can still lag by at most
# the TTL.
_SCHEMA_TEXT_CACHE = LRUTTLCache(max_size=100, ttl_seconds=300)


def _schema_text_cache_key(schema_docs, dfs, common_fields, other_tables=None) -> str | None:
    try:
        df_idents = []
        for name in sorted(dfs.keys()):
            df = dfs[name]
            head_hash = int(pd.util.hash_pandas_object(
                df.head(50).astype(str), index=False).sum()) if len(df) else 0
            df_idents.append([
                name, list(df.shape),
                [str(c) for c in df.columns],
                [str(t) for t in df.dtypes],
                head_hash,
            ])
        payload = (
            json.dumps(schema_docs, sort_keys=True, ensure_ascii=False, default=str)
            + "|" + json.dumps(df_idents, ensure_ascii=False)
            + "|" + json.dumps(common_fields or [], ensure_ascii=False, default=str)
        )
        # other_tables rows carry the registry's updated_at, so a re-registered
        # table changes the key automatically. Absent/empty → payload identical
        # to the pre-feature key (existing caches stay valid).
        if other_tables:
            payload += "|" + json.dumps(other_tables, sort_keys=True,
                                        ensure_ascii=False, default=str)
        return hashlib.md5(payload.encode("utf-8")).hexdigest()
    except Exception as e:
        log_with_sid("schema_text", "warning", f"SCHEMA_TEXT_CACHE_KEY_FAILED: {e}")
        return None


def schema_text(schema_docs: Dict[str, Dict], dfs: Dict[str, pd.DataFrame], common_fields: list | None = None,
                other_tables: list | None = None) -> str:
    cache_key = _schema_text_cache_key(schema_docs, dfs, common_fields, other_tables)
    if cache_key is not None:
        try:
            cached = _SCHEMA_TEXT_CACHE.get(cache_key)
            if cached is not None:
                return cached
        except Exception as e:
            log_with_sid("schema_text", "warning", f"SCHEMA_TEXT_CACHE_GET_FAILED: {e}")

    result = _schema_text_uncached(schema_docs, dfs, common_fields, other_tables)

    if cache_key is not None:
        try:
            _SCHEMA_TEXT_CACHE.set(cache_key, result)
        except Exception as e:
            log_with_sid("schema_text", "warning", f"SCHEMA_TEXT_CACHE_SET_FAILED: {e}")
    return result


def _schema_text_uncached(schema_docs: Dict[str, Dict], dfs: Dict[str, pd.DataFrame], common_fields: list | None = None,
                          other_tables: list | None = None) -> str:
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
                        # str(c): display keys must be JSON-safe even if a
                        # legacy cached parse still carries non-str column
                        # names (Timestamps, ints from read_json).
                        descs[str(c)] = d.get("description", "")
                        tech_descs[str(c)] = d.get("technical_description", "")
                        values = d.get("values", {}) or {}
                        if values and isinstance(values, dict):
                            value_descs[str(c)] = values

        dtype_info = {}
        for c in cols:
            try:
                # df[c] can be a DataFrame (not a Series) if a sheet slipped
                # through with duplicate column names — treat it as opaque
                # rather than crashing the whole question (Article IV; the
                # detector dedupes names at load, this is the safety net).
                dt = str(df[c].dtype)
            except Exception:
                dtype_info[str(c)] = "object"
                continue
            if df[c].dtype == object or df[c].dtype.kind == "O" or str(df[c].dtype) in ("str", "string", "object"):
                try:
                    uniq = df[c].dropna().unique()
                    n_unique = len(uniq)
                    if n_unique <= 20:
                        sample = uniq.tolist()
                        dtype_info[str(c)] = f"CATEGORICAL ({n_unique} unique values: {', '.join(str(v) for v in sample)})"
                    else:
                        sample = uniq[:10].tolist()
                        dtype_info[str(c)] = f"object ({n_unique} unique, sample: {', '.join(str(v) for v in sample)})"
                except Exception:
                    dtype_info[str(c)] = "object"
            else:
                dtype_info[str(c)] = dt

        file_info = f"File: {fname}"
        # Database-table entries (admin-registered "Data sources") carry a
        # source marker in schema_docs; file entries never have it, so any
        # chat without DB tables renders byte-identically to before.
        if isinstance(schema_docs.get(fname), dict) and schema_docs[fname].get("source") == "database":
            db_table = schema_docs[fname].get("db_table") or ""
            refreshed = schema_docs[fname].get("refreshed_at") or ""
            src_line = f"\nSource: database table {db_table}" if db_table else "\nSource: database table"
            if refreshed:
                src_line += f" (snapshot as of {refreshed})"
            file_info += src_line
        if file_desc and isinstance(file_desc, str) and file_desc.strip():
            file_info += f"\nFile Description: {file_desc.strip()}"
        file_info += f"\nColumns: {', '.join(str(c) for c in cols)}\nColumn Descriptions (business): {json.dumps(descs, ensure_ascii=False)}"
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

    # Declared join keys between database tables (admin-defined relations).
    # A relation renders only when BOTH endpoints are loaded in this chat —
    # the planner must never be pointed at a frame that isn't in dfs.
    # Local on purpose (schema_builder stays import-light); values mirror
    # relation_discovery.CARDINALITY_LABEL.
    _card_label = {"N:1": "many-to-one", "1:1": "one-to-one",
                   "1:N": "one-to-many", "N:M": "many-to-many"}
    db_rel_lines: list[str] = []
    seen_rels: set = set()
    for fname, f in (schema_docs or {}).items():
        if not (isinstance(f, dict) and f.get("source") == "database" and fname in dfs):
            continue
        for rel in f.get("relations") or []:
            if not isinstance(rel, dict):
                continue
            other = rel.get("related_df_key")
            if not other or other not in dfs:
                continue
            # Optional discovered-relation extra; absent/unknown values render
            # byte-identically to the pre-discovery format.
            card = _card_label.get(rel.get("cardinality"))
            suffix = f" ({card})" if card else ""
            for pair in rel.get("join_keys") or []:
                try:
                    c1, c2 = pair[0], pair[1]
                except Exception:
                    continue
                ident = tuple(sorted([(fname, str(c1)), (str(other), str(c2))]))
                if ident in seen_rels:
                    continue
                seen_rels.add(ident)
                db_rel_lines.append(f"  - {fname}[{c1}] ⟷ {other}[{c2}]{suffix}")
    if db_rel_lines:
        parts.append("\nDatabase Relations (declared join keys):\n" + "\n".join(db_rel_lines))

    # OTHER REGISTERED TABLES (QA report §4 / MISSING_DATA): metadata rows the
    # caller assembled from the registry (schema_builder stays import-light —
    # never reads db_sources itself). Lets the planner suggest which table to
    # ADD instead of fabricating values. Names/relations only — never values.
    # Absent/empty → output byte-identical to before.
    if other_tables:
        ot_lines: list[str] = []
        for t in other_tables:
            if not isinstance(t, dict):
                continue
            name = t.get("display_name") or ""
            cols = ", ".join(t.get("columns") or [])
            ot_lines.append(f"  - {name}: columns [{cols}]")
            for rel in t.get("relations") or []:
                if not isinstance(rel, dict):
                    continue
                other = rel.get("related_display_name")
                pairs = ", ".join(f"{p[0]} ⟷ {p[1]}" for p in (rel.get("join_keys") or [])
                                  if isinstance(p, (list, tuple)) and len(p) == 2)
                if other:
                    ot_lines.append(f"    relates to loaded table {other}"
                                    + (f" via [{pairs}]" if pairs else ""))
        if ot_lines:
            parts.append(
                "\nOTHER REGISTERED TABLES (registered in this installation but NOT "
                "loaded in this chat — their VALUES are not accessible to code; the "
                "user can add them to the chat):\n" + "\n".join(ot_lines))

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
