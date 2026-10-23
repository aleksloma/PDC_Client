"""narrative.validate_findings / merge_findings / merge_values / findings_marker
(DIAGNOSTICS_SPEC B7.1–B7.3), written BEFORE the module exists.

FINDINGS cross the data boundary (they ride on `/v1/plan` and in the history
marker), so the validator is the Article II guard for them: a key must be a
planner literal or a `pdc_diag` registry key, a value must be a bool, an
INPUT column name or a `FINDINGS_ENUM` code — never a number, a category
value or a derived column. Drops and collisions are logged as COUNTS only.

Runs offline: `.venv/Scripts/python.exe -m pytest tests/test_findings_validator.py -q`.
"""
import logging

import pytest

import narrative
import pdc_diag
from narrative import (FINDINGS_MAX_KEYS, MARKER_MAX, findings_marker,
                       merge_findings, merge_values, validate_findings)

SID = "t_findings"
COLS = frozenset({"CURRENCY", "LOAN_COUNT", "Department", "ok"})


def _v(raw, code="", cols=COLS):
    return validate_findings(raw, code, cols, SID)


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG, logger="datachat")
    return caplog


def _lines(caplog, needle):
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


# ------------------------------------------------------------------ B7.1 keys

def test_key_must_be_quoted_literal():
    code = 'FINDINGS["has_gaps"] = True\nFINDINGS[\'single_q\'] = True\n'
    out = _v({"has_gaps": True, "single_q": True, "not_in_code": True}, code)
    assert out == {"has_gaps": True, "single_q": True}
    # an unquoted mention is not a literal
    assert _v({"bare": True}, "FINDINGS[bare] = True  # bare") == {}
    # a key that is only a substring of a longer literal is not a literal
    assert _v({"gaps": True}, 'FINDINGS["has_gaps"] = True') == {}


def test_pdc_diag_keys_allowed_without_literal():
    assert "coverage" in pdc_diag.PDC_DIAG_FINDING_KEYS
    assert "anomaly" in pdc_diag.PDC_DIAG_FINDING_KEYS
    out = _v({"coverage": True, "anomaly": "anomaly_confirmed", "coverage_dim": "CURRENCY"},
             code="r = pdc_diag.coverage_check(df)")
    assert out == {"coverage": True, "anomaly": "anomaly_confirmed", "coverage_dim": "CURRENCY"}


def test_key_shape_regex():
    code = 'FINDINGS["Has_Gaps"] = True; FINDINGS["has gaps"] = True; FINDINGS["_x"] = True; FINDINGS["a-b"] = True'
    assert _v({"Has_Gaps": True, "has gaps": True, "_x": True, "a-b": True, 1: True}, code) == {}
    assert _v({"a" * 40: True}, 'FINDINGS["' + "a" * 40 + '"] = True') == {"a" * 40: True}
    assert _v({"a" * 41: True}, 'FINDINGS["' + "a" * 41 + '"] = True') == {}


def test_data_built_key_dropped():
    # f"is_{region}" never appears literally -> dropped (no key built from data)
    code = 'for region in df["REGION"].unique():\n    FINDINGS[f"is_{region}"] = True\n'
    assert _v({"is_north": True, "is_south": True}, code) == {}


def test_raw_must_be_dict():
    assert _v(None) == {}
    assert _v(["coverage"]) == {}
    assert _v("coverage") == {}


# ---------------------------------------------------------------- B7.1 values

def test_value_bool_column_enum_only():
    code = ('FINDINGS["b"]; FINDINGS["col"]; FINDINGS["enum"]; FINDINGS["num"]; '
            'FINDINGS["cat"]; FINDINGS["derived"]; FINDINGS["lower"]; FINDINGS["f"]; '
            'FINDINGS["n"]; FINDINGS["b0"]')
    assert "anomaly_confirmed" in pdc_diag.FINDINGS_ENUM
    out = _v({"b": True, "b0": False, "col": "CURRENCY", "enum": "anomaly_confirmed",
              "num": 5, "f": 2.5, "cat": "North", "derived": "loan_share",
              "lower": "currency", "n": None}, code)
    assert out == {"b": True, "b0": False, "col": "CURRENCY", "enum": "anomaly_confirmed"}


def test_needs_phase2_must_be_bool():
    code = 'FINDINGS["needs_phase2"] = "yes"'
    assert _v({"needs_phase2": "yes"}, code) == {}
    assert _v({"needs_phase2": "CURRENCY"}, code) == {}
    assert _v({"needs_phase2": True}, code) == {"needs_phase2": True}
    assert _v({"needs_phase2": False}, code) == {"needs_phase2": False}
    # needs_phase2 is a reserved KEY, never an enum VALUE (decision F14)
    assert "needs_phase2" not in pdc_diag.FINDINGS_ENUM
    assert _v({"x": "needs_phase2"}, 'FINDINGS["x"]') == {}


def test_column_value_with_marker_chars_dropped():
    bad_cols = frozenset({"a;b", "a]b", "a\nb", "a\rb", "a b", "a b", "ok"})
    code = 'FINDINGS["k1"]; FINDINGS["k2"]; FINDINGS["k3"]; FINDINGS["k4"]; FINDINGS["k5"]; FINDINGS["k6"]; FINDINGS["k7"]'
    out = _v({"k1": "a;b", "k2": "a]b", "k3": "a\nb", "k4": "a\rb", "k5": "a b",
              "k6": "a b", "k7": "ok"}, code, bad_cols)
    assert out == {"k7": "ok"}


def test_drops_logged_as_count_only(logs):
    code = 'FINDINGS["zzsecretkey"]'
    out = _v({"zzsecretkey": "SECRETVALUE", "zzotherkey": True, "zzsecretkey2": 7}, code)
    assert out == {}
    dropped = _lines(logs, "FINDINGS_DROPPED")
    assert dropped and "n=3" in dropped[0]
    blob = "\n".join(r.getMessage() for r in logs.records)
    assert "zzsecretkey" not in blob and "zzotherkey" not in blob and "SECRETVALUE" not in blob


def test_no_cap_per_block():
    # the cap applies at merge time only (B7.1 rule 7)
    keys = [f"k{i:02d}" for i in range(30)]
    code = "; ".join(f'FINDINGS["{k}"]' for k in keys)
    out = _v({k: True for k in keys}, code)
    assert len(out) == 30


# ---------------------------------------------------------------- B7.2 merge

def test_cap_24_priority_needs_phase2_then_registry():
    assert FINDINGS_MAX_KEYS == 24
    lits = [f"lit_{i:02d}" for i in range(22)]
    acc: dict = {}
    merge_findings(acc, {k: True for k in lits}, sid=SID)                      # block 0
    merge_findings(acc, {"coverage": True, "anomaly": "anomaly_confirmed",
                         "needs_phase2": True, "lit_x": True}, sid=SID)         # block 1
    assert len(acc) == 24
    assert set(acc) == {"needs_phase2", "coverage", "anomaly"} | set(lits[:21])
    assert "lit_21" not in acc and "lit_x" not in acc
    assert acc["needs_phase2"] is True and acc["anomaly"] == "anomaly_confirmed"


def test_cap_registry_keys_in_block_order():
    # registry keys of an EARLIER block outrank a later block's literals and
    # registry keys alike; literals of an earlier block outrank later literals
    acc: dict = {}
    merge_findings(acc, {f"lit_{i:02d}": True for i in range(23)}, sid=SID)   # 23 literals
    merge_findings(acc, {"coverage": True, "anomaly": True}, sid=SID)
    merge_findings(acc, {"needs_phase2": False, "units": True}, sid=SID)
    assert len(acc) == 24
    assert {"needs_phase2", "coverage", "anomaly", "units"} <= set(acc)
    assert set(acc) - {"needs_phase2", "coverage", "anomaly", "units"} == {f"lit_{i:02d}" for i in range(20)}


def test_merge_in_block_index_order_not_execution_order():
    # Blocks finish in execution order 2, 0, 1 (tables run after charts);
    # the merge must walk block_index order, so block 2's value wins.
    per_block = {2: {"coverage_dim": "LOAN_COUNT"}, 0: {"coverage_dim": "CURRENCY", "a": True},
                 1: {"b": True}}
    acc: dict = {}
    for idx in sorted(per_block):
        merge_findings(acc, per_block[idx], sid=SID)
    assert acc == {"coverage_dim": "LOAN_COUNT", "a": True, "b": True}
    # the opposite order (execution order) would let block 0 win — pinned so
    # the flow test can rely on the helper's later-wins semantics
    acc2: dict = {}
    for idx in (2, 0, 1):
        merge_findings(acc2, per_block[idx], sid=SID)
    assert acc2["coverage_dim"] == "CURRENCY"


def test_merge_later_wins_logs_collisions(logs):
    acc = {"a": True, "b": "CURRENCY", "c": True}
    merge_findings(acc, {"a": False, "b": "LOAN_COUNT", "d": True}, sid=SID)
    assert acc == {"a": False, "b": "LOAN_COUNT", "c": True, "d": True}
    col = _lines(logs, "FINDINGS_KEY_COLLISION")
    assert col and "n=2" in col[0]
    blob = "\n".join(col)
    assert "CURRENCY" not in blob and "LOAN_COUNT" not in blob

    vals = {"total": 1, "share": 0.5}
    merge_values(vals, {"total": 2, "name": "SECRET_VALUE_X"}, sid=SID)
    assert vals == {"total": 2, "share": 0.5, "name": "SECRET_VALUE_X"}
    vcol = _lines(logs, "VALUES_KEY_COLLISION")
    assert vcol and "n=1" in vcol[0]
    assert "SECRET_VALUE_X" not in "\n".join(r.getMessage() for r in logs.records)


def test_merge_without_collision_logs_nothing(logs):
    acc: dict = {}
    merge_findings(acc, {"a": True}, sid=SID)
    merge_values(acc2 := {}, {"x": 1}, sid=SID)
    assert acc == {"a": True} and acc2 == {"x": 1}
    assert not _lines(logs, "KEY_COLLISION")


def test_merge_never_raises():
    acc: dict = {}
    merge_findings(acc, None, sid=SID)
    merge_findings(acc, "junk", sid=SID)
    merge_values(acc, None, sid=SID)
    assert acc == {}


# --------------------------------------------------------------- B7.3 marker

def test_marker_sorted_bool_text_and_400_cap():
    assert MARKER_MAX == 400
    m = findings_marker({"needs_phase2": True, "coverage_dim": "CURRENCY",
                         "anomaly": "anomaly_confirmed", "flat": False})
    assert m == "[Findings: anomaly=anomaly_confirmed; coverage_dim=CURRENCY; flat=false; needs_phase2=true]"
    assert findings_marker({}) == ""
    assert findings_marker(None) == ""
    big = {f"k{i:02d}": "C" * 60 for i in range(24)}
    cap = findings_marker(big)
    assert len(cap) <= 400
    assert cap.startswith("[Findings: k00=") and cap.endswith("; …]")
    # keys were added in sorted order while the line stayed within the cap
    kept = cap[len("[Findings: "):-len("; …]")].split("; ")
    assert kept == sorted(kept) and kept[0].startswith("k00=")
    assert all(k.endswith("C" * 60) for k in kept)


def test_marker_full_line_without_ellipsis_when_it_fits():
    m = findings_marker({"b": True, "a": "LOAN_COUNT"})
    assert m == "[Findings: a=LOAN_COUNT; b=true]"
    assert "…" not in m
