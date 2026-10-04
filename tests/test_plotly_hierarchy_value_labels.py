"""Value labels of treemap / sunburst / icicle charts follow the label rule
(BRB findings 2, item 2).

The regression: a treemap whose code asked for
`textinfo='label+value+percent entry'` showed raw floats such as
"107.7232452" — the hierarchical traces were never reached by
`plot_utils._format_plotly_numeric`, which groups bar/pie/line labels as
"12,345" (integer-valued series, d3 ",.0f") or with at most 2 decimals
(",.2~f").

Pinned after `plot_utils._plotly_to_html(fig)`:

* `textinfo` containing `value` on a hierarchical trace becomes a
  `texttemplate` with `%{value:<rule format>}`, keeping the label and the
  percent part, never a bare `%{value}`; the hover template is untouched;
* a preset template binding a bare `%{value}` is regrouped;
* an already formatted value template is left alone;
* a `textinfo` without `value` leaves `texttemplate` unset;
* no regression on pie (`textinfo='percent'`) and bar (`text_auto=True`).

Icicle: plotly.js 2.35.2 DRAWS the dummy root of a multi-root icicle and a
single template string is applied to it (it then shows the literal template
text), so for an icicle the fix may set a per-node template LIST. Either
shape is accepted there, with the rule applied to every element that binds a
value; sunburst and treemap must keep a single string.

Offline, real figures.
"""
import warnings

import pandas as pd
import plotly.express as px
import pytest

import plot_utils

FLOAT_FMT = "%{value:,.2~f}"
INT_FMT = "%{value:,.0f}"
HIER = {"treemap": px.treemap, "sunburst": px.sunburst, "icicle": px.icicle}


def _frame(values) -> pd.DataFrame:
    return pd.DataFrame({"region": ["North", "North", "South"],
                         "branch": ["B1", "B2", "B3"],
                         "amount": values})


def _fig(kind: str, values):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return HIER[kind](_frame(values), path=["region", "branch"], values="amount")


def _templates(kind: str, tt) -> list:
    """The template string(s) to check. Sunburst / treemap: exactly one
    string. Icicle: a string, or a per-node list/tuple (dummy root drawn by
    plotly.js), every element a string; empty elements carry no value part."""
    if kind != "icicle" or isinstance(tt, str):
        assert isinstance(tt, str) and tt, tt
        return [tt]
    assert isinstance(tt, (list, tuple)) and tt, tt
    assert all(isinstance(t, str) for t in tt), tt
    return [t for t in tt if t]


FLOATS = [107.7232452, 3.25, 9.1]
INTS = [1200, 3400, 56000]


@pytest.mark.parametrize("kind", sorted(HIER))
def test_textinfo_value_on_non_integer_values_gets_two_decimals(kind):
    fig = _fig(kind, FLOATS)
    fig.update_traces(textinfo="label+value+percent entry")
    hover_before = fig.data[0].hovertemplate
    plot_utils._plotly_to_html(fig)
    templates = _templates(kind, fig.data[0].texttemplate)
    valued = [t for t in templates if "%{value" in t]
    assert valued, templates
    for tt in templates:
        assert "%{value}" not in tt, tt
    for tt in valued:
        assert FLOAT_FMT in tt, tt
        assert "%{label}" in tt, tt
        assert "percentEntry" in tt, tt
    assert fig.data[0].hovertemplate == hover_before


def test_treemap_from_the_finding_never_shows_the_raw_float():
    fig = _fig("treemap", FLOATS)
    fig.update_traces(textinfo="label+value+percent entry")
    plot_utils._plotly_to_html(fig)
    tt = fig.data[0].texttemplate
    assert FLOAT_FMT in tt and "%{value}" not in tt


def test_bare_value_template_is_regrouped_on_sunburst():
    fig = _fig("sunburst", FLOATS)
    fig.update_traces(texttemplate="%{label}<br>%{value}")
    plot_utils._plotly_to_html(fig)
    assert fig.data[0].texttemplate == "%{label}<br>" + FLOAT_FMT


@pytest.mark.parametrize("kind", sorted(HIER))
def test_an_already_formatted_template_is_unchanged(kind):
    fig = _fig(kind, FLOATS)
    fig.update_traces(texttemplate="%{label}<br>%{value:.1f}")
    plot_utils._plotly_to_html(fig)
    for tt in _templates(kind, fig.data[0].texttemplate):
        assert tt == "%{label}<br>%{value:.1f}", tt


@pytest.mark.parametrize("kind", sorted(HIER))
def test_integer_values_get_no_decimals(kind):
    fig = _fig(kind, INTS)
    fig.update_traces(textinfo="label+value")
    plot_utils._plotly_to_html(fig)
    templates = _templates(kind, fig.data[0].texttemplate)
    valued = [t for t in templates if "%{value" in t]
    assert valued, templates
    for tt in templates:
        assert "%{value}" not in tt, tt
    for tt in valued:
        assert INT_FMT in tt, tt
        assert "%{label}" in tt, tt


@pytest.mark.parametrize("kind", sorted(HIER))
def test_textinfo_without_value_leaves_texttemplate_unset(kind):
    fig = _fig(kind, FLOATS)
    fig.update_traces(textinfo="label+percent entry")
    plot_utils._plotly_to_html(fig)
    assert fig.data[0].texttemplate is None
    assert fig.data[0].textinfo == "label+percent entry"


# ---------------------------------------------------------------------------
# No regression on the label rule's existing traces.
# ---------------------------------------------------------------------------
def test_pie_percent_only_keeps_no_texttemplate():
    fig = px.pie(pd.DataFrame({"c": ["a", "b"], "v": [1.5, 2.5]}), names="c", values="v")
    fig.update_traces(textinfo="percent")
    plot_utils._plotly_to_html(fig)
    assert fig.data[0].texttemplate is None
    assert fig.data[0].textinfo == "percent"


def test_bar_text_auto_still_gets_a_formatted_y_template():
    fig = px.bar(pd.DataFrame({"c": ["a", "b"], "v": [1234.5, 2000.25]}),
                 x="c", y="v", text_auto=True)
    plot_utils._plotly_to_html(fig)
    tt = fig.data[0].texttemplate
    assert isinstance(tt, str) and "%{y:" in tt, tt
    assert "%{y}" not in tt
