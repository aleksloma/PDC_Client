"""MCP stage 2 — the day's conversation (MCP_INTEGRATION_PROMPT.txt 3.5, D6).

`mcp_conversations`: one conversation per acting user, per chat, per UTC
calendar day, titled "MCP YYYY-MM-DD", registered in the user's own index.

    conversation_for_today(email, chat_id) -> conv_id | None
    make_factory(email, chat_id) -> callable(store) -> conv_id | None

The mapping lives in `users/<email>/mcp_conversations.json`:
`{"version": 1, "chats": {<chat_id>: {"day": "YYYY-MM-DD", "conv_id": "cv_…"}}}`
and is written LAST, under `local_store._LOCK`, atomically, only while the
account exists. What the file says is never trusted: the stored conversation
must be in the caller's OWN index for this chat and exist under this chat.

`_now()` (an aware UTC datetime) is the module's clock; the tests replace it.

Not pinned: what the factory answers when the account is removed between the
index row and the mapping write (tests/test_account_write_guard.py pins that
nothing is written into the user's folder); in that case the conversation
file already made stays behind, holding only the chat's welcome row. A factory
refused before the index row (the account already gone) may leave an empty
conversation file, never a row.

Offline: DATA_ROOT is tmp_path; the pipeline is a scripted generator.
"""
import importlib
import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import local_store
from settings import settings

OWNER = "owner@acme.com"
FRIEND = "friend@acme.com"
CHAT = "c_mcpconv00000001"
OTHER_CHAT = "c_mcpconv00000002"
DAY1 = datetime(2026, 11, 25, 9, 30, tzinfo=timezone.utc)
WELCOME = "Welcome to Sales."


@pytest.fixture
def mc():
    return importlib.import_module("mcp_conversations")


@pytest.fixture
def clock(mc, monkeypatch):
    class _Clock:
        now = DAY1

    monkeypatch.setattr(mc, "_now", lambda: _Clock.now)
    return _Clock


def _make_chat(chat_id):
    store = local_store.ChatDataStore(chat_id)
    pd.DataFrame({"region": ["North", "South"], "amount": [1, 2]}).to_csv(
        store.files_dir / "sales.csv", index=False)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["files"] = [{"file_name": "sales.csv", "file_description": "sales",
                      "schema": {"fields": {"region": {}, "amount": {}}}}]
    meta["sharing"] = {"shared_with": [FRIEND]}
    store.write_meta(meta)
    store.set_welcome(WELCOME)
    return store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    for email in (OWNER, FRIEND):
        local_store.AuthStore().ensure_user(email)
    _make_chat(OTHER_CHAT)
    yield _make_chat(CHAT)
    local_store._DATAFRAME_CACHE.invalidate()


def _path(tmp_path, email=OWNER):
    return tmp_path / "users" / email / "mcp_conversations.json"


def _mapping(tmp_path, email=OWNER):
    return json.loads(_path(tmp_path, email).read_text(encoding="utf-8"))


def _write_mapping(tmp_path, value, email=OWNER):
    p = _path(tmp_path, email)
    if isinstance(value, bytes):
        p.write_bytes(value)
    else:
        p.write_text(json.dumps(value), encoding="utf-8")


def _entry(conv_id, day="2026-11-25"):
    return {"version": 1, "chats": {CHAT: {"day": day, "conv_id": conv_id}}}


def _convs(store):
    return sorted(p.stem for p in store.conversations_dir.glob("*.jsonl"))


def _index(email=OWNER, chat_id=CHAT):
    return [r for r in local_store.AuthStore().list_conversations(email)
            if r["chat_id"] == chat_id]


# ===========================================================================
# the first question of the day
# ===========================================================================
def test_the_default_clock_is_aware_utc(mc):
    now = mc._now()
    assert isinstance(now, datetime) and now.utcoffset() == timedelta(0)


def test_created_once_with_title_welcome_and_index_row(mc, clock, store, tmp_path):
    assert mc.conversation_for_today(OWNER, CHAT) is None
    conv_id = mc.make_factory(OWNER, CHAT)(store)
    assert local_store.valid_conv_id(conv_id)
    assert _convs(store) == [conv_id]
    rows = _index()
    assert [(r["conv_id"], r["title"]) for r in rows] == [(conv_id, "MCP 2026-11-25")]
    assert "shared_by" not in rows[0]
    history = store.get_history(conv_id)
    assert len(history) == 1 and list(history[0]) == ["role", "content", "ts"]
    assert (history[0]["role"], history[0]["content"]) == ("ai", WELCOME)
    assert isinstance(history[0]["ts"], float)
    assert _mapping(tmp_path) == _entry(conv_id)
    assert mc.conversation_for_today(OWNER, CHAT) == conv_id


def test_a_chat_without_a_welcome_starts_empty(mc, clock, store):
    store.set_welcome("")
    conv_id = mc.make_factory(OWNER, CHAT)(store)
    assert store.get_history(conv_id) == []


def test_looking_creates_nothing(mc, clock, store, tmp_path):
    before = sorted(p.as_posix() for p in tmp_path.rglob("*"))
    assert mc.conversation_for_today(OWNER, CHAT) is None
    assert mc.conversation_for_today("nobody@acme.com", CHAT) is None
    assert mc.conversation_for_today(OWNER, "c_nosuchchat") is None
    mc.make_factory("nobody@acme.com", "c_nosuchchat")      # making it runs nothing
    assert sorted(p.as_posix() for p in tmp_path.rglob("*")) == before


def test_the_day_is_the_utc_day(mc, clock, store):
    clock.now = datetime(2026, 11, 25, 23, 59, 59, tzinfo=timezone.utc)
    first = mc.make_factory(OWNER, CHAT)(store)
    clock.now = datetime(2026, 11, 26, 0, 0, 0, tzinfo=timezone.utc)
    assert mc.conversation_for_today(OWNER, CHAT) is None
    second = mc.make_factory(OWNER, CHAT)(store)
    assert second != first
    assert {r["conv_id"]: r["title"] for r in _index()} == \
        {first: "MCP 2026-11-25", second: "MCP 2026-11-26"}


def test_the_clock_is_read_when_the_factory_runs(mc, clock, store):
    """A factory made just before midnight and run after it."""
    factory = mc.make_factory(OWNER, CHAT)
    clock.now = DAY1 + timedelta(days=1)
    conv_id = factory(store)
    assert [r["title"] for r in _index()] == ["MCP 2026-11-26"]
    assert mc.conversation_for_today(OWNER, CHAT) == conv_id


def test_the_steps_run_in_order_and_the_mapping_comes_last(mc, clock, store, monkeypatch):
    steps = []

    def spy(owner, name):
        real = getattr(owner, name)

        def wrapped(*a, **k):
            steps.append(name)
            return real(*a, **k)
        monkeypatch.setattr(owner, name, wrapped)

    for owner, name in ((local_store.ChatDataStore, "new_conversation"),
                        (local_store.AuthStore, "record_conversation"),
                        (local_store.ChatDataStore, "append_history"),
                        (local_store.AuthStore, "_account_present")):
        spy(owner, name)
    real = local_store._write_json_atomic
    writes = []

    def write(path, payload):
        steps.append("mapping")
        writes.append((path.name, local_store._LOCK._is_owned()))
        return real(path, payload)

    monkeypatch.setattr(local_store, "_write_json_atomic", write)
    if hasattr(mc, "_write_json_atomic"):
        monkeypatch.setattr(mc, "_write_json_atomic", write)
    mc.make_factory(OWNER, CHAT)(store)
    assert [s for s in steps if s != "_account_present"] == \
        ["new_conversation", "record_conversation", "append_history", "mapping"], steps
    # the account is checked again right before the mapping is written
    assert steps[-2:] == ["_account_present", "mapping"], steps
    assert writes == [("mcp_conversations.json", True)], "not written under local_store._LOCK"


# ===========================================================================
# later questions
# ===========================================================================
def test_reused_the_same_day(mc, clock, store, tmp_path):
    first = mc.make_factory(OWNER, CHAT)(store)
    clock.now = DAY1 + timedelta(hours=14)
    assert mc.make_factory(OWNER, CHAT)(store) == first
    assert _convs(store) == [first]
    assert len(_index()) == 1
    assert len(store.get_history(first)) == 1, "the welcome row was written twice"
    assert _mapping(tmp_path) == _entry(first)


def test_the_earlier_questions_of_the_day_are_the_history(mc, clock, store, monkeypatch):
    """Through `chat_turn`: the second question of the day continues the
    conversation with the earlier rows, without the current question."""
    ct = importlib.import_module("chat_turn")
    chat_mod = importlib.import_module("routes.chat")
    seen = []

    def fake(**kw):
        seen.append([(r["role"], r["content"]) for r in kw["history_rows"]])
        yield {"single_response": True,
               "result": {"text": f"answer {len(seen)}", "image_base64": None,
                          "table": None, "code": "RESULT = 1", "usage": {}}}

    def no_title(*a, **k):
        raise AssertionError("an MCP conversation is never titled by the brain")

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot", fake)
    monkeypatch.setattr(chat_mod, "_start_title_generation", no_title)
    convs = []
    for question in ("first?", "second?"):
        turn = ct.prepare_turn(OWNER, CHAT, question,
                               new_conversation=mc.make_factory(OWNER, CHAT))
        convs.append(ct.run_prepared_turn(turn, title="never").conv_id)
    assert convs[0] == convs[1]
    assert seen == [[("ai", WELCOME)],
                    [("ai", WELCOME), ("human", "first?"), ("ai", "answer 1")]]
    assert [r["title"] for r in _index()] == ["MCP 2026-11-25"]


@pytest.mark.parametrize("refusal", ["busy", "empty"])
def test_a_refused_question_creates_no_conversation(mc, clock, store, tmp_path, refusal):
    ct = importlib.import_module("chat_turn")
    if refusal == "busy":
        meta = store.read_meta()
        meta["auto_analysis"] = {"status": "processing"}
        store.write_meta(meta)
    else:
        (store.files_dir / "sales.csv").unlink()
        local_store._DATAFRAME_CACHE.invalidate()
    out = ct.prepare_turn(OWNER, CHAT, "q", new_conversation=mc.make_factory(OWNER, CHAT))
    assert isinstance(out, ct.TurnRefusal), out
    assert _convs(store) == [] and _index() == [] and not _path(tmp_path).exists()


def test_a_new_one_the_next_day_and_only_the_newest_day_is_kept(mc, clock, store, tmp_path):
    first = mc.make_factory(OWNER, CHAT)(store)
    store.append_history(first, {"role": "human", "content": "day one", "ts": 1.0})
    clock.now = DAY1 + timedelta(days=1)
    assert mc.conversation_for_today(OWNER, CHAT) is None
    second = mc.make_factory(OWNER, CHAT)(store)
    assert second != first
    assert _mapping(tmp_path) == _entry(second, "2026-11-26")
    # yesterday's conversation stays a normal conversation
    assert len(store.get_history(first)) == 2
    assert {r["conv_id"] for r in _index()} == {first, second}
    assert [r["content"] for r in store.get_history(second)] == [WELCOME]


def test_each_chat_has_its_own(mc, clock, store, tmp_path):
    a = mc.make_factory(OWNER, CHAT)(store)
    b = mc.make_factory(OWNER, OTHER_CHAT)(local_store.ChatDataStore(OTHER_CHAT))
    assert a != b
    assert _mapping(tmp_path)["chats"] == {
        CHAT: {"day": "2026-11-25", "conv_id": a},
        OTHER_CHAT: {"day": "2026-11-25", "conv_id": b}}
    assert mc.conversation_for_today(OWNER, CHAT) == a
    assert mc.conversation_for_today(OWNER, OTHER_CHAT) == b


def test_each_user_has_their_own(mc, clock, store, tmp_path):
    mine = mc.make_factory(OWNER, CHAT)(store)
    theirs = mc.make_factory(FRIEND, CHAT)(store)
    assert mine != theirs
    assert [r["conv_id"] for r in _index(OWNER)] == [mine]
    assert [r["conv_id"] for r in _index(FRIEND)] == [theirs]
    assert _mapping(tmp_path, FRIEND)["chats"][CHAT]["conv_id"] == theirs
    assert mc.conversation_for_today(OWNER, CHAT) == mine
    assert mc.conversation_for_today(FRIEND, CHAT) == theirs


# ===========================================================================
# the stored conversation is gone
# ===========================================================================
def _delete(store, conv_id):
    assert local_store.AuthStore().delete_conversation(OWNER, conv_id) is True


def _lose_the_file(store, conv_id):
    (store.conversations_dir / f"{conv_id}.jsonl").unlink()


def _lose_the_index_row(store, conv_id):
    (local_store._data_root() / "users" / OWNER / "conversations.jsonl").write_text(
        "", encoding="utf-8")


@pytest.mark.parametrize("lose", [_delete, _lose_the_file, _lose_the_index_row],
                         ids=lambda f: f.__name__.strip("_"))
def test_a_deleted_conversation_is_replaced(mc, clock, store, tmp_path, lose):
    first = mc.make_factory(OWNER, CHAT)(store)
    lose(store, first)
    assert mc.conversation_for_today(OWNER, CHAT) is None
    second = mc.make_factory(OWNER, CHAT)(store)
    assert second and second != first
    assert _mapping(tmp_path) == _entry(second)
    assert mc.conversation_for_today(OWNER, CHAT) == second


# ===========================================================================
# the file is never trusted
# ===========================================================================
def _other_chats_conversation(store):
    """OWNER's own conversation — of ANOTHER chat."""
    other = local_store.ChatDataStore(OTHER_CHAT)
    conv_id = other.new_conversation("x")
    local_store.AuthStore().record_conversation(OWNER, OTHER_CHAT, conv_id, "x")
    other.append_history(conv_id, {"role": "human", "content": "SECRET", "ts": 1.0})
    return conv_id


def _other_users_conversation(store):
    """A conversation of THIS chat in FRIEND's index. The chat's owner could
    open it on the web, but a question of theirs must not land in it."""
    conv_id = store.new_conversation("x")
    local_store.AuthStore().record_conversation(FRIEND, CHAT, conv_id, "x")
    store.append_history(conv_id, {"role": "human", "content": "SECRET", "ts": 1.0})
    return conv_id


def _unindexed_conversation(store):
    conv_id = store.new_conversation("x")
    store.append_history(conv_id, {"role": "human", "content": "SECRET", "ts": 1.0})
    return conv_id


@pytest.mark.parametrize("make", [_other_chats_conversation, _other_users_conversation,
                                  _unindexed_conversation],
                         ids=lambda f: f.__name__.strip("_"))
def test_a_tampered_file_naming_a_foreign_conversation_is_never_used(
        mc, clock, store, tmp_path, make):
    foreign = make(store)
    _write_mapping(tmp_path, _entry(foreign))
    assert mc.conversation_for_today(OWNER, CHAT) is None
    conv_id = mc.make_factory(OWNER, CHAT)(store)
    assert conv_id and conv_id != foreign
    assert "SECRET" not in json.dumps(store.get_history(conv_id))
    assert _mapping(tmp_path) == _entry(conv_id)


@pytest.mark.parametrize("value", ["../../../users/x", "cv_0123456789ABCDEF",
                                   "cv_0123456789abcdef\n", "", None, 7, True,
                                   ["cv_0123456789abcdef"], {"conv_id": "x"}])
def test_a_malformed_stored_id_is_never_used(mc, clock, store, tmp_path, value):
    _write_mapping(tmp_path, _entry(value))
    assert mc.conversation_for_today(OWNER, CHAT) is None
    conv_id = mc.make_factory(OWNER, CHAT)(store)
    assert local_store.valid_conv_id(conv_id)


@pytest.mark.parametrize("day", [None, 20261125, "25.11.2026", "2026-11-24", "", ["2026-11-25"]])
def test_a_stored_day_that_is_not_today_is_not_used(mc, clock, store, tmp_path, day):
    first = mc.make_factory(OWNER, CHAT)(store)
    _write_mapping(tmp_path, _entry(first, day))
    assert mc.conversation_for_today(OWNER, CHAT) is None
    assert mc.make_factory(OWNER, CHAT)(store) != first


# ===========================================================================
# an unreadable file
# ===========================================================================
@pytest.mark.parametrize("raw", [b"", b"{not json SECRET", b"\xff\xfe\x00SECRET",
                                 b"[1, 2]", b'"text"', b"null",
                                 b'{"version": 1}', b'{"version": 1, "chats": []}',
                                 b'{"version": 1, "chats": {"c_mcpconv00000001": "x"}}'])
def test_an_unusable_file_is_treated_as_absent(mc, clock, store, tmp_path, raw):
    _write_mapping(tmp_path, raw)
    assert mc.conversation_for_today(OWNER, CHAT) is None
    conv_id = mc.make_factory(OWNER, CHAT)(store)
    assert local_store.valid_conv_id(conv_id)
    assert _mapping(tmp_path) == _entry(conv_id)
    assert mc.conversation_for_today(OWNER, CHAT) == conv_id


@pytest.mark.parametrize("raw", [b"{not json SECRET", b"\xff\xfe\x00SECRET"])
def test_an_unreadable_file_is_logged_without_its_content(mc, clock, store, tmp_path,
                                                          caplog, raw):
    _write_mapping(tmp_path, raw)
    with caplog.at_level(logging.DEBUG):
        assert mc.conversation_for_today(OWNER, CHAT) is None
    lines = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert lines, "an unreadable mapping file must be logged (Article IV)"
    assert not [ln for ln in lines if "SECRET" in ln or "\n" in ln], lines


def test_another_chats_entry_survives_a_broken_one(mc, clock, store, tmp_path):
    other = mc.make_factory(OWNER, OTHER_CHAT)(local_store.ChatDataStore(OTHER_CHAT))
    doc = _mapping(tmp_path)
    doc["chats"][CHAT] = "broken"
    _write_mapping(tmp_path, doc)
    conv_id = mc.make_factory(OWNER, CHAT)(store)
    assert _mapping(tmp_path)["chats"][OTHER_CHAT]["conv_id"] == other
    assert mc.conversation_for_today(OWNER, OTHER_CHAT) == other
    assert mc.conversation_for_today(OWNER, CHAT) == conv_id


# ===========================================================================
# a removed account (the write guard: tests/test_account_write_guard.py)
# ===========================================================================
def test_a_removed_account_gets_none_and_no_folder(mc, clock, store, tmp_path):
    factory = mc.make_factory(OWNER, CHAT)
    factory(store)
    existing = _convs(store)
    assert local_store.AuthStore().remove_user(OWNER) is True
    assert mc.conversation_for_today(OWNER, CHAT) is None
    assert factory(store) is None
    assert not (tmp_path / "users" / OWNER).exists()
    for left in set(_convs(store)) - set(existing):
        assert store.get_history(left) == [], "a refused factory wrote rows"


def test_a_failing_index_write_gets_none_and_no_mapping(mc, clock, store, tmp_path,
                                                        monkeypatch):
    monkeypatch.setattr(local_store.AuthStore, "record_conversation",
                        lambda self, *a, **k: False)
    assert mc.make_factory(OWNER, CHAT)(store) is None
    assert not _path(tmp_path).exists()
    assert all(store.get_history(c) == [] for c in _convs(store))


def test_a_removed_account_is_refused_by_the_turn(mc, clock, store, tmp_path):
    """The whole chain: the factory's None becomes a `no_conversation`
    refusal, and no question is saved anywhere."""
    ct = importlib.import_module("chat_turn")
    assert local_store.AuthStore().remove_user(OWNER) is True
    out = ct.prepare_turn(OWNER, CHAT, "q", new_conversation=mc.make_factory(OWNER, CHAT))
    assert isinstance(out, ct.TurnRefusal) and out.kind == "no_conversation", out
    assert not (tmp_path / "users" / OWNER).exists()
    assert all(store.get_history(c) == [] for c in _convs(store))
