"""What column values may reach the brain (data-boundary policy: no row data;
low-cardinality categorical values are acceptable).

- schema text: a text column with <= 20 distinct values lists them, each cut
  to 40 characters; any other text column sends its distinct COUNT only; a
  numeric column sends its dtype only.
- technical descriptions (computed once at upload / refresh): the same rule;
  ones stored by an earlier release are filtered as plain strings when the
  schema text is built — no frame is re-read per question.
- SCHEMA_VALUE_DENY_COLUMNS: named columns send no value anywhere (schema
  text, technical description, dataset profile, schema autofill).
- /v1/summarize previews are capped (dict 20 keys, string 500 chars) and
  /v1/title gets the first 300 characters of the answer.
"""
import json

import pandas as pd
import pytest

import brain_client
import dataset_profile
import schema_builder
from settings import settings

LONG = "L" * 90


def _text(dfs, docs=None, monkeypatch=None):
    return schema_builder._schema_text_uncached(docs or {}, dfs)


def _types(text: str) -> dict:
    line = next(l for l in text.splitlines() if l.startswith("Column Types: "))
    return json.loads(line[len("Column Types: "):])


def _tech(text: str) -> dict:
    lines = [l for l in text.splitlines() if l.startswith("Column Technical Info: ")]
    return json.loads(lines[0][len("Column Technical Info: "):]) if lines else {}


@pytest.fixture(autouse=True)
def _no_deny(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_VALUE_DENY_COLUMNS", "")


def test_high_cardinality_text_and_numeric_columns_send_no_values():
    df = pd.DataFrame({"name": [f"customer-{i}" for i in range(50)],
                       "balance": [1000.5 + i for i in range(50)]})
    text = _text({"f.csv": df})
    types = _types(text)
    assert types["name"] == "object (50 unique)", types
    assert types["balance"] == "float64", types
    assert "customer-" not in text and "1000.5" not in text


def test_low_cardinality_values_are_listed_and_cut_to_40_chars():
    df = pd.DataFrame({"region": ["East", "West", LONG] * 3})
    types = _types(_text({"f.csv": df}))
    assert types["region"].startswith("CATEGORICAL (3 unique values: ")
    assert "L" * 40 in types["region"] and "L" * 41 not in types["region"]


def test_a_denied_column_sends_its_dtype_only(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_VALUE_DENY_COLUMNS", " Region , iban")
    df = pd.DataFrame({"region": ["East", "West"] * 3, "IBAN": ["GE1", "GE2"] * 3,
                       "city": ["Tbilisi", "Batumi"] * 3})
    docs = {"f.csv": {"fields": {
        "region": {"technical_description": "object, 6/6 filled, CATEGORICAL (2 unique: East, West)",
                   "values": {"East": "eastern"}},
        "IBAN": {"technical_description": "object, 6/6 filled, CATEGORICAL (2 unique: GE1, GE2)"}}}}
    text = _text({"f.csv": df}, docs)
    types = _types(text)
    assert types["region"] == "object" and types["IBAN"] == "object", types
    assert types["city"].startswith("CATEGORICAL")
    for secret in ("East", "West", "GE1", "GE2", "eastern"):
        assert secret not in text, secret
    assert _tech(text)["region"] == "object, 6/6 filled"


@pytest.mark.parametrize("stored,expected", [
    ("int64, 3/3 filled, sample: 101, 202, 303", "int64, 3/3 filled"),
    ("object, 50/50 filled, 50 unique, sample: ann, bob, cy, dan, eve", "object, 50/50 filled, 50 unique"),
    ("object, 6/6 filled, CATEGORICAL (2 unique: East, " + LONG + ")",
     "object, 6/6 filled, CATEGORICAL (2 unique: East, " + "L" * 40 + ")"),
    ("bool, 3/3 filled, boolean", "bool, 3/3 filled, boolean"),
])
def test_descriptions_stored_by_an_earlier_release_are_filtered(stored, expected):
    df = pd.DataFrame({"c": [1, 2, 3]})
    docs = {"f.csv": {"fields": {"c": {"technical_description": stored}}}}
    assert _tech(_text({"f.csv": df}, docs))["c"] == expected


def test_the_filter_reads_no_frame(monkeypatch):
    """The stored-description filter is string work only (performance: never
    a full-frame recompute per question)."""
    calls = []
    monkeypatch.setattr(dataset_profile, "_generate_technical_description",
                        lambda *a, **k: calls.append(1) or "x")
    df = pd.DataFrame({"c": [1, 2, 3]})
    docs = {"f.csv": {"fields": {"c": {"technical_description": "int64, 3/3 filled, sample: 1, 2, 3"}}}}
    _text({"f.csv": df}, docs)
    assert calls == []


# ---------------------------------------------------------------- technical description
def test_technical_description_numeric_has_no_sample():
    assert dataset_profile._generate_technical_description(pd.Series([1, 2, 3]), 3) == "int64, 3/3 filled"


def test_technical_description_high_cardinality_text_has_a_count_only():
    s = pd.Series([f"v{i}" for i in range(30)])
    assert dataset_profile._generate_technical_description(s, 30) == "object, 30/30 filled, 30 unique"


def test_technical_description_values_are_cut_to_40():
    out = dataset_profile._generate_technical_description(pd.Series(["a", LONG]), 2)
    assert "L" * 40 in out and "L" * 41 not in out


def test_technical_description_of_a_denied_column(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_VALUE_DENY_COLUMNS", "segment")
    out = dataset_profile._generate_technical_description(pd.Series(["a", "b"]), 2, name="Segment")
    assert out == "object, 2/2 filled"


# ---------------------------------------------------------------- dataset profile
def test_profile_top_values_only_for_low_cardinality_and_not_denied(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_VALUE_DENY_COLUMNS", "iban")
    df = pd.DataFrame({"name": [f"n{i}" for i in range(40)],
                       "region": ["E", "W"] * 20,
                       "iban": ["GE1", "GE2"] * 20})
    cols = dataset_profile.compute_profile(df)["columns"]
    assert "top_values" not in cols["name"]
    assert "top_values" in cols["region"]
    assert "top_values" not in cols["iban"]


def test_a_denied_column_has_no_min_max_and_no_constant_value(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_VALUE_DENY_COLUMNS", "salary,flag")
    df = pd.DataFrame({"salary": [1234567.0, 7654321.0] * 10,
                       "flag": ["SECRET-FLAG"] * 20,
                       "amount": [1, 2] * 10})
    prof = dataset_profile.compute_profile(df)
    cols = prof["columns"]
    assert "min" not in cols["salary"] and "max" not in cols["salary"]
    assert "min" in cols["amount"]
    assert "flag is constant" in prof["warnings"]
    assert "SECRET-FLAG" not in json.dumps(prof, default=str)


def test_a_stored_profile_is_filtered_in_transport(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_VALUE_DENY_COLUMNS", "iban")
    stored = {"t": {"rows": 50, "warnings": ["iban is constant: every value = GE00XX",
                                             "status is constant: every value = open"],
                    "columns": {
                        "name": {"dtype": "object", "nunique": 50,
                                 "top_values": [["Nino Beridze", 1]]},
                        "region": {"dtype": "object", "nunique": 2,
                                   "top_values": [["East", 25], ["West", 25]]},
                        "iban": {"dtype": "object", "nunique": 1, "constant": True,
                                 "min": "GE00XX", "max": "GE00XX",
                                 "top_values": [["GE00XX", 50]]}}}}
    out = brain_client._compact_profiles_for_transport(stored)["t"]
    blob = json.dumps(out)
    assert "Nino Beridze" not in blob and "GE00XX" not in blob
    assert out["columns"]["region"]["top_values"] == [["East", 25], ["West", 25]]
    assert out["columns"]["iban"] == {"dtype": "object", "nunique": 1, "constant": True}
    assert "iban is constant" in out["warnings"]
    assert "status is constant: every value = open" in out["warnings"]


# ---------------------------------------------------------------- schema autofill
def test_autofill_sends_no_values_for_a_denied_column(monkeypatch):
    from routes.upload import _prepare_file_context
    monkeypatch.setattr(settings, "SCHEMA_VALUE_DENY_COLUMNS", "region")
    df = pd.DataFrame({"region": ["East", "West"] * 5, "city": ["A", "B"] * 5})
    ctx = _prepare_file_context("f.csv", df, {}, "")
    hints = json.dumps(ctx, default=str)
    assert "East" not in hints and "West" not in hints
    assert "A" in json.dumps(ctx.get("unique_hints", {}).get("city", []))


def test_autofill_profile_of_a_denied_high_cardinality_column_is_value_free(monkeypatch):
    from routes.upload import _prepare_file_context
    monkeypatch.setattr(settings, "SCHEMA_VALUE_DENY_COLUMNS", "iban")
    ibans = [f"GE{i:02d}TB{i:010d}" for i in range(60)]
    df = pd.DataFrame({"iban": ibans, "note": [f"Payment ref {i}" for i in range(60)]})
    ctx = _prepare_file_context("f.csv", df, {}, "")
    hint = json.dumps(ctx.get("unique_hints", {}).get("iban"))
    assert "[profile:" in hint
    for word in ("mask", "prefix", "len", "GE", "TB"):
        assert word not in hint, word
    assert "Payment" in json.dumps(ctx.get("unique_hints", {}).get("note"))


# ---------------------------------------------------------------- transport caps
@pytest.fixture
def captured(monkeypatch):
    seen = []

    def fake_post(path, payload, *a, **k):
        seen.append((path, payload))
        return {"text": "ok", "title": "t", "usage": {}}

    monkeypatch.setattr(brain_client, "_post", fake_post)
    return seen


def _summarize(preview):
    brain_client.summarize("sid", "q", "schema", [], preview, None, "u@x.com")


def test_a_long_string_preview_is_cut_and_marked(captured):
    _summarize("x" * 5000)
    sent = captured[-1][1]["preview"]
    assert len(sent) == 500 + len("…[truncated]") and sent.endswith("…[truncated]")


def test_a_wide_dict_preview_keeps_20_keys_and_counts_the_rest(captured):
    _summarize({f"k{i}": i for i in range(5000)})
    sent = captured[-1][1]["preview"]
    assert len(sent) == 21 and sent["_truncated_keys"] == 4980


def test_a_small_preview_passes_unchanged(captured):
    _summarize({"total": 12.5, "label": "east"})
    assert captured[-1][1]["preview"] == {"total": 12.5, "label": "east"}


def test_the_title_call_sends_at_most_300_answer_characters(captured):
    brain_client.title("sid", "q", "a" * 10_000)
    assert len(captured[-1][1]["answer"]) == 300


def test_the_preview_cap_never_raises():
    class Bad(dict):
        def items(self):
            raise RuntimeError("boom")

    assert brain_client._compact_preview_for_transport(Bad(a=1)) is None
