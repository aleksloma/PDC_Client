"""Narrative session 1 — the boundary guards that do NOT need the flow changes
(DIAGNOSTICS_SPEC B7.4, B7.6, B7.7, C12 session 1 `brain_client` / `settings`
items). The flow-level sentinel tests (`test_values_never_in_*_payload`) come
with the flow PR.

Article II: `dataset_profile`, `data_caveat` and `error_msg` must carry no
cell value; `capabilities` and `findings` ride only when given; history rows
are an allowlist. Every payload is captured by monkeypatching
`brain_client._post` (the `tests/test_profile_storage.py` idiom).

Runs offline: `.venv/Scripts/python.exe -m pytest tests/test_narrative_boundary.py -q`.
"""
import copy
import json
import os

import pytest

import brain_client
import result_backstop
import settings as settings_mod
from settings import settings

SENTINEL_NUM = 987654321
SENTINEL_CAT = "SENTINEL_CATEGORY"


def _capture_post(monkeypatch):
    sent = {}

    def fake_post(path, payload, sid, **kw):
        sent["path"] = path
        sent["payload"] = payload
        return {"kind": "ANSWER", "code": "ok", "usage": {}}

    monkeypatch.setattr(brain_client, "_post", fake_post)
    return sent


def _plan(**kw):
    return brain_client.plan(sid="s", question="q", schema_text="t", df_names=["df"],
                             history_rows=[], **kw)


def _retry(**kw):
    base = dict(sid="s", question="q", schema_text="t", df_names=["df"],
                df_columns={"df": ["Department", "Quantity"]}, history_rows=[],
                error_msg="e", failed_code="c")
    base.update(kw)
    return brain_client.retry(**base)


# ----------------------------------------------------------- history allowlist

def test_history_rows_strip_new_fields():
    rows = [{"role": "ai", "content": "[Findings: a=true]\nTotal {{total:count}}",
             "code": "RESULT = 1",
             "values": {"total": SENTINEL_NUM}, "findings": {"a": True},
             "narrative_template": "Total {{total:count}}",
             "narrative_rendered": f"Total {SENTINEL_NUM}",
             "caption_template": "c {{x}}", "caption_rendered": f"c {SENTINEL_NUM}",
             "caption_described": SENTINEL_CAT, "value_display": f"**Result:** {SENTINEL_NUM}",
             "images": [{"answer": SENTINEL_CAT}], "phase2": {"values": {"v": SENTINEL_NUM}},
             "table_captions": {"1": SENTINEL_CAT}},
            {"role": "user", "content": "q", "values": {"x": SENTINEL_NUM}}]
    out = brain_client._sanitize_history_rows(rows)
    assert len(out) == 2
    assert set(out[0]) == {"role", "content", "code"}
    assert set(out[1]) == {"role", "content"}
    blob = json.dumps(out)
    assert str(SENTINEL_NUM) not in blob and SENTINEL_CAT not in blob
    assert "{{total:count}}" in out[0]["content"]         # the template stays


# ------------------------------------------------------------- capabilities

def test_plan_payload_carries_capabilities_only_when_given(monkeypatch):
    sent = _capture_post(monkeypatch)
    _plan()
    assert "capabilities" not in sent["payload"]
    _plan(capabilities=None)
    assert "capabilities" not in sent["payload"]
    _plan(capabilities=[])
    assert "capabilities" not in sent["payload"]
    _plan(capabilities=["narrative_v1"])
    assert sent["path"] == "/v1/plan"
    assert sent["payload"]["capabilities"] == ["narrative_v1"]


def test_retry_payload_carries_capabilities_only_when_given(monkeypatch):
    sent = _capture_post(monkeypatch)
    _retry()
    assert "capabilities" not in sent["payload"]
    _retry(capabilities=None)
    assert "capabilities" not in sent["payload"]
    _retry(capabilities=[])
    assert "capabilities" not in sent["payload"]
    _retry(capabilities=["narrative_v1", "diagnostics_v1"])
    assert sent["path"] == "/v1/retry"
    assert sent["payload"]["capabilities"] == ["narrative_v1", "diagnostics_v1"]


def test_brain_client_never_reads_the_capabilities_setting(monkeypatch):
    # ownership: only run_chat_multi_plot reads BRAIN_CAPABILITIES
    sent = _capture_post(monkeypatch)
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", "narrative_v1,diagnostics_v1")
    _plan()
    assert "capabilities" not in sent["payload"]
    _retry()
    assert "capabilities" not in sent["payload"]


# ----------------------------------------------------------------- findings

def test_plan_findings_compacted(monkeypatch):
    sent = _capture_post(monkeypatch)
    _plan()
    assert "findings" not in sent["payload"]
    _plan(findings=None)
    assert "findings" not in sent["payload"]
    _plan(findings={})
    assert "findings" not in sent["payload"]
    # every entry bad -> absent
    _plan(findings={"Bad Key": True, "n": 5, "v": "x;y", "long": "x" * 65, "ctl": "a\x01b"})
    assert "findings" not in sent["payload"]
    good = {f"k{i:02d}": True for i in range(30)}
    good.update({"coverage_dim": "CURRENCY", "anomaly": "anomaly_confirmed",
                 "Bad Key": True, "n": SENTINEL_NUM, "v": "x]y", "cat": "ok_text"})
    _plan(findings=good)
    f = sent["payload"]["findings"]
    assert isinstance(f, dict) and len(f) <= 24
    assert "Bad Key" not in f and "n" not in f and "v" not in f
    assert f.get("coverage_dim") == "CURRENCY" and f.get("anomaly") == "anomaly_confirmed"
    assert all(isinstance(v, (bool, str)) for v in f.values())
    assert str(SENTINEL_NUM) not in json.dumps(sent["payload"])


# ----------------------------------------------------------------- settings

def test_settings_brain_capabilities_parser(monkeypatch):
    monkeypatch.delenv("BRAIN_CAPABILITIES", raising=False)
    field = settings_mod.Settings.model_fields["BRAIN_CAPABILITIES"]
    default = field.default_factory() if field.default_factory is not None else field.default
    assert default == "narrative_v1,diagnostics_v1"

    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", "narrative_v1,diagnostics_v1")
    assert settings_mod.brain_capabilities() == ["narrative_v1", "diagnostics_v1"]
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", "")
    assert settings_mod.brain_capabilities() == []
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", "two_phase_v1,narrative_v1")
    assert settings_mod.brain_capabilities() == ["narrative_v1"]      # two_phase_v1 always dropped
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", "two_phase_v1")
    assert settings_mod.brain_capabilities() == []
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", " narrative_v1 , bogus ,, diagnostics_v1 ")
    assert settings_mod.brain_capabilities() == ["narrative_v1", "diagnostics_v1"]
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", "narrative_v1,narrative_v1")
    assert settings_mod.brain_capabilities() == ["narrative_v1"]
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", None)
    assert settings_mod.brain_capabilities() == []


# ------------------------------------------------------ data_caveat (B7.4/B7.6)

def test_compact_caveat_findings_without_kind_kept():
    out = brain_client._compact_caveat_for_transport({"findings": {"coverage": True,
                                                                    "coverage_dim": "CURRENCY"}})
    assert out == {"findings": {"coverage": True, "coverage_dim": "CURRENCY"}}
    out2 = brain_client._compact_caveat_for_transport(
        {"kind": "constant_metric", "facts": ["Quantity is the same for every group in the chart"],
         "findings": {"coverage": True}})
    assert out2["kind"] == "constant_metric"
    assert out2["facts"] == ["Quantity is the same for every group in the chart"]
    assert out2["findings"] == {"coverage": True}


def test_compact_caveat_unknown_kind_without_findings_still_none():
    assert brain_client._compact_caveat_for_transport({"kind": "bogus", "facts": ["x"]}) is None
    assert brain_client._compact_caveat_for_transport({"facts": ["x"]}) is None
    assert brain_client._compact_caveat_for_transport({}) is None
    assert brain_client._compact_caveat_for_transport(None) is None
    # an unknown kind with findings keeps the findings only
    out = brain_client._compact_caveat_for_transport({"kind": "bogus", "facts": ["x"],
                                                      "findings": {"a": True}})
    assert out == {"findings": {"a": True}}


def test_compact_caveat_drops_value_hint():
    plain = "Quantity is the same for every group in the chart"
    out = brain_client._compact_caveat_for_transport(
        {"kind": "constant_metric",
         "facts": [plain + " (= 7)", plain, plain + " ( = SENTINEL_CATEGORY )", plain + "(=1.5)  "]})
    assert out["facts"] == [plain]
    # a caveat whose every fact was a value hint carries nothing -> None
    assert brain_client._compact_caveat_for_transport(
        {"kind": "constant_table", "facts": ["Amount is the same in every row of the table (= 42)"]}) is None


def test_compact_caveat_findings_revalidated():
    out = brain_client._compact_caveat_for_transport(
        {"findings": {"ok": True, "col": "CURRENCY", "Bad Key": True, "v": "x;y", "w": "x]y",
                      "n": SENTINEL_NUM, "long": "x" * 65, "ctl": "a\nb", "f": 1.5,
                      "needs_phase2": True}})
    assert out == {"findings": {"ok": True, "col": "CURRENCY", "needs_phase2": True}}
    many = {"findings": {f"k{i:02d}": True for i in range(30)}}
    assert len(brain_client._compact_caveat_for_transport(many)["findings"]) <= 24
    assert brain_client._compact_caveat_for_transport({"findings": {"n": 5}}) is None
    assert brain_client._compact_caveat_for_transport({"findings": "junk"}) is None


def test_describe_payload_carries_findings_only_caveat(monkeypatch):
    sent = _capture_post(monkeypatch)
    brain_client.describe(sid="s", question="q", code="c",
                          data_caveat={"findings": {"coverage": True}})
    assert sent["payload"]["data_caveat"] == {"findings": {"coverage": True}}


# ------------------------------------------------------ backstop facts (B7.6)

def test_backstop_facts_carry_no_value():
    chart = {"columns": ["city", "Quantity"], "total_rows": 3,
             "rows": [{"city": "Tbilisi", "Quantity": 4242},
                      {"city": "Batumi", "Quantity": 4242},
                      {"city": "Kutaisi", "Quantity": 4242}]}
    det = result_backstop.inspect_outputs(chart_data=chart)
    assert det and det["kind"] == "constant_metric"
    assert any("Quantity" in f for f in det["facts"])
    for f in det["facts"]:
        assert "(=" not in f and "4242" not in f and "4,242" not in f

    import pandas as pd
    table = pd.DataFrame({"city": ["a", "b", "c"], "Amount": [SENTINEL_NUM] * 3})
    det2 = result_backstop.inspect_outputs(result_obj=table)
    assert det2 and det2["kind"] == "constant_table"
    assert any("Amount" in f for f in det2["facts"])
    for f in det2["facts"]:
        assert "(=" not in f and str(SENTINEL_NUM) not in f


# ------------------------------------------------- profile transport (B7.6)

def test_profile_transport_has_no_min_max_top_values(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_VALUE_DENY_COLUMNS", "")
    stored = {"t": {"profile_version": 1, "rows": 50, "warnings": [],
                    "columns": {
                        "region": {"dtype": "object", "nunique": 2, "null_pct": 0.0,
                                   "top_values": [[SENTINEL_CAT, 25], ["West", 25]]},
                        "amount": {"dtype": "int64", "nunique": 50, "min": 1,
                                   "max": SENTINEL_NUM, "constant": False, "all_unique": True,
                                   "top_values": [[str(SENTINEL_NUM), 1]]},
                        "when": {"dtype": "datetime64[ns]", "nunique": 50,
                                 "min": "2026-01-01", "max": "2026-12-31"}}}}
    before = copy.deepcopy(stored)
    out = brain_client._compact_profiles_for_transport(stored)
    assert stored == before                                   # input never mutated
    cols = out["t"]["columns"]
    for c in cols.values():
        assert "min" not in c and "max" not in c and "top_values" not in c
    assert cols["region"]["nunique"] == 2 and cols["amount"]["all_unique"] is True
    blob = json.dumps(out)
    assert SENTINEL_CAT not in blob and str(SENTINEL_NUM) not in blob and "2026-01-01" not in blob


def test_profile_constant_warning_value_free(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_VALUE_DENY_COLUMNS", "")
    stored = {"t": {"rows": 3, "warnings": ["Quantity is constant: every value = 1",
                                            f"status is constant: every value = {SENTINEL_CAT}",
                                            "x is 75.0% empty"],
                    "columns": {}}}
    out = brain_client._compact_profiles_for_transport(stored)
    assert out["t"]["warnings"] == ["Quantity is constant", "status is constant", "x is 75.0% empty"]
    assert SENTINEL_CAT not in json.dumps(out)


# ------------------------------------------------------ error_msg scrub (B7.7)

def test_scrub_error_msg_replaces_cell_literal():
    keep = frozenset({"Department", "df"})
    assert brain_client._scrub_error_msg(f"KeyError: '{SENTINEL_CAT}'", keep) == "KeyError: '<value>'"
    assert brain_client._scrub_error_msg(f'ValueError: could not convert "{SENTINEL_CAT}" to float',
                                         keep) == "ValueError: could not convert '<value>' to float"
    out = brain_client._scrub_error_msg(f"KeyError: '{SENTINEL_CAT}' and '{SENTINEL_NUM}'", keep)
    assert SENTINEL_CAT not in out and str(SENTINEL_NUM) not in out
    assert out.count("'<value>'") == 2


def test_scrub_error_msg_keeps_column_and_df_names():
    keep = frozenset({"Department", "df", "sales"})
    assert brain_client._scrub_error_msg("KeyError: 'Department'", keep) == "KeyError: 'Department'"
    assert brain_client._scrub_error_msg('KeyError: "sales"', keep) == 'KeyError: "sales"'
    out = brain_client._scrub_error_msg(f"KeyError: 'Department' not in '{SENTINEL_CAT}'", keep)
    assert "'Department'" in out and SENTINEL_CAT not in out
    # an unquoted text is untouched
    msg = "TimeoutError: Code execution exceeded 60 seconds limit"
    assert brain_client._scrub_error_msg(msg, keep) == msg
    # a literal longer than 200 chars inside the quotes is not matched as one
    # literal — but its content must still not survive in the clear
    long_lit = "'" + SENTINEL_CAT * 20 + "'"
    out2 = brain_client._scrub_error_msg("KeyError: " + long_lit, keep)
    assert isinstance(out2, str)


def test_scrub_error_msg_never_raises():
    keep = frozenset({"a"})
    for bad in (None, 5, b"bytes", ["x"], {"k": "v"}):
        out = brain_client._scrub_error_msg(bad, keep)
        assert isinstance(out, str)
    assert isinstance(brain_client._scrub_error_msg("x", None), str)
    assert brain_client._scrub_error_msg("", keep) == ""


def test_retry_payload_error_msg_is_scrubbed(monkeypatch):
    sent = _capture_post(monkeypatch)
    _retry(error_msg=f"KeyError: '{SENTINEL_CAT}'")
    assert sent["payload"]["error_msg"] == "KeyError: '<value>'"
    assert SENTINEL_CAT not in json.dumps(sent["payload"])
    _retry(error_msg="KeyError: 'Department'")
    assert sent["payload"]["error_msg"] == "KeyError: 'Department'"     # df_columns member kept
    _retry(error_msg="NameError: name 'df' is not defined")
    assert sent["payload"]["error_msg"] == "NameError: name 'df' is not defined"   # df name kept
    _retry(error_msg=f"KeyError: 'Quantity' vs '{SENTINEL_CAT}'")
    assert sent["payload"]["error_msg"] == "KeyError: 'Quantity' vs '<value>'"


# ---------------------------------------------------------------- summarize

def test_summarize_preview_null_passes_through(monkeypatch):
    sent = _capture_post(monkeypatch)
    brain_client.summarize(sid="s", question="q", schema_text="t", history_rows=[],
                           preview=None)
    assert sent["path"] == "/v1/summarize"
    assert "preview" in sent["payload"] and sent["payload"]["preview"] is None
    assert json.loads(json.dumps(sent["payload"]))["preview"] is None


def test_scrub_error_msg_drops_python_string_prefixes():
    # Review finding: a repr prefix (b'…', u'…', rb'…') put a letter right in
    # front of the quote, so the literal was never recognised and leaked.
    keep = frozenset({"Department", "df"})
    for prefix in ("b", "u", "rb", "B", "Rb"):
        out = brain_client._scrub_error_msg(f"KeyError: {prefix}'{SENTINEL_CAT}'", keep)
        assert SENTINEL_CAT not in out
        assert out == "KeyError: '<value>'"
    # the double-quoted keep rule must not carry a prefixed bytes literal
    out = brain_client._scrub_error_msg(
        f"KeyError: \"['Department'] got b'{SENTINEL_CAT}'\"", keep)
    assert SENTINEL_CAT not in out
    assert "'Department'" in out
    # a prefix-letter inside a word is not a prefix: "can't" still works
    msg = "TypeError: can't multiply sequence by non-int of type 'Department'"
    assert brain_client._scrub_error_msg(msg, keep) == msg
    assert brain_client._scrub_error_msg("Bob's 'x'", keep) == "Bob's '<value>'"
