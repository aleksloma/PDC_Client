"""C5 §1 (F10) — the conversation title comes from the FIRST answered exchange.

Before: `routes/chat.py` titled a conversation when it held exactly two human
turns, i.e. from the SECOND question and its answer. Now:

  * the title is generated after the FIRST answered question, from that
    question and its answer;
  * a turn that fails (an error turn — no answer) does not title; the next
    ANSWERED turn does, from its own question and answer;
  * a conversation that already has a generated title is never re-titled.

The title is per conversation (`AuthStore.rename_conversation(email,
conv_id, ...)`), so a second conversation of the same chat gets its own.

Seams:
  * `brain_client.title` is stubbed (records its kwargs, answers a fixed
    title) — the trigger and the payload are asserted on it;
  * `run_chat_local.run_chat_multi_plot` is a scripted generator (the
    tests/test_c2_stream_route.py harness);
  * the title runs on a background thread. `routes.chat.threading` is
    replaced by a proxy whose `Thread` records every thread it creates, so
    the test joins them after the stream; should the implementation move off
    `threading.Thread`, the positive checks still poll for the call.

Offline: DATA_ROOT is tmp_path, the chat router on a bare FastAPI app with a
session middleware.
"""
import secrets
import threading
import time

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
CHAT = "c_c5title0000001"
GENERATED = "Generated conversation title"


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


class _ThreadingProxy:
    """`threading` with a `Thread` that remembers what it created."""

    def __init__(self):
        self.created = []

    def Thread(self, *args, **kwargs):  # noqa: N802 — mirrors threading.Thread
        t = threading.Thread(*args, **kwargs)
        self.created.append(t)
        return t

    def __getattr__(self, name):
        return getattr(threading, name)

    def join_all(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        for t in list(self.created):
            if t.ident is None:
                continue                       # never started
            t.join(max(0.0, deadline - time.monotonic()))


@pytest.fixture
def threads(monkeypatch):
    import routes.chat as chat_mod

    proxy = _ThreadingProxy()
    monkeypatch.setattr(chat_mod, "threading", proxy)
    return proxy


@pytest.fixture
def titles(monkeypatch):
    """The brain title call, stubbed: every call's kwargs, in order."""
    calls = []

    def fake_title(*args, **kwargs):
        calls.append(dict(kwargs, _args=args))
        return {"title": GENERATED, "usage": {}}

    monkeypatch.setattr(brain_client, "title", fake_title)
    return calls


@pytest.fixture
def gen(monkeypatch):
    """A scripted `run_chat_multi_plot`: `turns` is a list of scripts, one
    per stream request, each a generator function of the kwargs."""
    import routes.chat as chat_mod

    class _Gen:
        def __init__(self):
            self.turns = []
            self.calls = []

    g = _Gen()

    def fake(**kw):
        g.calls.append(kw)
        script = g.turns.pop(0)
        yield from script(kw)

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot", fake)
    return g


# ---------------------------------------------------------------------------
# scripts
# ---------------------------------------------------------------------------
def _answered(text):
    def script(kw):
        yield {"single_response": True,
               "result": {"text": text, "image_base64": None, "table": None,
                          "code": None, "usage": {}}}
    return script


def _failing(kw):
    raise RuntimeError("the analysis failed")
    yield  # pragma: no cover — makes this a generator


NT = "Revenue was {{total}}."
NR = "Revenue was 7."


def _narrated(kw):
    yield {"narrative": True, "phase": 1, "text": NR, "template": NT,
           "values": {"total": 7}, "findings": {}}
    yield {"done": True, "partial": False,
           "combined_answer": narrative.history_content(NT, {}),
           "display_answer": NR, "combined_codes": [], "total_usage": {},
           "narrative_view": {"phase1": {"narrative_template": NT,
                                         "narrative_rendered": NR,
                                         "values": {"total": 7}, "findings": {}},
                              "phase2": None, "phase2_status": None}}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _ask(client, threads, question, conv_id=None):
    body = {"question": question}
    if conv_id:
        body["conv_id"] = conv_id
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json=body, headers=JSON_HEADERS)
    assert r.status_code == 200, r.text[:300]
    import json as _json
    evs = [_json.loads(line[6:]) for line in r.text.splitlines()
           if line.startswith("data: ")]
    conv = next(e["conv_id"] for e in evs if e.get("conv_id"))
    threads.join_all()
    return conv, evs


def _wait_for(titles, n, timeout=3.0):
    deadline = time.monotonic() + timeout
    while len(titles) < n and time.monotonic() < deadline:
        time.sleep(0.02)
    return titles


def _settle():
    """Room for a title call that should NOT happen to show up anyway."""
    time.sleep(0.3)


def _conv_title(conv_id):
    for row in local_store.AuthStore().list_conversations(OWNER):
        if row.get("conv_id") == conv_id:
            return row.get("title")
    return None


# ---------------------------------------------------------------------------
# the first answered exchange titles the conversation
# ---------------------------------------------------------------------------
def test_first_answered_question_titles_the_conversation(client, gen, threads, titles):
    gen.turns = [_answered("Sales were 3 in total.")]
    conv, _evs = _ask(client, threads, "What were total sales by region?")

    _wait_for(titles, 1)
    assert len(titles) == 1, titles
    call = titles[0]
    assert call.get("question") == "What were total sales by region?", call
    assert call.get("answer") == "Sales were 3 in total.", call
    assert call.get("user_email") == OWNER, call

    deadline = time.monotonic() + 3
    while _conv_title(conv) != GENERATED and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _conv_title(conv) == GENERATED


def test_the_welcome_row_does_not_count_as_an_answered_turn(client, world, gen,
                                                           threads, titles):
    """A new conversation is seeded with the chat's welcome message (an AI
    row). The first question is still the first exchange."""
    world["store"].set_welcome("Welcome! Ask me about your sales.")
    gen.turns = [_answered("Sales were 3 in total.")]
    _conv, _evs = _ask(client, threads, "first question")

    _wait_for(titles, 1)
    assert len(titles) == 1, titles
    assert titles[0].get("question") == "first question"
    assert titles[0].get("answer") == "Sales were 3 in total."
    assert "Welcome" not in (titles[0].get("answer") or "")


def test_title_payload_is_brain_safe_for_a_narrated_turn(client, gen, threads, titles):
    """A narrated turn's title answer is the brain view of the TEMPLATE
    (slots as names), never the rendered text that carries values."""
    gen.turns = [_narrated]
    _ask(client, threads, "how much revenue?")
    _wait_for(titles, 1)
    assert len(titles) == 1, titles
    answer = titles[0].get("answer") or ""
    assert answer == narrative.brain_view(NT, {}), answer
    assert NR not in answer and "7" not in answer, answer


# ---------------------------------------------------------------------------
# a failed first turn: the next answered turn titles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("failures", [1, 2])
def test_failed_turns_do_not_title_the_next_answered_turn_does(client, gen, threads,
                                                               titles, failures):
    gen.turns = [_failing] * failures + [_answered("Here is the answer.")]
    conv, evs = _ask(client, threads, "broken question 1")
    assert any(e.get("error") for e in evs), evs
    for i in range(2, failures + 1):
        _ask(client, threads, f"broken question {i}", conv_id=conv)
    _settle()
    assert titles == [], titles

    _ask(client, threads, "working question", conv_id=conv)
    _wait_for(titles, 1)
    assert len(titles) == 1, titles
    assert titles[0].get("question") == "working question", titles[0]
    assert titles[0].get("answer") == "Here is the answer.", titles[0]

    deadline = time.monotonic() + 3
    while _conv_title(conv) != GENERATED and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _conv_title(conv) == GENERATED


def test_an_error_turn_after_a_titled_one_does_not_title(client, gen, threads, titles):
    gen.turns = [_answered("a1"), _failing]
    conv, _ = _ask(client, threads, "q1")
    _wait_for(titles, 1)
    _ask(client, threads, "q2", conv_id=conv)
    _settle()
    assert [c.get("question") for c in titles] == ["q1"], titles


# ---------------------------------------------------------------------------
# never re-titled
# ---------------------------------------------------------------------------
def test_a_titled_conversation_is_never_retitled(client, gen, threads, titles):
    gen.turns = [_answered("a1"), _answered("a2"), _answered("a3")]
    conv, _ = _ask(client, threads, "q1")
    _wait_for(titles, 1)
    _ask(client, threads, "q2", conv_id=conv)
    _ask(client, threads, "q3", conv_id=conv)
    _settle()
    assert [c.get("question") for c in titles] == ["q1"], titles
    assert _conv_title(conv) == GENERATED


def test_the_second_question_no_longer_triggers_a_title(client, gen, threads, titles):
    """Pins the removal of the old `human_count == 2` trigger."""
    gen.turns = [_answered("a1"), _answered("a2")]
    conv, _ = _ask(client, threads, "q1")
    _wait_for(titles, 1)
    _ask(client, threads, "q2", conv_id=conv)
    _settle()
    assert "q2" not in [c.get("question") for c in titles], titles


def test_each_conversation_gets_its_own_title(client, gen, threads, titles):
    """The title belongs to the conversation: a second conversation of the
    same chat is titled from ITS first exchange."""
    gen.turns = [_answered("a1"), _answered("b1"), _answered("a2")]
    conv_a, _ = _ask(client, threads, "first conversation q")
    _wait_for(titles, 1)
    conv_b, _ = _ask(client, threads, "second conversation q")
    assert conv_b != conv_a
    _wait_for(titles, 2)
    _ask(client, threads, "first conversation follow-up", conv_id=conv_a)
    _settle()
    assert [c.get("question") for c in titles] == [
        "first conversation q", "second conversation q"], titles
    assert [c.get("answer") for c in titles] == ["a1", "b1"], titles


# ---------------------------------------------------------------------------
# edit-regenerate (B1): an edit that produces the conversation's first
# answered turn titles it; an edit of an already-answered turn does not
# ---------------------------------------------------------------------------
def _edit(client, threads, conv_id, edited):
    r = client.post(f"/api/chat/{CHAT}/edit-regenerate",
                    json={"edited_question": edited, "conv_id": conv_id},
                    headers=JSON_HEADERS)
    threads.join_all()
    return r


def test_editing_a_failed_first_question_into_an_answer_titles_once(client, gen,
                                                                   threads, titles):
    gen.turns = [_failing, _answered("The edited answer.")]
    conv, evs = _ask(client, threads, "broken first question")
    assert any(e.get("error") for e in evs), evs
    _settle()
    assert titles == [], titles

    r = _edit(client, threads, conv, "the edited first question")
    assert r.status_code == 200, r.text[:300]
    _wait_for(titles, 1)
    _settle()
    assert len(titles) == 1, titles
    assert titles[0].get("question") == "the edited first question", titles[0]
    assert titles[0].get("answer") == "The edited answer.", titles[0]
    assert titles[0].get("user_email") == OWNER, titles[0]

    deadline = time.monotonic() + 3
    while _conv_title(conv) != GENERATED and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _conv_title(conv) == GENERATED


def test_editing_an_answered_titled_first_question_does_not_retitle(client, gen,
                                                                    threads, titles):
    gen.turns = [_answered("first answer"), _answered("regenerated answer")]
    conv, _ = _ask(client, threads, "q1")
    _wait_for(titles, 1)
    assert [c.get("question") for c in titles] == ["q1"], titles

    r = _edit(client, threads, conv, "q1 edited")
    assert r.status_code == 200, r.text[:300]
    _settle()
    assert [c.get("question") for c in titles] == ["q1"], titles
    assert _conv_title(conv) == GENERATED
