"""An infrastructure failure of the analysis sandbox is not a coding mistake.

WHY: `run_chat_local`'s three retry loops were written when generated code
ran in-process, where every `error` string really was the code's fault. Since
the sandbox hop landed, the same `error` channel also carries
`ExecutorUnavailable`, `ExecutorBusy`, `ExecutorError` and
`ExecutorResponseError` — and the loops answer those by asking the Brain to
REWRITE the code, three times, each rewrite paying another dispatch against a
service that is down. That is a per-question amplification of three Brain
calls and three sandbox round-trips for a condition no code change can fix,
and the user ends up with "Try rephrasing" for an outage.

So `executor_client.is_infrastructure_error(text)` decides, BEFORE
`brain_client.retry` is called, whether the planner can do anything about the
failure. `TimeoutError`, `MemoryError` and `ResultTooLarge` deliberately keep
retrying (cheaper code is a real fix), and so does an `ExecutorCrashError`
whose `reason` is `exit` or `signal` — those crashes are what the generated
code itself did (`sys.exit`, a provoked segfault), so a rewrite may help.

Offline: every brain call and both exec entry points are stubbed. The autouse
conftest fixture and a test's own `monkeypatch` are the same function-scoped
instance and `undo` runs in reverse, so the test's patch wins. Every asserted
value is bound to a local first.
"""
import pandas as pd
import pytest

import executor_client
import run_chat_local

BUSY_SENTENCE = ("The analysis service is busy right now. "
                 "Please try again in a moment.")
UNAVAILABLE_SENTENCE = ("The analysis service is not available right now. "
                        "Please try again in a moment or contact your "
                        "administrator.")
REPHRASE_SENTENCE = ("I couldn't run this analysis with your current data. "
                     "Try rephrasing or simplifying the request.")

CRASH_EXIT = ("ExecutorCrashError: the analysis process exited unexpectedly "
              "(exit=3, signal=None, reason=exit)")
CRASH_SIGNAL = ("ExecutorCrashError: the analysis process exited unexpectedly "
                "(exit=-11, signal=11, reason=signal)")
CRASH_SPAWN = ("ExecutorCrashError: the analysis process exited unexpectedly "
               "(exit=None, signal=None, reason=spawn_failed)")
CRASH_UNKNOWN = ("ExecutorCrashError: the analysis process exited unexpectedly "
                 "(exit=1, signal=None, reason=unknown)")

TIMEOUT_TEXT = "TimeoutError: Code execution exceeded 60 seconds limit"
MEMORY_TEXT = "MemoryError: execution exceeded the memory limit"
TOO_LARGE_TEXT = "ResultTooLarge: the result parquet decodes to 900 MB"
PLAIN_ERROR = "KeyError: 'city'"

CODE = "RESULT = df.head()"
PLOT_CODE = "fig = px.bar(df, x='a', y='b')"


def _predicate():
    """Bound through one helper so the missing-implementation failure is one
    named assertion instead of an AttributeError in every test."""
    fn = getattr(executor_client, "is_infrastructure_error", None)
    assert callable(fn), (
        "executor_client must expose is_infrastructure_error(text) -> bool — "
        "the predicate the three retry loops consult before brain_client.retry")
    return fn


@pytest.fixture
def dfs():
    return {"f.csv": pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})}


@pytest.fixture(autouse=True)
def _stub_schema(monkeypatch):
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")


@pytest.fixture
def retry_calls(monkeypatch):
    """Every `brain_client.retry` call this module's flows make."""
    calls = []

    def fake_retry(**kwargs):
        calls.append(kwargs)
        return {"kind": "PYTHON", "code": "RESULT = 2", "usage": {}}

    monkeypatch.setattr(run_chat_local.brain_client, "retry", fake_retry)
    monkeypatch.setattr(run_chat_local.brain_client, "describe",
                        lambda **k: {"text": "described", "usage": {}})
    monkeypatch.setattr(run_chat_local.brain_client, "greeting",
                        lambda *a, **k: {"text": "hi", "usage": {}})
    return calls


def _plan(kind, code):
    return lambda **k: {"kind": kind, "code": code, "usage": {},
                        "raw_text": code, "context_decision": {}}


def _plan_with_blocks(n: int):
    """A planner raw_text with `n` ###NEXT_PLOT###-separated plot blocks (the
    idiom from tests/test_retry_loop.py)."""
    blocks = "\n###NEXT_PLOT###\n".join(
        f"```plot_code\nplot_block_{i}\n```" for i in range(n))
    blocks += "\n###NEXT_PLOT###\n"
    return lambda **k: {"raw_text": blocks, "usage": {}, "kind": "PLOT_CODE"}


def _counting_exec(error_text, calls: list):
    def run(code, dfs, sid=None, **kwargs):
        calls.append(code)
        return {"error": error_text}
    return run


# ---------------------------------------------------------------------------
# the predicate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "ExecutorUnavailable: the analysis service is not reachable",
    "ExecutorBusy: the analysis service is busy, try again",
    "ExecutorError: cannot prepare the job (OSError)",
    "ExecutorError: the analysis service rejected the job (BAD_JOB_ID)",
    "ExecutorError: the analysis answer could not be read (ValueError)",
    "ExecutorResponseError: unknown status",
    CRASH_SPAWN,
    CRASH_UNKNOWN,
])
def test_an_executor_condition_is_infrastructure(text):
    """The closed `Executor*` set: no rewrite of the generated code can fix
    any of these, so the Brain must not be asked to try."""
    verdict = _predicate()(text)
    assert verdict is True, (text, verdict)


@pytest.mark.parametrize("text", [CRASH_EXIT, CRASH_SIGNAL])
def test_a_crash_the_generated_code_caused_still_retries(text):
    """`reason=exit` / `reason=signal` mean the runner died of what the code
    did — `sys.exit`, a provoked segfault. The planner CAN rewrite that, so
    these two are the documented exception inside the `Executor*` prefix."""
    verdict = _predicate()(text)
    assert verdict is False, (text, verdict)


@pytest.mark.parametrize("text", [
    TIMEOUT_TEXT, MEMORY_TEXT, TOO_LARGE_TEXT, PLAIN_ERROR,
    "ValueError: could not convert", "",
])
def test_a_code_level_failure_is_not_infrastructure(text):
    """Cheaper code is a real fix for a timeout, an out-of-memory run and an
    oversized result — those keep their three retries."""
    verdict = _predicate()(text)
    assert verdict is False, (text, verdict)


@pytest.mark.parametrize("value", [None, 0, 1.5, [], {}, ("Executor",), object()])
def test_a_non_string_is_never_infrastructure(value):
    """The predicate sits on the hot error path of four call sites and must
    never raise on whatever the dict happened to hold."""
    verdict = _predicate()(value)
    assert verdict is False, (repr(value), verdict)


# ---------------------------------------------------------------------------
# loop 1 — run_chat (the single-response path)
# ---------------------------------------------------------------------------
def _run_chat(dfs):
    return run_chat_local.run_chat(
        sid="t", dfs=dfs, schema_docs={}, question="how many rows",
        history_rows=[], user_email="alice@acme.com")


def test_run_chat_does_not_retry_an_unavailable_sandbox(monkeypatch, dfs, retry_calls):
    """Zero Brain calls, ONE execution attempt, and the user is told the
    service is down instead of being asked to rephrase."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan("PYTHON", CODE))
    execs = []
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec(executor_client.UNAVAILABLE_TEXT, execs))

    out = _run_chat(dfs)

    assert retry_calls == [], retry_calls
    assert len(execs) == 1, execs
    text = out.get("text")
    assert text == UNAVAILABLE_SENTENCE, text
    assert text != REPHRASE_SENTENCE, text
    assert out.get("code") == CODE, out.get("code")


def test_run_chat_names_a_busy_sandbox_separately(monkeypatch, dfs, retry_calls):
    """A queue that expired is not an outage — the user is told to retry, not
    to call an administrator."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan("PYTHON", CODE))
    execs = []
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec(executor_client.BUSY_TEXT, execs))

    out = _run_chat(dfs)

    assert retry_calls == [], retry_calls
    assert len(execs) == 1, execs
    text = out.get("text")
    assert text == BUSY_SENTENCE, text


def test_run_chat_logs_the_infrastructure_short_circuit(monkeypatch, dfs, retry_calls):
    """The operator must be able to tell an outage from a bad question."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan("PYTHON", CODE))
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec(executor_client.UNAVAILABLE_TEXT, []))
    lines = []
    monkeypatch.setattr(run_chat_local, "log_with_sid",
                        lambda sid, level, message, *a, **k: lines.append(message))

    _run_chat(dfs)

    hits = [line for line in lines if "EXEC_INFRA_ERROR" in line]
    assert len(hits) == 1, lines


def test_run_chat_still_retries_a_code_error_three_times(monkeypatch, dfs, retry_calls):
    """REGRESSION GUARD: the short-circuit must not swallow the ordinary
    retry policy (the assertion style of tests/test_retry_loop.py)."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan("PYTHON", CODE))
    execs = []
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec("SparseChartError", execs))

    out = _run_chat(dfs)

    assert len(retry_calls) == 3, retry_calls
    assert [c["use_pro"] for c in retry_calls] == [False, True, True], retry_calls
    text = out.get("text")
    assert text == REPHRASE_SENTENCE, text


def test_run_chat_plot_path_short_circuits_too(monkeypatch, dfs, retry_calls):
    """The chart branch shares the loop — `render_plot_safe` is the executor
    for `PLOT_CODE`, and its dispatch fails the same way."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan("PLOT_CODE", PLOT_CODE))
    renders = []
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        _counting_exec(executor_client.UNAVAILABLE_TEXT, renders))

    out = _run_chat(dfs)

    assert retry_calls == [], retry_calls
    assert len(renders) == 1, renders
    text = out.get("text")
    assert text == UNAVAILABLE_SENTENCE, text


# ---------------------------------------------------------------------------
# loop 2 — run_chat_multi_plot (the worklist)
# ---------------------------------------------------------------------------
def _run_multi(dfs):
    return list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question="dashboard",
        history_rows=[], user_email="alice@acme.com"))


def test_multi_plot_stops_the_whole_worklist_on_the_first_outage(monkeypatch, dfs,
                                                                 retry_calls):
    """Three blocks, the first one unavailable: the remaining two must NOT be
    attempted. They would each wait the dispatch queue for the same answer,
    and the user would wait three timeouts for one outage."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan_with_blocks(3))
    renders = []
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        _counting_exec(executor_client.UNAVAILABLE_TEXT, renders))

    events = _run_multi(dfs)

    assert retry_calls == [], retry_calls
    assert len(renders) == 1, renders
    partials = [e for e in events if e.get("partial")]
    assert partials == [], partials


def test_multi_plot_done_event_carries_the_sentence_exactly_once(monkeypatch, dfs,
                                                                 retry_calls):
    """Zero charts would otherwise produce "Something went wrong with this
    analysis." — true but useless. The sentence must appear ONCE, not once
    per abandoned block."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan_with_blocks(3))
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        _counting_exec(executor_client.UNAVAILABLE_TEXT, []))

    events = _run_multi(dfs)

    done = [e for e in events if e.get("done")]
    assert len(done) == 1, events
    answer = done[0].get("combined_answer")
    assert answer == UNAVAILABLE_SENTENCE, answer
    assert answer.count("The analysis service") == 1, answer


def test_multi_plot_python_rewrite_branch_short_circuits(monkeypatch, dfs):
    """The PYTHON-rewrite branch INSIDE the error loop runs its own
    `safe_execute`; without the predicate it burns three more sandbox
    round-trips after the render already failed."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan_with_blocks(1))
    renders = []
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        _counting_exec("NameError: px", renders))
    retry_calls = []

    def fake_retry(**kwargs):
        retry_calls.append(kwargs)
        return {"kind": "PYTHON", "code": "RESULT = df.head()", "usage": {}}

    monkeypatch.setattr(run_chat_local.brain_client, "retry", fake_retry)
    monkeypatch.setattr(run_chat_local.brain_client, "describe",
                        lambda **k: {"text": "described", "usage": {}})
    execs = []
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec(executor_client.UNAVAILABLE_TEXT, execs))

    events = _run_multi(dfs)

    assert len(execs) == 1, execs
    assert len(retry_calls) == 1, retry_calls
    done = [e for e in events if e.get("done")]
    assert len(done) == 1, events
    answer = done[0].get("combined_answer")
    assert answer == UNAVAILABLE_SENTENCE, answer


def test_multi_plot_still_retries_a_render_error_three_times(monkeypatch, dfs,
                                                             retry_calls):
    """REGRESSION GUARD, mirroring tests/test_retry_loop.py's contract."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan_with_blocks(1))
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        _counting_exec("SparseChartError", []))
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec("SparseChartError", []))

    events = _run_multi(dfs)

    assert len(retry_calls) == 3, retry_calls
    done = [e for e in events if e.get("done")]
    answer = done[0].get("combined_answer")
    assert answer == "Something went wrong with this analysis. Please try again.", answer


# ---------------------------------------------------------------------------
# loop 3 — _run_single_from_plan (the already-planned path)
# ---------------------------------------------------------------------------
def test_run_single_from_plan_short_circuits(monkeypatch, dfs, retry_calls):
    """`_run_single_from_plan` is reached through `run_chat_multi_plot` when
    the plan is not a chart (its only caller, run_chat_local.py:1090 — the
    shape Auto Analytics drives). It owns the third copy of the loop."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan("PYTHON", CODE))
    execs = []
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec(executor_client.UNAVAILABLE_TEXT, execs))

    events = _run_multi(dfs)

    singles = [e for e in events if e.get("single_response")]
    assert len(singles) == 1, events
    result = singles[0]["result"]
    assert retry_calls == [], retry_calls
    assert len(execs) == 1, execs
    text = result.get("text")
    assert text == UNAVAILABLE_SENTENCE, text
    assert result.get("code") == CODE, result.get("code")


def test_run_single_from_plan_names_a_busy_sandbox(monkeypatch, dfs, retry_calls):
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan("PYTHON", CODE))
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec(executor_client.BUSY_TEXT, []))

    events = _run_multi(dfs)

    singles = [e for e in events if e.get("single_response")]
    text = singles[0]["result"].get("text")
    assert text == BUSY_SENTENCE, text


def test_run_single_from_plan_still_retries_a_code_error(monkeypatch, dfs, retry_calls):
    """REGRESSION GUARD for the third loop."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan("PYTHON", CODE))
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec("SparseChartError", []))

    events = _run_multi(dfs)

    assert len(retry_calls) == 3, retry_calls
    singles = [e for e in events if e.get("single_response")]
    text = singles[0]["result"].get("text")
    assert text == "I couldn't run this analysis with your current data. Try rephrasing.", text


# ---------------------------------------------------------------------------
# mixed answers: the TABLE blocks pay the queue wait too
#
# A dashboard answer is a RESULT block plus chart blocks. The chart worklist
# and the table loop are separate passes over the SAME dispatch hop, so once
# one of them has established that the service is down, every remaining block
# in the other would queue for its own slot only to be told the same thing —
# the per-block wait the chart loop's own `break` exists to avoid, multiplied
# by however many tables the plan carried.
# ---------------------------------------------------------------------------
TABLE_CODE = "RESULT = df.head({n})"
CHART_CODE = "fig = px.bar(df, x='a', y='b')  # chart {n}"


def _plan_with_charts_and_tables(charts: int, tables: int):
    """A planner raw_text mixing chart and table blocks.

    `_looks_like_table_block` is what sorts them: a segment that assigns
    RESULT and touches no charting API is a table block.
    """
    segments = [f"```plot_code\n{CHART_CODE.format(n=i)}\n```" for i in range(charts)]
    segments += [f"```python\n{TABLE_CODE.format(n=i + 1)}\n```" for i in range(tables)]
    raw = "\n###NEXT_PLOT###\n".join(segments) + "\n###NEXT_PLOT###\n"
    return lambda **k: {"raw_text": raw, "usage": {}, "kind": "PLOT_CODE"}


def _table_success(frame):
    return {"error": None, "result": frame, "preview": None, "image_base64": None}


def test_a_chart_outage_stops_the_table_blocks_before_they_dispatch(monkeypatch, dfs,
                                                                    retry_calls):
    """The chart loop concluded the service is down; the three table blocks
    must not each queue for a slot to learn the same thing."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan",
                        _plan_with_charts_and_tables(charts=2, tables=3))
    renders = []
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        _counting_exec(executor_client.UNAVAILABLE_TEXT, renders))
    execs = []
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec(executor_client.UNAVAILABLE_TEXT, execs))

    events = _run_multi(dfs)

    assert len(renders) == 1, renders          # the chart worklist stopped
    assert execs == [], execs                  # and no table block dispatched
    assert retry_calls == [], retry_calls
    done = [event for event in events if event.get("done")]
    assert len(done) == 1, events
    answer = done[0].get("combined_answer")
    assert answer == UNAVAILABLE_SENTENCE, answer
    assert "tables" not in done[0], sorted(done[0])


def test_an_outage_starting_at_a_table_block_keeps_the_tables_already_built(monkeypatch,
                                                                           dfs,
                                                                           retry_calls):
    """The other direction: the charts were fine (there are none here) and the
    outage starts at the SECOND of three tables.

    Stopping must lose nothing that already exists — the first table is built
    and still rides the done event — while the third never dispatches.
    """
    import pandas as pd

    monkeypatch.setattr(run_chat_local.brain_client, "plan",
                        _plan_with_charts_and_tables(charts=0, tables=3))
    frame = pd.DataFrame({"city": ["Tbilisi"], "revenue": [100.0]})
    execs = []

    def flaky_exec(code, dfs, sid=None, **kwargs):
        execs.append(code)
        if len(execs) == 1:
            return _table_success(frame)
        return {"error": executor_client.UNAVAILABLE_TEXT}

    monkeypatch.setattr(run_chat_local, "safe_execute", flaky_exec)

    events = _run_multi(dfs)

    assert len(execs) == 2, execs              # the third block never ran
    assert retry_calls == [], retry_calls
    done = [event for event in events if event.get("done")]
    assert len(done) == 1, events
    tables = done[0].get("tables") or []
    assert len(tables) == 1, tables
    rows = tables[0].get("rows") or []
    assert len(rows) == 1, tables[0]
    answer = done[0].get("combined_answer") or ""
    assert UNAVAILABLE_SENTENCE in answer, answer
    assert answer.count("The analysis service") == 1, answer


def test_a_plain_table_block_error_does_not_stop_the_rest(monkeypatch, dfs,
                                                          retry_calls):
    """REGRESSION GUARD: only an INFRASTRUCTURE failure stops the loop. One
    block with a bad column must still let the others produce their tables —
    that was the behaviour before the short-circuit and it has to stay."""
    import pandas as pd

    monkeypatch.setattr(run_chat_local.brain_client, "plan",
                        _plan_with_charts_and_tables(charts=0, tables=3))
    frame = pd.DataFrame({"city": ["Tbilisi"], "revenue": [100.0]})
    execs = []

    def flaky_exec(code, dfs, sid=None, **kwargs):
        execs.append(code)
        if len(execs) == 2:
            return {"error": "KeyError: 'missing'"}
        return _table_success(frame)

    monkeypatch.setattr(run_chat_local, "safe_execute", flaky_exec)

    events = _run_multi(dfs)

    assert len(execs) == 3, execs              # every block was attempted
    done = [event for event in events if event.get("done")]
    tables = done[0].get("tables") or []
    assert len(tables) == 2, tables
    answer = done[0].get("combined_answer") or ""
    assert UNAVAILABLE_SENTENCE not in answer, answer


# ---------------------------------------------------------------------------
# no log site may write the execution error text raw
#
# The error text is authored by GENERATED CODE: `code_exec` builds it as
# `f"{type(e).__name__}: {e}"`, and the exception's message is whatever the
# code chose — including newlines. The log file is newline-delimited and is
# what an operator greps during an incident, so one embedded newline writes a
# second, complete, plausible-looking record that never happened.
#
# The escaping was first applied at ONE site, and a pin on that site alone
# passes while the others stay open — so there is one test PER SITE here. The
# property being pinned is not "the helper is correct", it is "no site writes
# it raw", and only a per-site test can say that.
# ---------------------------------------------------------------------------
FORGED_RECORD = "2026-09-17 00:00:00,000 | INFO | [sid=x] EXEC_OK job_id=forged"
FORGED_ERROR = f"ValueError: benign\n{FORGED_RECORD}"
FORGED_TOKEN = "job_id=forged"


def _log_capture(monkeypatch, module) -> list:
    """The idiom these modules already use: the name is imported into each
    module's own namespace, so the module attribute is the seam."""
    lines = []
    monkeypatch.setattr(module, "log_with_sid",
                        lambda sid, level, message, *a, **k: lines.append(str(message)))
    return lines


def _assert_single_line(messages: list, event: str, expected_count: int = 1) -> None:
    """Every record for `event` is ONE line, and nothing forged a second."""
    hits = [line for line in messages if line.startswith(event)]
    assert len(hits) == expected_count, (event, messages)
    for line in hits:
        assert "\n" not in line, (event, repr(line))
        assert "\r" not in line, (event, repr(line))
        # The text did arrive — escaped, not dropped.
        assert "\\n" in line, (event, line)
        assert FORGED_TOKEN in line, (event, line)
    # The forged record claims to be a DIFFERENT event; no record may.
    impostors = [line for line in messages if line.startswith("EXEC_OK")]
    assert impostors == [], impostors


def test_run_chat_retry_loop_cannot_be_used_to_forge_a_record(monkeypatch, dfs,
                                                              retry_calls):
    """`EXEC_ERROR attempt N:` — the MAINLINE path: it fires for every
    ordinary generated-code failure, three times per question."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan("PYTHON", CODE))
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec(FORGED_ERROR, []))
    messages = _log_capture(monkeypatch, run_chat_local)

    _run_chat(dfs)

    _assert_single_line(messages, "EXEC_ERROR attempt", expected_count=3)


def test_the_multi_plot_loop_cannot_be_used_to_forge_a_record(monkeypatch, dfs,
                                                              retry_calls):
    """`MULTI_PLOT_ERROR attempt=` — the chart worklist's own copy."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan_with_blocks(1))
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        _counting_exec(FORGED_ERROR, []))
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec(FORGED_ERROR, []))
    messages = _log_capture(monkeypatch, run_chat_local)

    _run_multi(dfs)

    _assert_single_line(messages, "MULTI_PLOT_ERROR", expected_count=3)


def test_the_mixed_table_block_cannot_be_used_to_forge_a_record(monkeypatch, dfs,
                                                                retry_calls):
    """`MIXED_TABLE_BLOCK_ERROR` — a table block of a dashboard answer that
    fails for an ordinary reason (no retry loop: one line per block)."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan",
                        _plan_with_charts_and_tables(charts=0, tables=1))
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec(FORGED_ERROR, []))
    messages = _log_capture(monkeypatch, run_chat_local)

    _run_multi(dfs)

    _assert_single_line(messages, "MIXED_TABLE_BLOCK_ERROR", expected_count=1)


def test_the_already_planned_path_cannot_be_used_to_forge_a_record(monkeypatch, dfs,
                                                                   retry_calls):
    """`_run_single_from_plan`'s copy of `EXEC_ERROR attempt N:` — the path
    Auto Analytics drives, reached here through its only caller."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", _plan("PYTHON", CODE))
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        _counting_exec(FORGED_ERROR, []))
    messages = _log_capture(monkeypatch, run_chat_local)

    events = _run_multi(dfs)

    singles = [event for event in events if event.get("single_response")]
    assert len(singles) == 1, events
    _assert_single_line(messages, "EXEC_ERROR attempt", expected_count=3)


# ---------------------------------------------------------------------------
# the two refresh sites, driven through the route helper with REAL execution
#
# These two go the whole way: the posted code raises an exception whose
# message carries the forged record, `code_exec` turns it into the error text
# exactly as it does in production, and the route logs it. Nothing about the
# error string is stubbed.
# ---------------------------------------------------------------------------
REFRESH_CHAT = "c_forge_probe"
RAISING_CODE = ('raise ValueError("benign\\n'
                '2026-09-17 00:00:00,000 | INFO | [sid=x] EXEC_OK job_id=forged")')


@pytest.fixture
def refresh_chat(tmp_path, monkeypatch):
    """A file-only chat with one dataframe (the idiom of
    tests/test_role_refresh_gate.py's file-only case), so the refresh helper
    reaches execution instead of the empty-dataset branch."""
    import local_store
    from settings import settings

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    store = local_store.ChatDataStore(REFRESH_CHAT)
    (store.files_dir / "old.csv").write_text("x\n1\n2\n", encoding="utf-8")
    meta = store.read_meta()
    meta["owner"] = "owner@x.com"
    meta["files"] = [{"file_name": "old.csv", "file_description": "",
                      "schema": {"file_name": "old.csv", "fields": {}}}]
    store.write_meta(meta)
    yield store
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.mark.parametrize("kind,event", [
    ("table", "REFRESH_ITEM_TABLE_EXEC_ERROR"),
    ("chart", "REFRESH_ITEM_CHART_EXEC_ERROR"),
])
def test_the_refresh_sites_cannot_be_used_to_forge_a_record(monkeypatch, refresh_chat,
                                                            kind, event):
    """Per-message refresh and dashboard tile refresh share this helper, and
    the code it runs is whatever the planner once produced for that item."""
    import asyncio

    import routes.chat as chat_mod

    messages = _log_capture(monkeypatch, chat_mod)

    out = asyncio.run(chat_mod.run_item_refresh(REFRESH_CHAT, RAISING_CODE, kind,
                                                sid="forge-probe"))

    assert out.get("ok") is False, out
    _assert_single_line(messages, event, expected_count=1)


# ---------------------------------------------------------------------------
# the structural tripwire
# ---------------------------------------------------------------------------
_MODULE_SITES = {
    "run_chat_local.py": [("EXEC_ERROR attempt", 2),
                          ("MULTI_PLOT_ERROR", 1),
                          ("MIXED_TABLE_BLOCK_ERROR", 1)],
    "routes/chat.py": [("REFRESH_ITEM_TABLE_EXEC_ERROR", 1),
                       ("REFRESH_ITEM_CHART_EXEC_ERROR", 1)],
}

# Names that COERCE a value without making it safe. `str(emsg)[:200]` is a
# call, so "the expression contains a call" cannot tell an escape from a
# coercion — which is exactly why the general form of this test is not
# reliable and the sites are enumerated instead.
_COERCIONS = {"str", "repr", "format", "int", "float", "type", "len"}

_ERRISH = ("error", "err", "emsg", "exc", "trace")

_SITE_CASES = [pytest.param(module, event, count, id=f"{module}:{event}")
               for module, sites in _MODULE_SITES.items()
               for event, count in sites]


def _callee_names(node) -> set:
    import ast

    names = set()
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _mentions_an_error(node) -> bool:
    import ast

    for inner in ast.walk(node):
        text = ""
        if isinstance(inner, ast.Name):
            text = inner.id
        elif isinstance(inner, ast.Attribute):
            text = inner.attr
        elif isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            text = inner.value
        if any(token in text.lower() for token in _ERRISH):
            return True
    return False


@pytest.mark.parametrize("module_name,event,expected", _SITE_CASES)
def test_every_known_log_site_passes_the_error_through_a_helper(module_name, event,
                                                                expected):
    """The tripwire for a SEVENTH site copied from one of these six.

    What it checks, per site: the log message is an f-string, the
    interpolation that carries the error goes through a call that is not a
    plain coercion, and the site still HAS such an interpolation. The last
    part is deliberate — if the error is pre-escaped into a local variable
    instead, this fails loudly and the list is updated on purpose, which is
    better than passing in silence with no idea what it still covers.

    Why the sites are named rather than discovered: `str(x)[:200]` is already
    a call, so no generic rule distinguishes a coercion from an escape
    without knowing the helper by name — and coupling a structural test to
    one helper's name is the thing that makes it stale. THIS LIST MUST GROW
    when a seventh site starts logging an execution error.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    tree = ast.parse((root / module_name).read_text(encoding="utf-8"))

    sites = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "log_with_sid"
                and len(node.args) >= 3
                and isinstance(node.args[2], ast.JoinedStr)):
            continue
        parts = node.args[2].values
        head = parts[0].value if (parts and isinstance(parts[0], ast.Constant)
                                  and isinstance(parts[0].value, str)) else ""
        if not head.startswith(event):
            continue
        sites.append(node)

    assert len(sites) == expected, (
        f"{module_name} has {len(sites)} {event} log sites, expected {expected} "
        "— update this list deliberately")

    for node in sites:
        errish = [part for part in node.args[2].values
                  if isinstance(part, ast.FormattedValue)
                  and _mentions_an_error(part.value)]
        assert errish, (
            f"{module_name}:{node.lineno} interpolates no error-bearing value "
            f"into {event} any more — re-read the site and update this test")
        for part in errish:
            expression = ast.unparse(part.value)
            helpers = _callee_names(part.value) - _COERCIONS
            assert helpers, (
                f"{module_name}:{node.lineno} writes the execution error raw "
                f"into {event}: {expression}")


# ---------------------------------------------------------------------------
# a library's exception ABOUT THE DATA is the same carrier
#
# The section above pins the error text generated code produces. This one
# pins the subtler half: the exceptions pandas and openpyxl raise about the
# DATA. Nobody writes those messages, which is exactly why they read as safe
# — but the library builds them by interpolating the label or the cell value
# it choked on, WITHOUT `repr`, so every character of that label reaches the
# log line. The label is chosen by whoever produced the frame: generated code
# on the chat paths, and an ordinary authenticated caller on the export path,
# whose rows come straight off the request body.
#
# So "a library raised it" is not a provenance argument. The carrier is the
# untrusted label inside the message, not the message's author.
# ---------------------------------------------------------------------------
FORGED_LABEL = f"city\n{FORGED_RECORD}"
SECRET_CELL = "SECRET-VALUE-42"
# A vertical tab: illegal in a worksheet, so openpyxl raises and QUOTES the
# whole value. `\t\n\r` are legal there, hence a control character that is not.
ILLEGAL_CELL = f"{SECRET_CELL}\x0b\n{FORGED_RECORD}"


def test_a_pandas_exception_about_a_label_cannot_forge_a_record(monkeypatch):
    """The reproduced case, and the shape of the whole class.

    A result frame whose INDEX NAME equals one of its COLUMN NAMES makes
    `reset_index()` raise `ValueError: cannot insert <name>, already exists`
    — and pandas interpolates `<name>` raw. Both names are the executed
    code's choice and both survive the parquet round trip, so a newline in a
    column label arrives here inside a pandas error message and, unescaped,
    writes a second record that never happened.
    """
    import pandas as pd

    frame = pd.DataFrame({FORGED_LABEL: [1, 2]})
    frame.index.name = FORGED_LABEL
    messages = _log_capture(monkeypatch, run_chat_local)

    out = run_chat_local._normalize_df_for_table(frame)

    assert out is not None, out
    _assert_single_line(messages, "TABLE_NORMALIZE_FAILED", expected_count=1)


@pytest.fixture
def chat_client(refresh_chat, monkeypatch):
    """The chat router behind a session, owning `refresh_chat`.

    Same shape as tests/test_role_refresh_gate.py's client: a bare app, the
    session middleware, and a login helper — so `_require_chat` passes
    without touching the real login flow.
    """
    from fastapi import FastAPI, Request
    from starlette.middleware.sessions import SessionMiddleware
    from starlette.testclient import TestClient

    import routes.chat as chat_mod

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    client = TestClient(app)
    login = client.post("/_login/owner@x.com")
    assert login.status_code == 200, login.text[:200]
    return client


def test_export_excel_logs_neither_the_cell_value_nor_a_newline(monkeypatch,
                                                                chat_client):
    """BOTH halves, because escaping alone would be the WRONG outcome here.

    `/export_excel` builds the sheet from rows posted in the request body, so
    the value openpyxl quotes back in `IllegalCharacterError` is whatever the
    caller sent. Escaping it would keep the log to one line and still write a
    caller-chosen customer value to disk — and no log line in this client
    carries a row value. So the exception TEXT is dropped entirely and only
    its type plus a fixed, value-free reason is logged.
    """
    import routes.chat as chat_mod

    messages = _log_capture(monkeypatch, chat_mod)

    response = chat_client.post(f"/api/chat/{REFRESH_CHAT}/export_excel",
                                json={"columns": ["a"], "rows": [{"a": ILLEGAL_CELL}]})

    status = response.status_code
    assert status == 502, (status, response.text[:300])
    _assert_no_value(messages, "EXPORT_EXCEL_FAILED")


def test_download_excel_logs_neither_the_cell_value_nor_a_newline(monkeypatch,
                                                                  chat_client):
    """The same site on the DOWNLOAD path, where the value came from the
    sandbox rather than from the caller. One test each, because the two
    routes are two `except` blocks that drifted apart once before."""
    import routes.chat as chat_mod

    key = chat_mod._cache_full_table({"columns": ["a"],
                                      "rows": [{"a": ILLEGAL_CELL}],
                                      "total_rows": 1})
    messages = _log_capture(monkeypatch, chat_mod)

    response = chat_client.post(f"/api/chat/{REFRESH_CHAT}/download_excel/{key}",
                                json={"filename": "t"})

    status = response.status_code
    assert status == 502, (status, response.text[:300])
    _assert_no_value(messages, "DOWNLOAD_EXCEL_FAILED")


def _assert_no_value(messages: list, event: str) -> None:
    """One line, AND no trace of the value the exception quoted."""
    hits = [line for line in messages if line.startswith(event)]
    assert len(hits) == 1, (event, messages)
    line = hits[0]
    assert "\n" not in line, (event, repr(line))
    assert "\r" not in line, (event, repr(line))
    # The value is DROPPED, not escaped — escaped would still be a row value
    # on disk.
    assert SECRET_CELL not in line, (event, line)
    assert FORGED_TOKEN not in line, (event, line)
    assert "\\n" not in line, (event, line)
    # What an operator can act on is still there.
    assert "IllegalCharacterError" in line, (event, line)
    impostors = [entry for entry in messages if entry.startswith("EXEC_OK")]
    assert impostors == [], impostors


# ---------------------------------------------------------------------------
# the structural guard — the INVERSE form
# ---------------------------------------------------------------------------
# Shape chosen, and why. The enumerate-by-name form above covers the sites
# that were known when it was written; it is silent about the next one, which
# is precisely the event it would exist to catch (this class grew from six
# sites to seventeen twice in a row, each time found by a reader rather than
# by a test). So the guard is INVERTED: every value that reaches a log LINE
# in these three modules must be either a call to one of the escaping
# helpers, or safe BY CONSTRUCTION under a rule below, or named in the
# allowlist with its reason. A new site fails by default and its author has
# to route it through a helper or add a line here — the reviewable act the
# allowlist exists to force.
#
# The general "must contain a call" rule was rejected for a reason that still
# holds: `str(x)[:200]` is a call too, so a coercion is indistinguishable
# from an escape unless the helpers are NAMED. They are named here.
#
# WHAT IT LOOKS AT. `logger_utils.log_with_sid(sid, level, message, **context)`
# renders the sid and EVERY context value onto the same line as the message,
# so all three are inspected — not the message alone. Call sites are matched
# by the NAME `log_with_sid` whether it is called bare or through a module
# attribute, and the message is taken from the third positional argument or
# from a `message=` keyword.
#
# WHAT IT CANNOT SEE — stated plainly, because a guarantee that overstates is
# the exact defect this whole class has been:
#   * A message (or sid, or context value) whose expression is not one of the
#     analysable shapes is REFUSED rather than analysed — a bare name, a
#     `%`/`.format()` assembly, a `**` splat, an unknown call. That converts
#     "cannot analyse" into a failure instead of a silent pass, but it means
#     the rule is "build the line inline", not "we understand every shape".
#   * The PROVENANCE of an id passed in from another module. `sid` arrives as
#     a parameter; every call site in this repository passes a server-
#     generated token, and NOTHING HERE VERIFIES THAT — it is an allowlist
#     entry, not a proof.
#   * Any module outside `LOG_SAFE_MODULES`. The same class may exist in
#     `db_connector.py`, `routes/admin_data.py` or `executor_client.py`; this
#     guard says nothing about them.
LOG_SAFE_MODULES = ["run_chat_local.py", "routes/chat.py", "exec_sanitizer.py"]

LOG_CALL_NAME = "log_with_sid"

# The escaping helpers. `_log_safe` is `exec_sanitizer`'s module-local one
# (that module is a leaf and imports nothing from the transport).
ESCAPING_HELPERS = {"log_safe_text", "_log_safe"}

# Helpers that return a FIXED, value-free string rather than escaping one.
VALUE_FREE_HELPERS = {"_xlsx_failure_reason"}

# Coercions whose RESULT is not a string, so it cannot carry a newline.
# `str` is deliberately absent: that is the whole point.
NON_STRING_COERCIONS = {"len", "bool", "int", "float"}

# Expressions safe BY CONSTRUCTION, as exact source text, each with the
# reason it is safe. Adding an entry is a visible, reviewable act — that is
# why the list lives in the test. Where the reason depends on something
# OUTSIDE these three modules, it says so.
SAFE_BY_CONSTRUCTION = {
    # counts, positions, flags: never strings
    "produced": "an int counter",
    "last_human_idx": "an int index",
    "_MAX_MULTI_PLOT_CANDIDATES": "a module constant",
    "match": "a bool",
    "catalog": "a bool flag from the profile facts",
    "plot_out.get('is_plotly')": "a bool from the renderer",
    "table.get('total_rows')": "an int row count",
    # fixed vocabularies owned by this codebase, not by any input
    "det['kind']": "a result_backstop finding kind (fixed set)",
    "reason": "this module's own literal reason strings",
    "m['reason']": "local_store's missing-table reason (fixed set)",
    # a LIST rendering: `str([...])` reprs its elements, and repr escapes
    # CR/LF, so element content cannot break the line
    "blocked": "a list of display names — list rendering reprs its elements",
    # a sha256 hexdigest slice: the alphabet is 0-9a-f
    "ch": "a hex code hash — sha256 hexdigest, no CR/LF in the alphabet",
    # the upload sanitizer strips control characters, so no CR/LF survives
    # (verified: it returns 'ab.csv' for 'a\nb.csv')
    "fname": "sanitize_upload_filename output — control chars stripped",
    # ids whose safety comes from OUTSIDE these modules — the mechanism is
    # named exactly, because a wrong reason is worse than none
    "chat_id": ("no format guard exists; a newline-bearing id matches no "
                "chatdata directory, so the request 404s before any log line"),
    "getattr(store, 'chat_id', '')": "same as chat_id, read back off the store",
    "email": ("the session email; the password path validates it, and the SSO "
              "path only requires non-empty with an '@', so an interior "
              "newline would need control of a directory attribute in the "
              "customer's own tenant — not reachable, not validated"),
    "str(user_email or '?')": "same as email, with a placeholder for None",
    "sid": ("the caller's request id; every call site in this repository "
            "passes a server-generated token (`secrets.token_hex`) or a "
            "stored id — NOT verified by this test"),
}


def _log_calls(tree):
    """Every `log_with_sid` call, bare or through a module attribute."""
    import ast

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name == LOG_CALL_NAME:
            yield node


def _log_fields(node):
    """`(label, expression_or_None)` for every value that reaches the line.

    `None` means the argument could not be located at all, which the caller
    treats as an offender rather than as an absence.
    """
    positional = list(node.args)
    by_keyword = {kw.arg: kw.value for kw in node.keywords if kw.arg}
    fields = []
    sid = (positional[0] if positional
           else by_keyword.get("sid_or_id") or by_keyword.get("sid"))
    fields.append(("sid", sid))
    message = (positional[2] if len(positional) >= 3
               else by_keyword.get("message"))
    fields.append(("message", message))
    for keyword in node.keywords:
        if keyword.arg in (None, "sid", "sid_or_id", "level", "message"):
            continue
        fields.append((f"context:{keyword.arg}", keyword.value))
    return fields


def _has_splat(node) -> bool:
    """A `*args` / `**context` splat hides values from every check below."""
    import ast

    if any(isinstance(arg, ast.Starred) for arg in node.args):
        return True
    return any(keyword.arg is None for keyword in node.keywords)


def _is_safe_value(expression, conversion: int = -1) -> bool:
    """One interpolated or passed value: safe, or not analysable."""
    import ast

    if expression is None:
        return False
    # `!r` — repr escapes CR/LF, so the value cannot break the line.
    if conversion == ord("r"):
        return True
    if isinstance(expression, ast.Constant):
        return True
    # `x + 1` on a counter is an int.
    if isinstance(expression, ast.BinOp) and isinstance(expression.right, ast.Constant) \
            and isinstance(expression.right.value, int):
        return True
    if isinstance(expression, ast.Call):
        name = getattr(expression.func, "id", None) or \
            getattr(expression.func, "attr", None) or ""
        if name in NON_STRING_COERCIONS:
            return True
    # a call to one of the NAMED helpers, anywhere in the expression
    for inner in ast.walk(expression):
        if not isinstance(inner, ast.Call):
            continue
        name = getattr(inner.func, "id", None) or \
            getattr(inner.func, "attr", None) or ""
        if name in ESCAPING_HELPERS or name in VALUE_FREE_HELPERS:
            return True
    return ast.unparse(expression) in SAFE_BY_CONSTRUCTION


def _message_offenders(expression) -> list:
    """Offenders inside a MESSAGE expression, refusing shapes it cannot read.

    Analysable: a string literal, an f-string, a `+` of analysable parts, and
    `sep.join(<comprehension or sequence of analysable parts>)` — the one
    non-f-string assembly this codebase uses. Anything else (a bare name, a
    `%` assembly, `.format()`, an unknown call) is an offender in itself: the
    guard refuses to guess, which is what keeps "a new site fails by default"
    true rather than aspirational.
    """
    import ast

    if expression is None:
        return ["the message argument could not be located"]
    if isinstance(expression, ast.Constant):
        return []
    if isinstance(expression, ast.JoinedStr):
        offenders = []
        for part in expression.values:
            if isinstance(part, ast.Constant):
                continue
            if not isinstance(part, ast.FormattedValue):
                offenders.append(ast.unparse(part))
            elif not _is_safe_value(part.value, part.conversion):
                offenders.append(ast.unparse(part.value))
        return offenders
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        return (_message_offenders(expression.left)
                + _message_offenders(expression.right))
    if isinstance(expression, ast.Call) and \
            getattr(expression.func, "attr", "") == "join" and len(expression.args) == 1:
        argument = expression.args[0]
        if isinstance(argument, (ast.ListComp, ast.GeneratorExp)):
            return _message_offenders(argument.elt)
        if isinstance(argument, (ast.List, ast.Tuple)):
            offenders = []
            for element in argument.elts:
                offenders.extend(_message_offenders(element))
            return offenders
    return [f"message shape not analysable: {ast.unparse(expression)[:80]}"]


def _module_tree(module_name):
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return ast.parse((root / module_name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("module_name", LOG_SAFE_MODULES)
def test_no_log_line_carries_an_unescaped_value(module_name):
    """Every value that reaches a log line in these modules is accounted for.

    Message, SID and every context value — `logger_utils` renders all three
    onto one line. Each must go through an escaping helper, or be safe by
    construction under a rule above, or appear in `SAFE_BY_CONSTRUCTION` with
    a stated reason. A NEW site fails here by default, which is the property
    the enumerate-by-name form could not offer.
    """
    import ast

    offenders = []
    for node in _log_calls(_module_tree(module_name)):
        where = f"{module_name}:{node.lineno}"
        if _has_splat(node):
            offenders.append(f"{where} a splat hides the arguments")
            continue
        for label, expression in _log_fields(node):
            if label == "message":
                offenders.extend(f"{where} message {problem}"
                                 for problem in _message_offenders(expression))
                continue
            if expression is None:
                offenders.append(f"{where} {label} could not be located")
            elif not _is_safe_value(expression):
                offenders.append(f"{where} {label} {ast.unparse(expression)}")
    assert offenders == [], offenders


@pytest.mark.parametrize("module_name", LOG_SAFE_MODULES)
def test_the_guard_can_see_every_call_site_in_the_module(module_name):
    """A count, so a call form the matcher does not recognise cannot reduce
    the guard's reach in silence.

    The matcher keys on the NAME `log_with_sid`; this compares what it found
    against a plain textual count of the occurrences that are calls. A
    mismatch means a shape exists that the walker skips — the failure mode
    the verification round found, now a test rather than a reading.
    """
    import ast

    tree = _module_tree(module_name)
    found = len(list(_log_calls(tree)))
    textual = sum(1 for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and LOG_CALL_NAME in ast.unparse(node.func))
    assert found == textual, (found, textual)


def test_the_allowlist_has_no_stale_entries():
    """An entry that matches nothing is a reader's false comfort: it suggests
    a site is reviewed when it is gone. Keeps the list honest as the modules
    change."""
    import ast

    seen = set()
    for module_name in LOG_SAFE_MODULES:
        for node in _log_calls(_module_tree(module_name)):
            for _, expression in _log_fields(node):
                if expression is None:
                    continue
                for inner in ast.walk(expression):
                    if isinstance(inner, ast.FormattedValue):
                        seen.add(ast.unparse(inner.value))
                seen.add(ast.unparse(expression))
    stale = sorted(set(SAFE_BY_CONSTRUCTION) - seen)
    assert stale == [], stale


# ---------------------------------------------------------------------------
# the guard's own reach, pinned
#
# The guard above is itself code, and its first version CLAIMED more than it
# reached: five ordinary call forms slipped past because it required the name
# to be a bare `Name`, the message to be the third POSITIONAL argument, and
# an interpolation to be a `FormattedValue`. Nothing in the tree exercised
# those forms, so nothing failed — the gap was found by a reader.
#
# So the reach is now a test. Each case below is a synthetic call driven
# through the guard's own helpers, and a future narrowing of the walker fails
# HERE instead of going quiet. `passes` cases matter as much as `caught`
# ones: a guard that rejects the safe forms would be abandoned within a week.
# ---------------------------------------------------------------------------
_GUARD_CAUGHT = [
    # the five forms the first version missed
    ("concatenation", 'log_with_sid(sid, "warning", "BOOM_FAILED: " + str(e))'),
    ("message_via_variable",
     'msg = f"BOOM_FAILED: {e}"\nlog_with_sid(sid, "warning", msg)'),
    ("attribute_call",
     'logger_utils.log_with_sid(sid, "warning", f"BOOM_FAILED: {e}")'),
    ("join_of_a_list",
     'log_with_sid(sid, "warning", "".join(["BOOM_FAILED: ", str(e)]))'),
    ("message_keyword",
     'log_with_sid(sid, "warning", message=f"BOOM_FAILED: {e}")'),
    # the form the first version did catch, kept so a rewrite cannot lose it
    ("message_positional", 'log_with_sid(sid, "warning", f"BOOM_FAILED: {e}")'),
    # other assemblies
    ("percent_format", 'log_with_sid(sid, "warning", "BOOM %s" % e)'),
    ("dot_format", 'log_with_sid(sid, "warning", "BOOM {}".format(e))'),
    ("join_of_a_genexpr",
     'log_with_sid(sid, "warning", "X " + ", ".join(f"{v}" for v in vs))'),
    # the two surfaces beyond the message — logger_utils renders both onto
    # the same line
    ("context_splat", 'log_with_sid(sid, "warning", "BOOM", **ctx)'),
    ("context_value", 'log_with_sid(sid, "warning", "BOOM", detail=str(e))'),
    ("sid_value", 'log_with_sid(str(e), "warning", "BOOM")'),
]

_GUARD_PASSES = [
    ("escaping_helper",
     'log_with_sid(sid, "warning", f"OK: {log_safe_text(str(e), 200)}")'),
    ("repr_conversion", 'log_with_sid(sid, "warning", f"OK: {e!r}")'),
    ("constant_message", 'log_with_sid(sid, "warning", "OK")'),
    ("counter", 'log_with_sid(sid, "info", f"OK n={len(work)} i={retry_count + 1}")'),
    ("non_string_coercion", 'log_with_sid(sid, "info", f"OK f={bool(flag)}")'),
]


def _guard_offenders(source: str) -> list:
    """Run the guard's own helpers over one synthetic snippet."""
    import ast

    offenders = []
    for node in _log_calls(ast.parse(source)):
        if _has_splat(node):
            offenders.append("splat")
            continue
        for label, expression in _log_fields(node):
            if label == "message":
                offenders.extend(_message_offenders(expression))
            elif expression is None or not _is_safe_value(expression):
                offenders.append(label)
    return offenders


@pytest.mark.parametrize("source", [case[1] for case in _GUARD_CAUGHT],
                         ids=[case[0] for case in _GUARD_CAUGHT])
def test_the_guard_catches_this_form(source):
    offenders = _guard_offenders(source)
    assert offenders, source


@pytest.mark.parametrize("source", [case[1] for case in _GUARD_PASSES],
                         ids=[case[0] for case in _GUARD_PASSES])
def test_the_guard_accepts_this_safe_form(source):
    offenders = _guard_offenders(source)
    assert offenders == [], (source, offenders)
