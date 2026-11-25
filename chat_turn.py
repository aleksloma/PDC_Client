"""Run one question and save the turn, without a browser.

OPTION B. A turn is saved by two pieces of code: the browser handler
`routes/chat.py` `chat_stream` (its closure `_worker`) and this module. The
browser handler keeps its own code, so this module MIRRORS it:

* `prepare_turn` mirrors the steps of `chat_stream` before the stream starts;
* `run_prepared_turn` mirrors `_worker`, in the same order and with the same
  names, so the two can be read side by side.

THE RULE: when saving changes in chat_stream, change this module too;
tests/test_chat_turn_drift.py fails until you do.

Only that logic exists twice. Every helper is the one of `routes/chat.py`,
called as it is and read as `_chat.<name>` at call time: a monkeypatch reaches
both paths, and `routes/chat.py` can import this module one day (option A: the
browser handler calls `run_prepared_turn`) without an import cycle.

Both functions take plain arguments and block: the caller decides the thread,
and handles what `prepare_turn` may raise while it reads the chat.

Deliberate differences from `_worker`:
* a failing `on_event` is logged and ignored — the browser's consumer sits
  behind a queue and cannot fail the turn either;
* `title="never"` skips the title generation;
* CHAT_THREAD_ERROR and CHAT_PERSIST_ERROR log the exception type only, never
  its text, which can quote table values;
* the per-chart `plot_generated` activity event is the consumer's, as on the
  web, and is not posted here.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import local_store
import routes.chat as _chat
from exec_transport import log_safe_text
from logger_utils import log_with_sid

NO_CONVERSATION_TEXT = "The conversation could not be opened."


@dataclass
class TurnRefusal:
    """The question was not accepted; nothing was saved."""
    kind: str               # auto_analysis_busy | empty_dataset | no_conversation
    status_code: int
    body: dict              # {"error": ..., ...} — what the web answers


@dataclass
class PreparedTurn:
    """An accepted question. Its human row is already in the conversation."""
    email: str
    chat_id: str
    question: str
    conv_id: str
    sid: str
    store: Any
    dfs: dict
    schema_docs: dict
    dataset_profiles: dict
    denied_text: Optional[str]      # set: every table was denied, no pipeline
    history_rows: list              # handed to the planner
    title_prior_rows: list          # a copy, the title is decided from it


@dataclass
class TurnResult:
    """`record` is the AI row that was built (None: the turn had none);
    `persisted` is True only when it was appended to the conversation."""
    record: Optional[dict]
    error_text: Optional[str]
    error_key: Optional[str]
    cancelled: bool
    persisted: bool
    conv_id: str
    sid: str


def _refusal(kind: str, response) -> TurnRefusal:
    """A refusal from the JSONResponse the web route would return."""
    return TurnRefusal(kind, response.status_code, json.loads(response.body))


def prepare_turn(email: str, chat_id: str, question: str, *,
                 conv_id: Optional[str] = None,
                 new_conversation: Optional[Callable] = None,
                 sid: Optional[str] = None) -> "PreparedTurn | TurnRefusal":
    """Every gate of a web question, then the question's row. Mirrors
    `chat_stream` up to the stream.

    The caller has already checked the chat access (`_chat._require_chat`),
    the use of a given `conv_id`, and that the question is not empty.
    `new_conversation(store)` returns a conversation id or None. It runs only
    without a `conv_id` and only after the gates, so a refused question
    creates nothing.
    """
    store = local_store.ChatDataStore(chat_id)
    busy = _chat._auto_analysis_busy_response(store, email, chat_id)
    if busy is not None:
        return _refusal("auto_analysis_busy", busy)
    dfs = store.load_dataframes(include_live=True)
    if not dfs:
        return _refusal("empty_dataset", _chat._empty_dataset_response(store, chat_id))
    schema_docs = store.schema_docs()
    sid = sid or secrets.token_hex(8)
    # Tables the requester's role does not cover leave before the planner sees
    # them; with nothing left the turn ends with the denial sentence.
    dropped = _chat._drop_uncovered_db_keys(email, store, dfs, schema_docs, sid)
    denied_text = _chat._live_denied_text(dropped, chat_id, sid) if not dfs else None
    # A profile failure never blocks the chat.
    try:
        dataset_profiles = local_store.ensure_chat_profiles(store, dfs)
    except Exception as e:
        log_with_sid(log_safe_text(sid, 80), "warning",
                     f"CHAT_PROFILES_FAILED error={log_safe_text(type(e).__name__, 80)}")
        dataset_profiles = {}

    if not conv_id:
        conv_id = new_conversation(store) if new_conversation is not None else None
    if not conv_id:
        return TurnRefusal("no_conversation", 409, {"error": NO_CONVERSATION_TEXT})

    history_rows = store.get_history(conv_id)
    # The rows before this turn decide the title (a copy: `history_rows` is
    # handed to the planner).
    title_prior_rows = list(history_rows)
    store.append_history(conv_id, {"role": "human", "content": question, "ts": time.time()})

    log_with_sid(log_safe_text(sid, 80), "info", "CHAT_TURN_REQ",
                 user=log_safe_text(email, 254), chat_id=log_safe_text(chat_id, 80),
                 conv_id=log_safe_text(str(conv_id), 80),
                 q=log_safe_text(question, 120))
    return PreparedTurn(
        email=email, chat_id=chat_id, question=question, conv_id=conv_id, sid=sid,
        store=store, dfs=dfs, schema_docs=schema_docs,
        dataset_profiles=dataset_profiles, denied_text=denied_text,
        history_rows=history_rows, title_prior_rows=title_prior_rows)


def run_prepared_turn(turn: PreparedTurn, *,
                      on_event: Optional[Callable[[str, Any], None]] = None,
                      title: str = "auto") -> TurnResult:
    """Run the pipeline and save the AI row exactly once. Mirrors `_worker`.

    `on_event(tag, payload)` gets what the web's live consumer gets:
    ("event", event) and ("error", text | {error, error_key}). It receives the
    live event and must not mutate it: the saved row is built from the same
    object. `title` is "auto" (the web's rule) or "never".
    """
    email, chat_id, question = turn.email, turn.chat_id, turn.question
    conv_id, sid, store = turn.conv_id, turn.sid, turn.store
    dfs, denied_text = turn.dfs, turn.denied_text
    consumer = {"failed": False}

    def _emit(tag, payload):
        """Stands where `_worker` queues an item for the browser."""
        if on_event is None:
            return
        try:
            on_event(tag, payload)
        except Exception as e:
            if not consumer["failed"]:      # one line per turn
                consumer["failed"] = True
                log_with_sid(log_safe_text(sid, 80), "warning",
                             f"CHAT_TURN_CONSUMER_FAILED "
                             f"error={log_safe_text(type(e).__name__, 80)}")

    w_charts: list[dict] = []      # per-chart {image_base64, answer, code, chart_data}
    w_combined_answer = None        # set by the multi-plot done event
    w_combined_codes: list = []
    w_usage: dict = {}
    w_single = None                 # single-shot result dict
    w_full_table_key = None         # durable key for a tabular result
    w_full_table_keys = None        # per-table keys for a multi-table result
    w_combined_tables = None        # tables of a mixed charts+tables answer
    w_live: dict = {}               # sql / live_truncated / live_rows of the turn
    w_narr: dict = {}               # {phase: narrative event} — the stop path only
    w_view = None                   # the done event's narrative_view (authoritative)
    w_tables: list = []             # a multi turn's tables (tables partials + done), in order
    w_table_keys: list = []         # their durable keys, aligned
    w_table_fields = {"table_captions": [], "table_caption_templates": [],
                      "table_phases": []}
    w_table_captioned = False       # any table event carried captions
    w_chart_by_n: dict = {}         # chart_n -> its w_charts entry (caption events)
    err_msg = None
    err_key = None                  # the result's error_key (never saved)
    cancelled = False               # STOP requested mid-generation
    w_inflight: list = []           # codes registered for refresh/pin
    record = None
    persisted = False
    # Clear any stale cancel flag, then mark the conversation in progress.
    # Both are cleared in `finally`, after the row is saved.
    _chat._clear_cancel(conv_id)
    _chat._mark_generating(conv_id)
    try:
        def _on_progress(ev):
            _emit("event", ev)

        gen = _chat._denied_events(denied_text) if denied_text else \
            _chat.run_chat_local.run_chat_multi_plot(
                sid=sid, dfs=dfs, schema_docs=turn.schema_docs,
                question=question, history_rows=turn.history_rows,
                user_email=email, dataset_profile=turn.dataset_profiles,
                on_progress=_on_progress,
            )
        for event in gen:
            # Tables are stored, and their keys put on the event, BEFORE the
            # consumer sees it.
            if isinstance(event, dict) and event.get("single_response"):
                _res = event.get("result") or {}
                # A table that already went out as a partial is stored once.
                _streamed = list(w_table_keys) if w_tables else None
                _tbl = _res.get("table")
                if isinstance(_tbl, dict) and _tbl.get("rows"):
                    if _streamed:
                        _k = _streamed[0]
                    else:
                        _k = _chat._persist_full_table(store, _tbl, _res.get("code"),
                                                       sql=_res.get("sql"),
                                                       first_key=next(iter(dfs), None))
                    if _k:
                        event["full_table_key"] = _k
                        w_full_table_key = _k
                # Multi-table answer: one durable key per table.
                _tbls = _res.get("tables")
                if isinstance(_tbls, list) and _tbls:
                    if _streamed is not None and len(_streamed) == len(_tbls):
                        _keys = list(_streamed)
                    else:
                        _keys = []
                        for _t in _tbls:
                            _k = _chat._persist_full_table(
                                store, _t, _res.get("code"),
                                result_key=_t.get("title"),
                                sql=_res.get("sql"),
                                first_key=next(iter(dfs), None))
                            _keys.append(_k)
                    event["full_table_keys"] = _keys
                    w_full_table_keys = _keys
            # Mixed dashboard tables, on the done event or a tables partial.
            if (isinstance(event, dict) and not event.get("single_response")
                    and event.get("tables")
                    and (event.get("done") or event.get("table_codes"))):
                _tbls = event.get("tables") or []
                _codes = event.get("table_codes") or []
                _rkeys = event.get("table_result_keys") or []
                _keys = []
                for _i, _t in enumerate(_tbls):
                    _keys.append(_chat._persist_full_table(
                        store, _t,
                        _codes[_i] if _i < len(_codes) else None,
                        result_key=_rkeys[_i] if _i < len(_rkeys) else None,
                        sql=event.get("sql"),
                        first_key=next(iter(dfs), None)))
                event["full_table_keys"] = _keys
                _caps, _tpls, _phs, _has = _chat._event_table_fields(event, len(_tbls))
                w_tables.extend(_tbls)
                w_table_keys.extend(_keys)
                w_table_fields["table_captions"].extend(_caps)
                w_table_fields["table_caption_templates"].extend(_tpls)
                w_table_fields["table_phases"].extend(_phs)
                w_table_captioned = w_table_captioned or _has
                w_combined_tables = w_tables
                w_full_table_keys = w_table_keys
            # Registered BEFORE the consumer sees the event; removed below,
            # after the row is saved.
            _event_sql_map = _chat._event_sql(event)
            for _c in _chat._event_codes(event):
                _chat._inflight_add(chat_id, _c, sql=_event_sql_map)
                w_inflight.append(_c)
            # The live fields ride every event; the last seen values are kept.
            if isinstance(event, dict) and not event.get("single_response"):
                for _k in ("sql", "live_truncated", "live_rows"):
                    if _k in event:
                        w_live[_k] = event[_k]
            # The consumer first, then accumulate.
            _emit("event", event)
            try:
                if isinstance(event, dict):
                    if event.get("narrative"):
                        w_narr[event.get("phase") or 1] = event
                    elif event.get("progress"):
                        pass
                    elif event.get("caption"):
                        _entry = w_chart_by_n.get(event.get("index"))
                        if _entry is not None:
                            _entry["answer"] = event.get("text") or ""
                    elif event.get("partial"):
                        w_usage = event.get("usage") or w_usage
                        if event.get("image_base64"):
                            _entry = {
                                "image_base64": event.get("image_base64"),
                                "answer": event.get("answer", ""),
                                "code": event.get("code"),
                                "chart_data": event.get("chart_data"),
                                "caption_template": event.get("caption_template"),
                                "phase": event.get("phase"),
                            }
                            w_charts.append(_entry)
                            w_chart_by_n[event.get("chart_n")] = _entry
                    elif event.get("done") and not event.get("single_response"):
                        w_combined_answer = event.get("combined_answer", "")
                        w_combined_codes = event.get("combined_codes") or []
                        w_usage = event.get("total_usage") or {}
                        w_view = event.get("narrative_view")
                    elif event.get("single_response"):
                        w_single = event.get("result") or {}
            except Exception as e:
                # One odd event never costs the turn (the web drops it too).
                log_with_sid(log_safe_text(sid, 80), "warning",
                             f"CHAT_TURN_EVENT_SKIPPED "
                             f"error={log_safe_text(type(e).__name__, 80)}")
            # Cooperative STOP: halt here, what was produced so far is kept.
            if _chat._is_cancelled(conv_id):
                cancelled = True
                log_with_sid(log_safe_text(sid, 80), "info", "CHAT_STOP_AT_BOUNDARY",
                             charts=len(w_charts))
                break
        # Stopped before the generator's own done event: build the combined
        # result from what there is, and emit a done.
        if cancelled and w_single is None and w_combined_answer is None:
            # The saved text is brain-safe (caption TEMPLATES, else describe
            # texts); the consumer gets the display text.
            w_display = None
            if w_charts:
                parts = [c.get("caption_template") or c.get("answer", "")
                         for c in w_charts]
                parts = [p for p in parts if p]
                w_combined_answer = "\n\n".join(parts) if parts else "Analysis stopped."
                answers = [c.get("answer", "") for c in w_charts if c.get("answer")]
                w_display = "\n\n".join(answers) if answers else "Analysis stopped."
                w_combined_codes = [c.get("code") for c in w_charts if c.get("code")]
            if w_display is None and isinstance(w_narr.get(1), dict):
                w_display = w_narr[1].get("text") or ""
            _emit("event", {
                "partial": False, "done": True,
                "combined_answer": w_combined_answer or "",
                "display_answer": w_display or "",
                "combined_codes": w_combined_codes,
                "total_usage": w_usage,
                "narrative_view": _chat._narr_event_view(w_narr.get(1)),
            })
    except _chat.TenantRevokedError:
        err_msg = "Service unavailable. Please contact your administrator."
        _emit("error", err_msg)
    except _chat.run_chat_local.AnalysisFailedError:
        # A BrainError subclass, caught FIRST.
        err_msg, err_key = _chat.ANALYSIS_FAILED_TEXT, _chat.ANALYSIS_FAILED_KEY
        _emit("error", {"error": err_msg, "error_key": err_key})
    except _chat.BrainTimeoutError:
        # A BrainError subclass too.
        err_msg, err_key = _chat.BRAIN_TIMEOUT_TEXT, _chat.BRAIN_TIMEOUT_KEY
        _emit("error", {"error": err_msg, "error_key": err_key})
    except _chat.BrainError:
        err_msg = "Analysis service is temporarily unavailable."
        _emit("error", err_msg)
    except Exception as e:
        # Type only, unlike the web: the text can quote table values (MCP logs none).
        log_with_sid(log_safe_text(sid, 80), "error",
                     f"CHAT_THREAD_ERROR: {log_safe_text(type(e).__name__, 80)}")
        err_msg = "Something went wrong while processing your request."
        _emit("error", err_msg)

    # ── Exactly-once AI-turn persistence (Articles IV & V) ──
    try:
        # The table arrays ride only when a table event carried them.
        w_tbl_src = dict(w_table_fields) if (w_tables and w_table_captioned) else {}
        if cancelled:
            record = _chat._build_stopped_record(
                w_combined_answer, w_combined_codes, w_usage, w_charts,
                live_fields=w_live, narr_event=w_narr.get(1),
                combined_tables=w_combined_tables,
                full_table_keys=w_full_table_keys,
                narrative_src=w_tbl_src)
        else:
            w_tbl_src["narrative_view"] = w_view
            record = _chat._build_ai_history_record(
                err_msg, w_single, w_combined_answer, w_combined_codes,
                w_usage, w_charts, full_table_key=w_full_table_key,
                full_table_keys=w_full_table_keys,
                combined_tables=w_combined_tables,
                live_fields=w_live, narrative_src=w_tbl_src)
        if record is not None:
            store.append_history(conv_id, record)
            persisted = True
            if not err_msg and not cancelled and title != "never":
                # Judged on the rows this turn started from — not a re-read,
                # which a concurrent turn could change.
                try:
                    if _chat._title_due([*turn.title_prior_rows, record]):
                        _chat._start_title_generation(
                            email, chat_id, conv_id, question,
                            _chat._answer_for_title(w_single, w_view, w_combined_answer))
                except Exception as e:
                    log_with_sid(log_safe_text(sid, 80), "warning",
                                 f"TITLE_TRIGGER_FAILED "
                                 f"error={log_safe_text(type(e).__name__, 80)}")
    except Exception as e:
        # Type only, unlike the web: the text can quote table values (MCP logs none).
        log_with_sid(log_safe_text(sid, 80), "error",
                     f"CHAT_PERSIST_ERROR: {log_safe_text(type(e).__name__, 80)}")
    finally:
        # Only after the row is saved: a status poll that sees
        # generating=false finds the AI row.
        _chat._inflight_discard(chat_id, w_inflight)
        _chat._unmark_generating(conv_id)
        _chat._clear_cancel(conv_id)
    return TurnResult(record=record, error_text=err_msg, error_key=err_key,
                      cancelled=cancelled, persisted=persisted,
                      conv_id=conv_id, sid=sid)
