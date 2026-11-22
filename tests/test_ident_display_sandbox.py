"""P31 Part D §3 — identifiers in the sandbox (styled tables + charts).

Generated Styler code applies `.format(thousands=',')` to every numeric
column and the chart formatters grouped any numeric axis whose title did not
end with one of an old word list — so a personal number printed as
"52,906,123,456" (and, on a linear Plotly axis, as "52.9B").

Pinned here (spec_partAD_client §3):
* `exec_transport._styled_html(styler)` re-formats the identifier columns of
  `styler.data` (decided by `ident_rule.identifier_columns(columns, job
  idents)`) with a plain formatter — `''` for NaN, `str(int(v))` for an
  integral number — on a COPY, overriding the generated `thousands=','` for
  those columns only; it never raises;
* the job idents are computed at job start in `code_exec._execute_in_process`
  and `plot_utils._render_in_process` from the job's INPUT frames, so a source
  identifier caught by the value rule (`client_since` years) is honoured too —
  driven here through the in-process functions and `serialize_result`, the
  same order the runner uses;
* `plot_utils._format_plotly_numeric`: an identifier axis gets no "," grouping
  and, on a CATEGORY axis, `type="category"` (or plain "d" ticks) when the
  figure set no axis type — a VALUE axis never becomes category (evaluator
  review, tests/test_ident_review_fixes.py); value labels of a
  trace whose value axis is an identifier are not grouped;
* `plot_utils._format_matplotlib_numeric`: an identifier-labelled axis is not
  grouped;
* `plot_utils._is_identifier_label` keeps its name and delegates to
  `ident_rule` (camelCase now recognised).

Offline: pure in-process calls, no sandbox container, no kaleido.
"""
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mtick  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import plotly.express as px  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import pytest  # noqa: E402

import code_exec  # noqa: E402
import exec_transport  # noqa: E402
import plot_utils  # noqa: E402

PN = [52906123456 + i * 1013 for i in range(30)]


def clients(n=30) -> pd.DataFrame:
    return pd.DataFrame({
        "personal_number": pd.Series([52906123456 + i * 1013 for i in range(n)], dtype="int64"),
        "client_since": [1995 + (i % 30) for i in range(n)],
        "loan_amount": [1234567.5 + i for i in range(n)],
        "name": [f"Client {i}" for i in range(n)],
    })


def _grouped(v: int) -> str:
    return f"{v:,}"


@pytest.fixture
def job_dir(tmp_path):
    return exec_transport.create_job_dir(tmp_path, exec_transport.new_job_id())


def _find_key(obj, key):
    """Every value stored under `key` anywhere in a nested dict/list."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            out.extend(_find_key(v, key))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_find_key(v, key))
    return out


# ---------------------------------------------------------------------------
# styled tables
# ---------------------------------------------------------------------------
def test_styled_html_shows_a_personal_number_as_plain_digits():
    df = clients()[["personal_number", "loan_amount"]]
    sty = df.style.format(thousands=",")
    html = exec_transport._styled_html(sty)
    assert isinstance(html, str) and html
    assert "52906123456" in html
    assert _grouped(52906123456) not in html
    # the measure keeps the generated grouping
    assert "1,234,567" in html


def test_styled_html_works_on_a_copy_and_leaves_the_styler_alone():
    df = clients()[["personal_number", "loan_amount"]]
    sty = df.style.format(thousands=",")
    exec_transport._styled_html(sty)
    assert _grouped(52906123456) in sty.to_html()


def test_styled_html_identifier_float_column_with_nan_has_no_decimals():
    pn = [float(v) for v in PN[:5]] + [np.nan]
    df = pd.DataFrame({"personal_number": pn, "amount": [1500.25] * 6})
    html = exec_transport._styled_html(df.style.format(thousands=","))
    assert "52906123456" in html
    assert "52906123456.0" not in html
    assert _grouped(52906123456) not in html
    assert ">nan<" not in html.lower()
    assert "1,500.25" in html


@pytest.mark.parametrize("label", ["clientID", "card_no", "Account #", "პირადი ნომერი"])
def test_styled_html_any_identifier_name_is_plain(label):
    df = pd.DataFrame({label: PN[:5], "amount": [1500.25] * 5})
    html = exec_transport._styled_html(df.style.format(thousands=","))
    assert "52906123456" in html
    assert _grouped(52906123456) not in html


def test_styled_html_without_identifiers_is_unchanged():
    df = pd.DataFrame({"amount": [1234567, 2345678], "rate": [0.5, 0.25]})
    sty = df.style.format(thousands=",").hide(axis="index")
    expected = sty.to_html()
    assert exec_transport._styled_html(sty) == expected


def test_styled_html_never_raises_on_a_styler_whose_data_is_odd():
    class _Fake:
        data = pd.DataFrame({"personal_number": PN[:3]})

        def to_html(self):
            return "<table><tr><td>x</td></tr></table>"

    out = exec_transport._styled_html(_Fake())
    assert out is None or isinstance(out, str)


def test_styled_result_through_the_transport_keeps_plain_identifiers(job_dir):
    df = clients()[["personal_number", "loan_amount"]]
    sty = df.style.format(thousands=",")
    payload = exec_transport.serialize_result(
        {"error": None, "result": sty, "preview": None, "image_base64": None}, job_dir)
    htmls = [h for h in _find_key(payload, "styled_html") if h]
    assert htmls, payload
    assert "52906123456" in htmls[0]
    assert _grouped(52906123456) not in htmls[0]


def test_job_source_identifier_by_value_is_plain_in_the_styled_table(job_dir):
    """`client_since` is not an identifier by NAME; its INPUT values (years)
    make it a source identifier for the job, so the styled table of a result
    holding it shows 2015, not 2,015."""
    code = ("RESULT = dfs['clients.csv'][['client_since', 'loan_amount']]"
            ".head(25).style.format(thousands=',')")
    out = code_exec._execute_in_process(code, {"clients.csv": clients()}, "t")
    assert not out.get("error"), out.get("error")
    payload = exec_transport.serialize_result(out, job_dir)
    htmls = [h for h in _find_key(payload, "styled_html") if h]
    assert htmls, payload
    html = htmls[0]
    assert "2015" in html
    assert "2,015" not in html and "1,995" not in html
    assert "1,234,567" in html


def test_job_idents_are_reset_between_jobs(job_dir, tmp_path):
    """A later job whose inputs hold no year column groups a column of the
    same NAME again — the per-job state does not leak."""
    code1 = ("RESULT = dfs['clients.csv'][['client_since']].head(25)"
             ".style.format(thousands=',')")
    code_exec._execute_in_process(code1, {"clients.csv": clients()}, "t")
    other = pd.DataFrame({"client_since": [1000 + i * 997 for i in range(25)],
                          "x": [1.5] * 25})
    code2 = "RESULT = dfs['o.csv'][['client_since']].style.format(thousands=',')"
    out = code_exec._execute_in_process(code2, {"o.csv": other}, "t")
    assert not out.get("error"), out.get("error")
    (tmp_path / "j2").mkdir()
    job2 = exec_transport.create_job_dir(tmp_path / "j2", exec_transport.new_job_id())
    payload = exec_transport.serialize_result(out, job2)
    html = [h for h in _find_key(payload, "styled_html") if h][0]
    assert "1,997" in html, html[:400]


# ---------------------------------------------------------------------------
# Plotly
# ---------------------------------------------------------------------------
def _axis(fig, name):
    return getattr(fig.layout, name)


def test_plotly_identifier_axis_is_not_grouped_and_is_categorical():
    df = clients(10)
    fig = px.bar(df, x="personal_number", y="loan_amount")
    plot_utils._format_plotly_numeric(fig)
    assert _axis(fig, "xaxis").tickformat != ","
    # (evaluator review) the CATEGORY axis of a bar: category, or linear with
    # plain-integer ticks ("d") — either shows the personal number unchanged
    assert _axis(fig, "xaxis").type == "category" or _axis(fig, "xaxis").tickformat == "d"
    # the measure axis still groups
    assert _axis(fig, "yaxis").tickformat == ","


@pytest.mark.parametrize("title", ["personalNumber", "clientID", "Client #", "card_no"])
def test_plotly_camelcase_and_new_words_are_identifier_axes(title):
    fig = go.Figure(go.Scatter(x=PN[:10], y=list(range(10))))
    fig.update_layout(xaxis_title=title, yaxis_title="loans")
    plot_utils._format_plotly_numeric(fig)
    assert _axis(fig, "xaxis").tickformat != ","


def test_plotly_identifier_axis_keeps_an_explicit_axis_type():
    fig = go.Figure(go.Scatter(x=PN[:10], y=list(range(10))))
    fig.update_layout(xaxis_title="personal_number", xaxis_type="linear")
    plot_utils._format_plotly_numeric(fig)
    assert _axis(fig, "xaxis").type == "linear"
    assert _axis(fig, "xaxis").tickformat != ","


def test_plotly_value_labels_of_an_identifier_value_axis_are_not_grouped():
    df = clients(5)
    fig = px.bar(df, x="name", y="personal_number", text_auto=True)
    plot_utils._format_plotly_numeric(fig)
    for tr in fig.data:
        assert "," not in (tr.texttemplate or ""), tr.texttemplate


def test_plotly_explicit_numeric_text_of_an_identifier_axis_is_not_grouped():
    fig = go.Figure(go.Bar(x=["a", "b", "c"], y=PN[:3], text=PN[:3]))
    fig.update_layout(yaxis_title="personal_number")
    plot_utils._format_plotly_numeric(fig)
    assert "," not in (fig.data[0].texttemplate or "")


def test_plotly_value_labels_of_a_measure_still_group():
    fig = go.Figure(go.Bar(x=["a", "b", "c"], y=[1234567, 2345678, 3456789],
                           text=[1234567, 2345678, 3456789]))
    fig.update_layout(yaxis_title="loan_amount")
    plot_utils._format_plotly_numeric(fig)
    assert fig.data[0].texttemplate == "%{y:,.0f}"


def _plotly_layout(html: str) -> dict:
    """The layout argument of the `Plotly.newPlot(id, data, layout, config)`
    call in a chart document."""
    start = html.index("Plotly.newPlot(") + len("Plotly.newPlot(")
    dec = json.JSONDecoder()
    pos = start
    parts = []
    for _ in range(3):
        while html[pos] in " \t\r\n,":
            pos += 1
        obj, pos = dec.raw_decode(html, pos)
        parts.append(obj)
    return parts[2]


def test_rendered_plotly_job_source_identifier_axis_is_not_grouped():
    """`client_since` is an identifier for the job by its INPUT values; the
    chart rendered from it does not group its axis."""
    code = "fig = px.scatter(dfs['clients.csv'], x='client_since', y='loan_amount')"
    out = plot_utils._render_in_process(code, {"clients.csv": clients()}, "t")
    assert out.get("ok") and out.get("is_plotly"), out.get("error")
    layout = _plotly_layout(out["plotly_html"])
    assert (layout.get("xaxis") or {}).get("tickformat") != ","
    assert (layout.get("yaxis") or {}).get("tickformat") == ","


def test_rendered_plotly_personal_number_axis_is_categorical():
    code = ("fig = px.bar(dfs['clients.csv'].head(5), x='personal_number', "
            "y='loan_amount')")
    out = plot_utils._render_in_process(code, {"clients.csv": clients()}, "t")
    assert out.get("ok") and out.get("is_plotly"), out.get("error")
    layout = _plotly_layout(out["plotly_html"])
    xaxis = layout.get("xaxis") or {}
    assert xaxis.get("tickformat") != ","
    assert xaxis.get("type") == "category" or xaxis.get("tickformat") == "d"


# ---------------------------------------------------------------------------
# matplotlib
# ---------------------------------------------------------------------------
def _fmt_text(axis, value):
    f = axis.get_major_formatter()
    try:
        return f(value, 0)
    except TypeError:
        return f(value)


def test_matplotlib_identifier_axis_is_not_grouped():
    fig, ax = plt.subplots()
    try:
        ax.scatter(PN[:10], [1234567 + i * 1000 for i in range(10)])
        ax.set_xlabel("personalNumber")
        ax.set_ylabel("loan_amount")
        plot_utils._format_matplotlib_numeric(fig)
        assert not (isinstance(ax.xaxis.get_major_formatter(), mtick.FuncFormatter)
                    and "," in _fmt_text(ax.xaxis, 52906123456.0))
        # the measure axis groups (positive control)
        assert isinstance(ax.yaxis.get_major_formatter(), mtick.FuncFormatter)
        assert _fmt_text(ax.yaxis, 1234567.0) == "1,234,567"
    finally:
        plt.close(fig)


# ---------------------------------------------------------------------------
# _is_identifier_label delegates to ident_rule
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label,expected", [
    ("personalNumber", True),
    ("personal_number", True),
    ("clientID", True),
    ("Client #", True),
    ("პირადი ნომერი", True),
    ("Лицевой счет", True),
    ("Order ID", True),
    ("Year", True),
    ("number_of_orders", False),
    ("Number of Orders", False),
    ("Order Value", False),
    ("ID Count", False),
    ("amount", False),
    ("", False),
    (None, False),
])
def test_is_identifier_label_delegates_to_ident_rule(label, expected):
    import ident_rule
    assert plot_utils._is_identifier_label(label) is expected
    if label:
        assert plot_utils._is_identifier_label(label) == ident_rule.is_identifier_name(label)
