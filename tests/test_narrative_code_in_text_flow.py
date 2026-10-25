"""Code-in-text gates of the run_chat_local flows (BRB run-3 follow-up;
written BEFORE the implementation).

BRB run 3, en q4: the planner answered with kind CLARIFICATION whose "text"
was a whole fenced Python program, and the user saw the code as the answer.
The contract:
- a prose kind (CLARIFICATION / ANSWER / MISSING_DATA) whose text is code
  (`narrative.code_in_text(code, what="answer")`) is EXECUTED as the code of
  its first fenced block (`_code_from_prose_reply`) down the normal path, and
  the answer comes from describe / summarize — never the code text;
- a rendered narrative that is code is not used (`NARRATIVE_SKIPPED
  reason=code_in_text`), a rendered caption that is code sends its visual to
  describe (`CAPTION_SKIPPED block=<n> reason=code_in_text`).

Every brain call is stubbed (`run_chat_local.brain_client.{plan, retry,
describe, summarize}`) and both executors are recording fakes: nothing runs.
"""
import logging

import pandas as pd
import pytest

import run_chat_local
from settings import settings

CAPS = "narrative_v1,diagnostics_v1"

Q4_CODE = """# answer_language: English
import pandas as pd
import re

df = dfs['sales.csv']
MONTHS = {
    'january': 1, 'february': 2, 'march': 3,
    'april': 4, 'may': 5, 'june': 6,
}

def parse_month(s):
    m = re.match(r'(\\w+)', str(s).lower())
    return MONTHS.get(m.group(1)) if m else None

df['month_num'] = df['month'].map(parse_month)
VALUES['new_items'] = int((df['status'] == 'new').sum())
RESULT = df.groupby('region')['amount'].sum()"""

Q4_REPLY = "```python\n" + Q4_CODE + "\n```"
CODE_LINES = "import pandas as pd\nx = 1\nprint(x)"
PROSE_KINDS = ("CLARIFICATION", "ANSWER", "MISSING_DATA")


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def dfs():
    return {"sales.csv": pd.DataFrame({"region": ["North", "South"], "amount": [10, 20],
                                       "month": ["january", "march"],
                                       "status": ["new", "old"]})}


@pytest.fixture(autouse=True)
def _stub_schema(monkeypatch):
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")


@pytest.fixture(autouse=True)
def _caps_on(monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", CAPS)


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG, logger="datachat")
    return caplog


def _lines(caplog, needle):
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


class Brain:
    """Recording stubs for the brain calls the flows make."""

    def __init__(self, plan, describe_text="described", summarize_text="summarized"):
        self.plan_reply = plan
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
            return {"kind": "NO_CODE", "code": "", "usage": {}}

        def describe(**kw):
            self.calls["describe"].append(kw)
            return {"text": self.describe_text, "usage": {}}

        def summarize(**kw):
            self.calls["summarize"].append(kw)
            return {"text": self.summarize_text, "usage": {}}

        monkeypatch.setattr(bc, "plan", plan)
        monkeypatch.setattr(bc, "retry", retry)
        monkeypatch.setattr(bc, "describe", describe)
        monkeypatch.setattr(bc, "summarize", summarize)
        return self


def _scalar_out(values=None):
    return {"error": None, "result": 5, "preview": 5, "image_base64": None,
            "values": dict(values or {}), "values_dropped": 0,
            "findings": {}, "findings_dropped": 0}


def _table_out():
    return {"error": None, "result": pd.DataFrame({"region": ["North", "South"],
                                                   "amount": [10, 20]}),
            "preview": None, "image_base64": None,
            "values": {}, "values_dropped": 0, "findings": {}, "findings_dropped": 0}


def _chart_out():
    return {"ok": True, "error": None, "is_plotly": True, "plotly_html": "<div>chart</div>",
            "chart_data": None, "values": {}, "values_dropped": 0,
            "findings": {}, "findings_dropped": 0}


def _recorders(monkeypatch, python_out=None, plot_out=None):
    """Recording executors; returns {"python": [codes], "plot": [codes]}."""
    calls = {"python": [], "plot": []}

    def execute(code, dfs, sid, **kw):
        calls["python"].append(code)
        return dict(python_out if python_out is not None else _scalar_out())

    def render(code, dfs, sid, **kw):
        calls["plot"].append(code)
        return dict(plot_out if plot_out is not None else _chart_out())

    monkeypatch.setattr(run_chat_local, "safe_execute", execute)
    monkeypatch.setattr(run_chat_local, "render_plot_safe", render)
    return calls


def _plan(kind, code, **extra):
    out = {"raw_text": "", "kind": kind, "code": code, "usage": {}, "context_decision": {}}
    out.update(extra)
    return out


def _run_multi(dfs, question="how many new items by region?"):
    return list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question=question,
        history_rows=[], user_email="alice@acme.com"))


def _single(events):
    assert len(events) == 1 and events[0].get("single_response"), events
    return events[0]["result"]


def _run_chat(dfs, question="how many new items by region?"):
    return run_chat_local.run_chat(
        sid="t", dfs=dfs, schema_docs={}, question=question,
        history_rows=[], user_email="alice@acme.com")


def _assert_code_executed(calls):
    assert calls["plot"] == []
    assert len(calls["python"]) == 1, calls
    ran = calls["python"][0]
    assert "```" not in ran
    assert ran.strip() == Q4_CODE.strip()


def _assert_no_code_shown(text):
    assert isinstance(text, str)
    assert "```" not in text
    assert "import pandas" not in text
    assert "RESULT =" not in text


# =================================================== prose kinds holding code

@pytest.mark.parametrize("kind", PROSE_KINDS)
def test_single_flow_prose_kind_with_fenced_code_is_executed(monkeypatch, dfs, logs, kind):
    brain = Brain(_plan(kind, Q4_REPLY)).install(monkeypatch)
    calls = _recorders(monkeypatch)

    result = _single(_run_multi(dfs))

    _assert_code_executed(calls)
    _assert_no_code_shown(result["text"])
    assert result["text"] == "summarized"
    assert len(brain.calls["summarize"]) == 1
    assert result.get("code") and "```" not in result["code"]
    lines = _lines(logs, "NARRATIVE_CODE_IN_TEXT")
    assert any("what=answer" in line for line in lines), lines
    blob = "\n".join(r.getMessage() for r in logs.records if "NARRATIVE_CODE_IN_TEXT" in r.getMessage())
    assert "import pandas" not in blob


def test_single_flow_clarification_with_table_code_is_described(monkeypatch, dfs):
    brain = Brain(_plan("CLARIFICATION", Q4_REPLY)).install(monkeypatch)
    calls = _recorders(monkeypatch, python_out=_table_out())

    result = _single(_run_multi(dfs))

    _assert_code_executed(calls)
    assert result["text"] == "described"
    assert len(brain.calls["describe"]) == 1
    assert result["table"] is not None
    _assert_no_code_shown(result["text"])


def test_single_flow_unfenced_code_lines_are_executed(monkeypatch, dfs):
    Brain(_plan("CLARIFICATION", Q4_CODE)).install(monkeypatch)
    calls = _recorders(monkeypatch)

    result = _single(_run_multi(dfs))

    _assert_code_executed(calls)
    assert result["text"] == "summarized"


@pytest.mark.parametrize("kind", PROSE_KINDS)
def test_run_chat_prose_kind_with_fenced_code_is_executed(monkeypatch, dfs, logs, kind):
    brain = Brain(_plan(kind, Q4_REPLY)).install(monkeypatch)
    calls = _recorders(monkeypatch)

    out = _run_chat(dfs)

    _assert_code_executed(calls)
    _assert_no_code_shown(out["text"])
    assert out["text"] == "summarized"
    assert len(brain.calls["summarize"]) == 1
    assert any("what=answer" in line for line in _lines(logs, "NARRATIVE_CODE_IN_TEXT"))


def test_run_chat_fenced_plot_code_goes_to_the_plot_executor(monkeypatch, dfs):
    plot_code = ("import plotly.express as px\n"
                 "d = dfs['sales.csv']\n"
                 "fig = px.bar(d, x='region', y='amount')")
    brain = Brain(_plan("CLARIFICATION", "```python\n" + plot_code + "\n```")).install(monkeypatch)
    calls = _recorders(monkeypatch)

    out = _run_chat(dfs)

    assert calls["python"] == []
    assert len(calls["plot"]) == 1
    assert calls["plot"][0].strip() == plot_code
    assert out["text"] == "described"
    assert len(brain.calls["describe"]) == 1


@pytest.mark.parametrize("kind", PROSE_KINDS)
def test_plain_prose_is_unchanged(monkeypatch, dfs, logs, kind):
    question_text = "Which region do you mean?"
    brain = Brain(_plan(kind, question_text)).install(monkeypatch)
    calls = _recorders(monkeypatch)

    result = _single(_run_multi(dfs))
    out = _run_chat(dfs)

    assert result["text"] == question_text
    assert out["text"] == question_text
    assert calls == {"python": [], "plot": []}
    assert brain.calls["summarize"] == [] and brain.calls["describe"] == []
    assert _lines(logs, "NARRATIVE_CODE_IN_TEXT") == []


# ============================================ templates that render to code

@pytest.mark.parametrize("template", [CODE_LINES, "```python\nx = 1\n```"],
                         ids=["code_lines", "fenced"])
def test_single_narrative_rendering_code_is_not_used(monkeypatch, dfs, logs, template):
    brain = Brain(_plan("PYTHON", "RESULT = 5", narrative=template)).install(monkeypatch)
    _recorders(monkeypatch)

    result = _single(_run_multi(dfs))

    assert "narrative_rendered" not in result
    assert result["text"] == "summarized"
    assert len(brain.calls["summarize"]) == 1
    _assert_no_code_shown(result["text"])
    assert _lines(logs, "NARRATIVE_SKIPPED reason=code_in_text"), "missing NARRATIVE_SKIPPED"
    assert any("what=narrative" in line for line in _lines(logs, "NARRATIVE_CODE_IN_TEXT"))


def test_single_caption_rendering_code_goes_to_describe(monkeypatch, dfs, logs):
    plan = _plan("PYTHON", "RESULT = dfs['sales.csv']", captions={"1": CODE_LINES})
    brain = Brain(plan).install(monkeypatch)
    _recorders(monkeypatch, python_out=_table_out())

    result = _single(_run_multi(dfs))

    assert len(brain.calls["describe"]) == 1
    assert result["text"] == "described"
    assert "caption_rendered" not in result
    assert _lines(logs, "CAPTION_SKIPPED block=1 reason=code_in_text")
    assert any("what=caption" in line for line in _lines(logs, "NARRATIVE_CODE_IN_TEXT"))


def _raw(*blocks):
    body = "\n###NEXT_PLOT###\n".join(f"```{fence}\n{code}\n```" for fence, code in blocks)
    return body + "\n###NEXT_PLOT###\n"


CHART_CODE = "fig = chart_a\nVALUES['a'] = 1"


def test_multi_caption_and_narrative_rendering_code(monkeypatch, dfs, logs):
    plan = {"raw_text": _raw(("plot_code", CHART_CODE)), "kind": "PLOT_CODE", "code": "",
            "usage": {}, "narrative": CODE_LINES, "captions": {"1": CODE_LINES}}
    brain = Brain(plan).install(monkeypatch)
    calls = _recorders(monkeypatch)

    events = _run_multi(dfs)

    assert calls["plot"] == [CHART_CODE]
    partials = [e for e in events if e.get("partial") and e.get("image_base64")]
    assert len(partials) == 1
    assert partials[0]["answer"] == "described"
    assert len(brain.calls["describe"]) == 1
    assert [e for e in events if e.get("narrative")] == []
    done = [e for e in events if e.get("done")]
    assert len(done) == 1
    _assert_no_code_shown(done[0].get("display_answer") or "")
    assert _lines(logs, "CAPTION_SKIPPED block=1 reason=code_in_text")
    assert _lines(logs, "NARRATIVE_SKIPPED reason=code_in_text")


# ============================== only RUNNABLE Python is taken from prose

SQL_ANSWER = ("The amount comes from this query:\n"
              "```sql\n"
              "SELECT region, SUM(amount) AS total\n"
              "FROM sales\n"
              "GROUP BY region\n"
              "```\n"
              "It sums the amount per region.")
JSON_ANSWER = ('The settings are:\n```json\n{"a": 1,\n "b": 2,\n "c": 3}\n```')


@pytest.mark.parametrize("text", [SQL_ANSWER, JSON_ANSWER], ids=["sql", "json"])
def test_answer_with_a_non_python_fence_stays_an_answer(monkeypatch, dfs, logs, text):
    brain = Brain(_plan("ANSWER", text)).install(monkeypatch)
    calls = _recorders(monkeypatch)

    result = _single(_run_multi(dfs))
    out = _run_chat(dfs)

    assert result["text"] == text
    assert out["text"] == text
    assert calls == {"python": [], "plot": []}
    assert brain.calls["summarize"] == [] and brain.calls["describe"] == []
    assert _lines(logs, "NARRATIVE_CODE_IN_TEXT") == []


def test_missing_data_prose_without_code_is_unchanged(monkeypatch, dfs, logs):
    text = ("The data has no customer age column.\n"
            "Add a file with customer ages (one row per customer).\n"
            "Then ask again.")
    Brain(_plan("MISSING_DATA", text)).install(monkeypatch)
    calls = _recorders(monkeypatch)

    result = _single(_run_multi(dfs))
    out = _run_chat(dfs)

    assert result["text"] == text and out["text"] == text
    assert calls == {"python": [], "plot": []}
    assert _lines(logs, "NARRATIVE_CODE_IN_TEXT") == []


def test_bare_fence_python_reply_is_executed_once_logged(monkeypatch, dfs, logs):
    reply = "Here is the analysis:\n```\n" + Q4_CODE + "\n```\nDone."
    Brain(_plan("CLARIFICATION", reply)).install(monkeypatch)
    calls = _recorders(monkeypatch)

    result = _single(_run_multi(dfs))

    _assert_code_executed(calls)
    assert result["text"] == "summarized"
    lines = _lines(logs, "NARRATIVE_CODE_IN_TEXT")
    assert len(lines) == 1 and "what=answer" in lines[0], lines


def test_one_line_python_fence_is_executed_not_shown(monkeypatch, dfs, logs):
    """A ```python fence declares its content code: a single parsing line
    runs (the 3-line window of contains_code would not see it)."""
    reply = "```python\nRESULT = dfs['sales.csv']['amount'].sum()\n```"
    Brain(_plan("CLARIFICATION", reply)).install(monkeypatch)
    calls = _recorders(monkeypatch)

    result = _single(_run_multi(dfs))

    assert [c.strip() for c in calls["python"]] == [
        "RESULT = dfs['sales.csv']['amount'].sum()"]
    assert "```" not in result["text"]
    assert len(_lines(logs, "NARRATIVE_CODE_IN_TEXT")) == 1


def test_one_line_bare_fence_prose_stays_an_answer(monkeypatch, dfs):
    """A bare fence must look like code: a quoted label is not run."""
    text = "Did you mean this column?\n```\nRevenue (USD)\n```"
    Brain(_plan("CLARIFICATION", text)).install(monkeypatch)
    calls = _recorders(monkeypatch)

    result = _single(_run_multi(dfs))

    assert result["text"] == text
    assert calls == {"python": [], "plot": []}


def test_python_fence_after_a_sql_fence_is_executed(monkeypatch, dfs):
    reply = SQL_ANSWER + "\n```python\n" + Q4_CODE + "\n```"
    Brain(_plan("ANSWER", reply)).install(monkeypatch)
    calls = _recorders(monkeypatch)

    out = _run_chat(dfs)

    _assert_code_executed(calls)
    assert out["text"] == "summarized"


def test_unparsable_unfenced_text_stays_an_answer(monkeypatch, dfs):
    text = "import pandas as pd\nThe total is 5.\nprint(total)"
    Brain(_plan("ANSWER", text)).install(monkeypatch)
    calls = _recorders(monkeypatch)

    out = _run_chat(dfs)

    assert out["text"] == text
    assert calls == {"python": [], "plot": []}
