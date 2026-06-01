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
import time as _time
from typing import Any, Optional

import pandas as pd

import brain_client
from code_exec import safe_execute
from plot_utils import render_plot_safe
from logger_utils import log_with_sid
from schema_builder import schema_text as build_schema_text
from settings import settings


def _build_table_from_result(result_obj) -> Optional[dict]:
    """Tiny port of the B2C `_build_table_from_result` for DataFrame / Series.

    Returns {columns, rows, total_rows} or None.
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
        rows = df.head(50).to_dict(orient="records")
        return {
            "columns": list(df.columns),
            "rows": rows,
            "total_rows": int(len(df)),
        }
    except Exception:
        return None


def _safe_preview(preview) -> Any:
    """Mirror the B2C 'safe_preview' guard: only scalars / simple dicts may
    be forwarded to the LLM. NEVER send DataFrame rows."""
    if preview is None:
        return None
    if isinstance(preview, (str, int, float, bool)):
        return preview
    if isinstance(preview, dict) and not any(isinstance(v, (list, dict)) for v in preview.values()):
        return preview
    return None


def run_chat(
    sid: str,
    dfs: dict[str, pd.DataFrame],
    schema_docs: dict,
    question: str,
    history_rows: list,
    user_email: str,
    common_fields: list | None = None,
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
    schema_str = build_schema_text(schema_docs or {}, dfs, common_fields)

    # 1) Plan
    plan_out = brain_client.plan(
        sid=sid,
        question=question,
        schema_text=schema_str,
        df_names=df_names,
        history_rows=history_rows,
        common_fields=common_fields,
        user_email=user_email,
    )
    kind = plan_out.get("kind")
    code = plan_out.get("code") or ""
    usage = plan_out.get("usage") or {}
    context_decision = plan_out.get("context_decision") or {}
    log_with_sid(sid, "info", f"PLAN kind={kind} model={plan_out.get('model_used')}")

    # Clarification — return as text
    if kind == "CLARIFICATION":
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
    max_retries = 2  # 0=original, 1=retry, 2=pro
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
        )
        new_code = retry_out.get("code") or ""
        new_kind = retry_out.get("kind")
        usage = _sum_usage(usage, retry_out.get("usage") or {})
        if not new_code or new_kind == "NO_CODE":
            break
        code = new_code
        kind = new_kind
        executor = render_plot_safe if kind == "PLOT_CODE" else safe_execute
        exec_out = executor(code, dfs, sid)
        retry_count += 1

    if exec_out.get("error"):
        log_with_sid(sid, "error", f"EXEC_FINAL_ERROR after {retry_count+1} attempts")
        return {
            "text": "I couldn't run this analysis with your current data. Try rephrasing or simplifying the request.",
            "image_base64": None, "table": None, "code": code, "usage": usage,
        }

    # 3) Collect outputs
    if kind == "PLOT_CODE":
        if exec_out.get("is_plotly"):
            img_b64 = exec_out.get("plotly_html")
        else:
            img_b64 = exec_out.get("image")
        # Describe from code (brain — no data leaves)
        d = brain_client.describe(sid=sid, question=question, code=code, user_email=user_email)
        return {
            "text": d.get("text", ""),
            "image_base64": img_b64,
            "table": None,
            "code": code,
            "usage": _sum_usage(usage, d.get("usage") or {}),
        }

    # PYTHON branch
    result_obj = exec_out.get("result")
    preview = exec_out.get("preview")
    table = _build_table_from_result(result_obj)
    if table and (int(table.get("total_rows", 0)) <= 0 or not (table.get("rows") or [])):
        table = None
    img_b64 = exec_out.get("image_base64")

    if img_b64:
        d = brain_client.describe(sid=sid, question=question, code=code, user_email=user_email)
        return {"text": d.get("text", ""), "image_base64": img_b64, "table": table, "code": code, "usage": _sum_usage(usage, d.get("usage") or {})}

    if table:
        d = brain_client.describe(sid=sid, question=question, code=code, user_email=user_email)
        return {"text": d.get("text", ""), "image_base64": None, "table": table, "code": code, "usage": _sum_usage(usage, d.get("usage") or {})}

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


# ---------------------------------------------------------------------------
# Multi-chart streaming
# ---------------------------------------------------------------------------
_MAX_MULTI_PLOTS = 6


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


def run_chat_multi_plot(
    sid: str,
    dfs: dict[str, pd.DataFrame],
    schema_docs: dict,
    question: str,
    history_rows: list,
    user_email: str,
    common_fields: list | None = None,
):
    """Generator port of global `agent.run_chat_multi_plot`. Yields:
      - {"partial": True,  "answer", "image_base64", "chart_n", "chart_total", "usage", "code"}
      - {"partial": False, "done": True, "combined_answer", "combined_codes", "total_usage"}
    For non-multi-plot responses, yields a single {"single_response": True, "result": {...}} item.
    """
    df_names = list(dfs.keys())
    df_columns = {name: list(df.columns) for name, df in dfs.items()}
    schema_str = build_schema_text(schema_docs or {}, dfs, common_fields)

    # 1) Plan once — reuse the raw_text for single-vs-multi detection
    plan_out = brain_client.plan(
        sid=sid, question=question, schema_text=schema_str,
        df_names=df_names, history_rows=history_rows,
        common_fields=common_fields, user_email=user_email,
    )
    raw_text = plan_out.get("raw_text") or ""
    plan_usage = plan_out.get("usage") or {}

    plot_blocks = _extract_multi_plot_blocks(raw_text)

    if not plot_blocks:
        # Single-plot path → delegate to run_chat, but reuse the already-fetched
        # plan output by short-circuiting the brain (the kind/code already
        # parsed in plan_out is what run_chat would re-derive).
        kind = plan_out.get("kind")
        code = plan_out.get("code") or ""
        context_decision = plan_out.get("context_decision") or {}

        result = _run_single_from_plan(
            sid=sid, dfs=dfs, schema_docs=schema_docs, schema_str=schema_str,
            df_columns=df_columns, df_names=df_names,
            question=question, history_rows=history_rows, user_email=user_email,
            kind=kind, code=code, usage=plan_usage,
            context_decision=context_decision,
        )
        yield {"single_response": True, "result": result}
        return

    # Cap at _MAX_MULTI_PLOTS (matches global)
    if len(plot_blocks) > _MAX_MULTI_PLOTS:
        log_with_sid(sid, "warning",
                     f"MULTI_PLOT_CAPPED from={len(plot_blocks)} to={_MAX_MULTI_PLOTS}")
        plot_blocks = plot_blocks[:_MAX_MULTI_PLOTS]

    total_charts = len(plot_blocks)
    total_usage = dict(plan_usage)
    combined_answers: list[str] = []
    combined_codes: list[str] = []

    log_with_sid(sid, "info", f"MULTI_PLOT_START charts={total_charts}")

    for chart_n, code in enumerate(plot_blocks, start=1):
        try:
            ch = hashlib.sha256(code.encode("utf-8", errors="ignore")).hexdigest()[:settings.CODE_HASH_LEN]
            log_with_sid(sid, "info", f"MULTI_PLOT_EXEC chart={chart_n}/{total_charts} code_hash={ch}")
        except Exception:
            pass

        plot_out = render_plot_safe(code, dfs, sid)
        retry_count = 0
        max_retries = 2

        while plot_out.get("error") and retry_count < max_retries:
            error_msg = plot_out.get("error", "Unknown")
            log_with_sid(sid, "warning",
                         f"MULTI_PLOT_ERROR chart={chart_n} attempt={retry_count+1}: {error_msg[:200]}")
            use_pro = retry_count >= 1
            use_search = retry_count >= 1
            retry_out = brain_client.retry(
                sid=sid, question=question, schema_text=schema_str,
                df_names=df_names, df_columns=df_columns,
                history_rows=history_rows,
                error_msg=error_msg, failed_code=code,
                use_pro=use_pro, use_search=use_search,
                user_email=user_email,
            )
            new_code = retry_out.get("code") or ""
            new_kind = retry_out.get("kind")
            total_usage = _sum_usage(total_usage, retry_out.get("usage") or {})
            if not new_code or new_kind != "PLOT_CODE":
                break
            code = new_code
            plot_out = render_plot_safe(code, dfs, sid)
            retry_count += 1

        if plot_out.get("error"):
            log_with_sid(sid, "warning",
                         f"MULTI_PLOT_SKIP chart={chart_n} after {retry_count+1} attempts")
            continue

        img_b64 = plot_out.get("plotly_html") if plot_out.get("is_plotly") else plot_out.get("image")
        # Describe from code (brain — no data leaves)
        d = brain_client.describe(sid=sid, question=question, code=code, user_email=user_email)
        text = d.get("text") or ""
        total_usage = _sum_usage(total_usage, d.get("usage") or {})

        combined_answers.append(text)
        combined_codes.append(code)

        log_with_sid(sid, "info",
                     f"PLOT_RENDERED chart={chart_n}/{total_charts} plotly={plot_out.get('is_plotly')}")

        yield {
            "partial": True, "answer": text, "image_base64": img_b64,
            "chart_n": chart_n, "chart_total": total_charts,
            "usage": dict(total_usage), "code": code,
        }

    combined_text = "\n\n".join(combined_answers) if combined_answers else "Analysis complete."
    log_with_sid(sid, "info",
                 f"MULTI_PLOT_DONE rendered={len(combined_answers)}/{total_charts}")
    yield {
        "partial": False, "done": True,
        "combined_answer": combined_text,
        "combined_codes": combined_codes,
        "total_usage": total_usage,
    }


def _run_single_from_plan(*, sid, dfs, schema_docs, schema_str, df_columns, df_names,
                           question, history_rows, user_email,
                           kind, code, usage, context_decision) -> dict:
    """Execute one already-planned response (kind/code from a prior /v1/plan)
    locally, reusing the same retry + describe/summarize flow as run_chat."""
    if kind == "CLARIFICATION":
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
    max_retries = 2
    while exec_out.get("error") and retry_count < max_retries:
        error_msg = exec_out.get("error", "Unknown")
        log_with_sid(sid, "warning", f"EXEC_ERROR attempt {retry_count+1}: {error_msg[:200]}")
        use_pro = retry_count >= 1
        retry_out = brain_client.retry(
            sid=sid, question=question, schema_text=schema_str,
            df_names=df_names, df_columns=df_columns,
            history_rows=history_rows,
            error_msg=error_msg, failed_code=code,
            use_pro=use_pro, use_search=use_pro,
            user_email=user_email,
        )
        new_code = retry_out.get("code") or ""
        new_kind = retry_out.get("kind")
        usage = _sum_usage(usage, retry_out.get("usage") or {})
        if not new_code or new_kind == "NO_CODE":
            break
        code = new_code
        kind = new_kind
        executor = render_plot_safe if kind == "PLOT_CODE" else safe_execute
        exec_out = executor(code, dfs, sid)
        retry_count += 1

    if exec_out.get("error"):
        return {
            "text": "I couldn't run this analysis with your current data. Try rephrasing.",
            "image_base64": None, "table": None, "code": code, "usage": usage,
        }

    if kind == "PLOT_CODE":
        img_b64 = exec_out.get("plotly_html") if exec_out.get("is_plotly") else exec_out.get("image")
        d = brain_client.describe(sid=sid, question=question, code=code, user_email=user_email)
        return {"text": d.get("text", ""), "image_base64": img_b64, "table": None,
                "code": code, "usage": _sum_usage(usage, d.get("usage") or {})}

    # PYTHON
    result_obj = exec_out.get("result")
    table = _build_table_from_result(result_obj)
    if table and (int(table.get("total_rows", 0)) <= 0 or not (table.get("rows") or [])):
        table = None
    img_b64 = exec_out.get("image_base64")

    if img_b64:
        d = brain_client.describe(sid=sid, question=question, code=code, user_email=user_email)
        return {"text": d.get("text", ""), "image_base64": img_b64, "table": table,
                "code": code, "usage": _sum_usage(usage, d.get("usage") or {})}
    if table:
        d = brain_client.describe(sid=sid, question=question, code=code, user_email=user_email)
        return {"text": d.get("text", ""), "image_base64": None, "table": table,
                "code": code, "usage": _sum_usage(usage, d.get("usage") or {})}

    preview = exec_out.get("preview")
    safe_p = _safe_preview(preview)
    s = brain_client.summarize(
        sid=sid, question=question, schema_text=schema_str,
        history_rows=history_rows, preview=safe_p,
        context_decision=context_decision, user_email=user_email,
    )
    return {"text": s.get("text", ""), "image_base64": None, "table": None,
            "code": code, "usage": _sum_usage(usage, s.get("usage") or {})}
