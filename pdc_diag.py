"""pdc_diag — the diagnostics library generated code calls inside the sandbox
(docs/DIAGNOSTICS_SPEC.md §A4 and §B8, decisions D5 / D12).

Registered as `pdc_diag` at BOTH exec sites (`code_exec._execute_in_process`
and `plot_utils.GLOBAL_PLOT_SCOPE`). It is a LEAF module: the module-level
imports are stdlib + numpy + pandas only; plotly is imported inside the three
plot helpers, sklearn inside `attribute_level(method="forest")`, shap inside
`method="shap"` (not a dependency — decision F7 keeps the fallback chain
shap -> forest -> variance).

It runs in the sandbox, so it never prints and never logs.

Every analysis function returns `{"table": DataFrame, "values": dict,
"findings": dict}` — the result, its VALUES and its FINDINGS are `_ResultDict`
(a dict subclass): a missing key raises `KeyError` naming the function, the
part and every key pdc_diag set when it built the dict (key names only, never
a value; a key the code adds later is never listed); `.get`, `in` and
everything else are plain dict:
- VALUES are plain Python scalars (str / int / float / bool / None) — the
  numbers and labels a narrative template may quote. They stay on the client.
- FINDINGS are a bool, a `FINDINGS_ENUM` code or one of the CALLER'S column
  names — never a number, never a category value. A boolean finding is
  emitted only when True (except `latest_is_current_period`, emitted both ways
  when it can be computed); an enum finding only when its condition holds.

A missing column or a non-numeric metric raises `ValueError` with a fixed
sentence naming the parameter and the column (schema-level information that
helps a retry). Degenerate data (an empty frame, one period) never raises: it
returns empty tables and findings such as `too_few_periods`.
"""
from __future__ import annotations

import math
import re
import warnings

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# B8.1 constants
# ---------------------------------------------------------------------------
PDC_DIAG_VERSION = 1

FINDINGS_ENUM = ("coverage_change", "coverage_stable", "partial_period", "metric_definition_overlap",
                 "mixed_units", "concentrated_in_one_dim", "spread_evenly", "weak_signal",
                 "dominant_feature", "anomaly_confirmed", "no_anomaly",
                 "data_gap", "outlier_driven", "seasonal_pattern")
# needs_phase2 is NOT a value (decision F14); it is only the reserved boolean KEY.

PDC_DIAG_FINDING_KEYS = frozenset({
    "coverage", "coverage_dim", "thin_period", "period_status", "unknown_dominated", "unknown_dim",
    "units", "metric_gap", "period_order_unknown", "latest_is_current_period", "latest_is_past_period",
    "anomaly", "anomaly_up", "anomaly_is_latest", "multiple_anomalies", "too_few_periods",
    "change_shape", "change_dim", "change_up", "change_large",
    "attribution", "top_feature", "second_feature", "method_fallback",
    "metric_relation", "metrics_identical"})

UNKNOWN_TOKENS = frozenset({"", "unknown", "n/a", "na", "none", "null", "nan", "-", "?",
                            "не указано", "неизвестно", "неизвестный", "номаълум", "noma'lum",
                            "nomaʼlum", "nomalum", "უცნობი"})


def _month_table() -> dict:
    names = {
        # English, full + 3-letter (+ "sept")
        "en": ["january", "february", "march", "april", "may", "june", "july", "august",
               "september", "october", "november", "december"],
        "en3": ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        # Russian, nominative + genitive
        "ru": ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
               "сентябрь", "октябрь", "ноябрь", "декабрь"],
        "ru_gen": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                   "сентября", "октября", "ноября", "декабря"],
        # Uzbek, Latin + Cyrillic
        "uz": ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust",
               "sentabr", "oktabr", "noyabr", "dekabr"],
        "uz_alt": ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust",
                   "sentyabr", "oktyabr", "noyabr", "dekabr"],
        "uz_cyr": ["январ", "феврал", "март", "апрел", "май", "июн", "июл", "август",
                   "сентябр", "октябр", "ноябр", "декабр"],
        # Georgian
        "ka": ["იანვარი", "თებერვალი", "მარტი", "აპრილი", "მაისი", "ივნისი", "ივლისი",
               "აგვისტო", "სექტემბერი", "ოქტომბერი", "ნოემბერი", "დეკემბერი"],
    }
    table = {"sept": 9}
    for months in names.values():
        for index, name in enumerate(months, start=1):
            table[name.casefold()] = index
    return table


# casefolded month names -> 1..12
_MONTHS = _month_table()

_PREFIX_RE = re.compile(r"([a-z][a-z0-9]{0,9}_)?")
_AGGS = ("sum", "mean", "median", "count", "nunique")
_NUMERIC_AGGS = ("sum", "mean", "median")
_LABEL_MAX = 200

_PALETTE = {"base": "#4C78A8", "highlight": "#E45756", "up": "#54A24B", "down": "#E45756",
            "total": "#4C78A8"}

# ---------------------------------------------------------------------------
# A4 fmt
# ---------------------------------------------------------------------------
KINDS = ("count", "float", "pct", "ratio", "amount", "text", "period")
_NUMERIC_KINDS = ("count", "float", "pct", "ratio", "amount")
# uz / ru (decision F20): a NO-BREAK space (U+00A0) groups thousands, a comma
# is the decimal separator, the magnitude words are the language's own.
_LOCALES = {"default": {"thousands": ",", "decimal": ".", "mln": "mln", "bln": "bln",
                        "trln": "trln"},
            "uz": {"thousands": " ", "decimal": ",", "mln": "mln", "bln": "mlrd",
                   "trln": "trln"},
            "ru": {"thousands": " ", "decimal": ",", "mln": "млн",
                   "bln": "млрд", "trln": "трлн"}}
FMT_MAX_LEN = 32
# A period holding fewer than THIN_PERIOD_RATIO x the median period's rows is
# thin (coverage_check `ratio_low`) and, for detect_anomalies, a data gap.
THIN_PERIOD_RATIO = 0.5
# A finite value >= 0 below COLLAPSE_RATIO x every existing neighbour (each
# finite and > 0) is a collapsed period: detect_anomalies treats it as a data
# gap ONLY when the frame holds one row per period (pre-aggregated input,
# where the row-count rule cannot fire). On raw rows the row count is the gap
# signal, and a collapsed value with a normal row count is a business change.
COLLAPSE_RATIO = 0.01
# `ratio` edges: 0 <= v < RATIO_FLOOR -> "<0.1x", v > RATIO_CEILING -> ">1000x".
RATIO_FLOOR = 0.05
RATIO_CEILING = 1000
_TEXT_MAX = 200
_NA = "n/a"


def fmt(value, kind: str | None = None, locale: str | None = None) -> str:
    """Format one slot value for a narrative template. Never raises."""
    try:
        return _fmt(value, kind, locale)
    except Exception:
        return _NA


def _fmt(value, kind, locale) -> str:
    if value is None:                                           # rule 1
        return _NA
    if isinstance(value, np.generic):                           # rule 2
        value = value.item()
        if value is None:
            return _NA
    if isinstance(value, bool):                                 # rule 3
        return "true" if value else "false"
    is_number = isinstance(value, (int, float))
    if kind not in KINDS:                                       # rule 4
        if isinstance(value, int):
            kind = "count"
        elif isinstance(value, float):
            kind = "float"
        else:
            kind = "text"
    if kind in ("text", "period"):                              # rule 5
        if isinstance(value, str):
            return value[:_TEXT_MAX]
        if is_number:
            kind = "count" if isinstance(value, int) else "float"
        else:
            return _NA
    if not is_number:                                           # rule 6
        if isinstance(value, str):
            return value[:_TEXT_MAX]
        return _NA
    if isinstance(value, float) and not math.isfinite(value):   # rule 7
        return _NA
    number, unit = _format_number(value, kind)                  # rule 8
    if number.startswith("-") and not any(ch in "123456789" for ch in number):
        number = number[1:]                                     # rule 9
    out = _localize(number, unit, locale)                       # rule 10
    if len(out) > FMT_MAX_LEN:                                  # rule 11
        return _NA
    return out


def _format_count(value) -> str:
    if isinstance(value, int):
        return format(value, ",")
    return format(value, ",.0f")


def _format_number(value, kind):
    """`(number text in the default locale, unit)` — unit is "", "%", "x" or
    one of the magnitude words' locale keys ("mln" / "bln" / "trln")."""
    if kind == "count":
        return _format_count(value), ""
    if kind == "float":
        return format(value, ",.2f"), ""
    if kind == "pct":
        return format(value * 100, ",.1f"), "%"
    if kind == "ratio":
        # Edges: a ratio that would print "0.0x" or a long run of digits
        # says what it means instead (A4 rule 8). Negatives keep ",.1f".
        if 0 <= value < RATIO_FLOOR:
            return "<0.1", "x"
        if value > RATIO_CEILING:
            return ">1000", "x"
        return format(value, ",.1f"), "x"
    # amount
    magnitude = abs(value)
    if magnitude >= 1e12:
        return format(value / 1e12, ",.2f"), "trln"
    if magnitude >= 1e9:
        return format(value / 1e9, ",.2f"), "bln"
    if magnitude >= 1e6:
        return format(value / 1e6, ",.2f"), "mln"
    return _format_count(value), ""


def _localize(number: str, unit: str, locale) -> str:
    entry = _LOCALES.get(locale) if isinstance(locale, str) else None
    if not isinstance(entry, dict):
        entry = _LOCALES["default"]
    if entry is not _LOCALES["default"]:
        number = (number.replace(",", "\0")
                  .replace(".", str(entry.get("decimal", ".")))
                  .replace("\0", str(entry.get("thousands", ","))))
    if unit in ("mln", "bln", "trln"):
        return number + " " + str(entry.get(unit, unit))
    return number + unit


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _check_prefix(prefix) -> str:
    if not isinstance(prefix, str) or not _PREFIX_RE.fullmatch(prefix):
        # No quoted literal here: the client's retry scrub turns every quoted
        # literal the failed code does not contain into '<value>', which hid
        # the trailing-underscore rule from the planner.
        raise ValueError("prefix: must be empty, or 1 to 10 lowercase letters or digits "
                         "starting with a letter and then exactly one underscore at the "
                         "end, for example ovl_; an underscore inside the prefix is not "
                         "allowed")
    return prefix


def _require_columns(df, columns, param: str) -> None:
    if not isinstance(df, pd.DataFrame):
        raise ValueError("df: must be a pandas DataFrame")
    for column in columns:
        if column not in df.columns:
            raise ValueError(f"{param}: column {column!r} not found in the frame")


def _as_list(value, param: str) -> list:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        return [value]
    return list(value)


def _require_numeric(df, column, param: str) -> None:
    series = df[column]
    if pd.api.types.is_numeric_dtype(series):
        return
    present = series.dropna()
    if len(present) == 0:
        return
    if pd.to_numeric(present, errors="coerce").isna().any():
        raise ValueError(f"{param}: column {column!r} is not numeric")


def _numeric(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.astype(float)
    return pd.to_numeric(series, errors="coerce").astype(float)


def _plain(value):
    """A plain Python scalar for VALUES: numpy -> Python, NaN/inf -> None,
    a Timestamp / anything else -> its label text."""
    try:
        if value is None:
            return None
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float):
            return float(value) if math.isfinite(value) else None
        if isinstance(value, str):
            return value[:_LABEL_MAX]
        return _label_str(value)
    except Exception:
        return None


def _label_str(value):
    """A period / member label as text (at most 200 chars), or None."""
    try:
        if value is None:
            return None
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, (pd.Timestamp, np.datetime64)) or hasattr(value, "strftime"):
            stamp = pd.Timestamp(value)
            if pd.isna(stamp):
                return None
            if stamp == stamp.normalize():
                return stamp.strftime("%Y-%m-%d")
            return stamp.isoformat()
        return str(value)[:_LABEL_MAX]
    except Exception:
        return None


def _num(value):
    """A finite Python float, or None."""
    try:
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


def _int_or_float(value):
    number = _num(value)
    if number is None:
        return None
    return int(number) if number.is_integer() else number


def _prefixed(values: dict, prefix: str) -> dict:
    return {prefix + key: _plain(value) for key, value in values.items()}


_KEY_REPR_MAX = 80


class _ResultDict(dict):
    """A pdc_diag result dict (the result, its VALUES, its FINDINGS). A
    missing key raises a `KeyError` naming the available keys — key names
    only, never a value — so a retry reads the right name instead of
    guessing another. The names listed are the ones pdc_diag set when it
    built the dict (`_names`): a key the generated code adds later may be
    data-derived (`"total_" + currency`), so it is never listed. `.get`,
    `in`, `pop`, iteration, `dict(...)`, deepcopy and pickle are plain
    dict (`_what` / `_names` live in `__dict__`, which both restore)."""

    _what = "pdc_diag result"
    _names: tuple = ()

    def __init__(self, data=(), what=None):
        super().__init__(data)
        if what is not None:
            self._what = what
        self._names = tuple(sorted(str(k) for k in self))

    def __missing__(self, key):
        listed = ", ".join(self._names) or "none"
        raise KeyError(f"{repr(key)[:_KEY_REPR_MAX]} is not a key of {self._what}; "
                       f"available keys: {listed}")


def _result(table: pd.DataFrame, values: dict, findings: dict, prefix: str, fn: str) -> dict:
    return _ResultDict({"table": table,
                        "values": _ResultDict(_prefixed(values, prefix), f"{fn} values"),
                        "findings": _ResultDict(findings, f"{fn} findings")},
                       f"{fn} result")


def _agg_series(series: pd.Series, agg: str):
    if agg == "count":
        return float(series.count())
    if agg == "nunique":
        return float(series.nunique())
    numeric = _numeric(series)
    if agg == "sum":
        return float(numeric.sum())
    if agg == "mean":
        return float(numeric.mean()) if numeric.notna().any() else float("nan")
    return float(numeric.median()) if numeric.notna().any() else float("nan")


def _check_agg(agg) -> str:
    if agg not in _AGGS:
        raise ValueError("agg: must be one of sum, mean, median, count, nunique")
    return agg


# ---------------------------------------------------------------------------
# B8.2 period ordering
# ---------------------------------------------------------------------------
_ISO_DAY_RE = re.compile(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?")
_YEAR_SLASH_RE = re.compile(r"(\d{4})/(\d{1,2})")
_MONTH_YEAR_RE = re.compile(r"(\d{1,2})[./](\d{4})")
_QUARTER_A_RE = re.compile(r"(\d{4})\s*[-/ ]?\s*[Qq]([1-4])")
_QUARTER_B_RE = re.compile(r"[Qq]([1-4])\s*[-/ ]?\s*(\d{4})")
_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+")


def _stamp(year: int, month: int, day: int = 1):
    try:
        return pd.Timestamp(year=int(year), month=int(month), day=int(day))
    except Exception:
        return None


def _parse_period_label(label):
    """A period label -> `pd.Timestamp`, or None when it does not parse.
    Never raises."""
    try:
        if label is None:
            return None
        if isinstance(label, pd.Timestamp):
            return None if pd.isna(label) else label
        if isinstance(label, (np.datetime64,)) or hasattr(label, "strftime"):
            stamp = pd.Timestamp(label)
            return None if pd.isna(stamp) else stamp
        text = str(label).strip()
        if not text:
            return None
        match = _ISO_DAY_RE.fullmatch(text)
        if match:
            return _stamp(match.group(1), match.group(2), match.group(3) or 1)
        match = _YEAR_SLASH_RE.fullmatch(text)
        if match:
            return _stamp(match.group(1), match.group(2))
        match = _MONTH_YEAR_RE.fullmatch(text)
        if match:
            return _stamp(match.group(2), match.group(1))
        tokens = _TOKEN_RE.findall(text)
        years = [t for t in tokens if t.isdigit() and len(t) == 4]
        months = [_MONTHS[t.casefold()] for t in tokens
                  if not t.isdigit() and t.casefold() in _MONTHS]
        other_digits = [t for t in tokens if t.isdigit() and len(t) != 4]
        if len(years) == 1 and len(months) == 1 and not other_digits:
            return _stamp(years[0], months[0])
        match = _QUARTER_A_RE.fullmatch(text)
        if match:
            return _stamp(match.group(1), 3 * (int(match.group(2)) - 1) + 1)
        match = _QUARTER_B_RE.fullmatch(text)
        if match:
            return _stamp(match.group(2), 3 * (int(match.group(1)) - 1) + 1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            stamp = pd.to_datetime(text, errors="coerce")
        if stamp is None or pd.isna(stamp):
            return None
        return pd.Timestamp(stamp)
    except Exception:
        return None


def _naive(stamp):
    try:
        if stamp is not None and stamp.tzinfo is not None:
            return stamp.tz_localize(None)
    except Exception:
        pass
    return stamp


def _first_appearance(series: pd.Series) -> list:
    seen = []
    known = set()
    for value in series.tolist():
        key = value
        try:
            hash(key)
        except TypeError:
            key = repr(value)
        if key in known:
            continue
        known.add(key)
        seen.append(value)
    return seen


def _order_with_stamps(series: pd.Series, order=None):
    """`(labels, order_known, stamps | None)`; `stamps` holds one Timestamp
    per label when every label is a date (parsed or datetime64)."""
    present = series.dropna()
    if order is not None:
        present_list = _first_appearance(present)
        present_set = set(present_list)
        in_order = []
        for label in list(order):
            if label in present_set and label not in in_order:
                in_order.append(label)
        rest = [label for label in present_list if label not in set(in_order)]
        labels = in_order + rest
        stamps = [_parse_period_label(label) for label in labels]
        return labels, not rest, (stamps if labels and all(s is not None for s in stamps) else None)
    if len(present) == 0:
        return [], True, None
    if pd.api.types.is_datetime64_any_dtype(present):
        labels = list(pd.DatetimeIndex(pd.unique(present)).sort_values())
        return labels, True, list(labels)
    if pd.api.types.is_numeric_dtype(present):
        uniques = [v.item() if isinstance(v, np.generic) else v for v in pd.unique(present)]
        return sorted(uniques), True, None
    labels = _first_appearance(present)
    stamps = [_parse_period_label(label) for label in labels]
    if all(s is not None for s in stamps):
        naive = [_naive(s) for s in stamps]
        order_idx = sorted(range(len(labels)), key=lambda i: (naive[i], i))
        return [labels[i] for i in order_idx], True, [naive[i] for i in order_idx]
    return labels, False, None


def _order_periods(series, order=None):
    """`(ordered labels, order_known)` — B8.2."""
    labels, known, _stamps = _order_with_stamps(pd.Series(series), order)
    return labels, known


def _period_frames(df: pd.DataFrame, period_col, labels: list) -> list:
    """One sub-frame per ordered label."""
    groups = {}
    try:
        for key, sub in df.groupby(period_col, sort=False, dropna=True):
            groups[key] = sub
    except Exception:
        groups = {}
    out = []
    for label in labels:
        sub = groups.get(label)
        if sub is None:
            sub = df[df[period_col] == label]
        out.append(sub)
    return out


def _period_values(df, period_col, metric, labels, agg) -> list:
    frames = _period_frames(df, period_col, labels)
    return [_agg_series(sub[metric], agg) for sub in frames]


# ---------------------------------------------------------------------------
# B8.3 coverage_check
# ---------------------------------------------------------------------------
def _jaccard(a: set, b: set) -> float:
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def _member_shares(series: pd.Series) -> dict:
    """{member: share of the non-null rows} for one period's values."""
    try:
        clean = series.dropna()
        if not len(clean):
            return {}
        return {k: float(v) for k, v in clean.value_counts(normalize=True, dropna=True).items()}
    except Exception:
        return {}      # unhashable members: this rule cannot judge the period


def _member_share_shift(frames: list, dim, share_shift: float, vanish_share: float) -> bool:
    """True when, between two consecutive periods that both hold a non-null
    member of `dim`, some member's row share moves by more than `share_shift`
    or a member above `vanish_share` of the earlier period disappears."""
    previous: dict = {}
    for sub in frames:
        shares = _member_shares(sub[dim])
        if previous and shares:
            for member in set(previous) | set(shares):
                before = previous.get(member, 0.0)
                after = shares.get(member, 0.0)
                if abs(after - before) > share_shift:
                    return True
                if before > vanish_share and after == 0.0:
                    return True
        previous = shares
    return False


def _unknown_mask(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip().str.casefold()
    return series.isna() | text.isin(UNKNOWN_TOKENS)


def coverage_check(df, period_col: str, dims: list, metrics: list, *, order=None,
                   ratio_low: float = THIN_PERIOD_RATIO, ratio_high: float = 2.0,
                   jaccard_min: float = 0.5,
                   share_shift: float = 0.5, vanish_share: float = 0.2,
                   unknown_share: float = 0.5, unit_cols: list | None = None,
                   as_of=None, prefix: str = "") -> dict:
    """Did the population behind the periods change (members, rows, nulls,
    units)? — B8.3.

    A coverage dim (each of `dims` and `unit_cols`) counts as CHANGED when
    any of three rules fires: its member-set size swings outside
    [`ratio_low`, `ratio_high`] × the median size; the Jaccard overlap of the
    first and last periods' member sets is below `jaccard_min`; or, between
    two CONSECUTIVE periods that both hold a non-null member, a member's
    share of the period's non-null rows moves by more than `share_shift`
    (a member absent from a period has share 0), or a member holding more
    than `vanish_share` of the earlier period's rows is absent from the later
    one. `coverage_dim` is the changed dim with the lowest first/last Jaccard
    (ties by argument order) and `coverage_jaccard` that dim's Jaccard."""
    prefix = _check_prefix(prefix)
    dims = _as_list(dims, "dims")
    metrics = _as_list(metrics, "metrics")
    unit_cols = _as_list(unit_cols, "unit_cols")
    _require_columns(df, [period_col], "period_col")
    _require_columns(df, dims, "dims")
    _require_columns(df, metrics, "metrics")
    _require_columns(df, unit_cols, "unit_cols")
    for metric in metrics:
        _require_numeric(df, metric, "metrics")
    try:
        as_of_stamp = _naive(pd.Timestamp(as_of if as_of is not None else pd.Timestamp.now()))
        if pd.isna(as_of_stamp):
            raise ValueError("as_of")
    except Exception:
        raise ValueError("as_of: must be a date") from None

    labels, known, stamps = _order_with_stamps(df[period_col], order)
    frames = _period_frames(df, period_col, labels)
    rows = [int(len(sub)) for sub in frames]

    table = pd.DataFrame({"period": labels, "rows": rows})
    member_sets = {}
    for dim in dims:
        sets = [set(_first_appearance(sub[dim].dropna())) for sub in frames]
        member_sets[dim] = sets
        table[f"{dim}_members"] = [len(s) for s in sets]
    null_shares = {}
    for metric in metrics:
        shares = [float(sub[metric].isna().mean()) if len(sub) else 0.0 for sub in frames]
        null_shares[metric] = shares
        table[f"{metric}_null_share"] = shares
    for unit in unit_cols:
        if unit not in member_sets:
            member_sets[unit] = [set(_first_appearance(sub[unit].dropna())) for sub in frames]

    findings: dict = {}
    values: dict = {
        "n_periods": len(labels),
        "first_period": _label_str(labels[0]) if labels else None,
        "latest_period": _label_str(labels[-1]) if labels else None,
        # aliases: the names the planner's period-scope rule has the code set
        "period_from": _label_str(labels[0]) if labels else None,
        "period_to": _label_str(labels[-1]) if labels else None,
        "latest_rows": rows[-1] if rows else None,
        "median_rows": _int_or_float(np.median(rows)) if rows else None,
        "thin_period_label": None,
        "coverage_jaccard": None,
        "unknown_share_max": None,
        "as_of_period": str(as_of_stamp.to_period("M")),
        "coverage_dim_label": None,
        "unknown_dim_label": None,
    }

    if labels:
        # coverage: member-set size swings or first/last overlap
        coverage_dims = list(dict.fromkeys(dims + unit_cols))
        changed = []
        jaccards = {}
        for dim in coverage_dims:
            sets = member_sets[dim]
            sizes = [len(s) for s in sets]
            median_size = float(np.median(sizes))
            jac = _jaccard(sets[0], sets[-1])
            jaccards[dim] = jac
            swing = any(size < ratio_low * median_size or size > ratio_high * median_size
                        for size in sizes)
            if swing or jac < jaccard_min or _member_share_shift(
                    frames, dim, share_shift, vanish_share):
                changed.append(dim)
        if coverage_dims:
            if changed:
                findings["coverage"] = "coverage_change"
                lowest = min(changed, key=lambda d: (jaccards[d], coverage_dims.index(d)))
                findings["coverage_dim"] = lowest
                values["coverage_dim_label"] = str(lowest)
                values["coverage_jaccard"] = jaccards[lowest]
            else:
                findings["coverage"] = "coverage_stable"
                values["coverage_jaccard"] = min(jaccards.values())

        # thin / partial periods
        median_rows = float(np.median(rows))
        thin = [i for i, n in enumerate(rows) if n < ratio_low * median_rows]
        if thin:
            findings["thin_period"] = True
            thinnest = min(thin, key=lambda i: (rows[i], i))
            values["thin_period_label"] = _label_str(labels[thinnest])
        if len(rows) >= 2:
            others = float(np.median(rows[:-1]))
            if rows[-1] < ratio_low * others:
                findings["period_status"] = "partial_period"

        # unknown-dominated dims
        best_share, best_dim = 0.0, None
        for dim in dims:
            for sub in frames:
                if not len(sub):
                    continue
                share = float(_unknown_mask(sub[dim]).mean())
                if share > best_share:
                    best_share, best_dim = share, dim
        values["unknown_share_max"] = best_share
        if best_dim is not None and best_share >= unknown_share:
            findings["unknown_dominated"] = True
            findings["unknown_dim"] = best_dim
            values["unknown_dim_label"] = str(best_dim)

        # mixed units
        for unit in unit_cols:
            if df[unit].dropna().nunique() >= 2:
                findings["units"] = "mixed_units"
                break

        # a metric entirely null in one period, present in another
        for metric in metrics:
            shares = null_shares[metric]
            if any(s >= 1.0 for s in shares) and any(s < 1.0 for s in shares):
                findings["metric_gap"] = True
                break

    if not known:
        findings["period_order_unknown"] = True
    if labels and stamps is not None and stamps[-1] is not None:
        latest = _naive(pd.Timestamp(stamps[-1])).to_period("M")
        current = as_of_stamp.to_period("M")
        findings["latest_is_current_period"] = bool(latest == current)
        if latest < current:
            findings["latest_is_past_period"] = True

    return _result(table, values, findings, prefix, "coverage_check")


# ---------------------------------------------------------------------------
# B8.4 detect_anomalies
# ---------------------------------------------------------------------------
def _anomaly_core(values: list, z: float, ratio: float, skip=None) -> dict:
    """`skip`: period indexes (data gaps) that are never flagged nor primary;
    the median / MAD statistics stay computed over every period."""
    skip = set(skip or ())
    v = np.asarray(values, dtype=float)
    n = len(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        m = float(np.nanmedian(v)) if n else float("nan")
        dev = np.abs(v - m)
        mad = float(np.nanmedian(dev)) if n else float("nan")
        if mad and math.isfinite(mad):
            robust_z = 0.6745 * (v - m) / mad
        else:
            mean_ad = float(np.nanmean(dev)) if n else float("nan")
            if mean_ad and math.isfinite(mean_ad):
                robust_z = 0.6745 * (v - m) / mean_ad
            else:
                robust_z = np.zeros(n)
        ratio_med = v / m if (math.isfinite(m) and m > 0) else np.full(n, np.nan)
        ratio_prev = np.full(n, np.nan)
        for i in range(1, n):
            if math.isfinite(v[i - 1]) and v[i - 1] > 0:
                ratio_prev[i] = v[i] / v[i - 1]
        flagged = []
        for i in range(n):
            if i in skip:
                flagged.append(False)
                continue
            hit = bool(math.isfinite(robust_z[i]) and abs(robust_z[i]) >= z)
            r = ratio_med[i]
            if not hit and math.isfinite(r):
                if r == 0:
                    hit = True                      # max(0, 1/0) is infinite
                elif r > 0:
                    hit = max(r, 1.0 / r) >= ratio
            flagged.append(bool(hit))
        primary = None
        best = None
        for i in range(n):
            if not flagged[i]:
                continue
            r = ratio_med[i]
            if math.isfinite(r) and r > 0:
                score = abs(math.log(r))
            elif r == 0:
                score = math.inf
            else:
                score = abs(robust_z[i]) if math.isfinite(robust_z[i]) else 0.0
            if best is None or score >= best:
                best, primary = score, i
    return {"median": m, "robust_z": robust_z, "ratio_med": ratio_med, "ratio_prev": ratio_prev,
            "flagged": flagged, "primary": primary}


def _data_gaps(values: list, rows: list, thin_ratio: float) -> list:
    """Indexes of the DATA-GAP periods: fewer rows than `thin_ratio` x the
    median period's rows; an aggregated value of exactly 0 while every
    existing neighbour is non-zero and finite; or — only when every period
    holds at most one row (`max(rows) <= 1`, a pre-aggregated frame where the
    row-count rule cannot fire) — a COLLAPSED value: finite, >= 0 and below
    `COLLAPSE_RATIO` x every existing neighbour, each of them finite and > 0.
    On raw rows a collapsed value with a normal row count is a business
    change, not a data gap; the row-count and zero rules apply there."""
    n = len(values)
    if not n:
        return []
    median_rows = float(np.median(rows))
    one_row_per_period = max(rows) <= 1
    gaps = []
    for i in range(n):
        if rows[i] < thin_ratio * median_rows:
            gaps.append(i)
            continue
        v = values[i]
        if not (isinstance(v, (int, float)) and math.isfinite(v)):
            continue
        neighbours = [values[j] for j in (i - 1, i + 1) if 0 <= j < n]
        if not neighbours:
            continue
        if v == 0 and all(isinstance(w, (int, float)) and math.isfinite(w) and w != 0
                          for w in neighbours):
            gaps.append(i)
            continue
        if one_row_per_period and v >= 0 and all(
                isinstance(w, (int, float)) and math.isfinite(w) and w > 0
                and v < COLLAPSE_RATIO * w for w in neighbours):
            gaps.append(i)
    return gaps


def _gap_period(labels: list, values: list, rows: list, gaps) -> str | None:
    """The label of the gap period with the fewest rows (ties: the lowest
    value, then the earliest period), else None."""
    if not gaps:
        return None

    def key(i):
        v = values[i]
        v = v if isinstance(v, (int, float)) and math.isfinite(v) else math.inf
        return (rows[i], v, i)

    return _label_str(labels[min(gaps, key=key)])


def detect_anomalies(df, period_col, metric, agg: str = "sum", *, order=None,
                     z: float = 3.5, ratio: float = 2.0, thin_ratio: float = THIN_PERIOD_RATIO,
                     prefix: str = "") -> dict:
    """Is one period far from the others? (robust z or ratio to the median) — B8.4.

    A DATA-GAP period (`_data_gaps`: thin rows, a zero between non-zero
    neighbours, or — in a one-row-per-period frame only — a value collapsed
    below `COLLAPSE_RATIO` x its neighbours) is never an anomaly nor the
    primary one; any gap sets
    `period_status = "data_gap"`. The table's trailing `rows` / `is_data_gap`
    columns carry the per-period row count and the gap flag."""
    prefix = _check_prefix(prefix)
    agg = _check_agg(agg)
    _require_columns(df, [period_col], "period_col")
    _require_columns(df, [metric], "metric")
    if agg in _NUMERIC_AGGS:
        _require_numeric(df, metric, "metric")

    try:
        thin_ratio = float(thin_ratio)
    except Exception:
        raise ValueError("thin_ratio: must be a number") from None

    labels, _known, _stamps = _order_with_stamps(df[period_col], order)
    frames = _period_frames(df, period_col, labels)
    v = [_agg_series(sub[metric], agg) for sub in frames]
    rows = [int(len(sub)) for sub in frames]
    n = len(labels)
    findings: dict = {}
    values: dict = {
        "anomaly_period": None, "anomaly_value": None, "baseline_value": None,
        "anomaly_ratio_median": None, "anomaly_prev_period": None, "anomaly_prev_value": None,
        "anomaly_ratio_prev": None, "n_periods": n, "n_anomalies": 0,
        "data_gap_period": None,
        "period_from": _label_str(labels[0]) if labels else None,
        "period_to": _label_str(labels[-1]) if labels else None,
    }
    columns = ["period", "value", "robust_z", "ratio_prev", "ratio_median", "is_anomaly",
               "is_primary", "rows", "is_data_gap"]
    if n < 3:
        findings["too_few_periods"] = True
        table = pd.DataFrame({
            "period": labels, "value": v, "robust_z": [np.nan] * n, "ratio_prev": [np.nan] * n,
            "ratio_median": [np.nan] * n, "is_anomaly": [False] * n, "is_primary": [False] * n,
            "rows": rows, "is_data_gap": [False] * n,
        }, columns=columns)
        table["is_anomaly"] = table["is_anomaly"].astype(bool)
        table["is_primary"] = table["is_primary"].astype(bool)
        table["rows"] = table["rows"].astype(int)
        table["is_data_gap"] = table["is_data_gap"].astype(bool)
        if n:
            values["baseline_value"] = _num(np.median(v))
        return _result(table, values, findings, prefix, "detect_anomalies")

    gaps = set(_data_gaps(v, rows, thin_ratio))
    core = _anomaly_core(v, z, ratio, skip=gaps)
    flagged, primary = core["flagged"], core["primary"]
    table = pd.DataFrame({
        "period": labels,
        "value": v,
        "robust_z": [float(x) for x in core["robust_z"]],
        "ratio_prev": [float(x) for x in core["ratio_prev"]],
        "ratio_median": [float(x) for x in core["ratio_med"]],
        "is_anomaly": flagged,
        "is_primary": [i == primary for i in range(n)],
        "rows": rows,
        "is_data_gap": [i in gaps for i in range(n)],
    }, columns=columns)
    table["is_anomaly"] = table["is_anomaly"].astype(bool)
    table["is_primary"] = table["is_primary"].astype(bool)
    table["rows"] = table["rows"].astype(int)
    table["is_data_gap"] = table["is_data_gap"].astype(bool)
    table.attrs["pdc_metric"] = str(metric)
    if gaps:
        findings["period_status"] = "data_gap"
        values["data_gap_period"] = _gap_period(labels, v, rows, gaps)

    n_anomalies = int(sum(flagged))
    values["baseline_value"] = _num(core["median"])
    values["n_anomalies"] = n_anomalies
    if primary is None:
        findings["anomaly"] = "no_anomaly"
    else:
        findings["anomaly"] = "anomaly_confirmed"
        if v[primary] > core["median"]:
            findings["anomaly_up"] = True
        if primary == n - 1:
            findings["anomaly_is_latest"] = True
        values["anomaly_period"] = _label_str(labels[primary])
        values["anomaly_value"] = _num(v[primary])
        values["anomaly_ratio_median"] = _num(core["ratio_med"][primary])
        if primary > 0:
            values["anomaly_prev_period"] = _label_str(labels[primary - 1])
            values["anomaly_prev_value"] = _num(v[primary - 1])
            values["anomaly_ratio_prev"] = _num(core["ratio_prev"][primary])
    if n_anomalies > 1:
        findings["multiple_anomalies"] = True
    return _result(table, values, findings, prefix, "detect_anomalies")


# ---------------------------------------------------------------------------
# B8.5 decompose_change
# ---------------------------------------------------------------------------
_DECOMPOSE_COLUMNS = ["dim", "member", "from_value", "to_value", "delta", "share_of_delta",
                      "share_of_base"]


def _find_period(labels: list, wanted, param: str, period_col) -> int:
    for index, label in enumerate(labels):
        try:
            if label == wanted:
                return index
        except Exception:
            pass
        if _label_str(label) == _label_str(wanted):
            return index
    raise ValueError(f"{param}: not a period of column {period_col!r}")


def _member_aggs(sub: pd.DataFrame, dim, metric, agg: str) -> dict:
    out = {}
    if not len(sub):
        return out
    keys = sub[dim].astype(object).where(sub[dim].notna(), "(missing)")
    for key, part in sub.groupby(keys, sort=False):
        out[key] = _agg_series(part[metric], agg)
    return out


def decompose_change(df, period_col, metric, from_period=None, to_period=None, dims=(), *,
                     agg: str = "sum", order=None, top_k: int = 5, concentrated_share: float = 0.6,
                     even_tolerance: float = 0.15, large_ratio: float = 2.0,
                     unknown_share: float = 0.5, prefix: str = "") -> dict:
    """Which members of which dimension carry the change between two
    periods? — B8.5.

    A dim whose Unknown-like share (`_unknown_mask`) in the from OR the to
    period is above `unknown_share` is SKIPPED — no table rows, no part in
    the shape / choice — and reported as `unknown_dominated` / `unknown_dim`
    (the first such dim in argument order). `change_dim_label` names the dim
    of `change_top_member` whenever one is set."""
    prefix = _check_prefix(prefix)
    agg = _check_agg(agg)
    dims = _as_list(dims, "dims")
    _require_columns(df, [period_col], "period_col")
    _require_columns(df, [metric], "metric")
    _require_columns(df, dims, "dims")
    if agg in _NUMERIC_AGGS:
        _require_numeric(df, metric, "metric")
    try:
        top_k = max(int(top_k), 0)
    except Exception:
        raise ValueError("top_k: must be a whole number") from None
    try:
        unknown_share = float(unknown_share)
    except Exception:
        raise ValueError("unknown_share: must be a number") from None

    labels, _known, _stamps = _order_with_stamps(df[period_col], order)
    findings: dict = {}
    values: dict = {
        "change_from_period": None, "change_to_period": None, "change_from_value": None,
        "change_to_value": None, "change_abs": None, "change_ratio": None, "change_pct": None,
        "change_top_member": None, "change_top_member_share": None,
        "change_top_member_delta": None, "change_dim_label": None,
    }
    empty = pd.DataFrame(columns=_DECOMPOSE_COLUMNS)

    to_index = None
    if isinstance(to_period, str) and to_period == "primary_anomaly":
        detected = detect_anomalies(df, period_col, metric, agg=agg, order=order)
        table = detected["table"]
        if detected["findings"].get("anomaly") == "anomaly_confirmed" and len(table):
            hits = [i for i, flag in enumerate(table["is_primary"].tolist()) if flag]
            if hits:
                to_index = hits[0]
        elif detected["findings"].get("anomaly") == "no_anomaly":
            findings["anomaly"] = "no_anomaly"
        to_period = None
    if len(labels) < 2:
        findings["too_few_periods"] = True
        return _result(empty, values, findings, prefix, "decompose_change")
    if to_index is None:
        to_index = (len(labels) - 1 if to_period is None
                    else _find_period(labels, to_period, "to_period", period_col))
    if from_period is None:
        from_index = to_index - 1
        if from_index < 0:
            findings["too_few_periods"] = True
            return _result(empty, values, findings, prefix, "decompose_change")
    else:
        from_index = _find_period(labels, from_period, "from_period", period_col)

    frames = _period_frames(df, period_col, [labels[from_index], labels[to_index]])
    sub_from, sub_to = frames
    from_total = _agg_series(sub_from[metric], agg) if len(sub_from) else 0.0
    to_total = _agg_series(sub_to[metric], agg) if len(sub_to) else 0.0
    from_total = 0.0 if not math.isfinite(from_total) else from_total
    to_total = 0.0 if not math.isfinite(to_total) else to_total
    change = to_total - from_total
    change_ratio = to_total / from_total if from_total != 0 else None

    values.update({
        "change_from_period": _label_str(labels[from_index]),
        "change_to_period": _label_str(labels[to_index]),
        "change_from_value": from_total,
        "change_to_value": to_total,
        "change_abs": change,
        "change_ratio": change_ratio,
        "change_pct": (change_ratio - 1.0) if change_ratio is not None else None,
    })
    if change > 0:
        findings["change_up"] = True
    if change_ratio is not None and from_total > 0 and change_ratio >= 0:
        if change_ratio >= large_ratio or (large_ratio > 0 and change_ratio <= 1.0 / large_ratio):
            findings["change_large"] = True

    usable = []
    for dim in dims:
        dominated = False
        for sub in (sub_from, sub_to):
            if len(sub) and float(_unknown_mask(sub[dim]).mean()) > unknown_share:
                dominated = True
                break
        if dominated:
            if not findings.get("unknown_dominated"):
                findings["unknown_dominated"] = True
                findings["unknown_dim"] = dim
        else:
            usable.append(dim)

    rows = []
    per_dim = {}
    for dim in usable:
        agg_from = _member_aggs(sub_from, dim, metric, agg)
        agg_to = _member_aggs(sub_to, dim, metric, agg)
        members = list(dict.fromkeys(list(agg_from) + list(agg_to)))
        entries = []
        for member in members:
            f_v = agg_from.get(member, 0.0)
            t_v = agg_to.get(member, 0.0)
            f_v = 0.0 if not math.isfinite(f_v) else f_v
            t_v = 0.0 if not math.isfinite(t_v) else t_v
            entries.append({"member": member, "from": f_v, "to": t_v, "delta": t_v - f_v})
        sum_delta = sum(e["delta"] for e in entries)
        sum_from = sum(e["from"] for e in entries)
        for e in entries:
            e["sod"] = e["delta"] / sum_delta if sum_delta != 0 else float("nan")
            e["sob"] = e["from"] / sum_from if sum_from > 0 else float("nan")
            e["mismatch"] = abs(e["sod"] - e["sob"])
        ranked = sorted(range(len(entries)), key=lambda i: (-abs(entries[i]["delta"]), i))
        top = entries[ranked[0]] if ranked else None
        concentrated = bool(top is not None and math.isfinite(top["sod"])
                            and top["sod"] >= concentrated_share
                            and math.isfinite(top["mismatch"]) and top["mismatch"] > even_tolerance)
        relevant = [e for e in entries
                    if (math.isfinite(e["sob"]) and e["sob"] >= 0.01)
                    or (math.isfinite(e["sod"]) and e["sod"] >= 0.01)]
        even = bool(relevant) and all(math.isfinite(e["mismatch"]) and e["mismatch"] <= even_tolerance
                                      for e in relevant)
        per_dim[dim] = {"top": top, "concentrated": concentrated, "even": even}

        kept = [entries[i] for i in ranked[:top_k]]
        rest = [entries[i] for i in ranked[top_k:]]
        for e in kept:
            rows.append([dim, _label_str(e["member"]), e["from"], e["to"], e["delta"],
                         e["sod"], e["sob"]])
        o_from = sum(e["from"] for e in rest)
        o_to = sum(e["to"] for e in rest)
        o_delta = o_to - o_from
        rows.append([dim, "(other)", o_from, o_to, o_delta,
                     o_delta / sum_delta if sum_delta != 0 else float("nan"),
                     o_from / sum_from if sum_from > 0 else float("nan")])

    table = pd.DataFrame(rows, columns=_DECOMPOSE_COLUMNS) if rows else empty
    table.attrs["pdc_metric"] = str(metric)
    table.attrs["pdc_from_period"] = values["change_from_period"]
    table.attrs["pdc_to_period"] = values["change_to_period"]

    if usable:
        concentrated_dims = [d for d in usable if per_dim[d]["concentrated"]]
        if concentrated_dims:
            chosen = max(concentrated_dims,
                         key=lambda d: (per_dim[d]["top"]["sod"], -usable.index(d)))
            findings["change_shape"] = "concentrated_in_one_dim"
            findings["change_dim"] = chosen
        else:
            if all(per_dim[d]["even"] for d in usable):
                findings["change_shape"] = "spread_evenly"
            scored = [d for d in usable if per_dim[d]["top"] is not None]
            chosen = (max(scored, key=lambda d: (
                abs(per_dim[d]["top"]["sod"]) if math.isfinite(per_dim[d]["top"]["sod"]) else -1.0,
                -usable.index(d))) if scored else None)
        top = per_dim[chosen]["top"] if chosen is not None else None
        if top is not None:
            values["change_top_member"] = _label_str(top["member"])
            values["change_top_member_share"] = _num(top["sod"])
            values["change_top_member_delta"] = _num(top["delta"])
            values["change_dim_label"] = str(chosen)
            table.attrs["pdc_change_dim"] = str(chosen)
    return _result(table, values, findings, prefix, "decompose_change")


# ---------------------------------------------------------------------------
# B8.6 attribute_level
# ---------------------------------------------------------------------------
_METHODS = ("auto", "variance", "forest", "shap")


def _weighted_mean(y: np.ndarray, w: np.ndarray) -> float:
    total = float(w.sum())
    return float((y * w).sum() / total) if total > 0 else float("nan")


def _pooled(series: pd.Series) -> pd.Series:
    keys = series.astype(object).where(series.notna(), "(missing)")
    counts = keys.value_counts()
    rare = set(counts[counts < 0.005 * len(keys)].index)
    if rare:
        keys = keys.where(~keys.isin(rare), "(rare)")
    return keys


def _eta_squared(keys: pd.Series, y: np.ndarray, w: np.ndarray) -> float:
    overall = _weighted_mean(y, w)
    ss_total = float((w * (y - overall) ** 2).sum())
    if not ss_total > 0:
        return 0.0
    frame = pd.DataFrame({"k": keys.to_numpy(), "y": y, "w": w})
    ss_between = 0.0
    for _key, part in frame.groupby("k", sort=False):
        pw = part["w"].to_numpy()
        mean = _weighted_mean(part["y"].to_numpy(), pw)
        if math.isfinite(mean):
            ss_between += float(pw.sum()) * (mean - overall) ** 2
    return float(min(max(ss_between / ss_total, 0.0), 1.0))


def _spearman_sq(x: pd.Series, y: pd.Series) -> float:
    both = pd.DataFrame({"x": x.to_numpy(), "y": y.to_numpy()}).dropna()
    if len(both) < 3:
        return 0.0
    rx, ry = both["x"].rank(), both["y"].rank()
    if rx.std() == 0 or ry.std() == 0:
        return 0.0
    rho = float(np.corrcoef(rx.to_numpy(), ry.to_numpy())[0, 1])
    return rho * rho if math.isfinite(rho) else 0.0


def _variance_importance(data, target, dims, numeric_cols, weights) -> dict:
    y = data[target].to_numpy(dtype=float)
    out = {}
    for dim in dims:
        out[dim] = _eta_squared(_pooled(data[dim]), y, weights)
    for col in numeric_cols:
        out[col] = _spearman_sq(data[col], data[target])
    return out


def _forest_model(data, target, dims, numeric_cols, weights):
    from sklearn.ensemble import RandomForestRegressor

    parts, owners = [], []
    for dim in dims:
        keys = data[dim].astype(object).where(data[dim].notna(), "(missing)").astype(str)
        top = list(keys.value_counts().index[:20])
        keys = keys.where(keys.isin(top), "(other)")
        dummies = pd.get_dummies(keys, prefix="d", dtype=float)
        parts.append(dummies.reset_index(drop=True))
        owners.extend([dim] * dummies.shape[1])
    for col in numeric_cols:
        series = data[col].astype(float)
        fill = series.median() if series.notna().any() else 0.0
        parts.append(series.fillna(fill).reset_index(drop=True).to_frame(name=f"n_{len(owners)}"))
        owners.append(col)
    x = pd.concat(parts, axis=1).to_numpy(dtype=float)
    y = data[target].to_numpy(dtype=float)
    model = RandomForestRegressor(n_estimators=100, max_depth=8, random_state=0, oob_score=True)
    model.fit(x, y, sample_weight=weights)
    oob = float(getattr(model, "oob_score_", 0.0))
    scale = max(oob, 0.0) if math.isfinite(oob) else 0.0
    return model, x, owners, scale


def _forest_importance(data, target, dims, numeric_cols, weights) -> dict:
    model, _x, owners, scale = _forest_model(data, target, dims, numeric_cols, weights)
    out = {col: 0.0 for col in list(dims) + list(numeric_cols)}
    for owner, importance in zip(owners, model.feature_importances_):
        out[owner] += float(importance) * scale
    return out


def _shap_importance(data, target, dims, numeric_cols, weights) -> dict:
    import shap

    model, x, owners, scale = _forest_model(data, target, dims, numeric_cols, weights)
    contributions = np.abs(np.asarray(shap.TreeExplainer(model).shap_values(x))).mean(axis=0)
    total = float(contributions.sum())
    out = {col: 0.0 for col in list(dims) + list(numeric_cols)}
    if total > 0:
        for owner, value in zip(owners, contributions):
            out[owner] += float(value) / total * scale
    return out


def _top_member(data, dim, target, weights):
    keys = _pooled(data[dim])
    y = data[target].to_numpy(dtype=float)
    overall = _weighted_mean(y, weights)
    frame = pd.DataFrame({"k": keys.to_numpy(), "y": y, "w": weights})
    best, best_gap = None, -1.0
    for key, part in frame.groupby("k", sort=False):
        mean = _weighted_mean(part["y"].to_numpy(), part["w"].to_numpy())
        if not math.isfinite(mean):
            continue
        gap = abs(mean - overall)
        if gap > best_gap:
            best, best_gap = (key, mean), gap
    return best


def attribute_level(df, target, dims, numeric_cols, *, method: str = "auto",
                    dominant_ratio: float = 2.0, weak_share: float = 0.05,
                    weight: str | None = None, max_rows: int = 200_000, prefix: str = "") -> dict:
    """Which column explains the level of `target` best? — B8.6."""
    prefix = _check_prefix(prefix)
    dims = _as_list(dims, "dims")
    numeric_cols = _as_list(numeric_cols, "numeric_cols")
    _require_columns(df, [target], "target")
    _require_columns(df, dims, "dims")
    _require_columns(df, numeric_cols, "numeric_cols")
    _require_numeric(df, target, "target")
    for col in numeric_cols:
        _require_numeric(df, col, "numeric_cols")
    if weight is not None:
        _require_columns(df, [weight], "weight")
        _require_numeric(df, weight, "weight")
    if method not in _METHODS:
        raise ValueError("method: must be one of auto, variance, forest, shap")
    try:
        max_rows = max(int(max_rows), 1)
    except Exception:
        raise ValueError("max_rows: must be a whole number") from None

    columns = list(dict.fromkeys([target] + dims + numeric_cols + ([weight] if weight else [])))
    data = df[columns].copy()
    data[target] = _numeric(data[target])
    for col in numeric_cols:
        data[col] = _numeric(data[col])
    data = data[data[target].notna()]
    if len(data) > max_rows:
        data = data.sample(n=max_rows, random_state=0)
    data = data.reset_index(drop=True)
    if weight:
        weights = _numeric(data[weight]).fillna(0.0).clip(lower=0.0).to_numpy(dtype=float)
    else:
        weights = np.ones(len(data), dtype=float)

    findings: dict = {}
    values: dict = {
        "top_feature_label": None, "second_feature_label": None, "top_feature_share": None,
        "second_feature_share": None, "n_rows_used": int(len(data)), "top_member": None,
        "top_member_target": None,
        "overall_target": _num(_weighted_mean(data[target].to_numpy(dtype=float), weights))
        if len(data) else None,
    }
    table_columns = ["feature", "kind", "importance", "top_member", "top_member_target"]
    features = dims + [c for c in numeric_cols if c not in dims]
    if not len(data) or not features:
        return _result(pd.DataFrame(columns=table_columns), values, findings, prefix,
                       "attribute_level")

    importance = None
    fell_back = False
    chain = {"shap": ["shap", "forest", "variance"], "forest": ["forest", "variance"]}.get(
        method, ["variance"])
    for step in chain:
        try:
            if step == "shap":
                importance = _shap_importance(data, target, dims, numeric_cols, weights)
            elif step == "forest":
                importance = _forest_importance(data, target, dims, numeric_cols, weights)
            else:
                importance = _variance_importance(data, target, dims, numeric_cols, weights)
            break
        except Exception:
            fell_back = True
            importance = None
    if importance is None:
        importance = {col: 0.0 for col in features}
    if fell_back:
        findings["method_fallback"] = True

    members = {}
    for dim in dims:
        members[dim] = _top_member(data, dim, target, weights)
    rows = []
    for col in features:
        hit = members.get(col)
        rows.append({
            "feature": col,
            "kind": "dimension" if col in dims else "numeric",
            "importance": float(importance.get(col, 0.0)),
            "top_member": _label_str(hit[0]) if hit else None,
            "top_member_target": float(hit[1]) if hit else float("nan"),
        })
    order_idx = sorted(range(len(rows)), key=lambda i: (-rows[i]["importance"], i))
    table = pd.DataFrame([rows[i] for i in order_idx], columns=table_columns)
    table.attrs["pdc_target"] = str(target)

    top = rows[order_idx[0]]
    second = rows[order_idx[1]] if len(order_idx) > 1 else None
    top_share = top["importance"]
    second_share = second["importance"] if second else 0.0
    if top_share >= weak_share and (second_share == 0 or top_share / second_share >= dominant_ratio):
        findings["attribution"] = "dominant_feature"
    elif top_share < weak_share:
        findings["attribution"] = "weak_signal"
    findings["top_feature"] = top["feature"]
    if second is not None and second_share >= weak_share:
        findings["second_feature"] = second["feature"]

    values["top_feature_label"] = str(top["feature"])
    values["top_feature_share"] = top_share
    if second is not None:
        values["second_feature_label"] = str(second["feature"])
        values["second_feature_share"] = second_share
    hit = members.get(top["feature"])
    if hit:
        values["top_member"] = _label_str(hit[0])
        values["top_member_target"] = _num(hit[1])
    return _result(table, values, findings, prefix, "attribute_level")


# ---------------------------------------------------------------------------
# B8.7 check_metric_overlap
# ---------------------------------------------------------------------------
def check_metric_overlap(df, metric_a, metric_b, *, equal_share: float = 0.5,
                         rel_tol: float = 1e-9, min_compared_share: float = 0.1,
                         prefix: str = "") -> dict:
    """Are two metrics (mostly) the same numbers under two names? — B8.7.

    The non-null rows are those where both metrics are set. A row where BOTH
    are 0 says nothing about whether they measure the same thing (an empty
    month of two flow metrics), so the equal share is taken over the COMPARED
    rows only — non-null and not both zero. When no row is compared, or fewer
    than `min_compared_share` of the non-null rows, the share is None and no
    finding is set. VALUES: `overlap_equal_share` (over the compared rows),
    `overlap_sum_ratio` (sum a / sum b over the non-null rows),
    `overlap_rows` (the compared count), `overlap_nonnull_rows`."""
    prefix = _check_prefix(prefix)
    _require_columns(df, [metric_a], "metric_a")
    _require_columns(df, [metric_b], "metric_b")
    _require_numeric(df, metric_a, "metric_a")
    _require_numeric(df, metric_b, "metric_b")
    both = pd.DataFrame({"a": _numeric(df[metric_a]).to_numpy(),
                         "b": _numeric(df[metric_b]).to_numpy()}).dropna()
    a, b = both["a"].to_numpy(dtype=float), both["b"].to_numpy(dtype=float)
    n_nonnull = int(len(both))
    compared = ~((a == 0) & (b == 0))
    n = int(compared.sum())
    findings: dict = {}
    if n and n >= min_compared_share * n_nonnull:
        ac, bc = a[compared], b[compared]
        scale = np.maximum(np.maximum(np.abs(ac), np.abs(bc)), 1.0)
        share = float((np.abs(ac - bc) <= rel_tol * scale).mean())
    else:
        share = None
    sum_b = float(b.sum()) if n_nonnull else 0.0
    sum_ratio = float(a.sum()) / sum_b if sum_b != 0 else None
    if share is not None and share >= equal_share:
        findings["metric_relation"] = "metric_definition_overlap"
    if share is not None and share >= 0.99:
        findings["metrics_identical"] = True
    table = pd.DataFrame([{"metric_a": str(metric_a), "metric_b": str(metric_b), "rows": n,
                           "nonnull_rows": n_nonnull,
                           "equal_share": share if share is not None else float("nan"),
                           "sum_ratio": sum_ratio if sum_ratio is not None else float("nan")}])
    values = {"overlap_equal_share": share, "overlap_sum_ratio": sum_ratio, "overlap_rows": n,
              "overlap_nonnull_rows": n_nonnull}
    return _result(table, values, findings, prefix, "check_metric_overlap")


# ---------------------------------------------------------------------------
# B8.8 chart helpers (plotly imported lazily)
# ---------------------------------------------------------------------------
def _require_table(table, columns, helper: str) -> None:
    if not isinstance(table, pd.DataFrame):
        raise ValueError(f"table: {helper} needs a DataFrame")
    for column in columns:
        if column not in table.columns:
            raise ValueError(f"table: column {column!r} is required by {helper}")


def plot_highlighted_series(table, period_col="period", value_col="value", *,
                            highlight_col="is_primary", title=None, y_title=None):
    """One bar per period; the highlighted period in the highlight colour."""
    _require_table(table, [period_col, value_col], "plot_highlighted_series")
    import plotly.graph_objects as go

    if highlight_col in table.columns:
        flags = [bool(x) if not pd.isna(x) else False for x in table[highlight_col].tolist()]
    else:
        flags = [False] * len(table)
    colors = [_PALETTE["highlight"] if flag else _PALETTE["base"] for flag in flags]
    x = [_label_str(p) for p in table[period_col].tolist()]
    fig = go.Figure(go.Bar(x=x, y=table[value_col].tolist(), marker_color=colors))
    fig.update_layout(
        template="plotly_white",
        title=title if title is not None else f"{value_col} by period — highlighted period",
        xaxis_title=str(period_col),
        yaxis_title=y_title if y_title is not None else str(value_col),
        showlegend=False,
    )
    fig.update_xaxes(type="category")
    return fig


def plot_waterfall(table, *, dim=None, title=None, y_title=None):
    """From a `decompose_change` table: start = Σfrom, one bar per member
    delta of the dim, end = Σto. The dim is the one `decompose_change` chose
    (`attrs["pdc_change_dim"]`) whenever it is in the table — a conflicting
    `dim=` is ignored, so the chart always shows what the text names — else
    `dim`, else the first dim. The title (given or default) ends with
    " (<dim>)" unless it already names the dim (case-insensitive)."""
    _require_table(table, ["dim", "member", "from_value", "to_value", "delta"], "plot_waterfall")
    import plotly.graph_objects as go

    dims = list(dict.fromkeys(table["dim"].tolist()))
    chosen = table.attrs.get("pdc_change_dim")
    if chosen is not None and chosen in dims:
        dim = chosen
    elif dim is None:
        if not dims:
            raise ValueError("table: plot_waterfall needs at least one row")
        dim = dims[0]
    part = table[table["dim"] == dim]
    if not len(part):
        raise ValueError(f"dim: no rows for {dim!r} in the table")
    start = float(pd.to_numeric(part["from_value"], errors="coerce").fillna(0).sum())
    end = float(pd.to_numeric(part["to_value"], errors="coerce").fillna(0).sum())
    deltas = pd.to_numeric(part["delta"], errors="coerce").fillna(0).astype(float).tolist()
    labels = ["Start"] + [str(m) for m in part["member"].tolist()] + ["End"]
    metric = table.attrs.get("pdc_metric") or "value"
    if title is None:
        from_p = table.attrs.get("pdc_from_period")
        to_p = table.attrs.get("pdc_to_period")
        if from_p is not None and to_p is not None:
            title = f"What changed: {metric} by {dim}, {from_p} to {to_p}"
        else:
            title = f"What changed: {metric} by {dim}"
    title = str(title)
    if str(dim).lower() not in title.lower():
        title = f"{title} ({dim})"
    fig = go.Figure(go.Waterfall(
        x=labels,
        y=[start] + deltas + [end],
        measure=["absolute"] + ["relative"] * len(deltas) + ["total"],
        increasing={"marker": {"color": _PALETTE["up"]}},
        decreasing={"marker": {"color": _PALETTE["down"]}},
        totals={"marker": {"color": _PALETTE["total"]}},
    ))
    fig.update_layout(
        template="plotly_white",
        title=title,
        xaxis_title=str(dim),
        yaxis_title=y_title if y_title is not None else str(metric),
        showlegend=False,
    )
    return fig


def plot_importance(table, *, title=None, x_title="Share of variation explained"):
    """Horizontal bars of an `attribute_level` table, largest on top."""
    _require_table(table, ["feature", "importance"], "plot_importance")
    import plotly.graph_objects as go

    ordered = table.assign(_imp=pd.to_numeric(table["importance"], errors="coerce").fillna(0.0))
    ordered = ordered.sort_values("_imp", ascending=True, kind="stable")
    target = table.attrs.get("pdc_target") or "the target"
    fig = go.Figure(go.Bar(
        x=ordered["_imp"].tolist(),
        y=[str(f) for f in ordered["feature"].tolist()],
        orientation="h",
        marker_color=_PALETTE["base"],
    ))
    fig.update_layout(
        template="plotly_white",
        title=title if title is not None else f"What explains {target}",
        xaxis_title=x_title,
        showlegend=False,
    )
    return fig
