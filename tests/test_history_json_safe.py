"""Regressions for the two manual-test crashes:

  CHAT_PERSIST_ERROR: Object of type Timestamp is not JSON serializable
    — history writers json.dumps'd raw records; result tables carry raw
      pd.Timestamp cells. Fixed by _json_safe at every history write.
  CHAT_THREAD_ERROR: Object of type Styler is not JSON serializable
    — _safe_preview let a dict of Stylers/DataFrames through to the brain
      call's HTTP JSON encoding. Fixed by a strict scalar allow-list.

All offline; DATA_ROOT isolated to tmp_path.
"""
import json

import numpy as np
import pandas as pd
import pytest

import local_store
import run_chat_local


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store.settings, "DATA_ROOT", str(tmp_path))
    return local_store.ChatDataStore("c_jsonsafe")


def _q1_style_record():
    """The exact Q1 shape: an AI turn whose table rows hold raw Timestamps."""
    return {
        "role": "ai",
        "content": "first 10 rows",
        "table": {
            "columns": ["Date", "Store", "Amount"],
            "rows": [
                {"Date": pd.Timestamp("2026-01-05 10:30"), "Store": "Dana Mall",
                 "Amount": np.float64(12.5)},
                {"Date": pd.NaT, "Store": "Dana Mall", "Amount": np.int64(7)},
            ],
            "total_rows": np.int64(121170),
        },
        "usage": {"tokens": np.int64(100)},
        "ts": 1234.5,
    }


def test_append_history_with_timestamps_persists_and_round_trips(store):
    conv = store.new_conversation(title="t")
    store.append_history(conv, _q1_style_record())  # must NOT raise
    hist = store.get_history(conv)
    assert len(hist) == 1
    row0 = hist[0]["table"]["rows"][0]
    assert row0["Date"] == "2026-01-05T10:30:00"  # ISO string, not repr
    assert row0["Amount"] == 12.5
    assert hist[0]["table"]["rows"][1]["Date"] is None  # NaT → None
    assert hist[0]["table"]["total_rows"] == 121170


def test_append_history_with_styler_degrades_to_string(store):
    conv = store.new_conversation(title="t")
    styler = pd.DataFrame({"a": [1, 2]}).style
    store.append_history(conv, {"role": "ai", "content": "x", "weird": styler})
    hist = store.get_history(conv)
    assert isinstance(hist[0]["weird"], str)  # stringified, not crashed


def test_truncate_and_copy_writers_are_safe_too(store):
    conv = store.new_conversation(title="t")
    store.append_history(conv, {"role": "human", "content": "q", "ts": 1.0})
    store.append_history(conv, _q1_style_record())
    kept = store.truncate_conv_history(conv, 1)  # rewrite path must not raise
    assert len(kept) == 1
    new_conv = store.copy_conv_to_new(conv)  # copy path must not raise
    assert len(store.get_history(new_conv)) == 1


def test_safe_preview_scalars_pass():
    assert run_chat_local._safe_preview(42) == 42
    assert run_chat_local._safe_preview("ok") == "ok"
    assert run_chat_local._safe_preview({"total": 10, "note": "x", "none": None}) \
        == {"total": 10, "note": "x", "none": None}


def test_safe_preview_coerces_numpy_scalars():
    out = run_chat_local._safe_preview({"mean": np.float64(1.5), "n": np.int64(3)})
    assert out == {"mean": 1.5, "n": 3}
    json.dumps(out)  # and the result is genuinely serializable


def test_safe_preview_rejects_dict_of_frames_and_stylers():
    df = pd.DataFrame({"a": [1, 2]})
    # The exact Q2 shape: RESULT = {"sales": df1.head(10).style, ...}
    assert run_chat_local._safe_preview({"sales": df.style, "stock": df.style}) is None
    assert run_chat_local._safe_preview({"sales": df, "stock": df}) is None
    assert run_chat_local._safe_preview({"when": pd.Timestamp("2026-01-01")}) is None
    assert run_chat_local._safe_preview(df) is None
    assert run_chat_local._safe_preview([1, 2, 3]) is None


def test_safe_preview_output_always_json_serializable():
    df = pd.DataFrame({"a": [1, 2]})
    for candidate in (None, 1, "s", {"k": np.int64(1)}, {"bad": df.style},
                      {"bad": df}, df, [df]):
        out = run_chat_local._safe_preview(candidate)
        json.dumps(out)  # never raises, whatever came in
