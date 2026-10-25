"""Narrative follow-up, item 1 — the first-sentence slot cause (DIAGNOSTICS_SPEC
A5.1 "Invalid template", PROTOCOL `error_msg`).

Client PR #27 run 3: the plan's first sentence rendered "n/a" because its
headline slot (`uzs_amount`) was None, and the narrative-only retry said only
`narrative_invalid: first_sentence`, so the brain could not name the slot.
The client now names the FIRST slot that rendered "n/a" inside the first
sentence and why:

    error_msg = "narrative_invalid: first_sentence slot=<name> cause=<missing|none|nan>"

`missing` = the key is absent from VALUES, `none` = the value is None, `nan` =
a non-finite float. Any other reason for "n/a" (a literal "n/a" the template
typed, a fmt overflow such as a 1e40 count, an unsupported value type) keeps
the plain `narrative_invalid: first_sentence`. The slot NAME is a planner
literal of the template grammar (`narrative._TOKEN_RE`), never a data value,
and the message has no quotes, so `brain_client._scrub_error_msg` leaves it
byte-identical on the wire.

Offline: brain calls are stubbed (`run_chat_local.brain_client.*` for the flow
tests, `brain_client._post` for the wire test); chart blocks are stubbed per
code text (a real Plotly render reaches kaleido).
"""
import copy
import logging
import math

import pandas as pd
import pytest

import brain_client
import narrative
import run_chat_local
from settings import settings

CAPS = "narrative_v1,diagnostics_v1"


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


# ======================================================================
# narrative.first_sentence_na_slot
# ======================================================================

def _slot(template, values, findings=None, **kw):
    fn = getattr(narrative, "first_sentence_na_slot", None)
    assert callable(fn), "narrative.first_sentence_na_slot is missing"
    return fn(template, values, findings or {}, **kw)


@pytest.mark.parametrize("values,expected", [
    ({}, ("uzs_amount", "missing")),
    ({"uzs_amount": None}, ("uzs_amount", "none")),
    ({"uzs_amount": float("nan")}, ("uzs_amount", "nan")),
    ({"uzs_amount": float("inf")}, ("uzs_amount", "nan")),
    ({"uzs_amount": float("-inf")}, ("uzs_amount", "nan")),
])
def test_slot_cause_of_first_sentence(values, expected):
    t = "Credit issued: {{uzs_amount:amount}} UZS. Second sentence."
    assert _slot(t, values) == expected


def test_slot_cause_none_when_first_sentence_is_clean():
    t = "Credit issued: {{uzs_amount:amount}} UZS. Second sentence."
    assert _slot(t, {"uzs_amount": 1234567}) is None


def test_slot_cause_second_sentence_only_is_none():
    t = "Credit issued: {{a}}. The share is {{b}}."
    assert _slot(t, {"a": 5}) is None
    assert _slot(t, {"a": 5, "b": None}) is None


def test_slot_cause_first_failing_slot_wins():
    t = "A is {{x}} and B is {{y}}. Done."
    assert _slot(t, {"y": None}) == ("x", "missing")
    assert _slot(t, {"x": 1, "y": None}) == ("y", "none")
    assert _slot(t, {"x": float("nan"), "y": None}) == ("x", "nan")


@pytest.mark.parametrize("template,values", [
    # a fmt overflow: the count is longer than FMT_MAX_LEN -> n/a, no slot cause
    ("Total is {{n:count}}. Second.", {"n": 1e40}),
    # an unsupported value type renders n/a, no slot cause
    ("Total is {{n}}. Second.", {"n": [1, 2]}),
    # a literal "n/a" the template typed
    ("Total is n/a. Then {{n}}.", {"n": 3}),
    ("Total {{n}} is n/a today.", {"n": 3}),
])
def test_slot_cause_other_na_reasons_are_none(template, values):
    assert _slot(template, values) is None


def test_slot_cause_uses_first_non_empty_line():
    # the first non-blank line is the first sentence's line
    assert _slot("\n\nTotal {{a}}. Next.", {}) == ("a", "missing")
    # a clean first line: a missing slot on the next line is not the headline
    assert _slot("Intro line\nTotal {{a}}.", {}) is None


def test_slot_cause_section_shown_counts_hidden_does_not():
    t = "{{#rising}}Revenue rose to {{rev}}.{{/rising}} Then {{other}}."
    # shown: the slot inside the section is in the first sentence
    assert _slot(t, {"other": 1}, {"rising": True}) == ("rev", "missing")
    # hidden: the section is not rendered, so its slot never renders n/a;
    # the first sentence is then "Then n/a." -> `other`
    assert _slot(t, {}, {"rising": False}) == ("other", "missing")
    assert _slot(t, {"other": 1}, {"rising": False}) is None


def test_slot_cause_prefixed_section_resolves_through_passed_prefix():
    t = ("{{#ovl_metric_definition_overlap}}It counts {{stock}}."
         "{{/ovl_metric_definition_overlap}} Total {{t}}.")
    findings = {"metric_relation": "metric_definition_overlap"}
    # the prefix is PASSED: the section shows, its missing slot is the cause
    assert _slot(t, {"t": 1}, findings, prefixes={"ovl_"}) == ("stock", "missing")
    # not passed: the section is an unknown name (false), first sentence clean
    assert _slot(t, {"t": 1}, findings, prefixes=None) is None


def test_slot_cause_locale_kw_accepted():
    t = "Jami: {{uzs_amount:amount}}. Ikkinchi."
    assert _slot(t, {"uzs_amount": None}, locale="uz") == ("uzs_amount", "none")
    assert _slot(t, {"uzs_amount": 61576335238995.79}, locale="uz") is None


def test_slot_cause_never_raises_on_bad_template():
    # a template that does not parse is no slot verdict
    assert _slot("{{#a}}x{{#b}}y{{/b}}{{/a}}", {}) is None
    assert _slot(None, {}) is None


def test_slot_cause_agrees_with_first_sentence_has_na():
    """Whenever a slot cause is named, the rendered first sentence holds n/a."""
    cases = [("Total {{a}}. B.", {}), ("Total {{a}}. B.", {"a": None}),
             ("Total {{a}}. B.", {"a": math.nan}), ("A {{x}} and {{y}}. C.", {"x": 1})]
    for t, v in cases:
        assert _slot(t, v) is not None
        assert narrative.first_sentence_has_na(narrative.render(t, v, {}, sid="t"))


# ======================================================================
# narrative.invalid_error_msg
# ======================================================================

def _err(*a, **kw):
    fn = getattr(narrative, "invalid_error_msg", None)
    assert callable(fn), "narrative.invalid_error_msg is missing"
    return fn(*a, **kw)


@pytest.mark.parametrize("kind", ["nested", "unclosed", "mismatched", "first_sentence"])
def test_invalid_error_msg_plain(kind):
    assert _err(kind) == f"narrative_invalid: {kind}"
    assert _err(kind, None) == f"narrative_invalid: {kind}"


@pytest.mark.parametrize("cause", ["missing", "none", "nan"])
def test_invalid_error_msg_first_sentence_with_slot(cause):
    assert _err("first_sentence", ("uzs_amount", cause)) == \
        f"narrative_invalid: first_sentence slot=uzs_amount cause={cause}"
    assert _err("first_sentence", slot=("uzs_amount", cause)) == \
        f"narrative_invalid: first_sentence slot=uzs_amount cause={cause}"


def test_invalid_error_msg_slot_only_for_first_sentence():
    assert _err("nested", ("x", "none")) == "narrative_invalid: nested"


# ======================================================================
# _NarrState._checked_render: invalid_slot + the log line
# ======================================================================

def _state(template, values, lang="en"):
    st = run_chat_local._NarrState(phase=1, template=template, captions={},
                                   input_columns=frozenset(), sid="t", lang=lang)
    st.values = dict(values)
    st.ok_blocks = 1
    return st


@pytest.mark.parametrize("values,slot", [
    ({}, ("uzs_amount", "missing")),
    ({"uzs_amount": None}, ("uzs_amount", "none")),
    ({"uzs_amount": float("nan")}, ("uzs_amount", "nan")),
])
def test_checked_render_stores_slot_and_logs_it(caplog, values, slot):
    t = "Credit issued: {{uzs_amount:amount}}. Second."
    st = _state(t, values)
    with caplog.at_level(logging.INFO, logger="datachat"):
        kind, rendered, sections = st._checked_render(t)
    assert (kind, rendered, sections) == ("first_sentence", None, None)
    assert st.invalid_slot == slot
    lines = [m for m in _msgs(caplog) if "NARRATIVE_INVALID reason=first_sentence" in m]
    assert len(lines) == 1, _msgs(caplog)
    assert f"NARRATIVE_INVALID reason=first_sentence slot={slot[0]} cause={slot[1]}" in lines[0]


def test_checked_render_plain_reason_without_slot(caplog):
    t = "Total is n/a. Then {{a}}."
    st = _state(t, {"a": 1})
    with caplog.at_level(logging.INFO, logger="datachat"):
        kind, _r, _s = st._checked_render(t)
    assert kind == "first_sentence"
    assert getattr(st, "invalid_slot", "absent") is None
    lines = [m for m in _msgs(caplog) if "NARRATIVE_INVALID reason=first_sentence" in m]
    assert len(lines) == 1
    assert "slot=" not in lines[0] and "cause=" not in lines[0]


def test_checked_render_valid_template_has_no_slot():
    t = "Total is {{a}}. Second {{b}}."
    st = _state(t, {"a": 1})
    kind, rendered, _s = st._checked_render(t)
    assert kind is None and rendered.startswith("Total is 1.")
    assert getattr(st, "invalid_slot", "absent") is None


# ======================================================================
# Flows: the narrative-only retry carries the slot cause
# ======================================================================

CODE_A = "fig = chart_a\nVALUES['uzs_amount'] = maybe_none"
REPLY_CODE = "fig = chart_reply_never_run"


class Brain:
    """Recording stubs for plan / retry / describe / summarize."""

    def __init__(self, plan, retry=None):
        self.plan_reply = plan
        self.retry_reply = retry
        self.calls = {"plan": [], "retry": [], "describe": [], "summarize": []}

    def install(self, monkeypatch):
        bc = run_chat_local.brain_client

        def retry(**kw):
            self.calls["retry"].append(kw)
            r = self.retry_reply
            if callable(r):
                return r(kw)
            return dict(r or {"kind": "PLOT_CODE", "code": REPLY_CODE, "usage": {}})

        monkeypatch.setattr(bc, "plan", lambda **kw: (self.calls["plan"].append(kw),
                                                      dict(self.plan_reply))[1])
        monkeypatch.setattr(bc, "retry", retry)
        monkeypatch.setattr(bc, "describe", lambda **kw: (self.calls["describe"].append(kw),
                                                          {"text": "described",
                                                           "usage": {}})[1])
        monkeypatch.setattr(bc, "summarize", lambda **kw: (self.calls["summarize"].append(kw),
                                                           {"text": "summarized",
                                                            "usage": {}})[1])
        return self

    def narr_retries(self):
        return [kw for kw in self.calls["retry"]
                if str(kw.get("error_msg", "")).startswith("narrative_invalid")]


def _multi_plan(code, template):
    raw = f"```plot_code\n{code}\n```\n###NEXT_PLOT###\n"
    return {"raw_text": raw, "kind": "PLOT_CODE", "code": "", "usage": {},
            "narrative": template}


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


def _run(dfs):
    return list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question="how much credit was issued?",
        history_rows=[], user_email="alice@acme.com"))


TEMPLATE_UZS = "Credit issued: {{uzs_amount:amount}} UZS. It is the latest month."
CAUSE_CASES = [
    ({}, "missing"),
    ({"uzs_amount": None}, "none"),
    ({"uzs_amount": float("nan")}, "nan"),
]


@pytest.mark.parametrize("values,cause", CAUSE_CASES)
def test_multi_narrative_retry_names_slot_cause(monkeypatch, dfs, caplog, values, cause):
    brain = Brain(_multi_plan(CODE_A, TEMPLATE_UZS)).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart(values)})

    with caplog.at_level(logging.INFO, logger="datachat"):
        events = _run(dfs)

    narr = brain.narr_retries()
    assert len(narr) == 1, [kw.get("error_msg") for kw in brain.calls["retry"]]
    assert narr[0]["error_msg"] == \
        f"narrative_invalid: first_sentence slot=uzs_amount cause={cause}"
    assert any(f"NARRATIVE_INVALID reason=first_sentence slot=uzs_amount cause={cause}" in m
               for m in _msgs(caplog)), _msgs(caplog)
    assert [e for e in events if e.get("done")]


@pytest.mark.parametrize("values,cause", CAUSE_CASES)
def test_single_narrative_retry_names_slot_cause(monkeypatch, dfs, values, cause):
    code = "VALUES['uzs_amount'] = maybe_none\nRESULT = dfs['f.csv']"
    plan = {"kind": "PYTHON", "code": code, "raw_text": "", "usage": {},
            "narrative": TEMPLATE_UZS}
    brain = Brain(plan, retry={"kind": "PYTHON", "code": "RESULT = 1",
                               "usage": {}}).install(monkeypatch)
    _install_renders(monkeypatch, tables={code: _table(values)})

    events = _run(dfs)

    assert len(events) == 1 and events[0].get("single_response")
    narr = brain.narr_retries()
    assert len(narr) == 1, [kw.get("error_msg") for kw in brain.calls["retry"]]
    assert narr[0]["error_msg"] == \
        f"narrative_invalid: first_sentence slot=uzs_amount cause={cause}"


def test_multi_literal_na_keeps_plain_error_msg(monkeypatch, dfs):
    brain = Brain(_multi_plan(CODE_A, "Total is n/a. Then {{uzs_amount}}.")).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart({"uzs_amount": 5})})

    _run(dfs)

    narr = brain.narr_retries()
    assert len(narr) == 1
    assert narr[0]["error_msg"] == "narrative_invalid: first_sentence"


def test_multi_parse_error_keeps_plain_error_msg(monkeypatch, dfs):
    brain = Brain(_multi_plan(CODE_A, "{{#a}}x{{#b}}y{{/b}}{{/a}}")).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart({"uzs_amount": None})})

    _run(dfs)

    narr = brain.narr_retries()
    assert len(narr) == 1
    assert narr[0]["error_msg"] == "narrative_invalid: nested"


# ======================================================================
# Wire: the error_msg reaches /v1/retry byte-identical (real retry builder,
# only `brain_client._post` replaced)
# ======================================================================

@pytest.mark.parametrize("cause", ["missing", "none", "nan"])
def test_scrub_error_msg_leaves_slot_cause_unchanged(cause):
    msg = f"narrative_invalid: first_sentence slot=uzs_amount cause={cause}"
    assert brain_client._scrub_error_msg(msg, frozenset(), CODE_A) == msg
    assert brain_client._scrub_error_msg(msg, frozenset({"region"}), None) == msg


@pytest.mark.parametrize("values,cause", CAUSE_CASES)
def test_slot_cause_error_msg_on_the_wire(monkeypatch, dfs, values, cause):
    _install_renders(monkeypatch, charts={CODE_A: _chart(values)})
    posts = []

    def fake_post(path, payload, sid, **kw):
        posts.append((path, copy.deepcopy(payload), dict(kw)))
        if path == "/v1/plan":
            return _multi_plan(CODE_A, TEMPLATE_UZS)
        if path == "/v1/retry":
            return {"kind": "PLOT_CODE", "code": REPLY_CODE, "usage": {}}
        if path == "/v1/describe":
            return {"text": "A chart.", "usage": {}}
        return {}

    monkeypatch.setattr(brain_client, "_post", fake_post)

    _run(dfs)

    retries = [(p, kw) for path, p, kw in posts if path == "/v1/retry"]
    assert len(retries) == 1, [p.get("error_msg") for p, _kw in retries]
    payload, _kw = retries[0]
    assert payload["error_msg"] == \
        f"narrative_invalid: first_sentence slot=uzs_amount cause={cause}"
