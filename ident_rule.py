"""ident_rule — the ONE rule that decides whether a column is an IDENTIFIER
(a personal number, a client id, an account, a year), whose numeric values
are shown as plain digit strings: never thousands-grouped, never with
decimals ("52906123456", not "52,906,123,456" or "52906123456.0").

Display only: nothing here changes how a column is loaded or what the
analysis computes.

Two kinds of evidence:
- the NAME (`is_identifier_name`): the label's last word is one of
  `IDENT_WORDS`, or the label ends with "#" — never when ANY word is one of
  `MEASURE_WORDS` ("Total Number", "ID Count") unless the last word is one of
  the `ORIGINAL_IDENT_WORDS` ("sales_year", "revenue_account" stay
  identifiers); "no" and "#" count only after
  another word ("account_no", not "No" or "Yes/No"). Words are lower-cased
  letter runs split on non-letters AND camelCase boundaries
  (`personalNumber`, `clientID`). "number of orders" / "order_count" are not
  identifiers.
- the VALUES of a SOURCE input column (`is_identifier_values`,
  `is_year_values`) — applied only to the frames a turn was asked about,
  never to a result or an aggregate. A result column is an identifier when
  its name passes the name rule or equals (case-insensitive) an identifier
  input column (`identifier_columns`, the only decision function for result
  frames).

Runs in BOTH images (the web app and the analysis sandbox). A LEAF: stdlib +
numpy + pandas only, no logging (a caller logs counts), and no function here
ever raises — every failure answers the conservative value (False / empty).

The per-job state (`begin_job` / `job_identifiers`) is for the sandbox: one
job runs per runner subprocess, and the job's source identifiers are computed
from its own input frames at job start, then read where the result is
formatted (`exec_transport._styled_html`, the chart formatters).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Last words that mark a column as an identifier / year DIMENSION (EN, KA, RU).
IDENT_WORDS = frozenset({
    # the original list (kept) — see ORIGINAL_IDENT_WORDS below
    "year", "yr", "წელი", "id", "ids", "code", "codes", "კოდი", "იდ",
    "zip", "postal", "phone", "account", "iban", "invoice", "order", "ref",
    # numbers and keys
    "number", "num", "no", "nr", "nbr", "key", "uid", "guid", "uuid",
    # personal / tax / document identifiers
    "pin", "pinfl", "ssn", "tin", "inn", "iin", "passport", "card",
    "msisdn", "imei",
    # Georgian / Russian
    "ნომერი", "номер", "код", "счет", "счёт", "ид",
})

# The ORIGINAL identifier words (before P31). A label whose LAST word is one
# of them is an identifier even when it holds a measure word ("sales_year",
# "Sales Order", "revenue_account", "price_code"): the measure veto applies
# to the words added in P31 only ("Total Number", "ID Count").
ORIGINAL_IDENT_WORDS = frozenset({
    "year", "yr", "წელი", "id", "ids", "code", "codes", "კოდი", "იდ",
    "zip", "postal", "phone", "account", "iban", "invoice", "order", "ref",
})

# Words that make a label a MEASURE: a label holding ANY of them is never an
# identifier by name ("loan_count_id", "price_code" are measures), and the
# value / year rules are never applied to such a source column (a "salary"
# whose values happen to be 8-digit numbers stays a grouped measure). Entity
# nouns (loan, payment, deposit, principal, limit) are deliberately NOT here:
# "loan_id" / "payment_id" / "deposit_no" are identifiers, while
# "loan_amount" / "payment_total" / "deposit_sum" are vetoed by their measure
# word.
MEASURE_WORDS = frozenset({
    "amount", "amt", "sum", "total", "price", "cost", "revenue", "sales",
    "salary", "income", "balance", "fee", "value", "qty",
    "quantity", "count", "avg", "mean", "median", "per", "spend", "profit",
    "margin", "rate", "score",
    # Russian
    "сумма", "цена", "стоимость", "выручка", "доход", "баланс", "количество",
    "итого",
    # Georgian
    "თანხა", "ფასი", "ჯამი", "შემოსავალი", "ბალანსი", "რაოდენობა",
})

# Identifier words that count only after another word ("account_no", but not
# "No" alone, nor "Yes/No"); a trailing "#" follows the same rule.
_NEEDS_PREFIX = frozenset({"no"})
_NOT_A_PREFIX = frozenset({"yes"})

# The value rule (source input columns only).
MIN_VALUES = 20          # at least this many non-null values
SAME_DIGITS_MIN = 8      # every value has the same digit count, at least this
UNIQUE_DIGITS_MIN = 6    # or every value has at least this many digits ...
UNIQUE_SHARE_MIN = 0.9   # ... and nunique / non-null is at least this
YEAR_MIN, YEAR_MAX = 1900, 2100
YEAR_NUNIQUE_MAX = 200


def name_tokens(label) -> list:
    """Lower-cased letter runs of `label`, split on every non-letter AND on
    camelCase boundaries: `personalNumber` -> ["personal", "number"],
    `clientID` -> ["client", "id"], `ClientId` -> ["client", "id"],
    `HTTPServer` -> ["http", "server"]; `IDs` stays one word ("ids"). Never
    raises (an unreadable label -> [])."""
    try:
        if label is None:
            return []
        text = str(label)
        tokens: list = []
        cur: list = []
        n = len(text)
        for i, ch in enumerate(text):
            if not ch.isalpha():
                if cur:
                    tokens.append("".join(cur).lower())
                    cur = []
                continue
            if cur:
                prev = text[i - 1]
                split = prev.islower() and ch.isupper()
                if not split and prev.isupper() and ch.isupper():
                    # An acronym followed by a word: split before the word's
                    # capital — only when at least two lower-case letters
                    # follow, so a plural "IDs" stays one word.
                    j = i + 1
                    while j < n and text[j].isalpha() and text[j].islower():
                        j += 1
                    split = (j - (i + 1)) >= 2
                if split:
                    tokens.append("".join(cur).lower())
                    cur = []
            cur.append(ch)
        if cur:
            tokens.append("".join(cur).lower())
        return tokens
    except Exception:
        return []


def has_measure_word(label) -> bool:
    """True when ANY word of the label is in MEASURE_WORDS. Never raises."""
    try:
        return any(t in MEASURE_WORDS for t in name_tokens(label))
    except Exception:
        return False


def _counts_after(toks: list) -> bool:
    """A word that needs a prefix ("no", a trailing "#") counts when another
    word comes before it that is not "yes" ("account_no", "Client #"; not
    "No", "#", "Yes/No")."""
    return len(toks) >= 1 and toks[-1] not in _NOT_A_PREFIX


def is_identifier_name(label) -> bool:
    """True when the label's LAST word is in IDENT_WORDS, or the label ends
    with "#" — never when any word is a MEASURE word, unless the last word is
    one of ORIGINAL_IDENT_WORDS ("sales_year", "revenue_account" stay
    identifiers); "no" and "#" count only after another word (not after
    "yes"). Never raises."""
    try:
        if label is None:
            return False
        text = str(label).strip()
        if not text:
            return False
        toks = name_tokens(text)
        if (any(t in MEASURE_WORDS for t in toks)
                and not (toks and toks[-1] in ORIGINAL_IDENT_WORDS)):
            return False
        if text.endswith("#"):
            return _counts_after(toks)
        if not toks:
            return False
        last = toks[-1]
        if last in _NEEDS_PREFIX:
            return _counts_after(toks[:-1])
        return last in IDENT_WORDS
    except Exception:
        return False


def _integral_values(series):
    """The non-null values of a numeric, non-bool series whose values are all
    integral (int dtype, or float with only whole finite values), as a
    Series; None otherwise."""
    if not isinstance(series, pd.Series):
        return None
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype) or not pd.api.types.is_numeric_dtype(dtype):
        return None
    if pd.api.types.is_complex_dtype(dtype):
        return None
    vals = series.dropna()
    if pd.api.types.is_integer_dtype(dtype):
        return vals
    if not pd.api.types.is_float_dtype(dtype):
        return None
    arr = vals.to_numpy(dtype="float64", na_value=np.nan)
    if arr.size and (not np.isfinite(arr).all() or not (arr == np.floor(arr)).all()):
        return None
    return vals


def _digits(value) -> int:
    return len(str(int(value)))


def is_identifier_values(series) -> bool:
    """For a SOURCE input column only (never a result frame): integer-valued
    numeric, every value >= 0, at least MIN_VALUES non-null values, and
    either every value has the same digit count >= SAME_DIGITS_MIN, or every
    value has >= UNIQUE_DIGITS_MIN digits and nunique / non-null >=
    UNIQUE_SHARE_MIN. Never raises (False on any error)."""
    try:
        vals = _integral_values(series)
        if vals is None or len(vals) < MIN_VALUES:
            return False
        lo, hi = vals.min(), vals.max()
        if lo < 0:
            return False
        d_lo, d_hi = _digits(lo), _digits(hi)
        if d_lo == d_hi and d_lo >= SAME_DIGITS_MIN:
            return True
        if d_lo >= UNIQUE_DIGITS_MIN:
            return vals.nunique() / len(vals) >= UNIQUE_SHARE_MIN
        return False
    except Exception:
        return False


def is_year_values(series) -> bool:
    """Integer-valued, at least MIN_VALUES non-null values, every value in
    YEAR_MIN..YEAR_MAX, at most YEAR_NUNIQUE_MAX distinct values (a
    "client_since" column). Never raises."""
    try:
        vals = _integral_values(series)
        if vals is None or len(vals) < MIN_VALUES:
            return False
        if vals.min() < YEAR_MIN or vals.max() > YEAR_MAX:
            return False
        return vals.nunique() <= YEAR_NUNIQUE_MAX
    except Exception:
        return False


def source_identifier_columns(dfs) -> frozenset:
    """Names (str) of the NUMERIC input columns of `dfs` (a dict of frames)
    that pass `is_identifier_name`, `is_identifier_values` or
    `is_year_values`. Text columns are never grouped, so they are not
    examined. Cost: per numeric column one dropna + min/max and, only when
    the digit counts call for it, one nunique. Never raises (an unreadable
    column is skipped; an unreadable argument -> empty)."""
    out: set = set()
    try:
        frames = dfs.values() if isinstance(dfs, dict) else ()
        for df in frames:
            if not isinstance(df, pd.DataFrame):
                continue
            for i, col in enumerate(df.columns):
                try:
                    name = str(col)
                    if name in out:
                        continue
                    series = df.iloc[:, i]
                    dtype = series.dtype
                    if (pd.api.types.is_bool_dtype(dtype)
                            or not pd.api.types.is_numeric_dtype(dtype)):
                        continue
                    if is_identifier_name(name):
                        out.add(name)
                    elif has_measure_word(name):
                        # a measure is never an identifier by its values
                        continue
                    elif is_identifier_values(series) or is_year_values(series):
                        out.add(name)
                except Exception:
                    continue
    except Exception:
        pass
    return frozenset(out)


def identifier_columns(columns, source_idents=()) -> list:
    """The labels (str of each, in order, once each) among `columns` that pass
    `is_identifier_name` or equal, case-insensitively, a name in
    `source_idents`. The ONLY decision for result frames: no value rule is
    ever applied to a result or an aggregate. Never raises."""
    try:
        try:
            known = {str(s).lower() for s in (source_idents or ())}
        except Exception:
            known = set()
        out: list = []
        seen: set = set()
        for col in list(columns if columns is not None else ()):
            try:
                label = str(col)
                if label in seen:
                    continue
                if is_identifier_name(label) or label.lower() in known:
                    out.append(label)
                    seen.add(label)
            except Exception:
                continue
        return out
    except Exception:
        return []


def plain_text(value):
    """An identifier cell as plain text: None for a missing value (None /
    NaN / NaT), `str(int(v))` for an integral number, `str(v)` otherwise (a
    string is returned unchanged). Never raises."""
    try:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (bool, np.bool_)):
            return str(value)
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, float):
            if value != value:
                return None
            if value in (float("inf"), float("-inf")):
                return str(value)
            if value.is_integer():
                return str(int(value))
            return str(value)
        if isinstance(value, int):
            return str(value)
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        return str(value)
    except Exception:
        try:
            return str(value)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# per-job state (the sandbox: one job per runner subprocess)
# ---------------------------------------------------------------------------
# The job's input frames (shallow copies, so code that adds or replaces a
# column of an input frame does not change the verdict) and, once computed,
# their identifier names. Computed LAZILY — only when a Styler is serialized
# or a figure is formatted — so a job that needs neither pays nothing.
_JOB: dict = {"dfs": None, "idents": frozenset()}


def begin_job(dfs) -> None:
    """Reset the per-job state and remember the job's input frames; the
    identifiers are computed on the first `job_identifiers()` call. Never
    raises (a failure leaves the state empty)."""
    _JOB["dfs"] = None
    _JOB["idents"] = frozenset()
    try:
        if isinstance(dfs, dict) and dfs:
            _JOB["dfs"] = {k: (v.copy(deep=False) if isinstance(v, pd.DataFrame) else v)
                           for k, v in dfs.items()}
    except Exception:
        _JOB["dfs"] = None


def reset_job() -> None:
    """Forget the per-job state (tests; a process between jobs)."""
    begin_job({})


def job_identifiers() -> frozenset:
    """The current job's source identifiers (empty before `begin_job` or
    after `reset_job`), computed once per job. Never raises."""
    try:
        frames = _JOB.get("dfs")
        if frames is not None:
            _JOB["dfs"] = None
            _JOB["idents"] = source_identifier_columns(frames)
        idents = _JOB.get("idents")
        return idents if isinstance(idents, frozenset) else frozenset()
    except Exception:
        _JOB["dfs"] = None
        _JOB["idents"] = frozenset()
        return frozenset()
