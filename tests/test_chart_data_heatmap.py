"""Heatmap chart_data extraction (Prompt 14).

A px.imshow / go.Heatmap figure is ONE trace whose numbers live in `z`; the
extractor only read `x`/`y`, so the rendered data carried labels and no values
at all — "Show data" on a heatmap tile showed empty cells, and the
deterministic result check could not see a heatmap's metric. The whole verified
wrong-grain failure was rendered as a heatmap, so this gap mattered.

Real rendering (no stubs): the code runs through render_plot_safe.
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plot_utils
import result_backstop


@pytest.fixture
def dfs():
    return {"m.csv": pd.DataFrame({
        "city": ["Tbilisi", "Tbilisi", "Batumi", "Batumi"],
        "product": ["Milk", "Cheese", "Milk", "Cheese"],
        "n": [1, 1, 2, 2],
    })}


HEATMAP_CODE = """
import plotly.express as px
piv = dfs['m.csv'].pivot_table(index='city', columns='product', values='n', fill_value=0)
fig = px.imshow(piv, text_auto=True)
"""


def test_heatmap_chart_data_carries_z_values(dfs):
    out = plot_utils.render_plot_safe(HEATMAP_CODE, dfs, "t")
    assert out.get("ok")
    cd = out.get("chart_data")
    rows = cd["rows"]
    assert rows, "heatmap produced no rows"
    # every row is (x label, y label, value) and the values are real numbers
    numeric = [v for r in rows for v in r.values()
               if isinstance(v, (int, float)) and not isinstance(v, bool)]
    assert len(numeric) == len(rows), f"missing z values: {rows[:4]}"
    assert sorted(numeric) == [1, 1, 2, 2]
    assert len(cd["columns"]) == 3


def test_backstop_sees_identical_columns_in_a_heatmap(dfs):
    # Tbilisi=1/1, Batumi=2/2 -> every product repeats the same numbers, which
    # is the catalog-grain signature the answer must explain.
    out = plot_utils.render_plot_safe(HEATMAP_CODE, dfs, "t")
    det = result_backstop.inspect_outputs(chart_data=out.get("chart_data"), sid="t")
    assert det and det["kind"] == "identical_series"


def test_varying_heatmap_is_not_flagged(dfs):
    dfs["m.csv"].loc[1, "n"] = 8      # Cheese in Tbilisi differs now
    out = plot_utils.render_plot_safe(HEATMAP_CODE, dfs, "t")
    assert result_backstop.inspect_outputs(chart_data=out.get("chart_data"), sid="t") is None


def test_bar_chart_extraction_unchanged(dfs):
    out = plot_utils.render_plot_safe(
        "import plotly.express as px\n"
        "fig = px.bar(dfs['m.csv'], x='city', y='n', color='product', barmode='group')",
        dfs, "t")
    cd = out.get("chart_data")
    assert cd and cd["rows"]
    # multi-trace charts keep the Series column (the old shape)
    assert "Series" in cd["rows"][0]
