"""PROMPT 31 Part C — `text_chart_check.check_block`: does a number the answer
text gives match the number the chart shows (same statistic, same rows)?

The pure web-app module classifies every NUMERIC slot (int / float, finite)
the narrative template and the block's caption reference and the block's
VALUES set:

- `exempt`    kind pvalue / text / period / ratio; a name like p_value*,
              *pvalue*, effect*, *_all_rows*, n_excluded, n_dropped*,
              period*, latest_period, n_periods, *_pct_change*, change_pct;
              an integer value below 10 (chance matches);
- `match`     equal to a DRAWN number by DISPLAY equality
              (`pdc_diag.fmt(candidate, kind, lang) == pdc_diag.fmt(value,
              kind, lang)` — no tolerance constant); for box / violin /
              histogram the drawn numbers are each group's median, q1, q3,
              min, max and n;
- `derived`   equal to a trace total, a value / total share (compared with
              kind pct) or a group n;
- `other_stat` equal to a statistic the chart type computes but does not
              draw — the group MEAN of a box / violin / histogram — unless
              the trace draws the mean (`draws_mean`: box `boxmean`, violin
              `meanline.visible`), where the mean is a drawn number (`match`);
- `not_found` none of the above.

`first` is the class of the first numeric non-exempt slot of the FIRST
sentence (None when there is none). Never raises: any error →
`{"error": "<Type>"}`.

The chart_stats fixtures here are built by hand in the shape the sandbox
produces (`{"traces": [{"type", "values", "total", "groups", "draws_mean"}]}`),
so this file pins the classifier on its own; tests/test_chart_stats.py pins
the sandbox side and tests/test_text_chart_check_flow.py the wiring.

Readings chosen where the spec leaves room:
- the result's counts are read either flat (`res["match"]`) or under a
  `counts` dict; the slot names per class from `<class>_slots` (the log
  line's field names) or a `names` dict — `_counts` / `_names` below;
- `first` with no qualifying slot may be None or the string "none";
- a slot whose value is a string, None or non-finite is in NO class.

Offline: `.venv/Scripts/python.exe -m pytest tests/test_text_chart_check.py -q`.
"""
import importlib
import math

import pytest

CLASSES = ("match", "derived", "other_stat", "not_found", "exempt")


# ----------------------------------------------------------------- helpers

def _tcc():
    # Imported per test, so a missing module fails each test on its own
    # instead of erroring the whole file at collection.
    return importlib.import_module("text_chart_check")


def _check(slots, values, stats, lang="en"):
    return _tcc().check_block(template_slots=slots, block_values=values,
                              chart_stats=stats, lang=lang)


def _counts(res: dict) -> dict:
    assert isinstance(res, dict), res
    assert "error" not in res, res
    src = res.get("counts") if isinstance(res.get("counts"), dict) else res
    return {c: int(src.get(c, 0) or 0) for c in CLASSES}


def _names(res: dict, cls: str) -> list:
    if f"{cls}_slots" in res:
        return list(res[f"{cls}_slots"] or [])
    names = res.get("names") if isinstance(res.get("names"), dict) else {}
    return list(names.get(cls) or [])


def _first(res: dict):
    first = res.get("first")
    return None if first in (None, "none") else first


def _bar(values, total=None):
    vals = [float(v) for v in values]
    return {"type": "bar", "values": vals,
            "total": float(sum(vals)) if total is None else total,
            "groups": [], "draws_mean": False}


def _group(sample):
    """One group's stats from its raw sample, pandas' default linear
    quantile (method 7) — the samples below are built so the quartiles do
    not depend on the method."""
    import pandas as pd
    s = pd.Series(sorted(sample), dtype="float64")
    return {"n": int(len(s)), "mean": float(s.mean()), "median": float(s.median()),
            "q1": float(s.quantile(0.25)), "q3": float(s.quantile(0.75)),
            "min": float(s.min()), "max": float(s.max())}


def _dist(kind, samples, draws_mean=False):
    return {"type": kind, "values": [], "total": None,
            "groups": [_group(s) for s in samples], "draws_mean": draws_mean}


# Q2 of 22 Nov: px.box(guarded, x='segment', y='amount'). The guarded frame:
PREMIUM = [7311, 7311, 7311, 8423, 61977]      # mean 18,466.6 -> "18,467"
MASS = [523, 523, 641, 641, 9187]              # mean 2,303   -> "2,303"
MEAN_PREMIUM = sum(PREMIUM) / len(PREMIUM)
MEAN_MASS = sum(MASS) / len(MASS)


def _q2_stats(draws_mean=False):
    return {"traces": [_dist("box", [PREMIUM, MASS], draws_mean=draws_mean)]}


# Q7 of 22 Nov: a bar of order counts per channel; text "139,415" and "69.7%".
Q7_COUNTS = [139415, 45000, 15585]


def _q7_stats():
    return {"traces": [_bar(Q7_COUNTS)]}


# ===================================================================== Q7

def test_q7_bar_counts_and_share_match_and_derived():
    """Q7 is the wanted behaviour: the count is drawn (match), the share is
    value / total (derived), nothing is unexplained."""
    slots = [("online_orders", "count", True), ("online_share", "pct", True)]
    values = {"online_orders": 139415, "online_share": 139415 / 200000}
    res = _check(slots, values, _q7_stats())
    c = _counts(res)
    assert c["match"] == 1, res
    assert c["derived"] == 1, res
    assert c["not_found"] == 0 and c["other_stat"] == 0, res
    assert _first(res) == "match", res
    assert _names(res, "not_found") == [] and _names(res, "other_stat") == []


def test_q7_trace_total_is_derived():
    slots = [("orders_total", "count", True)]
    res = _check(slots, {"orders_total": 200000}, _q7_stats())
    assert _counts(res)["derived"] == 1, res
    assert _first(res) == "derived", res


# ===================================================================== Q2

def _q2_slots():
    return [
        ("mean_premium", "amount", True),
        ("mean_mass", "amount", True),
        ("n_excluded", "count", False),
        ("mean_all_rows_premium", "amount", False),
        ("mean_all_rows_mass", "amount", False),
        ("p_value", "pvalue", False),
        ("median_premium", "amount", False),
    ]


def _q2_values():
    return {"mean_premium": MEAN_PREMIUM, "mean_mass": MEAN_MASS,
            "n_excluded": 10380,
            "mean_all_rows_premium": 84619.0, "mean_all_rows_mass": 31919.17,
            "p_value": 0.0004, "median_premium": 7311.0}


def test_q2_box_means_are_other_stat_all_rows_and_pvalue_exempt():
    """The production failure: a box draws medians / quartiles, the text
    reported group MEANS (other_stat) and all-row means (exempt)."""
    res = _check(_q2_slots(), _q2_values(), _q2_stats())
    c = _counts(res)
    assert c["other_stat"] == 2, res
    assert sorted(_names(res, "other_stat")) == ["mean_mass", "mean_premium"], res
    # n_excluded, both *_all_rows*, p_value
    assert c["exempt"] == 4, res
    # the median of the plotted frame IS drawn
    assert c["match"] == 1, res
    assert c["not_found"] == 0, res
    assert _first(res) == "other_stat", res


def test_q2_mean_on_other_rows_is_not_found():
    slots = [("mean_recent", "amount", True)]
    res = _check(slots, {"mean_recent": 4321.5}, _q2_stats())
    c = _counts(res)
    assert c["not_found"] == 1, res
    assert _names(res, "not_found") == ["mean_recent"], res
    assert _first(res) == "not_found", res


@pytest.mark.parametrize("stat", ["median", "q1", "q3", "min", "max", "n"])
def test_box_drawn_numbers_match(stat):
    g = _group(PREMIUM)
    value = g[stat]
    kind = "count" if stat == "n" else "amount"
    # n = 5 is a small integer (exempt); use a group large enough to count
    if stat == "n":
        sample = list(range(100, 100 + 37))
        stats = {"traces": [_dist("box", [sample])]}
        value = 37
    else:
        stats = _q2_stats()
    res = _check([("x_" + stat, kind, True)], {"x_" + stat: value}, stats)
    c = _counts(res)
    # n is listed both as drawn (match) and as a group n (derived): either
    # explains the number; it must never be other_stat / not_found
    if stat == "n":
        assert c["match"] + c["derived"] == 1, res
    else:
        assert c["match"] == 1, res
    assert c["other_stat"] == 0 and c["not_found"] == 0, res


def test_boxmean_true_draws_the_mean_so_mean_slots_match():
    """The new planner rule: an average question with a box plot adds
    `fig.update_traces(boxmean=True)` — the mean is then on the chart."""
    res = _check(_q2_slots(), _q2_values(), _q2_stats(draws_mean=True))
    c = _counts(res)
    assert c["other_stat"] == 0, res
    assert c["match"] == 3, res          # two means + the median
    assert _first(res) == "match", res


def test_violin_meanline_draws_the_mean():
    stats = {"traces": [_dist("violin", [PREMIUM, MASS], draws_mean=True)]}
    res = _check([("mean_premium", "amount", True)], {"mean_premium": MEAN_PREMIUM}, stats)
    assert _counts(res)["match"] == 1, res
    stats_no = {"traces": [_dist("violin", [PREMIUM, MASS], draws_mean=False)]}
    res_no = _check([("mean_premium", "amount", True)], {"mean_premium": MEAN_PREMIUM}, stats_no)
    assert _counts(res_no)["other_stat"] == 1, res_no


def test_histogram_group_mean_is_other_stat():
    stats = {"traces": [_dist("histogram", [PREMIUM])]}
    res = _check([("avg_amount", "amount", True)], {"avg_amount": MEAN_PREMIUM}, stats)
    assert _counts(res)["other_stat"] == 1, res
    assert _names(res, "other_stat") == ["avg_amount"], res


# =========================================================== display equality

def test_display_equality_not_a_tolerance():
    # count kind rounds to whole numbers: 139,415.4 displays as 139,415
    res = _check([("n_online", "count", True)], {"n_online": 139415.4}, _q7_stats())
    assert _counts(res)["match"] == 1, res
    # one unit off displays differently -> not found
    res = _check([("n_online", "count", True)], {"n_online": 139416}, _q7_stats())
    assert _counts(res)["not_found"] == 1, res


def test_display_equality_follows_the_locale():
    # "ru" formats 18 466,6 with its own separators on both sides; the
    # comparison is display-to-display, so it still matches the drawn max
    g = _group(PREMIUM)
    res = _check([("top_amount", "amount", True)], {"top_amount": g["max"]},
                 _q2_stats(), lang="ru")
    assert _counts(res)["match"] == 1, res


def test_pct_kind_share_is_derived():
    # Not percent-like bars (a bar that draws 50 may be read as "50%" and
    # match a 0.5 pct slot): 500 of 1,000.
    stats = {"traces": [_bar([300, 500, 200])]}
    # 0.5004 displays "50.0%" exactly like 500 / 1000
    for share in (0.5, 0.5004):
        res = _check([("share_b", "pct", True)], {"share_b": share}, stats)
        assert _counts(res)["derived"] == 1, (share, res)
        assert _first(res) == "derived", res
    res = _check([("share_b", "pct", True)], {"share_b": 0.51}, stats)
    assert _counts(res)["not_found"] == 1, res


def test_pct_kind_drawn_share_matches():
    # a bar that DRAWS shares: the pct slot is a drawn number
    stats = {"traces": [_bar([0.3, 0.5, 0.2])]}
    res = _check([("share_b", "pct", True)], {"share_b": 0.5}, stats)
    assert _counts(res)["match"] == 1, res


def test_drawn_number_across_several_traces():
    stats = {"traces": [_bar([10, 20]), _bar([1234, 5678])]}
    res = _check([("b2", "count", True)], {"b2": 5678}, stats)
    assert _counts(res)["match"] == 1, res


# ================================================================== exempt

@pytest.mark.parametrize("kind", ["pvalue", "text", "period", "ratio"])
def test_exempt_kinds(kind):
    value = 0.0004 if kind == "pvalue" else 2.75
    res = _check([("val", kind, True)], {"val": value}, _q7_stats())
    c = _counts(res)
    assert c["exempt"] == 1, res
    assert c["not_found"] == 0, res
    assert _first(res) is None, res


@pytest.mark.parametrize("name", [
    "p_value", "p_value_segments", "seg_pvalue", "effect_size", "effect",
    "mean_all_rows_premium", "n_excluded", "n_dropped", "n_dropped_rows",
    "period_start", "latest_period", "n_periods", "sales_pct_change_q",
    "change_pct",
])
def test_exempt_names(name):
    res = _check([(name, "amount", True)], {name: 98765.4}, _q7_stats())
    c = _counts(res)
    assert c["exempt"] == 1, (name, res)
    assert c["not_found"] == 0, (name, res)


def test_small_integers_are_exempt_and_ten_is_not():
    slots = [("n_segments", "count", True), ("n_nine", "count", True),
             ("n_ten", "count", True)]
    res = _check(slots, {"n_segments": 3, "n_nine": 9, "n_ten": 10}, _q7_stats())
    c = _counts(res)
    assert c["exempt"] == 2, res
    assert c["not_found"] == 1, res
    assert _names(res, "not_found") == ["n_ten"], res
    assert _first(res) == "not_found", res


def test_first_is_none_when_the_first_sentence_has_only_exempt_slots():
    slots = [("p_value", "pvalue", True), ("online_orders", "count", False)]
    res = _check(slots, {"p_value": 0.01, "online_orders": 139415}, _q7_stats())
    assert _first(res) is None, res
    assert _counts(res)["match"] == 1, res


def test_first_is_the_first_qualifying_slot_in_order():
    slots = [("n_excluded", "count", True), ("mean_recent", "amount", True),
             ("online_orders", "count", True)]
    res = _check(slots, {"n_excluded": 10380, "mean_recent": 4321.5,
                         "online_orders": 139415}, _q7_stats())
    assert _first(res) == "not_found", res


def test_non_numeric_values_are_in_no_class():
    slots = [("top", "text", True), ("missing", "amount", True),
             ("nan_value", "amount", True), ("inf_value", "amount", True)]
    res = _check(slots, {"top": "North", "missing": None,
                         "nan_value": float("nan"), "inf_value": math.inf}, _q7_stats())
    assert sum(_counts(res).values()) == 0, res
    assert _first(res) is None, res


# ================================================================== errors

def test_never_raises_on_missing_or_odd_stats():
    for stats in (None, {}, {"traces": []}, {"traces": [{"type": "bar"}]}):
        res = _check([("online_orders", "count", True)], {"online_orders": 139415}, stats)
        assert isinstance(res, dict), (stats, res)


def test_error_path_returns_the_type_name():
    class Boom(list):
        def __iter__(self):
            raise RuntimeError("template slots exploded")

    res = _check(Boom([("x", "count", True)]), {"x": 139415}, _q7_stats())
    assert isinstance(res, dict), res
    assert res.get("error") == "RuntimeError", res
    # the type only — never the message
    assert "exploded" not in repr(res)


def test_result_names_are_template_identifiers_only():
    """Names are template identifiers (allowed on a log line); a value never
    appears in the result."""
    res = _check(_q2_slots(), _q2_values(), _q2_stats())
    text = repr(res)
    for shown in ("18,467", "18466", "2,303", "2303", "84,619", "31,919", "7,311", "7311"):
        assert shown not in text, (shown, res)
