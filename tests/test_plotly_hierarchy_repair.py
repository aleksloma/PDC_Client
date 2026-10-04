"""Hierarchical Plotly charts built from a categorical path column render
(BRB findings 2, item 1).

The regression: generated code bins a numeric column with `pd.cut` and passes
the resulting CATEGORICAL column in `path=` of `px.sunburst` / `px.treemap` /
`px.icicle`. Plotly Express then groups with pandas' `observed=False`
default, so the trace carries a zero-value node for EVERY combination of the
categories — including nodes whose parent id does not exist in `ids` (the
parent combination was never observed and was dropped). plotly.js refuses a
hierarchy with an orphan parent reference and draws NOTHING: the chart frame
was blank.

The fix lives in `plot_utils._plotly_to_html` (post-processing the figure in
place before it is exported). Pinned here:

* the raw px figure really is broken (an orphan exists) — the bug pattern;
* after `_plotly_to_html` no node references a parent id that is absent;
* the zero-value nodes that exist only because of unobserved category
  combinations are not drawn — the drawn node set is exactly the observed
  hierarchy, with the values/totals of an independent `groupby(observed=True)`
  on the source frame;
* "Show data" (`chart_data`) lists exactly the nodes the chart draws;
* charts that are not affected are left exactly as they are: a hierarchy
  built from plain string columns, and a valid hierarchy that legitimately
  contains a zero-value node under an existing parent.

Offline; real rendering through `plot_utils._render_in_process` (the function
the sandbox runner imports) and directly through `_plotly_to_html`.
"""
import copy
import json
import warnings
from collections import Counter

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pytest

import plot_utils

PATH = ["ps", "ct", "age_group"]
BINS = [0, 30, 45, 60, 120]
AGE_LABELS = ["<30", "30-44", "45-59", "60+"]

# Several (payment system, card type, age band) combinations are absent on
# purpose: Visa/Classic, UnionPay/Gold, UnionPay/Premium, Humo/Premium, and
# most age bands under every observed (ps, ct).
_ROWS = [
    ("Uzcard", "Classic", 25), ("Uzcard", "Classic", 33), ("Uzcard", "Gold", 50),
    ("Uzcard", "Premium", 70), ("Humo", "Classic", 22), ("Humo", "Classic", 28),
    ("Humo", "Gold", 40), ("Visa", "Gold", 35), ("Visa", "Premium", 62),
    ("Visa", "Premium", 65), ("Visa", "Premium", 48), ("UnionPay", "Classic", 19),
]

PX_FUNCS = {"sunburst": px.sunburst, "treemap": px.treemap, "icicle": px.icicle}


def _cards() -> pd.DataFrame:
    return pd.DataFrame(_ROWS, columns=["ps", "ct", "age"])


def _agg(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["age_group"] = pd.cut(d["age"], bins=BINS, labels=AGE_LABELS)
    return d.groupby(PATH, observed=True).size().reset_index(name="n")


def _expected_nodes(df: pd.DataFrame) -> dict:
    """{id: value} of the OBSERVED hierarchy, computed independently of
    plotly: leaves from groupby(observed=True), parents as sums."""
    agg = _agg(df)
    agg["age_group"] = agg["age_group"].astype(str)
    out = {}
    for depth in range(1, len(PATH) + 1):
        cols = PATH[:depth]
        g = agg.groupby(cols, observed=True)["n"].sum()
        for key, val in g.items():
            key = key if isinstance(key, tuple) else (key,)
            out["/".join(str(k) for k in key)] = int(val)
    return out


def _px_figure(kind: str):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return PX_FUNCS[kind](_agg(_cards()), path=PATH, values="n",
                              title="Cards by payment system, type and age")


def _orphans(ids, parents) -> list:
    known = set(ids)
    return [(i, p) for i, p in zip(ids, parents) if p and p not in known]


def _trace_nodes(trace) -> tuple[list, list, list, list]:
    return (list(trace["ids"] if isinstance(trace, dict) else trace.ids),
            list(trace["labels"] if isinstance(trace, dict) else trace.labels),
            list(trace["parents"] if isinstance(trace, dict) else trace.parents),
            list(trace["values"] if isinstance(trace, dict) else trace.values))


def _new_plot_traces(html: str) -> list:
    """The `data` argument of the `Plotly.newPlot(` call in the exported
    document — what plotly.js is actually asked to draw."""
    i = html.index("Plotly.newPlot(") + len("Plotly.newPlot(")
    dec = json.JSONDecoder()

    def skip(pos):
        while html[pos] in " \t\r\n,":
            pos += 1
        return pos

    pos = skip(i)
    _div_id, pos = dec.raw_decode(html, pos)
    pos = skip(pos)
    data, _ = dec.raw_decode(html, pos)
    return data


def _render_code(kind: str) -> str:
    return (
        "import pandas as pd\n"
        "import plotly.express as px\n"
        "d = dfs['cards.csv'].copy()\n"
        "d['age_group'] = pd.cut(d['age'], bins=[0, 30, 45, 60, 120],\n"
        "                        labels=['<30', '30-44', '45-59', '60+'])\n"
        "agg = d.groupby(['ps', 'ct', 'age_group'], observed=True).size()"
        ".reset_index(name='n')\n"
        f"fig = px.{kind}(agg, path=['ps', 'ct', 'age_group'], values='n',\n"
        "                title='Cards by payment system, type and age')\n"
    )


# ---------------------------------------------------------------------------
# The bug pattern (precondition) — proves the fixture reproduces the defect.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", sorted(PX_FUNCS))
def test_the_raw_px_figure_has_orphan_parent_references(kind):
    ids, _labels, parents, values = _trace_nodes(_px_figure(kind).data[0])
    assert _orphans(ids, parents), (
        "precondition: px must emit nodes under parent ids that do not exist "
        "for a categorical path column — otherwise this file tests nothing")
    assert any(v == 0 for v in values)


# ---------------------------------------------------------------------------
# Direct: _plotly_to_html repairs the figure in place.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", sorted(PX_FUNCS))
def test_plotly_to_html_leaves_no_orphan_parent(kind):
    fig = _px_figure(kind)
    plot_utils._plotly_to_html(fig)
    ids, _labels, parents, _values = _trace_nodes(fig.data[0])
    assert ids, "the repaired hierarchy must still draw nodes"
    assert _orphans(ids, parents) == []


@pytest.mark.parametrize("kind", sorted(PX_FUNCS))
def test_plotly_to_html_draws_exactly_the_observed_hierarchy(kind):
    fig = _px_figure(kind)
    plot_utils._plotly_to_html(fig)
    ids, labels, parents, values = _trace_nodes(fig.data[0])
    drawn = {i: v for i, v in zip(ids, values)}
    expected = _expected_nodes(_cards())
    # Unobserved category combinations (all zero) are not drawn; every
    # observed segment is, with its value and total unchanged.
    assert set(drawn) == set(expected), (
        sorted(set(drawn) ^ set(expected)))
    for node_id, val in expected.items():
        assert drawn[node_id] == pytest.approx(val), node_id
    assert all(v != 0 for v in values)
    assert len(ids) == len(labels) == len(parents) == len(values)
    # Each label is still the last path component of its id.
    for i, lab in zip(ids, labels):
        assert str(i).split("/")[-1] == str(lab)


# ---------------------------------------------------------------------------
# End to end: generated code through the sandbox's render function.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", sorted(PX_FUNCS))
def test_rendered_document_draws_every_non_empty_segment(kind):
    out = plot_utils._render_in_process(_render_code(kind), {"cards.csv": _cards()}, "t")
    assert out.get("ok"), out.get("error")
    assert out.get("is_plotly")
    traces = _new_plot_traces(out["plotly_html"])
    assert len(traces) == 1
    ids, _labels, parents, values = _trace_nodes(traces[0])
    assert _orphans(ids, parents) == [], "plotly.js draws nothing with an orphan parent"
    expected = _expected_nodes(_cards())
    drawn = dict(zip(ids, values))
    assert set(drawn) == set(expected)
    for node_id, val in expected.items():
        assert drawn[node_id] == pytest.approx(val), node_id


@pytest.mark.parametrize("kind", sorted(PX_FUNCS))
def test_show_data_lists_exactly_the_drawn_nodes(kind):
    out = plot_utils._render_in_process(_render_code(kind), {"cards.csv": _cards()}, "t")
    assert out.get("ok"), out.get("error")
    traces = _new_plot_traces(out["plotly_html"])
    _ids, labels, _parents, values = _trace_nodes(traces[0])
    cd = out.get("chart_data")
    assert cd and cd.get("columns") == ["Label", "Value"], cd and cd.get("columns")
    shown = Counter((str(r["Label"]), float(r["Value"])) for r in cd["rows"])
    drawn = Counter((str(lab), float(v)) for lab, v in zip(labels, values))
    assert shown == drawn
    assert cd["total_rows"] == len(labels)
    assert all(float(r["Value"]) != 0 for r in cd["rows"])


# ---------------------------------------------------------------------------
# Not affected: left exactly as today.
# ---------------------------------------------------------------------------
def _snapshot(trace) -> dict:
    return {k: copy.deepcopy(list(getattr(trace, k)) if getattr(trace, k) is not None else None)
            for k in ("ids", "labels", "parents", "values")}


@pytest.mark.parametrize("kind", sorted(PX_FUNCS))
def test_string_path_hierarchy_is_unchanged(kind):
    df = _cards()
    df["age_group"] = pd.cut(df["age"], bins=BINS, labels=AGE_LABELS).astype(str)
    agg = df.groupby(PATH).size().reset_index(name="n")
    assert all(agg[c].dtype == object for c in PATH)
    fig = PX_FUNCS[kind](agg, path=PATH, values="n")
    before = _snapshot(fig.data[0])
    assert _orphans(before["ids"], before["parents"]) == []
    plot_utils._plotly_to_html(fig)
    assert _snapshot(fig.data[0]) == before


@pytest.mark.parametrize("kind", sorted(PX_FUNCS))
def test_a_legitimate_zero_node_under_an_existing_parent_is_kept_px(kind):
    agg = pd.DataFrame({"a": ["x", "x", "y"], "b": ["p", "q", "r"], "v": [5, 0, 3]})
    fig = PX_FUNCS[kind](agg, path=["a", "b"], values="v")
    before = _snapshot(fig.data[0])
    assert "x/q" in before["ids"]
    plot_utils._plotly_to_html(fig)
    after = _snapshot(fig.data[0])
    assert after == before
    assert dict(zip(after["ids"], after["values"]))["x/q"] == 0


@pytest.mark.parametrize("trace_cls", [go.Sunburst, go.Treemap, go.Icicle])
def test_a_legitimate_zero_node_under_an_existing_parent_is_kept_go(trace_cls):
    fig = go.Figure(trace_cls(
        ids=["root", "root/a", "root/b", "root/a/a1", "root/a/a2"],
        labels=["root", "a", "b", "a1", "a2"],
        parents=["", "root", "root", "root/a", "root/a"],
        values=[10, 10, 0, 10, 0],
        branchvalues="total",
    ))
    before = _snapshot(fig.data[0])
    plot_utils._plotly_to_html(fig)
    assert _snapshot(fig.data[0]) == before


# ---------------------------------------------------------------------------
# Per-node arrays stay aligned with the surviving nodes (review follow-up).
# A trace can carry other arrays with one entry per node — hover/text
# templates, marker line widths, text colours. Dropping nodes from ids /
# labels / parents / values without dropping the same positions from those
# arrays would shift every later entry onto the wrong node.
# ---------------------------------------------------------------------------
def _attach_per_node_arrays(trace) -> None:
    n = len(trace.ids)
    trace.hovertemplate = [f"H{i}" for i in range(n)]
    trace.texttemplate = [f"T{i}" for i in range(n)]
    trace.marker.line.width = [i + 1 for i in range(n)]
    trace.textfont.color = [f"#{i + 1:06x}" for i in range(n)]


def _per_node_arrays(trace) -> dict:
    return {
        "hovertemplate": trace.hovertemplate,
        "texttemplate": trace.texttemplate,
        "marker.line.width": trace.marker.line.width,
        "textfont.color": trace.textfont.color,
    }


@pytest.mark.parametrize("kind", sorted(PX_FUNCS))
def test_per_node_arrays_stay_aligned_after_the_repair(kind):
    fig = _px_figure(kind)
    trace = fig.data[0]
    _attach_per_node_arrays(trace)
    ids_before = list(trace.ids)
    assert _orphans(ids_before, list(trace.parents)), "precondition: broken trace"
    owner = {name: dict(zip(ids_before, list(arr)))
             for name, arr in _per_node_arrays(trace).items()}

    plot_utils._plotly_to_html(fig)

    trace = fig.data[0]
    ids_after = list(trace.ids)
    assert ids_after and len(ids_after) < len(ids_before)
    assert _orphans(ids_after, list(trace.parents)) == []
    for name, arr in _per_node_arrays(trace).items():
        assert arr is not None and not isinstance(arr, str), (name, arr)
        arr = list(arr)
        assert len(arr) == len(ids_after), (name, len(arr), len(ids_after))
        for node_id, entry in zip(ids_after, arr):
            assert entry == owner[name][node_id], (name, node_id, entry)


@pytest.mark.parametrize("kind", sorted(PX_FUNCS))
def test_per_node_arrays_of_a_valid_trace_are_unchanged(kind):
    df = _cards()
    df["age_group"] = pd.cut(df["age"], bins=BINS, labels=AGE_LABELS).astype(str)
    agg = df.groupby(PATH).size().reset_index(name="n")
    fig = PX_FUNCS[kind](agg, path=PATH, values="n")
    _attach_per_node_arrays(fig.data[0])
    before = _snapshot(fig.data[0])
    arrays_before = {k: copy.deepcopy(list(v))
                     for k, v in _per_node_arrays(fig.data[0]).items()}
    plot_utils._plotly_to_html(fig)
    assert _snapshot(fig.data[0]) == before
    assert {k: list(v) for k, v in _per_node_arrays(fig.data[0]).items()} == arrays_before
