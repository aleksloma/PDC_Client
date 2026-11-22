"""PROMPT 31 Part C — evaluator accuracy fixes to `chart_stats` (LOG ONLY).

- a bar trace that repeats a category, and a stacked figure (barmode
  "stack" / "relative" — px.bar with `color` stacks by default), carry the
  per-category sums: the bar heights the user sees;
- a pie / funnelarea with a repeated label carries the per-label sums;
- above the value cap a trace keeps its first and last numbers plus its min
  and max, so a long line's latest point stays a candidate;
- one bad trace is skipped, the others kept;
- a `visible="legendonly"` trace draws nothing and is skipped;
- `multi_charts[*].chart_stats` round-trips through the transport;
- `narrative.rendered_slots` (hidden section, repeated name, first sentence);
- box quartiles pinned to plotly.js' own methods: "linear" (the default,
  plotly.js `Lib.interp` = numpy "hazen") and "exclusive" / "inclusive".

Offline: `.venv/Scripts/python.exe -m pytest tests/test_chart_stats_accuracy.py -q`.
"""
import logging

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pytest

import exec_transport
import narrative
import plot_utils
import text_chart_check

PX = "import plotly.express as px\nimport plotly.graph_objects as go\n"


@pytest.fixture(autouse=True)
def _close_figures():
    import matplotlib.pyplot as plt
    plt.close("all")
    yield
    plt.close("all")


def _render(code, frames, **kw):
    out = plot_utils._render_in_process(code, frames, "t", **kw)
    assert out.get("ok") is True, out.get("error")
    return out


def _stats(out):
    st = out.get("chart_stats")
    assert isinstance(st, dict) and st.get("traces"), out.get("chart_stats")
    return st


def _has(values, number):
    return any(v == pytest.approx(number) for v in values)


def _check(slots, values, stats):
    return text_chart_check.check_block(template_slots=slots, block_values=values,
                                        chart_stats=stats, lang="en")


# ================================================================ bar sums

def test_unaggregated_bar_draws_the_category_sums():
    # px.bar over raw rows: one bar per channel whose height is the row sum
    df = pd.DataFrame({"channel": ["web", "web", "web", "app", "app"],
                       "amount": [100.25, 200.5, 300.75, 40.5, 60.25]})
    tr = _stats(_render(PX + "fig = px.bar(dfs['d'], x='channel', y='amount')",
                        {"d": df}))["traces"][0]
    assert tr["type"] == "bar"
    assert _has(tr["values"], 601.5) and _has(tr["values"], 100.75), tr["values"]
    for row in (100.25, 200.5, 300.75, 40.5, 60.25):       # rows kept too
        assert _has(tr["values"], row)
    assert tr["total"] == pytest.approx(702.25)
    res = _check([("web_total", "amount", True)], {"web_total": 601.5}, _stats(
        {"chart_stats": exec_transport.decode_chart_stats({"traces": [tr]})}))
    assert res["match"] == 1 and res["first"] == "match", res


def test_stacked_bar_draws_the_sum_across_the_stack():
    df = pd.DataFrame({"month": ["Jan", "Feb", "Jan", "Feb"], "seg": ["a", "a", "b", "b"],
                       "v": [10.5, 20.25, 30.5, 40.75]})
    st = _stats(_render(PX + "fig = px.bar(dfs['d'], x='month', y='v', color='seg')",
                        {"d": df}))
    assert len(st["traces"]) == 2
    drawn = [v for t in st["traces"] for v in t["values"]]
    assert _has(drawn, 41.0) and _has(drawn, 61.0), drawn           # Jan, Feb heights
    res = _check([("jan_total", "float", True)], {"jan_total": 41.0}, st)
    assert res["match"] == 1, res


def test_grouped_bar_adds_no_stack_sums():
    df = pd.DataFrame({"month": ["Jan", "Feb", "Jan", "Feb"], "seg": ["a", "a", "b", "b"],
                       "v": [10.5, 20.25, 30.5, 40.75]})
    st = _stats(_render(PX + "fig = px.bar(dfs['d'], x='month', y='v', color='seg', "
                             "barmode='group')", {"d": df}))
    assert sorted(sorted(t["values"]) for t in st["traces"]) == [[10.5, 20.25], [30.5, 40.75]]


def test_horizontal_stacked_bar_sums_along_x():
    df = pd.DataFrame({"seg": ["a", "a", "b", "b"], "region": ["N", "S", "N", "S"],
                       "v": [1.5, 2.25, 3.5, 4.75]})
    st = _stats(_render(PX + "fig = px.bar(dfs['d'], x='v', y='region', color='seg', "
                             "orientation='h')", {"d": df}))
    drawn = [v for t in st["traces"] for v in t["values"]]
    assert _has(drawn, 5.0) and _has(drawn, 7.0), drawn


# ================================================================ pie sums

def test_pie_with_repeated_labels_draws_the_label_sums():
    df = pd.DataFrame({"channel": ["web", "app", "web", "app", "phone"],
                       "n": [100.0, 30.0, 50.0, 20.0, 10.0]})
    tr = _stats(_render(PX + "fig = px.pie(dfs['d'], names='channel', values='n')",
                        {"d": df}))["traces"][0]
    assert tr["type"] == "pie"
    assert _has(tr["values"], 150.0) and _has(tr["values"], 50.0), tr["values"]
    assert tr["total"] == pytest.approx(210.0)
    stats = {"traces": [tr]}
    res = _check([("web_n", "count", True), ("web_share", "pct", False)],
                 {"web_n": 150, "web_share": 150 / 210}, stats)
    assert res["match"] == 1 and res["derived"] == 1, res


def test_funnelarea_with_repeated_labels_draws_the_label_sums():
    fig = go.Figure(go.Funnelarea(labels=["a", "b", "a"], values=[5.5, 2.25, 4.5]))
    tr = plot_utils._chart_stats(fig)["traces"][0]
    assert tr["type"] == "funnel"
    assert _has(tr["values"], 10.0), tr["values"]


# ================================================================ value cap

def test_long_line_keeps_its_last_point_min_and_max():
    n = 5000
    y = np.linspace(1.0, 2.0, n)
    y[1234] = -77.5                 # the minimum, in the middle
    y[3456] = 999.25                # the maximum, in the middle
    y[-1] = 432.75                  # the latest point (not the max)
    fig = go.Figure(go.Scatter(x=np.arange(n), y=y, mode="lines"))
    tr = plot_utils._chart_stats(fig)["traces"][0]
    assert len(tr["values"]) <= exec_transport.CHART_STATS_MAX_VALUES
    assert _has(tr["values"], 432.75) and _has(tr["values"], y[0]), "first / last kept"
    assert _has(tr["values"], -77.5) and _has(tr["values"], 999.25), "min / max kept"
    res = _check([("latest", "float", True)], {"latest": 432.75}, {"traces": [tr]})
    assert res["match"] == 1, res


def test_values_below_the_cap_are_unchanged():
    fig = go.Figure(go.Scatter(x=[1, 2, 3], y=[3.5, 1.25, 2.75]))
    assert plot_utils._chart_stats(fig)["traces"][0]["values"] == [3.5, 1.25, 2.75]


# ======================================================= per-trace failures

def test_a_bad_trace_is_skipped_and_the_others_kept(monkeypatch, caplog):
    fig = go.Figure([go.Heatmap(z=[[1.0, 2.0], [3.0]]),
                     go.Bar(x=["a", "b"], y=[11.5, 22.5])])
    real = plot_utils._stats_trace

    def flaky(tr, caps, stack_sums=()):
        if tr.type == "heatmap":
            raise ValueError("boom 31415")
        return real(tr, caps, stack_sums)

    monkeypatch.setattr(plot_utils, "_stats_trace", flaky)
    caplog.set_level(logging.WARNING)
    st = plot_utils._chart_stats(fig)
    assert st is not None and len(st["traces"]) == 1, st
    assert st["traces"][0]["type"] == "bar" and st["traces"][0]["values"] == [11.5, 22.5]
    lines = [r.getMessage() for r in caplog.records if "CHART_STATS_FAILED" in r.getMessage()]
    assert lines and all("error=ValueError" in ln and "31415" not in ln for ln in lines), lines


def test_a_ragged_heatmap_next_to_a_bar_keeps_the_bar():
    fig = go.Figure([go.Heatmap(z=[[1.0, 2.0], [3.0]]),
                     go.Bar(x=["a", "b"], y=[11.5, 22.5])])
    st = plot_utils._chart_stats(fig)
    assert st is not None
    bars = [t for t in st["traces"] if t["type"] == "bar"]
    assert bars and bars[0]["values"] == [11.5, 22.5], st


def test_every_trace_failing_gives_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(plot_utils, "_stats_trace", boom)
    assert plot_utils._chart_stats(go.Figure(go.Bar(x=["a"], y=[1.5]))) is None


# =================================================================== hidden

@pytest.mark.parametrize("visible", [False, "legendonly"])
def test_hidden_traces_are_skipped(visible):
    fig = go.Figure([go.Bar(x=["a", "b"], y=[11.5, 22.5]),
                     go.Bar(x=["a", "b"], y=[777.5, 888.5], visible=visible)])
    st = plot_utils._chart_stats(fig)
    drawn = [v for t in st["traces"] for v in t["values"]]
    assert len(st["traces"]) == 1 and not _has(drawn, 777.5), st


# =============================================================== transport

def test_multi_charts_chart_stats_round_trip(tmp_path):
    code = (PX + "from plotly.subplots import make_subplots\n"
            "fig = make_subplots(rows=1, cols=2)\n"
            "fig.add_trace(go.Bar(x=['a', 'b'], y=[1250.5, 3310.25]), row=1, col=1)\n"
            "fig.add_trace(go.Bar(x=['c', 'd'], y=[17.25, 19.75]), row=1, col=2)\n")
    out = _render(code, {"d": pd.DataFrame({"x": [1]})}, split_multi_axes=True)
    assert len(out.get("multi_charts") or []) == 2, sorted(out)
    job = exec_transport.create_job_dir(tmp_path, exec_transport.new_job_id())
    payload = exec_transport.serialize_result(out, job)
    wire = exec_transport.loads(exec_transport.dumps(payload))
    resp = {"status": "ok", "kind": "PLOT", "payload": wire, "elapsed_ms": 1,
            "peak_rss_mb": 1.0, "stdout": "", "stderr": "", "traceback": "",
            "exit_code": 0, "signal": None, "reason": None}
    decoded = exec_transport.deserialize_result(resp, job, "PLOT", 60)
    found = [c["chart_stats"]["traces"][0]["values"] for c in decoded["multi_charts"]]
    assert sorted(found) == [[17.25, 19.75], [1250.5, 3310.25]], found


def test_decoder_failure_logs_the_type_only(monkeypatch, caplog):
    def boom(_value):
        raise RuntimeError("leak 2718")

    monkeypatch.setattr(exec_transport, "_stats_trace", boom)
    caplog.set_level(logging.WARNING)
    assert exec_transport.decode_chart_stats({"traces": [{"type": "bar"}]}) is None
    lines = [r.getMessage() for r in caplog.records
             if "CHART_STATS_DECODE_FAILED" in r.getMessage()]
    assert lines and "error=RuntimeError" in lines[0] and "2718" not in lines[0], lines


# ============================================================ rendered_slots

def test_rendered_slots_hidden_section_repeated_name_first_sentence():
    template = ("Sales were {{total:amount}} in {{period:period}}. "
                "{{#flag_x}}Hidden {{secret:count}}.{{/flag_x}}"
                "Again {{total:amount}}, share {{share:pct}}.")
    values = {"total": 1500.0, "period": "2026-10", "secret": 5, "share": 0.25}
    slots = narrative.rendered_slots(template, values, {})
    assert slots == [("total", "amount", True), ("period", "period", True),
                     ("share", "pct", False)], slots
    shown = narrative.rendered_slots(template, values, {"flag_x": True})
    assert ("secret", "count", False) in shown, shown


def test_rendered_slots_repeated_name_marks_first_sentence_from_a_later_use():
    slots = narrative.rendered_slots("Intro text. Then {{a:count}}.\n\nOther.", {"a": 12}, {})
    assert slots == [("a", "count", False)], slots
    slots = narrative.rendered_slots("{{a:count}} first. Then {{a:count}}.", {"a": 12}, {})
    assert slots == [("a", "count", True)], slots


def test_rendered_slots_bad_template_is_empty():
    assert narrative.rendered_slots("{{#x}}never closed", {}, {}) == []
    assert narrative.rendered_slots(None, {}, {}) == []


# ========================================================= quartile methods

SAMPLE = [2.0, 3.5, 7.25, 8.0, 11.5, 13.0, 19.75, 21.0, 30.5]       # n = 9, odd, no ties


def _box_group(method):
    fig = go.Figure(go.Box(y=SAMPLE, **({"quartilemethod": method} if method else {})))
    return plot_utils._chart_stats(fig)["traces"][0]["groups"][0]


def test_box_quartiles_default_is_plotly_linear_hazen():
    g = _box_group(None)
    assert g["q1"] == pytest.approx(float(np.quantile(SAMPLE, 0.25, method="hazen")))
    assert g["q3"] == pytest.approx(float(np.quantile(SAMPLE, 0.75, method="hazen")))
    assert g["median"] == pytest.approx(11.5)
    # hazen differs from pandas' linear (type 7) on this sample: pinned, not either
    assert g["q1"] != pytest.approx(float(pd.Series(SAMPLE).quantile(0.25)))


def test_box_quartiles_exclusive_and_inclusive():
    s = sorted(SAMPLE)
    excl = _box_group("exclusive")
    assert excl["q1"] == pytest.approx(float(np.median(s[:4])))
    assert excl["q3"] == pytest.approx(float(np.median(s[5:])))
    incl = _box_group("inclusive")
    assert incl["q1"] == pytest.approx(float(np.median(s[:5])))
    assert incl["q3"] == pytest.approx(float(np.median(s[4:])))
