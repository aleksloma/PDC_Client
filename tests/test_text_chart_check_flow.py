"""PROMPT 31 Part C — the TEXT_CHART_CHECK wiring in `run_chat_local` (LOG ONLY).

Where a plot block's VALUES and its chart are both in hand and the turn has a
narrative template and the block produced Plotly `chart_stats`, the flow
calls `text_chart_check.check_block` and logs ONE line:

    TEXT_CHART_CHECK block=<n> chart=<types> slots=<n> match= derived=
      other_stat= not_found= exempt= first=<class|none>
      other_stat_slots=<names> not_found_slots=<names>

Nothing is shown to the user, stored, retried or sent to the brain; a check
failure logs `TEXT_CHART_CHECK_FAILED error=<Type>` and the turn continues.

These tests drive `run_chat_local.run_chat_multi_plot` END TO END with the
REAL chart render (conftest binds `render_plot_safe` to the in-process
`plot_utils._render_in_process`, the sandbox runner's entry point — Plotly
HTML only, no kaleido) and the brain stubbed at the HTTP seam
(`brain_client._post`), so every payload that would leave the container is
recorded and checked.

- Q2 reproduction (22 Nov): `px.box(guarded, x='segment', y='amount')` with a
  narrative of the guarded MEANS, the all-row means and a p-value → one line,
  other_stat >= 2, first=other_stat.
- Q7 reproduction: a bar of order counts per channel and its share →
  first=match, not_found=0.
- no number of the test data on any log line (log_with_sid captured as well
  as every stdlib record); `chart_stats` in no event (the stored record is
  built from these) and in no brain payload.

Offline: `.venv/Scripts/python.exe -m pytest tests/test_text_chart_check_flow.py -q`.
"""
import json
import logging
import re

import pandas as pd
import pytest

import brain_client
import run_chat_local
from settings import settings

CAPS = "narrative_v1,diagnostics_v1"


@pytest.fixture(autouse=True)
def _stub_schema(monkeypatch):
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")


@pytest.fixture(autouse=True)
def _caps_on(monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", CAPS)


@pytest.fixture(autouse=True)
def _close_figures():
    import matplotlib.pyplot as plt
    plt.close("all")
    yield
    plt.close("all")


class LogCapture:
    """Every log line of the turn: the `log_with_sid` calls of run_chat_local
    (recorded, then passed on) and every stdlib record (caplog — the sandbox
    side logs through `logging`, log_with_sid through the `datachat` logger)."""

    def __init__(self, monkeypatch, caplog):
        self.calls: list = []
        self.caplog = caplog
        caplog.set_level(logging.DEBUG)
        real = run_chat_local.log_with_sid

        def rec(sid, level, message, **context):
            self.calls.append(f"{message} {context}")
            return real(sid, level, message, **context)

        monkeypatch.setattr(run_chat_local, "log_with_sid", rec)

    def lines(self) -> list:
        return self.calls + [r.getMessage() for r in self.caplog.records]

    def checks(self) -> list:
        # the recorded calls only (each line once — caplog would repeat them)
        return [ln for ln in self.calls if ln.startswith("TEXT_CHART_CHECK ")]

    def failures(self) -> list:
        return [ln for ln in self.lines() if "TEXT_CHART_CHECK_FAILED" in ln]


class Brain:
    """`brain_client._post` stand-in: answers by path, records every payload."""

    def __init__(self, plan: dict, retry: dict | None = None):
        self.plan = plan
        self.retry = retry or {"kind": "NO_CODE", "code": "", "usage": {}}
        self.posts: list = []

    def install(self, monkeypatch):
        def post(path, payload, sid, timeout=None):
            self.posts.append((path, payload))
            if path == "/v1/plan":
                return dict(self.plan)
            if path == "/v1/retry":
                return dict(self.retry)
            if path in ("/v1/describe", "/v1/summarize"):
                return {"text": "described", "usage": {}}
            return {}

        monkeypatch.setattr(brain_client, "_post", post)
        return self

    def paths(self) -> list:
        return [p for p, _ in self.posts]

    def dumped(self) -> str:
        return json.dumps([p for _, p in self.posts], default=str)


def _plan(code: str, narrative_t: str | None, captions: dict | None = None) -> dict:
    # a trailing delimiter keeps even one block on the multi path
    raw = f"```plot_code\n{code}\n```\n###NEXT_PLOT###\n"
    out = {"raw_text": raw, "kind": "PLOT_CODE", "code": "", "usage": {}}
    if narrative_t is not None:
        out["narrative"] = narrative_t
    if captions is not None:
        out["captions"] = captions
    return out


def _run(dfs, question="average order amount premium vs mass"):
    return list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question=question,
        history_rows=[], user_email="alice@acme.com"))


def _fields(line: str) -> dict:
    return dict(re.findall(r"(\w+)=(\S*)", line))


def _contains_key(obj, key: str) -> bool:
    if isinstance(obj, dict):
        return key in obj or any(_contains_key(v, key) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_contains_key(v, key) for v in obj)
    return False


def _charts(events) -> list:
    return [e for e in events if e.get("partial") and e.get("image_base64")]


# ===================================================================== Q2

# The guarded frame (amount < 100,000) and three excluded rows.
PREMIUM = [7311, 7311, 7311, 8423, 61977]          # guarded mean 18,466.6
MASS = [523, 523, 641, 641, 9187]                  # guarded mean 2,303
EXCLUDED = [("premium", 250000), ("premium", 250000), ("mass", 180000)]


def _q2_frame() -> pd.DataFrame:
    rows = ([("premium", v) for v in PREMIUM] + [("mass", v) for v in MASS] + EXCLUDED)
    return pd.DataFrame(rows, columns=["segment", "amount"]).astype({"amount": "float64"})


Q2_CODE = """import plotly.express as px
d = dfs['orders.csv']
keep = d['amount'] < 100000
guarded = d[keep]
fig = px.box(guarded, x='segment', y='amount')
VALUES['mean_premium'] = float(guarded.loc[guarded['segment'] == 'premium', 'amount'].mean())
VALUES['mean_mass'] = float(guarded.loc[guarded['segment'] == 'mass', 'amount'].mean())
VALUES['mean_all_rows_premium'] = float(d.loc[d['segment'] == 'premium', 'amount'].mean())
VALUES['mean_all_rows_mass'] = float(d.loc[d['segment'] == 'mass', 'amount'].mean())
VALUES['n_excluded'] = int((~keep).sum())
VALUES['p_value'] = 0.0004"""

Q2_NARR = ("Premium customers spend {{mean_premium:amount}} per order on average versus "
           "{{mean_mass:amount}} for mass customers. Including the {{n_excluded:count}} "
           "excluded orders the averages are {{mean_all_rows_premium:amount}} and "
           "{{mean_all_rows_mass:amount}} (p {{p_value:pvalue}}).")

# Every number of the Q2 data as it would be DISPLAYED (or printed raw): none
# may reach a log line or a brain payload.
Q2_SHOWN = ("18,467", "18466.6", "2,303", "84,619", "84619", "31,919", "31919.1",
            "7,311", "8,423", "61,977", "9,187", "250,000", "180,000")


def test_q2_reproduction_logs_other_stat_once(monkeypatch, caplog):
    logs = LogCapture(monkeypatch, caplog)
    brain = Brain(_plan(Q2_CODE, Q2_NARR)).install(monkeypatch)

    events = _run({"orders.csv": _q2_frame()})

    assert len(_charts(events)) == 1, [sorted(e) for e in events]
    checks = logs.checks()
    assert len(checks) == 1, checks
    f = _fields(checks[0])
    assert f["block"] == "1", checks
    assert "box" in f["chart"].split(","), checks
    assert int(f["other_stat"]) >= 2, checks
    assert f["first"] == "other_stat", checks
    assert {"mean_premium", "mean_mass"} <= set(re.split(r"[,;|]", f["other_stat_slots"])), checks
    for k in ("slots", "match", "derived", "not_found", "exempt"):
        assert f[k].isdigit(), (k, checks)
    # mean_all_rows_* + n_excluded + p_value
    assert int(f["exempt"]) >= 4, checks
    assert logs.failures() == []
    # LOG ONLY: no extra brain call, the answer is the same turn
    assert brain.paths().count("/v1/plan") == 1
    assert "/v1/retry" not in brain.paths(), brain.paths()


def test_q2_no_value_on_any_log_line(monkeypatch, caplog):
    logs = LogCapture(monkeypatch, caplog)
    Brain(_plan(Q2_CODE, Q2_NARR)).install(monkeypatch)
    _run({"orders.csv": _q2_frame()})
    assert logs.checks(), "no TEXT_CHART_CHECK line"
    for line in logs.lines():
        for shown in Q2_SHOWN:
            assert shown not in line, (shown, line)


def test_q2_chart_stats_never_stored_nor_sent(monkeypatch, caplog):
    LogCapture(monkeypatch, caplog)
    brain = Brain(_plan(Q2_CODE, Q2_NARR)).install(monkeypatch)
    events = _run({"orders.csv": _q2_frame()})
    # the stored record is built from these events (routes/chat.py)
    for e in events:
        assert not _contains_key(e, "chart_stats"), sorted(e)
        assert not _contains_key(e, "text_chart_check"), sorted(e)
    dumped = brain.dumped()
    assert "chart_stats" not in dumped
    assert "TEXT_CHART_CHECK" not in dumped
    for shown in Q2_SHOWN:
        assert shown not in dumped, shown


def test_boxmean_box_makes_the_mean_slots_match(monkeypatch, caplog):
    """The planner's new rule: `fig.update_traces(boxmean=True)` — the means
    are then on the chart and the same narrative checks as match."""
    logs = LogCapture(monkeypatch, caplog)
    code = Q2_CODE.replace("fig = px.box(guarded, x='segment', y='amount')",
                           "fig = px.box(guarded, x='segment', y='amount')\n"
                           "fig.update_traces(boxmean=True)")
    Brain(_plan(code, Q2_NARR)).install(monkeypatch)
    _run({"orders.csv": _q2_frame()})
    checks = logs.checks()
    assert len(checks) == 1, checks
    f = _fields(checks[0])
    assert f["other_stat"] == "0", checks
    assert f["first"] == "match", checks
    assert int(f["match"]) >= 2, checks


# ===================================================================== Q7

Q7_CODE = """import plotly.express as px
c = dfs['channels.csv']
fig = px.bar(c, x='channel', y='orders')
VALUES['online_orders'] = int(c.loc[c['channel'] == 'online', 'orders'].iloc[0])
VALUES['online_share'] = float(c.loc[c['channel'] == 'online', 'orders'].iloc[0] / c['orders'].sum())"""

Q7_NARR = ("Online brought {{online_orders:count}} orders, {{online_share:pct}} of all "
           "orders.")


def _q7_frame() -> pd.DataFrame:
    return pd.DataFrame({"channel": ["online", "branch", "phone"],
                         "orders": [139415, 45000, 15585],
                         "returns": [1234, 567, 89]})


def test_q7_reproduction_first_match_nothing_unexplained(monkeypatch, caplog):
    logs = LogCapture(monkeypatch, caplog)
    brain = Brain(_plan(Q7_CODE, Q7_NARR)).install(monkeypatch)

    events = _run({"channels.csv": _q7_frame()}, question="orders per channel")

    assert len(_charts(events)) == 1
    checks = logs.checks()
    assert len(checks) == 1, checks
    f = _fields(checks[0])
    assert f["first"] == "match", checks
    assert f["not_found"] == "0", checks
    assert f["other_stat"] == "0", checks
    assert f["match"] == "1" and f["derived"] == "1", checks
    assert f["chart"] == "bar", checks
    assert f["slots"] == "2", checks
    for line in logs.lines():
        for shown in ("139,415", "139415", "69.7", "45,000", "15,585"):
            assert shown not in line, (shown, line)
    for e in events:
        assert not _contains_key(e, "chart_stats"), sorted(e)
    assert "chart_stats" not in brain.dumped()


def test_caption_slots_are_checked_too(monkeypatch, caplog):
    """The slots of the block's CAPTION count as well as the narrative's."""
    logs = LogCapture(monkeypatch, caplog)
    Brain(_plan(Q7_CODE, "Orders by channel.",
                captions={"1": "Online: {{online_orders:count}} orders."})).install(monkeypatch)
    _run({"channels.csv": _q7_frame()}, question="orders per channel")
    checks = logs.checks()
    assert len(checks) == 1, checks
    f = _fields(checks[0])
    assert f["slots"] == "1" and f["match"] == "1", checks


# ============================================================ when not to check

def test_no_narrative_template_no_check(monkeypatch, caplog):
    logs = LogCapture(monkeypatch, caplog)
    Brain(_plan(Q7_CODE, None)).install(monkeypatch)
    events = _run({"channels.csv": _q7_frame()}, question="orders per channel")
    assert len(_charts(events)) == 1
    assert logs.checks() == []
    assert logs.failures() == []


def test_matplotlib_chart_no_check(monkeypatch, caplog):
    logs = LogCapture(monkeypatch, caplog)
    code = ("c = dfs['channels.csv']\n"
            "fig, ax = plt.subplots()\n"
            "ax.bar(c['channel'], c['orders'])\n"
            "VALUES['online_orders'] = int(c['orders'].iloc[0])")
    Brain(_plan(code, "Online brought {{online_orders:count}} orders.")).install(monkeypatch)
    events = _run({"channels.csv": _q7_frame()}, question="orders per channel")
    assert len(_charts(events)) == 1
    assert logs.checks() == []
    assert logs.failures() == []


def test_two_chart_blocks_two_lines(monkeypatch, caplog):
    logs = LogCapture(monkeypatch, caplog)
    raw = (f"```plot_code\n{Q7_CODE}\n```\n###NEXT_PLOT###\n"
           "```plot_code\nimport plotly.express as px\n"
           "c = dfs['channels.csv']\n"
           "fig = px.pie(c, names='channel', values='returns')\n"
           "VALUES['branch_returns'] = int(c['returns'].iloc[1])\n```\n###NEXT_PLOT###\n")
    plan = {"raw_text": raw, "kind": "PLOT_CODE", "code": "", "usage": {},
            "narrative": Q7_NARR + " Branch had {{branch_returns:count}} returns."}
    Brain(plan).install(monkeypatch)
    events = _run({"channels.csv": _q7_frame()}, question="orders per channel")
    assert len(_charts(events)) == 2
    checks = logs.checks()
    assert len(checks) == 2, checks
    assert sorted(_fields(c)["block"] for c in checks) == ["1", "2"], checks
    pie = [c for c in checks if _fields(c)["block"] == "2"][0]
    assert _fields(pie)["chart"] == "pie", pie


# ================================================================ split grid

GRID_CODE = """import plotly.graph_objects as go
from plotly.subplots import make_subplots
c = dfs['channels.csv']
fig = make_subplots(rows=1, cols=2)
fig.add_trace(go.Bar(x=c['channel'], y=c['orders']), row=1, col=1)
fig.add_trace(go.Bar(x=c['channel'], y=c['orders'] * 2), row=1, col=2)
VALUES['online_orders'] = int(c['orders'].iloc[0])"""


def test_split_grid_block_logs_one_line(monkeypatch, caplog):
    """A plotly grid the brain does not regenerate is split into sub-charts;
    the block is still checked ONCE."""
    logs = LogCapture(monkeypatch, caplog)
    Brain(_plan(GRID_CODE, "Online brought {{online_orders:count}} orders.")).install(monkeypatch)
    events = _run({"channels.csv": _q7_frame()}, question="orders per channel")
    assert len(_charts(events)) == 2, [sorted(e) for e in events]
    checks = logs.checks()
    assert len(checks) == 1, checks
    f = _fields(checks[0])
    assert f["block"] == "1" and f["first"] == "match", checks
    for e in events:
        assert not _contains_key(e, "chart_stats"), sorted(e)


# ================================================================== failure

def test_check_failure_is_logged_and_the_turn_continues(monkeypatch, caplog):
    import text_chart_check

    def boom(**kw):
        raise RuntimeError("check exploded 139415")

    monkeypatch.setattr(text_chart_check, "check_block", boom)
    logs = LogCapture(monkeypatch, caplog)
    Brain(_plan(Q7_CODE, Q7_NARR)).install(monkeypatch)
    events = _run({"channels.csv": _q7_frame()}, question="orders per channel")
    assert len(_charts(events)) == 1
    assert [e for e in events if e.get("done")], "the turn did not finish"
    fails = logs.failures()
    assert fails and all("error=RuntimeError" in ln for ln in fails), fails
    assert not any("exploded" in ln or "139415" in ln for ln in logs.lines())
    assert logs.checks() == []


def test_check_error_result_is_logged_as_failure(monkeypatch, caplog):
    import text_chart_check
    monkeypatch.setattr(text_chart_check, "check_block", lambda **kw: {"error": "ValueError"})
    logs = LogCapture(monkeypatch, caplog)
    Brain(_plan(Q7_CODE, Q7_NARR)).install(monkeypatch)
    events = _run({"channels.csv": _q7_frame()}, question="orders per channel")
    assert len(_charts(events)) == 1
    fails = logs.failures()
    assert fails and "error=ValueError" in fails[0], fails
    assert logs.checks() == []
