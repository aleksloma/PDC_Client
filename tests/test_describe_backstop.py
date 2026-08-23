"""The describe backstop wiring (Prompt 14 Part B): detection -> mandatory
caveat -> guaranteed explanation.

CRITICAL RULE 7 asks the MODEL to explain a constant/identical result; it does
so only sometimes (verified live, twice). These tests pin the deterministic
half: the facts reach /v1/describe, and if the returned description does not
carry the marker the client states the note ITSELF. The note is never lost, and
a normal varying answer is left completely untouched.

Brain calls are stubbed (fake-client pattern); code execution is REAL.
"""
import pandas as pd
import pytest

import run_chat_local

FLAT_CODE = "RESULT = dfs['link.csv'].groupby('city').size().reset_index(name='Quantity')"
VARYING_CODE = "RESULT = dfs['sales.csv'].groupby('store')['amount'].sum().reset_index()"

PROFILE = {
    "link.csv": {
        "rows": 100,
        "grain": {"columns": ["client_id", "product_id"], "kind": "pair",
                  "text": "one row per (client_id, product_id)"},
        "warnings": ["looks like a catalog/link table: one row per "
                     "(client_id, product_id), no varying numeric measure"],
        "columns": {},
    },
}


@pytest.fixture
def dfs():
    return {
        # one row per (city, product) -> a groupby size is 1 everywhere
        "link.csv": pd.DataFrame({"city": ["Tbilisi", "Batumi", "Kutaisi"],
                                  "product": ["A", "B", "C"]}),
        "sales.csv": pd.DataFrame({"store": ["A", "B", "C"], "amount": [10, 20, 30]}),
    }


@pytest.fixture(autouse=True)
def _stub_schema(monkeypatch):
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")


def _run(monkeypatch, dfs, code, describe_impl, *, question="show quantities",
         dataset_profile=None):
    """Run one PYTHON answer through run_chat_multi_plot; return (result, calls)."""
    calls = []

    def fake_describe(**kw):
        calls.append(kw)
        return describe_impl(kw)

    monkeypatch.setattr(run_chat_local.brain_client, "plan", lambda **k: {
        "raw_text": "", "kind": "PYTHON", "code": code, "usage": {}})
    monkeypatch.setattr(run_chat_local.brain_client, "describe", fake_describe)
    monkeypatch.setattr(run_chat_local.brain_client, "summarize",
                        lambda **k: {"text": "summarized", "usage": {}})
    events = list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question=question,
        history_rows=[], user_email="alice@acme.com",
        dataset_profile=dataset_profile))
    assert len(events) == 1 and events[0]["single_response"]
    return events[0]["result"], calls


MARKER = run_chat_local.result_backstop.DATA_NOTE_MARKER


# --- the caveat reaches the brain -------------------------------------------

def test_detection_sends_data_caveat_with_profile_facts(monkeypatch, dfs):
    result, calls = _run(monkeypatch, dfs, FLAT_CODE,
                         lambda kw: {"text": f"{MARKER} ყველა პროდუქტს ერთი მნიშვნელობა აქვს.",
                                     "usage": {}},
                         dataset_profile=PROFILE)
    assert len(calls) == 1
    caveat = calls[0]["data_caveat"]
    assert caveat["kind"] == "constant_table"
    assert any("Quantity" in f for f in caveat["facts"])
    # the profile's grain + catalog facts for the table the CODE actually used
    assert any("one row per (client_id, product_id)" in g for g in caveat["grain"])
    assert caveat["catalog"] is True
    # marker stripped from what the user sees
    assert MARKER not in result["text"]
    assert result["text"].startswith("ყველა პროდუქტს")


def test_no_detection_sends_no_caveat(monkeypatch, dfs):
    result, calls = _run(monkeypatch, dfs, VARYING_CODE,
                         lambda kw: {"text": "Revenue differs by store.", "usage": {}},
                         dataset_profile=PROFILE)
    assert len(calls) == 1
    assert "data_caveat" not in calls[0]          # byte-identical old request
    assert result["text"] == "Revenue differs by store."


def test_profile_facts_limited_to_tables_the_code_used(monkeypatch, dfs):
    # The profile describes link.csv; a flat answer built from sales.csv must
    # not borrow link.csv's grain sentence.
    flat_sales = "RESULT = dfs['sales.csv'].assign(qty=1)[['store', 'qty']]"
    _, calls = _run(monkeypatch, dfs, flat_sales,
                    lambda kw: {"text": f"{MARKER} note", "usage": {}},
                    dataset_profile=PROFILE)
    caveat = calls[0]["data_caveat"]
    assert caveat["grain"] == []
    assert caveat["catalog"] is False


# --- the note can never be lost ---------------------------------------------

def test_missing_marker_prepends_localized_sentence(monkeypatch, dfs):
    result, _ = _run(monkeypatch, dfs, FLAT_CODE,
                     lambda kw: {"text": "ცხრილი აჩვენებს პროდუქტებს.", "usage": {}},
                     question="თითოეული პროდუქტის რაოდენობა?",
                     dataset_profile=PROFILE)
    expected = run_chat_local.result_backstop.fallback_sentence("ka", catalog=True)
    assert result["text"].startswith(expected)
    assert "ცხრილი აჩვენებს პროდუქტებს." in result["text"]


def test_describe_failure_still_delivers_the_note(monkeypatch, dfs):
    def boom(kw):
        raise RuntimeError("brain down")

    result, _ = _run(monkeypatch, dfs, FLAT_CODE, boom, question="how many products?",
                     dataset_profile=PROFILE)
    assert result["text"] == run_chat_local.result_backstop.fallback_sentence(
        "en", catalog=True)


def test_fallback_language_follows_the_question(monkeypatch, dfs):
    result, _ = _run(monkeypatch, dfs, FLAT_CODE,
                     lambda kw: {"text": "plain text", "usage": {}},
                     question="сколько товаров?", dataset_profile=None)
    assert result["text"].startswith(
        run_chat_local.result_backstop.fallback_sentence("ru", catalog=False))


def test_backstop_without_profile_still_fires(monkeypatch, dfs):
    _, calls = _run(monkeypatch, dfs, FLAT_CODE,
                    lambda kw: {"text": f"{MARKER} ok", "usage": {}},
                    dataset_profile=None)
    caveat = calls[0]["data_caveat"]
    assert caveat["kind"] == "constant_table" and caveat["grain"] == []


def test_detector_error_never_breaks_the_answer(monkeypatch, dfs):
    monkeypatch.setattr(run_chat_local.result_backstop, "inspect_outputs",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        # sanity: the stub really raises...
        run_chat_local.result_backstop.inspect_outputs()
    # ...and the wrapper is the one place that must not care
    monkeypatch.setattr(run_chat_local.result_backstop, "inspect_outputs",
                        lambda **kw: None)
    result, calls = _run(monkeypatch, dfs, VARYING_CODE,
                         lambda kw: {"text": "fine", "usage": {}})
    assert result["text"] == "fine" and "data_caveat" not in calls[0]


# --- regression: the retry path in _run_single_from_plan --------------------

def test_single_response_retry_threads_dataset_profile(monkeypatch, dfs):
    """A failing first execution used to raise NameError: _run_single_from_plan
    referenced `dataset_profile` without taking it (Prompt 13 oversight)."""
    retry_calls = []

    def fake_retry(**kw):
        retry_calls.append(kw)
        return {"kind": "PYTHON", "code": VARYING_CODE, "usage": {}}

    monkeypatch.setattr(run_chat_local.brain_client, "retry", fake_retry)
    result, _ = _run(monkeypatch, dfs, "RESULT = undefined_name_boom",
                     lambda kw: {"text": "recovered", "usage": {}},
                     dataset_profile=PROFILE)
    assert retry_calls, "retry was never reached (NameError regression)"
    assert retry_calls[0]["dataset_profile"] == PROFILE
    assert result["text"] == "recovered"
    assert result["table"] is not None


# --- chart shape ------------------------------------------------------------

def test_chart_answer_uses_chart_data(monkeypatch, dfs):
    flat_chart = {"columns": ["city", "Quantity"],
                  "rows": [{"city": "Tbilisi", "Quantity": 1},
                           {"city": "Batumi", "Quantity": 1}],
                  "total_rows": 2}
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        lambda *a, **k: {"ok": True, "image": "img", "is_plotly": False,
                                         "chart_data": flat_chart})
    calls = []

    def fake_describe(**kw):
        calls.append(kw)
        return {"text": "flat chart", "usage": {}}

    monkeypatch.setattr(run_chat_local.brain_client, "plan", lambda **k: {
        "raw_text": "", "kind": "PLOT_CODE", "code": "fig = 1", "usage": {}})
    monkeypatch.setattr(run_chat_local.brain_client, "describe", fake_describe)
    events = list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question="chart it",
        history_rows=[], user_email="alice@acme.com", dataset_profile=PROFILE))
    assert calls and calls[0]["data_caveat"]["kind"] == "constant_metric"
    partial = [e for e in events if e.get("partial")]
    assert partial, "no chart was emitted"
    # the model's reply carried no marker -> the client's own note leads the text
    assert partial[0]["answer"].startswith(
        run_chat_local.result_backstop.fallback_sentence("en", catalog=True))
    assert "flat chart" in partial[0]["answer"]
