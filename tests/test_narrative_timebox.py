"""Narrative follow-up, item 2 — the narrative-only retry is time-boxed to 45 s
and announced to the page (DIAGNOSTICS_SPEC A5.1 / A5.4 / A6).

Client PR #27 run 3: the narrative-only retry took 120 s while the page showed
nothing. Now:
- `run_chat_local.NARRATIVE_RETRY_TIMEOUT_S == 45.0`; both narrative-only
  retry closures call `brain_client.retry(..., timeout=45.0)`, which passes it
  to `_post` (the ordinary exec-error retries keep the client-wide timeout);
- a `brain_client.BrainTimeoutError` from that retry logs
  `NARRATIVE_RETRY_TIMEOUT elapsed_ms=<int> timeout_s=45`, gives no narrative,
  and the answer takes today's no-narrative path (describe for a visual,
  summarize + value line for a scalar);
- right BEFORE the retry, `run_chat_multi_plot(..., on_progress=cb)` calls
  `cb({"progress": True, "message_key": "lab.narrative_retry",
  "message": "Writing the summary…"})` — multi and single flow alike. A
  raising callback is logged and never breaks the answer.

Offline: brain calls are stubbed at `run_chat_local.brain_client.*`; the
`brain_client.retry` timeout pass-through uses the `_FakeClient` seam
(`brain_client._get_client`).
"""
import logging
import re

import httpx
import pandas as pd
import pytest

import brain_client
import run_chat_local
from settings import settings

CAPS = "narrative_v1,diagnostics_v1"
PROGRESS = {"progress": True, "message_key": "lab.narrative_retry",
            "message": "Writing the summary…"}
NESTED = "{{#a}}x{{#b}}y{{/b}}{{/a}}"
CODE_A = "fig = chart_a\nVALUES['a'] = 1"
CODE_FAILS = "fig = chart_fails_once"
CODE_FIXED = "fig = chart_fixed\nVALUES['b'] = 2"
REPLY_CODE = "fig = chart_reply_never_run"


# ---------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", CAPS)


@pytest.fixture
def dfs():
    return {"f.csv": pd.DataFrame({"region": ["North", "South"], "amount": [10, 20]})}


def _msgs(caplog):
    return [r.getMessage() for r in caplog.records]


class Brain:
    """Recording stubs; `order` is shared with the progress callback so the
    test can see what happened first."""

    def __init__(self, plan, retry=None, order=None):
        self.plan_reply = plan
        self.retry_reply = retry
        self.order = order if order is not None else []
        self.calls = {"retry": [], "describe": [], "summarize": []}

    def install(self, monkeypatch):
        bc = run_chat_local.brain_client

        def retry(**kw):
            self.calls["retry"].append(kw)
            self.order.append(("retry", str(kw.get("error_msg", ""))))
            r = self.retry_reply
            if callable(r):
                return r(kw)
            return dict(r or {"kind": "PLOT_CODE", "code": REPLY_CODE, "usage": {}})

        def describe(**kw):
            self.calls["describe"].append(kw)
            return {"text": "described", "usage": {}}

        def summarize(**kw):
            self.calls["summarize"].append(kw)
            return {"text": "summarized", "usage": {}}

        monkeypatch.setattr(bc, "plan", lambda **kw: dict(self.plan_reply))
        monkeypatch.setattr(bc, "retry", retry)
        monkeypatch.setattr(bc, "describe", describe)
        monkeypatch.setattr(bc, "summarize", summarize)
        return self

    def narr_retries(self):
        return [kw for kw in self.calls["retry"]
                if str(kw.get("error_msg", "")).startswith("narrative_invalid")]

    def exec_retries(self):
        narr = self.narr_retries()
        return [kw for kw in self.calls["retry"] if kw not in narr]


def _multi_plan(*codes, template=None):
    raw = "\n###NEXT_PLOT###\n".join(f"```plot_code\n{c}\n```" for c in codes)
    out = {"raw_text": raw + "\n###NEXT_PLOT###\n", "kind": "PLOT_CODE", "code": "",
           "usage": {}}
    if template is not None:
        out["narrative"] = template
    return out


def _single_plan(code, template):
    return {"kind": "PYTHON", "code": code, "raw_text": "", "usage": {}, "narrative": template}


def _chart(values):
    return {"ok": True, "error": None, "is_plotly": True, "plotly_html": "<div>c</div>",
            "chart_data": None, "values": dict(values), "values_dropped": 0,
            "findings": {}, "findings_dropped": 0}


def _table(values):
    return {"error": None, "result": pd.DataFrame({"region": ["N", "S"], "amount": [1, 2]}),
            "preview": None, "image_base64": None, "values": dict(values),
            "values_dropped": 0, "findings": {}, "findings_dropped": 0}


def _install_renders(monkeypatch, charts=None, tables=None):
    def render(code, dfs, sid, **kw):
        out = (charts or {}).get(code)
        return dict(out) if out is not None else {"ok": False, "error": "ValueError: boom",
                                                  "trace": ""}

    def execute(code, dfs, sid, **kw):
        out = (tables or {}).get(code)
        return dict(out) if out is not None else {"error": "ValueError: boom"}

    monkeypatch.setattr(run_chat_local, "render_plot_safe", render)
    monkeypatch.setattr(run_chat_local, "safe_execute", execute)


def _run(dfs, **kw):
    return list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question="show the totals",
        history_rows=[], user_email="alice@acme.com", **kw))


def _done(events):
    done = [e for e in events if e.get("done")]
    assert len(done) == 1, events
    return done[0]


def _single(events):
    assert len(events) == 1 and events[0].get("single_response"), events
    return events[0]["result"]


def _timeout_retry(kw):
    if str(kw.get("error_msg", "")).startswith("narrative_invalid"):
        raise brain_client.BrainTimeoutError("Brain timed out: ReadTimeout")
    return {"kind": "PLOT_CODE", "code": CODE_FIXED, "usage": {}}


# ======================================================================
# the constant and the brain_client pass-through
# ======================================================================

def test_narrative_retry_timeout_constant():
    assert getattr(run_chat_local, "NARRATIVE_RETRY_TIMEOUT_S", None) == 45.0


class _FakeResp:
    status_code = 200
    text = ""

    def json(self):
        return {"kind": "PLOT_CODE", "code": "fig = x", "usage": {}}


class _FakeClient:
    def __init__(self):
        self.posts = []

    def post(self, path, json=None, headers=None, timeout=None):
        self.posts.append({"path": path, "timeout": timeout})
        return _FakeResp()


def _retry_args():
    return dict(sid="s", question="q", schema_text="t", df_names=["df"],
                df_columns={"df": ["a"]}, history_rows=[], error_msg="e", failed_code="c")


def test_brain_client_retry_passes_timeout_to_post(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(brain_client, "_get_client", lambda: fake)
    monkeypatch.setattr(brain_client.settings, "BRAIN_TENANT_TOKEN", "tkn", raising=False)

    brain_client.retry(**_retry_args(), timeout=45.0)
    brain_client.retry(**_retry_args())

    assert [p["path"] for p in fake.posts] == ["/v1/retry", "/v1/retry"]
    assert fake.posts[0]["timeout"] == 45.0
    # no timeout -> the client-wide BRAIN_REQUEST_TIMEOUT, as before
    assert fake.posts[1]["timeout"] is httpx.USE_CLIENT_DEFAULT


def test_brain_client_retry_timeout_reaches_post_kwarg(monkeypatch):
    seen = []

    def fake_post(path, payload, sid, timeout=None):
        seen.append((path, timeout, "timeout" in payload))
        return {"kind": "PLOT_CODE", "code": "x", "usage": {}}

    monkeypatch.setattr(brain_client, "_post", fake_post)
    brain_client.retry(**_retry_args(), timeout=45.0)
    brain_client.retry(**_retry_args())
    # the timeout is a transport setting, never a payload field
    assert seen == [("/v1/retry", 45.0, False), ("/v1/retry", None, False)]


# ======================================================================
# the closures pass timeout=45.0; exec-error retries do not
# ======================================================================

def test_multi_narrative_retry_passes_timeout_exec_retry_does_not(monkeypatch, dfs):
    def retry(kw):
        if str(kw.get("error_msg", "")).startswith("narrative_invalid"):
            return {"kind": "PLOT_CODE", "code": REPLY_CODE, "usage": {},
                    "narrative": "Fixed {{a}}."}
        return {"kind": "PLOT_CODE", "code": CODE_FIXED, "usage": {}}

    brain = Brain(_multi_plan(CODE_A, CODE_FAILS, template=NESTED),
                  retry=retry).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart({"a": 1}),
                                          CODE_FIXED: _chart({"b": 2})})

    _run(dfs)

    narr, exec_ = brain.narr_retries(), brain.exec_retries()
    assert len(narr) == 1 and len(exec_) == 1
    assert narr[0].get("timeout") == 45.0
    assert exec_[0].get("timeout") is None


def test_single_narrative_retry_passes_timeout_exec_retry_does_not(monkeypatch, dfs):
    failing = "VALUES['a'] = 1\nRESULT = will_fail"
    fixed = "VALUES['a'] = 7\nRESULT = dfs['f.csv']"

    def retry(kw):
        if str(kw.get("error_msg", "")).startswith("narrative_invalid"):
            return {"kind": "PYTHON", "code": "RESULT = 0", "usage": {},
                    "narrative": "Value {{a}}."}
        return {"kind": "PYTHON", "code": fixed, "usage": {}}

    brain = Brain(_single_plan(failing, NESTED), retry=retry).install(monkeypatch)
    _install_renders(monkeypatch, tables={fixed: _table({"a": 7})})

    result = _single(_run(dfs))

    narr, exec_ = brain.narr_retries(), brain.exec_retries()
    assert len(narr) == 1 and len(exec_) == 1, [kw.get("error_msg") for kw in brain.calls["retry"]]
    assert narr[0].get("timeout") == 45.0
    assert exec_[0].get("timeout") is None
    assert result.get("narrative_rendered") == "Value 7."


# ======================================================================
# a timeout -> NARRATIVE_RETRY_TIMEOUT, no narrative, today's path
# ======================================================================

_TIMEOUT_LINE = re.compile(r"NARRATIVE_RETRY_TIMEOUT elapsed_ms=\d+ timeout_s=45(?!\d)")


def test_multi_narrative_retry_timeout_falls_back_to_describe(monkeypatch, dfs, caplog):
    brain = Brain(_multi_plan(CODE_A, template=NESTED), retry=_timeout_retry).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart({"a": 1})})

    with caplog.at_level(logging.INFO, logger="datachat"):
        events = _run(dfs)

    assert len(brain.narr_retries()) == 1
    assert [e for e in events if e.get("narrative")] == []
    done = _done(events)
    assert done["display_answer"] == "described"
    assert done["combined_answer"] == "described"
    msgs = _msgs(caplog)
    lines = [m for m in msgs if "NARRATIVE_RETRY_TIMEOUT" in m]
    assert len(lines) == 1 and _TIMEOUT_LINE.search(lines[0]), msgs
    assert not any("NARRATIVE_RETRY_FAILED" in m for m in msgs), msgs
    assert any("NARRATIVE_INVALID_TWICE reason=no_narrative" in m for m in msgs), msgs


def test_single_table_narrative_retry_timeout_falls_back_to_describe(monkeypatch, dfs, caplog):
    code = "VALUES['a'] = 3\nRESULT = dfs['f.csv']"
    brain = Brain(_single_plan(code, NESTED), retry=_timeout_retry).install(monkeypatch)
    _install_renders(monkeypatch, tables={code: _table({"a": 3})})

    with caplog.at_level(logging.INFO, logger="datachat"):
        result = _single(_run(dfs))

    assert len(brain.narr_retries()) == 1
    assert len(brain.calls["describe"]) == 1
    assert result["text"] == "described"
    assert "narrative_rendered" not in result and "narrative_template" not in result
    msgs = _msgs(caplog)
    lines = [m for m in msgs if "NARRATIVE_RETRY_TIMEOUT" in m]
    assert len(lines) == 1 and _TIMEOUT_LINE.search(lines[0]), msgs
    assert not any("NARRATIVE_RETRY_FAILED" in m for m in msgs), msgs


def test_single_scalar_narrative_retry_timeout_falls_back_to_summarize(monkeypatch, dfs, caplog):
    brain = Brain(_single_plan("RESULT = 42", "{{missing}}"),
                  retry=_timeout_retry).install(monkeypatch)

    with caplog.at_level(logging.INFO, logger="datachat"):
        result = _single(_run(dfs))

    assert len(brain.narr_retries()) == 1
    assert len(brain.calls["summarize"]) == 1
    assert result["text"] == "summarized"
    assert result["value_display"] == "**Result:** 42"
    assert "narrative_rendered" not in result
    lines = [m for m in _msgs(caplog) if "NARRATIVE_RETRY_TIMEOUT" in m]
    assert len(lines) == 1 and _TIMEOUT_LINE.search(lines[0]), _msgs(caplog)


# ======================================================================
# progress before the narrative-only retry
# ======================================================================

def _recorder(order):
    seen = []

    def cb(ev):
        seen.append(ev)
        order.append(("progress", dict(ev)))

    return seen, cb


def test_multi_progress_emitted_right_before_narrative_retry(monkeypatch, dfs):
    order = []
    seen, cb = _recorder(order)
    Brain(_multi_plan(CODE_A, template=NESTED),
          retry={"kind": "PLOT_CODE", "code": REPLY_CODE, "usage": {},
                 "narrative": "Fixed {{a}}."}, order=order).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart({"a": 1})})

    events = _run(dfs, on_progress=cb)

    assert seen == [PROGRESS]
    assert seen[0]["message"].endswith("…")
    assert [o[0] for o in order] == ["progress", "retry"]
    assert order[1][1].startswith("narrative_invalid")
    assert [e["text"] for e in events if e.get("narrative")] == ["Fixed 1."]


def test_single_progress_emitted_right_before_narrative_retry(monkeypatch, dfs):
    order = []
    seen, cb = _recorder(order)
    code = "VALUES['a'] = 3\nRESULT = dfs['f.csv']"
    Brain(_single_plan(code, NESTED),
          retry={"kind": "PYTHON", "code": "RESULT = 0", "usage": {},
                 "narrative": "Rows {{a}}."}, order=order).install(monkeypatch)
    _install_renders(monkeypatch, tables={code: _table({"a": 3})})

    result = _single(_run(dfs, on_progress=cb))

    assert seen == [PROGRESS]
    assert [o[0] for o in order] == ["progress", "retry"]
    assert result["narrative_rendered"] == "Rows 3."


def test_progress_before_timed_out_retry_too(monkeypatch, dfs):
    order = []
    seen, cb = _recorder(order)
    Brain(_multi_plan(CODE_A, template=NESTED), retry=_timeout_retry,
          order=order).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart({"a": 1})})

    events = _run(dfs, on_progress=cb)

    assert seen == [PROGRESS]
    assert [o[0] for o in order] == ["progress", "retry"]
    assert _done(events)["display_answer"] == "described"


def test_no_progress_without_narrative_retry(monkeypatch, dfs):
    """A valid template makes no narrative retry, and an ordinary exec-error
    retry is no reason for the progress line."""
    order = []
    seen, cb = _recorder(order)
    brain = Brain(_multi_plan(CODE_A, CODE_FAILS, template="Total {{a}} and {{b}}."),
                  retry={"kind": "PLOT_CODE", "code": CODE_FIXED, "usage": {}},
                  order=order).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart({"a": 1}),
                                          CODE_FIXED: _chart({"b": 2})})

    events = _run(dfs, on_progress=cb)

    assert brain.narr_retries() == [] and len(brain.exec_retries()) == 1
    assert seen == []
    assert [e["text"] for e in events if e.get("narrative")] == ["Total 1 and 2."]


def test_no_progress_single_without_narrative_retry(monkeypatch, dfs):
    seen, cb = _recorder([])
    code = "VALUES['a'] = 3\nRESULT = dfs['f.csv']"
    brain = Brain(_single_plan(code, "Rows {{a}}.")).install(monkeypatch)
    _install_renders(monkeypatch, tables={code: _table({"a": 3})})

    result = _single(_run(dfs, on_progress=cb))

    assert brain.calls["retry"] == []
    assert seen == []
    assert result["narrative_rendered"] == "Rows 3."


def test_on_progress_none_is_accepted(monkeypatch, dfs):
    brain = Brain(_multi_plan(CODE_A, template=NESTED),
                  retry={"kind": "PLOT_CODE", "code": REPLY_CODE, "usage": {},
                         "narrative": "Fixed {{a}}."}).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart({"a": 1})})

    events = _run(dfs, on_progress=None)

    assert len(brain.narr_retries()) == 1
    assert [e["text"] for e in events if e.get("narrative")] == ["Fixed 1."]


@pytest.mark.parametrize("flow", ["multi", "single"])
def test_raising_on_progress_is_logged_and_answer_kept(monkeypatch, dfs, caplog, flow):
    def cb(ev):
        raise RuntimeError("page gone SECRET_TEXT")

    if flow == "multi":
        brain = Brain(_multi_plan(CODE_A, template=NESTED),
                      retry={"kind": "PLOT_CODE", "code": REPLY_CODE, "usage": {},
                             "narrative": "Fixed {{a}}."}).install(monkeypatch)
        _install_renders(monkeypatch, charts={CODE_A: _chart({"a": 1})})
    else:
        code = "VALUES['a'] = 1\nRESULT = dfs['f.csv']"
        brain = Brain(_single_plan(code, NESTED),
                      retry={"kind": "PYTHON", "code": "RESULT = 0", "usage": {},
                             "narrative": "Fixed {{a}}."}).install(monkeypatch)
        _install_renders(monkeypatch, tables={code: _table({"a": 1})})

    with caplog.at_level(logging.INFO, logger="datachat"):
        events = _run(dfs, on_progress=cb)

    # the retry still ran and its narrative was used
    assert len(brain.narr_retries()) == 1
    if flow == "multi":
        assert [e["text"] for e in events if e.get("narrative")] == ["Fixed 1."]
        assert _done(events)["display_answer"] == "Fixed 1."
    else:
        assert _single(events)["narrative_rendered"] == "Fixed 1."
    warn = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("RuntimeError" in m for m in warn), warn
