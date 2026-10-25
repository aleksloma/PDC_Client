"""Narrative follow-up, item 3 — the local headline when the template is dropped
(DIAGNOSTICS_SPEC A5.1 / A5.6 / A6).

Client PR #27 run 3: both the plan's and the retry's first sentence held
"n/a", the template was dropped, and the answer showed the caption with no
headline figure. When the narrative is dropped (`NARRATIVE_INVALID_TWICE`:
the retry gave no narrative, raised or timed out, or its template is invalid
too) the client now shows ONE local sentence with the main figure:

    narrative.headline_sentence(values, lang) ->
        f"{_RESULT_LABEL[lang]}: {pdc_diag.fmt(v, 'amount', locale_for(lang))}."

over the first of VALUES "result", "total", then keys ending "_total" (dict
order) whose value is a finite int/float (never a bool, None, NaN/inf or a
str). It is DISPLAY text (R0): never in `content` / `combined_answer` / a
brain payload. A scalar / flat-dict single answer already shows its value
through `value_display`, so it gets no headline.

Offline: brain calls stubbed at `run_chat_local.brain_client.*`; chart blocks
stubbed per code text; the record helpers are called directly.
"""
import json
import logging
import math

import pandas as pd
import pytest

import brain_client
import narrative
import pdc_diag
import run_chat_local
from settings import settings

CAPS = "narrative_v1,diagnostics_v1"
UZS = 61576335238995.79
H_UZ = "Natija: 61,58 trln."
H_EN = "Result: 61.58 trln."
CODE_A = "fig = chart_a\nVALUES['uzs_total'] = total_of_uzs"
REPLY_CODE = "fig = chart_reply_never_run"
# first sentence holds a missing slot: invalid, and the fake retry gives none
BAD_T = "Credit issued: {{uzs_amount:amount}} UZS. Second sentence."


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


def _hs(values, lang, **kw):
    fn = getattr(narrative, "headline_sentence", None)
    assert callable(fn), "narrative.headline_sentence is missing"
    return fn(values, lang, **kw)


# ======================================================================
# narrative.headline_sentence
# ======================================================================

def test_headline_uz_and_en_format():
    assert _hs({"uzs_total": UZS}, "uz") == H_UZ
    assert _hs({"uzs_total": UZS}, "en") == H_EN


def test_headline_matches_its_definition_in_every_language():
    for lang in ("en", "uz", "ru", "ka"):
        expected = (f"{narrative._RESULT_LABEL[lang]}: "
                    f"{pdc_diag.fmt(5e6, 'amount', narrative.locale_for(lang))}.")
        assert _hs({"total": 5e6}, lang) == expected, lang


def test_headline_unknown_lang_is_english():
    assert _hs({"total": 5e6}, "fr") == "Result: 5.00 mln."
    assert _hs({"total": 5e6}, None) == "Result: 5.00 mln."


def test_headline_key_order_result_total_then_suffix():
    assert _hs({"x_total": 1, "total": 2, "result": 3}, "en") == "Result: 3."
    assert _hs({"x_total": 1, "total": 2}, "en") == "Result: 2."
    # keys ending "_total": the VALUES dict order
    assert _hs({"a_total": 7, "b_total": 9}, "en") == "Result: 7."
    assert _hs({"b_total": 9, "a_total": 7}, "en") == "Result: 9."


def test_headline_skips_unusable_values():
    assert _hs({"result": None, "total": 5e6}, "en") == "Result: 5.00 mln."
    assert _hs({"result": math.nan, "total": math.inf, "y_total": 8}, "en") == "Result: 8."
    assert _hs({"result": True, "total": 4}, "en") == "Result: 4."
    assert _hs({"result": "12", "total": -math.inf, "z_total": 8.0}, "en") == "Result: 8."


@pytest.mark.parametrize("values", [
    {},
    {"count": 5, "share": 0.4},
    {"subtotal": 5},                 # ends with "total", not "_total"
    {"x_total": True},
    {"result": None, "total": math.nan, "x_total": "big"},
    {"result": [1, 2]},
])
def test_headline_none_without_a_usable_key(values):
    assert _hs(values, "en") is None


def test_headline_never_raises_on_bad_input():
    assert _hs(None, "en") is None
    assert _hs("not a dict", "uz") is None


def test_headline_logs_key_and_lang(caplog):
    with caplog.at_level(logging.INFO, logger="datachat"):
        assert _hs({"uzs_total": UZS}, "uz", sid="t") == H_UZ
    lines = [m for m in _msgs(caplog) if "NARRATIVE_HEADLINE_FALLBACK" in m]
    assert len(lines) == 1
    assert "NARRATIVE_HEADLINE_FALLBACK key=uzs_total lang=uz" in lines[0]
    assert "61" not in lines[0]                      # never the value


def test_headline_no_log_when_none(caplog):
    with caplog.at_level(logging.INFO, logger="datachat"):
        assert _hs({"count": 3}, "en") is None
    assert not any("NARRATIVE_HEADLINE_FALLBACK" in m for m in _msgs(caplog))


# ======================================================================
# _NarrState.headline: set by finish on the NARRATIVE_INVALID_TWICE paths only
# ======================================================================

def _state(template, values, lang="en", ok_blocks=1):
    st = run_chat_local._NarrState(phase=1, template=template, captions={},
                                   input_columns=frozenset(), sid="t", lang=lang)
    st.values = dict(values)
    st.ok_blocks = ok_blocks
    return st


def test_state_headline_default_none():
    assert _state(BAD_T, {}).headline is None


def _raise(exc):
    def cb(kind):
        raise exc
    return cb


@pytest.mark.parametrize("retry_cb", [
    None,                                                    # no callback
    lambda kind: None,                                       # no reply
    lambda kind: {"kind": "PLOT_CODE", "code": "x"},         # no narrative
    _raise(RuntimeError("down")),                            # a raise
    _raise(brain_client.BrainTimeoutError("Brain timed out: ReadTimeout")),
    lambda kind: {"narrative": "{{#a}}x{{/b}}"},             # second template invalid
    lambda kind: {"narrative": "Still {{uzs_amount}}. More."},
])
def test_state_headline_set_on_invalid_twice(retry_cb):
    st = _state(BAD_T, {"total": 5e6})
    assert st.finish(retry_narrative=retry_cb) is None
    assert st.headline == "Result: 5.00 mln."


def test_state_headline_uz_locale():
    st = _state(BAD_T, {"uzs_total": UZS}, lang="uz")
    assert st.finish(retry_narrative=lambda kind: None) is None
    assert st.headline == H_UZ


def test_state_headline_none_when_narrative_used():
    st = _state("Total {{total}}.", {"total": 5})
    assert st.finish() == "Total 5."
    assert st.headline is None
    st2 = _state(BAD_T, {"total": 5})
    assert st2.finish(retry_narrative=lambda kind: {"narrative": "Total {{total}}."}) == "Total 5."
    assert st2.headline is None


def test_state_headline_none_without_template_or_block():
    st = _state(None, {"total": 5})
    assert st.finish() is None and st.headline is None
    st2 = _state(BAD_T, {"total": 5}, ok_blocks=0)
    assert st2.finish(retry_narrative=lambda kind: None) is None and st2.headline is None


def test_state_headline_none_without_matching_key():
    st = _state(BAD_T, {"count": 5})
    assert st.finish(retry_narrative=lambda kind: None) is None
    assert st.headline is None


# ======================================================================
# Flows
# ======================================================================

class Brain:
    def __init__(self, plan, retry=None):
        self.plan_reply = plan
        self.retry_reply = retry
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
            return dict(r or {"kind": "PLOT_CODE", "code": REPLY_CODE, "usage": {}})

        def describe(**kw):
            self.calls["describe"].append(kw)
            return {"text": "described", "usage": {}}

        def summarize(**kw):
            self.calls["summarize"].append(kw)
            return {"text": "summarized", "usage": {}}

        monkeypatch.setattr(bc, "plan", plan)
        monkeypatch.setattr(bc, "retry", retry)
        monkeypatch.setattr(bc, "describe", describe)
        monkeypatch.setattr(bc, "summarize", summarize)
        return self


def _multi_plan(code, template, lang=None):
    out = {"raw_text": f"```plot_code\n{code}\n```\n###NEXT_PLOT###\n", "kind": "PLOT_CODE",
           "code": "", "usage": {}, "narrative": template}
    if lang:
        out["context_decision"] = {"answer_language": lang}
    return out


def _single_plan(code, template, lang=None):
    out = {"kind": "PYTHON", "code": code, "raw_text": "", "usage": {}, "narrative": template}
    if lang:
        out["context_decision"] = {"answer_language": lang}
    return out


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


def _run(dfs, question="qancha miqdorda kredit ajratildi"):
    return list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question=question,
        history_rows=[], user_email="alice@acme.com"))


def _done(events):
    done = [e for e in events if e.get("done")]
    assert len(done) == 1, events
    return done[0]


def _single(events):
    assert len(events) == 1 and events[0].get("single_response"), events
    return events[0]["result"]


def _no_value(text, where):
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    for bad in ("61,58", "61.58", "61576335238995", "trln"):
        assert bad not in s, f"{where}: {bad!r} leaked into {s[:300]!r}"


def test_multi_dropped_template_shows_headline(monkeypatch, dfs, caplog):
    brain = Brain(_multi_plan(CODE_A, BAD_T, lang="Uzbek")).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart({"uzs_total": UZS})})

    with caplog.at_level(logging.INFO, logger="datachat"):
        events = _run(dfs)

    assert len(brain.calls["retry"]) == 1                       # the narrative-only retry
    narr = [e for e in events if e.get("narrative")]
    assert len(narr) == 1, events
    assert narr[0]["text"] == H_UZ and narr[0]["phase"] == 1
    assert not narr[0].get("template")                          # not a narrative template
    done = _done(events)
    assert events.index(narr[0]) < events.index(done)
    assert done["display_answer"] == H_UZ + "\n\n" + "described"
    assert done["narrative_view"]["headline_display"] == H_UZ
    # R0: the brain-safe text never carries the figure
    assert done["combined_answer"] == "described"
    _no_value(done["combined_answer"], "combined_answer")
    assert any("NARRATIVE_HEADLINE_FALLBACK key=uzs_total lang=uz" in m
               for m in _msgs(caplog)), _msgs(caplog)


def test_multi_timed_out_retry_shows_headline(monkeypatch, dfs):
    def retry(kw):
        raise brain_client.BrainTimeoutError("Brain timed out: ReadTimeout")

    Brain(_multi_plan(CODE_A, BAD_T), retry=retry).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart({"uzs_total": UZS})})

    events = _run(dfs, question="how much credit was issued?")

    done = _done(events)
    assert done["display_answer"] == H_EN + "\n\n" + "described"
    assert done["narrative_view"]["headline_display"] == H_EN
    _no_value(done["combined_answer"], "combined_answer")


def test_multi_without_matching_key_has_no_headline(monkeypatch, dfs):
    Brain(_multi_plan(CODE_A, BAD_T)).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart({"count": 5})})

    events = _run(dfs, question="how much credit was issued?")

    assert [e for e in events if e.get("narrative")] == []
    done = _done(events)
    assert done["display_answer"] == "described"
    assert not done["narrative_view"].get("headline_display")


def test_multi_used_narrative_has_no_headline(monkeypatch, dfs):
    Brain(_multi_plan(CODE_A, "Total {{uzs_total:amount}}.")).install(monkeypatch)
    _install_renders(monkeypatch, charts={CODE_A: _chart({"uzs_total": UZS})})

    events = _run(dfs, question="how much credit was issued?")

    narr = [e for e in events if e.get("narrative")]
    assert [e["text"] for e in narr] == ["Total 61.58 trln."]
    done = _done(events)
    assert not done["narrative_view"].get("headline_display")
    assert done["display_answer"] == "Total 61.58 trln."


def test_single_table_dropped_template_has_headline_display(monkeypatch, dfs):
    code = "VALUES['uzs_total'] = total_of_uzs\nRESULT = dfs['f.csv']"
    Brain(_single_plan(code, BAD_T, lang="Uzbek"),
          retry={"kind": "PYTHON", "code": "RESULT = 0", "usage": {}}).install(monkeypatch)
    _install_renders(monkeypatch, tables={code: _table({"uzs_total": UZS})})

    result = _single(_run(dfs))

    assert result["headline_display"] == H_UZ
    assert result["text"] == "described"
    _no_value(result["text"], "text")
    assert "narrative_rendered" not in result and "value_display" not in result


def test_single_scalar_value_display_gets_no_headline(monkeypatch, dfs):
    """The scalar's `value_display` line already shows the figure (the
    implicit `result` slot would otherwise make it the headline)."""
    Brain(_single_plan("RESULT = 42", "{{missing}}")).install(monkeypatch)

    result = _single(_run(dfs, question="what is the total?"))

    assert result["value_display"] == "**Result:** 42"
    assert "headline_display" not in result
    assert result["text"] == "summarized"


def test_single_flat_dict_value_display_gets_no_headline(monkeypatch, dfs):
    code = "VALUES['total'] = 5000000\nRESULT = {'a': 1, 'b': 2}"
    Brain(_single_plan(code, "{{missing}}")).install(monkeypatch)

    result = _single(_run(dfs, question="what is the total?"))

    assert result.get("value_display")
    assert "headline_display" not in result
    assert result["text"] == "summarized"
    assert "mln" not in result["text"]


def test_single_without_matching_key_has_no_headline(monkeypatch, dfs):
    code = "VALUES['count'] = 3\nRESULT = dfs['f.csv']"
    Brain(_single_plan(code, BAD_T),
          retry={"kind": "PYTHON", "code": "RESULT = 0", "usage": {}}).install(monkeypatch)
    _install_renders(monkeypatch, tables={code: _table({"count": 3})})

    result = _single(_run(dfs, question="how much?"))

    assert "headline_display" not in result
    assert result["text"] == "described"


# ======================================================================
# routes/chat.py: display, record, brain-bound history
# ======================================================================

def test_single_display_with_headline():
    import routes.chat as chat_mod
    h = "Result: 5.00 mln."
    assert chat_mod._single_display({"text": "described", "headline_display": h}) == {
        "answer": h + "\n\n" + "described", "narrative": h, "caption": "described"}
    # a caption-only answer: the base is the rendered caption
    capped = {"text": "cap {{a}}", "caption_template": "cap {{a}}", "caption_rendered": "cap 1",
              "headline_display": h}
    assert chat_mod._single_display(capped) == {
        "answer": h + "\n\n" + "cap 1", "narrative": h, "caption": "cap 1"}


def test_single_display_headline_ignored_with_narrative_template():
    import routes.chat as chat_mod
    res = {"text": "[x]", "narrative_template": "T {{a}}", "narrative_rendered": "T 1",
           "headline_display": "Result: 9."}
    assert chat_mod._single_display(res) == {"answer": "T 1", "narrative": "T 1", "caption": ""}


def test_single_display_without_headline_unchanged():
    import routes.chat as chat_mod
    assert chat_mod._single_display({"text": "described"}) == {"answer": "described"}


def test_record_single_keeps_headline_display_content_brain_safe():
    import routes.chat as chat_mod
    single = {"text": "described", "image_base64": None, "table": None, "code": "RESULT = 1",
              "usage": {}, "headline_display": H_UZ}
    rec = chat_mod._build_ai_history_record(None, single, None, [], {}, [])
    assert rec["headline_display"] == H_UZ
    assert rec["content"] == "described"
    sent = brain_client._sanitize_history_rows([rec])
    assert sent == [{"role": "ai", "content": "described", "code": "RESULT = 1"}]
    _no_value(sent, "history rows")


def test_record_multi_keeps_headline_display_from_narrative_view():
    import routes.chat as chat_mod
    rec = {"role": "ai", "content": "described"}
    src = {"narrative_view": {"phase1": None, "phase2": None, "phase2_status": None,
                              "headline_display": H_UZ}}
    out = chat_mod._apply_narrative_fields(rec, src)
    assert out["headline_display"] == H_UZ
    assert out["content"] == "described"
    _no_value(brain_client._sanitize_history_rows([out]), "history rows")


def test_record_without_headline_keeps_old_shape():
    import routes.chat as chat_mod
    rec = chat_mod._apply_narrative_fields(
        {"role": "ai", "content": "x"},
        {"narrative_view": {"phase1": None, "phase2": None, "phase2_status": None}})
    assert "headline_display" not in rec
    rec2 = chat_mod._apply_narrative_fields({"role": "ai", "content": "x"}, {"text": "x"})
    assert "headline_display" not in rec2
