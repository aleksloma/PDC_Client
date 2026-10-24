"""The chat SSE consumer and the edit-regenerate route with narrative-template
answers (docs/DIAGNOSTICS_SPEC.md A5.4 consumer routing, A5.5 worker /
edit-regenerate, A5.2 R0).

Routing rule under test, in the order the consumer applies it:
  1. `narrative`, `progress` and a TABLES partial (`partial` without
     `image_base64` but with `tables`) each have their own branch, BEFORE the
     chart-partial branch;
  2. the single-shot block runs only for `single_response`;
  3. anything else is dropped and logged `SSE_EVENT_UNKNOWN keys=<names>`.
The browser gets display text (`display_answer`, rendered captions); the
persisted `content` and every brain-bound history row stay brain-safe.

Session 1 emits phase 1 only. Offline: DATA_ROOT is tmp_path, the planner
generator is replaced by a scripted one.
"""
import json
import secrets

import pandas as pd
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import local_store
import narrative
from conftest import JSON_HEADERS
from settings import settings

OWNER = "owner@acme.com"
CHAT = "c_narrsse00000001"

V = {"total": 987654321, "region": "SENTINEL_CATEGORY"}
SENTINELS = ("987654321", "987,654,321", "SENTINEL")
T = ("Revenue was {{total:count}} in {{region}}."
     "{{#anomaly}} One month stands out.{{/anomaly}}")
F = {"anomaly": True, "coverage_dim": "region"}
R = narrative.render(T, V, F, sid="test")
CAP = "Top region is {{region}} at {{total:count}}."
CAP_R = narrative.render(CAP, V, F, sid="test")
CODE1 = "fig = px.bar(dfs['sales.csv'], x='region', y='amount')"
CODE2 = "fig = px.line(dfs['sales.csv'], x='region', y='amount')"
TCODE = "RESULT = dfs['sales.csv'].groupby('region')['amount'].sum().reset_index()"
TABLE = {"columns": ["region", "amount"],
         "rows": [{"region": "North", "amount": 3}, {"region": "South", "amount": 4}],
         "total_rows": 2}
STOP_NOTE = "\n\n⏹ Response stopped by user."


def _no_sentinel(obj, where=""):
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    for bad in SENTINELS:
        assert bad not in s, f"{where}: value {bad!r} leaked into {s[:400]!r}"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(OWNER)
    store = local_store.ChatDataStore(CHAT)
    pd.DataFrame({"region": ["North", "South"], "amount": [1, 2]}).to_csv(
        store.files_dir / "sales.csv", index=False)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["title"] = "Sales"
    meta["files"] = [{"file_name": "sales.csv", "file_description": "sales",
                      "schema": {"fields": {"region": {}, "amount": {}}}}]
    store.write_meta(meta)
    yield {"store": store}
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def client(world):
    import routes.chat as chat_mod

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_" + secrets.token_hex(8)
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}", headers=JSON_HEADERS)
    return tc


@pytest.fixture
def events(monkeypatch):
    import routes.chat as chat_mod

    class _Ev:
        def __init__(self):
            self.seq = []
            self.calls = []

        def set(self, seq):
            self.seq = [dict(e) for e in seq]

    ev = _Ev()

    def gen(**kw):
        ev.calls.append(kw)
        for e in ev.seq:
            yield dict(e)

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        lambda **kw: gen(**kw))
    return ev


@pytest.fixture
def logs(monkeypatch):
    """Every `log_with_sid` call made from routes/chat.py."""
    import routes.chat as chat_mod
    seen = []
    real = chat_mod.log_with_sid

    def spy(sid, level, msg="", *a, **kw):
        seen.append({"sid": sid, "level": level, "msg": str(msg), "args": a, "kw": kw})
        return real(sid, level, msg, *a, **kw)

    monkeypatch.setattr(chat_mod, "log_with_sid", spy)
    return seen


def _sse(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def _stream(client, question="q", conv_id=None):
    body = {"question": question}
    if conv_id:
        body["conv_id"] = conv_id
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json=body, headers=JSON_HEADERS)
    assert r.status_code == 200, r.text[:300]
    evs = _sse(r.text)
    conv = next(e["conv_id"] for e in evs if e.get("conv_id"))
    return evs[1:], conv          # drop the opening {"progress": "Working..."}


def _last_ai(conv_id):
    rows = [r for r in local_store.ChatDataStore(CHAT).get_history(conv_id)
            if r.get("role") == "ai"]
    assert rows, "no AI record persisted"
    return rows[-1]


def _dones(evs):
    return [e for e in evs if e.get("done")]


def _chart(n, *, answer, caption_template=None, code=CODE1, total=2):
    return {"partial": True, "phase": 1, "block_index": n,
            "image_base64": f"<div>plotly chart {n}</div>", "answer": answer,
            "caption_template": caption_template, "code": code,
            "chart_n": n, "chart_total": total, "usage": {}}


def _narr_event():
    return {"narrative": True, "phase": 1, "text": R, "template": T,
            "values": dict(V), "findings": dict(F)}


def _view(with_narrative=True):
    return {"phase1": ({"narrative_template": T, "narrative_rendered": R,
                        "values": dict(V), "findings": dict(F)}
                       if with_narrative else None),
            "phase2": None, "phase2_status": None}


def _done(combined, display, codes=(CODE1, CODE2), *, view=None, **extra):
    ev = {"done": True, "partial": False, "combined_answer": combined,
          "display_answer": display, "combined_codes": list(codes),
          "total_usage": {}, "narrative_view": view or _view(False),
          "table_captions": [], "table_caption_templates": [], "table_phases": []}
    ev.update(extra)
    return ev


def _narrative_turn():
    return [
        _chart(1, answer=CAP_R, caption_template=CAP, code=CODE1),
        _chart(2, answer="Units peaked in March.", code=CODE2),
        _narr_event(),
        _done(narrative.history_content(T, F), R, view=_view(True)),
    ]


# ---------------------------------------------------------------------------
# consumer routing
# ---------------------------------------------------------------------------
def test_narrative_event_does_not_end_stream(client, events):
    """A narrative event is forwarded as `{narrative, phase, text, conv_id}`
    (no template, VALUES or FINDINGS) and the stream ends with exactly ONE
    done — the generator's own."""
    events.set([_narr_event(), _done(narrative.history_content(T, F), R,
                                     codes=(), view=_view(True))])
    evs, conv = _stream(client, "explain")
    narr = [e for e in evs if e.get("narrative")]
    assert len(narr) == 1, evs
    assert set(narr[0]) == {"narrative", "phase", "text", "conv_id"}, narr[0]
    assert narr[0]["text"] == R and narr[0]["phase"] == 1 and narr[0]["conv_id"] == conv
    dones = _dones(evs)
    assert len(dones) == 1, evs
    assert evs.index(narr[0]) < evs.index(dones[0])


def test_tables_partial_not_rendered_as_chart(client, events):
    """A partial with tables and no image is a TABLES partial: forwarded as
    `{partial, phase, tables, full_table_keys, table_captions, conv_id}`, its
    full tables persisted with their own code, no chart counter consumed."""
    events.set([
        _chart(1, answer="Chart one.", code=CODE1, total=1),
        {"partial": True, "phase": 1, "tables": [dict(TABLE)],
         "table_codes": [TCODE], "table_result_keys": [None],
         "table_captions": ["Totals by region."],
         "table_caption_templates": ["Totals by {{region}}."]},
        _done("Chart one.\n\nTotals by {{region}}.", "Chart one.\n\nTotals by region.",
              codes=(CODE1,)),
    ])
    evs, conv = _stream(client, "chart and table")
    parts = [e for e in evs if e.get("partial")]
    assert len(parts) == 2, evs
    tbl = parts[1]
    assert set(tbl) == {"partial", "phase", "tables", "full_table_keys",
                        "table_captions", "conv_id"}, tbl
    assert tbl["phase"] == 1 and tbl["table_captions"] == ["Totals by region."]
    assert tbl["tables"][0]["columns"] == TABLE["columns"]
    keys = tbl["full_table_keys"]
    assert isinstance(keys, list) and len(keys) == 1 and keys[0], keys
    full = local_store.ChatDataStore(CHAT).conversations_dir / "full" / f"{keys[0]}.json"
    assert full.is_file()
    assert json.loads(full.read_text(encoding="utf-8")).get("code") == TCODE
    assert len(_dones(evs)) == 1
    rec = _last_ai(conv)
    assert rec.get("tables") and rec["tables"][0]["columns"] == TABLE["columns"]
    assert rec.get("full_table_keys") == keys
    assert rec.get("table_captions") == ["Totals by region."]
    # The single chart is the record's chart; the table never became one.
    assert rec.get("image_base64") == "<div>plotly chart 1</div>"
    assert not rec.get("images")


def test_unknown_event_dropped_and_logged(client, events, logs):
    """An event with none of the known flags is not forwarded and does not end
    the stream; one `SSE_EVENT_UNKNOWN keys=` line names its KEYS only."""
    events.set([{"mystery_flag": True, "payload": "SENTINEL_CATEGORY 987654321"},
                _done("Chart one.", "Chart one.", codes=())])
    evs, _ = _stream(client, "odd")
    assert not [e for e in evs if "mystery_flag" in e or "payload" in e], evs
    for e in evs:
        _no_sentinel(e, "forwarded event")
    assert len(_dones(evs)) == 1, evs
    hits = [c for c in logs if "SSE_EVENT_UNKNOWN" in c["msg"]
            or any("SSE_EVENT_UNKNOWN" in str(v) for v in c["args"])]
    assert len(hits) == 1, [c["msg"] for c in logs]
    line = hits[0]["msg"] + " " + " ".join(map(str, hits[0]["args"])) + " " + \
        " ".join(f"{k}={v}" for k, v in hits[0]["kw"].items())
    assert "keys" in line and "mystery_flag" in line and "payload" in line, line
    _no_sentinel(line, "log line")


def test_single_shot_requires_single_response(client, events):
    """The single-shot block runs only for `single_response`: a stray
    `result` without the flag is dropped, not turned into a done."""
    events.set([
        {"result": {"text": "should not show", "image_base64": None, "table": None}},
        {"single_response": True, "result": {"text": "real answer", "image_base64": None,
                                             "table": None, "code": None, "usage": {}}},
    ])
    evs, conv = _stream(client, "single")
    dones = _dones(evs)
    assert len(dones) == 1, evs
    assert dones[0]["answer"] == "real answer"
    assert "should not show" not in json.dumps(evs)
    assert _last_ai(conv)["content"] == "real answer"


def test_done_answer_is_display_answer(client, events):
    """Multi done: the browser's `answer` is the display text (the rendered
    narrative), never the brain-safe marker + template; `record_view` carries
    the rendered narrative for the page."""
    events.set(_narrative_turn())
    evs, _ = _stream(client, "explain")
    done = _dones(evs)[-1]
    assert done["answer"] == R, done["answer"]
    assert "[Findings:" not in done["answer"] and "{{" not in done["answer"]
    view = done.get("record_view")
    assert isinstance(view, dict), done
    assert view.get("narrative_rendered") == R


def test_chart_partial_carries_phase(client, events):
    """Each chart partial forwarded to the browser carries its phase; its
    `answer` is the rendered caption."""
    events.set(_narrative_turn())
    evs, _ = _stream(client, "explain")
    charts = [e for e in evs if e.get("partial") and e.get("image_base64")]
    assert len(charts) == 2, evs
    assert [c.get("phase") for c in charts] == [1, 1], charts
    assert charts[0]["answer"] == CAP_R
    assert charts[0].get("chart_ref")


def test_edit_regenerate_answer_is_display_text(client, world, events):
    """Edit-regenerate (single path) answers with the display text and the
    persisted record; never `result.text` (the marker + template)."""
    store = world["store"]
    conv = store.new_conversation("t")
    store.append_history(conv, {"role": "human", "content": "q"})
    store.append_history(conv, {"role": "ai", "content": "a"})
    local_store.AuthStore().record_conversation(OWNER, CHAT, conv, "t")
    single = {"text": narrative.history_content(T, F), "image_base64": None,
              "table": dict(TABLE), "code": TCODE, "usage": {},
              "narrative_template": T, "narrative_rendered": R,
              "values": dict(V), "findings": dict(F)}
    events.set([{"single_response": True, "result": single}])
    r = client.post(f"/api/chat/{CHAT}/edit-regenerate",
                    json={"edited_question": "again", "conv_id": conv},
                    headers=JSON_HEADERS)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert R in data["answer"], data["answer"]
    assert "[Findings:" not in data["answer"] and "{{" not in data["answer"]
    view = data.get("record_view")
    assert isinstance(view, dict) and view.get("narrative_rendered") == R
    assert view.get("content") == narrative.history_content(T, F)
    hist = store.get_history(conv)
    assert data.get("ai_index") == len(hist) - 1
    assert hist[-1]["content"] == narrative.history_content(T, F)


def test_cancel_done_uses_display_answer(client, events, monkeypatch):
    """Stop before the generator's done: the synthesized done sends the DISPLAY
    text built from the rendered answers; the persisted content uses each
    chart's caption template, or its answer when it has none."""
    import routes.chat as chat_mod

    calls = {"n": 0}

    def stop_after_second_chart(conv_id):
        calls["n"] += 1
        return calls["n"] >= 2

    monkeypatch.setattr(chat_mod, "_is_cancelled", stop_after_second_chart)
    events.set(_narrative_turn())
    evs, conv = _stream(client, "stop me")
    dones = _dones(evs)
    assert len(dones) == 1, evs
    assert dones[0]["answer"] == CAP_R + "\n\nUnits peaked in March.", dones[0]["answer"]
    rec = _last_ai(conv)
    assert rec.get("stopped") is True
    assert rec["content"] == CAP + "\n\nUnits peaked in March." + STOP_NOTE, rec["content"]
    _no_sentinel(rec["content"], "stopped content")


def test_persisted_content_has_no_value(client, events):
    """A turn whose rendered narrative and captions carry the VALUES sentinels:
    the rendered text is stored in its own field, while `content` and the
    history rows the NEXT question sends to the brain carry no value."""
    events.set(_narrative_turn())
    _, conv = _stream(client, "explain")
    rec = _last_ai(conv)
    assert rec.get("narrative_rendered") == R          # display, stored apart
    _no_sentinel(rec["content"], "content")

    events.set([{"single_response": True, "result": {
        "text": "ok", "image_base64": None, "table": None, "code": None, "usage": {}}}])
    _stream(client, "follow-up", conv_id=conv)
    history_rows = events.calls[-1]["history_rows"]
    assert any(r.get("narrative_rendered") == R for r in history_rows)
    slim = brain_client._sanitize_history_rows(history_rows)
    _no_sentinel(slim, "brain history rows")
    assert any(r["content"] == narrative.history_content(T, F) for r in slim), slim


# ---------------------------------------------------------------------------
# a template that renders to "" (review finding 1)
# ---------------------------------------------------------------------------
# A section-only template whose flag is absent renders to the empty string.
T_EMPTY = "{{#anomaly}}One month stands out.{{/anomaly}}"
CAP_EMPTY = "{{#anomaly}}The spike is in March.{{/anomaly}}"


def _assert_no_template(text, where):
    assert "{{" not in text and "[Findings" not in text, f"{where}: raw template shown: {text!r}"


def test_empty_render_never_shows_template(client, events):
    """Presence of the TEMPLATE decides, not the truthiness of its rendering:
    a narrative / caption that renders to "" is shown as "" — never replaced
    by the brain-safe `text` (marker + raw template) — and the record keeps
    `narrative_rendered` / `caption_rendered` = "" so a reload does not fall
    back to `content` either."""
    assert narrative.render(T_EMPTY, {}, {}, sid="t") == ""
    assert narrative.render(CAP_EMPTY, {}, {}, sid="t") == ""

    # Single answer, narrative renders to "".
    events.set([{"single_response": True, "result": {
        "text": narrative.history_content(T_EMPTY, {}), "image_base64": "<div>plotly</div>",
        "table": None, "code": CODE1, "usage": {},
        "narrative_template": T_EMPTY, "narrative_rendered": ""}}])
    evs, conv = _stream(client, "empty narrative")
    done = _dones(evs)[-1]
    _assert_no_template(done["answer"], "single narrative done")
    assert done.get("narrative") == ""
    rec = _last_ai(conv)
    assert rec["content"] == narrative.history_content(T_EMPTY, {})
    assert rec.get("narrative_template") == T_EMPTY
    assert rec.get("narrative_rendered") == "", rec

    # Single answer, no narrative, caption renders to "".
    events.set([{"single_response": True, "result": {
        "text": CAP_EMPTY, "image_base64": "<div>plotly</div>", "table": None,
        "code": CODE1, "usage": {},
        "caption_template": CAP_EMPTY, "caption_rendered": ""}}])
    evs, conv = _stream(client, "empty caption")
    done = _dones(evs)[-1]
    _assert_no_template(done["answer"], "single caption done")
    rec = _last_ai(conv)
    assert rec.get("caption_template") == CAP_EMPTY
    assert rec.get("caption_rendered") == "", rec

    # Multi turn: the narrative event and the display answer are "".
    events.set([
        _chart(1, answer="", caption_template=CAP_EMPTY, code=CODE1, total=1),
        {"narrative": True, "phase": 1, "text": "", "template": T_EMPTY,
         "values": {}, "findings": {}},
        _done(narrative.history_content(T_EMPTY, {}) , "", codes=(CODE1,),
              view={"phase1": {"narrative_template": T_EMPTY, "narrative_rendered": "",
                               "values": {}, "findings": {}},
                    "phase2": None, "phase2_status": None}),
    ])
    evs, conv = _stream(client, "empty multi")
    for e in evs:
        for key in ("answer", "text"):
            if isinstance(e.get(key), str):
                _assert_no_template(e[key], f"multi event {key}")
    rec = _last_ai(conv)
    assert rec.get("narrative_template") == T_EMPTY
    assert rec.get("narrative_rendered") == "", rec
    assert rec.get("caption_rendered") == "", rec


def test_edit_regenerate_single_chart_keeps_live_chart_data_key(client, world, events,
                                                                monkeypatch):
    """Review finding 4: a single chart whose chart_data is over the persist
    cap has no stored `chart_data`; the edit response's `record_view` carries
    the live `chart_data_key` so "Show data" survives the edit. The stored
    record never holds that key."""
    import routes.chat as chat_mod

    monkeypatch.setattr(chat_mod, "_PERSIST_CHART_DATA_MAX_CHARS", 10)
    store = world["store"]
    conv = store.new_conversation("t")
    store.append_history(conv, {"role": "human", "content": "q"})
    store.append_history(conv, {"role": "ai", "content": "a"})
    local_store.AuthStore().record_conversation(OWNER, CHAT, conv, "t")
    events.set([{"single_response": True, "result": {
        "text": "Sales rose.", "image_base64": "<div>plotly</div>", "table": None,
        "code": CODE1, "usage": {},
        "chart_data": {"rows": [{"region": "North", "amount": 1}] * 5}}}])
    r = client.post(f"/api/chat/{CHAT}/edit-regenerate",
                    json={"edited_question": "again", "conv_id": conv},
                    headers=JSON_HEADERS)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert data.get("chart_data_key"), data
    view = data["record_view"]
    assert "chart_data" not in view
    assert view.get("chart_data_key") == data["chart_data_key"]
    stored = store.get_history(conv)[data["ai_index"]]
    assert "chart_data_key" not in stored and "chart_data" not in stored
