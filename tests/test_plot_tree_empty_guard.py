"""Decision-tree plots are charts; empty figures are still rejected.

`sklearn.tree.plot_tree` draws a figure out of annotations only — no lines,
bars, patches-with-data or collections — so the empty-chart check used to
answer `EmptyChartError: Chart rendered with no data.` for a perfectly good
tree. The check must accept it while still rejecting a figure nothing was
drawn on (bare axes, or axes carrying only a title and axis labels).

Calls `plot_utils._render_in_process` directly — the function the sandbox
runner imports, where the check lives.
"""
import pandas as pd
import pytest

import plot_utils

EMPTY_PREFIX = "EmptyChartError: Chart rendered with no data."


@pytest.fixture(autouse=True)
def _close_figures():
    import matplotlib.pyplot as plt
    plt.close("all")
    yield
    plt.close("all")


def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "age":    [22, 25, 31, 35, 41, 46, 52, 58, 63, 67, 29, 44],
        "income": [18, 22, 40, 43, 55, 61, 38, 30, 27, 25, 35, 58],
        "bought": [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 1, 1],
    })


def _image(out: dict):
    return out.get("image") or out.get("image_base64")


TREE_CODE = (
    "import matplotlib.pyplot as plt\n"
    "from sklearn.tree import DecisionTreeClassifier, plot_tree\n"
    "df = dfs['t']\n"
    "X = df[['age', 'income']]\n"
    "y = df['bought']\n"
    "clf = DecisionTreeClassifier(max_depth=2, random_state=0)\n"
    "clf.fit(X, y)\n"
    "fig, ax = plt.subplots(figsize=(8, 5))\n"
    "plot_tree(clf, ax=ax, feature_names=['age', 'income'])\n"
)


def test_a_decision_tree_plot_renders_as_a_chart():
    pytest.importorskip("sklearn")
    out = plot_utils._render_in_process(TREE_CODE, {"t": _frame()}, "t")
    assert out.get("error") is None, out.get("error")
    assert _image(out), sorted(out)
    assert not out.get("is_plotly")


def test_a_figure_with_nothing_drawn_is_rejected():
    code = ("import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots()\n")
    out = plot_utils._render_in_process(code, {"t": _frame()}, "t")
    assert (out.get("error") or "").startswith(EMPTY_PREFIX), out.get("error")
    assert not _image(out)


def test_axes_with_only_a_title_and_labels_are_rejected():
    code = ("import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots()\n"
            "ax.set_title('Sales by region')\n"
            "ax.set_xlabel('Region')\n"
            "ax.set_ylabel('Sales')\n")
    out = plot_utils._render_in_process(code, {"t": _frame()}, "t")
    assert (out.get("error") or "").startswith(EMPTY_PREFIX), out.get("error")
    assert not _image(out)


def test_an_empty_chart_with_a_stray_annotation_is_rejected():
    code = ("import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots()\n"
            "ax.bar([], [])\n"
            "ax.annotate('n=0', (0.5, 0.5))\n")
    out = plot_utils._render_in_process(code, {"t": _frame()}, "t")
    assert (out.get("error") or "").startswith(EMPTY_PREFIX), out.get("error")
    assert not _image(out)


def test_an_empty_axes_with_only_a_text_note_is_rejected():
    code = ("import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots()\n"
            "ax.text(0.5, 0.5, 'No data available')\n")
    out = plot_utils._render_in_process(code, {"t": _frame()}, "t")
    assert (out.get("error") or "").startswith(EMPTY_PREFIX), out.get("error")
    assert not _image(out)


def test_a_filled_decision_tree_plot_renders_as_a_chart():
    pytest.importorskip("sklearn")
    code = TREE_CODE.replace(
        "plot_tree(clf, ax=ax, feature_names=['age', 'income'])",
        "plot_tree(clf, ax=ax, filled=True)")
    assert "filled=True" in code
    out = plot_utils._render_in_process(code, {"t": _frame()}, "t")
    assert out.get("error") is None, out.get("error")
    assert _image(out), sorted(out)
    assert not out.get("is_plotly")


def test_an_ordinary_bar_chart_still_renders():
    code = ("import matplotlib.pyplot as plt\n"
            "df = dfs['t']\n"
            "fig, ax = plt.subplots()\n"
            "ax.bar(df['age'].astype(str), df['income'])\n"
            "ax.set_title('Income by age')\n")
    out = plot_utils._render_in_process(code, {"t": _frame()}, "t")
    assert out.get("error") is None, out.get("error")
    assert _image(out), sorted(out)
    assert not out.get("is_plotly")
