"""/v1/chat_metadata carries the titles of the user's existing chats
(BRB findings 2, item 5).

The finding: the brain chose chat names blind, so a user's chats piled up as
"Ledger Pulse", "Ledger Pulse_2", "Ledger Pulse_3" — the client's suffix loop
was the only thing keeping names apart. The request now carries an optional
`existing_names`: the titles of the user's active chats (the
`active_chats.jsonl` rows `AuthStore.record_active_chat` writes), at most 50 —
the most recent by `created_at`, newest first, pinning ignored — each cut to
60 characters; `[]` when the user has none. Pinned here:

* it is sent only where the brain is asked for a NAME (`POST
  /generate_chatdata` without a user-provided "name"); with a provided name
  the payload carries no `existing_names` key;
* the client's `_2` / `_3` suffix loop still de-duplicates what comes back;
* titles are never logged — with `CLIENT_LLM_DEBUG` on, the BRAIN_REQUEST
  line for /v1/chat_metadata carries `existing_names=<count>` and no title
  appears in any log line;
* at the wrapper, `brain_client.chat_metadata(..., existing_names=[...])`
  posts the list under "existing_names" and a call without it posts exactly
  the previous payload (no key).

Offline: DATA_ROOT is tmp_path, the brain is stubbed at `brain_client._post`
(or at `_get_client` for the debug-log case), router-only app — the
tests/test_chat_welcome_metadata.py world.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import db_sources
import local_store
import logger_utils
import roles_store
from settings import settings

OWNER = "user@x.com"
SID = "s_0000000000000e05"
BRAIN_NAME = "Ledger Pulse"
PATH = "/v1/chat_metadata"
OLD_PAYLOAD_KEYS = {"sid", "files_info", "file_descriptions", "context",
                    "lang_instruction", "columns_to_human", "user_email"}
BASE_TIME = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _brain_answer(name=BRAIN_NAME) -> dict:
    return {"name": name, "welcome_message": "Welcome to the ledger.",
            "suggested_questions": ["How many customers?", "Top branch?", "Trend?"]}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(brain_client.settings, "BRAIN_TENANT_TOKEN", "tkn", raising=False)
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(OWNER)

    # A controllable clock for created_at: every stamp is one minute later.
    ticks = {"n": 0}

    def fake_now() -> str:
        ticks["n"] += 1
        return (BASE_TIME + timedelta(minutes=ticks["n"])).isoformat()

    monkeypatch.setattr(local_store, "_now", fake_now)

    import routes.chat as chat_mod
    import routes.upload as upload_mod
    monkeypatch.setattr(upload_mod.brain_client, "post_activity", lambda *a, **k: None)

    store = db_sources.DataSourceStore()
    conn = store.create_connection(
        {"name": "BRB Core Banking", "db_type": "postgresql", "host": "h",
         "port": 5432, "database": "d", "user": "u"}, "pw", actor="ladmin")
    table = store.upsert_table({
        "connection_id": conn["id"], "schema": "core", "table_name": "customers",
        "display_name": "customers", "description": "Bank customers, one row each",
        "is_connector": False, "relations": [],
        "columns": [
            {"name": "customer_id", "dtype": "int", "description": "Customer key",
             "indexed": True},
            {"name": "branch_code", "dtype": "str", "description": "Home branch",
             "indexed": False},
        ],
    }, actor="ladmin")
    pd.DataFrame({"customer_id": [1, 2], "branch_code": ["ZQX1", "ZQX2"]}).to_parquet(
        local_store.db_snapshot_path(table["id"]))
    rs = roles_store.RolesStore()
    rs.ensure_base_role()
    rs.update_role(roles_store.BASE_ROLE_ID,
                   {"scope_grants": [{"connection_id": conn["id"], "schema": None}]},
                   actor="ladmin")

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(upload_mod.router)
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = SID
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    yield {"client": tc, "table_id": table["id"], "tmp": tmp_path}
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def captured_posts(monkeypatch):
    """`brain_client._post` replaced: records (path, payload) and answers
    the chat-metadata shape."""
    calls = []
    answer = {"value": _brain_answer()}

    def fake_post(path, payload, sid, timeout=None):
        calls.append((path, json.loads(json.dumps(payload))))
        return dict(answer["value"])

    monkeypatch.setattr(brain_client, "_post", fake_post)
    return {"calls": calls, "answer": answer}


def _record(titles) -> None:
    auth = local_store.AuthStore()
    for i, title in enumerate(titles):
        assert auth.record_active_chat(OWNER, f"c_{i:016x}", title, ["customers"])


def _generate(world, body=None) -> dict:
    tc = world["client"]
    r = tc.post("/session/db_tables", json={"table_ids": [world["table_id"]]})
    assert r.status_code == 200, r.text[:300]
    r = tc.post("/generate_chatdata", json=body or {})
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _metadata_payloads(captured) -> list:
    return [p for (path, p) in captured["calls"] if path == PATH]


# ---------------------------------------------------------------------------
# What /generate_chatdata sends.
# ---------------------------------------------------------------------------
def test_no_chats_sends_an_empty_list(world, captured_posts):
    _generate(world)
    payloads = _metadata_payloads(captured_posts)
    assert len(payloads) == 1, captured_posts["calls"]
    assert payloads[0]["existing_names"] == []


def test_at_most_fifty_newest_first(world, captured_posts):
    titles = [f"Zebra Portfolio {i:02d}" for i in range(55)]   # created_at increasing
    _record(titles)
    _generate(world)
    sent = _metadata_payloads(captured_posts)[0]["existing_names"]
    assert sent == list(reversed(titles))[:50]
    assert len(sent) == 50
    assert "Zebra Portfolio 04" not in sent      # the five oldest are left out


def test_order_is_by_created_at_not_file_order(world, captured_posts):
    p = world["tmp"] / "users" / OWNER / "active_chats.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    stamps = {"Alpha Ledger": 3, "Beta Ledger": 1, "Gamma Ledger": 5, "Delta Ledger": 2}
    with p.open("a", encoding="utf-8") as fh:
        for i, (title, minute) in enumerate(stamps.items()):
            fh.write(json.dumps({
                "chat_id": f"c_{i:016x}", "title": title, "files": ["customers"],
                "created_at": (datetime(2024, 6, 1, tzinfo=timezone.utc)
                               + timedelta(minutes=minute)).isoformat(),
            }) + "\n")
    _generate(world)
    sent = _metadata_payloads(captured_posts)[0]["existing_names"]
    assert sent == ["Gamma Ledger", "Alpha Ledger", "Delta Ledger", "Beta Ledger"]


def test_a_pinned_chat_does_not_jump_the_order(world, captured_posts):
    titles = [f"Harbor Report {i}" for i in range(5)]
    _record(titles)
    assert local_store.AuthStore().set_chat_pinned(OWNER, f"c_{0:016x}", True)
    _generate(world)
    sent = _metadata_payloads(captured_posts)[0]["existing_names"]
    assert sent == list(reversed(titles))


def test_a_pinned_old_chat_does_not_displace_a_newer_one(world, captured_posts):
    titles = [f"Orchid Summary {i:02d}" for i in range(55)]
    _record(titles)
    assert local_store.AuthStore().set_chat_pinned(OWNER, f"c_{2:016x}", True)
    _generate(world)
    sent = _metadata_payloads(captured_posts)[0]["existing_names"]
    assert sent == list(reversed(titles))[:50]


def test_each_title_is_cut_to_sixty_characters(world, captured_posts):
    long_title = "".join(chr(ord("a") + i % 26) for i in range(70))
    assert len(long_title) == 70
    _record(["Short One", long_title])
    _generate(world)
    sent = _metadata_payloads(captured_posts)[0]["existing_names"]
    assert sent == [long_title[:60], "Short One"]
    assert all(len(t) <= 60 for t in sent)


def test_a_provided_name_sends_no_existing_names_key(world, captured_posts):
    _record(["Zebra Portfolio 01", "Zebra Portfolio 02"])
    out = _generate(world, {"name": "My Own Chat"})
    assert out["name"] == "My Own Chat"
    payloads = _metadata_payloads(captured_posts)
    assert payloads, "the welcome is still asked for"
    for payload in payloads:
        assert "existing_names" not in payload, payload
        assert set(payload) == OLD_PAYLOAD_KEYS


def test_no_row_values_in_the_payload(world, captured_posts):
    _record(["Zebra Portfolio 01"])
    _generate(world)
    assert "ZQX" not in repr(captured_posts["calls"])


# ---------------------------------------------------------------------------
# The suffix loop still de-duplicates.
# ---------------------------------------------------------------------------
def test_a_taken_name_gets_suffix_two(world, captured_posts):
    _record([BRAIN_NAME])
    out = _generate(world)
    assert out["name"] == f"{BRAIN_NAME}_2"
    assert BRAIN_NAME in _metadata_payloads(captured_posts)[0]["existing_names"]
    stored = {r["title"] for r in local_store.AuthStore().list_active_chats(OWNER)}
    assert f"{BRAIN_NAME}_2" in stored


def test_name_and_suffix_two_taken_gets_suffix_three(world, captured_posts):
    _record([BRAIN_NAME, f"{BRAIN_NAME}_2"])
    out = _generate(world)
    assert out["name"] == f"{BRAIN_NAME}_3"


# ---------------------------------------------------------------------------
# Titles are never logged.
# ---------------------------------------------------------------------------
class _FakeResp:
    status_code = 200
    text = ""

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, sink):
        self.sink = sink

    def post(self, path, json=None, headers=None, timeout=None):
        self.sink.append((path, json))
        return _FakeResp(_brain_answer())


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            self.lines.append(str(record.msg))


@pytest.fixture
def log_lines():
    logger = logger_utils.get_logger()
    handler = _ListHandler()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def test_debug_log_carries_the_count_and_no_title(world, monkeypatch, log_lines):
    sink = []
    monkeypatch.setattr(brain_client, "_get_client", lambda: _FakeClient(sink))
    monkeypatch.setattr(brain_client.settings, "CLIENT_LLM_DEBUG", True, raising=False)
    titles = [f"Zebra Portfolio {i:02d}" for i in range(7)]
    _record(titles)
    out = _generate(world)
    assert out["name"] == BRAIN_NAME

    posted = [p for (path, p) in sink if path == PATH]
    assert len(posted) == 1
    assert posted[0]["existing_names"] == list(reversed(titles))

    request_lines = [ln for ln in log_lines if "BRAIN_REQUEST" in ln and PATH in ln]
    assert request_lines, log_lines
    assert any("existing_names=7" in ln for ln in request_lines), request_lines
    for ln in log_lines:
        for t in titles:
            assert t not in ln, ln


# ---------------------------------------------------------------------------
# The wrapper.
# ---------------------------------------------------------------------------
def _wrapper_capture(monkeypatch):
    calls = []

    def fake_post(path, payload, sid, timeout=None):
        calls.append((path, payload))
        return _brain_answer()

    monkeypatch.setattr(brain_client, "_post", fake_post)
    return calls


def _wrapper_kwargs() -> dict:
    return dict(sid="chatmeta:s", files_info=["customers"],
                file_descriptions={"customers": "d"}, context="ctx",
                lang_instruction="English", columns_to_human={}, user_email=OWNER)


def test_wrapper_posts_existing_names(monkeypatch):
    calls = _wrapper_capture(monkeypatch)
    brain_client.chat_metadata(**_wrapper_kwargs(), existing_names=["A", "B"])
    assert len(calls) == 1
    path, payload = calls[0]
    assert path == PATH
    assert payload["existing_names"] == ["A", "B"]
    assert OLD_PAYLOAD_KEYS <= set(payload)


def test_wrapper_without_existing_names_posts_the_previous_payload(monkeypatch):
    calls = _wrapper_capture(monkeypatch)
    brain_client.chat_metadata(**_wrapper_kwargs())
    _path, payload = calls[0]
    assert "existing_names" not in payload
    assert payload == {
        "sid": "chatmeta:s", "files_info": ["customers"],
        "file_descriptions": {"customers": "d"}, "context": "ctx",
        "lang_instruction": "English", "columns_to_human": {}, "user_email": OWNER,
    }
