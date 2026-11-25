"""MCP stage 2 — the DRIFT test (MCP_INTEGRATION_PROMPT.txt part 5).

A question is saved by two pieces of code: the browser handler
(`routes/chat.py` `chat_stream` -> `_worker`) and the run-and-save module of
the MCP path (`chat_turn.run_prepared_turn`). This file feeds the SAME
scripted `run_chat_multi_plot` events through both and compares everything
they leave behind:

  * the rows appended to the conversation file (key ORDER included);
  * the durable full-table records the rows point at, and how many record
    files were written (a table streamed early is stored once);
  * what the planner is called with (minus `sid` / `on_progress`);
  * the (tag, payload) items handed to the live consumer;
  * the in-flight code / SQL registry and the "generating" mark after every
    event, and their clean state at the end;
  * the title trigger, the log markers, the row the web history route serves.

If it fails, saving changed on one side only: change the other side too.
The code of `chat_stream` itself is pinned as well (`CHAT_STREAM_PIN`), so a
change no scenario covers is noticed too.

Normalised: the `ts` of a row (after asserting it is a float) and the random
full-table keys (replaced by K1, K2, … in order of first appearance, so a key
reused where the other side minted a new one still differs). Nothing else.

Progress events pushed through the flow's `on_progress` callback are live UX
of the browser and are left out of the event comparison; progress events the
generator YIELDS are compared.

Offline: DATA_ROOT is tmp_path, every brain call is stubbed or recorded.
"""
import copy
import dataclasses
import hashlib
import importlib
import inspect
import io
import json
import logging
import re
import secrets
import textwrap
import tokenize

import pandas as pd
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import local_store
import narrative
from conftest import JSON_HEADERS
from settings import settings

OWNER = "owner@acme.com"
FRIEND = "friend@acme.com"
OTHER = "other.friend@acme.com"
STRANGER = "stranger@acme.com"
CHAT = "c_turndrift000001"
QUESTION = "How did sales go?"
KEY = "sales.csv"

TCODE = "RESULT = dfs['sales.csv'].groupby('region')['amount'].sum().reset_index()"
DFCODE = "RESULT = df.head()"
SQL_MAP = {KEY: "SELECT region, amount FROM sales"}
NT = "Revenue was {{total:count}} in {{region}}.{{#anomaly}} One month stands out.{{/anomaly}}"
VALUES = {"total": 1234567, "region": "North"}
FINDINGS = {"anomaly": True}
NR = narrative.render(NT, VALUES, FINDINGS, sid="test")
CAP = "Top region is {{region}}."
CAP_R = "Top region is North."

# Log markers both sides must write for the same turn.
MARKERS = ("CHAT_THREAD_ERROR", "CHAT_PERSIST_ERROR", "CHAT_STOP_AT_BOUNDARY",
           "TITLE_TRIGGER_FAILED", "PERSIST_FULL_TABLE_FAILED")

ON_PROGRESS_MARK = "_pushed_through_on_progress"


def _chat_mod():
    return importlib.import_module("routes.chat")


def _ct():
    return importlib.import_module("chat_turn")


# ===========================================================================
# the world
# ===========================================================================
@pytest.fixture
def world(tmp_path, monkeypatch):
    chat_mod = _chat_mod()
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "SLOW_ANSWER_NOTICE_SECONDS", 0)
    local_store._DATAFRAME_CACHE.invalidate()
    for email in (OWNER, FRIEND, OTHER, STRANGER):
        local_store.AuthStore().ensure_user(email)
    store = local_store.ChatDataStore(CHAT)
    pd.DataFrame({"region": ["North", "South"], "amount": [1, 2]}).to_csv(
        store.files_dir / "sales.csv", index=False)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["title"] = "Sales"
    meta["files"] = [{"file_name": "sales.csv", "file_description": "sales",
                      "schema": {"fields": {"region": {"description": "region"},
                                            "amount": {"description": "amount"}}}}]
    meta["sharing"] = {"shared_with": [FRIEND, OTHER]}
    store.write_meta(meta)
    store.set_welcome("Welcome to Sales.")
    for reg in (chat_mod._INFLIGHT_CODES, chat_mod._INFLIGHT_SQL):
        reg.clear()
    yield store
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def client(world):
    chat_mod = _chat_mod()
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_" + secrets.token_hex(8)
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}", headers=JSON_HEADERS)
    return tc


@dataclasses.dataclass
class Run:
    """Everything one path left behind, raw."""
    conv_id: str = ""
    rows: list = dataclasses.field(default_factory=list)        # appended rows
    events: list = dataclasses.field(default_factory=list)      # (tag, payload)
    calls: list = dataclasses.field(default_factory=list)       # pipeline kwargs
    registry: list = dataclasses.field(default_factory=list)    # after each event
    titles: list = dataclasses.field(default_factory=list)
    title_rows: list = dataclasses.field(default_factory=list)  # rows on disk at the trigger
    brain: list = dataclasses.field(default_factory=list)       # brain_client calls
    markers: list = dataclasses.field(default_factory=list)
    served: list = dataclasses.field(default_factory=list)      # GET .../history
    records: dict = dataclasses.field(default_factory=dict)     # key -> durable record
    new_files: int = 0                                          # full-table files written
    end_state: dict = dataclasses.field(default_factory=dict)
    result: object = None                                       # TurnResult (MCP only)


# ---------------------------------------------------------------------------
# the scripted pipeline
# ---------------------------------------------------------------------------
# A script is a list of steps:
#   {...}                  an event, yielded as it is
#   ("progress", event)    pushed through kw["on_progress"], when there is one
#   ("stop",)              the user presses Stop while the next event is made
#   ("raise", factory)     the generator raises
#   ("raise_at_call", f)   calling the pipeline raises (first step only)
#   ("rewrite_history",)   the planner rewrites the history list it was handed
class Harness:
    def __init__(self, client, store, monkeypatch, caplog):
        self.client = client
        self.store = store
        self.mp = monkeypatch
        self.caplog = caplog
        self.chat_mod = _chat_mod()
        self.current = None         # the Run being recorded
        self.script = []
        self._install()

    # -- stubs shared by both paths ----------------------------------------
    def _install(self):
        chat_mod, h = self.chat_mod, self

        def fake(**kw):
            run = h.current
            run.calls.append(_freeze_kwargs(kw))
            steps = copy.deepcopy(h.script)
            if steps and isinstance(steps[0], tuple) and steps[0][0] == "raise_at_call":
                raise steps[0][1]()
            return h._generate(steps, kw)

        self.mp.setattr(chat_mod.run_chat_local, "run_chat_multi_plot", fake)
        self.mp.setattr(chat_mod, "_start_title_generation", h._title)

        # Every public brain_client function is recorded: neither path may
        # reach the brain around the (stubbed) pipeline.
        for name, fn in list(vars(brain_client).items()):
            if name.startswith("_") or not inspect.isfunction(fn):
                continue
            if fn.__module__ != "brain_client" and name not in ("post_activity", "title"):
                continue        # log_with_sid, log_safe_text, … imported names
            if name in ("close_client", "live_sql_error"):
                continue

            def rec(*a, _name=name, **k):
                if h.current is not None:
                    h.current.brain.append(_name)
                return {}

            self.mp.setattr(brain_client, name, rec)

        # The browser path's (tag, payload) items: `_is_answer_event` sees
        # each one the consumer dequeues as long as it answers False.
        def seen(tag, payload):
            if h.current is not None:
                h.current.events.append((tag, copy.deepcopy(payload)))
            return False

        self.mp.setattr(chat_mod, "_is_answer_event", seen)

    def _title(self, *a, **k):
        bound = inspect.signature(
            lambda email, chat_id, conv_id, question, answer: None).bind(*a, **k)
        self.current.titles.append(dict(bound.arguments))
        conv_id = self.current.conv_id
        self.current.title_rows.append(
            len(self.store.get_history(conv_id)) if conv_id else 0)

    def _generate(self, steps, kw):
        chat_mod, run = self.chat_mod, self.current
        for step in steps:
            if isinstance(step, dict):
                yield step
                # resumed: the path has processed the event
                run.registry.append(json.dumps([
                    chat_mod._INFLIGHT_CODES.get(CHAT),
                    chat_mod._INFLIGHT_SQL.get(CHAT),
                    chat_mod._is_generating(run.conv_id)], sort_keys=True))
                continue
            what = step[0]
            if what == "progress":
                cb = kw.get("on_progress")
                if callable(cb):
                    cb({**step[1], ON_PROGRESS_MARK: True})
            elif what == "stop":
                chat_mod._request_cancel(run.conv_id)
            elif what == "raise":
                raise step[1]()
            elif what == "rewrite_history":
                kw["history_rows"][:] = [{"role": "ai", "content": "x", "code": "x = 1"}]
            else:                                               # pragma: no cover
                raise AssertionError(f"unknown script step {step!r}")

    # -- recording ----------------------------------------------------------
    def seed(self, email, *, answered=False):
        """A conversation in `email`'s index with earlier rows."""
        conv_id = self.store.new_conversation("seeded")
        local_store.AuthStore().record_conversation(email, CHAT, conv_id, "seeded")
        rows = [{"role": "ai", "content": "Welcome to Sales.", "ts": 1.5},
                {"role": "human", "content": "earlier question", "ts": 2.5},
                {"role": "ai", "content": "Something went wrong.", "ts": 3.5}]
        if answered:
            rows += [{"role": "human", "content": "and then?", "ts": 4.5},
                     {"role": "ai", "content": "42 rows.", "image_base64": None,
                      "table": None, "code": "RESULT = 42", "usage": {}, "ts": 5.5}]
        for row in rows:
            self.store.append_history(conv_id, row)
        return conv_id, len(rows)

    def _full_files(self):
        d = self.store.conversations_dir / "full"
        return {p.name for p in d.glob("*.json")} if d.is_dir() else set()

    def _record(self, script, email, conv_id, skip, go):
        chat_mod = self.chat_mod
        self.script = script
        run = self.current = Run(conv_id=conv_id)
        before, first_log = self._full_files(), len(self.caplog.records)
        with self.caplog.at_level(logging.INFO):
            go(run)
        run.new_files = len(self._full_files() - before)
        run.rows = _raw_rows(self.store, conv_id)[skip:]
        run.markers = [m for r in self.caplog.records[first_log:]
                       for m in MARKERS if m in r.getMessage()]
        run.end_state = {"codes": CHAT in chat_mod._INFLIGHT_CODES,
                         "sql": CHAT in chat_mod._INFLIGHT_SQL,
                         "generating": chat_mod._is_generating(conv_id),
                         "cancel": chat_mod._is_cancelled(conv_id)}
        # The durable records must come from DISK (what survives a restart).
        chat_mod._FULL_TABLE_CACHE.clear()
        chat_mod._FULL_TABLE_ORDER.clear()
        for key in _keys_in([run.events, run.rows]):
            run.records[key] = chat_mod._load_full_table_record(self.store, key)
        run.served = self.history(conv_id, skip)
        self.current = None
        return run

    def history(self, conv_id, skip=0):
        r = self.client.get(f"/api/chat/{CHAT}/conversation/{conv_id}/history")
        assert r.status_code == 200, r.text[:300]
        return r.json()["history"][skip:]

    def login(self, email):
        self.client.post(f"/_login/{email}", headers=JSON_HEADERS)

    # -- the two paths ------------------------------------------------------
    def web(self, script, *, email=OWNER, answered=False, question=QUESTION):
        self.login(email)
        conv_id, skip = self.seed(email, answered=answered)

        def go(run):
            r = self.client.post(f"/api/chat/{CHAT}/chat/stream",
                                 json={"question": question, "conv_id": conv_id},
                                 headers=JSON_HEADERS)
            assert r.status_code == 200, r.text[:300]

        return self._record(script, email, conv_id, skip, go)

    def mcp(self, script, *, email=OWNER, answered=False, question=QUESTION, title="auto"):
        ct = _ct()
        self.login(email)
        conv_id, skip = self.seed(email, answered=answered)

        def go(run):
            turn = ct.prepare_turn(email, CHAT, question, conv_id=conv_id)
            assert isinstance(turn, ct.PreparedTurn), turn
            run.result = ct.run_prepared_turn(
                turn, title=title,
                on_event=lambda tag, payload: run.events.append(
                    (tag, copy.deepcopy(payload))))

        return self._record(script, email, conv_id, skip, go)


@pytest.fixture
def harness(client, world, monkeypatch, caplog):
    return Harness(client, world, monkeypatch, caplog)


# ===========================================================================
# reading, normalising, comparing
# ===========================================================================
def _raw_rows(store, conv_id):
    """The conversation file, line by line, with the stored key order."""
    p = store.conversations_dir / f"{conv_id}.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


def _keys_in(value, out=None):
    """The full-table keys in `value`, by first appearance."""
    out = [] if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "full_table_key" and isinstance(v, str):
                out.append(v)
            elif k == "full_table_keys" and isinstance(v, list):
                out.extend(x for x in v if isinstance(x, str))
            else:
                _keys_in(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _keys_in(v, out)
    return list(dict.fromkeys(out))


def _alias(value, names):
    """`value` with every full-table key replaced by its name in `names`."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "full_table_key" and isinstance(v, str):
                out[k] = names[v]
            elif k == "full_table_keys" and isinstance(v, list):
                out[k] = [names[x] if isinstance(x, str) else x for x in v]
            else:
                out[k] = _alias(v, names)
        return out
    if isinstance(value, (list, tuple)):
        return [_alias(v, names) for v in value]
    return value


def _names(*values):
    return {k: f"K{i}" for i, k in enumerate(_keys_in(list(values)), 1)}


def _dump(value):
    """Order-preserving on purpose: no sort_keys."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _norm_rows(rows, names):
    out = []
    for row in rows:
        assert isinstance(row.get("ts"), float), f"ts is not a float: {row.get('ts')!r}"
        row = _alias(row, names)
        row["ts"] = 0.0
        out.append(_dump(row))
    return out


def _freeze_kwargs(kw):
    """The pipeline's keyword arguments as comparable text, taken at call
    time (the planner is handed live objects)."""
    out = {}
    for k, v in kw.items():
        if k in ("sid", "on_progress"):
            continue
        if k == "dfs":
            v = [[name, list(map(str, df.columns)), list(map(str, df.dtypes)),
                  df.to_json(orient="split"), str(dict(df.attrs))]
                 for name, df in v.items()]
        out[k] = _dump(v)
    out["_sid_is_text"] = isinstance(kw.get("sid"), str) and bool(kw.get("sid"))
    return out


def compare_rows(a_rows, b_rows, a_names=None, b_names=None):
    """THE comparison: the rows both paths appended, normalised."""
    a = _norm_rows(a_rows, _names(a_rows) if a_names is None else a_names)
    b = _norm_rows(b_rows, _names(b_rows) if b_names is None else b_names)
    assert a == b, "the saved history rows differ:\n" + "\n".join(
        f"  browser: {x}\n  mcp    : {y}" for x, y in _zip_diff(a, b))


def _zip_diff(a, b):
    n = max(len(a), len(b))
    pad = lambda s: s + ["<missing>"] * (n - len(s))    # noqa: E731
    return [(x, y) for x, y in zip(pad(list(a)), pad(list(b))) if x != y]


def _comparable_events(run):
    return [(tag, p) for tag, p in run.events
            if not (isinstance(p, dict) and p.get(ON_PROGRESS_MARK))]


def compare(web: Run, mcp: Run, *, titles=True):
    """Everything a turn leaves behind, browser path vs the other one."""
    wn, mn = _names(web.events, web.rows), _names(mcp.events, mcp.rows)
    compare_rows(web.rows, mcp.rows, wn, mn)
    # the row the web history route serves
    compare_rows(web.served, mcp.served, wn, mn)
    # durable records, in the order the turn names them
    wr = [_dump(web.records[k]) for k in wn]
    mr = [_dump(mcp.records[k]) for k in mn]
    assert wr == mr, f"durable table records differ: {_zip_diff(wr, mr)}"
    assert web.new_files == mcp.new_files, \
        f"full-table files written: browser {web.new_files}, mcp {mcp.new_files}"
    # what the planner was called with
    assert [list(c) for c in map(sorted, web.calls)] == [list(c) for c in map(sorted, mcp.calls)], \
        "run_chat_multi_plot got different argument names"
    for w, m in zip(web.calls, mcp.calls):
        for k in w:
            assert w[k] == m[k], f"run_chat_multi_plot {k} differs:\n{w[k]}\n{m[k]}"
    # what the live consumer saw
    we = [_dump([t, _alias(p, wn)]) for t, p in _comparable_events(web)]
    me = [_dump([t, _alias(p, mn)]) for t, p in _comparable_events(mcp)]
    assert we == me, f"emitted events differ: {_zip_diff(we, me)}"
    # registries while running and afterwards
    assert web.registry == mcp.registry, _zip_diff(web.registry, mcp.registry)
    assert web.end_state == mcp.end_state
    assert web.markers == mcp.markers, (web.markers, mcp.markers)
    if titles:
        blind = lambda run: [{**t, "conv_id": "CONV"} for t in run.titles]   # noqa: E731
        assert all(t["conv_id"] == r.conv_id for r in (web, mcp) for t in r.titles)
        assert blind(web) == blind(mcp), (web.titles, mcp.titles)
        assert web.title_rows == mcp.title_rows


CLEAN = {"codes": False, "sql": False, "generating": False, "cancel": False}


# ===========================================================================
# event builders
# ===========================================================================
def _table(n=2, title=None, total=None, **extra):
    t = {"columns": ["region", "amount"],
         "rows": [{"region": f"R{i}", "amount": i} for i in range(n)],
         "total_rows": total or n}
    if title is not None:
        t["title"] = title
    t.update(extra)
    return t


def _single(text="real answer", **extra):
    res = {"text": text, "image_base64": None, "table": None, "code": None,
           "usage": {"total_tokens": 11}}
    res.update(extra)
    return {"single_response": True, "result": res}


def _narr_fields():
    return {"narrative_template": NT, "narrative_rendered": NR,
            "values": dict(VALUES), "findings": dict(FINDINGS)}


def _chart(n, *, answer="", template=None, total=2, phase=1, **extra):
    ev = {"partial": True, "phase": phase, "block_index": n,
          "image_base64": f"<div>plotly chart {n}</div>", "answer": answer,
          "caption_template": template, "code": f"fig = chart_{n}",
          "chart_n": n, "chart_total": total,
          "chart_data": {"columns": ["x"], "rows": [{"x": n}]},
          "usage": {"total_tokens": n}}
    ev.update(extra)
    return ev


def _caption(n, text):
    return {"caption": True, "index": n, "text": text, "phase": 1, "block_index": n}


def _narr(phase=1):
    return {"narrative": True, "phase": phase, "text": NR, "template": NT,
            "values": dict(VALUES), "findings": dict(FINDINGS)}


def _view(phase1=True, phase2=None, status=None):
    return {"phase1": _narr_fields() if phase1 else None,
            "phase2": phase2, "phase2_status": status}


def _done(combined, display=None, codes=(), *, view=None, **extra):
    ev = {"done": True, "partial": False, "combined_answer": combined,
          "display_answer": combined if display is None else display,
          "combined_codes": list(codes), "total_usage": {"total_tokens": 99},
          "narrative_view": _view(False) if view is None else view}
    ev.update(extra)
    return ev


def _tables_partial(tables, codes, keys=None, **extra):
    ev = {"partial": True, "phase": 1, "tables": tables, "table_codes": codes,
          "table_result_keys": keys or [None] * len(tables)}
    ev.update(extra)
    return ev


def _single_table_partial(tables, *, sql=None):
    ev = {"partial": True, "single_table": True, "phase": 1, "tables": tables,
          "table": tables[0] if len(tables) == 1 else None, "code": TCODE,
          "table_codes": [TCODE] * len(tables),
          "table_result_keys": [t.get("title") for t in tables]}
    if sql is not None:
        ev["sql"] = sql
    return ev


HISTORY_CONTENT = narrative.history_content(NT, FINDINGS)
BIG_CHART_DATA = {"columns": ["x"], "rows": [{"x": "y" * 250_000}]}


def _two_charts_full():
    """Two charts (one captioned by template, one described later), the
    narrative, a tables partial with captions, done with a KPI table."""
    return [
        ("progress", {"progress": True, "message_key": "lab.stage_planning",
                      "message": "Planning the analysis…"}),
        _chart(1, answer=CAP_R, template=CAP),
        _chart(2),
        _caption(2, "Units peaked in March."),
        _tables_partial([_table(3, title="Detail")], [TCODE], ["Detail"],
                        table_captions=["By region"],
                        table_caption_templates=["By {{region}}"], table_phases=[1]),
        _narr(),
        _done(HISTORY_CONTENT, NR, ("fig = chart_1", "fig = chart_2"), view=_view(),
              tables=[_table(1, title="KPI")], table_codes=[DFCODE],
              table_result_keys=["KPI"], table_captions=["", ], table_phases=[1],
              table_caption_templates=[""]),
    ]


def _err(name):
    chat_mod = _chat_mod()
    return {
        "tenant": lambda: chat_mod.TenantRevokedError("revoked"),
        "analysis": lambda: chat_mod.run_chat_local.AnalysisFailedError("llm_unavailable"),
        "timeout": lambda: chat_mod.BrainTimeoutError("slow"),
        "brain": lambda: chat_mod.BrainError("down"),
        "generic": lambda: RuntimeError("KeyError about column 'secret'\nFORGED"),
    }[name]


ERRORS = ("tenant", "analysis", "timeout", "brain", "generic")


def _scenarios():
    s = {}
    # --- single answers ----------------------------------------------------
    s["single_text"] = lambda: [_single("There are three regions.",
                                        value_display="**Result:** 3")]
    s["single_empty_result"] = lambda: [{"single_response": True, "result": None}]
    s["single_table_no_partial"] = lambda: [
        _single("By region.", table=_table(2, total=400), code=TCODE)]
    s["single_table_identifier_columns_and_df_alias"] = lambda: [
        _single("Rows.", table=_table(2, identifier_columns=["region"]), code=DFCODE,
                sql=dict(SQL_MAP), live_truncated=False, live_rows={KEY: 2})]
    s["single_table_after_partial_with_live"] = lambda: [
        _single_table_partial([_table(2)], sql=dict(SQL_MAP)),
        _single("By region.", table=_table(2), code=TCODE, sql=dict(SQL_MAP),
                live_truncated=True, live_rows={KEY: 200000})]
    s["single_tables_equal_streamed"] = lambda: [
        _single_table_partial([_table(2, title="A"), _table(3, title="B")]),
        _single("Two tables.", code=TCODE,
                tables=[_table(2, title="A"), _table(3, title="B")])]
    s["single_tables_unequal_streamed"] = lambda: [
        _single_table_partial([_table(2, title="A"), _table(3, title="B")]),
        _single("Three tables.", code=TCODE,
                tables=[_table(2, title="A"), _table(3, title="B"),
                        _table(1, title="C")])]
    s["single_tables_no_partial"] = lambda: [
        _single("Two tables.", code=TCODE, sql=dict(SQL_MAP),
                tables=[_table(2, title="A"), _table(3, title="B")])]
    s["single_table_and_tables"] = lambda: [
        _single("Both.", code=TCODE, table=_table(2),
                tables=[_table(2, title="A")])]
    s["single_table_without_rows"] = lambda: [
        _single("Nothing found.", code=TCODE,
                table={"columns": ["a"], "rows": [], "total_rows": 0})]
    s["single_chart_narrative"] = lambda: [
        _single(HISTORY_CONTENT, image_base64="<div>plotly</div>", code="fig = c",
                chart_data={"columns": ["x"], "rows": [{"x": 1}]},
                caption_template=CAP, caption_rendered=CAP_R, **_narr_fields())]
    s["single_chart_described"] = lambda: [
        _single(HISTORY_CONTENT, image_base64="<div>plotly</div>", code="fig = c",
                caption_described="Sales rose.", **_narr_fields())]
    s["single_chart_oversized_chart_data"] = lambda: [
        _single("A chart.", image_base64="<div>plotly</div>", code="fig = c",
                chart_data=copy.deepcopy(BIG_CHART_DATA))]
    s["single_headline"] = lambda: [
        _single("Summary.", headline_display="Total: 1.2 mln.")]
    # --- multi answers -----------------------------------------------------
    s["two_charts_full"] = _two_charts_full
    s["two_charts_oversized_chart_data"] = lambda: [
        _chart(1, answer="One.", chart_data=copy.deepcopy(BIG_CHART_DATA)),
        _chart(2, answer="Two."),
        _done("One.\n\nTwo.", codes=("fig = chart_1", "fig = chart_2"))]
    s["one_chart_captioned"] = lambda: [
        _chart(1, answer=CAP_R, template=CAP, total=1), _narr(),
        _done(HISTORY_CONTENT, NR, ("fig = chart_1",), view=_view())]
    s["one_chart_described_narrated"] = lambda: [
        _chart(1, total=1), _caption(1, "Desc one"), _narr(),
        _done(HISTORY_CONTENT, NR, ("fig = chart_1",), view=_view())]
    s["one_chart_no_narrative"] = lambda: [
        _chart(1, answer="Desc", total=1), _done("Desc", codes=("fig = chart_1",))]
    s["phase2_chart"] = lambda: [
        _chart(1, answer="One.", total=1), _narr(),
        {"progress": True, "phase": 2, "message_key": "lab.phase2_progress",
         "message": "Checking the drivers…"},
        _chart(2, answer="Why.", phase=2, total=1), _narr(2),
        _done(HISTORY_CONTENT, NR, ("fig = chart_1", "fig = chart_2"),
              view=_view(phase2=_narr_fields(), status="done"))]
    s["only_phase2_chart"] = lambda: [
        _chart(1, answer="Why.", phase=2, total=1), _done("Why.", codes=("fig = chart_1",))]
    s["headline_without_template"] = lambda: [
        _chart(1, answer="One."), _chart(2, answer="Two."),
        {"narrative": True, "phase": 1, "text": "Total: 1.2 mln."},
        _done("One.\n\nTwo.", "Total: 1.2 mln.", ("fig = chart_1", "fig = chart_2"),
              view={**_view(False), "headline_display": "Total: 1.2 mln."})]
    s["done_tables_no_captions"] = lambda: [
        _chart(1, answer="One.", total=1),
        _done("One.", codes=("fig = chart_1",),
              tables=[_table(2, title="A"), _table(1, title="B")],
              table_codes=[TCODE], table_result_keys=["A"])]
    s["done_tables_captions_and_live"] = lambda: [
        _done("Tables.", codes=(), sql=dict(SQL_MAP), live_truncated=True,
              live_rows={KEY: 5}, tables=[_table(2, title="A")], table_codes=[TCODE],
              table_result_keys=["A"], table_captions=["Cap A"],
              table_caption_templates=["Cap {{a}}"], table_phases=[2])]
    s["tables_partial_then_done_tables"] = lambda: [
        _tables_partial([_table(2)], [TCODE]),
        _done("T.", codes=(TCODE,), tables=[_table(1, title="B")], table_codes=[TCODE],
              table_result_keys=["B"])]
    s["captioned_partial_then_uncaptioned_done_tables"] = lambda: [
        _tables_partial([_table(2)], [TCODE], table_captions=["By region"],
                        table_caption_templates=["By {{x}}"], table_phases=[1]),
        _done("T.", codes=(TCODE,), tables=[_table(1, title="B")], table_codes=[TCODE],
              table_result_keys=["B"])]
    s["tables_partial_without_codes_is_not_stored"] = lambda: [
        {"partial": True, "phase": 1, "tables": [_table(2)]}, _done("T.")]
    s["live_fields_on_partials_last_wins"] = lambda: [
        _chart(1, answer="One.", sql={KEY: "SELECT 1"}, live_truncated=False),
        _chart(2, answer="Two.", sql=dict(SQL_MAP), live_rows={KEY: 7}),
        _done("One.\n\nTwo.", codes=("fig = chart_1", "fig = chart_2"),
              live_truncated=True)]
    s["partial_without_image"] = lambda: [
        {"partial": True, "phase": 1, "image_base64": None, "answer": "x",
         "code": "fig = none", "chart_n": 1, "chart_total": 1,
         "usage": {"total_tokens": 4}},
        _done("No chart.")]
    s["done_empty_answer"] = lambda: [_done("", total_usage=None)]
    s["repeated_code_two_charts"] = lambda: [
        _chart(1, answer="One.", code="fig = same"),
        _chart(2, answer="Two.", code="fig = same"),
        _done("One.\n\nTwo.", codes=("fig = same", "fig = same"))]
    # --- odd events --------------------------------------------------------
    s["unknown_and_progress_events"] = lambda: [
        {"weird": 1, "other": [1]},
        {"progress": True, "message_key": "lab.narrative_retry", "message": "…"},
        ("progress", {"progress": True, "message_key": "lab.stage_running",
                      "message": "Running the analysis…"}),
        {"caption": True, "index": 9, "text": "no such chart"},
        _single("still answered")]
    s["accumulation_error_is_swallowed"] = lambda: [
        _chart(1, answer="One.", chart_n=[1]),       # unhashable: raises inside the block
        {"caption": True, "index": [1], "text": "lost"},
        _chart(2, answer="Two."),
        _done("One.\n\nTwo.", codes=("fig = chart_1", "fig = chart_2"))]
    s["table_that_cannot_be_stored"] = lambda: [
        _chart(1, answer="One.", total=1),
        _done("One.", codes=("fig = chart_1",), tables=["not a table"],
              table_codes=[TCODE])]
    s["tables_not_a_list_is_an_error_turn"] = lambda: [
        _chart(1, answer="One.", total=1), _done("One.", tables=5)]
    s["empty_generator"] = lambda: []
    s["only_progress"] = lambda: [
        ("progress", {"progress": True, "message_key": "lab.stage_planning",
                      "message": "…"})]
    s["history_list_rewritten_by_the_planner"] = lambda: [
        ("rewrite_history",), _single("answer", code=TCODE)]
    # --- errors ------------------------------------------------------------
    for name in ERRORS:
        s[f"error_{name}_first"] = lambda n=name: [("raise", _err(n))]
        s[f"error_{name}_after_chart"] = lambda n=name: [
            _chart(1, answer="One."), ("raise", _err(n))]
    s["error_at_the_call"] = lambda: [("raise_at_call", _err("generic"))]
    s["error_after_tables"] = lambda: [
        _tables_partial([_table(2)], [TCODE]), ("raise", _err("brain"))]
    s["error_after_done"] = lambda: [_done("Done."), ("raise", _err("generic"))]
    # --- stop --------------------------------------------------------------
    s["stop_after_first_chart"] = lambda: [
        ("stop",), _chart(1, answer=CAP_R, template=CAP, sql=dict(SQL_MAP)),
        _chart(2, answer="Two."), _done("never")]
    s["stop_after_uncaptioned_chart"] = lambda: [
        ("stop",), _chart(1, total=1), _done("never")]
    s["stop_after_caption"] = lambda: [
        _chart(1), ("stop",), _caption(1, "Desc one"), _chart(2, answer="never")]
    s["stop_after_narrative_without_charts"] = lambda: [
        ("stop",), _narr(), _done("never")]
    s["stop_after_headline_without_charts"] = lambda: [
        ("stop",), {"narrative": True, "phase": 1, "text": "Total: 3."}, _done("never")]
    s["stop_after_charts_and_narrative"] = lambda: [
        _chart(1, answer=CAP_R, template=CAP), _chart(2, answer="Two."),
        ("stop",), _narr(), _done("never")]
    s["stop_after_the_phase2_narrative"] = lambda: [
        _chart(1, answer="One.", total=1), _narr(), ("stop",),
        {**_narr(2), "template": "Second {{total}}.", "text": "Second 7."}, _done("never")]
    s["stop_after_captioned_tables"] = lambda: [
        ("stop",), _tables_partial([_table(2)], [TCODE], table_captions=["By region"],
                                   table_caption_templates=["By {{x}}"],
                                   table_phases=[1], sql=dict(SQL_MAP)),
        _done("never")]
    s["stop_after_single_table_partial"] = lambda: [
        ("stop",), _single_table_partial([_table(2)]),
        _single("never", table=_table(2), code=TCODE)]
    s["stop_after_single_response"] = lambda: [
        ("stop",), _single("late answer", table=_table(2), code=TCODE)]
    s["stop_after_done"] = lambda: [
        _chart(1, answer="One."), _chart(2, answer="Two."),
        ("stop",), _done("One.\n\nTwo.", codes=("fig = chart_1", "fig = chart_2"),
                         view=_view())]
    s["stop_after_progress"] = lambda: [
        ("stop",), {"progress": True, "message_key": "lab.narrative_retry", "message": "…"},
        _single("never")]
    return s


SCENARIOS = _scenarios()


# ===========================================================================
# THE drift test
# ===========================================================================
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_both_paths_save_the_same_turn(harness, name):
    script = SCENARIOS[name]()
    web = harness.web(script)
    mcp = harness.mcp(script)
    compare(web, mcp)
    assert web.end_state == mcp.end_state == CLEAN
    # one human row, at most one AI row, and the human row is the question
    assert [r["role"] for r in mcp.rows][:1] == ["human"]
    assert mcp.rows[0]["content"] == QUESTION
    assert [r["role"] for r in mcp.rows[1:]] in ([], ["ai"])
    # the result names what was saved
    res = mcp.result
    assert (res.conv_id, res.cancelled) == (mcp.conv_id, name.startswith("stop_"))
    assert isinstance(res.sid, str) and res.sid
    if len(mcp.rows) == 2:
        assert res.persisted is True
        names = _names(mcp.events, mcp.rows)
        compare_rows([local_store._json_safe(res.record)], mcp.rows[1:], names, names)
    else:
        assert res.record is None and res.persisted is False


@pytest.mark.parametrize("name", ["two_charts_full", "single_table_after_partial_with_live",
                                  "stop_after_first_chart", "error_timeout_after_chart"])
def test_both_paths_agree_on_a_conversation_that_was_already_answered(harness, name):
    """An earlier answered turn: its code rides the history, no title."""
    script = SCENARIOS[name]()
    web = harness.web(script, answered=True)
    mcp = harness.mcp(script, answered=True)
    compare(web, mcp)
    assert web.titles == mcp.titles == []
    assert "RESULT = 42" in mcp.calls[0]["history_rows"]


@pytest.mark.parametrize("name", ["two_charts_full", "single_table_no_partial"])
def test_both_paths_agree_for_a_share_recipient(harness, name):
    script = SCENARIOS[name]()
    web = harness.web(script, email=FRIEND)
    mcp = harness.mcp(script, email=FRIEND)
    compare(web, mcp)
    assert json.loads(mcp.calls[0]["user_email"]) == FRIEND


def test_a_stale_stop_flag_is_cleared_by_both_paths(harness):
    """A Stop pressed after an earlier turn ended must not stop this one."""
    chat_mod = harness.chat_mod
    real_seed = harness.seed

    def seed(email, **kw):
        conv_id, n = real_seed(email, **kw)
        chat_mod._request_cancel(conv_id)
        return conv_id, n

    harness.seed = seed
    script = SCENARIOS["two_charts_full"]()
    web, mcp = harness.web(script), harness.mcp(script)
    compare(web, mcp)
    assert mcp.result.cancelled is False
    assert not mcp.rows[-1].get("stopped")


def test_a_title_failure_is_logged_by_both_and_the_answer_stays(harness, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("title broke")

    monkeypatch.setattr(harness.chat_mod, "_start_title_generation", boom)
    script = SCENARIOS["single_table_no_partial"]()
    web, mcp = harness.web(script), harness.mcp(script)
    compare(web, mcp, titles=False)
    assert mcp.markers == ["TITLE_TRIGGER_FAILED"]
    assert mcp.result.persisted is True


def test_a_failing_record_builder_saves_nothing_on_both(harness, monkeypatch):
    def boom(*a, **k):
        raise ValueError("cannot build")

    monkeypatch.setattr(harness.chat_mod, "_build_ai_history_record", boom)
    script = SCENARIOS["two_charts_full"]()
    web, mcp = harness.web(script), harness.mcp(script)
    compare(web, mcp)
    assert mcp.markers == ["CHAT_PERSIST_ERROR"]
    assert mcp.end_state == CLEAN
    assert [r["role"] for r in mcp.rows] == ["human"]
    assert mcp.result.persisted is False


def test_the_record_builder_is_called_with_the_same_arguments(harness, monkeypatch):
    """`narrative_src` always carries `narrative_view` on the non-stopped
    path (a None view included) — pinned at the call, where a difference
    would not show in an old-shape record."""
    chat_mod = harness.chat_mod
    real = chat_mod._build_ai_history_record
    seen = []

    def spy(*a, **k):
        bound = inspect.signature(real).bind(*a, **k)
        bound.apply_defaults()
        seen.append(copy.deepcopy(dict(bound.arguments)))
        return real(*a, **k)

    monkeypatch.setattr(chat_mod, "_build_ai_history_record", spy)
    for name in ("single_text", "two_charts_full", "done_tables_no_captions",
                 "done_empty_answer", "error_brain_first", "empty_generator"):
        seen.clear()
        script = SCENARIOS[name]()
        harness.web(script)
        harness.mcp(script)
        assert len(seen) == 2, (name, len(seen))
        for args in seen:
            assert "narrative_view" in args["narrative_src"], (name, args["narrative_src"])
        # each call's aliases are its own: keys are random per path
        wn, mn = _names(seen[0]), _names(seen[1])
        assert _dump(_alias(seen[0], wn)) == _dump(_alias(seen[1], mn)), name


# ---------------------------------------------------------------------------
# titles and brain calls
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["two_charts_full", "single_table_no_partial",
                                  "single_chart_narrative"])
def test_title_never_changes_nothing_but_the_title(harness, name):
    script = SCENARIOS[name]()
    web = harness.web(script)
    mcp = harness.mcp(script, title="never")
    compare(web, mcp, titles=False)
    assert len(web.titles) == 1, "the scenario must be one the web titles"
    assert mcp.titles == []


@pytest.mark.parametrize("name", ["two_charts_full", "single_chart_narrative",
                                  "error_generic_after_chart", "stop_after_first_chart"])
def test_no_brain_call_is_made_around_the_pipeline(harness, name):
    """With title="never" the only brain traffic of an MCP turn is the
    pipeline's own: not even the per-chart activity event (the MCP job posts
    that one)."""
    script = SCENARIOS[name]()
    web = harness.web(script)
    mcp = harness.mcp(script, title="never")
    assert set(web.brain) <= {"post_activity"}, web.brain
    if "chart" in name and not name.startswith("error"):
        assert web.brain, "the recorder is blind: the web posts an event per chart"
    assert mcp.brain == []


def test_the_real_pipeline_sends_the_brain_the_same_payloads(client, world, monkeypatch):
    """The REAL `run_chat_multi_plot` behind both paths, recorded at the
    transport: the same /v1/* calls with the same payloads (minus sid)."""
    posts = []

    def post(path, payload, sid, timeout=None):
        posts.append((path, {k: v for k, v in copy.deepcopy(payload).items() if k != "sid"}))
        if path == "/v1/plan":
            return {"kind": "PYTHON", "code": TCODE, "usage": {},
                    "raw_text": f"```python\n{TCODE}\n```"}
        return {"text": "Both regions sold.", "usage": {}}

    monkeypatch.setattr(brain_client, "_post", post)
    ct = _ct()
    h_conv = {}
    for which in ("web", "mcp"):
        conv_id = world.new_conversation("seeded")
        local_store.AuthStore().record_conversation(OWNER, CHAT, conv_id, "seeded")
        world.append_history(conv_id, {"role": "ai", "content": "42", "ts": 1.0,
                                       "code": "RESULT = 42", "table": _table(1)})
        posts.clear()
        if which == "web":
            r = client.post(f"/api/chat/{CHAT}/chat/stream",
                            json={"question": QUESTION, "conv_id": conv_id},
                            headers=JSON_HEADERS)
            assert r.status_code == 200, r.text[:300]
        else:
            turn = ct.prepare_turn(OWNER, CHAT, QUESTION, conv_id=conv_id)
            ct.run_prepared_turn(turn, title="never")
        h_conv[which] = (_dump(posts), _raw_rows(world, conv_id)[1:])
    assert "/v1/plan" in h_conv["web"][0], "the recorder is blind"
    assert h_conv["web"][0] == h_conv["mcp"][0]
    compare_rows(h_conv["web"][1], h_conv["mcp"][1])
    assert h_conv["mcp"][1][-1]["table"]["rows"], h_conv["mcp"][1][-1]


# ---------------------------------------------------------------------------
# the role gate, through the REAL helpers
# ---------------------------------------------------------------------------
def _add_snapshot_table(store, *, keep_file):
    """A registered SNAPSHOT table of the chat that OWNER's role does not
    cover (OWNER holds Base only)."""
    import db_sources
    import roles_store
    roles_store.RolesStore().ensure_base_role()
    reg = db_sources.DataSourceStore()
    cid = reg.create_connection({"name": "S", "db_type": "sqlite",
                                 "url_override": "sqlite+pysqlite:///x.db"},
                                "pw", actor="ladmin")["id"]
    doc = reg.upsert_table({
        "connection_id": cid, "schema": "", "table_name": "loans",
        "display_name": "Loans", "description": "", "is_connector": False,
        "relations": [], "columns": [{"name": "a", "dtype": "INTEGER",
                                      "description": "col a"}]}, actor="ladmin")
    pd.DataFrame({"a": [1, 2, 3]}).to_parquet(local_store.db_snapshot_path(doc["id"]))
    meta = store.read_meta()
    entry = {"file_name": "Loans", "file_description": "", "source": "database",
             "db": {"table_id": doc["id"], "connection_id": cid, "schema": "",
                    "table_name": "loans", "display_name": "Loans",
                    "is_connector": False, "auto_included": False, "row_count": None,
                    "refreshed_at": None, "relations": []},
             "schema": {"file_name": "Loans",
                        "fields": {"a": {"description": "col a", "values": None}}}}
    if not keep_file:
        (store.files_dir / "sales.csv").unlink()
        meta["files"] = []
    meta["files"].append(entry)
    store.write_meta(meta)
    local_store._DATAFRAME_CACHE.invalidate()
    return doc["id"]


def test_a_denied_table_never_reaches_the_planner_on_either_path(harness, world):
    _add_snapshot_table(world, keep_file=True)
    assert set(world.load_dataframes(include_live=True)) == {KEY, "Loans"}, \
        "the fixture's table must load, or the gate is not tested"
    script = SCENARIOS["single_text"]()
    web, mcp = harness.web(script), harness.mcp(script)
    compare(web, mcp)
    assert "Loans" not in mcp.calls[0]["dfs"]
    assert "Loans" not in mcp.calls[0]["schema_docs"]


def test_a_turn_with_every_table_denied_saves_the_denial_on_both(harness, world):
    _add_snapshot_table(world, keep_file=False)
    assert set(world.load_dataframes(include_live=True)) == {"Loans"}
    script = [_single("the planner must not run")]
    web, mcp = harness.web(script), harness.mcp(script)
    compare(web, mcp)
    assert web.calls == mcp.calls == []
    assert "no longer have access to Loans" in mcp.rows[-1]["content"]
    assert mcp.result.error_text is None and mcp.result.persisted is True


# ---------------------------------------------------------------------------
# the MCP record on the web
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("email,expected", [(FRIEND, 200), (OWNER, 200),
                                            (OTHER, 403), (STRANGER, 403)])
def test_the_mcp_record_of_a_recipient_loads_through_the_web_history(harness, email, expected):
    """FRIEND's MCP conversation: FRIEND (own index) and the chat's owner read
    it, another recipient and a stranger do not."""
    mcp = harness.mcp(SCENARIOS["two_charts_full"](), email=FRIEND)
    assert mcp.served[-1]["images"] and mcp.served[-1]["narrative_rendered"] == NR
    assert mcp.served[-1]["tables"] and all(mcp.served[-1]["full_table_keys"])
    harness.login(email)
    r = harness.client.get(f"/api/chat/{CHAT}/conversation/{mcp.conv_id}/history")
    assert r.status_code == expected, r.text[:200]
    if expected == 200:
        row = r.json()["history"][-1]
        assert [bool(i["image_base64"]) for i in row["images"]] == [True, True]


def test_new_conversation_by_factory_leaves_what_the_web_leaves(harness, world):
    """Option A needs this: with a factory doing the web's own steps, a turn
    without a conversation id leaves the same rows (welcome included)."""
    ct = _ct()
    script = SCENARIOS["single_table_no_partial"]()
    harness.login(OWNER)

    def index():
        return [r["conv_id"] for r in local_store.AuthStore().list_conversations(OWNER)]

    def go_web(run):
        known = set(index())
        r = harness.client.post(f"/api/chat/{CHAT}/chat/stream",
                                json={"question": QUESTION}, headers=JSON_HEADERS)
        assert r.status_code == 200, r.text[:300]
        run.conv_id = next(c for c in index() if c not in known)

    def factory(store):
        conv_id = store.new_conversation(title=QUESTION[:80])
        local_store.AuthStore().record_conversation(OWNER, CHAT, conv_id,
                                                    title=QUESTION[:80])
        welcome = store.get_welcome()
        if welcome:
            store.append_history(conv_id, {"role": "ai", "content": welcome, "ts": 1.0})
        return conv_id

    def go_mcp(run):
        turn = ct.prepare_turn(OWNER, CHAT, QUESTION, new_conversation=factory)
        run.result = ct.run_prepared_turn(turn)
        run.conv_id = run.result.conv_id

    runs = []
    for go in (go_web, go_mcp):
        harness.script = script
        harness.current = run = Run()
        go(run)
        run.rows = _raw_rows(world, run.conv_id)
        runs.append(run)
    harness.current = None
    web, mcp = runs
    compare_rows(web.rows, mcp.rows)
    assert [r["role"] for r in mcp.rows] == ["ai", "human", "ai"]
    # the planner got the same, the welcome row's own timestamp aside
    blind = lambda run: [{k: re.sub(r'"ts": [0-9.]+', '"ts": 0', v)      # noqa: E731
                          if isinstance(v, str) else v for k, v in c.items()}
                         for c in run.calls]
    assert blind(web) == blind(mcp)
    assert len(mcp.calls) == 1 and "Welcome to Sales." in mcp.calls[0]["history_rows"]


# ===========================================================================
# the pin of the browser handler's code
# ===========================================================================
# sha256 over the tokens of `routes.chat.chat_stream` (comments, blank lines,
# line wrapping inside brackets, newline style and indentation width do not
# count). It changes when somebody edits the handler: then mirror the change
# in chat_turn.py, add a scenario here, and put the new value in.
CHAT_STREAM_PIN = "cbd5e121617b27fabb89319cd6596b6c594a9190b62459258740e93daaedf732"
PIN_MESSAGE = ("chat_stream changed: mirror it in chat_turn.py, add a scenario to "
               "this file, then update this pin.")


def code_hash(source: str) -> str:
    """The hash of a function's source tokens, the same on every Python: an
    f-string counts as its source text (one token up to 3.11, several from
    3.12 on)."""
    source = textwrap.dedent(source.replace("\r\n", "\n").replace("\r", "\n"))
    lines = source.splitlines(keepends=True)
    parts, depth, start = [], 0, None
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        name = tokenize.tok_name[tok.type]
        if name in ("FSTRING_START", "TSTRING_START"):
            depth += 1
            start = tok.start if depth == 1 else start
        elif name in ("FSTRING_END", "TSTRING_END"):
            depth -= 1
            if depth == 0:
                (r1, c1), (r2, c2) = start, tok.end
                text = lines[r1 - 1][c1:c2] if r1 == r2 else "".join(
                    [lines[r1 - 1][c1:], *lines[r1:r2 - 1], lines[r2 - 1][:c2]])
                parts.append(text)
        elif depth or name in ("COMMENT", "NL", "ENCODING", "ENDMARKER"):
            continue
        elif name in ("NEWLINE", "INDENT", "DEDENT"):
            parts.append(f"<{name}>")       # the structure, never the width
        else:
            parts.append(tok.string)
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _chat_stream_source() -> str:
    return inspect.getsource(_chat_mod().chat_stream)


def test_the_code_of_chat_stream_is_the_one_chat_turn_mirrors():
    found = code_hash(_chat_stream_source())
    assert found == CHAT_STREAM_PIN, f"{PIN_MESSAGE} (now {found})"


def _edited(source, old, new):
    assert source.count(old) >= 1, f"the meta-check needs {old!r} in chat_stream"
    return source.replace(old, new, 1)


@pytest.mark.parametrize("old,new", [
    ("if _is_cancelled(conv_id):", "if not _is_cancelled(conv_id):"),
    ('w_tbl_src["narrative_view"] = w_view', "pass"),
    ('"role": "human"', '"role": "user"'),
    ("TITLE_TRIGGER_FAILED", "TITLE_FAILED"),                    # inside an f-string
    ("{log_safe_text(str(e), 200)}", "{log_safe_text(str(e), 20)}"),   # its expression
    ("w_tables.extend(_tbls)", "w_tables.append(_tbls)"),
])
def test_meta_a_code_edit_changes_the_pin(old, new):
    source = _chat_stream_source()
    assert code_hash(_edited(source, old, new)) != code_hash(source)


def test_meta_a_moved_statement_changes_the_pin():
    """Indentation WIDTH does not count, the block structure does."""
    a = "def f():\n    if x:\n        g()\n        h()\n"
    b = "def f():\n    if x:\n        g()\n    h()\n"
    assert code_hash(a) != code_hash(b)


def test_meta_a_comment_or_layout_edit_does_not_change_the_pin():
    source = _chat_stream_source()
    base = code_hash(source)
    mark = "    store = local_store.ChatDataStore(chat_id)\n"
    assert source.count(mark) == 1
    comment = next(ln for ln in source.splitlines(keepends=True) if ln.strip().startswith("#"))
    wide = "".join(" " * (len(ln) - len(ln.lstrip(" "))) + ln
                   for ln in source.splitlines(keepends=True))
    for what, text in {
        "comment added": source.replace(mark, "    # a new comment\n" + mark),
        "comment removed": source.replace(comment, "", 1),
        "trailing comment": source.replace(mark, mark.rstrip("\n") + "   # why\n"),
        "blank lines": source.replace(mark, "\n\n" + mark + "\n"),
        "CRLF": source.replace("\n", "\r\n"),
        "wider indentation": wide,
        "line wrapped in brackets": source.replace(
            mark, "    store = local_store.ChatDataStore(\n        chat_id)\n"),
    }.items():
        assert text != source, what
        assert code_hash(text) == base, what


# ===========================================================================
# meta-checks: the comparison itself can fail
# ===========================================================================
def _control(harness):
    script = SCENARIOS["two_charts_full"]()
    return harness.web(script), harness.web(script)


def test_meta_the_browser_path_equals_itself(harness):
    a, b = _control(harness)
    compare(a, b)
    assert a.rows and a.events and a.calls and a.registry and a.records
    assert len(a.records) == 2, a.records.keys()
    assert all(isinstance(r, dict) and r["rows"] for r in a.records.values()), a.records
    assert a.end_state == CLEAN
    assert len(a.titles) == 1 and a.titles[0]["question"] == QUESTION
    # while running: the codes were in flight and the conversation "generating"
    assert "fig = chart_1" in a.registry[0] and "true" in a.registry[0]


def _edit(path):
    """An edit of the last row: walk `path` to a container, then apply."""
    *walk, (key, value) = path

    def apply(run):
        target = run.rows[-1]
        for step in walk:
            target = target[step]
        target[key] = value
    return apply


def _drop(key):
    return lambda run: run.rows[-1].pop(key)


def _reorder(run):
    row = run.rows[-1]
    first = next(iter(row))
    row[first] = row.pop(first)


def _reuse_key(run):
    keys = run.rows[-1]["full_table_keys"]
    keys[1] = keys[0]


def _extra_row(run):
    run.rows.append(dict(run.rows[-1]))


def _other_record(run):
    run.records[list(run.records)[0]]["code"] = "RESULT = 1"


ALTERATIONS = {
    "content": _edit([("content", "altered")]),
    "chart answer": _edit(["images", 1, ("answer", "altered")]),
    "chart order": lambda run: run.rows[-1]["images"].reverse(),
    "extra chart key": _edit(["images", 0, ("extra", 1)]),
    "missing key": _drop("narrative_rendered"),
    "missing usage": _drop("usage"),
    "extra key": _edit([("surprise", None)]),
    "key order": _reorder,
    "usage": _edit([("usage", {"total_tokens": 1})]),
    "table row": _edit(["tables", 0, "rows", 0, ("amount", 777)]),
    "caption array": lambda run: run.rows[-1]["table_captions"].__setitem__(0, "x"),
    "a table key reused": _reuse_key,
    "ts as int": _edit([("ts", 5)]),
    "ts missing": _drop("ts"),
    "question": lambda run: run.rows[0].__setitem__("content", "altered"),
    "second AI row": _extra_row,
    "no AI row": lambda run: run.rows.pop(),
    "durable record": _other_record,
    "durable record missing": lambda run: run.records.__setitem__(
        list(run.records)[0], None),
    "one more table file": lambda run: setattr(run, "new_files", run.new_files + 1),
    "planner argument": lambda run: run.calls[0].__setitem__("question", '"x"'),
    "planner argument missing": lambda run: run.calls[0].pop("dataset_profile"),
    "planner not called": lambda run: run.calls.clear(),
    "event dropped": lambda run: run.events.pop(),
    "event payload": lambda run: run.events[-1][1].__setitem__("display_answer", "x"),
    "event tag": lambda run: run.events.__setitem__(-1, ("error", run.events[-1][1])),
    "in flight registry": lambda run: run.registry.__setitem__(0, "[null, null, false]"),
    "left generating": lambda run: run.end_state.__setitem__("generating", True),
    "title": lambda run: run.titles.clear(),
    "log marker": lambda run: run.markers.append("CHAT_THREAD_ERROR"),
    "served row": lambda run: run.served[-1].__setitem__("content", "altered"),
}


@pytest.mark.parametrize("what", sorted(ALTERATIONS))
def test_meta_an_altered_turn_is_detected(harness, what):
    """Without this the drift test could pass by comparing nothing."""
    a, b = _control(harness)
    compare(a, b)
    ALTERATIONS[what](b)
    with pytest.raises((AssertionError, KeyError)):
        compare(a, b)


def test_meta_a_changed_script_is_detected(harness):
    """The whole chain: a pipeline difference ends in a failed comparison."""
    script = _two_charts_full()
    a = harness.web(script)
    changed = copy.deepcopy(script)
    next(s for s in changed if isinstance(s, dict) and s.get("caption"))["text"] = "Other."
    b = harness.web(changed)
    with pytest.raises(AssertionError):
        compare_rows(a.rows, b.rows)


def test_meta_a_changed_mcp_record_builder_is_detected(harness, monkeypatch):
    """A record altered on ONE side only (as a real drift would be)."""
    chat_mod = harness.chat_mod
    script = SCENARIOS["two_charts_full"]()
    web = harness.web(script)
    real = chat_mod._build_ai_history_record

    def altered(*a, **k):
        rec = real(*a, **k)
        rec["images"][0]["answer"] = ""
        return rec

    monkeypatch.setattr(chat_mod, "_build_ai_history_record", altered)
    mcp = harness.mcp(script)
    with pytest.raises(AssertionError, match="saved history rows differ"):
        compare(web, mcp)
