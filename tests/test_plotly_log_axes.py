"""Automatic log axis — decided per REAL axis (P29 finding F8, task C3).

The regression: `plot_utils._maybe_log_plotly_axes` pooled the y values of
EVERY trace, whatever axis the trace is drawn on (`tr.yaxis`), and could only
switch the PRIMARY axis. A dual-axis chart — revenue bars of 1 200–2 500 on
`y` and a 0.05–0.12 rate line on `y2` — pooled into a "range" of five orders
of magnitude, so the bars' axis went logarithmic although neither axis needs
it; and a secondary axis that genuinely needed log never got it. The switch
also left no trace on the chart: a reader could not tell the axis was log.

Contract pinned here (from the C3 build spec):

* traces are grouped by their axis reference (`xaxis` / `yaxis` of each
  trace: "x", "y", "y2", … → layout `xaxis`, `yaxis`, `yaxis2`, …) and the
  EXISTING rule (≥ 4 numeric points, all > 0, max/median or p95/p5 ≥ 1000)
  decides per axis; only that axis is switched;
* an axis whose `type` the figure set explicitly is never touched (no switch,
  no title note) — `linear` included;
* when the switch fires the axis title gets a "(log scale)" note appended to
  an existing title, or the title "log scale" when it had none;
* single-axis figures decide exactly as before (compared with a verbatim copy
  of today's rule below), and a numeric x axis (horizontal bars) still
  switches on x.

Offline: pure figure post-processing, no sandbox, no browser.
"""
import json
import numbers
import re

import plotly.graph_objects as go
import pytest

import plot_utils

CATS = ["A", "B", "C", "D", "E"]
NOTE_RE = re.compile(r"\(\s*log scale\s*\)", re.I)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run(fig):
    plot_utils._maybe_log_plotly_axes(fig)
    return fig


def _axis(fig, name):
    return getattr(fig.layout, name)


def _type(fig, name):
    return getattr(_axis(fig, name), "type", None)


def _title(fig, name):
    ax = _axis(fig, name)
    t = getattr(ax, "title", None)
    return getattr(t, "text", None) if t is not None else None


def _is_log(fig, name):
    return _type(fig, name) == "log"


def _dual(y_vals, y2_vals, *, y_title=None, y2_title=None,
          y_type=None, y2_type=None):
    """Bars on the primary y, a line on the secondary y2 (overlaying)."""
    fig = go.Figure()
    fig.add_trace(go.Bar(x=CATS[:len(y_vals)], y=y_vals, name="Revenue"))
    fig.add_trace(go.Scatter(x=CATS[:len(y2_vals)], y=y2_vals,
                             mode="lines+markers", name="Rate", yaxis="y2"))
    y = {}
    y2 = {"overlaying": "y", "side": "right"}
    if y_title is not None:
        y["title"] = {"text": y_title}
    if y2_title is not None:
        y2["title"] = {"text": y2_title}
    if y_type is not None:
        y["type"] = y_type
    if y2_type is not None:
        y2["type"] = y2_type
    fig.update_layout(yaxis=y, yaxis2=y2)
    return fig


# Values: each set is judged ALONE by the existing rule.
BARS_MODERATE = [1200, 1500, 1800, 2100, 2500]          # 2500/1800 → linear
RATE_MODERATE = [0.05, 0.07, 0.09, 0.10, 0.12]          # 0.12/0.09 → linear
BARS_EXTREME = [1, 3, 5, 8, 20000]                      # 20000/5 = 4000 → log
RATE_EXTREME = [0.001, 0.002, 0.003, 5.0, 50.0]         # 50/0.003 ≈ 16 667 → log


# ---------------------------------------------------------------------------
# Today's rule, copied verbatim from plot_utils (before C3) so single-axis
# figures can be compared with today's decision. Returns the set of layout
# axis names ("xaxis"/"yaxis") the OLD code would switch to log.
# ---------------------------------------------------------------------------
def _legacy_switches(fig):
    traces = list(fig.data or [])
    if not traces:
        return set()
    for tr in traces:
        if (getattr(tr, "type", None) or "scatter") not in plot_utils._LOG_ALLOWED_TYPES:
            return set()

    def _nums_on(letter):
        out = []
        for tr in traces:
            arr = getattr(tr, letter, None)
            if arr is None:
                continue
            for v in arr:
                if isinstance(v, numbers.Number) and not isinstance(v, bool):
                    out.append(float(v))
        return out

    def _pct(svals, p):
        k = (len(svals) - 1) * p
        f = int(k)
        if f + 1 >= len(svals):
            return svals[-1]
        return svals[f] + (svals[f + 1] - svals[f]) * (k - f)

    switched = set()
    for letter, axis_name in (("x", "xaxis"), ("y", "yaxis")):
        vals = _nums_on(letter)
        if len(vals) < plot_utils._LOG_MIN_POINTS or any(v <= 0 for v in vals):
            continue
        atype = getattr(getattr(fig.layout, axis_name, None), "type", None)
        if atype in ("date", "category", "multicategory", "log"):
            continue
        svals = sorted(vals)
        n = len(svals)
        med = svals[n // 2] if n % 2 else 0.5 * (svals[n // 2 - 1] + svals[n // 2])
        p5, p95 = _pct(svals, 0.05), _pct(svals, 0.95)
        ratio_med = (svals[-1] / med) if med > 0 else float("inf")
        ratio_pct = (p95 / p5) if (p5 and p5 > 0) else float("inf")
        if ratio_med >= plot_utils._LOG_RANGE_RATIO or ratio_pct >= plot_utils._LOG_RANGE_RATIO:
            switched.add(axis_name)
    return switched


# ---------------------------------------------------------------------------
# Dual axis: the F8 case
# ---------------------------------------------------------------------------
def test_the_f8_dual_axis_chart_keeps_both_axes_linear():
    """Bars 1200–2500 on y + a 0.05–0.12 line on y2: neither axis needs log
    on its own values, so neither switches (today y switched — pooled)."""
    fig = _run(_dual(BARS_MODERATE, RATE_MODERATE,
                     y_title="Revenue", y2_title="Conversion rate"))
    assert not _is_log(fig, "yaxis"), "the bars' axis was switched by the line's values"
    assert not _is_log(fig, "yaxis2")
    assert _title(fig, "yaxis") == "Revenue"
    assert _title(fig, "yaxis2") == "Conversion rate"


def test_the_f8_figure_is_the_bug_pattern_today():
    """Guard on the fixture itself: today's pooled rule DOES fire on it."""
    fig = _dual(BARS_MODERATE, RATE_MODERATE)
    assert "yaxis" in _legacy_switches(fig)


def test_only_the_primary_axis_switches_when_only_it_needs_log():
    fig = _run(_dual(BARS_EXTREME, RATE_MODERATE))
    assert _is_log(fig, "yaxis")
    assert not _is_log(fig, "yaxis2")


def test_only_the_secondary_axis_switches_when_only_it_needs_log():
    fig = _run(_dual(BARS_MODERATE, RATE_EXTREME))
    assert _is_log(fig, "yaxis2"), "the secondary axis needed log and did not get it"
    assert not _is_log(fig, "yaxis"), "the primary axis switched on the secondary's values"


def test_both_axes_switch_when_both_need_log():
    fig = _run(_dual(BARS_EXTREME, RATE_EXTREME))
    assert _is_log(fig, "yaxis")
    assert _is_log(fig, "yaxis2")


def test_secondary_axis_needs_at_least_the_minimum_points_on_its_own():
    """Three extreme points on y2 are too few for y2 alone, even though the
    pooled figure has ten numeric y values."""
    fig = _run(_dual(BARS_MODERATE, [0.001, 0.01, 50.0]))
    assert not _is_log(fig, "yaxis2")
    assert not _is_log(fig, "yaxis")


def test_a_non_positive_value_keeps_only_its_own_axis_linear():
    fig = _run(_dual(BARS_EXTREME, [0.0, 0.002, 0.003, 5.0, 50.0]))
    assert _is_log(fig, "yaxis")
    assert not _is_log(fig, "yaxis2")


def test_traces_sharing_an_axis_are_judged_together():
    """Two traces on y2 are pooled with each other (not with y)."""
    fig = _dual(BARS_MODERATE, [0.001, 0.002])
    fig.add_trace(go.Scatter(x=CATS[:3], y=[0.003, 5.0, 50.0], yaxis="y2",
                             mode="lines", name="Other rate"))
    _run(fig)
    assert _is_log(fig, "yaxis2")
    assert not _is_log(fig, "yaxis")


# ---------------------------------------------------------------------------
# Explicit axis types are never touched
# ---------------------------------------------------------------------------
def test_an_explicit_linear_axis_is_never_switched():
    fig = _run(_dual(BARS_MODERATE, RATE_EXTREME,
                     y2_title="Rate", y2_type="linear"))
    assert _type(fig, "yaxis2") == "linear"
    assert _title(fig, "yaxis2") == "Rate", "no note on an axis that was not switched"


def test_an_explicit_log_axis_gets_no_note():
    fig = _run(_dual(BARS_EXTREME, RATE_MODERATE,
                     y_title="Revenue", y_type="log"))
    assert _type(fig, "yaxis") == "log"
    assert _title(fig, "yaxis") == "Revenue"


@pytest.mark.parametrize("atype", ["category", "date", "linear"])
def test_an_explicit_primary_type_is_kept_on_a_single_axis_figure(atype):
    fig = go.Figure(go.Bar(x=CATS, y=BARS_EXTREME))
    fig.update_layout(yaxis={"type": atype, "title": {"text": "Amount"}})
    _run(fig)
    assert _type(fig, "yaxis") == atype
    assert _title(fig, "yaxis") == "Amount"


# ---------------------------------------------------------------------------
# The axis note
# ---------------------------------------------------------------------------
def test_the_note_is_appended_to_an_existing_title():
    fig = _run(_dual(BARS_MODERATE, RATE_EXTREME,
                     y_title="Revenue", y2_title="Conversion rate"))
    t2 = _title(fig, "yaxis2")
    assert t2 is not None
    assert t2.startswith("Conversion rate"), t2
    assert NOTE_RE.search(t2), t2
    assert re.fullmatch(r"Conversion rate\s*\(log scale\)", t2), t2
    # The axis that did not switch keeps its title as written.
    assert _title(fig, "yaxis") == "Revenue"


def test_the_title_is_log_scale_when_the_axis_had_none():
    fig = _run(_dual(BARS_EXTREME, RATE_MODERATE))
    t = _title(fig, "yaxis")
    assert t is not None and t.strip().lower() == "log scale", t
    assert not _title(fig, "yaxis2"), "the linear axis must not get a title"


def test_the_note_is_added_once_when_the_function_runs_twice():
    fig = _dual(BARS_MODERATE, RATE_EXTREME, y2_title="Rate")
    _run(fig)
    _run(fig)
    t2 = _title(fig, "yaxis2")
    assert len(re.findall(r"log scale", t2, re.I)) == 1, t2
    assert _is_log(fig, "yaxis2")


def test_no_note_when_nothing_switches():
    fig = _run(_dual(BARS_MODERATE, RATE_MODERATE, y_title="Revenue"))
    assert "log" not in (_title(fig, "yaxis") or "").lower()
    assert "log" not in (_title(fig, "yaxis2") or "").lower()


def test_the_title_font_and_other_axis_settings_are_kept():
    fig = _dual(BARS_EXTREME, RATE_MODERATE)
    fig.update_layout(yaxis={"title": {"text": "Revenue", "font": {"size": 15}},
                             "tickformat": ",.0f"})
    _run(fig)
    assert _is_log(fig, "yaxis")
    assert fig.layout.yaxis.title.font.size == 15
    assert fig.layout.yaxis.tickformat == ",.0f"


# ---------------------------------------------------------------------------
# Single-axis figures: today's decision unchanged
# ---------------------------------------------------------------------------
_SINGLE_FIXTURES = {
    "skewed_bars": lambda: go.Figure(go.Bar(x=CATS, y=BARS_EXTREME)),
    "moderate_bars": lambda: go.Figure(go.Bar(x=CATS, y=BARS_MODERATE)),
    "zero_value": lambda: go.Figure(go.Bar(x=CATS, y=[0, 1, 10000, 5, 3])),
    "too_few_points": lambda: go.Figure(go.Bar(x=CATS[:3], y=[1, 10, 100000])),
    "two_traces_same_axis": lambda: go.Figure([
        go.Bar(x=CATS[:3], y=[1, 2, 3], name="a"),
        go.Bar(x=CATS[:2], y=[5000, 10000], name="b")]),
    "skewed_line": lambda: go.Figure(go.Scatter(
        x=[1, 2, 3, 4, 5, 6], y=[2, 3, 4, 5, 6, 90000], mode="lines")),
    "horizontal_bars": lambda: go.Figure(go.Bar(
        y=CATS, x=BARS_EXTREME, orientation="h")),
    "scatter_both_numeric": lambda: go.Figure(go.Scatter(
        x=[1, 2, 3, 4, 50000], y=[1, 2, 3, 4, 50000], mode="markers")),
    "pie_aborts": lambda: go.Figure(go.Pie(labels=CATS, values=BARS_EXTREME)),
    "mixed_with_heatmap_aborts": lambda: go.Figure([
        go.Bar(x=CATS, y=BARS_EXTREME),
        go.Heatmap(z=[[1, 2], [3, 4]])]),
}


@pytest.mark.parametrize("name", sorted(_SINGLE_FIXTURES))
def test_single_axis_figures_decide_as_today(name):
    expected = _legacy_switches(_SINGLE_FIXTURES[name]())
    fig = _run(_SINGLE_FIXTURES[name]())
    got = {a for a in ("xaxis", "yaxis") if _is_log(fig, a)}
    assert got == expected, f"{name}: today {sorted(expected)}, now {sorted(got)}"


def test_the_single_axis_fixtures_cover_both_outcomes():
    """Guard: the comparison above is not vacuous."""
    outcomes = {name: bool(_legacy_switches(f())) for name, f in _SINGLE_FIXTURES.items()}
    assert outcomes["skewed_bars"] and outcomes["horizontal_bars"]
    assert not outcomes["moderate_bars"] and not outcomes["pie_aborts"]


def test_a_numeric_x_axis_switches_on_x_with_the_note():
    """Horizontal bars carry the metric on x: x switches, y (categories) not."""
    fig = go.Figure(go.Bar(y=CATS, x=BARS_EXTREME, orientation="h"))
    fig.update_layout(xaxis={"title": {"text": "Revenue"}})
    _run(fig)
    assert _is_log(fig, "xaxis")
    assert not _is_log(fig, "yaxis")
    assert re.fullmatch(r"Revenue\s*\(log scale\)", _title(fig, "xaxis") or ""), \
        _title(fig, "xaxis")


def test_a_secondary_x_axis_is_judged_on_its_own():
    """Two horizontal bar traces on x and x2 (overlaying): only x2 needs log."""
    fig = go.Figure()
    fig.add_trace(go.Bar(y=CATS, x=BARS_MODERATE, orientation="h", name="a"))
    fig.add_trace(go.Bar(y=CATS, x=BARS_EXTREME, orientation="h", name="b",
                         xaxis="x2"))
    fig.update_layout(xaxis2={"overlaying": "x", "side": "top"})
    _run(fig)
    assert _is_log(fig, "xaxis2")
    assert not _is_log(fig, "xaxis")


def test_subplot_axes_are_judged_per_subplot():
    """make_subplots: each subplot's y axis decides on its own traces."""
    from plotly.subplots import make_subplots
    fig = make_subplots(rows=1, cols=2)
    fig.add_trace(go.Bar(x=CATS, y=BARS_MODERATE), row=1, col=1)
    fig.add_trace(go.Bar(x=CATS, y=BARS_EXTREME), row=1, col=2)
    _run(fig)
    assert _is_log(fig, "yaxis2")
    assert not _is_log(fig, "yaxis")


# ---------------------------------------------------------------------------
# Never raises, and the export path uses it
# ---------------------------------------------------------------------------
def test_a_trace_pointing_at_a_missing_layout_axis_does_not_raise():
    fig = go.Figure()
    fig.add_trace(go.Bar(x=CATS, y=BARS_MODERATE))
    fig.add_trace(go.Scatter(x=CATS, y=RATE_EXTREME, yaxis="y3"))
    _run(fig)   # must not raise (Article IV)
    assert not _is_log(fig, "yaxis")


def test_plotly_to_html_keeps_the_f8_chart_linear():
    fig = _dual(BARS_MODERATE, RATE_MODERATE, y_title="Revenue", y2_title="Rate")
    html = plot_utils._plotly_to_html(fig)
    assert isinstance(html, str) and html
    assert not _is_log(fig, "yaxis")
    assert not _is_log(fig, "yaxis2")


def _capture(fig):
    """The capture path's order (plot_utils `_render_in_process`): the HTML
    first — which runs the log step on the figure — then "Show data" from
    the SAME figure."""
    html = plot_utils._plotly_to_html(fig)
    return html, plot_utils._extract_plotly_chart_data(fig)


def test_show_data_keeps_the_measure_name_of_a_switched_axis():
    fig = go.Figure(go.Bar(x=CATS, y=BARS_EXTREME))
    fig.update_layout(yaxis={"title": {"text": "Revenue"}},
                      xaxis={"title": {"text": "Region"}})
    _html, data = _capture(fig)
    # Guard: the axis really switched and carries the note on the chart.
    assert _is_log(fig, "yaxis")
    assert NOTE_RE.search(_title(fig, "yaxis") or ""), _title(fig, "yaxis")
    assert data and data.get("columns"), data
    cols = [str(c) for c in data["columns"]]
    assert "Revenue" in cols, cols
    assert not any("log scale" in c.lower() for c in cols), cols


def test_show_data_keeps_the_default_header_of_an_untitled_switched_axis():
    import copy
    before = plot_utils._extract_plotly_chart_data(
        copy.deepcopy(go.Figure(go.Bar(x=CATS, y=BARS_EXTREME))))
    fig = go.Figure(go.Bar(x=CATS, y=BARS_EXTREME))
    _html, data = _capture(fig)
    assert _is_log(fig, "yaxis")
    assert (_title(fig, "yaxis") or "").strip().lower() == "log scale"
    assert data and before, (data, before)
    assert data["columns"] == before["columns"], (data["columns"], before["columns"])
    assert not any("log scale" in str(c).lower() for c in data["columns"]), data["columns"]


def test_show_data_names_both_measures_on_a_dual_axis_chart():
    fig = _dual(BARS_MODERATE, RATE_EXTREME, y_title="Revenue", y2_title="Rate")
    _html, data = _capture(fig)
    assert _is_log(fig, "yaxis2")
    text = json.dumps(data)
    assert "log scale" not in text.lower(), text


@pytest.mark.parametrize("titled", [True, False])
def test_the_private_title_attribute_never_reaches_the_chart(titled):
    attr = getattr(plot_utils, "_LOG_TITLE_ORIG_ATTR", "_pdc_title_before_log")
    assert attr == "_pdc_title_before_log"
    fig = _dual(BARS_EXTREME, RATE_EXTREME,
                y_title="Revenue" if titled else None,
                y2_title="Rate" if titled else None)
    html, _data = _capture(fig)
    assert _is_log(fig, "yaxis") and _is_log(fig, "yaxis2")
    assert attr not in html
    assert attr not in fig.to_json()
    assert attr not in json.dumps(fig.to_plotly_json(), default=str)
    assert attr not in json.dumps(fig.to_dict(), default=str)


def test_plotly_to_html_switches_only_the_secondary_axis():
    fig = _dual(BARS_MODERATE, RATE_EXTREME, y_title="Revenue", y2_title="Rate")
    plot_utils._plotly_to_html(fig)
    assert not _is_log(fig, "yaxis")
    assert _is_log(fig, "yaxis2")
    assert NOTE_RE.search(_title(fig, "yaxis2") or "")
