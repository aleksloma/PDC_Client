"""Client-side chat orchestrator.

This is the enterprise split of the B2C `agent.run_chat`. The logic of the
flow is identical to the existing app (per the prompt: "Do not change the
logic of this flow — only split it across the client and brain containers."):

  1. Client builds schema_text from local dfs.
  2. Client → POST /v1/plan → Brain returns generated code.
  3. Client executes code locally with safe_execute / render_plot_safe.
  4. On error: Client → POST /v1/retry with error text → Brain returns
     corrected code → Client retries execution (up to 3 attempts, matching
     the B2C retry policy).
  5. Client → POST /v1/describe to get the natural-language intro,
     OR POST /v1/summarize when the result is scalar (no chart/table).

Raw data values stay on the client. The brain only sees: question, schema
text (column-level), generated code, execution error text, and scalar-safe
previews.
"""
from __future__ import annotations

import hashlib
import re
import time as _time
from typing import Any, Optional

import pandas as pd

import brain_client
import result_backstop
from code_exec import safe_execute
from plot_utils import render_plot_safe, _is_noninteractive_standard_chart
from logger_utils import log_with_sid
from schema_builder import schema_text as build_schema_text
from schema_builder import _detect_language as _detect_answer_language
from settings import settings


# Caps for capturing a pandas Styler's rendered HTML. A Styler renders the
# WHOLE frame (not just the 50-row preview), so guard against pathologically
# large styled payloads bloating the SSE/persisted history — fall back to the
# plain {columns,rows} table when exceeded. Generic, dataset-agnostic limits.
_STYLED_MAX_ROWS = 200
_STYLED_MAX_COLS = 40
_STYLED_MAX_CHARS = 500_000


def _styler_to_html(result_obj) -> Optional[str]:
    """Render a pandas Styler to self-contained HTML (gradient + .format kept).

    The model may return ``RESULT = df.style.background_gradient(...).format(...)``
    to convey conditional formatting. ``Styler.to_html`` embeds the gradient as
    uuid-scoped ``<style>`` and applies the model's ``.format()`` so numbers are
    already rounded/grouped. Returns the HTML string, or None for a non-Styler
    result / oversized table / any failure — callers then fall back to the plain
    table. Defensively strips ``<script>`` (the markup is table + ``<style>``
    only; the CSS is uuid-namespaced so it can't restyle the rest of the page).
    """
    try:
        # A Styler exposes the underlying frame as ``.data`` plus ``.to_html``;
        # a bare DataFrame has no ``.data`` attribute, so this picks out Stylers.
        if not (hasattr(result_obj, "data")
                and isinstance(getattr(result_obj, "data", None), pd.DataFrame)
                and hasattr(result_obj, "to_html")):
            return None
        df = result_obj.data
        if df.shape[0] > _STYLED_MAX_ROWS or df.shape[1] > _STYLED_MAX_COLS:
            return None
        html = result_obj.to_html()
        if not isinstance(html, str) or not html.strip() or len(html) > _STYLED_MAX_CHARS:
            return None
        import re
        html = re.sub(r"(?is)<script\b.*?</script\s*>", "", html)
        html = re.sub(r"(?is)<script\b[^>]*/?>", "", html)
        return html
    except Exception:
        return None


def _normalize_df_for_table(df: pd.DataFrame) -> pd.DataFrame:
    """Make a DataFrame serialize losslessly into the {columns, rows} JSON the
    frontend renders (QA 2.2).

    Two defects this fixes for pivot-shaped frames:
      - the row index (the pivot's grouping key, e.g. city names) was dropped
        by to_dict(orient="records") — reset a MEANINGFUL index into visible
        columns (MultiIndex, any named level, or a non-integer index such as
        Datetime/object). A plain RangeIndex or an unnamed integer index (a
        filtered frame) passes through untouched;
      - MultiIndex/tuple column labels became JSON arrays in `columns` but
        "_"-joined strings as row-dict keys (local_store._json_safe), so the
        frontend's by-name lookup missed every cell — flatten tuple labels to
        strings here so both halves agree.
    Never raises: any failure logs and returns the frame unchanged (Art. IV).
    """
    try:
        out = df
        idx = out.index
        meaningful = (isinstance(idx, pd.MultiIndex)
                      or any(n is not None for n in (idx.names or []))
                      or not (isinstance(idx, pd.RangeIndex)
                              or pd.api.types.is_integer_dtype(idx)))
        if meaningful:
            out = out.reset_index()
        if isinstance(out.columns, pd.MultiIndex) or any(isinstance(c, tuple) for c in out.columns):
            if out is df:
                out = out.copy(deep=False)
            out.columns = [
                " / ".join(str(p) for p in c if str(p).strip()) if isinstance(c, tuple)
                else str(c)
                for c in out.columns]
        # uniqueness after flattening (pandas "name.1" convention)
        if len(set(map(str, out.columns))) != len(out.columns):
            if out is df:
                out = out.copy(deep=False)
            seen: dict = {}
            cols = []
            for c in map(str, out.columns):
                n = seen.get(c, 0)
                seen[c] = n + 1
                cols.append(c if n == 0 else f"{c}.{n}")
            out.columns = cols
        return out
    except Exception as e:
        log_with_sid("table", "warning", f"TABLE_NORMALIZE_FAILED: {e}")
        return df


def _build_table_from_result(result_obj) -> Optional[dict]:
    """Tiny port of the B2C `_build_table_from_result` for DataFrame / Series.

    Returns {columns, rows, total_rows[, styled_html]} or None. When the result
    is a pandas Styler, also carries `styled_html` so the frontend can render
    the conditional formatting (B2C parity) while keeping the raw rows/columns
    for "Show full table" and "Download Excel".
    """
    if result_obj is None:
        return None
    try:
        if hasattr(result_obj, "data") and isinstance(result_obj.data, pd.DataFrame):
            df = result_obj.data
        elif isinstance(result_obj, pd.DataFrame):
            df = result_obj
        elif isinstance(result_obj, pd.Series):
            df = result_obj.reset_index()
        else:
            return None
        df = _normalize_df_for_table(df)
        rows = df.head(50).to_dict(orient="records")
        table = {
            "columns": list(df.columns),
            "rows": rows,
            "total_rows": int(len(df)),
        }
        styled = _styler_to_html(result_obj)
        if styled:
            table["styled_html"] = styled
        return table
    except Exception:
        return None


def _build_tables_from_result(result_obj) -> Optional[list]:
    """Handle a DICT of DataFrames/Series/Stylers as the executed RESULT
    (e.g. RESULT = {'Sales first 10': df1, 'Stock first 10': df2} — a shape
    models produce for "show each table" questions even though the planner
    prompt asks for a single DataFrame; before this, such answers rendered as
    prose garbage because no table event was built).

    Maps each DataFrame-like value through `_build_table_from_result` and
    carries the dict key as the table's `title`. Non-DF values are skipped.
    Returns a non-empty list of {title, columns, rows, total_rows
    [, styled_html]} dicts, or None."""
    if not isinstance(result_obj, dict) or not result_obj:
        return None
    tables: list = []
    for key, val in result_obj.items():
        try:
            t = _build_table_from_result(val)
            if t and int(t.get("total_rows", 0)) > 0 and (t.get("rows") or []):
                t["title"] = str(key)
                tables.append(t)
        except Exception as e:
            log_with_sid(str(key), "warning", f"MULTI_TABLE_BUILD_FAILED: {e}")
    return tables or None


def _tables_from_python_result(result_obj, table):
    """Shared post-processing for the PYTHON branches: when the single-table
    builder found nothing and the result is a dict of DF-likes, build the
    tables list; a single-entry dict unwraps back into the plain single-table
    path (no UI change needed for that common shape). Returns (table, tables)."""
    tables = None
    if table is None:
        tables = _build_tables_from_result(result_obj)
        if tables and len(tables) == 1:
            table = dict(tables[0])
            table.pop("title", None)
            tables = None
    return table, tables


def _other_registered_tables(schema_docs, dfs, user_email) -> list:
    """Registry tables NOT loaded in this chat, as metadata rows for the
    schema text's OTHER REGISTERED TABLES section (QA report §4 / MISSING_DATA).

    Lets the planner NAME the table that would answer a question instead of
    fabricating a mapping from domain context. Gated on the chat actually
    using database tables (pure file-upload chats get no section — their
    schema text stays byte-identical); role-filtered so a user is never shown
    a table their role cannot add (fail-closed like the picker). Metadata
    only — display names, column NAMES, declared relations — never values
    (Article II). Never raises: any failure returns [] (Article IV)."""
    try:
        docs = schema_docs or {}
        if not any(isinstance(v, dict) and v.get("source") == "database"
                   for v in docs.values()):
            return []
        from db_sources import DataSourceStore
        from roles_store import allowed_table_ids_for
        rows = DataSourceStore().list_tables(include_connector=False)
        if not rows:
            return []
        allowed = allowed_table_ids_for(user_email) if user_email else set()
        loaded_names = set(dfs or {}) | set(docs)
        id_to_name = {t.get("id"): (t.get("display_name") or "") for t in rows}
        out = []
        for t in rows:
            name = t.get("display_name") or ""
            if not name or name in loaded_names:
                continue
            if t.get("id") not in allowed:
                continue
            relations = []
            for rel in (t.get("relations") or []):
                if not isinstance(rel, dict):
                    continue
                parent_name = id_to_name.get(rel.get("related_table_id"))
                if not parent_name:
                    # legacy relation shape stores a display NAME, not an id
                    legacy = rel.get("related_table")
                    if isinstance(legacy, str) and legacy in loaded_names:
                        parent_name = legacy
                if parent_name and parent_name in loaded_names:
                    relations.append({
                        "related_display_name": parent_name,
                        "join_keys": [[str(p[0]), str(p[1])]
                                      for p in (rel.get("join_keys") or [])
                                      if isinstance(p, (list, tuple)) and len(p) == 2],
                    })
            out.append({
                "id": t.get("id"),
                "display_name": name,
                "updated_at": t.get("updated_at"),
                "columns": [str(c.get("name"))
                            for c in (t.get("columns") or []) if isinstance(c, dict)],
                "relations": relations,
            })
        out.sort(key=lambda r: r["display_name"])
        return out[:20]
    except Exception as e:
        log_with_sid(str(user_email or "?"), "warning", f"OTHER_TABLES_FAILED: {e}")
        return []


def _safe_preview(preview) -> Any:
    """The hard data-boundary guard (Article II): only scalars — or dicts whose
    VALUES are all plain scalars/None — may be forwarded to the brain. NEVER
    send DataFrame rows.

    The value check must be a scalar allow-list, not a list/dict deny-list: a
    dict of DataFrames/Stylers/Timestamps (e.g. RESULT = {"sales": df1.head(10),
    "stock": df2.head(10)}) previously slipped through and crashed the brain
    call's JSON encoding mid-turn ("Object of type Styler is not JSON
    serializable"). numpy scalars are coerced to native first so a
    {"mean": np.float64(...)} summary still passes."""
    if preview is None:
        return None
    if isinstance(preview, (str, int, float, bool)):
        return preview
    if isinstance(preview, dict):
        safe: dict = {}
        for k, v in preview.items():
            if not isinstance(k, (str, int, float, bool)):
                return None
            item = getattr(v, "item", None)
            if callable(item) and not isinstance(v, (str, int, float, bool)):
                try:
                    v = v.item()
                except Exception:
                    return None
            if v is not None and not isinstance(v, (str, int, float, bool)):
                return None
            safe[k] = v
        return safe
    return None


def run_chat(
    sid: str,
    dfs: dict[str, pd.DataFrame],
    schema_docs: dict,
    question: str,
    history_rows: list,
    user_email: str,
    common_fields: list | None = None,
    dataset_profile: dict | None = None,
) -> dict:
    """Run one chat turn end-to-end. Returns:
        {
          "text": str,
          "image_base64": str | None,
          "table": dict | None,
          "code": str | None,
          "usage": dict,
        }
    """
    df_names = list(dfs.keys())
    df_columns = {name: list(df.columns) for name, df in dfs.items()}
    schema_str = build_schema_text(
        schema_docs or {}, dfs, common_fields,
        other_tables=_other_registered_tables(schema_docs, dfs, user_email))

    # 1) Plan
    plan_out = brain_client.plan(
        sid=sid,
        question=question,
        schema_text=schema_str,
        df_names=df_names,
        history_rows=history_rows,
        common_fields=common_fields,
        user_email=user_email,
        dataset_profile=dataset_profile,
    )
    kind = plan_out.get("kind")
    code = plan_out.get("code") or ""
    usage = plan_out.get("usage") or {}
    context_decision = plan_out.get("context_decision") or {}
    log_with_sid(sid, "info", f"PLAN kind={kind} model={plan_out.get('model_used')}")

    # Clarification / text answer — return as text
    if kind in ("CLARIFICATION", "ANSWER", "MISSING_DATA"):
        return {"text": code, "image_base64": None, "table": None, "code": None, "usage": usage}

    # Greeting — call brain for natural reply
    if kind == "NO_CODE":
        if context_decision.get("is_greeting"):
            g = brain_client.greeting(sid, question, df_names, user_email=user_email)
            return {"text": g.get("text", ""), "image_base64": None, "table": None, "code": None, "usage": _sum_usage(usage, g.get("usage", {}))}
        # Else: try to summarize from scalar preview (we have no result yet, so just return generic)
        return {"text": "I couldn't generate analysis code for this question. Try rephrasing.", "image_base64": None, "table": None, "code": None, "usage": usage}

    # 2) Execute locally with retry (up to 3 attempts matching B2C policy)
    executor = render_plot_safe if kind == "PLOT_CODE" else safe_execute
    exec_out = executor(code, dfs, sid) if kind == "PLOT_CODE" else executor(code, dfs, sid)

    retry_count = 0
    max_retries = 3  # up to 3 retry attempts; escalate to pro/search from the 2nd
    while exec_out.get("error") and retry_count < max_retries:
        error_msg = exec_out.get("error", "Unknown")
        log_with_sid(sid, "warning", f"EXEC_ERROR attempt {retry_count+1}: {error_msg[:200]}")
        use_pro = retry_count >= 1
        use_search = retry_count >= 1
        retry_out = brain_client.retry(
            sid=sid, question=question, schema_text=schema_str,
            df_names=df_names, df_columns=df_columns,
            history_rows=history_rows,
            error_msg=error_msg, failed_code=code,
            use_pro=use_pro, use_search=use_search,
            user_email=user_email,
            dataset_profile=dataset_profile,
        )
        new_code = retry_out.get("code") or ""
        new_kind = retry_out.get("kind")
        usage = _sum_usage(usage, retry_out.get("usage") or {})
        retry_count += 1
        # A retry that returns prose (NO_CODE/CLARIFICATION/ANSWER/MISSING_DATA)
        # or no code is a FAILED attempt, not a reason to abort: keep retrying
        # with escalation. Prose text must never reach an executor.
        if not new_code or new_kind in ("NO_CODE", "CLARIFICATION", "ANSWER", "MISSING_DATA"):
            continue
        code = new_code
        kind = new_kind
        executor = render_plot_safe if kind == "PLOT_CODE" else safe_execute
        exec_out = executor(code, dfs, sid)

    # One-figure-per-chart: if the chart still combines multiple axes after the
    # retries (used by Auto Analytics, which renders one finding at a time),
    # split it and keep the FIRST single-figure chart — so a deck slide never
    # shows two plots in one image and is never silently dropped.
    if exec_out.get("multi_axes"):
        split_out = render_plot_safe(code, dfs, sid, split_multi_axes=True)
        sub = [c for c in (split_out.get("multi_charts") or []) if c.get("image")]
        if sub:
            d = _describe_with_backstop(sid, question, code, user_email,
                                        chart_data=sub[0].get("chart_data"),
                                        dataset_profile=dataset_profile)
            log_with_sid(sid, "info", f"MULTI_AXES_SPLIT_SINGLE kept=1 of {len(sub)}")
            return {
                "text": d.get("text", ""),
                "image_base64": sub[0].get("image"),
                "table": None, "code": code,
                "chart_data": sub[0].get("chart_data"),
                "usage": _sum_usage(usage, d.get("usage") or {}),
            }

    if exec_out.get("error"):
        log_with_sid(sid, "error", f"EXEC_FINAL_ERROR after {retry_count+1} attempts")
        return {
            "text": "I couldn't run this analysis with your current data. Try rephrasing or simplifying the request.",
            "image_base64": None, "table": None, "code": code, "usage": usage,
        }

    # Interactivity enforcement: a STANDARD chart that rendered as static
    # matplotlib/seaborn gets ONE Plotly-rewrite retry. Genuine matplotlib
    # exceptions (plot_tree/venn/wordcloud/...) are left untouched.
    if (kind == "PLOT_CODE" and not exec_out.get("is_plotly")
            and not exec_out.get("multi_charts")
            and _is_noninteractive_standard_chart(code)):
        log_with_sid(sid, "info", "NONINTERACTIVE_REGEN_SINGLE")
        retry_out = brain_client.retry(
            sid=sid, question=question, schema_text=schema_str,
            df_names=df_names, df_columns=df_columns, history_rows=history_rows,
            error_msg=_PLOTLY_REGEN_INSTRUCTION, failed_code=code,
            use_pro=False, use_search=False, user_email=user_email,
            dataset_profile=dataset_profile,
        )
        usage = _sum_usage(usage, retry_out.get("usage") or {})
        new_code = retry_out.get("code") or ""
        if new_code and retry_out.get("kind") == "PLOT_CODE":
            re_out = render_plot_safe(new_code, dfs, sid)
            if not re_out.get("error") and not re_out.get("multi_axes"):
                code, exec_out = new_code, re_out

    # 3) Collect outputs
    if kind == "PLOT_CODE":
        if exec_out.get("is_plotly"):
            img_b64 = exec_out.get("plotly_html")
        else:
            img_b64 = exec_out.get("image")
        # Describe from code (brain — no data leaves)
        d = _describe_with_backstop(sid, question, code, user_email,
                                    chart_data=exec_out.get("chart_data"),
                                    dataset_profile=dataset_profile)
        return {
            "text": d.get("text", ""),
            "image_base64": img_b64,
            "table": None,
            "code": code,
            "chart_data": exec_out.get("chart_data"),
            "usage": _sum_usage(usage, d.get("usage") or {}),
        }

    # PYTHON branch
    result_obj = exec_out.get("result")
    preview = exec_out.get("preview")
    table = _build_table_from_result(result_obj)
    if table and (int(table.get("total_rows", 0)) <= 0 or not (table.get("rows") or [])):
        table = None
    table, tables = _tables_from_python_result(result_obj, table)
    img_b64 = exec_out.get("image_base64")

    if img_b64:
        d = _describe_with_backstop(sid, question, code, user_email,
                                    result_obj=result_obj, dataset_profile=dataset_profile)
        return {"text": d.get("text", ""), "image_base64": img_b64, "table": table, "tables": tables, "code": code, "usage": _sum_usage(usage, d.get("usage") or {})}

    if table or tables:
        d = _describe_with_backstop(sid, question, code, user_email,
                                    result_obj=result_obj, dataset_profile=dataset_profile)
        return {"text": d.get("text", ""), "image_base64": None, "table": table, "tables": tables, "code": code, "usage": _sum_usage(usage, d.get("usage") or {})}

    # Scalar result — safe to forward to the summarizer
    safe_p = _safe_preview(preview)
    s = brain_client.summarize(
        sid=sid, question=question, schema_text=schema_str,
        history_rows=history_rows, preview=safe_p,
        context_decision=context_decision,
        user_email=user_email,
    )
    return {"text": s.get("text", ""), "image_base64": None, "table": None, "code": code, "usage": _sum_usage(usage, s.get("usage") or {})}


def _sum_usage(a: dict, b: dict) -> dict:
    keys = {"input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"}
    out = {}
    for k in keys:
        out[k] = int((a.get(k) or 0) + (b.get(k) or 0))
    return out


# ── Deterministic result backstop (Prompt 14 Part B) ─────────────────────────
# CRITICAL RULE 7 asks the MODEL to detect a constant metric / identical series
# and explain it. Verified live: it complies only sometimes. So every describe
# call goes through this wrapper — the client inspects the executed result
# itself (result_backstop, pure pandas), sends the findings to /v1/describe as a
# mandatory caveat, and if the returned description does not carry the marker
# the client prepends its OWN localized sentence. The note is never lost.
_DF_KEY_RE = re.compile(r"dfs\[['\"]([^'\"]+)['\"]\]")


def _profile_caveat_facts(code: str, dataset_profile: dict | None) -> tuple[list, bool]:
    """Grain / catalog facts for the tables the executed CODE actually used.
    Returns (grain_lines, catalog_flag). Never raises."""
    grain: list = []
    catalog = False
    try:
        if not dataset_profile:
            return grain, catalog
        used = set(_DF_KEY_RE.findall(code or ""))
        for key, prof in dataset_profile.items():
            if used and key not in used:
                continue
            if not isinstance(prof, dict):
                continue
            g = prof.get("grain") or {}
            if isinstance(g, dict) and g.get("text"):
                grain.append(f"{key}: {g['text']}")
            for w in (prof.get("warnings") or []):
                if "catalog/link table" in str(w):
                    catalog = True
                    if str(w) not in grain:
                        grain.append(f"{key}: {w}")
    except Exception:                                        # noqa: BLE001
        return grain, catalog
    return grain[:4], catalog


def _describe_with_backstop(sid: str, question: str, code: str,
                            user_email: str | None = None, *,
                            chart_data=None, chart_data_list=None,
                            result_obj=None,
                            dataset_profile: dict | None = None) -> dict:
    """brain_client.describe + the deterministic flat-result backstop.

    No detection -> a plain describe call with unchanged behavior (errors
    propagate exactly as before). Detection -> the facts ride along as
    `data_caveat`, and the answer is guaranteed to carry the explanation."""
    det = result_backstop.inspect_outputs(
        chart_data=chart_data, chart_data_list=chart_data_list,
        result_obj=result_obj, sid=sid)
    if not det:
        return brain_client.describe(sid=sid, question=question, code=code,
                                     user_email=user_email)

    grain, catalog = _profile_caveat_facts(code, dataset_profile)
    caveat = {"kind": det["kind"], "facts": det.get("facts") or [],
              "grain": grain, "catalog": catalog}
    log_with_sid(sid, "info",
                 f"RESULT_BACKSTOP_FIRED kind={det['kind']} "
                 f"facts={len(caveat['facts'])} catalog={catalog}")
    try:
        d = brain_client.describe(sid=sid, question=question, code=code,
                                  user_email=user_email, data_caveat=caveat)
        reason = "missing_marker"
    except Exception as e:                                   # noqa: BLE001
        log_with_sid(sid, "warning", f"RESULT_BACKSTOP_DESCRIBE_FAILED: {e}")
        d = {"text": "", "usage": {}}
        reason = "describe_failed"

    text = (d.get("text") or "").strip()
    if result_backstop.DATA_NOTE_MARKER in text:
        log_with_sid(sid, "info", "RESULT_BACKSTOP_MARKER_OK")
        return {"text": result_backstop.strip_marker(text), "usage": d.get("usage") or {}}

    # Hard fallback: the model dropped the note (or describe failed) — the
    # client states it itself, FIRST, so the user can never see a flat result
    # without the explanation.
    lang = _detect_answer_language(question)
    sentence = result_backstop.fallback_sentence(lang, catalog=catalog)
    log_with_sid(sid, "info", f"RESULT_BACKSTOP_NOTE_PREPENDED reason={reason}")
    combined = f"{sentence}\n\n{text}".strip() if text else sentence
    return {"text": combined, "usage": d.get("usage") or {}}


# ---------------------------------------------------------------------------
# Multi-chart streaming
# ---------------------------------------------------------------------------
_MAX_MULTI_PLOTS = 6
# Max BLOCKS considered before dedup (bounds render cost). The final EMITTED
# chart count is capped at _MAX_MULTI_PLOTS *after* dedup so the kept charts are
# the distinct ones — a candidate pool a bit larger than the emit cap gives
# dedup room to drop near-duplicates without starving the response.
_MAX_MULTI_PLOT_CANDIDATES = 12
# How many times a single block that renders >1 axes is sent back to the brain
# asking for SEPARATE ###NEXT_PLOT### blocks before falling back to image-split.
_MAX_MULTI_AXES_RETRY = 1
# How many times a STANDARD chart that rendered as static matplotlib/seaborn is
# sent back to the brain asking for an interactive Plotly rewrite (the model can
# regress to seaborn on retry/stochastically — this guard makes interactivity
# reliable rather than dependent on model compliance).
_MAX_PLOTLY_REGEN = 1
_PLOTLY_REGEN_INSTRUCTION = (
    "NonInteractiveChartError: this is a STANDARD chart but it was rendered with "
    "matplotlib/seaborn. Rewrite it using INTERACTIVE Plotly — plotly.express (px) "
    "or go.Figure assigned to a variable named fig — with category NAMES on the axis "
    "(never set_xticks(range(n))+set_xticklabels). Do NOT use matplotlib or seaborn. "
    "Keep the same data, grouping, title and axis labels."
)


# ── Near-duplicate chart dedup (chat multi-plot) ──────────────────────────
# Deterministic safety net mirroring the deck-side `_aa_dedupe_instructions`
# (brain/routes/llm.py): drop a chart whose ANALYSIS is near-identical to one
# already produced in THIS multi-chart response. The deck path compares the
# natural-language instructions; here the client only sees CODE, so we compare
# an analysis SIGNATURE built from the code: the set of PLOTTED source columns
# (core px/go axis/measure kwargs + groupby dimension keys) plus the chart KIND.
# Renamed/aggregated outputs (named-agg + .rename) and concatenated labels are
# resolved back to their SOURCE columns; orientation, dataframe filenames, and a
# purely cosmetic categorical color are ignored — so two re-worded versions of
# the SAME measure by the SAME dimension (even flipped, renamed, or tinted by an
# extra category) collapse, while genuinely different charts (different metric,
# dimension, chart type, or a numeric color carrying a 2nd measure) stay.
# Threshold 0.8 matches the deck path and was calibrated on real persisted decks
# (158 within-response pairs): every true duplicate scored >= 0.8 and was dropped
# (incl. the sell-through, avg-sales-progress-rename, and oldest-models-tinted-by-
# brand cases at 1.0); distinct pairs — e.g. "Closing Balance" vs "Age in
# Inventory" by the same products (0.50), inventory-cost-AND-stock-quantity vs
# inventory-cost-only (0.75), and a bar vs a scatter on the same columns — kept.
import re as _re_sig

_DEDUP_JACCARD = 0.8
_SIG_FILE_RE = _re_sig.compile(r"\.(xlsx|xls|csv|json|parquet|tsv)$", _re_sig.IGNORECASE)
_SIG_AGGS = "sum|mean|median|count|nunique|min|max|std|var|size|first|last"
# CORE encodings define WHAT is measured against WHAT — they always count toward
# the analysis identity. Orientation is irrelevant because the signature is an
# unordered SET (a vertical and a horizontal bar of the same columns coincide).
_SIG_CORE_KW = ("x", "y", "values", "z", "dimensions", "path", "theta", "r",
                "parents", "names")
# DECORATIVE encodings (a secondary grouping/coloring). A `color=`/`line_group=`
# column only counts when it is a REAL analysis variable: an aggregation key, a
# renamed/derived measure, or a numeric color scale (color_continuous_*). A plain
# categorical color (e.g. coloring a top-N bar by brand) is cosmetic and ignored,
# so "same measure by same dimension, one tinted by an extra category" collapses.
_SIG_DECOR_KW = ("color", "line_group")
_SIG_CHART_KINDS = (
    "px.bar", "px.line", "px.scatter", "px.pie", "px.histogram", "px.box",
    "px.violin", "px.area", "px.treemap", "px.sunburst", "px.imshow",
    "px.density_heatmap", "px.density_contour", "px.funnel", "px.ecdf",
    "px.line_polar", "px.strip", "go.bar", "go.scatter", "go.pie",
    "go.histogram", "go.box", "go.violin", "go.heatmap", "go.scatterpolar",
    "go.waterfall", "go.funnel", "go.indicator",
)


def _sig_aliases(code: str) -> tuple:
    """Resolve renamed/derived columns back to their SOURCE columns. Returns
    (named, concat):
      - named  : full-substitution aliases — a renamed/aggregated output stands
        in for its source measure/dimension. Catches named-agg outputs
        `Agg=('source_col', 'mean')` AND `.rename(columns={'old':'new'})`.
      - concat : ordered components of a concatenated label
        `df['label'] = df['col_a'] + ' (' + df['col_b'] + ')'`. Whether the
        extra components count depends on context (see _chart_signature).
    """
    named: dict = {}
    concat: dict = {}
    for new, src in _re_sig.findall(
            r"(\w+)\s*=\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"](?:" + _SIG_AGGS + r")['\"]\s*\)", code):
        named.setdefault(new.strip().lower(), []).append(src.strip().lower())
    for body in _re_sig.findall(r"rename\(\s*columns\s*=\s*\{([^}]*)\}", code):
        for old, new in _re_sig.findall(r"['\"]([^'\"]+)['\"]\s*:\s*['\"]([^'\"]+)['\"]", body):
            named.setdefault(new.strip().lower(), []).append(old.strip().lower())
    for new, expr in _re_sig.findall(r"\[\s*['\"]([^'\"]+)['\"]\s*\]\s*=\s*([^\n]+)", code):
        srcs = _re_sig.findall(r"\[\s*['\"]([^'\"]+)['\"]\s*\]", expr)
        if len(srcs) >= 2 and "+" in expr:
            concat.setdefault(new.strip().lower(), []).extend(s.strip().lower() for s in srcs)
    return named, concat


def _chart_signature(code: str) -> set:
    """Analysis signature of a single plot block: the set of PLOTTED source
    columns (resolved through renames/aggregations/concat-labels) + groupby
    dimension keys + chart kind. NOT the title or styling. Two charts of the same
    measure by the same dimension yield near-identical signatures regardless of
    bar orientation, column renaming, or a cosmetic extra coloring; charts on a
    different metric, dimension, or chart type do not. Returns an empty set when
    no plotted columns can be identified (such a chart is never a duplicate)."""
    c = code or ""
    named, concat = _sig_aliases(c)
    has_numeric_color = bool(_re_sig.search(r"color_continuous_(scale|midpoint)", c))

    # groupby keys identify the real aggregation dimensions; resolve them first so
    # color / concat components can be classified against them below.
    gb_keys: set = set()
    for gb in _re_sig.findall(r"groupby\(\s*(\[[^\]]*\]|['\"][^'\"]+['\"])", c):
        for col in _re_sig.findall(r"['\"]([^'\"]+)['\"]", gb):
            gb_keys.add((col or "").strip().lower())

    def resolve(col: str) -> list:
        col = (col or "").strip().lower()
        if col in named:
            out: list = []
            for s in named[col]:
                out.extend(resolve(s) if s != col else [s])
            return out
        if col in concat:
            comps = concat[col]
            # a real multi-dimension label keeps every component that is an
            # aggregation key; a per-row label (no component is a groupby key)
            # is identified by its primary (first) component, the rest decoration.
            keep = [s for s in comps if s in gb_keys] or comps[:1]
            out = []
            for s in keep:
                out.extend(resolve(s) if s != col else [s])
            return out
        return [col]

    cols: set = set()

    def add(raw: str) -> None:
        for r in resolve(raw):
            if r and not _SIG_FILE_RE.search(r):
                cols.add(r)

    for k in gb_keys:
        add(k)

    # CORE encodings always count (scalar and list forms)
    for kw in _SIG_CORE_KW:
        for m in _re_sig.findall(rf"\b{kw}\s*=\s*['\"]([^'\"]+)['\"]", c):
            add(m)
        for lst in _re_sig.findall(rf"\b{kw}\s*=\s*\[([^\]]*)\]", c):
            for col in _re_sig.findall(r"['\"]([^'\"]+)['\"]", lst):
                add(col)
    # DECORATIVE encodings count only when the column is a real analysis variable
    for kw in _SIG_DECOR_KW:
        for m in _re_sig.findall(rf"\b{kw}\s*=\s*['\"]([^'\"]+)['\"]", c):
            for r in resolve(m):
                if not r or _SIG_FILE_RE.search(r):
                    continue
                if r in gb_keys or r in named or has_numeric_color:
                    cols.add(r)

    if not cols:
        # Nothing plotted could be identified — don't risk a false dedup.
        return set()

    toks = {("c", col) for col in cols}
    cl = c.lower()
    for k in _SIG_CHART_KINDS:
        if k in cl:
            toks.add(("k", k))
    return toks


def _signature_is_duplicate(sig: set, produced_sigs: list,
                            threshold: float = _DEDUP_JACCARD) -> bool:
    """True when `sig` is near-identical (Jaccard overlap >= threshold) to any
    signature already produced. Empty/unknown signatures are never treated as
    duplicates (keep them — better an extra chart than a dropped insight)."""
    if not sig:
        return False
    for prev in produced_sigs:
        if not prev:
            continue
        union = sig | prev
        if not union:
            continue
        if len(sig & prev) / len(union) >= threshold:
            return True
    return False


# ── Degenerate (zero-variance) chart detection (chat multi-plot) ──────────
# A chart whose plotted numeric measure is effectively constant (every bar the
# same height, a flat line, equal pie slices) conveys no insight. Detection is
# purely STATISTICAL and generic — it reads the rendered chart's numeric series
# from chart_data and compares their spread to their magnitude via a RELATIVE
# tolerance, so it works for any dataset/column/units with no hardcoded names or
# absolute thresholds. Charts with any real variation, purely categorical charts
# (no numeric series), single-point charts, and unavailable data are all kept.
_DEGENERATE_REL_TOL = 1e-6


def _series_is_flat(nums: list) -> bool:
    """True when a numeric series of >=2 points is effectively constant: its
    relative spread (range / magnitude) is within tolerance. All-equal (incl.
    all-zero) -> True; any meaningful variation -> False."""
    if len(nums) < 2:
        return False
    lo, hi = min(nums), max(nums)
    spread = hi - lo
    scale = max(abs(hi), abs(lo))
    rel = (spread / scale) if scale > 0 else spread
    return rel <= _DEGENERATE_REL_TOL


def _chart_is_degenerate(chart_data) -> bool:
    """True only when the chart has >=1 numeric plotted series AND every numeric
    series is effectively constant (see _series_is_flat) — i.e. the whole chart
    is flat and uninformative. Conservative by design: if any numeric series
    varies, or there is no numeric series (categorical/identifier chart), or the
    data is unavailable/odd-shaped, returns False (keep the chart). Never raises
    (Article IV)."""
    try:
        if not isinstance(chart_data, dict):
            return False
        tables = chart_data.get("tables")
        if isinstance(tables, list) and tables:
            row_sets = [t.get("rows") or [] for t in tables]
        else:
            row_sets = [chart_data.get("rows") or []]
        saw_numeric = False
        for rows in row_sets:
            if not rows:
                continue
            for col in list(rows[0].keys()):
                vals = [r.get(col) for r in rows]
                non_null = [v for v in vals if v is not None]
                nums = [v for v in non_null
                        if isinstance(v, (int, float)) and not isinstance(v, bool)]
                # treat a column as a numeric series only when (nearly) all its
                # non-null values are numbers and there are >=2 of them
                if len(nums) < 2 or len(nums) < len(non_null):
                    continue
                saw_numeric = True
                if not _series_is_flat([float(v) for v in nums]):
                    return False  # a real-varying series -> chart is informative
        return saw_numeric
    except Exception:
        return False


# ── Data-level near-duplicate dedup (chat multi-plot, SECOND net) ─────────
# `_chart_signature` reads the generated CODE and so cannot resolve columns
# renamed positionally (`df.columns = [...]`) instead of via `.rename(...)`.
# This net compares the chart's actual RENDERED values (the same chart_data we
# already extract), keyed on the VALUES not the column header text — so two
# charts with identical numbers under different labels (e.g. a renamed measure)
# collapse, while a different dimension, a different set of values, or a real
# extra series (dual-axis 2nd measure) stays distinct. Conservative: it acts
# ONLY on charts with a clear categorical-dimension structure and never drops
# when the data is missing/ambiguous (caller falls back to the code signature).
_DATA_DEDUP_CONTAIN = 0.9   # share of the SMALLER chart's tokens that must match
_DATA_SIG_MIN_KEYS = 2      # need >=2 distinct dimension labels to dedup safely
_DATA_SIZE_RATIO = 3.0      # only dedup comparably-sized charts (larger/smaller);
                            # a tiny rollup contained in a much larger breakdown
                            # is NOT treated as a duplicate (conservative)


def _round_sig(v, sig: int = 4):
    """Round to `sig` significant figures so tiny recompute/float differences
    don't defeat value matching. None for non-finite/non-numeric."""
    try:
        v = float(v)
    except Exception:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    if v == 0:
        return 0.0
    import math
    try:
        d = sig - 1 - int(math.floor(math.log10(abs(v))))
        return round(v, d)
    except Exception:
        return None


def _chart_data_signature(chart_data):
    """Value-keyed signature of a chart's RENDERED data for the data-level dedup.
    Maps each dimension-label key (the row's non-numeric cell values) to the SET
    of rounded numeric values plotted for it, merged across subplots/tables — so a
    real dual-axis 2nd series makes a key's value-set differ from a single-series
    chart, keeping them distinct. Column header TEXT is ignored entirely. Returns
    a frozenset of (key, frozenset(values)) tokens, or None when the data is
    missing or lacks a categorical-dimension structure (>= _DATA_SIG_MIN_KEYS
    keys) — in which case the caller relies on the code signature only. Never
    raises (Article IV)."""
    try:
        if not isinstance(chart_data, dict):
            return None
        tables = chart_data.get("tables")
        if isinstance(tables, list) and tables:
            row_sets = [t.get("rows") or [] for t in tables]
        else:
            row_sets = [chart_data.get("rows") or []]
        rowmap: dict = {}
        for rows in row_sets:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                labels = []
                vals = set()
                for v in row.values():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        rv = _round_sig(v)
                        if rv is not None:
                            vals.add(rv)
                    elif v is not None:
                        s = str(v).strip().lower()
                        if s:
                            labels.append(s)
                if not labels and not vals:
                    continue
                # de-duplicate identical label strings within a row: a chart that
                # colors by its own dimension makes _plotly_traces_to_table add a
                # "Series" column equal to the category, which would otherwise
                # carry the same label twice and never match the un-colored twin.
                rowmap.setdefault(tuple(sorted(set(labels))), set()).update(vals)
        keyed = {k: v for k, v in rowmap.items() if v}
        if len(keyed) < _DATA_SIG_MIN_KEYS:
            return None
        return frozenset((k, frozenset(v)) for k, v in keyed.items())
    except Exception:
        return None


def _data_signature_is_duplicate(sig, produced: list,
                                 contain: float = _DATA_DEDUP_CONTAIN,
                                 max_ratio: float = _DATA_SIZE_RATIO) -> bool:
    """True when `sig` shares >= `contain` of the SMALLER chart's tokens with an
    already-produced data signature AND the two charts are comparable in size
    (larger/smaller token count <= max_ratio). Containment (not Jaccard) catches a
    Top-10 view contained in a Top-15 view of the same data; the size-ratio bound
    keeps a small rollup that is merely a fragment of a much larger breakdown
    distinct. Requires >= 2 shared tokens so a single coincidental value never
    triggers a drop. Empty/None sigs never match (keep the chart)."""
    if not sig:
        return False
    for prev in produced:
        if not prev:
            continue
        inter = len(sig & prev)
        if inter < 2:
            continue
        smaller = min(len(sig), len(prev))
        larger = max(len(sig), len(prev))
        if smaller and larger / smaller <= max_ratio and inter / smaller >= contain:
            return True
    return False


def _has_hidden_color_measure(code: str) -> bool:
    """True when the chart encodes a 2nd numeric MEASURE via a continuous color
    scale on a column that is NOT already on an axis (x/y/values). chart_data
    captures only x/y, so such a measure is invisible to the data signature —
    exclude these charts from the data-level dedup so a genuinely richer
    dual-measure chart is never collapsed onto a single-measure one. (A
    continuous color on the SAME column already plotted is just decorative and
    does NOT trigger this.) Structural/generic; never raises (Article IV)."""
    try:
        c = code or ""
        if not _re_sig.search(r"color_continuous_(scale|midpoint)", c):
            return False
        color_cols = _re_sig.findall(r"\bcolor\s*=\s*['\"]([^'\"]+)['\"]", c)
        if not color_cols:
            return False
        axis_cols = set()
        for kw in ("x", "y", "values"):
            axis_cols.update(m.strip().lower()
                             for m in _re_sig.findall(rf"\b{kw}\s*=\s*['\"]([^'\"]+)['\"]", c))
        return any(cc.strip().lower() not in axis_cols for cc in color_cols)
    except Exception:
        return False


def _split_code_on_delimiter(code: str) -> list[str]:
    """Split regenerated code on ###NEXT_PLOT### into standalone blocks (strip
    code fences if present). Returns [] when there is no delimiter."""
    if not code or "###NEXT_PLOT###" not in code:
        return []
    import re as _re
    out: list[str] = []
    for seg in code.split("###NEXT_PLOT###"):
        seg = seg.strip()
        m = _re.search(r"```(?:plot_code|python)?\s*([\s\S]*?)```", seg, _re.IGNORECASE)
        if m:
            seg = m.group(1).strip()
        if seg:
            out.append(seg)
    return out


def _extract_multi_plot_blocks(raw_text: str) -> list[str]:
    """Verbatim port of global agent._extract_multi_plot_blocks. Splits the
    planner output by ###NEXT_PLOT### and pulls one plot_code/python block
    out of each segment. Returns [] if no delimiter is present (single-plot)."""
    if not raw_text or "###NEXT_PLOT###" not in raw_text:
        return []
    import re as _re
    blocks: list[str] = []
    for seg in raw_text.split("###NEXT_PLOT###"):
        seg = seg.strip()
        if not seg:
            continue
        m = _re.search(r"```plot_code\s*([\s\S]*?)```", seg, _re.IGNORECASE)
        if not m:
            m = _re.search(r"```(?:python)?\s*([\s\S]*?)```", seg, _re.IGNORECASE)
        if m:
            code = m.group(1).strip()
            if code:
                blocks.append(code)
    return blocks


_CHART_MARKERS = ("plt.", "px.", "go.", "sns.", "plotly", "matplotlib", "seaborn",
                  "fig =", "fig=", ".figure(")


def _looks_like_table_block(code: str) -> bool:
    """A ###NEXT_PLOT### segment that assigns RESULT and never touches a
    charting API is a TABLE block (the planner's dashboard/KPI pattern mixes a
    RESULT block with plot blocks). Previously such blocks were fed to the
    chart renderer, always failed, and the table silently vanished from the
    answer."""
    if "RESULT" not in code:
        return False
    return not any(m in code for m in _CHART_MARKERS)


_MAX_MIXED_TABLE_BLOCKS = 4


def run_chat_multi_plot(
    sid: str,
    dfs: dict[str, pd.DataFrame],
    schema_docs: dict,
    question: str,
    history_rows: list,
    user_email: str,
    common_fields: list | None = None,
    dataset_profile: dict | None = None,
):
    """Generator port of global `agent.run_chat_multi_plot`. Yields:
      - {"partial": True,  "answer", "image_base64", "chart_n", "chart_total", "usage", "code"}
      - {"partial": False, "done": True, "combined_answer", "combined_codes", "total_usage"
         [, "tables", "table_codes", "table_result_keys"]}
    For non-multi-plot responses, yields a single {"single_response": True, "result": {...}} item.
    """
    df_names = list(dfs.keys())
    df_columns = {name: list(df.columns) for name, df in dfs.items()}
    schema_str = build_schema_text(
        schema_docs or {}, dfs, common_fields,
        other_tables=_other_registered_tables(schema_docs, dfs, user_email))

    # 1) Plan once — reuse the raw_text for single-vs-multi detection
    plan_out = brain_client.plan(
        sid=sid, question=question, schema_text=schema_str,
        df_names=df_names, history_rows=history_rows,
        common_fields=common_fields, user_email=user_email,
        dataset_profile=dataset_profile,
    )
    raw_text = plan_out.get("raw_text") or ""
    plan_usage = plan_out.get("usage") or {}

    plot_blocks = _extract_multi_plot_blocks(raw_text)

    if not plot_blocks:
        kind = plan_out.get("kind")
        code = plan_out.get("code") or ""
        context_decision = plan_out.get("context_decision") or {}

        # A single PLOT_CODE block is routed through the per-chart pipeline below
        # too, so the one-figure-per-chart guard applies (a lone block that
        # renders >1 axes is regenerated / split into separate charts). Non-plot
        # responses (tables, scalars, greetings, clarifications) keep the
        # single_response shape.
        if kind == "PLOT_CODE" and code.strip():
            plot_blocks = [code]
        else:
            result = _run_single_from_plan(
                sid=sid, dfs=dfs, schema_docs=schema_docs, schema_str=schema_str,
                df_columns=df_columns, df_names=df_names,
                question=question, history_rows=history_rows, user_email=user_email,
                kind=kind, code=code, usage=plan_usage,
                context_decision=context_decision,
                dataset_profile=dataset_profile,
            )
            yield {"single_response": True, "result": result}
            return

    # Mixed dashboard answers (KPI table + charts): pull TABLE blocks out of
    # the worklist — they execute via safe_execute and land on the done event
    # as `tables` instead of dying in the chart renderer (which silently
    # dropped the table part of every mixed answer before).
    table_blocks = [b for b in plot_blocks if _looks_like_table_block(b)]
    if table_blocks:
        plot_blocks = [b for b in plot_blocks if not _looks_like_table_block(b)]
        table_blocks = table_blocks[:_MAX_MIXED_TABLE_BLOCKS]
        log_with_sid(sid, "info",
                     f"MIXED_TABLE_BLOCKS extracted={len(table_blocks)} charts={len(plot_blocks)}")

    # Cap CANDIDATE blocks to bound render cost; the final EMITTED count is
    # capped at _MAX_MULTI_PLOTS *after* dedup (see the produced>=cap breaks
    # below), so the charts kept are the distinct ones rather than the first N.
    if len(plot_blocks) > _MAX_MULTI_PLOT_CANDIDATES:
        log_with_sid(sid, "warning",
                     f"MULTI_PLOT_CANDIDATES_CAPPED from={len(plot_blocks)} to={_MAX_MULTI_PLOT_CANDIDATES}")
        plot_blocks = plot_blocks[:_MAX_MULTI_PLOT_CANDIDATES]

    total_usage = dict(plan_usage)
    combined_answers: list[str] = []
    combined_codes: list[str] = []
    # Analysis signatures of charts already EMITTED this response — used to drop
    # near-duplicate charts (same measure by the same dimension) the planner or a
    # regen path may produce. Mirrors the deck-side dedup (llm.py).
    produced_sigs: list = []
    # Value-keyed signatures of EMITTED charts' rendered data — the second dedup
    # net that catches duplicates the code parser can't resolve (positional
    # column renames). See _chart_data_signature.
    produced_data_sigs: list = []

    # Worklist so a single block can EXPAND into several (when a multi-axes block
    # is regenerated as separate ###NEXT_PLOT### blocks). Each item: (code, n_ma_retry).
    from collections import deque
    work = deque((blk, 0, 0) for blk in plot_blocks)
    produced = 0
    log_with_sid(sid, "info", f"MULTI_PLOT_START blocks={len(work)}")

    while work:
        # Emit cap AFTER dedup: stop once _MAX_MULTI_PLOTS distinct charts have
        # been produced (dedup already ran on the earlier ones, so the kept set
        # is the distinct one). Prefer fewer, genuinely-distinct insights.
        if produced >= _MAX_MULTI_PLOTS:
            log_with_sid(sid, "info", f"MULTI_PLOT_EMIT_CAP reached={produced}")
            break
        code, ma_retry, np_retry = work.popleft()
        try:
            ch = hashlib.sha256(code.encode("utf-8", errors="ignore")).hexdigest()[:settings.CODE_HASH_LEN]
            log_with_sid(sid, "info", f"MULTI_PLOT_EXEC produced={produced} remaining={len(work)} code_hash={ch}")
        except Exception:
            pass

        plot_out = render_plot_safe(code, dfs, sid)

        # Standard execution-error retry (skip when it's the multi-axes signal).
        retry_count = 0
        while plot_out.get("error") and not plot_out.get("multi_axes") and retry_count < 3:
            error_msg = plot_out.get("error", "Unknown")
            log_with_sid(sid, "warning", f"MULTI_PLOT_ERROR attempt={retry_count+1}: {error_msg[:200]}")
            use_pro = retry_count >= 1
            use_search = retry_count >= 1
            retry_out = brain_client.retry(
                sid=sid, question=question, schema_text=schema_str,
                df_names=df_names, df_columns=df_columns, history_rows=history_rows,
                error_msg=error_msg, failed_code=code,
                use_pro=use_pro, use_search=use_search, user_email=user_email,
                dataset_profile=dataset_profile,
            )
            new_code = retry_out.get("code") or ""
            new_kind = retry_out.get("kind")
            total_usage = _sum_usage(total_usage, retry_out.get("usage") or {})
            retry_count += 1
            # A retry returning prose (NO_CODE) or no code is a FAILED attempt,
            # not a reason to abort the block: keep retrying with escalation.
            if not new_code:
                continue
            if new_kind == "PLOT_CODE":
                code = new_code
                plot_out = render_plot_safe(code, dfs, sid)
                continue
            if new_kind == "PYTHON":
                # The brain rewrote it as analysis code. Execute it; accept the
                # chart ONLY if it produced an image, else treat as a failed
                # attempt and keep retrying.
                py_out = safe_execute(new_code, dfs, sid)
                if py_out.get("image_base64"):
                    code = new_code
                    plot_out = py_out
                    break
                continue
            # Any other kind (CLARIFICATION, ANSWER, MISSING_DATA, …) → failed
            # attempt; keep retrying.
            continue

        # ── One-figure-per-chart enforcement ──
        if plot_out.get("multi_axes"):
            # Preferred: ask the brain to regenerate as SEPARATE single-figure blocks.
            if ma_retry < _MAX_MULTI_AXES_RETRY:
                log_with_sid(sid, "info", f"MULTI_AXES_REGEN attempt={ma_retry+1}")
                retry_out = brain_client.retry(
                    sid=sid, question=question, schema_text=schema_str,
                    df_names=df_names, df_columns=df_columns, history_rows=history_rows,
                    error_msg=plot_out.get("error", ""), failed_code=code,
                    use_pro=True, use_search=False, user_email=user_email,
                    dataset_profile=dataset_profile,
                )
                total_usage = _sum_usage(total_usage, retry_out.get("usage") or {})
                new_code = retry_out.get("code") or ""
                if new_code:
                    blocks = _split_code_on_delimiter(new_code) or [new_code]
                    for b in blocks:
                        work.append((b, ma_retry + 1, np_retry))
                    continue
            # Fallback: split the rendered figure into one image (+ data) per axis.
            split_out = render_plot_safe(code, dfs, sid, split_multi_axes=True)
            sub = [c for c in (split_out.get("multi_charts") or []) if c.get("image")]
            if sub:
                # Dedup: a split group is ONE analysis (all sub-charts share this
                # block's code) — drop the whole group if near-identical to an
                # already-emitted chart. The sub-charts within it are distinct
                # axes and are NOT deduped against each other.
                sig = _chart_signature(code)
                if _signature_is_duplicate(sig, produced_sigs):
                    log_with_sid(sid, "info", "MULTI_PLOT_DEDUP_SKIP split")
                    continue
                produced_sigs.append(sig)
                d = _describe_with_backstop(
                    sid, question, code, user_email,
                    chart_data_list=[c.get("chart_data") for c in sub],
                    dataset_profile=dataset_profile)
                base_txt = d.get("text") or ""
                total_usage = _sum_usage(total_usage, d.get("usage") or {})
                log_with_sid(sid, "info", f"MULTI_AXES_SPLIT_EMIT charts={len(sub)}")
                for i, cobj in enumerate(sub):
                    produced += 1
                    txt = base_txt if i == 0 else (cobj.get("title") or "")
                    combined_answers.append(txt)
                    if i == 0:
                        combined_codes.append(code)
                    sub_data_sig = (None if _has_hidden_color_measure(code)
                                    else _chart_data_signature(cobj.get("chart_data")))
                    if sub_data_sig is not None:
                        produced_data_sigs.append(sub_data_sig)
                    yield {
                        "partial": True, "answer": txt, "image_base64": cobj.get("image"),
                        "chart_n": produced, "chart_total": produced + len(work),
                        "usage": dict(total_usage), "code": code,
                        "chart_data": cobj.get("chart_data"),
                    }
                    # A split block can expand into several axes — respect the
                    # emit cap within the group too.
                    if produced >= _MAX_MULTI_PLOTS:
                        break
            continue

        if plot_out.get("error"):
            log_with_sid(sid, "warning", "MULTI_PLOT_SKIP after retries")
            continue

        # ── Interactivity enforcement (standard charts must be Plotly) ──
        # The render succeeded but came back static matplotlib/seaborn for a
        # STANDARD chart — ask the brain once for an interactive Plotly rewrite.
        if (np_retry < _MAX_PLOTLY_REGEN and not plot_out.get("is_plotly")
                and not plot_out.get("multi_charts")
                and _is_noninteractive_standard_chart(code)):
            log_with_sid(sid, "info", f"NONINTERACTIVE_REGEN attempt={np_retry+1}")
            retry_out = brain_client.retry(
                sid=sid, question=question, schema_text=schema_str,
                df_names=df_names, df_columns=df_columns, history_rows=history_rows,
                error_msg=_PLOTLY_REGEN_INSTRUCTION, failed_code=code,
                use_pro=False, use_search=False, user_email=user_email,
                dataset_profile=dataset_profile,
            )
            total_usage = _sum_usage(total_usage, retry_out.get("usage") or {})
            new_code = retry_out.get("code") or ""
            if new_code and retry_out.get("kind") == "PLOT_CODE":
                # Reprocess this item next (preserve order); np_retry+1 caps it.
                work.appendleft((new_code, ma_retry, np_retry + 1))
                continue

        # ── Degenerate (zero-variance) chart skip ──
        # Drop a chart whose every numeric series is effectively constant (no
        # insight). Purely statistical (see _chart_is_degenerate). Safety: never
        # skip when it would leave the response with zero charts — only skip if at
        # least one chart was already produced OR another block remains to try; the
        # sole/last candidate is always kept. (run_chat single-chart path is
        # untouched — this guard lives only in the multi-plot loop.)
        if ((produced >= 1 or len(work) >= 1)
                and _chart_is_degenerate(plot_out.get("chart_data"))):
            log_with_sid(sid, "info", "DEGENERATE_CHART_SKIP")
            continue

        # ── Near-duplicate dedup ──
        # Skip a chart whose analysis is near-identical to one already emitted in
        # THIS response (planner re-wording or a regen re-introducing the same
        # measure×dimension). Applies to initial AND regen-added blocks (every
        # block reaches this point before emission).
        sig = _chart_signature(code)
        if _signature_is_duplicate(sig, produced_sigs):
            log_with_sid(sid, "info", "MULTI_PLOT_DEDUP_SKIP")
            continue
        # ── Data-level dedup (second net) ──
        # Catches duplicates the code parser misses (e.g. positional
        # `df.columns=[...]` renames) by comparing the actual rendered values.
        # Only fires when chart_data yields a usable value-keyed signature; never
        # drops on missing/ambiguous data (falls back to the code signature).
        data_sig = (None if _has_hidden_color_measure(code)
                    else _chart_data_signature(plot_out.get("chart_data")))
        if data_sig is not None and _data_signature_is_duplicate(data_sig, produced_data_sigs):
            log_with_sid(sid, "info", "MULTI_PLOT_DEDUP_SKIP_DATA")
            continue
        produced_sigs.append(sig)
        if data_sig is not None:
            produced_data_sigs.append(data_sig)

        # Determine the renderable chart from either executor's output shape
        # (render_plot_safe → image/plotly_html; safe_execute → image_base64).
        if plot_out.get("image_base64"):
            img_b64 = plot_out.get("image_base64")
        elif plot_out.get("is_plotly"):
            img_b64 = plot_out.get("plotly_html")
        else:
            img_b64 = plot_out.get("image")
        # Describe from code (brain — no data leaves)
        d = _describe_with_backstop(sid, question, code, user_email,
                                    chart_data=plot_out.get("chart_data"),
                                    dataset_profile=dataset_profile)
        text = d.get("text") or ""
        total_usage = _sum_usage(total_usage, d.get("usage") or {})

        produced += 1
        combined_answers.append(text)
        combined_codes.append(code)
        log_with_sid(sid, "info", f"PLOT_RENDERED chart={produced} plotly={plot_out.get('is_plotly')}")

        yield {
            "partial": True, "answer": text, "image_base64": img_b64,
            "chart_n": produced, "chart_total": produced + len(work),
            "usage": dict(total_usage), "code": code,
            "chart_data": plot_out.get("chart_data"),
        }

    # Execute the extracted TABLE blocks (mixed dashboard answers). Each block
    # yields one table (or several, when its RESULT was a dict of DataFrames);
    # they ride on the done event with their block code + dict entry so the
    # per-table Download Excel / Show full table re-execute correctly.
    combined_tables: list = []
    table_codes: list = []
    table_result_keys: list = []
    for tb in table_blocks if table_blocks else []:
        try:
            exec_out = safe_execute(tb, dfs, sid)
            if exec_out.get("error"):
                log_with_sid(sid, "warning",
                             f"MIXED_TABLE_BLOCK_ERROR: {str(exec_out['error'])[:200]}")
                continue
            result_obj = exec_out.get("result")
            t = _build_table_from_result(result_obj)
            if t and (int(t.get("total_rows", 0)) <= 0 or not (t.get("rows") or [])):
                t = None
            ts = None
            if t is None:
                ts = _build_tables_from_result(result_obj)
            emitted = ([t] if t else []) + (ts or [])
            if not emitted:
                continue
            d = _describe_with_backstop(sid, question, tb, user_email,
                                        result_obj=result_obj,
                                        dataset_profile=dataset_profile)
            total_usage = _sum_usage(total_usage, d.get("usage") or {})
            if d.get("text"):
                combined_answers.append(d["text"])
            for entry in emitted:
                combined_tables.append(entry)
                table_codes.append(tb)
                table_result_keys.append(entry.get("title") if ts else None)
            log_with_sid(sid, "info", f"MIXED_TABLE_EMIT tables={len(emitted)}")
        except Exception as e:
            log_with_sid(sid, "warning", f"MIXED_TABLE_BLOCK_FAILED: {e}")

    # If every block failed all its retries we rendered ZERO charts — surface a
    # clear failure rather than the misleading bare "Analysis complete." (a run
    # that still emitted tables is not a failure).
    if combined_answers:
        combined_text = "\n\n".join([a for a in combined_answers if a])
    elif combined_tables:
        combined_text = "Analysis complete."
    else:
        combined_text = "Something went wrong with this analysis. Please try again."
    log_with_sid(sid, "info", f"MULTI_PLOT_DONE rendered={produced}")
    done_event = {
        "partial": False, "done": True,
        "combined_answer": combined_text,
        "combined_codes": combined_codes,
        "total_usage": total_usage,
    }
    if combined_tables:
        done_event["tables"] = combined_tables
        done_event["table_codes"] = table_codes
        done_event["table_result_keys"] = table_result_keys
    yield done_event


def _run_single_from_plan(*, sid, dfs, schema_docs, schema_str, df_columns, df_names,
                           question, history_rows, user_email,
                           kind, code, usage, context_decision,
                           dataset_profile: dict | None = None) -> dict:
    """Execute one already-planned response (kind/code from a prior /v1/plan)
    locally, reusing the same retry + describe/summarize flow as run_chat.

    `dataset_profile` feeds both the retry planner and the result backstop. It
    was referenced by the retry call below without being a parameter (Prompt 13
    oversight) — a failing first execution raised NameError instead of retrying.
    """
    if kind in ("CLARIFICATION", "ANSWER", "MISSING_DATA"):
        return {"text": code, "image_base64": None, "table": None, "code": None, "usage": usage}

    if kind == "NO_CODE":
        if context_decision.get("is_greeting"):
            g = brain_client.greeting(sid, question, df_names, user_email=user_email)
            return {"text": g.get("text", ""), "image_base64": None, "table": None,
                    "code": None, "usage": _sum_usage(usage, g.get("usage", {}))}
        return {"text": "I couldn't generate analysis code for this question. Try rephrasing.",
                "image_base64": None, "table": None, "code": None, "usage": usage}

    executor = render_plot_safe if kind == "PLOT_CODE" else safe_execute
    exec_out = executor(code, dfs, sid)

    retry_count = 0
    max_retries = 3  # up to 3 retry attempts; escalate to pro/search from the 2nd
    while exec_out.get("error") and retry_count < max_retries:
        error_msg = exec_out.get("error", "Unknown")
        log_with_sid(sid, "warning", f"EXEC_ERROR attempt {retry_count+1}: {error_msg[:200]}")
        use_pro = retry_count >= 1
        use_search = retry_count >= 1
        retry_out = brain_client.retry(
            sid=sid, question=question, schema_text=schema_str,
            df_names=df_names, df_columns=df_columns,
            history_rows=history_rows,
            error_msg=error_msg, failed_code=code,
            use_pro=use_pro, use_search=use_search,
            user_email=user_email,
            dataset_profile=dataset_profile,
        )
        new_code = retry_out.get("code") or ""
        new_kind = retry_out.get("kind")
        usage = _sum_usage(usage, retry_out.get("usage") or {})
        retry_count += 1
        # A retry that returns prose (NO_CODE/CLARIFICATION/ANSWER/MISSING_DATA)
        # or no code is a FAILED attempt, not a reason to abort: keep retrying
        # with escalation. Prose text must never reach an executor.
        if not new_code or new_kind in ("NO_CODE", "CLARIFICATION", "ANSWER", "MISSING_DATA"):
            continue
        code = new_code
        kind = new_kind
        executor = render_plot_safe if kind == "PLOT_CODE" else safe_execute
        exec_out = executor(code, dfs, sid)

    if exec_out.get("error"):
        return {
            "text": "I couldn't run this analysis with your current data. Try rephrasing.",
            "image_base64": None, "table": None, "code": code, "usage": usage,
        }

    if kind == "PLOT_CODE":
        img_b64 = exec_out.get("plotly_html") if exec_out.get("is_plotly") else exec_out.get("image")
        d = _describe_with_backstop(sid, question, code, user_email,
                                    chart_data=exec_out.get("chart_data"),
                                    dataset_profile=dataset_profile)
        return {"text": d.get("text", ""), "image_base64": img_b64, "table": None,
                "code": code, "chart_data": exec_out.get("chart_data"),
                "usage": _sum_usage(usage, d.get("usage") or {})}

    # PYTHON
    result_obj = exec_out.get("result")
    table = _build_table_from_result(result_obj)
    if table and (int(table.get("total_rows", 0)) <= 0 or not (table.get("rows") or [])):
        table = None
    table, tables = _tables_from_python_result(result_obj, table)
    img_b64 = exec_out.get("image_base64")

    if img_b64:
        d = _describe_with_backstop(sid, question, code, user_email,
                                    result_obj=result_obj, dataset_profile=dataset_profile)
        return {"text": d.get("text", ""), "image_base64": img_b64, "table": table,
                "tables": tables, "code": code, "usage": _sum_usage(usage, d.get("usage") or {})}
    if table or tables:
        d = _describe_with_backstop(sid, question, code, user_email,
                                    result_obj=result_obj, dataset_profile=dataset_profile)
        return {"text": d.get("text", ""), "image_base64": None, "table": table,
                "tables": tables, "code": code, "usage": _sum_usage(usage, d.get("usage") or {})}

    preview = exec_out.get("preview")
    safe_p = _safe_preview(preview)
    s = brain_client.summarize(
        sid=sid, question=question, schema_text=schema_str,
        history_rows=history_rows, preview=safe_p,
        context_decision=context_decision, user_email=user_email,
    )
    return {"text": s.get("text", ""), "image_base64": None, "table": None,
            "code": code, "usage": _sum_usage(usage, s.get("usage") or {})}
