"""Auto Analytics on the client — orchestrates a planner-driven multi-chart
PPTX, split correctly across brain/client per the enterprise architecture.

Flow:
  1. Read the chat's schema + dataframes locally.
  2. Call brain `/v1/auto_analytics_plan` with the schema_text only (NO row
     data) → get back 3-15 natural-language analytical instructions.
  3. For each instruction, run `run_chat_local.run_chat` LOCALLY against the
     raw dataframes (this calls brain `/v1/plan` for code generation but
     EXECUTES the code on the client). Run through a small bounded worker
     pool so a single auto-analysis job never starves the live chat path.
  4. Collected charts + answers become synthetic Q&A pairs.
  5. Call brain `/v1/report` (already implemented) with the no-values findings
     payload → narrative JSON.
  6. Render the PPTX LOCALLY via `routes.report._render_pptx` (reuses the same
     brand template the manual export uses) and persist to
     `chatdata/{chat_id}/auto_analysis.pptx`.

State lives in the chat's `meta.json` under the `auto_analysis` key. The job
runs in a daemon thread so it survives the client HTTP request returning.

ENTERPRISE NOTE: raw data values NEVER leave the client. Only the schema text
(column names + descriptions) and the no-values `findings` payload go to the
brain. Same architecture decision as global → enterprise split.
"""
from __future__ import annotations

import atexit
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import brain_client
import local_store
import run_chat_local
from brain_client import BrainError, TenantRevokedError
from logger_utils import log_with_sid


AUTO_ANALYTICS_CONCURRENCY = 4
# The brain planner (/v1/auto_analytics_plan) is the SINGLE source of truth for
# how many analyses/plots a job produces — it enforces the floor, diversity and
# the hard cap server-side. The client just executes whatever instruction list
# the brain returns, so there are deliberately no min/max constants here.
AUTO_ANALYSIS_PPTX_NAME = "auto_analysis.pptx"


_POOL = ThreadPoolExecutor(
    max_workers=AUTO_ANALYTICS_CONCURRENCY, thread_name_prefix="auto_analytics"
)
_META_LOCK = threading.Lock()


def _shutdown_pool():
    try:
        _POOL.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


atexit.register(_shutdown_pool)


def _default_state() -> dict:
    return {
        "status": "idle",
        "pptx_path": None,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "progress": None,
    }


def get_auto_analysis_state(meta: dict) -> dict:
    base = _default_state()
    st = meta.get("auto_analysis") if isinstance(meta, dict) else None
    if isinstance(st, dict):
        for k in base:
            if k in st:
                base[k] = st[k]
        if base.get("status") not in ("idle", "processing", "done"):
            base["status"] = "idle"
    return base


def _update_state(store: local_store.ChatDataStore, **fields) -> dict:
    with _META_LOCK:
        meta = store.read_meta()
        state = get_auto_analysis_state(meta)
        state.update(fields)
        meta["auto_analysis"] = state
        store.write_meta(meta)
    return state


def _run_job(chat_id: str, user_email: str) -> None:
    """Background worker: plan -> render charts -> build deck.

    Mirrors the global `auto_analytics._run_job` flow; only the LLM calls go
    through the brain (planner + narrative) and the PPTX render stays local.
    """
    store = local_store.ChatDataStore(chat_id)
    sid = "aa_" + secrets.token_hex(6)
    try:
        log_with_sid(sid, "info", f"AUTO_ANALYTICS_BG_START chat={chat_id}")

        meta = store.read_meta()
        schema_docs = store.schema_docs()
        common_fields = meta.get("common_fields", []) or []
        dfs = store.load_dataframes()
        if not dfs:
            raise RuntimeError("Chat dataset is empty — nothing to analyze")

        # Step 1: Build schema_text locally (no row values)
        from schema_builder import schema_text as _schema_text
        schema_text = _schema_text(schema_docs, dfs, common_fields)

        # Step 2: Brain planner → instruction list
        _update_state(store, progress="Planning…")
        try:
            rsp = brain_client.auto_analytics_plan(
                sid=sid, schema_text=schema_text,
                df_names=list(dfs.keys()), common_fields=common_fields,
                user_email=user_email,
            )
        except (TenantRevokedError, BrainError) as e:
            raise RuntimeError(f"Planner unavailable: {e}")

        instructions: list[str] = rsp.get("instructions") or []
        if not instructions:
            raise RuntimeError("Planner returned no instructions")
        log_with_sid(sid, "info", f"AUTO_ANALYTICS_PLAN n={len(instructions)}")

        # Step 3: Run each instruction locally (one chart per)
        results: list = [None] * len(instructions)

        def _build_one(idx: int, instruction: str):
            try:
                local_sid = f"{sid}-{idx}"
                # Per-worker copies: executed LLM code mutates frames in place,
                # and the single load above would otherwise be SHARED by all 4
                # concurrent workers — restoring the copy-on-get isolation the
                # dataframe cache guarantees per load (QA 2.3).
                local_dfs = {k: df.copy() for k, df in dfs.items()}
                out = run_chat_local.run_chat(
                    sid=local_sid, dfs=local_dfs, schema_docs=schema_docs,
                    question=instruction, history_rows=[],
                    user_email=user_email,
                )
                img = out.get("image_base64")
                return idx, instruction, out.get("text", ""), img, out.get("code", "")
            except Exception as ex:
                log_with_sid(sid, "warning",
                             f"AUTO_ANALYTICS_CHART idx={idx} ok=False err={ex}")
                return idx, instruction, None, None, None

        futures = [_POOL.submit(_build_one, i, instr)
                   for i, instr in enumerate(instructions)]
        done_count = 0
        total = len(futures)
        for fut in as_completed(futures):
            idx, instruction, answer, img_b64, code = fut.result()
            done_count += 1
            ok = bool(img_b64)
            log_with_sid(sid, "info", f"AUTO_ANALYTICS_CHART idx={idx} ok={ok}")
            _update_state(store, progress=f"Chart {done_count}/{total}")
            if ok:
                results[idx] = (instruction, answer or "", img_b64, code or "")

        charts = [r for r in results if r is not None]
        if not charts:
            raise RuntimeError("No charts could be rendered")

        # Step 4: Resolve plotly→PNG, build synthetic qa_pairs
        from routes.report import (
            _resolve_chart_image, _render_pptx, _slot_budgets_for,
        )
        _update_state(store, progress="Building narrative…")

        qa_pairs = []
        for i, (instruction, answer, img_b64, code) in enumerate(charts):
            resolved_png, is_chart = _resolve_chart_image(img_b64, sid)
            qa_pairs.append({
                "human": {"content": instruction},
                "ai": {
                    "content": answer,
                    "image_base64": resolved_png or img_b64,
                    "_resolved_png": resolved_png,
                    "code": code,
                },
                "ai_index": i,
                "has_chart": True,
                "has_table": False,
            })

        # Step 5: Brain narrative → report_structure
        findings = []
        for idx, p in enumerate(qa_pairs):
            findings.append({
                "index": idx,
                "question": (p["human"].get("content") or "")[:500],
                "answer_text": (p["ai"].get("content") or "")[:500],
                "has_chart": True, "has_table": False,
                "table_columns": [],
                "code_snippet": (p["ai"].get("code") or "")[:300],
            })
        # Pass per-slot char/line budgets so the brain narrative is generated to
        # the SAME budgets the renderer enforces (matches the manual export
        # path). `_slot_budgets_for` returns {} when no plan is cached, so this
        # is a no-op-safe addition.
        try:
            report_rsp = brain_client.report(sid=sid, qa_pairs=findings,
                                              user_email=user_email,
                                              slot_budgets=_slot_budgets_for(sid))
        except (TenantRevokedError, BrainError) as e:
            raise RuntimeError(f"Narrative unavailable: {e}")
        report_structure = (report_rsp or {}).get("report_structure") or {}

        # Step 6: Render PPTX locally + persist
        _update_state(store, progress="Rendering deck…")
        pptx_bytes = _render_pptx(qa_pairs, report_structure, sid)

        pptx_path = store.root / AUTO_ANALYSIS_PPTX_NAME
        pptx_path.write_bytes(pptx_bytes)

        _update_state(
            store,
            status="done",
            pptx_path=f"chatdata/{chat_id}/{AUTO_ANALYSIS_PPTX_NAME}",
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=None, progress=None,
        )
        log_with_sid(sid, "info",
                     f"AUTO_ANALYTICS_DONE chat={chat_id} charts={len(charts)}")

        # Activity: tenant-wide rollup so the operator sees auto-analytics fires
        try:
            brain_client.post_activity(
                "auto_analytics_completed", user_email,
                {"chat_id": chat_id, "charts": len(charts)},
            )
        except Exception:
            pass

    except Exception as e:
        log_with_sid(sid, "error", f"AUTO_ANALYTICS_FAILED chat={chat_id} err={e}")
        try:
            _update_state(
                store, status="idle", error=str(e)[:500],
                finished_at=datetime.now(timezone.utc).isoformat(), progress=None,
            )
        except Exception as e2:
            log_with_sid(sid, "error",
                         f"AUTO_ANALYTICS_STATE_RESET_FAILED chat={chat_id} err={e2}")


def start_job(chat_id: str, user_email: str) -> None:
    """Start the auto-analysis daemon thread for `chat_id`. The thread keeps
    running even if the HTTP request returns / the client disconnects."""
    t = threading.Thread(
        target=_run_job, args=(chat_id, user_email), daemon=True,
        name=f"auto_analytics_{chat_id[:8]}",
    )
    t.start()
