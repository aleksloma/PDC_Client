"""PROMPT 31 Part C — `chart_stats`: the numbers a Plotly chart draws, computed
in the sandbox from the FULL figure next to `chart_data`, and the web app's
STRICT hostile-input decoder for them.

Sandbox side (`plot_utils._render_in_process`, called directly — the runner's
entry point, the tests/test_values_capture.py idiom):

    chart_stats = {"traces": [
      {"type": "bar"|"scatter"|"pie"|"box"|"violin"|"histogram"|"heatmap"|
               "treemap"|"sunburst"|"icicle"|"waterfall"|"funnel"|"other",
       "values": [<drawn numbers, <= 2000, finite floats>],
       "total": <sum of values or None>,
       "groups": [{"n", "mean", "median", "q1", "q3", "min", "max"}, ...],  # <= 50
       "draws_mean": bool},                                                # box/violin
      ...]}                                                                # <= 20 traces

Matplotlib → None; any failure → None + `CHART_STATS_FAILED error=<Type>`.

QUARTILE METHOD. The reference statistics here are pandas'
`Series.quantile(q)` with its default LINEAR interpolation (Hyndman & Fan
method 7). plotly.js' own default `quartilemethod: "linear"` interpolates at
`p * n - 0.5` (method 5), so the two differ on general data; every sample in
this file whose quartiles are asserted is built with TIES around the q1 / q3
positions, where both methods give the same number — so the tests pin the
statistic without pinning the implementation's method (either is accepted by
the spec). The one sample without that property (`test_box_q1_q3_method`)
accepts either method's value explicitly.

Web side: `exec_transport.deserialize_result(..., "PLOT")` decodes a
`chart_stats` payload key through the new decoder — numbers only, finite, the
caps above, unknown keys dropped, a wrong shape → None and NEVER a violation
of the whole response (the same discipline as `decode_values`). The decoder
is exercised through `deserialize_result`, so its own function name is the
implementation's choice.

Offline: `.venv/Scripts/python.exe -m pytest tests/test_chart_stats.py -q`.
"""
import json
import logging
import math
import time

import numpy as np
import pandas as pd
import pytest

import exec_transport
import plot_utils

TYPES = {"bar", "scatter", "pie", "box", "violin", "histogram", "heatmap", "treemap",
         "sunburst", "icicle", "waterfall", "funnel", "other"}
GROUP_KEYS = {"n", "mean", "median", "q1", "q3", "min", "max"}
TRACE_KEYS = {"type", "values", "total", "groups", "draws_mean"}


@pytest.fixture(autouse=True)
def _close_figures():
    import matplotlib.pyplot as plt
    plt.close("all")
    yield
    plt.close("all")


# ----------------------------------------------------------------- helpers

def _render(code: str, frames: dict, **kw) -> dict:
    out = plot_utils._render_in_process(code, frames, "t", **kw)
    assert out.get("ok") is True, out.get("error")
    return out


def _stats(out: dict) -> dict:
    stats = out.get("chart_stats")
    assert isinstance(stats, dict), f"no chart_stats: {sorted(out)}"
    traces = stats.get("traces")
    assert isinstance(traces, list) and traces, stats
    for tr in traces:
        _assert_trace_shape(tr)
    return stats


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _assert_trace_shape(tr):
    assert isinstance(tr, dict), tr
    assert set(tr) <= TRACE_KEYS, sorted(tr)
    assert tr.get("type") in TYPES, tr.get("type")
    values = tr.get("values") or []
    assert isinstance(values, list) and len(values) <= 2000, len(values)
    assert all(_is_num(v) for v in values), [v for v in values if not _is_num(v)][:5]
    total = tr.get("total")
    assert total is None or _is_num(total), total
    groups = tr.get("groups") or []
    assert isinstance(groups, list) and len(groups) <= 50, len(groups)
    for g in groups:
        assert isinstance(g, dict) and set(g) <= GROUP_KEYS, g
        for k, v in g.items():
            assert _is_num(v), (k, v)
        if "n" in g:
            assert float(g["n"]).is_integer() and g["n"] >= 0, g
    dm = tr.get("draws_mean", False)
    assert isinstance(dm, bool), dm


def _approx_list(got, expected):
    assert [float(v) for v in got] == pytest.approx([float(v) for v in expected]), (got, expected)


def _ref_group(sample) -> dict:
    s = pd.Series(sample, dtype="float64")
    return {"n": int(len(s)), "mean": float(s.mean()), "median": float(s.median()),
            "q1": float(s.quantile(0.25)), "q3": float(s.quantile(0.75)),
            "min": float(s.min()), "max": float(s.max())}


def _assert_groups(groups, samples):
    """Group order is the implementation's: matched by (n, median, mean)."""
    want = sorted((_ref_group(s) for s in samples), key=lambda g: (g["n"], g["median"], g["mean"]))
    got = sorted(groups, key=lambda g: (g.get("n", 0), g.get("median", 0), g.get("mean", 0)))
    assert len(got) == len(want), (got, want)
    for g, w in zip(got, want):
        for k in GROUP_KEYS:
            assert k in g, (k, g)
            assert float(g[k]) == pytest.approx(w[k]), (k, g, w)


def _channels() -> pd.DataFrame:
    return pd.DataFrame({"channel": ["online", "branch", "phone"],
                         "orders": [139415, 45000, 15585]})


# Tie-built samples (see QUARTILE METHOD): n = 11, the q1 / q3 positions of
# both methods fall inside runs of equal values.
SEG_A = [1, 1, 1, 1, 5, 5, 5, 9, 9, 9, 9]
SEG_B = [20, 20, 20, 20, 30, 30, 30, 70, 70, 70, 70]


def _segments() -> pd.DataFrame:
    return pd.DataFrame({"segment": ["a"] * len(SEG_A) + ["b"] * len(SEG_B),
                         "amount": [float(v) for v in SEG_A + SEG_B]})


PX = "import plotly.express as px\nimport plotly.graph_objects as go\n"


# ============================================================= bar / line / pie

def test_px_bar_vertical_values_and_total():
    out = _render(PX + "fig = px.bar(dfs['c'], x='channel', y='orders')", {"c": _channels()})
    st = _stats(out)
    assert len(st["traces"]) == 1
    tr = st["traces"][0]
    assert tr["type"] == "bar"
    _approx_list(tr["values"], [139415, 45000, 15585])
    assert tr["total"] == pytest.approx(200000)


def test_px_bar_horizontal_takes_values_from_x():
    out = _render(PX + "fig = px.bar(dfs['c'], x='orders', y='channel', orientation='h')",
                  {"c": _channels()})
    tr = _stats(out)["traces"][0]
    assert tr["type"] == "bar"
    _approx_list(tr["values"], [139415, 45000, 15585])
    assert tr["total"] == pytest.approx(200000)


def test_px_bar_colored_gives_one_trace_per_series():
    df = pd.DataFrame({"m": ["J", "F", "J", "F"], "s": ["x", "x", "y", "y"],
                       "v": [11.5, 12.5, 21.25, 22.75]})
    out = _render(PX + "fig = px.bar(dfs['d'], x='m', y='v', color='s', barmode='group')",
                  {"d": df})
    traces = _stats(out)["traces"]
    assert len(traces) == 2
    assert sorted(sorted(t["values"]) for t in traces) == [[11.5, 12.5], [21.25, 22.75]]


def test_px_line_values():
    df = pd.DataFrame({"month": ["2026-01", "2026-02", "2026-03", "2026-04"],
                       "sales": [1250.5, 1310.25, 990.0, 1777.75]})
    out = _render(PX + "fig = px.line(dfs['d'], x='month', y='sales')", {"d": df})
    tr = _stats(out)["traces"][0]
    assert tr["type"] == "scatter"
    _approx_list(tr["values"], [1250.5, 1310.25, 990.0, 1777.75])
    assert tr["total"] == pytest.approx(5328.5)


def test_px_pie_values_and_total():
    out = _render(PX + "fig = px.pie(dfs['c'], names='channel', values='orders')",
                  {"c": _channels()})
    tr = _stats(out)["traces"][0]
    assert tr["type"] == "pie"
    assert sorted(tr["values"]) == pytest.approx(sorted([139415, 45000, 15585]))
    assert tr["total"] == pytest.approx(200000)


# ============================================================ box / violin / hist

def test_px_box_groups_per_x_category():
    out = _render(PX + "fig = px.box(dfs['d'], x='segment', y='amount')", {"d": _segments()})
    traces = _stats(out)["traces"]
    assert len(traces) == 1
    tr = traces[0]
    assert tr["type"] == "box"
    _assert_groups(tr["groups"], [SEG_A, SEG_B])
    assert tr.get("draws_mean") is False


def test_px_box_horizontal_groups_per_y_category():
    out = _render(PX + "fig = px.box(dfs['d'], x='amount', y='segment', orientation='h')",
                  {"d": _segments()})
    tr = _stats(out)["traces"][0]
    assert tr["type"] == "box"
    _assert_groups(tr["groups"], [SEG_A, SEG_B])


def test_px_box_without_category_is_one_group_per_trace():
    out = _render(PX + "fig = px.box(dfs['d'], y='amount')", {"d": _segments()})
    tr = _stats(out)["traces"][0]
    _assert_groups(tr["groups"], [SEG_A + SEG_B])


def test_px_box_colored_gives_groups_in_each_trace():
    df = _segments()
    df["tier"] = ["x"] * len(df)
    out = _render(PX + "fig = px.box(dfs['d'], x='segment', y='amount', color='tier')",
                  {"d": df})
    groups = [g for t in _stats(out)["traces"] for g in t["groups"]]
    _assert_groups(groups, [SEG_A, SEG_B])


@pytest.mark.parametrize("boxmean", ["True", "'sd'"])
def test_box_with_boxmean_draws_the_mean(boxmean):
    out = _render(PX + "fig = px.box(dfs['d'], x='segment', y='amount')\n"
                  f"fig.update_traces(boxmean={boxmean})", {"d": _segments()})
    tr = _stats(out)["traces"][0]
    assert tr["draws_mean"] is True
    _assert_groups(tr["groups"], [SEG_A, SEG_B])


def test_go_box_precomputed_quartiles_is_one_group():
    code = PX + ("fig = go.Figure(go.Box(q1=[12.0], median=[17.5], q3=[23.0], "
                 "lowerfence=[3.0], upperfence=[41.0], x=['all']))")
    out = _render(code, {"d": _segments()})
    tr = _stats(out)["traces"][0]
    assert tr["type"] == "box"
    assert len(tr["groups"]) == 1
    g = tr["groups"][0]
    assert g["q1"] == pytest.approx(12.0)
    assert g["median"] == pytest.approx(17.5)
    assert g["q3"] == pytest.approx(23.0)


def test_box_q1_q3_method():
    """A sample WITHOUT ties: q1 / q3 equal pandas' linear (method 7) or
    plotly.js' default 'linear' quartilemethod (method 5, numpy 'hazen')."""
    sample = [3.0, 7.0, 8.0, 15.0, 22.0, 41.0]
    df = pd.DataFrame({"amount": sample})
    out = _render(PX + "fig = px.box(dfs['d'], y='amount')", {"d": df})
    g = _stats(out)["traces"][0]["groups"][0]
    for q, key in ((25, "q1"), (75, "q3")):
        linear = float(np.percentile(sample, q, method="linear"))
        hazen = float(np.percentile(sample, q, method="hazen"))
        assert any(g[key] == pytest.approx(v) for v in (linear, hazen)), (key, g[key], linear, hazen)
    assert g["median"] == pytest.approx(float(np.median(sample)))
    assert g["n"] == 6


def test_px_violin_groups_and_meanline():
    out = _render(PX + "fig = px.violin(dfs['d'], x='segment', y='amount')", {"d": _segments()})
    tr = _stats(out)["traces"][0]
    assert tr["type"] == "violin"
    _assert_groups(tr["groups"], [SEG_A, SEG_B])
    assert tr.get("draws_mean") is False
    out2 = _render(PX + "fig = px.violin(dfs['d'], x='segment', y='amount')\n"
                   "fig.update_traces(meanline_visible=True)", {"d": _segments()})
    assert _stats(out2)["traces"][0]["draws_mean"] is True


def test_px_histogram_one_group_per_trace():
    out = _render(PX + "fig = px.histogram(dfs['d'], x='amount')", {"d": _segments()})
    tr = _stats(out)["traces"][0]
    assert tr["type"] == "histogram"
    _assert_groups(tr["groups"], [SEG_A + SEG_B])


def test_px_histogram_colored_one_group_per_trace():
    out = _render(PX + "fig = px.histogram(dfs['d'], x='amount', color='segment')",
                  {"d": _segments()})
    groups = [g for t in _stats(out)["traces"] for g in t["groups"]]
    _assert_groups(groups, [SEG_A, SEG_B])


# ============================================================ matrix / hierarchy

def test_px_imshow_heatmap_values_are_z():
    df = pd.DataFrame({"city": ["T", "T", "B", "B"], "product": ["m", "c", "m", "c"],
                       "n": [11.5, 13.25, 17.75, 19.5]})
    code = PX + ("piv = dfs['d'].pivot_table(index='city', columns='product', values='n')\n"
                 "fig = px.imshow(piv, text_auto=True)")
    tr = _stats(_render(code, {"d": df}))["traces"][0]
    assert tr["type"] == "heatmap"
    assert sorted(tr["values"]) == pytest.approx([11.5, 13.25, 17.75, 19.5])


def test_go_heatmap_values_are_z():
    code = PX + "fig = go.Figure(go.Heatmap(z=[[1.5, 2.5], [3.5, 4.5]], x=['a','b'], y=['c','d']))"
    tr = _stats(_render(code, {"d": _segments()}))["traces"][0]
    assert tr["type"] == "heatmap"
    assert sorted(tr["values"]) == pytest.approx([1.5, 2.5, 3.5, 4.5])


def test_px_treemap_node_values():
    df = pd.DataFrame({"region": ["N", "N", "S"], "city": ["a", "b", "c"],
                       "n": [1200.0, 3400.0, 5600.0]})
    code = PX + "fig = px.treemap(dfs['d'], path=['region', 'city'], values='n')"
    tr = _stats(_render(code, {"d": df}))["traces"][0]
    assert tr["type"] == "treemap"
    got = [round(v, 6) for v in tr["values"]]
    for leaf in (1200.0, 3400.0, 5600.0):
        assert leaf in got, (leaf, got)
    # the parents px computed are hierarchy node values too
    assert 4600.0 in got, got


# ======================================================================= caps

def test_cap_values_per_trace():
    df = pd.DataFrame({"k": [f"k{i}" for i in range(5000)], "v": np.arange(5000) + 0.5})
    tr = _stats(_render(PX + "fig = px.bar(dfs['d'], x='k', y='v')", {"d": df}))["traces"][0]
    assert 0 < len(tr["values"]) <= 2000


def test_cap_traces():
    code = PX + ("fig = go.Figure()\n"
                 "for i in range(30):\n"
                 "    fig.add_trace(go.Bar(x=['a', 'b'], y=[i + 0.5, i + 1.5], name=f's{i}'))")
    st = _stats(_render(code, {"d": _segments()}))
    assert 0 < len(st["traces"]) <= 20


def test_cap_groups():
    rng = np.random.default_rng(7)
    df = pd.DataFrame({"g": [f"g{i:02d}" for i in range(80) for _ in range(5)],
                       "v": rng.normal(100.0, 5.0, 400)})
    tr = _stats(_render(PX + "fig = px.box(dfs['d'], x='g', y='v')", {"d": df}))["traces"][0]
    assert 0 < len(tr["groups"]) <= 50


def test_200k_point_box_is_bounded_and_uses_the_full_figure():
    """Computed from the FULL figure (not the 5,000-row "Show data" cut) in
    bounded time."""
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"segment": ["a"] * 120_000 + ["b"] * 80_000,
                       "amount": rng.normal(100.0, 10.0, 200_000)})
    t0 = time.monotonic()
    out = _render(PX + "fig = px.box(dfs['d'], x='segment', y='amount')", {"d": df})
    elapsed = time.monotonic() - t0
    tr = _stats(out)["traces"][0]
    assert sorted(int(g["n"]) for g in tr["groups"]) == [80_000, 120_000]
    a = df.loc[df.segment == "a", "amount"]
    ga = max(tr["groups"], key=lambda g: g["n"])
    assert ga["median"] == pytest.approx(float(a.median()), rel=1e-6)
    assert ga["mean"] == pytest.approx(float(a.mean()), rel=1e-9)
    assert elapsed < 20.0, elapsed


# ================================================================ no / bad data

def test_matplotlib_has_no_chart_stats():
    out = plot_utils._render_in_process(
        "plt.figure()\nplt.bar(['a', 'b'], [1250.5, 3310.25])\n", {"d": _segments()}, "t")
    assert out.get("ok") is True, out.get("error")
    assert out.get("chart_stats") is None


def test_non_finite_numbers_never_appear():
    code = PX + ("fig = go.Figure(go.Bar(x=['a', 'b', 'c', 'd'], "
                 "y=[1.5, float('nan'), float('inf'), 2.5]))")
    out = _render(code, {"d": _segments()})
    stats = out.get("chart_stats")
    if stats is not None:
        tr = _stats(out)["traces"][0]
        _approx_list(tr["values"], [1.5, 2.5])
    # wire-safe: no NaN / Infinity token anywhere
    assert "NaN" not in exec_transport.dumps(stats) and "Infinity" not in exec_transport.dumps(stats)


def test_broken_statistics_degrade_to_none_and_keep_the_chart(monkeypatch, caplog):
    """Any failure → chart_stats None + `CHART_STATS_FAILED error=<Type>`; the
    chart itself renders. Assumes the group statistics are computed with
    numpy / pandas / statistics (any of them) — all are made to raise."""
    import statistics

    def boom(*a, **k):
        raise RuntimeError("stats exploded 4242.42")

    for name in ("percentile", "quantile", "nanpercentile", "nanquantile", "median",
                 "nanmedian", "mean", "nanmean", "sort", "average"):
        monkeypatch.setattr(np, name, boom)
    for name in ("quantile", "median", "mean", "describe"):
        monkeypatch.setattr(pd.Series, name, boom)
    for name in ("median", "mean", "quantiles", "fmean"):
        monkeypatch.setattr(statistics, name, boom)
    caplog.set_level(logging.DEBUG)
    out = plot_utils._render_in_process(PX + "fig = px.box(dfs['d'], x='segment', y='amount')",
                                        {"d": _segments()}, "t")
    assert out.get("ok") is True, out.get("error")
    assert out.get("plotly_html")
    assert out.get("chart_stats") is None
    lines = [r.getMessage() for r in caplog.records if "CHART_STATS_FAILED" in r.getMessage()]
    assert lines, "no CHART_STATS_FAILED line"
    assert any("error=RuntimeError" in ln for ln in lines), lines
    # sandbox log rules: the type, never the text
    assert not any("exploded" in ln or "4242" in ln for ln in lines), lines


def test_sandbox_logs_carry_no_statistic(caplog):
    caplog.set_level(logging.DEBUG)
    df = pd.DataFrame({"segment": ["p"] * 5 + ["m"] * 5,
                       "amount": [7311.0, 7311.0, 7311.0, 8423.0, 61977.0,
                                  523.0, 523.0, 641.0, 641.0, 9187.0]})
    out = _render(PX + "fig = px.box(dfs['d'], x='segment', y='amount')", {"d": df})
    _stats(out)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for shown in ("18466.6", "18,467", "2303", "2,303", "61977", "61,977", "8423", "9187"):
        assert shown not in text, shown


# ============================================================ split / transport

def test_chart_stats_is_a_transport_plot_key():
    assert "chart_stats" in exec_transport._PLOT_KEYS
    assert "chart_stats" not in exec_transport._PYTHON_KEYS


def test_split_grid_charts_carry_stats():
    """The multi-chart (split) path computes stats too: per sub-chart in
    `multi_charts` and/or for the whole figure at the top level."""
    code = PX + ("from plotly.subplots import make_subplots\n"
                 "fig = make_subplots(rows=1, cols=2)\n"
                 "fig.add_trace(go.Bar(x=['a', 'b'], y=[1250.5, 3310.25]), row=1, col=1)\n"
                 "fig.add_trace(go.Bar(x=['c', 'd'], y=[17.25, 19.75]), row=1, col=2)\n")
    out = _render(code, {"d": _segments()}, split_multi_axes=True)
    assert out.get("multi_charts"), sorted(out)
    found = []
    top = out.get("chart_stats")
    if isinstance(top, dict):
        found += [sorted(t.get("values") or []) for t in top.get("traces") or []]
    for c in out["multi_charts"]:
        st = c.get("chart_stats")
        if isinstance(st, dict):
            found += [sorted(t.get("values") or []) for t in st.get("traces") or []]
    assert [1250.5, 3310.25] in found and [17.25, 19.75] in found, found


def _job_dir(tmp_path):
    return exec_transport.create_job_dir(tmp_path, exec_transport.new_job_id())


def _response(payload, kind="PLOT"):
    return {"status": "ok", "kind": kind, "payload": payload, "elapsed_ms": 1,
            "peak_rss_mb": 1.0, "stdout": "", "stderr": "", "traceback": "",
            "exit_code": 0, "signal": None, "reason": None}


def _wire(obj):
    return exec_transport.loads(exec_transport.dumps(obj))


def test_render_output_round_trips_through_the_transport(tmp_path):
    out = _render(PX + "fig = px.box(dfs['d'], x='segment', y='amount')\n"
                  "fig.update_traces(boxmean=True)", {"d": _segments()})
    job = _job_dir(tmp_path)
    payload = exec_transport.serialize_result(out, job)
    decoded = exec_transport.deserialize_result(_response(_wire(payload)), job, "PLOT", 60)
    assert decoded.get("ok") is True, decoded.get("error")
    st = decoded.get("chart_stats")
    assert isinstance(st, dict), sorted(decoded)
    tr = st["traces"][0]
    assert tr["type"] == "box" and tr["draws_mean"] is True
    _assert_groups(tr["groups"], [SEG_A, SEG_B])


# =========================================================== hostile decoder

def _decode(stats, tmp_path, kind="PLOT", include=True):
    payload = {"ok": True, "is_plotly": True, "plotly_html": "<div>chart</div>",
               "chart_data": None}
    if include:
        payload["chart_stats"] = stats
    job = _job_dir(tmp_path)
    out = exec_transport.deserialize_result(_response(_wire(payload), kind=kind), job, kind, 60)
    # a hostile chart_stats never turns the chart into a failed execution
    if kind == "PLOT":
        assert out.get("ok") is True, out
        assert out.get("plotly_html") == "<div>chart</div>", out
        assert not out.get("error"), out
    return out


def _valid():
    return {"traces": [
        {"type": "bar", "values": [139415.0, 45000.0, 15585.0], "total": 200000.0,
         "groups": [], "draws_mean": False},
        {"type": "box", "values": [], "total": None, "draws_mean": True,
         "groups": [{"n": 5, "mean": 18466.6, "median": 7311.0, "q1": 7311.0,
                     "q3": 8423.0, "min": 7311.0, "max": 61977.0}]},
    ]}


def _walk_numbers(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_numbers(v)
    else:
        yield obj


def _assert_strict(st):
    """Everything a decoded chart_stats may contain."""
    if st is None:
        return
    assert isinstance(st, dict) and set(st) <= {"traces"}, st
    assert isinstance(st["traces"], list) and len(st["traces"]) <= 20
    for tr in st["traces"]:
        _assert_trace_shape(tr)


def test_decoder_keeps_a_valid_structure(tmp_path):
    st = _decode(_valid(), tmp_path).get("chart_stats")
    _assert_strict(st)
    assert st is not None
    bar, box = st["traces"]
    _approx_list(bar["values"], [139415, 45000, 15585])
    assert bar["total"] == pytest.approx(200000)
    assert box["draws_mean"] is True
    g = box["groups"][0]
    assert g["n"] == 5 and g["mean"] == pytest.approx(18466.6) and g["max"] == pytest.approx(61977)


def test_decoder_absent_key_stays_absent(tmp_path):
    out = _decode(None, tmp_path, include=False)
    assert out.get("chart_stats") is None


@pytest.mark.parametrize("bad", [[], "traces", 5, True, {"traces": "x"},
                                 {"traces": {"type": "bar"}}, {"traces": None}])
def test_decoder_wrong_shape_is_none(bad, tmp_path):
    assert _decode(bad, tmp_path).get("chart_stats") is None


def test_decoder_empty_dict_is_none_or_empty(tmp_path):
    st = _decode({}, tmp_path).get("chart_stats")
    assert st is None or st == {"traces": []}, st


def test_decoder_drops_non_finite_strings_bools_and_nested(tmp_path):
    raw = {"traces": [{"type": "bar",
                       "values": [1.5, float("nan"), float("inf"), float("-inf"), "7",
                                  True, None, [2.0], {"v": 3.0}, 4.5, 2.5],
                       "total": float("nan"), "groups": [], "draws_mean": False}]}
    st = _decode(raw, tmp_path).get("chart_stats")
    _assert_strict(st)
    assert st is not None
    _approx_list(st["traces"][0]["values"], [1.5, 4.5, 2.5])
    assert st["traces"][0].get("total") is None


def test_decoder_group_junk(tmp_path):
    raw = {"traces": [{"type": "box", "values": [], "total": None, "draws_mean": "yes",
                       "groups": [
                           {"n": 5, "mean": float("nan"), "median": "7311", "q1": [1.0],
                            "q3": 8423.0, "min": True, "max": 61977.0, "label": "premium"},
                           "not a group", 7, None,
                           {"n": "5", "median": 12.5},
                           {"n": -3, "median": 13.5},
                           {"n": 2.5, "median": 14.5},
                       ]}]}
    st = _decode(raw, tmp_path).get("chart_stats")
    _assert_strict(st)
    if st is None or not st["traces"]:
        return
    tr = st["traces"][0]
    assert tr.get("draws_mean") is not True        # "yes" is not a bool
    nums = list(_walk_numbers(tr.get("groups")))
    assert "premium" not in nums and "7311" not in nums
    for g in tr.get("groups") or []:
        assert "label" not in g
        if "n" in g:
            assert isinstance(g["n"], int) and not isinstance(g["n"], bool) and g["n"] >= 0, g


def test_decoder_drops_unknown_keys_and_types(tmp_path):
    raw = {"junk": {"x": 1}, "traces": [
        {"type": "bar", "values": [1.5], "total": 1.5, "groups": [], "draws_mean": False,
         "evil": "payload", "labels": ["premium"]},
        {"type": "evil\ntype", "values": [2.5], "total": 2.5, "groups": []},
        {"type": 7, "values": [3.5]},
        "not a trace", None, 12,
    ]}
    st = _decode(raw, tmp_path).get("chart_stats")
    _assert_strict(st)
    if st is None:
        return
    assert "junk" not in st
    text = json.dumps(st)
    assert "payload" not in text and "premium" not in text and "evil" not in text


def test_decoder_caps(tmp_path):
    raw = {"traces": [{"type": "bar", "values": [float(i) + 0.5 for i in range(100_000)],
                       "total": 1.0, "groups": [{"n": 10, "median": float(i)} for i in range(500)],
                       "draws_mean": False} for _ in range(100)]}
    st = _decode(raw, tmp_path).get("chart_stats")
    _assert_strict(st)                    # <= 20 traces, <= 2000 values, <= 50 groups


def test_decoder_deep_nesting_is_not_a_crash(tmp_path):
    deep = [1.0]
    for _ in range(200):
        deep = [deep]
    raw = {"traces": [{"type": "bar", "values": deep, "total": None, "groups": [deep]}]}
    st = _decode(raw, tmp_path).get("chart_stats")
    _assert_strict(st)


def test_python_kind_never_carries_chart_stats(tmp_path):
    payload = {"error": None, "result": {"kind": "none"}, "preview": None,
               "image_base64": None, "chart_stats": _valid()}
    job = _job_dir(tmp_path)
    out = exec_transport.deserialize_result(_response(_wire(payload), kind="PYTHON"),
                                            job, "PYTHON", 60)
    assert "chart_stats" not in out
