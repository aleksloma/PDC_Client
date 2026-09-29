"""Article II structural guard for the schema-autofill hints.

`routes.upload._prepare_file_context` builds the `unique_hints` that
`brain_client.schema_autofill` sends to the brain. A categorical column (at
most SCHEMA_AUTOFILL_UNIQUE_THRESHOLD distinct values) still sends its values;
every other column sends ONE computed `[profile: …]` string that carries no
real row value. Offline — no brain call.
"""
import json

import pandas as pd
import pytest

from routes.upload import _column_profile, _prepare_file_context
from settings import settings

CODES = [f"TR-2024-{i:05d}" for i in range(50)]
AMOUNTS = [round(1234.56 + 17.25 * i, 2) for i in range(50)]
DATES = pd.date_range("2024-01-05", periods=50, freq="D")
REGIONS = ["North", "South", "East", "West", "Central"]

FIRST_NAMES = ["Alice", "Bernard", "Camille", "Dmitri", "Eleonora", "Fatima",
               "Giorgio", "Hannah", "Isidora", "Jasper"]
LAST_NAMES = ["Kowalski", "Lindqvist", "Moreau", "Nakamura", "Okonkwo"]
PERSON_NAMES = [f"{f} {l}" for f in FIRST_NAMES for l in LAST_NAMES]   # 50


def _frame():
    return pd.DataFrame({
        "code": CODES,
        "amount": AMOUNTS,
        "event_at": DATES,
        "region": [REGIONS[i % 5] for i in range(50)],
        "person": PERSON_NAMES,
    })


def _hints():
    df = _frame()
    return _prepare_file_context("sales.csv", df, {}, "")["unique_hints"]


def _value_strings(col, values):
    out = set()
    for v in values:
        out.add(str(v))
        out.add(f"{v}")
        if col == "event_at":
            out.add(v.strftime("%Y-%m-%d"))
            out.add(v.isoformat())
    return out


@pytest.mark.parametrize("col,values", [
    ("code", CODES), ("amount", AMOUNTS), ("event_at", list(DATES)),
    ("person", PERSON_NAMES)])
def test_no_real_value_of_a_high_cardinality_column_leaves(col, values):
    hints = _hints()
    blob = json.dumps(hints, default=str)
    leaked = sorted(s for s in _value_strings(col, values) if s in blob)
    assert leaked == [], f"{col} values reached unique_hints: {leaked[:5]}"


@pytest.mark.parametrize("col", ["code", "amount", "event_at", "person"])
def test_high_cardinality_column_sends_one_profile_string(col):
    hint = _hints()[col]
    assert isinstance(hint, list) and len(hint) == 1
    assert isinstance(hint[0], str)
    assert hint[0].startswith("[profile: ") and hint[0].endswith("]")
    assert len(hint[0]) <= settings.SCHEMA_AUTOFILL_VALUE_TRUNC * 4


def test_categorical_column_still_sends_its_values():
    assert sorted(_hints()["region"]) == sorted(REGIONS)


def test_profile_contents_per_type():
    hints = _hints()
    code = hints["code"][0]
    assert "mask=AA-9999-99999" in code
    assert "distinct=50" in code and "unique=yes" in code
    assert "nulls=0.0%" in code
    assert "prefixes=" not in code          # the first word IS the whole value
    amount = hints["amount"][0]
    assert "integers=no" in amount and "increasing=yes" in amount
    assert "non_negative=yes" in amount
    assert "min=1200" in amount and "max=2100" in amount   # 2 significant figures
    ev = hints["event_at"][0]
    assert "min=2024-01" in ev and "max=2024-02" in ev
    assert "granularity=day" in ev


def test_person_names_carry_no_prefixes_and_no_name_part():
    """The first words of a names column are real first names, each far below
    the 20% structural share — none may be reported, and no first or last
    name may appear anywhere in the hints."""
    hints = _hints()
    prof = hints["person"][0]
    assert "prefixes=" not in prof
    blob = json.dumps(hints)
    for part in FIRST_NAMES + LAST_NAMES:
        assert part not in blob, part


def test_structural_prefix_is_kept():
    df = pd.DataFrame({"memo": [f"Invoice {i:05d} paid, net" for i in range(50)]})
    prof = _prepare_file_context("f.csv", df, {}, "")["unique_hints"]["memo"][0]
    assert "prefixes='Invoice'(50)" in prof
    assert "avg_commas=1.0" in prof


def test_prefix_below_the_share_is_dropped():
    # 'Refund' is 30% of the values (kept); every 'Order<n>' first word is 2%.
    vals = [f"Refund {i:04d}" for i in range(15)] + \
           [f"Order{i} {i:04d}" for i in range(35)]
    df = pd.DataFrame({"memo": vals})
    prof = _prepare_file_context("f.csv", df, {}, "")["unique_hints"]["memo"][0]
    assert "prefixes='Refund'(15)" in prof
    assert "Order" not in prof


def test_bool_profile_reports_the_true_share():
    ser = pd.Series([True, False, True, True])
    prof = _column_profile(ser, ser.dropna(), 2, str(ser.dtype))
    assert "true_share=75.0%" in prof


def test_datetime_granularity_time():
    ser = pd.Series(pd.date_range("2024-03-01 10:30", periods=30, freq="h"))
    prof = _column_profile(ser, ser, 30, str(ser.dtype))
    assert "granularity=time" in prof and "min=2024-03" in prof


def test_profile_is_capped_and_closed():
    df = pd.DataFrame({"blob": [("x y " * 150) + str(i) for i in range(50)]})
    prof = _prepare_file_context("f.csv", df, {}, "")["unique_hints"]["blob"][0]
    assert len(prof) <= settings.SCHEMA_AUTOFILL_VALUE_TRUNC * 4
    assert prof.startswith("[profile: ") and prof.endswith("]")


def test_profile_failure_falls_back_to_the_dtype():
    assert _column_profile(None, None, 3, "object") == "[profile: dtype=object]"


def test_all_null_column_gets_a_profile_not_values():
    df = pd.DataFrame({"empty": [None] * 20, "k": range(20)})
    hint = _prepare_file_context("f.csv", df, {}, "")["unique_hints"]["empty"]
    assert len(hint) == 1 and "nulls=100.0%" in hint[0] and "distinct=0" in hint[0]


def test_a_whole_value_is_never_a_prefix():
    """Per value: 'Approved' is a complete value (20 rows) and must not be
    listed even though other values are multi-word; 'Rejected:' starts
    longer values and is structural."""
    vals = ["Approved"] * 20 + [f"Rejected: reason{i}" for i in range(30)]
    df = pd.DataFrame({"status": vals})
    prof = _prepare_file_context("f.csv", df, {}, "")["unique_hints"]["status"][0]
    assert "Approved" not in prof
    assert "prefixes='Rejected:'(30)" in prof


def test_a_symbol_only_mask_is_omitted():
    vals = ["***"] * 30 + [f"x{i}" for i in range(20)]
    df = pd.DataFrame({"flag": vals})
    prof = _prepare_file_context("f.csv", df, {}, "")["unique_hints"]["flag"][0]
    assert "***" not in prof and "mask=" not in prof


def test_profile_failure_is_logged_by_type(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        _column_profile(None, None, 3, "object")
    assert any("AUTOFILL_PROFILE_FAILED error=TypeError" in r.getMessage()
               for r in caplog.records)
