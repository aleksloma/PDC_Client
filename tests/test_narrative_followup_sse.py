"""Narrative follow-up — the chat stream route (DIAGNOSTICS_SPEC A5.4 progress
row, A5.5 record, A5.6 display).

Item 2: the stream worker hands `run_chat_multi_plot` an `on_progress`
callable; an event the flow pushes through it (the narrative-only retry's
`{"progress": True, "message_key": "lab.narrative_retry", "message": ...}`)
reaches the browser through the consumer's existing progress branch, with the
turn's `conv_id`.

Item 3: a dropped template's local headline (`headline_display`) is display
text — the single done shows it as the narrative, the record keeps it, and the
record `content` (what the brain sees next turn) never carries it.

Offline: DATA_ROOT is tmp_path; the planner generator is replaced by a
scripted one (the tests/test_narrative_sse.py harness).
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
from conftest import JSON_HEADERS
from settings import settings

OWNER = "owner@acme.com"
CHAT = "c_narrfollow0001"
PROGRESS = {"progress": True, "message_key": "lab.narrative_retry",
            "message": "Writing the summary…"}
HEADLINE = "Natija: 61,58 trln."


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
def gen(monkeypatch):
    """A scripted `run_chat_multi_plot`: records its kwargs, then runs
    `script(kw)` — a generator function the test sets."""
    import routes.chat as chat_mod

    class _Gen:
        def __init__(self):
            self.calls = []
            self.script = None

    g = _Gen()

    def fake(**kw):
        g.calls.append(kw)
        yield from g.script(kw)

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot", fake)
    return g


def _sse(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def _stream(client, question="q"):
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": question},
                    headers=JSON_HEADERS)
    assert r.status_code == 200, r.text[:300]
    evs = _sse(r.text)
    conv = next(e["conv_id"] for e in evs if e.get("conv_id"))
    return evs[1:], conv          # drop the opening {"progress": "Working..."}


def _last_ai(conv_id):
    rows = [r for r in local_store.ChatDataStore(CHAT).get_history(conv_id)
            if r.get("role") == "ai"]
    assert rows, "no AI record persisted"
    return rows[-1]


def _single_response(result):
    return {"single_response": True, "result": result}


# ---------------------------------------------------------------------------
# item 2: on_progress -> SSE progress event
# ---------------------------------------------------------------------------
def test_stream_passes_on_progress_and_forwards_its_event(client, gen):
    def script(kw):
        kw["on_progress"](dict(PROGRESS))
        yield _single_response({"text": "real answer", "image_base64": None, "table": None,
                                "code": None, "usage": {}})

    gen.script = script
    evs, conv = _stream(client, "how much?")

    assert len(gen.calls) == 1
    assert callable(gen.calls[0].get("on_progress")), gen.calls[0].keys()
    prog = [e for e in evs if e.get("progress") and e.get("message_key") == "lab.narrative_retry"]
    assert len(prog) == 1, evs
    assert prog[0]["message"] == "Writing the summary…"
    assert prog[0]["conv_id"] == conv
    dones = [e for e in evs if e.get("done")]
    assert len(dones) == 1 and dones[0]["answer"] == "real answer"
    assert evs.index(prog[0]) < evs.index(dones[0])
    # a progress event is live UX only: the record is the answer
    assert _last_ai(conv)["content"] == "real answer"


def test_stream_progress_event_from_multi_turn(client, gen):
    def script(kw):
        yield {"partial": True, "phase": 1, "block_index": 1,
               "image_base64": "<div>plotly chart 1</div>", "answer": "described",
               "caption_template": None, "code": "fig = x", "chart_n": 1,
               "chart_total": 1, "usage": {}}
        kw["on_progress"](dict(PROGRESS))
        yield {"done": True, "partial": False, "combined_answer": "described",
               "display_answer": "described", "combined_codes": ["fig = x"],
               "total_usage": {},
               "narrative_view": {"phase1": None, "phase2": None, "phase2_status": None}}

    gen.script = script
    evs, conv = _stream(client, "show it")

    prog = [e for e in evs if e.get("message_key") == "lab.narrative_retry"]
    assert len(prog) == 1 and prog[0]["conv_id"] == conv, evs
    part = [e for e in evs if e.get("partial")]
    done = [e for e in evs if e.get("done")]
    assert evs.index(part[0]) < evs.index(prog[0]) < evs.index(done[0])


# ---------------------------------------------------------------------------
# item 3: headline_display on the live done and on the record
# ---------------------------------------------------------------------------
def test_single_done_shows_headline_record_keeps_it(client, gen):
    def script(kw):
        yield _single_response({"text": "described", "image_base64": None,
                                "table": None, "code": "RESULT = 1", "usage": {},
                                "headline_display": HEADLINE})

    gen.script = script
    evs, conv = _stream(client, "qancha miqdorda kredit ajratildi")

    done = [e for e in evs if e.get("done")][0]
    assert done["answer"] == HEADLINE + "\n\ndescribed"
    assert done["narrative"] == HEADLINE
    assert done["caption"] == "described"
    rec = _last_ai(conv)
    assert rec["content"] == "described"
    assert rec.get("headline_display") == HEADLINE
    sent = json.dumps(brain_client._sanitize_history_rows([rec]), ensure_ascii=False)
    assert "61,58" not in sent and "trln" not in sent


def test_multi_done_record_keeps_headline(client, gen):
    def script(kw):
        yield {"partial": True, "phase": 1, "block_index": 1,
               "image_base64": "<div>plotly chart 1</div>", "answer": "described",
               "caption_template": None, "code": "fig = x", "chart_n": 1,
               "chart_total": 1, "usage": {}}
        yield {"narrative": True, "phase": 1, "text": HEADLINE}
        yield {"done": True, "partial": False, "combined_answer": "described",
               "display_answer": HEADLINE + "\n\ndescribed", "combined_codes": ["fig = x"],
               "total_usage": {},
               "narrative_view": {"phase1": None, "phase2": None, "phase2_status": None,
                                  "headline_display": HEADLINE}}

    gen.script = script
    evs, conv = _stream(client, "qancha miqdorda kredit ajratildi")

    narr = [e for e in evs if e.get("narrative")]
    assert len(narr) == 1 and narr[0]["text"] == HEADLINE
    done = [e for e in evs if e.get("done")][0]
    assert done["answer"] == HEADLINE + "\n\ndescribed"
    rec = _last_ai(conv)
    assert rec["content"] == "described"
    assert rec.get("headline_display") == HEADLINE
    assert "narrative_template" not in rec and "narrative_rendered" not in rec
    sent = json.dumps(brain_client._sanitize_history_rows([rec]), ensure_ascii=False)
    assert "61,58" not in sent and "trln" not in sent
