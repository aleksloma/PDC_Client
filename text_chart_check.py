"""Does a number the answer text gives match a number the chart shows?
(PROMPT 31 Part C — LOG ONLY.)

`check_block` classifies every numeric narrative / caption slot of one plot
block against that block's `chart_stats` (built in the sandbox by
`plot_utils._chart_stats`, decoded by `exec_transport.decode_chart_stats`):

- `exempt`     — a kind that is not a chart number (pvalue, text, period,
                 ratio), a name that by convention is not one (p-values,
                 effects, all-row figures, exclusion counts, periods,
                 percent changes), or an integer below 10 (chance matches);
- `match`      — a number the chart DRAWS: a bar / line / pie / funnel /
                 waterfall / hierarchy / heatmap value, or a box / violin /
                 histogram group's median, q1, q3, min, max or n (and its
                 mean when the trace draws it: box `boxmean`, violin
                 `meanline.visible`);
- `derived`    — a trace total, a share of it (value / total, compared as a
                 pct), or a group n;
- `other_stat` — a statistic the chart type computes but does not draw (a
                 box / violin / histogram group mean);
- `not_found`  — none of the above.

Equality is DISPLAY equality — `pdc_diag.fmt(candidate, kind, locale) ==
pdc_diag.fmt(value, kind, locale)` — never a tolerance constant. A cheap
numeric pre-filter (a strict superset of every display-equal candidate) keeps
the work bounded on a chart of 20 x 2000 numbers.

Pure: no I/O, no logging of its own, no state. The result holds counts,
class names and slot NAMES (template identifiers) — never a value. Main app
only (never in the executor image). Never raises (Article IV).
"""
from __future__ import annotations

import fnmatch
import math

import numpy as np

import narrative
import pdc_diag

CLASSES = ("match", "derived", "other_stat", "not_found", "exempt")
EXEMPT_KINDS = frozenset({"pvalue", "text", "period", "ratio"})
EXEMPT_NAME_PATTERNS = ("p_value*", "*pvalue*", "effect*", "*_all_rows*", "n_excluded",
                        "n_dropped*", "period*", "latest_period", "n_periods",
                        "*_pct_change*", "change_pct")
# An integral value below this is exempt: it matches some chart number by chance.
SMALL_INT_LIMIT = 10
# The slot names a class lists at most (the log line caps them again).
NAMES_MAX = 20
# The nearest candidates formatted per comparison at most (the display-equal
# ones are the nearest, so this bounds the work without changing a verdict
# unless 200 distinct chart numbers sit closer than the matching one).
_FORMAT_MAX = 200
_NA = "n/a"


def _number(value):
    """A finite int / float (never a bool) as itself, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except (OverflowError, TypeError, ValueError):
        return None


def _exempt(name: str, kind, value) -> bool:
    if kind in EXEMPT_KINDS:
        return True
    lowered = name.lower()
    if any(fnmatch.fnmatchcase(lowered, p) for p in EXEMPT_NAME_PATTERNS):
        return True
    return float(value).is_integer() and abs(value) < SMALL_INT_LIMIT


def _kind_of(kind, value) -> str:
    """The kind `pdc_diag.fmt` would apply to the slot VALUE — the candidate
    is formatted with the same one, so an int count and a float bar height
    compare on the same text."""
    if kind in pdc_diag.KINDS:
        return kind
    return "count" if isinstance(value, int) else "float"


class _Candidates:
    """The chart's numbers, by class, as float arrays."""

    def __init__(self, chart_stats: dict):
        drawn, totals, shares, group_n, means_drawn, means_other = [], [], [], [], [], []
        types = []
        for trace in chart_stats.get("traces") or []:
            if not isinstance(trace, dict):
                continue
            types.append(str(trace.get("type") or "other"))
            values = [v for v in (trace.get("values") or []) if _number(v) is not None]
            drawn.extend(values)
            total = _number(trace.get("total"))
            if total is not None:
                totals.append(total)
                if total != 0:
                    shares.extend(v / total for v in values)
            draws_mean = trace.get("draws_mean") is True
            for group in trace.get("groups") or []:
                if not isinstance(group, dict):
                    continue
                for key in ("median", "q1", "q3", "min", "max"):
                    number = _number(group.get(key))
                    if number is not None:
                        drawn.append(number)
                n = _number(group.get("n"))
                if n is not None:
                    group_n.append(n)
                mean = _number(group.get("mean"))
                if mean is not None:
                    (means_drawn if draws_mean else means_other).append(mean)
        as_array = lambda xs: np.asarray(xs, dtype=float)  # noqa: E731
        # a box / violin / histogram group n is a drawn number (the spec's
        # `match` list) and a derived one; `match` is checked first.
        self.match = as_array(drawn + group_n + means_drawn)
        self.totals = as_array(totals + group_n)
        self.shares = as_array(shares)
        self.other = as_array(means_other)
        self.types = types


def _display_equal(candidates, value, kind, locale) -> bool:
    """Whether any candidate prints like `value` (`pdc_diag.fmt`, same kind,
    same locale). Only candidates within 2 % + 1 of the value are formatted:
    every kind's rounding (count ±0.5, float ±0.005, pct ±0.0005, amount
    ±0.5 % of a mln / bln / trln figure) lies inside that band."""
    if not candidates.size:
        return False
    target = pdc_diag.fmt(value, kind, locale)
    if target == _NA:
        return False
    distance = np.abs(candidates - value)
    near = candidates[distance <= 0.02 * abs(value) + 1.0]
    if near.size > _FORMAT_MAX:
        near = near[np.argsort(np.abs(near - value), kind="stable")[:_FORMAT_MAX]]
    return any(pdc_diag.fmt(float(c), kind, locale) == target for c in near)


def _classify(value, kind, locale, cands: _Candidates) -> str:
    if _display_equal(cands.match, value, kind, locale):
        return "match"
    if (_display_equal(cands.totals, value, kind, locale)
            or _display_equal(cands.shares, value, "pct", locale)
            or (kind != "pct" and _display_equal(cands.shares * 100.0, value, kind, locale))):
        return "derived"
    if _display_equal(cands.other, value, kind, locale):
        return "other_stat"
    return "not_found"


def check_block(*, template_slots, block_values, chart_stats, lang) -> dict:
    """Classify the numeric slots of one plot block against its chart.

    `template_slots`: the ordered `(name, kind, in_first_sentence)` of the
    slots the narrative template and the block's caption show
    (`narrative.rendered_slots`) that this block's VALUES set;
    `block_values`: the block's VALUES; `chart_stats`: the decoded
    `chart_stats`; `lang`: the answer language code (its `pdc_diag.fmt`
    locale is `narrative.locale_for(lang)`).

    Returns `{"slots": <numeric slots>, "match", "derived", "other_stat",
    "not_found", "exempt": <counts>, "first": <class of the FIRST numeric
    non-exempt slot in the first sentence, or "none">, "chart": [trace
    types], "derived_slots", "other_stat_slots", "not_found_slots": [names]}`.
    Any failure -> `{"error": "<Type>"}`. Never raises."""
    try:
        if not isinstance(chart_stats, dict):
            raise TypeError("chart_stats is not a dict")
        values = block_values if isinstance(block_values, dict) else {}
        locale = narrative.locale_for(lang)
        cands = _Candidates(chart_stats)
        out = {"slots": 0, "first": "none", "chart": cands.types}
        for cls in CLASSES:
            out[cls] = 0
        names = {"derived": [], "other_stat": [], "not_found": []}
        seen: set = set()
        first_done = False
        for slot in template_slots or ():
            name, kind, in_first = slot
            if not isinstance(name, str) or name in seen or name not in values:
                continue
            seen.add(name)
            value = _number(values[name])
            if value is None:
                continue
            out["slots"] += 1
            if _exempt(name, kind, value):
                out["exempt"] += 1
                continue
            cls = _classify(value, _kind_of(kind, value), locale, cands)
            out[cls] += 1
            if cls in names and len(names[cls]) < NAMES_MAX:
                names[cls].append(name)
            if in_first and not first_done:
                out["first"] = cls
                first_done = True
        for cls, listed in names.items():
            out[f"{cls}_slots"] = listed
        return out
    except Exception as e:                                   # noqa: BLE001
        return {"error": type(e).__name__}
