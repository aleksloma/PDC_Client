"""Precomputed dataset profiles — pure pandas, no I/O, no LLM.

A profile is a compact dict of computed FACTS about one table (row count,
duplicate rows, per-column stats, table grain, deterministic warnings).
Computed at upload/refresh time, stored as a sidecar JSON (see
`local_store.write_profile`), and attached to `/v1/plan` / `/v1/retry`
payloads so the brain can ground its plan in what the data actually IS —
e.g. "every Quantity value = 1, looks like a catalog/link table" — at
PLANNING time, before any code is generated.

Leaf module: imports pandas/stdlib only (like `outlier_utils.py`). Also owns
`_generate_technical_description` (moved verbatim from `routes/upload.py`)
so both the upload autofill step and `db_scheduler` share one stats module
without a routes→scheduler import inversion.

Only aggregate metadata leaves this module: counts, rates, flags, min/max,
and top-value hints truncated to 40 chars — the same class as the unique-
value hints `technical_description` already carries (Constitution Art. II).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

PROFILE_VERSION = 1
SAMPLE_THRESHOLD = 1_000_000   # above this, stats run on a sample
SAMPLE_ROWS = 500_000
PAIR_SCAN_MAX_ROWS = 500_000   # pair-grain scan only at or below this
MAX_GRAIN_CANDIDATE_COLS = 12
MAX_WARNINGS = 6
TOP_VALUES_N = 5
TOP_VALUE_MAXLEN = 40
LOW_CARDINALITY_MAX = 20       # top_values emitted when nunique <= this
NULL_PCT_WARN = 30.0


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


def _is_numeric_kind(series: pd.Series) -> bool:
    return getattr(series.dtype, "kind", "") in ("i", "u", "f")


def _is_datetime_kind(series: pd.Series) -> bool:
    return getattr(series.dtype, "kind", "") == "M"


def _column_profile(series: pd.Series, total: int) -> dict:
    """Stats for one column. Best-effort: a failing stat is omitted, the
    partial dict is still returned (Art. IV — never raise)."""
    out: dict = {"dtype": str(series.dtype)}
    try:
        non_null = int(series.count())
        nunique = int(series.nunique(dropna=True))
        null_count = int(total - non_null)
        out["nunique"] = nunique
        out["null_count"] = null_count
        out["null_pct"] = round(100.0 * null_count / total, 1) if total else 0.0
        out["constant"] = bool(non_null > 0 and nunique == 1)
        out["all_unique"] = bool(non_null > 0 and nunique == non_null)
    except Exception:
        return out
    try:
        if _is_numeric_kind(series) or _is_datetime_kind(series):
            clean = series.dropna()
            if len(clean):
                out["min"] = clean.min()
                out["max"] = clean.max()
    except Exception:
        pass
    try:
        dtype = str(series.dtype)
        is_texty = (dtype in ("object", "str", "string", "category", "bool")
                    or getattr(series.dtype, "kind", "") == "O")
        if is_texty or out.get("nunique", 0) <= LOW_CARDINALITY_MAX:
            vc = series.value_counts(dropna=True).head(TOP_VALUES_N)
            if len(vc):
                out["top_values"] = [[str(v)[:TOP_VALUE_MAXLEN], int(c)]
                                     for v, c in vc.items()]
    except Exception:
        pass
    return out


def _key_like_candidates(work: pd.DataFrame, col_profiles: dict, rows: int) -> list:
    """Candidate columns for the pair-grain scan: id/code-suffixed names
    first, then low-cardinality categoricals; capped at 12."""
    suffixed, categorical = [], []
    for col in work.columns:
        prof = col_profiles.get(str(col)) or {}
        name_l = str(col).strip().lower()
        if name_l.endswith("id") or name_l.endswith("code"):
            suffixed.append(col)
        elif "top_values" in prof and prof.get("nunique", rows) < rows / 2:
            categorical.append(col)
    return (suffixed + categorical)[:MAX_GRAIN_CANDIDATE_COLS]


def _grain_candidates(work: pd.DataFrame, col_profiles: dict) -> dict | None:
    """Detect the table grain (bounded). Single columns first; then pairs
    among key-like columns only. First match wins."""
    rows = len(work)
    if rows == 0:
        return None
    for col in work.columns:
        prof = col_profiles.get(str(col)) or {}
        if prof.get("nunique") == rows and prof.get("null_count") == 0:
            return {"columns": [str(col)], "kind": "single",
                    "text": f"one row per ({col})"}
    if rows > PAIR_SCAN_MAX_ROWS:
        return None
    cands = _key_like_candidates(work, col_profiles, rows)
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            try:
                if len(work[[a, b]].drop_duplicates()) == rows:
                    return {"columns": [str(a), str(b)], "kind": "pair",
                            "text": f"one row per ({a}, {b})"}
            except Exception:
                continue
    return None


def _build_warnings(work: pd.DataFrame, col_profiles: dict,
                    grain: dict | None, duplicate_row_count: int) -> list:
    """Deterministic short sentences, at most MAX_WARNINGS."""
    warnings: list = []
    for col in work.columns:
        prof = col_profiles.get(str(col)) or {}
        if prof.get("constant"):
            try:
                v = work[col].dropna().iloc[0]
            except Exception:
                v = "?"
            warnings.append(f"{col} is constant: every value = {str(v)[:TOP_VALUE_MAXLEN]}")
    for col in work.columns:
        prof = col_profiles.get(str(col)) or {}
        if prof.get("null_pct", 0) > NULL_PCT_WARN:
            warnings.append(f"{col} is {prof['null_pct']}% empty")
    try:
        if len(work) > 1 and bool(work.duplicated(keep=False).all()):
            warnings.append("All rows are duplicated at least once")
    except Exception:
        pass
    if grain and grain.get("kind") == "pair":
        try:
            key_cols = set(grain["columns"])
            numeric = [c for c in work.columns
                       if _is_numeric_kind(work[c]) and str(c) not in key_cols
                       and not str(c).strip().lower().endswith(("id", "code"))]
            if numeric and all((col_profiles.get(str(c)) or {}).get("constant")
                               for c in numeric):
                a, b = grain["columns"]
                warnings.append(
                    f"looks like a catalog/link table: one row per ({a}, {b}), "
                    f"no varying numeric measure")
        except Exception:
            pass
    return warnings[:MAX_WARNINGS]


def compute_profile(df: pd.DataFrame) -> dict:
    """The profile for one table. Pure; deterministic (sample uses a fixed
    seed); bounded (sampling above SAMPLE_THRESHOLD, pair scan capped)."""
    rows = int(len(df))
    sampled = rows > SAMPLE_THRESHOLD
    work = df.sample(n=SAMPLE_ROWS, random_state=0) if sampled else df
    total = len(work)

    col_profiles: dict = {}
    for col in work.columns:
        try:
            col_profiles[str(col)] = _column_profile(work[col], total)
        except Exception:
            col_profiles[str(col)] = {"dtype": str(work[col].dtype)}

    try:
        duplicate_row_count = int(work.duplicated().sum())
    except Exception:
        duplicate_row_count = 0

    grain = _grain_candidates(work, col_profiles)
    warnings = _build_warnings(work, col_profiles, grain, duplicate_row_count)

    return {
        "profile_version": PROFILE_VERSION,
        "rows": rows,
        "duplicate_row_count": duplicate_row_count,
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sampled": sampled,
        "grain": grain,
        "warnings": warnings,
        "columns": col_profiles,
    }
