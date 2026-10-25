"""pdc_diag result dicts fail LOUDLY on a missing key + the period_from /
period_to alias VALUES (follow-up to the BRB runs after client PR #28).

BRB run 3: generated code read `r["values"]["from_period"]` from a
`coverage_check` result (the key is `first_period`). The sandbox answered
`KeyError: 'from_period'`, the retry planner had no idea which keys exist and
guessed again. Now:

1. `r`, `r["values"]` and `r["findings"]` of every analysis function
   (`coverage_check`, `detect_anomalies`, `decompose_change`,
   `attribute_level`, `check_metric_overlap`) are a dict subclass whose
   `__getitem__` on a missing key raises a plain `KeyError` whose single arg
   is EXACTLY
   `f"{key!r} is not a key of {fn} {part}; available keys: {', '.join(sorted(keys))}"`
   (`part` = result / values / findings; "none" when there are no keys; the
   key repr capped at 80 chars; VALUES keys listed WITH the prefix; `keys`
   are the names pdc_diag set when it built the dict — a key the code adds
   later may be data-derived, so it is never listed). Key
   names only — never a VALUES value. Everything else behaves like a dict
   (`.get`, `in`, `dict(...)`, `json.dumps`, deepcopy, pickle).
2. `coverage_check` and `detect_anomalies` VALUES also carry `period_from` /
   `period_to` (the first / last period label; None without periods).
3. The sandbox passes the message through unchanged at both exec sites
   (`code_exec._execute_in_process`, `plot_utils._render_in_process`), and
   the retry scrub (`brain_client._scrub_error_msg`) keeps it byte-identical
   when the failed code wrote the key as a literal; a dynamically built key
   becomes `'<value>'` while the key list stays as it is.

Offline, small synthetic frames with invented numbers:
`.venv/Scripts/python.exe -m pytest tests/test_pdc_diag_result_keys.py -q`.
"""
import copy
import json
import logging
import pickle
import sys

import pandas as pd
import pytest

import brain_client
import code_exec
import exec_transport
import pdc_diag
import plot_utils

PERIODS = ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03"]


# ---------------------------------------------------------------------------
# fixtures (invented numbers)
# ---------------------------------------------------------------------------
def _frame(periods=PERIODS) -> pd.DataFrame:
    """4 rows per period, two currencies, a metric and an identical copy."""
    rows = []
    for i, p in enumerate(periods):
        for j in range(4):
            amount = 4696.25 + i * 10 + j
            rows.append({"month": p, "cur": ["UZS", "USD"][j % 2],
                         "amt": amount, "amt2": amount})
    return pd.DataFrame(rows, columns=["month", "cur", "amt", "amt2"])


def _cases():
    df = _frame()
    return {
        "coverage_check": lambda: pdc_diag.coverage_check(df, "month", dims=["cur"], metrics=["amt"]),
        "detect_anomalies": lambda: pdc_diag.detect_anomalies(df, "month", "amt"),
        "decompose_change": lambda: pdc_diag.decompose_change(df, "month", "amt", dims=["cur"]),
        "attribute_level": lambda: pdc_diag.attribute_level(df, "amt", ["cur"], []),
        "check_metric_overlap": lambda: pdc_diag.check_metric_overlap(df, "amt", "amt2"),
    }


FUNCTIONS = list(_cases())


def _run(fn):
    return _cases()[fn]()


def _message(key, fn, part, keys) -> str:
    listed = ", ".join(sorted(str(k) for k in keys)) or "none"
    return f"{key!r} is not a key of {fn} {part}; available keys: {listed}"


def _raised(mapping, key) -> KeyError:
    with pytest.raises(KeyError) as info:
        mapping[key]
    return info.value


def _assert_message(err, expected):
    assert type(err) is KeyError and type(err).__name__ == "KeyError"
    assert err.args == (expected,)
    assert str(err) == repr(expected)


def _value_tokens(values) -> list:
    """VALUES values as text: every label (str of 3+ chars) and every number."""
    out = []
    for v in dict(values).values():
        if isinstance(v, str) and len(v) >= 3:
            out.append(v)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(str(v))
    return out


# ---------------------------------------------------------------------------
# 1. missing keys fail loudly
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fn", FUNCTIONS)
def test_missing_values_key_names_available_keys(fn):
    r = _run(fn)
    err = _raised(r["values"], "from_period")
    expected = _message("from_period", fn, "values", list(r["values"]))
    _assert_message(err, expected)


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_missing_findings_key_names_available_keys(fn):
    r = _run(fn)
    assert len(r["findings"]) > 0
    err = _raised(r["findings"], "zz_not_a_finding")
    _assert_message(err, _message("zz_not_a_finding", fn, "findings", list(r["findings"])))


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_missing_result_key_names_available_keys(fn):
    r = _run(fn)
    err = _raised(r, "value")
    _assert_message(err, f"'value' is not a key of {fn} result; available keys: findings, table, values")


def test_coverage_check_values_message_exact_text():
    r = pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"])
    err = _raised(r["values"], "from_period")
    keys = ", ".join(sorted(r["values"]))
    assert "period_from" in r["values"] and "period_to" in r["values"]
    assert str(err) == repr(f"'from_period' is not a key of coverage_check values; "
                            f"available keys: {keys}")
    assert str(err).startswith("\"'from_period' is not a key of coverage_check values; "
                               "available keys: as_of_period, ")


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_message_carries_key_names_only_never_values(fn):
    r = _run(fn)
    msg = _raised(r["values"], "from_period").args[0]
    # no key name holds a digit, so no number / period label leaked in
    assert not any(c.isdigit() for c in msg)
    for token in _value_tokens(r["values"]):
        assert token not in msg
    for label in PERIODS:
        assert label not in msg


def test_empty_findings_list_reads_none():
    cov = pdc_diag.coverage_check(_frame().iloc[0:0], "month", dims=["cur"], metrics=["amt"])
    assert dict(cov["findings"]) == {}
    _assert_message(_raised(cov["findings"], "thin_period"),
                    "'thin_period' is not a key of coverage_check findings; available keys: none")
    zeros = pd.DataFrame({"a": [0.0, 0.0], "b": [0.0, 0.0]})
    ovl = pdc_diag.check_metric_overlap(zeros, "a", "b")
    assert dict(ovl["findings"]) == {}
    _assert_message(_raised(ovl["findings"], "metric_relation"),
                    "'metric_relation' is not a key of check_metric_overlap findings; "
                    "available keys: none")


def test_prefixed_values_listed_with_prefix():
    r = pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"], prefix="cov_")
    err = _raised(r["values"], "cov_from_period")
    keys = sorted(r["values"])
    assert all(k.startswith("cov_") for k in keys)
    assert "cov_first_period" in keys and "cov_period_from" in keys
    _assert_message(err, _message("cov_from_period", "coverage_check", "values", keys))
    assert "available keys: cov_as_of_period, " in err.args[0]


def test_long_missing_key_repr_is_capped():
    r = pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"])
    key = "k" * 300
    msg = _raised(r["values"], key).args[0]
    suffix = (" is not a key of coverage_check values; available keys: "
              + ", ".join(sorted(r["values"])))
    assert msg.startswith(repr(key)[:80])
    assert msg.endswith(suffix)                      # the key list is intact
    assert len(msg) - len(suffix) <= 83              # repr capped near 80 chars
    assert len(msg) < len(repr(key)) + len(suffix)


# ---------------------------------------------------------------------------
# 1b. everything else is still a dict
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fn", FUNCTIONS)
def test_result_dicts_still_behave_like_dicts(fn):
    r = _run(fn)
    for mapping in (r, r["values"], r["findings"]):
        assert isinstance(mapping, dict)
        assert mapping.get("zz_missing") is None
        assert mapping.get("zz_missing", 7) == 7
        assert "zz_missing" not in mapping
    assert set(r) == {"table", "values", "findings"}
    assert isinstance(r["table"], pd.DataFrame)
    plain_values = dict(r["values"])
    assert type(plain_values) is dict and plain_values == r["values"]
    plain_findings = dict(r["findings"])
    assert type(plain_findings) is dict and plain_findings == r["findings"]
    for key, value in plain_values.items():
        assert key in r["values"]
        assert r["values"][key] == value
        assert r["values"].get(key) == value
    json.loads(json.dumps(r["values"]))
    json.loads(json.dumps(r["findings"]))
    assert json.loads(json.dumps(r["values"])) == plain_values
    merged = {}
    merged.update(r["values"])
    assert merged == plain_values and {**r["values"]} == plain_values


def test_existing_keys_unchanged():
    r = pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"])
    assert r["values"]["n_periods"] == 5
    assert r["values"]["first_period"] == "2025-11"
    assert r["values"]["latest_period"] == "2026-03"
    assert r["findings"]["coverage"] == "coverage_stable"
    r["values"]["zq_new"] = 1
    assert r["values"]["zq_new"] == 1


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_deepcopy_and_pickle_keep_keys_and_message(fn):
    r = _run(fn)
    expected = _message("from_period", fn, "values", list(r["values"]))
    for clone in (copy.deepcopy(r), pickle.loads(pickle.dumps(r))):
        assert set(clone) == {"table", "values", "findings"}
        assert dict(clone["values"]) == dict(r["values"])
        assert dict(clone["findings"]) == dict(r["findings"])
        _assert_message(_raised(clone["values"], "from_period"), expected)
    for clone in (copy.deepcopy(r["values"]), pickle.loads(pickle.dumps(r["values"]))):
        assert dict(clone) == dict(r["values"])
        _assert_message(_raised(clone, "from_period"), expected)


# ---------------------------------------------------------------------------
# 2. period_from / period_to aliases
# ---------------------------------------------------------------------------
def test_coverage_check_period_aliases():
    v = pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"])["values"]
    assert v["period_from"] == v["first_period"] == "2025-11"
    assert v["period_to"] == v["latest_period"] == "2026-03"
    assert v["n_periods"] == 5


def test_coverage_check_period_aliases_two_periods():
    v = pdc_diag.coverage_check(_frame(PERIODS[:2]), "month", dims=["cur"], metrics=["amt"])["values"]
    assert v["period_from"] == "2025-11" and v["period_to"] == "2025-12"
    assert v["n_periods"] == 2


def test_coverage_check_period_aliases_empty_frame():
    v = pdc_diag.coverage_check(_frame().iloc[0:0], "month", dims=["cur"], metrics=["amt"])["values"]
    assert "period_from" in v and "period_to" in v
    assert v["period_from"] is None and v["period_to"] is None
    assert v["n_periods"] == 0


def test_coverage_check_period_aliases_prefixed():
    v = pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"], prefix="cov_")["values"]
    assert v["cov_period_from"] == v["cov_first_period"] == "2025-11"
    assert v["cov_period_to"] == v["cov_latest_period"] == "2026-03"
    assert v["cov_n_periods"] == 5
    assert "period_from" not in v and "period_to" not in v


def test_detect_anomalies_period_aliases():
    r = pdc_diag.detect_anomalies(_frame(), "month", "amt")
    v, t = r["values"], r["table"]
    assert v["period_from"] == "2025-11" and v["period_to"] == "2026-03"
    assert v["period_from"] == pdc_diag._label_str(t["period"].iloc[0])
    assert v["period_to"] == pdc_diag._label_str(t["period"].iloc[-1])
    assert v["n_periods"] == 5 == len(t)


def test_detect_anomalies_period_aliases_datetime_labels():
    df = _frame()
    df["month"] = pd.to_datetime(df["month"] + "-01")
    r = pdc_diag.detect_anomalies(df, "month", "amt")
    v, t = r["values"], r["table"]
    assert v["period_from"] == "2025-11-01" and v["period_to"] == "2026-03-01"
    assert isinstance(v["period_from"], str) and isinstance(v["period_to"], str)
    assert v["period_from"] == pdc_diag._label_str(t["period"].iloc[0])
    assert v["period_to"] == pdc_diag._label_str(t["period"].iloc[-1])


def test_detect_anomalies_period_aliases_too_few_periods():
    r = pdc_diag.detect_anomalies(_frame(PERIODS[:2]), "month", "amt")
    assert r["findings"].get("too_few_periods") is True
    v = r["values"]
    assert v["period_from"] == "2025-11" and v["period_to"] == "2025-12"
    assert v["n_periods"] == 2


def test_detect_anomalies_period_aliases_one_period():
    v = pdc_diag.detect_anomalies(_frame(PERIODS[:1]), "month", "amt")["values"]
    assert v["period_from"] == "2025-11" and v["period_to"] == "2025-11"
    assert v["n_periods"] == 1


def test_detect_anomalies_period_aliases_empty_frame():
    v = pdc_diag.detect_anomalies(_frame().iloc[0:0], "month", "amt")["values"]
    assert "period_from" in v and "period_to" in v
    assert v["period_from"] is None and v["period_to"] is None
    assert v["n_periods"] == 0


def test_detect_anomalies_period_aliases_prefixed():
    v = pdc_diag.detect_anomalies(_frame(), "month", "amt", prefix="cov_")["values"]
    assert v["cov_period_from"] == "2025-11" and v["cov_period_to"] == "2026-03"
    assert v["cov_n_periods"] == 5
    assert "period_from" not in v and "period_to" not in v


# ---------------------------------------------------------------------------
# 3. the sandbox and the retry scrub pass the message through
# ---------------------------------------------------------------------------
LITERAL_CODE = ("df = dfs['t']\n"
                "r = pdc_diag.coverage_check(df, 'month', dims=['cur'], metrics=['amt'])\n"
                "RESULT = r[\"values\"][\"from_period\"]")
DYNAMIC_CODE = ("df = dfs['t']\n"
                "r = pdc_diag.coverage_check(df, 'month', dims=['cur'], metrics=['amt'])\n"
                "k = \"from\" + \"_period\"\n"
                "RESULT = r[\"values\"][k]")
KEEP = ["t", "month", "cur", "amt", "amt2"]


def _expected_error() -> str:
    keys = sorted(pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"])["values"])
    assert "period_from" in keys
    return "KeyError: " + repr(_message("from_period", "coverage_check", "values", keys))


def test_python_site_passes_message_through():
    out = code_exec._execute_in_process(LITERAL_CODE, {"t": _frame()}, sid="t")
    assert out.get("error") == _expected_error()


def test_plot_site_passes_message_through():
    out = plot_utils._render_in_process(LITERAL_CODE, {"t": _frame()}, "t")
    assert out.get("ok") is False
    assert out.get("error") == _expected_error()


def test_scrub_keeps_message_byte_identical_for_literal_key():
    err = code_exec._execute_in_process(LITERAL_CODE, {"t": _frame()}, sid="t").get("error")
    assert err == _expected_error()
    assert brain_client._scrub_error_msg(err, keep=KEEP, code=LITERAL_CODE) == err


def test_scrub_dynamic_key_only_scrubs_the_key():
    err = code_exec._execute_in_process(DYNAMIC_CODE, {"t": _frame()}, sid="t").get("error")
    assert err == _expected_error()
    scrubbed = brain_client._scrub_error_msg(err, keep=KEEP, code=DYNAMIC_CODE)
    assert scrubbed == err.replace("'from_period'", "'<value>'", 1)
    marker = "available keys: "
    assert scrubbed.split(marker, 1)[1] == err.split(marker, 1)[1]
    assert scrubbed.split(marker, 1)[1].rstrip('"').split(", ") == sorted(
        pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"])["values"])


# ---------------------------------------------------------------------------
# 1c. a key the code adds later is never listed (Article II)
# ---------------------------------------------------------------------------
# Generated code may add data-derived keys (`r["values"]["total_" + cur]`):
# the listed names are the ones pdc_diag set when it built the dict, so a
# category value never reaches the KeyError text (nor the retry error_msg,
# whose unquoted key list the scrub keeps).
def _parts(r):
    return {"result": r, "values": r["values"], "findings": r["findings"]}


@pytest.mark.parametrize("fn", FUNCTIONS)
@pytest.mark.parametrize("part", ["result", "values", "findings"])
def test_keys_added_later_are_never_listed(fn, part):
    r = _run(fn)
    mapping = _parts(r)[part]
    original = sorted(str(k) for k in mapping)
    mapping["total_UZS"] = 4696
    mapping[7] = "x"                                   # a non-str key
    mapping[("UZS", "USD")] = 1                        # an odd key
    err = _raised(mapping, "zz_missing")
    _assert_message(err, _message("zz_missing", fn, part, original))
    msg = err.args[0]
    for leak in ("UZS", "USD", "total_UZS", "7"):
        assert leak not in msg


def test_keys_deleted_later_are_still_listed():
    r = pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"])
    original = sorted(r["values"])
    del r["values"]["first_period"]
    _assert_message(_raised(r["values"], "first_period"),
                    _message("first_period", "coverage_check", "values", original))


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_deepcopy_and_pickle_keep_the_original_names(fn):
    r = _run(fn)
    original = sorted(r["values"])
    names = r["values"]._names
    r["values"]["total_UZS"] = 1
    expected = _message("from_period", fn, "values", original)
    for clone in (copy.deepcopy(r["values"]), pickle.loads(pickle.dumps(r["values"])),
                  pickle.loads(pickle.dumps(r["values"], protocol=0)),
                  copy.deepcopy(r)["values"], pickle.loads(pickle.dumps(r))["values"]):
        assert clone._names == names
        assert "total_UZS" in clone
        _assert_message(_raised(clone, "from_period"), expected)


def test_a_bare_instance_lists_none():
    bare = pdc_diag._ResultDict.__new__(pdc_diag._ResultDict)
    _assert_message(_raised(bare, "x"), "'x' is not a key of pdc_diag result; available keys: none")


GROUPBY_CODE = ("df = dfs['t']\n"
                "r = pdc_diag.coverage_check(df, 'month', dims=['cur'], metrics=['amt'])\n"
                "for cur, sub in df.groupby('cur'):\n"
                "    r['values']['total_' + str(cur)] = float(sub['amt'].sum())\n"
                "    r['findings']['seen_' + str(cur)] = True\n"
                "    r['by_' + str(cur)] = len(sub)\n"
                "RESULT = r[\"values\"][\"from_period\"]")


def test_sandbox_data_derived_keys_never_reach_the_error():
    err = code_exec._execute_in_process(GROUPBY_CODE, {"t": _frame()}, sid="t").get("error")
    assert err == _expected_error()
    scrubbed = brain_client._scrub_error_msg(err, keep=KEEP, code=GROUPBY_CODE)
    assert scrubbed == err
    for text in (err, scrubbed):
        for leak in ("UZS", "USD", "total_", "seen_", "by_"):
            assert leak not in text


# ---------------------------------------------------------------------------
# 4. `VALUES = r["values"]` / `FINDINGS = r["findings"]` are captured
# ---------------------------------------------------------------------------
# The sandbox capture (`exec_transport.encode_values` / `encode_findings`)
# reads an exact dict OR pdc_diag's own `_ResultDict` (type identity), both
# through `dict.items`; any other dict subclass is still one drop, and the
# main-app decode (plain JSON dicts) is unchanged.
REBIND_CODE = ("df = dfs['t']\n"
               "r = pdc_diag.coverage_check(df, 'month', dims=['cur'], metrics=['amt'])\n"
               "VALUES = r['values']\n"
               "FINDINGS = r['findings']\n")
PLOT_TAIL = "plt.figure()\nplt.bar(df['cur'], df['amt'])\n"
SUBCLASS_CODE = ("class D(dict):\n"
                 "    pass\n"
                 "VALUES = D(a=1)\n"
                 "FINDINGS = D(ok=True)\n")
RESULT_SUBCLASS_CODE = ("r = pdc_diag.coverage_check(dfs['t'], 'month', dims=['cur'], metrics=['amt'])\n"
                        "class E(type(r['values'])):\n"
                        "    pass\n"
                        "VALUES = E({'a': 1})\n"
                        "FINDINGS = E({'ok': True})\n")


def _direct():
    r = pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"])
    return dict(r["values"]), dict(r["findings"])


def _assert_rebind_captured(out):
    values, findings = _direct()
    assert len(values) > 10 and len(findings) > 0
    assert out["values"] == values and out["values_dropped"] == 0
    assert out["findings"] == findings and out["findings_dropped"] == 0
    assert type(out["values"]) is dict and type(out["findings"]) is dict


def test_python_site_captures_a_pdc_diag_result_rebind():
    out = code_exec._execute_in_process(REBIND_CODE + "RESULT = 1\n", {"t": _frame()}, sid="t")
    assert out.get("error") is None, out.get("error")
    _assert_rebind_captured(out)


def test_plot_site_captures_a_pdc_diag_result_rebind():
    out = plot_utils._render_in_process(REBIND_CODE + PLOT_TAIL, {"t": _frame()}, "t")
    assert out.get("ok") is True, out.get("error")
    _assert_rebind_captured(out)


@pytest.mark.parametrize("code", [SUBCLASS_CODE, RESULT_SUBCLASS_CODE], ids=["dict_sub", "result_sub"])
def test_python_site_still_refuses_another_dict_subclass(code):
    out = code_exec._execute_in_process(code + "RESULT = 1\n", {"t": _frame()}, sid="t")
    assert out.get("error") is None, out.get("error")
    assert out["values"] == {} and out["values_dropped"] == 1
    assert out["findings"] == {} and out["findings_dropped"] == 1


@pytest.mark.parametrize("code", [SUBCLASS_CODE, RESULT_SUBCLASS_CODE], ids=["dict_sub", "result_sub"])
def test_plot_site_still_refuses_another_dict_subclass(code):
    out = plot_utils._render_in_process(code + "df = dfs['t']\n" + PLOT_TAIL, {"t": _frame()}, "t")
    assert out.get("ok") is True, out.get("error")
    assert out["values"] == {} and out["values_dropped"] == 1
    assert out["findings"] == {} and out["findings_dropped"] == 1


def test_main_app_decode_still_refuses_a_result_dict():
    r = pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"])
    assert exec_transport._pdc_result_type() is pdc_diag._ResultDict
    assert exec_transport.decode_values(r["values"]) == ({}, 1)
    assert exec_transport.decode_findings(r["findings"]) == ({}, 1)
    assert exec_transport.encode_values(r["values"]) == (dict(r["values"]), 0)
    assert exec_transport.encode_findings(r["findings"]) == (dict(r["findings"]), 0)


def test_result_dict_methods_never_run_in_the_capture(monkeypatch):
    values = pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"])["values"]
    plain = dict.copy(values)

    def boom(*_a, **_k):
        raise AssertionError("a _ResultDict method ran")

    for name in ("items", "__iter__", "keys", "values", "__getitem__", "get"):
        monkeypatch.setattr(pdc_diag._ResultDict, name, boom, raising=False)
    kept, dropped = exec_transport.encode_values(values)
    assert dropped == 0 and kept == plain


def test_pdc_diag_unavailable_falls_back_to_exact_dict_only(monkeypatch, caplog):
    r = pdc_diag.coverage_check(_frame(), "month", dims=["cur"], metrics=["amt"])
    caplog.set_level(logging.DEBUG, logger="datachat")
    # a fresh, unresolved cache (monkeypatch restores the primed one after)
    monkeypatch.setattr(exec_transport, "_PDC_RESULT_TYPE", [])
    monkeypatch.setitem(sys.modules, "pdc_diag", None)       # import pdc_diag -> ImportError
    assert exec_transport._pdc_result_type() is None
    assert exec_transport.encode_values(r["values"]) == ({}, 1)
    assert exec_transport.encode_values({"a": 1}) == ({"a": 1}, 0)
    lines = [rec.getMessage() for rec in caplog.records
             if "PDC_DIAG_RESULT_TYPE_UNAVAILABLE" in rec.getMessage()]
    assert len(lines) == 1                                    # None is cached: logged once


def test_result_type_is_resolved_once_per_process(monkeypatch):
    monkeypatch.setattr(exec_transport, "_PDC_RESULT_TYPE", [])
    kind = pdc_diag._ResultDict
    assert exec_transport._pdc_result_type() is kind
    # a later change to the module is never looked at again
    monkeypatch.delattr(pdc_diag, "_ResultDict")
    assert exec_transport._pdc_result_type() is kind
    assert exec_transport._PDC_RESULT_TYPE == [kind]


# ---------------------------------------------------------------------------
# 5. the lookup happens BEFORE the job's code: a module hook never runs
# ---------------------------------------------------------------------------
# `setattr(pdc_diag, ...)` / `delattr(pdc_diag, ...)` are not caught by the
# redefinition guard. Code that deletes `_ResultDict` and installs a module
# `__getattr__` must not get that function called by the capture: both exec
# sites resolve the class before anything runs. The in-process tests share
# the real module, so the fixture puts every attribute back.
HOOK_CODE = ("df = dfs['t']\n"
             "r = pdc_diag.coverage_check(df, 'month', dims=['cur'], metrics=['amt'])\n"
             "calls = []\n"
             "def hook(name):\n"
             "    calls.append(name)\n"
             "    raise AttributeError(name)\n"
             "setattr(pdc_diag, 'zq_hook_calls', calls)\n"
             "delattr(pdc_diag, '_ResultDict')\n"
             "setattr(pdc_diag, '__getattr__', hook)\n"
             "VALUES = r['values']\n"
             "FINDINGS = r['findings']\n")


@pytest.fixture
def restore_pdc_diag(monkeypatch):
    saved = dict(vars(pdc_diag))
    # unresolved: the exec site's own call must be the one that resolves it
    monkeypatch.setattr(exec_transport, "_PDC_RESULT_TYPE", [])
    try:
        yield
    finally:
        for name in list(vars(pdc_diag)):
            if name not in saved:
                delattr(pdc_diag, name)
        for name, value in saved.items():
            if vars(pdc_diag).get(name, object()) is not value:
                setattr(pdc_diag, name, value)


def _assert_hook_never_ran(out, expected):
    assert "__getattr__" in vars(pdc_diag) and "_ResultDict" not in vars(pdc_diag)
    assert vars(pdc_diag)["zq_hook_calls"] == []
    values, findings = expected
    assert out["values"] == values and out["values_dropped"] == 0
    assert out["findings"] == findings and out["findings_dropped"] == 0


def test_python_site_never_runs_a_module_hook(restore_pdc_diag):
    expected = _direct()          # before the code deletes `_ResultDict`
    out = code_exec._execute_in_process(HOOK_CODE + "RESULT = 1\n", {"t": _frame()}, sid="t")
    assert out.get("error") is None, out.get("error")
    _assert_hook_never_ran(out, expected)


def test_plot_site_never_runs_a_module_hook(restore_pdc_diag):
    expected = _direct()
    out = plot_utils._render_in_process(HOOK_CODE + PLOT_TAIL, {"t": _frame()}, "t")
    assert out.get("ok") is True, out.get("error")
    _assert_hook_never_ran(out, expected)
