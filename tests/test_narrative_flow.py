"""Narrative session 1, PR B — the `run_chat_local` flows (DIAGNOSTICS_SPEC
2.1 / 2.2, A1.5, A3.2, A5.1-A5.4, A6, B7.4; C12 session 1 test list).

The brain writes the answer as a TEMPLATE (`narrative`, `captions`) next to
the code; the client fills it locally from the VALUES the code set, so no
result value reaches the brain (Article II). These tests drive
`run_chat_local.run_chat_multi_plot` (and `run_chat`, the Auto Analytics
entry) with every brain call stubbed (`run_chat_local.brain_client.{plan,
retry,describe,summarize}`, the tests/test_describe_backstop.py idiom).

Single-flow answers run their generated code for real (the conftest runs it
in-process). Multi-flow chart blocks are stubbed per code text, because a
real Plotly render reaches kaleido, which hangs on the dev box.

Readings chosen where the spec leaves room (also listed in the PR B report):
- tables of a mixed answer may ride a tables partial OR the done event; the
  helper `_table_view` reads both;
- with no successful block the user-facing `display_answer` is today's
  failure text (it must not be blank);
- the backstop language is the question's (`_detect_answer_language`).

Runs offline: `.venv/Scripts/python.exe -m pytest tests/test_narrative_flow.py -q`.
"""
import logging

import pandas as pd
import pytest

import code_exec
import narrative
import result_backstop
import run_chat_local
import settings as settings_mod
from settings import settings

CAPS = "narrative_v1,diagnostics_v1"
FAIL_TEXT = "Something went wrong with this analysis. Please try again."


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def dfs():
    return {"f.csv": pd.DataFrame({"region": ["North", "South", "East"],
                                   "amount": [10, 20, 30]})}


@pytest.fixture(autouse=True)
def _stub_schema(monkeypatch):
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")


@pytest.fixture(autouse=True)
def _caps_on(monkeypatch):
    # brain_capabilities() reads the attribute at call time, whatever name the
    # flow imports it under.
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", CAPS)


def _caps():
    return list(settings_mod.brain_capabilities())


# ----------------------------------------------------------------- helpers

def _raw(*blocks):
    """A ###NEXT_PLOT### planner reply. `blocks` = (fence, code) pairs; a
    trailing delimiter keeps even one block on the multi path."""
    body = "\n###NEXT_PLOT###\n".join(f"```{fence}\n{code}\n```" for fence, code in blocks)
    return body + "\n###NEXT_PLOT###\n"


def _multi_plan(*blocks, narrative_t=None, captions=None):
    out = {"raw_text": _raw(*blocks), "kind": "PLOT_CODE", "code": "", "usage": {}}
    if narrative_t is not None:
        out["narrative"] = narrative_t
    if captions is not None:
        out["captions"] = captions
    return out


def _chart(values=None, findings=None, chart_data=None):
    """A successful Plotly render (the `_render_in_process` success shape)."""
    return {"ok": True, "error": None, "is_plotly": True,
            "plotly_html": "<div>chart</div>", "chart_data": chart_data,
            "values": dict(values or {}), "values_dropped": 0,
            "findings": dict(findings or {}), "findings_dropped": 0}


def _table(values=None, findings=None, df=None):
    """A successful python block whose RESULT is a table."""
    frame = df if df is not None else pd.DataFrame({"region": ["North", "South"],
                                                    "amount": [1, 2]})
    return {"error": None, "result": frame, "preview": None, "image_base64": None,
            "values": dict(values or {}), "values_dropped": 0,
            "findings": dict(findings or {}), "findings_dropped": 0}


class Brain:
    """Recording stubs for the four brain calls the flows make."""

    def __init__(self, plan, retry=None, describe_text="described",
                 summarize_text="summarized"):
        self.plan_reply = plan
        self.retry_reply = retry
        self.describe_text = describe_text
        self.summarize_text = summarize_text
        self.calls = {"plan": [], "retry": [], "describe": [], "summarize": []}

    def install(self, monkeypatch):
        bc = run_chat_local.brain_client

        def plan(**kw):
            self.calls["plan"].append(kw)
            return dict(self.plan_reply)

        def retry(**kw):
            self.calls["retry"].append(kw)
            r = self.retry_reply
            if callable(r):
                return r(kw)
            return dict(r or {"kind": "NO_CODE", "code": "", "usage": {}})

        def describe(**kw):
            self.calls["describe"].append(kw)
            t = self.describe_text
            return {"text": t(kw) if callable(t) else t, "usage": {}}

        def summarize(**kw):
            self.calls["summarize"].append(kw)
            return {"text": self.summarize_text, "usage": {}}

        monkeypatch.setattr(bc, "plan", plan)
        monkeypatch.setattr(bc, "retry", retry)
        monkeypatch.setattr(bc, "describe", describe)
        monkeypatch.setattr(bc, "summarize", summarize)
        return self


def _install_renders(monkeypatch, charts: dict, tables: dict | None = None):
    """Stub the two executors by code text; anything unknown fails."""
    def render(code, dfs, sid, **kw):
        out = charts.get(code)
        return dict(out) if out is not None else {"ok": False, "error": "ValueError: boom",
                                                  "trace": ""}

    def execute(code, dfs, sid, **kw):
        out = (tables or {}).get(code)
        return dict(out) if out is not None else {"error": "ValueError: boom"}

    monkeypatch.setattr(run_chat_local, "render_plot_safe", render)
    monkeypatch.setattr(run_chat_local, "safe_execute", execute)


def _run_multi(dfs, question="show sales"):
    return list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question=question,
        history_rows=[], user_email="alice@acme.com"))


def _single(events):
    assert len(events) == 1 and events[0].get("single_response"), events
    return events[0]["result"]


def _partials(events):
    return [e for e in events if e.get("partial") and e.get("image_base64")]


def _narrative_events(events):
    return [e for e in events if e.get("narrative")]


def _done(events):
    done = [e for e in events if e.get("done")]
    assert len(done) == 1
    return done[0]


def _table_view(events):
    """(tables, captions, caption_templates, phases) from a tables partial
    and/or the done event, in emission order."""
    tables, caps, tmpls, phases = [], [], [], []
    for e in events:
        if e.get("single_response") or not e.get("tables"):
            continue
        n = len(e["tables"])
        tables += list(e["tables"])
        caps += list(e.get("table_captions") or [None] * n)
        tmpls += list(e.get("table_caption_templates") or [None] * n)
        phases += list(e.get("table_phases") or [e.get("phase")] * n)
    return tables, caps, tmpls, phases


# ======================================================== the 14 spec cases

SCALAR_CODE = "VALUES['total'] = 1234567\nFINDINGS['rising'] = True\nRESULT = 42"
SCALAR_NARR = "Total: {{total:count}}.{{#rising}} Rising.{{/rising}}"
TABLE_CODE = "VALUES['total'] = 3\nRESULT = dfs['f.csv']"


@pytest.mark.parametrize("shape", ["scalar", "table_with_caption"])
def test_single_narrative_skips_describe_and_summarize(monkeypatch, dfs, shape):
    if shape == "scalar":
        plan = {"kind": "PYTHON", "code": SCALAR_CODE, "raw_text": "", "usage": {},
                "narrative": SCALAR_NARR}
    else:
        plan = {"kind": "PYTHON", "code": TABLE_CODE, "raw_text": "", "usage": {},
                "narrative": "Rows counted: {{total}}.",
                "captions": {"1": "This table has {{total}} rows."}}
    brain = Brain(plan).install(monkeypatch)

    result = _single(_run_multi(dfs))

    assert brain.calls["describe"] == []
    assert brain.calls["summarize"] == []
    if shape == "scalar":
        assert result["narrative_template"] == SCALAR_NARR
        assert result["narrative_rendered"] == "Total: 1,234,567. Rising."
        assert result["values"]["total"] == 1234567
        assert result["values"]["result"] == 42          # the implicit result slot
        assert result["findings"] == {"rising": True}
        # brain-safe text: marker + template, never a rendered value (R0)
        assert result["text"] == narrative.history_content(SCALAR_NARR, {"rising": True})
        assert result["text"].startswith("[Findings: rising=true]\n")
        assert "1,234,567" not in result["text"] and "1234567" not in result["text"]
        assert "value_display" not in result
    else:
        assert result["narrative_rendered"] == "Rows counted: 3."
        assert result["caption_template"] == "This table has {{total}} rows."
        assert result["caption_rendered"] == "This table has 3 rows."
        assert result["text"] == "Rows counted: {{total}}."
        assert result["table"] is not None


FAILING_CODE = "VALUES['a'] = 1\nRESULT = undefined_name_xyz"
FIXED_CODE = "VALUES['a'] = 7\nRESULT = dfs['f.csv']"


def test_single_retry_narrative_replaces_plan(monkeypatch, dfs):
    plan = {"kind": "PYTHON", "code": FAILING_CODE, "raw_text": "", "usage": {},
            "narrative": "Old {{a}}", "captions": {"1": "old cap {{a}}"}}
    retry = {"kind": "PYTHON", "code": FIXED_CODE, "usage": {},
             "narrative": "New {{a}}", "captions": {"1": "new cap {{a}}"}}
    brain = Brain(plan, retry=retry).install(monkeypatch)

    result = _single(_run_multi(dfs))

    assert len(brain.calls["retry"]) == 1
    assert list(brain.calls["retry"][0].get("capabilities") or []) == _caps()
    assert result["narrative_template"] == "New {{a}}"
    assert result["narrative_rendered"] == "New 7"
    assert result["caption_template"] == "new cap {{a}}"
    assert result["caption_rendered"] == "new cap 7"
    assert result["values"] == {"a": 7}            # the failed attempt never merges
    assert brain.calls["describe"] == []
    assert result["text"] == "New {{a}}"


def test_single_retry_absent_narrative_keeps_plan(monkeypatch, dfs):
    plan = {"kind": "PYTHON", "code": FAILING_CODE, "raw_text": "", "usage": {},
            "narrative": "Old {{a}}", "captions": {"1": "old cap {{a}}"}}
    retry = {"kind": "PYTHON", "code": FIXED_CODE, "usage": {}}
    brain = Brain(plan, retry=retry).install(monkeypatch)

    result = _single(_run_multi(dfs))

    assert len(brain.calls["retry"]) == 1
    assert result["narrative_template"] == "Old {{a}}"
    assert result["narrative_rendered"] == "Old 7"
    assert result["caption_template"] == "old cap {{a}}"
    assert result["caption_rendered"] == "old cap 7"
    assert brain.calls["describe"] == []


def test_single_caption_only_text_is_caption_template(monkeypatch, dfs):
    tmpl = "Top region: {{top:text}}"
    plan = {"kind": "PYTHON", "code": "VALUES['top'] = 'North'\nRESULT = dfs['f.csv']",
            "raw_text": "", "usage": {}, "captions": {"1": tmpl}}
    brain = Brain(plan).install(monkeypatch)

    result = _single(_run_multi(dfs))

    assert brain.calls["describe"] == []
    assert result["text"] == tmpl                    # never the rendered caption (R0)
    assert result["caption_template"] == tmpl
    assert result["caption_rendered"] == "Top region: North"
    assert "narrative_template" not in result
    assert "narrative_rendered" not in result


CODE_A = "fig = chart_a\nVALUES['a'] = 1"
CODE_B = "fig = chart_b\nVALUES['b'] = 2"


def test_caption_uses_own_block_values_only(monkeypatch, dfs):
    # Two sentences per caption: each block renders only its own value; the
    # sentence naming the OTHER block's slot reads n/a and is dropped by the
    # quality gate (BRB retest follow-up; this used to show "A=1 B=n/a").
    cap = "A is {{a}}. B is {{b}}."
    brain = Brain(_multi_plan(("plot_code", CODE_A), ("plot_code", CODE_B),
                              narrative_t="Sum {{a}} {{b}}",
                              captions={"1": cap, "2": cap})).install(monkeypatch)
    _install_renders(monkeypatch, {CODE_A: _chart({"a": 1}), CODE_B: _chart({"b": 2})})

    events = _run_multi(dfs)

    parts = _partials(events)
    assert [p["answer"] for p in parts] == ["A is 1.", "B is 2."]
    assert [p.get("caption_template") for p in parts] == [cap, cap]
    assert [p.get("block_index") for p in parts] == [1, 2]
    assert [p.get("phase") for p in parts] == [1, 1]
    narr = _narrative_events(events)
    assert len(narr) == 1 and narr[0]["text"] == "Sum 1 2" and narr[0]["phase"] == 1
    assert brain.calls["describe"] == []


def test_multi_caption_index_counts_table_blocks(monkeypatch, dfs):
    t_code = "RESULT = tbl_1\nVALUES['t'] = 5"
    c_code = "fig = chart_c\nVALUES['c'] = 9"
    brain = Brain(_multi_plan(("python", t_code), ("plot_code", c_code),
                              captions={"1": "Table has {{t}}",
                                        "2": "Chart has {{c}}"})).install(monkeypatch)
    _install_renders(monkeypatch, {c_code: _chart({"c": 9})},
                     {t_code: _table({"t": 5})})

    events = _run_multi(dfs)

    parts = _partials(events)
    assert len(parts) == 1
    assert parts[0]["answer"] == "Chart has 9"
    assert parts[0].get("caption_template") == "Chart has {{c}}"
    assert parts[0].get("block_index") == 2       # numbered BEFORE the table split
    tables, caps, tmpls, phases = _table_view(events)
    assert len(tables) == 1
    assert caps == ["Table has 5"]
    assert tmpls == ["Table has {{t}}"]
    assert phases == [1]
    assert brain.calls["describe"] == []


def test_multi_missing_caption_describes_that_chart_only(monkeypatch, dfs):
    narr_t = "N {{a}} {{b}}"
    brain = Brain(_multi_plan(("plot_code", CODE_A), ("plot_code", CODE_B),
                              narrative_t=narr_t, captions={"1": "cap {{a}}"}),
                  describe_text="described B").install(monkeypatch)
    _install_renders(monkeypatch, {CODE_A: _chart({"a": 1}), CODE_B: _chart({"b": 2})})

    events = _run_multi(dfs)

    assert len(brain.calls["describe"]) == 1
    assert brain.calls["describe"][0]["code"] == CODE_B
    parts = _partials(events)
    assert [p["answer"] for p in parts] == ["cap 1", "described B"]
    assert parts[0].get("caption_template") == "cap {{a}}"
    assert parts[1].get("caption_template") is None
    assert brain.calls["summarize"] == []
    done = _done(events)
    assert done["combined_answer"] == narrative.history_content(narr_t, {})
    assert done["display_answer"] == "N 1 2"


def test_multi_no_narrative_captionless_chart_describe_with_findings(monkeypatch, dfs):
    code = "fig = chart_f\nFINDINGS['coverage'] = 'coverage_change'"
    brain = Brain(_multi_plan(("plot_code", code))).install(monkeypatch)
    _install_renders(monkeypatch, {code: _chart(findings={"coverage": "coverage_change"})})

    events = _run_multi(dfs)

    assert len(brain.calls["describe"]) == 1
    assert brain.calls["describe"][0].get("data_caveat") == {
        "findings": {"coverage": "coverage_change"}}
    assert _narrative_events(events) == []
    done = _done(events)
    assert done["combined_answer"] == "described"
    assert done["display_answer"] == "described"


def test_backstop_sentence_prepended_locally(monkeypatch, dfs):
    code = "fig = chart_q\nVALUES['q'] = 5"
    flat = {"rows": [{"g": "A", "Quantity": 5}, {"g": "B", "Quantity": 5},
                     {"g": "C", "Quantity": 5}]}
    tmpl = "Quantity is {{q}}"
    brain = Brain(_multi_plan(("plot_code", code), captions={"1": tmpl})).install(monkeypatch)
    _install_renders(monkeypatch, {code: _chart({"q": 5}, chart_data=flat)})

    events = _run_multi(dfs, question="show quantity per group")

    sentence = result_backstop.fallback_sentence("en", catalog=False, matrix=False, flat=True)
    parts = _partials(events)
    assert len(parts) == 1
    assert parts[0]["answer"] == f"{sentence}\n\nQuantity is 5"
    assert parts[0].get("caption_template") == f"{sentence}\n\n{tmpl}"
    assert brain.calls["describe"] == []              # no brain call for the note
    assert _done(events)["combined_answer"] == f"{sentence}\n\n{tmpl}"


def test_values_merge_order_collision_log(monkeypatch, dfs, caplog):
    # block 1 = table (runs LAST), blocks 2 and 3 = charts (run first).
    t_code = "RESULT = tbl\nFINDINGS['flag'] = True"
    c1 = "fig = chart_1\nVALUES['x'] = 1\nFINDINGS['flag'] = False"
    c2 = "fig = chart_2\nVALUES['x'] = 2"
    narr_t = "x={{x}}{{#flag}} up{{/flag}}{{^flag}} down{{/flag}}"
    Brain(_multi_plan(("python", t_code), ("plot_code", c1), ("plot_code", c2),
                      narrative_t=narr_t)).install(monkeypatch)
    _install_renders(monkeypatch,
                     {c1: _chart({"x": 1}, {"flag": False}), c2: _chart({"x": 2})},
                     {t_code: _table(findings={"flag": True})})

    with caplog.at_level(logging.INFO, logger="datachat"):
        events = _run_multi(dfs)

    narr = _narrative_events(events)
    assert len(narr) == 1
    # VALUES: later block wins; FINDINGS: merged in block_index order (block 2
    # beats block 1), not execution order (the table ran last).
    assert narr[0]["values"]["x"] == 2
    assert narr[0]["findings"] == {"flag": False}
    assert narr[0]["text"] == "x=2 down"
    assert _done(events)["combined_answer"] == narrative.history_content(narr_t, {"flag": False})
    msgs = [r.getMessage() for r in caplog.records]
    assert any("VALUES_KEY_COLLISION n=1" in m for m in msgs)
    assert any("FINDINGS_KEY_COLLISION n=1" in m for m in msgs)


def test_failed_block_slot_renders_na(monkeypatch, dfs, caplog):
    """A failed block's slot renders n/a. "a={{a}} b={{b}}" is ONE sentence,
    so the FIRST sentence holds n/a: the template is invalid (BRB retest 2
    follow-up, kind `first_sentence`) and ONE narrative-only retry asks for a
    new one — 3 exec retries of the bad block + 1 narrative retry. The fake
    reply carries no narrative, so the narrative is not used and the turn
    shows today's no-narrative answer, the successful chart's describe text
    (it has no caption)."""
    code_ok = "fig = chart_ok\nVALUES['a'] = 1"
    code_bad = "fig = chart_bad\nVALUES['b'] = 5"
    brain = Brain(_multi_plan(("plot_code", code_ok), ("plot_code", code_bad),
                              narrative_t="a={{a}} b={{b}}")).install(monkeypatch)
    # The failing block even reports VALUES next to its error: they must never merge.
    bad = {"ok": False, "error": "ValueError: boom", "trace": "", "values": {"b": 5},
           "values_dropped": 0, "findings": {}, "findings_dropped": 0}
    _install_renders(monkeypatch, {code_ok: _chart({"a": 1}), code_bad: bad})

    with caplog.at_level(logging.INFO, logger="datachat"):
        events = _run_multi(dfs)

    assert len(brain.calls["retry"]) == 4             # 3 exec retries + 1 narrative retry
    narr_retries = [kw for kw in brain.calls["retry"]
                    if str(kw.get("error_msg", "")).startswith("narrative_invalid")]
    assert len(narr_retries) == 1
    # the slot cause names the missing `b` (narrative follow-up, item 1)
    assert narr_retries[0]["error_msg"] == \
        "narrative_invalid: first_sentence slot=b cause=missing"
    assert narr_retries[0]["failed_code"] == code_ok      # the emitted code only
    assert len(brain.calls["describe"]) == 1
    assert brain.calls["describe"][0]["code"] == code_ok
    assert _narrative_events(events) == []
    done = _done(events)
    assert done["display_answer"] == "described"
    assert done["combined_answer"] == "described"
    assert "n/a" not in done["display_answer"]
    assert done["narrative_view"]["phase1"] is None
    msgs = [r.getMessage() for r in caplog.records]
    assert any("NARRATIVE_INVALID reason=first_sentence" in m for m in msgs), msgs
    assert any("NARRATIVE_INVALID_TWICE reason=no_narrative" in m for m in msgs), msgs
    assert not any("all_sentences_dropped" in m for m in msgs), msgs


def test_narrative_keeps_clean_sentence_drops_na_one(monkeypatch, dfs, caplog):
    code_ok = "fig = chart_ok2\nVALUES['a'] = 1"
    code_bad = "fig = chart_bad2\nVALUES['b'] = 5"
    narr_t = "A is {{a}}. B is {{b}}."
    brain = Brain(_multi_plan(("plot_code", code_ok), ("plot_code", code_bad),
                              narrative_t=narr_t)).install(monkeypatch)
    _install_renders(monkeypatch, {code_ok: _chart({"a": 1})})

    with caplog.at_level(logging.INFO, logger="datachat"):
        events = _run_multi(dfs)

    narr = _narrative_events(events)
    assert len(narr) == 1
    assert narr[0]["text"] == "A is 1."
    done = _done(events)
    assert done["display_answer"] == "A is 1."
    assert done["narrative_view"]["phase1"]["narrative_rendered"] == "A is 1."
    # the brain-safe content is still the template (R0)
    assert done["combined_answer"] == narrative.history_content(narr_t, {})
    msgs = [r.getMessage() for r in caplog.records]
    dropped = [m for m in msgs if "NARRATIVE_SENTENCE_DROPPED" in m]
    assert len(dropped) == 1 and "n=1" in dropped[0] and "what=narrative" in dropped[0]
    assert not any("all_sentences_dropped" in m for m in msgs)
    assert [kw["code"] for kw in brain.calls["describe"]] == [code_ok]


def test_caption_with_none_value_goes_to_describe(monkeypatch, dfs, caplog):
    code_x = "fig = chart_x\nVALUES['x'] = None"
    code_y = "fig = chart_y\nVALUES['y'] = 3"
    brain = Brain(_multi_plan(("plot_code", code_x), ("plot_code", code_y),
                              captions={"1": "{{x}} here", "2": "Y is {{y}}."}),
                  describe_text="described X").install(monkeypatch)
    _install_renders(monkeypatch, {code_x: _chart({"x": None}), code_y: _chart({"y": 3})})

    with caplog.at_level(logging.INFO, logger="datachat"):
        events = _run_multi(dfs)

    assert len(brain.calls["describe"]) == 1          # that visual only
    assert brain.calls["describe"][0]["code"] == code_x
    parts = _partials(events)
    assert [p["answer"] for p in parts] == ["described X", "Y is 3."]
    assert all("n/a" not in p["answer"] for p in parts)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("NARRATIVE_SENTENCE_DROPPED" in m and "what=caption" in m for m in msgs), msgs


def test_single_scalar_all_dropped_narrative_falls_back_to_summarize(monkeypatch, dfs,
                                                                    caplog):
    """"{{missing}}" renders "n/a" in the FIRST sentence: the template is
    invalid (BRB retest 2 follow-up), one narrative-only retry is made, the
    fake reply carries no narrative, so the turn takes today's summarize path."""
    plan = {"kind": "PYTHON", "code": "RESULT = 42", "raw_text": "", "usage": {},
            "narrative": "{{missing}}"}
    brain = Brain(plan, summarize_text="The answer is computed.").install(monkeypatch)

    with caplog.at_level(logging.INFO, logger="datachat"):
        result = _single(_run_multi(dfs, question="what is the total?"))

    assert len(brain.calls["retry"]) == 1
    assert brain.calls["retry"][0]["error_msg"] == \
        "narrative_invalid: first_sentence slot=missing cause=missing"
    assert brain.calls["retry"][0]["failed_code"] == "RESULT = 42"
    assert len(brain.calls["summarize"]) == 1
    assert brain.calls["summarize"][0].get("preview", "missing") is None
    assert result["text"] == "The answer is computed."
    assert result["value_display"] == "**Result:** 42"
    assert "narrative_rendered" not in result
    msgs = [r.getMessage() for r in caplog.records]
    assert any("NARRATIVE_INVALID_TWICE reason=no_narrative" in m for m in msgs), msgs
    assert not any("all_sentences_dropped" in m for m in msgs), msgs


def test_single_table_all_dropped_narrative_uses_describe(monkeypatch, dfs):
    code = "VALUES['a'] = None\nRESULT = dfs['f.csv']"
    plan = {"kind": "PYTHON", "code": code, "raw_text": "", "usage": {},
            "narrative": "The value is {{a}}."}
    brain = Brain(plan).install(monkeypatch)
    _install_renders(monkeypatch, {}, {code: _table({"a": None})})

    result = _single(_run_multi(dfs))

    # the only sentence holds n/a: one narrative-only retry (no narrative in
    # the fake reply), then today's describe path
    assert [kw["error_msg"] for kw in brain.calls["retry"]] == [
        "narrative_invalid: first_sentence slot=a cause=none"]
    assert len(brain.calls["describe"]) == 1
    assert result["text"] == "described"
    assert "narrative_rendered" not in result
    assert "narrative_template" not in result


def test_no_successful_block_drops_narrative(monkeypatch, dfs):
    code = "fig = chart_bad\nVALUES['a'] = 1"
    brain = Brain(_multi_plan(("plot_code", code),
                              narrative_t="a={{a}}")).install(monkeypatch)
    _install_renders(monkeypatch, {})

    events = _run_multi(dfs)

    assert len(brain.calls["retry"]) == 3
    assert _narrative_events(events) == []
    done = _done(events)
    assert done["combined_answer"] == FAIL_TEXT
    assert done["display_answer"] == FAIL_TEXT
    assert done["narrative_view"]["phase1"] is None


@pytest.mark.parametrize("code,expected", [
    ("RESULT = 1234", "**Result:** 1,234"),
    ("RESULT = {'a': 1, 'b': 2.5}", "- a: 1\n- b: 2.50"),
])
def test_fallback_value_display_scalar_and_dict(monkeypatch, dfs, code, expected):
    brain = Brain({"kind": "PYTHON", "code": code, "raw_text": "", "usage": {}},
                  summarize_text="Here is the figure you asked for.").install(monkeypatch)

    result = _single(_run_multi(dfs, question="what is the total?"))

    assert len(brain.calls["summarize"]) == 1
    assert brain.calls["summarize"][0].get("preview", "missing") is None
    assert result["text"] == "Here is the figure you asked for."   # no value in text
    assert result["value_display"] == expected
    assert "narrative_template" not in result


def test_old_brain_response_unchanged_flow(monkeypatch, dfs):
    # (a) multi: one chart, no template, no FINDINGS -> today's describe
    code = "fig = chart_old"
    brain = Brain(_multi_plan(("plot_code", code))).install(monkeypatch)
    _install_renders(monkeypatch, {code: _chart()})
    events = _run_multi(dfs)
    assert len(brain.calls["describe"]) == 1
    assert not brain.calls["describe"][0].get("data_caveat")
    parts = _partials(events)
    assert [p["answer"] for p in parts] == ["described"]
    assert _narrative_events(events) == []
    done = _done(events)
    assert done["combined_answer"] == "described"
    assert done["display_answer"] == "described"
    assert done["narrative_view"]["phase1"] is None

    # (b) single scalar: summarize, only the preview is now null
    # real in-process execution again for the scalar (the conftest binding)
    monkeypatch.setattr(run_chat_local, "safe_execute", code_exec._execute_in_process)
    brain = Brain({"kind": "PYTHON", "code": "RESULT = 7", "raw_text": "", "usage": {}}
                  ).install(monkeypatch)
    result = _single(_run_multi(dfs, question="how many?"))
    assert len(brain.calls["summarize"]) == 1
    assert brain.calls["summarize"][0].get("preview", "missing") is None
    assert result["text"] == "summarized"
    assert brain.calls["describe"] == []
    for key in ("narrative_template", "narrative_rendered", "caption_template",
                "caption_rendered", "caption_described"):
        assert key not in result


# ========================================================== capabilities

def test_plan_and_retries_send_capabilities_from_setting(monkeypatch, dfs):
    expected = _caps()
    assert expected == ["narrative_v1", "diagnostics_v1"]
    err = "fig = chart_err"
    ma = "fig = chart_multi_axes"
    mpl = "plt.bar(x, y)"
    mx = "fig = px.imshow(m)"
    grid = {"rows": [{"r": "a", "c": "x", "n": 1}, {"r": "a", "c": "y", "n": 2},
                     {"r": "b", "c": "x", "n": 3}, {"r": "b", "c": "y", "n": 4}]}
    charts = {
        ma: {"ok": False, "error": "MultiAxesError: 2 axes", "multi_axes": True, "trace": ""},
        mpl: {"ok": True, "error": None, "is_plotly": False, "image": "IMG",
              "chart_data": None},
        mx: _chart(chart_data=grid),
    }
    fixed = iter(f"fig = chart_fixed_{i}" for i in range(20))

    def retry(kw):
        code = next(fixed)
        charts[code] = _chart()
        return {"kind": "PLOT_CODE", "code": code, "usage": {}}

    brain = Brain(_multi_plan(("plot_code", err), ("plot_code", ma), ("plot_code", mpl),
                              ("plot_code", mx)), retry=retry).install(monkeypatch)
    _install_renders(monkeypatch, charts)

    _run_multi(dfs, question="show sales per store")

    failed = [kw["failed_code"] for kw in brain.calls["retry"]]
    for code in (err, ma, mpl, mx):          # exec-error, multi-axes, plotly, matrix
        assert code in failed, code
    assert len(brain.calls["plan"]) == 1
    for kw in brain.calls["plan"] + brain.calls["retry"]:
        assert list(kw.get("capabilities") or []) == expected

    # the single flow's exec-error retry
    single = Brain({"kind": "PYTHON", "code": FAILING_CODE, "raw_text": "", "usage": {}},
                   retry={"kind": "PYTHON", "code": FIXED_CODE, "usage": {}}
                   ).install(monkeypatch)
    _install_renders(monkeypatch, {}, {FIXED_CODE: _table({"a": 7})})
    _single(_run_multi(dfs))
    assert len(single.calls["retry"]) == 1
    for kw in single.calls["plan"] + single.calls["retry"]:
        assert list(kw.get("capabilities") or []) == expected


def test_kill_switch_sends_no_capabilities(monkeypatch, dfs):
    """Through the REAL payload builders: BRAIN_CAPABILITIES="" posts no
    `capabilities` key on plan or retry; switching it back on adds it."""
    import brain_client
    code = "fig = chart_k"
    fixed = "fig = chart_k_fixed"
    posts = []

    def fake_post(path, payload, sid, **kw):
        posts.append((path, payload))
        if path == "/v1/plan":
            return _multi_plan(("plot_code", code))
        if path == "/v1/retry":
            return {"kind": "PLOT_CODE", "code": fixed, "usage": {}}
        return {"text": "described", "usage": {}}

    monkeypatch.setattr(brain_client, "_post", fake_post)
    _install_renders(monkeypatch, {fixed: _chart()})

    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", "")
    _run_multi(dfs)
    sent = [(p, pl) for p, pl in posts if p in ("/v1/plan", "/v1/retry")]
    assert {p for p, _ in sent} == {"/v1/plan", "/v1/retry"}
    for _, payload in sent:
        assert "capabilities" not in payload

    posts.clear()
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", "narrative_v1")
    _run_multi(dfs)
    sent = [(p, pl) for p, pl in posts if p in ("/v1/plan", "/v1/retry")]
    assert {p for p, _ in sent} == {"/v1/plan", "/v1/retry"}
    for _, payload in sent:
        assert payload.get("capabilities") == ["narrative_v1"]


def test_run_chat_body_sends_no_capabilities_and_null_preview(monkeypatch, dfs):
    """Auto Analytics (`run_chat` -> `_run_chat_body`): its text feeds
    /v1/report, so it sends no capabilities, summarizes with a null preview
    and appends no value_display."""
    brain = Brain({"kind": "PYTHON", "code": "RESULT = undefined_name_xyz",
                   "raw_text": "", "usage": {}},
                  retry={"kind": "PYTHON", "code": "RESULT = 1234", "usage": {}},
                  summarize_text="The total was computed.").install(monkeypatch)

    result = run_chat_local.run_chat(sid="t", dfs=dfs, schema_docs={},
                                     question="what is the total?", history_rows=[],
                                     user_email="alice@acme.com")

    assert len(brain.calls["plan"]) == 1 and len(brain.calls["retry"]) == 1
    for kw in brain.calls["plan"] + brain.calls["retry"]:
        assert not kw.get("capabilities")
    assert len(brain.calls["summarize"]) == 1
    assert brain.calls["summarize"][0].get("preview", "missing") is None
    assert result["text"] == "The total was computed."
    assert "value_display" not in result
    assert "1,234" not in result["text"]


# ===================================================== live notes / findings

def _truncated_state():
    state = run_chat_local._new_live_state()
    state["truncated"] = {"big"}
    state["live_rows"] = {"big": 100}
    return state


def test_live_notes_go_on_rendered_text_not_brain_text(monkeypatch, dfs):
    note = result_backstop.live_truncated_sentence("en", "big", 100)

    # single result with a narrative: the note goes on the rendered text only
    out = run_chat_local._finish_live_result(
        {"text": "[Findings: a=true]\nTotal {{n}}", "narrative_template": "Total {{n}}",
         "narrative_rendered": "Total 5", "values": {"n": 5}, "findings": {"a": True}},
        _truncated_state(), "how many?", "t")
    assert out["text"] == "[Findings: a=true]\nTotal {{n}}"
    assert out["narrative_rendered"] == f"Total 5\n\n{note}"

    # caption only: the note goes on the rendered caption
    out = run_chat_local._finish_live_result(
        {"text": "Rows: {{n}}", "caption_template": "Rows: {{n}}",
         "caption_rendered": "Rows: 5", "values": {"n": 5}},
        _truncated_state(), "how many?", "t")
    assert out["text"] == "Rows: {{n}}"
    assert out["caption_rendered"] == f"Rows: 5\n\n{note}"

    # an old-shape result keeps today's behaviour
    out = run_chat_local._finish_live_result({"text": "plain"}, _truncated_state(),
                                             "how many?", "t")
    assert out["text"] == f"plain\n\n{note}"

    # multi flow: the narrative event and display_answer carry the note,
    # combined_answer (brain-safe) does not
    real_new_state = run_chat_local._new_live_state

    def new_state(plan_out=None):
        state = real_new_state(plan_out)
        state["truncated"] = {"big"}
        state["live_rows"] = {"big": 100}
        return state

    monkeypatch.setattr(run_chat_local, "_new_live_state", new_state)
    Brain(_multi_plan(("plot_code", CODE_A), narrative_t="Total {{a}}",
                      captions={"1": "cap {{a}}"})).install(monkeypatch)
    _install_renders(monkeypatch, {CODE_A: _chart({"a": 1})})
    events = _run_multi(dfs, question="how many?")
    narr = _narrative_events(events)
    assert len(narr) == 1 and narr[0]["text"] == f"Total 1\n\n{note}"
    done = _done(events)
    assert done["combined_answer"] == "Total {{a}}"
    assert note not in done["combined_answer"]
    assert done["display_answer"] == f"Total 1\n\n{note}"


def test_findings_validated_before_describe(monkeypatch, dfs):
    code = ("fig = chart_v\nFINDINGS['dim'] = 'region'\nFINDINGS['top_cat'] = top\n"
            "FINDINGS['coverage'] = 'coverage_change'\nFINDINGS['n_rows'] = n\n"
            "FINDINGS[f'is_{x}'] = True")
    raw_findings = {"dim": "region",                  # literal key, input column -> kept
                    "top_cat": "North",               # a CATEGORY value -> dropped
                    "coverage": "coverage_change",    # registry key, enum -> kept
                    "n_rows": "3",                    # not a column / enum -> dropped
                    "is_north": True}                 # key built from data -> dropped
    brain = Brain(_multi_plan(("plot_code", code))).install(monkeypatch)
    _install_renders(monkeypatch, {code: _chart(findings=raw_findings)})

    _run_multi(dfs)

    assert len(brain.calls["describe"]) == 1
    assert brain.calls["describe"][0].get("data_caveat") == {
        "findings": {"dim": "region", "coverage": "coverage_change"}}


def test_summarize_always_preview_null(monkeypatch, dfs):
    # site 1: the chat single flow (`_run_single_body`)
    brain = Brain({"kind": "PYTHON", "code": "RESULT = 99", "raw_text": "", "usage": {}}
                  ).install(monkeypatch)
    _single(_run_multi(dfs, question="how many?"))
    assert [kw.get("preview", "missing") for kw in brain.calls["summarize"]] == [None]

    # site 2: Auto Analytics (`_run_chat_body`)
    brain = Brain({"kind": "PYTHON", "code": "RESULT = {'a': 1}", "raw_text": "",
                   "usage": {}}).install(monkeypatch)
    run_chat_local.run_chat(sid="t", dfs=dfs, schema_docs={}, question="how many?",
                            history_rows=[], user_email="alice@acme.com")
    assert [kw.get("preview", "missing") for kw in brain.calls["summarize"]] == [None]


# ============================== FINDINGS stored without a narrative (A5.5)
# Record `findings` are written "when the phase-1 narrative or FINDINGS are
# present": a describe-only turn whose code set valid FINDINGS keeps them
# (validated, merged), while its brain-safe text gains NO marker (no template
# => today's text).

def test_single_describe_only_turn_keeps_validated_findings(monkeypatch, dfs):
    code = ("FINDINGS['coverage'] = 'coverage_change'\nFINDINGS['top'] = top\n"
            "RESULT = dfs['f.csv']")
    raw = {"coverage": "coverage_change", "top": "North"}
    brain = Brain({"kind": "PYTHON", "code": code, "raw_text": "", "usage": {}}
                  ).install(monkeypatch)
    _install_renders(monkeypatch, {}, {code: _table(findings=raw)})

    result = _single(_run_multi(dfs))

    assert len(brain.calls["describe"]) == 1
    assert result["text"] == "described"                 # no marker, no template
    assert result["findings"] == {"coverage": "coverage_change"}
    for key in ("narrative_template", "narrative_rendered", "caption_template",
                "caption_rendered", "caption_described"):
        assert key not in result


def test_single_without_findings_has_no_findings_key(monkeypatch, dfs):
    code = "RESULT = dfs['f.csv']"
    Brain({"kind": "PYTHON", "code": code, "raw_text": "", "usage": {}}).install(monkeypatch)
    _install_renders(monkeypatch, {}, {code: _table()})

    result = _single(_run_multi(dfs))

    assert result["text"] == "described"
    assert "findings" not in result and "values" not in result


def test_multi_describe_only_turn_exposes_findings(monkeypatch, dfs):
    code = "fig = chart_f\nFINDINGS['coverage'] = 'coverage_change'\nFINDINGS['top'] = top"
    raw = {"coverage": "coverage_change", "top": "North"}
    Brain(_multi_plan(("plot_code", code))).install(monkeypatch)
    _install_renders(monkeypatch, {code: _chart(findings=raw)})

    events = _run_multi(dfs)

    done = _done(events)
    assert done["combined_answer"] == "described"        # no marker
    assert done["display_answer"] == "described"
    assert done["narrative_view"]["phase1"] == {"findings": {"coverage": "coverage_change"}}
    assert done["narrative_view"]["phase2"] is None
    assert _narrative_events(events) == []


def test_multi_without_findings_or_narrative_phase1_none(monkeypatch, dfs):
    code = "fig = chart_plain"
    Brain(_multi_plan(("plot_code", code))).install(monkeypatch)
    _install_renders(monkeypatch, {code: _chart()})

    done = _done(_run_multi(dfs))

    assert done["narrative_view"]["phase1"] is None


# =========================================================================
# BRB retest 2 follow-up (DIAGNOSTICS_SPEC A5.1): an INVALID narrative
# template (a parse error, or a first sentence holding "n/a") gets ONE
# narrative-only retry: `brain_client.retry` with the flow's usual keywords,
# `error_msg = "narrative_invalid: <kind>"`, `use_pro=True` and the executed
# code as `failed_code`. Only the reply's `narrative` is used — its code never
# runs. A valid template gains the fixed FINDING sentences for every finding
# no rendered section covers (display text only, R0).
# =========================================================================

import re                                                       # noqa: E402

NARR_DELIM = "\n###NEXT_PLOT###\n"
NESTED = "{{#a}}x{{#b}}y{{/b}}{{/a}}"
REPLY_CODE = "fig = chart_reply_never_run\nVALUES['a'] = 999"


def _recording_renders(monkeypatch, charts: dict, tables: dict | None = None):
    """`_install_renders` that also records every code it was asked to run."""
    seen = []

    def render(code, dfs, sid, **kw):
        seen.append(code)
        out = charts.get(code)
        return dict(out) if out is not None else {"ok": False, "error": "ValueError: boom",
                                                  "trace": ""}

    def execute(code, dfs, sid, **kw):
        seen.append(code)
        out = (tables or {}).get(code)
        return dict(out) if out is not None else {"error": "ValueError: boom"}

    monkeypatch.setattr(run_chat_local, "render_plot_safe", render)
    monkeypatch.setattr(run_chat_local, "safe_execute", execute)
    return seen


def _narr_reply(narrative_t=None, code=REPLY_CODE, usage=None):
    out = {"kind": "PLOT_CODE", "code": code, "usage": dict(usage or {}),
           "captions": {"1": "REPLY CAPTION {{a}}"}}
    if narrative_t is not None:
        out["narrative"] = narrative_t
    return out


def _narr_retries(brain):
    return [kw for kw in brain.calls["retry"]
            if str(kw.get("error_msg", "")).startswith("narrative_invalid")]


def _msgs(caplog):
    return [r.getMessage() for r in caplog.records]


def test_multi_nested_template_one_narrative_retry(monkeypatch, dfs, caplog):
    brain = Brain(_multi_plan(("plot_code", CODE_A), ("plot_code", CODE_B), narrative_t=NESTED),
                  retry=_narr_reply("Fixed {{a}} and {{b}}.",
                                    usage={"input_tokens": 7})).install(monkeypatch)
    seen = _recording_renders(monkeypatch, {CODE_A: _chart({"a": 1}), CODE_B: _chart({"b": 2})})

    with caplog.at_level(logging.INFO, logger="datachat"):
        events = _run_multi(dfs)

    assert len(brain.calls["retry"]) == 1
    kw = brain.calls["retry"][0]
    assert kw["error_msg"] == "narrative_invalid: nested"
    assert kw["use_pro"] is True
    assert kw["failed_code"] == NARR_DELIM.join([CODE_A, CODE_B])
    assert list(kw.get("capabilities") or []) == _caps()
    assert kw.get("sql_error") is None
    # the reply's code never runs
    assert REPLY_CODE not in seen
    assert seen.count(CODE_A) == 1 and seen.count(CODE_B) == 1
    narr = _narrative_events(events)
    assert len(narr) == 1
    assert narr[0]["text"] == "Fixed 1 and 2."
    assert narr[0]["template"] == "Fixed {{a}} and {{b}}."
    done = _done(events)
    assert done["display_answer"] == "Fixed 1 and 2."
    assert done["combined_answer"] == narrative.history_content("Fixed {{a}} and {{b}}.", {})
    assert done["total_usage"]["input_tokens"] >= 7           # the retry's usage counted
    msgs = _msgs(caplog)
    assert any("NARRATIVE_PARSE_FAILED kind=nested" in m for m in msgs), msgs
    assert not any("all_sentences_dropped" in m for m in msgs), msgs
    assert not any("NARRATIVE_INVALID_TWICE" in m for m in msgs), msgs
    done_lines = [m for m in msgs if "NARRATIVE_RETRY_DONE" in m]
    assert len(done_lines) == 1, msgs
    assert "kind=nested" in done_lines[0] and "narrative=True" in done_lines[0]
    assert re.search(r"elapsed_ms=\d+", done_lines[0]), done_lines


def test_multi_narrative_retry_uses_exec_retry_keywords(monkeypatch, dfs):
    """The narrative-only retry is built with the same keywords as the flow's
    exec-error retry (only the values differ)."""
    fixed = "fig = chart_fixed_kw\nVALUES['b'] = 2"

    def retry(kw):
        if str(kw.get("error_msg", "")).startswith("narrative_invalid"):
            return _narr_reply("Fixed {{a}}.")
        return {"kind": "PLOT_CODE", "code": fixed, "usage": {}}

    brain = Brain(_multi_plan(("plot_code", CODE_A), ("plot_code", "fig = chart_fails_once"),
                              narrative_t=NESTED), retry=retry).install(monkeypatch)
    _recording_renders(monkeypatch, {CODE_A: _chart({"a": 1}), fixed: _chart({"b": 2})})

    events = _run_multi(dfs)

    narr_kw = _narr_retries(brain)
    exec_kw = [kw for kw in brain.calls["retry"] if kw not in narr_kw]
    assert len(narr_kw) == 1 and len(exec_kw) == 1
    assert set(narr_kw[0]) == set(exec_kw[0])
    assert narr_kw[0]["failed_code"] == NARR_DELIM.join([CODE_A, fixed])
    assert _narrative_events(events)[0]["text"] == "Fixed 1."


def test_multi_second_template_invalid_falls_back_to_describe(monkeypatch, dfs, caplog):
    brain = Brain(_multi_plan(("plot_code", CODE_A), narrative_t=NESTED),
                  retry=_narr_reply("{{#a}}x{{/b}}")).install(monkeypatch)
    seen = _recording_renders(monkeypatch, {CODE_A: _chart({"a": 1})})

    with caplog.at_level(logging.INFO, logger="datachat"):
        events = _run_multi(dfs)

    assert len(brain.calls["retry"]) == 1                     # never a second one
    assert REPLY_CODE not in seen
    assert _narrative_events(events) == []
    done = _done(events)
    assert done["display_answer"] == "described"
    assert done["combined_answer"] == "described"
    assert done["narrative_view"]["phase1"] is None
    msgs = _msgs(caplog)
    assert any("NARRATIVE_INVALID_TWICE reason=invalid kind=mismatched" in m for m in msgs), msgs
    assert not any("reason=no_narrative" in m for m in msgs), msgs
    assert not any("all_sentences_dropped" in m for m in msgs), msgs


def test_multi_first_sentence_protection(monkeypatch, dfs, caplog):
    brain = Brain(_multi_plan(("plot_code", CODE_A),
                              narrative_t="Total is {{missing}}. Second sentence."),
                  retry=_narr_reply(None)).install(monkeypatch)
    seen = _recording_renders(monkeypatch, {CODE_A: _chart({"a": 1})})

    with caplog.at_level(logging.INFO, logger="datachat"):
        events = _run_multi(dfs)

    assert len(brain.calls["retry"]) == 1
    kw = brain.calls["retry"][0]
    assert kw["error_msg"] == "narrative_invalid: first_sentence slot=missing cause=missing"
    assert kw["use_pro"] is True
    assert kw["failed_code"] == CODE_A
    assert REPLY_CODE not in seen
    assert _narrative_events(events) == []
    done = _done(events)
    assert done["display_answer"] == "described"
    assert "Second sentence" not in done["display_answer"]
    msgs = _msgs(caplog)
    assert any("NARRATIVE_INVALID reason=first_sentence" in m for m in msgs), msgs
    assert any("NARRATIVE_INVALID_TWICE reason=no_narrative" in m for m in msgs), msgs
    assert any("NARRATIVE_RETRY_DONE kind=first_sentence" in m and "narrative=False" in m
               for m in msgs), msgs


def test_multi_narrative_retry_raise_is_no_reply(monkeypatch, dfs, caplog):
    def retry(kw):
        raise RuntimeError("brain down")

    Brain(_multi_plan(("plot_code", CODE_A), narrative_t=NESTED),
          retry=retry).install(monkeypatch)
    _recording_renders(monkeypatch, {CODE_A: _chart({"a": 1})})

    with caplog.at_level(logging.INFO, logger="datachat"):
        events = _run_multi(dfs)

    assert _narrative_events(events) == []
    assert _done(events)["display_answer"] == "described"
    msgs = _msgs(caplog)
    assert any("NARRATIVE_RETRY_FAILED error=RuntimeError" in m for m in msgs), msgs
    assert any("NARRATIVE_INVALID_TWICE reason=no_narrative" in m for m in msgs), msgs
    assert not any("brain down" in m for m in msgs)


def test_multi_first_sentence_retry_reply_rendered(monkeypatch, dfs):
    brain = Brain(_multi_plan(("plot_code", CODE_A),
                              narrative_t="Total is {{missing}}. Second sentence."),
                  retry=_narr_reply("Total is {{a}}.")).install(monkeypatch)
    _recording_renders(monkeypatch, {CODE_A: _chart({"a": 1})})

    events = _run_multi(dfs)

    assert len(_narr_retries(brain)) == 1
    narr = _narrative_events(events)
    assert len(narr) == 1 and narr[0]["text"] == "Total is 1."


def test_multi_valid_template_makes_no_narrative_retry(monkeypatch, dfs):
    brain = Brain(_multi_plan(("plot_code", CODE_A),
                              narrative_t="Total is {{a}}. Second is {{missing}}.")
                  ).install(monkeypatch)
    _recording_renders(monkeypatch, {CODE_A: _chart({"a": 1})})

    events = _run_multi(dfs)

    assert brain.calls["retry"] == []
    assert _narrative_events(events)[0]["text"] == "Total is 1."


SINGLE_TABLE_CODE = "VALUES['total'] = 3\nRESULT = dfs['f.csv']"


def test_single_nested_template_one_narrative_retry(monkeypatch, dfs, caplog):
    plan = {"kind": "PYTHON", "code": SINGLE_TABLE_CODE, "raw_text": "", "usage": {},
            "narrative": NESTED.replace("{{#a}}x", "{{#a}}{{total}}")}
    reply = {"kind": "PYTHON", "usage": {"input_tokens": 5},
             "code": "VALUES['total'] = 999\nRESULT = dfs['f.csv']",
             "narrative": "Rows: {{total}}."}
    brain = Brain(plan, retry=reply).install(monkeypatch)

    with caplog.at_level(logging.INFO, logger="datachat"):
        result = _single(_run_multi(dfs))

    assert len(brain.calls["retry"]) == 1
    kw = brain.calls["retry"][0]
    assert kw["error_msg"] == "narrative_invalid: nested"
    assert kw["use_pro"] is True
    assert kw["failed_code"] == SINGLE_TABLE_CODE
    # the reply's code never ran: VALUES still hold the plan code's 3
    assert result["narrative_template"] == "Rows: {{total}}."
    assert result["narrative_rendered"] == "Rows: 3."
    assert result["values"]["total"] == 3
    assert result["code"] == SINGLE_TABLE_CODE
    # brain-safe text = history_content of the NEW template (R0)
    assert result["text"] == narrative.history_content("Rows: {{total}}.", result.get("findings") or {})
    assert result["usage"]["input_tokens"] >= 5
    msgs = _msgs(caplog)
    assert any("NARRATIVE_PARSE_FAILED kind=nested" in m for m in msgs), msgs


# ------------------------------------------------- finding sentences, flow

OVERLAP_FINDINGS = {"metric_relation": "metric_definition_overlap"}
OVERLAP_VALUES = {"t": 5, "overlap_equal_share": 0.976}
OVERLAP_EN = ("In most rows this metric equals another metric of the data (97.6% of rows), "
              "so it counts a stock, not new items.")
CODE_OVL = "fig = chart_ovl\nVALUES['t'] = 5"


# the block passes `prefix='ovl_'` to a pdc_diag helper: only then does an
# `ovl_`-prefixed section resolve (the brain's template-validation rule)
CODE_OVL_PREFIXED = ("ov = pdc_diag.check_metric_overlap(df, 'a', 'b', prefix='ovl_')\n"
                     "fig = chart_ovl\nVALUES['t'] = 5")


def _overlap_turn(monkeypatch, dfs, template, code=CODE_OVL):
    Brain(_multi_plan(("plot_code", code), narrative_t=template)).install(monkeypatch)
    _recording_renders(monkeypatch, {code: _chart(OVERLAP_VALUES, OVERLAP_FINDINGS)})
    events = _run_multi(dfs)
    narr = _narrative_events(events)
    assert len(narr) == 1, events
    return narr[0]["text"], _done(events)


def test_flow_uncovered_finding_gets_sentence(monkeypatch, dfs):
    text, done = _overlap_turn(monkeypatch, dfs, "Total is {{t}}.")
    assert text == f"Total is 5.\n\n{OVERLAP_EN}"
    assert done["display_answer"] == text
    # display only: the brain-safe text is the marker + the template (R0)
    assert done["combined_answer"] == narrative.history_content("Total is {{t}}.",
                                                                OVERLAP_FINDINGS)
    assert "97.6" not in done["combined_answer"]


def test_flow_exact_section_covers_finding(monkeypatch, dfs):
    t = ("Total is {{t}}. {{#metric_definition_overlap}}It counts a stock."
         "{{/metric_definition_overlap}}")
    text, _done_ev = _overlap_turn(monkeypatch, dfs, t)
    assert text == "Total is 5. It counts a stock."


def test_flow_prefixed_section_covers_finding(monkeypatch, dfs):
    t = ("Total is {{t}}. {{#ovl_metric_definition_overlap}}It counts a stock."
         "{{/ovl_metric_definition_overlap}}")
    text, _done_ev = _overlap_turn(monkeypatch, dfs, t, code=CODE_OVL_PREFIXED)
    assert text == "Total is 5. It counts a stock."


def test_flow_prefix_not_passed_by_code_does_not_resolve(monkeypatch, dfs, caplog):
    """The block code never passes `prefix='ovl_'`: the prefixed section is an
    unknown name (false), so the finding gets its fixed sentence instead."""
    t = ("Total is {{t}}. {{#ovl_metric_definition_overlap}}It counts a stock."
         "{{/ovl_metric_definition_overlap}}")
    with caplog.at_level(logging.INFO, logger="datachat"):
        text, _done_ev = _overlap_turn(monkeypatch, dfs, t)
    assert text == f"Total is 5.\n\n{OVERLAP_EN}"
    assert any("NARRATIVE_SECTION_UNKNOWN" in m and "name=ovl_metric_definition_overlap" in m
               for m in _msgs(caplog))


def test_flow_negating_prefix_is_not_the_bare_code(monkeypatch, dfs):
    """`{{#no_metric_definition_overlap}}` must never be shown when the
    overlap finding fires (it means the opposite)."""
    t = ("Total is {{t}}. {{#no_metric_definition_overlap}}The metrics are separate."
         "{{/no_metric_definition_overlap}}")
    text, _done_ev = _overlap_turn(monkeypatch, dfs, t, code=CODE_OVL_PREFIXED)
    assert "separate" not in text
    assert text == f"Total is 5.\n\n{OVERLAP_EN}"


def test_flow_inverted_section_does_not_cover(monkeypatch, dfs):
    t = "Total is {{t}}. {{^metrics_identical}}The two metrics differ.{{/metrics_identical}}"
    text, _done_ev = _overlap_turn(monkeypatch, dfs, t)
    assert text == f"Total is 5. The two metrics differ.\n\n{OVERLAP_EN}"


def test_flow_section_dropped_by_gate_does_not_cover(monkeypatch, dfs):
    t = ("Total is {{t}}. {{#metric_definition_overlap}}It equals {{missing}} rows."
         "{{/metric_definition_overlap}}")
    text, _done_ev = _overlap_turn(monkeypatch, dfs, t)
    assert text == f"Total is 5.\n\n{OVERLAP_EN}"
    assert "n/a" not in text


def _fragment(finding: str, lang: str) -> str:
    """The longest literal piece of a FINDING_SENTENCES entry (placeholders
    and parentheses removed) — wording-agnostic evidence the entry was used."""
    entry = narrative.FINDING_SENTENCES[finding][lang]
    base = entry[0] if isinstance(entry, tuple) else entry
    pieces = re.split(r"\{[^}]*\}|\([^)]*\)|[()]", base)
    return max((p.strip(" ,;:.") for p in pieces), key=len)


def test_flow_uzbek_finding_sentences_and_number_format(monkeypatch, dfs):
    code = "fig = chart_uz\nVALUES['total'] = 2345678901"
    findings = {"units": "mixed_units", "unknown_dominated": True, "unknown_dim": "region"}
    template = "Jami: {{total:amount}}."
    plan = _multi_plan(("plot_code", code), narrative_t=template)
    plan["context_decision"] = {"answer_language": "Uzbek"}
    Brain(plan).install(monkeypatch)
    _recording_renders(monkeypatch, {code: _chart({"total": 2345678901}, findings)})

    events = _run_multi(dfs, question="qancha miqdorda kredit ajratildi")

    narr = _narrative_events(events)
    assert len(narr) == 1
    text = narr[0]["text"]
    assert text.startswith("Jami: 2,35 mlrd.")
    table = getattr(narrative, "FINDING_SENTENCES", None)
    assert isinstance(table, dict), "narrative.FINDING_SENTENCES is missing"
    for finding in ("mixed_units", "unknown_dominated"):
        frag = _fragment(finding, "uz")
        assert frag and frag in text, (finding, frag, text)
        assert _fragment(finding, "en") not in text
    # the unknown_dim finding inside the parenthesis, without a share
    assert re.search(r"\([^)]*region[^)]*\)", text), text
    assert text == narrative.add_finding_sentences(
        "Jami: 2,35 mlrd.", findings, {"total": 2345678901}, set(), "uz", sid="t")
    done = _done(events)
    assert done["combined_answer"] == narrative.history_content(template, findings)
    for frag in (_fragment("mixed_units", "uz"), "2,35"):
        assert frag not in done["combined_answer"]
