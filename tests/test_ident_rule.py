"""P31 Part D — `ident_rule.py`, the ONE identifier rule (spec_partAD_client).

Incident: the top-5 clients table showed `personal_number` (an 11-digit int64,
unique per row) as "52,906,…". Five name-only copies of a last-word list
disagreed with each other, never split camelCase, knew no Russian and had no
value rule. `ident_rule` is the single decision module (stdlib + pandas /
numpy only) that every site now asks:

* `name_tokens(label)` — lower-cased letter runs, split on non-letters AND on
  camelCase boundaries;
* `IDENT_WORDS` + `is_identifier_name(label)` — the LAST token is an
  identifier word, or the label ends with `#`;
* `is_identifier_values(series)` / `is_year_values(series)` — value rules for
  SOURCE input columns only, never raising;
* `source_identifier_columns(dfs)` — the input columns that pass any of the
  three rules (numeric columns);
* `identifier_columns(columns, source_idents)` — the ONLY decision function
  for a result frame: the name rule or a case-insensitive match with a source
  identifier; no value rule is ever applied to a result.

Pure unit tests, offline.
"""
import ast
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import ident_rule

ROOT = Path(__file__).resolve().parent.parent

OLD_WORDS = ["year", "yr", "წელი", "id", "ids", "code", "codes", "კოდი", "იდ", "zip",
             "postal", "phone", "account", "iban", "invoice", "order", "ref"]
NEW_WORDS = ["number", "num", "no", "nr", "nbr", "key", "uid", "guid", "uuid", "pin",
             "pinfl", "ssn", "tin", "inn", "iin", "passport", "card", "msisdn", "imei",
             "ნომერი", "номер", "код", "счет", "счёт", "ид"]


def _pn(n=60, base=52906123456):
    """An 11-digit, unique-per-row personal number column (int64)."""
    return pd.Series([base + i * 1013 for i in range(n)], dtype="int64")


def bank_clients(n=60) -> pd.DataFrame:
    """A frame shaped like the bank clients file of the incident."""
    limits = [500, 1500, 3000, 25000, 100000, 750]
    return pd.DataFrame({
        "personal_number": _pn(n),
        "name": [f"Client {i}" for i in range(n)],
        "client_since": [1995 + (i % 30) for i in range(n)],
        "age": [18 + (i * 7) % 60 for i in range(n)],
        "credit_limit_gel": [limits[i % len(limits)] for i in range(n)],
        "loan_amount": [12345.67 + i * 101.3 for i in range(n)],
    })


# ---------------------------------------------------------------------------
# module boundary: a leaf (it runs in the sandbox image AND the web app)
# ---------------------------------------------------------------------------
def test_ident_rule_imports_only_stdlib_pandas_and_numpy():
    tree = ast.parse((ROOT / "ident_rule.py").read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    project = {p.stem for p in ROOT.glob("*.py")}
    # logger_utils would be acceptable for a never-raising helper, but nothing
    # else of the project: the module must stay importable on both images.
    offenders = sorted((roots & project) - {"logger_utils"})
    assert offenders == [], offenders
    assert not (roots & {"httpx", "requests", "sqlalchemy", "openpyxl"}), roots


# ---------------------------------------------------------------------------
# name_tokens
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label,expected", [
    ("personal_number", ["personal", "number"]),
    ("personalNumber", ["personal", "number"]),
    ("clientID", ["client", "id"]),
    ("ClientId", ["client", "id"]),
    ("Personal Number", ["personal", "number"]),
    ("personal-number (GEL)", ["personal", "number", "gel"]),
    ("IBAN", ["iban"]),
    ("client2_id", ["client", "id"]),
    ("პირადი ნომერი", ["პირადი", "ნომერი"]),
    ("Номер счета", ["номер", "счета"]),
    ("Лицевой счет", ["лицевой", "счет"]),
    ("Account #", ["account"]),
    ("", []),
    ("123_456", []),
])
def test_name_tokens(label, expected):
    assert ident_rule.name_tokens(label) == expected


@pytest.mark.parametrize("label", [None, 2024, 3.5, ("a", "b")])
def test_name_tokens_of_a_non_string_label_never_raises(label):
    out = ident_rule.name_tokens(label)
    assert isinstance(out, list)


# ---------------------------------------------------------------------------
# IDENT_WORDS / is_identifier_name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("word", OLD_WORDS + NEW_WORDS)
def test_ident_words_hold_the_old_list_and_the_new_words(word):
    assert word in ident_rule.IDENT_WORDS


@pytest.mark.parametrize("word", OLD_WORDS + NEW_WORDS)
def test_a_label_ending_with_an_ident_word_is_an_identifier(word):
    assert ident_rule.is_identifier_name(f"client_{word}") is True


@pytest.mark.parametrize("label", [
    "personal_number", "personalNumber", "PersonalNumber", "client_id", "clientID",
    "ClientId", "phone", "iban", "IBAN", "card_no", "Account #", "Client #",
    "client #", "Personal Number", "პირადი ნომერი", "Лицевой счет", "Year", "order_id",
])
def test_identifier_names(label):
    assert ident_rule.is_identifier_name(label) is True


@pytest.mark.parametrize("label", [
    "number_of_orders", "Number of Orders", "order_count", "amount", "income",
    "client_since", "Order Value", "ID Count", "# of clients", "age",
    "credit_limit_gel", "loan_amount", "",
    # The LAST-token rule, pinned as the spec states it: "счета" (genitive of
    # счет) is not in IDENT_WORDS, so "Номер счета" ("account number") is NOT
    # an identifier by name; "Код клиента" ("client code") ends with
    # "клиента" and is not one either. Such a column is still caught by the
    # value rule when it is a source column.
    "Номер счета", "Код клиента",
])
def test_non_identifier_names(label):
    assert ident_rule.is_identifier_name(label) is False


@pytest.mark.parametrize("label", [None, 0, 52906123456, 1.5])
def test_is_identifier_name_of_odd_input_is_false(label):
    assert ident_rule.is_identifier_name(label) is False


# ---------------------------------------------------------------------------
# is_identifier_values (SOURCE input columns only)
# ---------------------------------------------------------------------------
def test_eleven_digit_unique_ints_are_identifier_values():
    assert ident_rule.is_identifier_values(_pn(60)) is True


def test_twenty_values_is_enough_and_nineteen_is_not():
    assert ident_rule.is_identifier_values(_pn(20)) is True
    assert ident_rule.is_identifier_values(_pn(19)) is False


def test_six_digit_ninety_five_percent_unique_is_identifier_values():
    vals = [100000 + i * 37 for i in range(38)] + [100000, 100037]
    s = pd.Series(vals, dtype="int64")
    assert s.nunique() / len(s) == pytest.approx(0.95)
    assert ident_rule.is_identifier_values(s) is True


def test_six_digit_half_unique_is_not_identifier_values():
    vals = [100000 + (i % 20) * 37 for i in range(40)]
    assert ident_rule.is_identifier_values(pd.Series(vals)) is False


def test_mixed_six_and_seven_digits_all_unique_is_identifier_values():
    vals = [100000 + i * 11 for i in range(15)] + [1000000 + i * 13 for i in range(15)]
    assert ident_rule.is_identifier_values(pd.Series(vals)) is True


def test_same_digit_count_eight_or_more_needs_no_uniqueness():
    # "every value has the same digit count >= 8" — the first branch.
    vals = [12345678, 23456789, 34567890] * 10
    assert ident_rule.is_identifier_values(pd.Series(vals)) is True


def test_same_digit_count_seven_with_repeats_is_not_identifier_values():
    vals = [1234567, 2345678, 3456789] * 10
    assert ident_rule.is_identifier_values(pd.Series(vals)) is False


def test_amounts_with_varying_digit_counts_are_not_identifier_values():
    vals = [150, 2300, 45000, 1200000, 875, 99000] * 6
    assert ident_rule.is_identifier_values(pd.Series(vals)) is False


def test_integral_floats_with_nan_are_identifier_values():
    vals = [float(v) for v in _pn(25)] + [np.nan, np.nan, np.nan]
    s = pd.Series(vals, dtype="float64")
    assert ident_rule.is_identifier_values(s) is True


def test_non_integral_floats_are_not_identifier_values():
    s = pd.Series([float(v) + 0.5 for v in _pn(30)])
    assert ident_rule.is_identifier_values(s) is False


def test_negative_values_are_not_identifier_values():
    assert ident_rule.is_identifier_values(-_pn(30)) is False
    one_negative = _pn(30).copy()
    one_negative.iloc[3] = -one_negative.iloc[3]
    assert ident_rule.is_identifier_values(one_negative) is False


@pytest.mark.parametrize("series", [
    pd.Series([str(v) for v in range(10000000000, 10000000030)], dtype=object),
    pd.Series([], dtype="int64"),
    pd.Series([], dtype=object),
    pd.Series([None] * 30, dtype=object),
    pd.Series([np.nan] * 30, dtype="float64"),
    pd.Series([True, False] * 15),
    pd.Series(pd.date_range("2020-01-01", periods=30)),
    pd.Series(["a", 1, 2.5, None] * 8, dtype=object),
])
def test_is_identifier_values_is_false_on_non_numeric_or_empty(series):
    assert ident_rule.is_identifier_values(series) is False


@pytest.mark.parametrize("value", [None, [1, 2, 3], "52906123456", 52906123456, object()])
def test_is_identifier_values_never_raises(value):
    assert ident_rule.is_identifier_values(value) is False


# ---------------------------------------------------------------------------
# is_year_values
# ---------------------------------------------------------------------------
def test_client_since_years_are_year_values():
    assert ident_rule.is_year_values(bank_clients()["client_since"]) is True


def test_float_years_with_nan_are_year_values():
    s = pd.Series([1995.0 + (i % 30) for i in range(40)] + [np.nan, np.nan])
    assert ident_rule.is_year_values(s) is True


@pytest.mark.parametrize("vals", [
    [95, 3, 12, 99, 7] * 6,              # two-digit years
    [1899] + [2000 + i % 20 for i in range(30)],
    [2101] + [2000 + i % 20 for i in range(30)],
    [2015.5] + [2000.0 + i % 20 for i in range(30)],
])
def test_out_of_range_or_non_integral_are_not_year_values(vals):
    assert ident_rule.is_year_values(pd.Series(vals)) is False


@pytest.mark.parametrize("value", [
    None, "2015", object(),
    pd.Series(["2015", "2016"] * 15, dtype=object),
    pd.Series(pd.date_range("2020-01-01", periods=30)),
])
def test_is_year_values_never_raises_and_rejects_non_numeric(value):
    assert ident_rule.is_year_values(value) is False


def test_is_year_values_on_an_empty_series_returns_a_bool():
    assert isinstance(ident_rule.is_year_values(pd.Series([], dtype="int64")), bool)


# ---------------------------------------------------------------------------
# source_identifier_columns
# ---------------------------------------------------------------------------
def test_bank_clients_source_identifiers_are_personal_number_and_client_since():
    out = ident_rule.source_identifier_columns({"clients.xlsx": bank_clients()})
    assert isinstance(out, frozenset)
    assert out == frozenset({"personal_number", "client_since"})


def test_a_numeric_column_named_like_an_identifier_counts_without_a_value_rule():
    df = pd.DataFrame({"client_id": list(range(1, 41)), "amount": [10.5] * 40})
    assert ident_rule.source_identifier_columns({"d": df}) == frozenset({"client_id"})


def test_the_value_rule_catches_an_identifier_whose_name_does_not():
    # "Номер счета" fails the name rule (last token "счета"); its VALUES are
    # 13-digit and unique, so as a SOURCE column it is an identifier.
    df = pd.DataFrame({"Номер счета": [2200000000000 + i * 17 for i in range(30)],
                       "Сумма": [1000.5 + i for i in range(30)]})
    assert ident_rule.source_identifier_columns({"d": df}) == frozenset({"Номер счета"})


def test_source_identifiers_union_over_every_frame():
    a = pd.DataFrame({"personal_number": _pn(30), "x": np.arange(30) * 1.5})
    b = pd.DataFrame({"client_since": [2000 + i % 20 for i in range(30)], "y": np.arange(30) * 2.5})
    out = ident_rule.source_identifier_columns({"a": a, "b": b})
    assert out == frozenset({"personal_number", "client_since"})


def test_source_identifier_columns_skips_a_value_that_is_not_a_frame():
    out = ident_rule.source_identifier_columns(
        {"junk": "not a frame", "none": None, "clients": bank_clients()})
    assert out == frozenset({"personal_number", "client_since"})


def test_source_identifier_columns_of_no_frames_is_empty():
    assert ident_rule.source_identifier_columns({}) == frozenset()


def test_source_identifier_columns_cost_is_bounded_on_a_million_rows():
    n = 1_000_000
    df = pd.DataFrame({
        "personal_number": np.arange(10_000_000_000, 10_000_000_000 + n, dtype="int64"),
        "amount": np.random.default_rng(0).random(n) * 1e6,
        "client_since": (np.arange(n) % 30 + 1995).astype("int64"),
    })
    started = time.perf_counter()
    out = ident_rule.source_identifier_columns({"big": df})
    elapsed = time.perf_counter() - started
    assert out == frozenset({"personal_number", "client_since"})
    assert elapsed < 10.0, elapsed


# ---------------------------------------------------------------------------
# identifier_columns (result frames: NO value rule)
# ---------------------------------------------------------------------------
def test_identifier_columns_by_name():
    out = ident_rule.identifier_columns(["personal_number", "total_loans"], frozenset())
    assert out == ["personal_number"]


def test_identifier_columns_by_source_identifier_case_insensitive():
    out = ident_rule.identifier_columns(
        ["Client_Since", "n_clients", "personalNumber"], frozenset({"client_since"}))
    assert out == ["Client_Since", "personalNumber"]


def test_identifier_columns_keeps_column_order_and_returns_strings():
    out = ident_rule.identifier_columns([0, "client_id", "amount", "card_no"], frozenset())
    assert out == ["client_id", "card_no"]
    assert all(isinstance(c, str) for c in out)


def test_identifier_columns_applies_no_value_rule():
    """A result column with identifier-LIKE values (an aggregate that happens
    to be 11-digit and unique) is not an identifier: only names decide."""
    assert ident_rule.identifier_columns(["total_balance", "loan_amount"], frozenset()) == []
    # The source identifier set is matched by NAME only, too.
    assert ident_rule.identifier_columns(["total_balance"], frozenset({"client_since"})) == []


def test_identifier_columns_of_nothing_is_an_empty_list():
    assert ident_rule.identifier_columns([], frozenset()) == []
