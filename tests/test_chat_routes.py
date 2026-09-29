"""Authorization on the chat routes: who may do what to a chat and its
conversations, and which code the re-run route agrees to execute.

The matrix, per route family (owner / share recipient / stranger):

* Re-running an item (`POST /api/chat/{id}/refresh_item`) executes only code
  the chat already holds: an AI history row's `code` (whole, or one
  `###NEXT_PLOT###` segment, compared after `strip()` and CRLF->LF), the
  `code` of a durable full-table record, or a code of the turn currently being
  generated. Anything else is `403 CODE_NOT_STORED` — decided BEFORE the role
  gate and AFTER the two existing 400s (empty / joined code).
* Owner-only mutations: saving descriptions, Add Data, the chat-level share and
  starting Auto Analytics answer `403 {"error": "Access denied"}` to a share
  recipient. `GET /schema` tells the page which it is (`is_owner`).
* Conversation-scoped operations by a NON-owner are bound to the caller's own
  conversation index (edit-regenerate, stream with a `conv_id`, stop, history;
  the legacy newest-conversation history is empty for a non-owner). The owner
  is unrestricted.
* Stop and status name a conversation of THIS chat: the conversation file must
  exist under the chat in the path — for the owner too.
* A chat id outside `^[A-Za-z0-9_-]{1,64}$` is a 404.
* A session that must change its password is refused by every API route
  (`403 PASSWORD_CHANGE_REQUIRED`) except sign-in/out, reset, the change form
  itself, `/auth/me`, `/health`, `/version` and static files; pages keep
  redirecting to the change form.

By-design rows are pinned too, so the verdict table is executable: a
recipient re-runs stored items, starts a new conversation and reads their own
conversation; `full_table` / `download_excel` carry no role gate.

Offline: DATA_ROOT is tmp_path, the planner generator and the share mail
relay are stubbed, the activity worker is stubbed by tests/conftest.py.
"""
import json
import secrets

import pandas as pd
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
from conftest import JSON_HEADERS, csrf_form, seed_history
from settings import settings

OWNER = "owner@acme.com"
FRIEND = "friend@acme.com"
STRANGER = "stranger@acme.com"
CHAT = "c_routes00000001"
OTHER_CHAT = "c_routes00000002"
STORED_CODE = "RESULT = dfs['d.csv'].head(1)"
OK_REFRESH = {"ok": True, "kind": "table",
              "table": {"columns": ["a"], "rows": [{"a": 1}], "total_rows": 1}}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _make_chat(chat_id, owner, shared_with=()):
    store = local_store.ChatDataStore(chat_id)
    pd.DataFrame({"a": [1, 2]}).to_csv(store.files_dir / "d.csv", index=False)
    meta = store.read_meta()
    meta["owner"] = owner
    meta["title"] = "Sales"
    meta["files"] = [{"file_name": "d.csv", "file_description": "orig",
                      "schema": {"fields": {"a": {"description": "orig"}}}}]
    if shared_with:
        meta["sharing"] = {"shared_with": sorted(shared_with)}
    store.write_meta(meta)
    return store


def _conversation(store, email, rows):
    """A conversation file with `rows`, recorded in `email`'s own index."""
    conv_id = store.new_conversation("t")
    for row in rows:
        store.append_history(conv_id, row)
    local_store.AuthStore().record_conversation(email, store.chat_id, conv_id, "t")
    return conv_id


_EXCHANGE = [{"role": "human", "content": "q"},
             {"role": "ai", "content": "a", "code": STORED_CODE}]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """OWNER's chat shared with FRIEND; one conversation each, in their own
    index. STRANGER has nothing. Planner and mail relay are stubbed."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    for email in (OWNER, FRIEND, STRANGER):
        local_store.AuthStore().ensure_user(email)
    store = _make_chat(CHAT, OWNER, shared_with=[FRIEND])
    owner_conv = _conversation(store, OWNER, _EXCHANGE)
    friend_conv = _conversation(store, FRIEND, _EXCHANGE)

    import routes.chat as chat_mod

    def fake_multi_plot(**kw):
        yield {"single_response": True,
               "result": {"text": "stubbed answer", "image_base64": None,
                          "table": None, "code": None, "usage": {}}}

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        lambda **kw: fake_multi_plot(**kw))
    mails = []

    def fake_mail(**kw):
        mails.append(kw)
        return {"smtp_configured": True, "sent": kw.get("to") or [], "failed": []}

    monkeypatch.setattr(chat_mod.brain_client, "send_share_email", fake_mail)
    started = []
    import auto_analytics
    monkeypatch.setattr(auto_analytics, "start_job",
                        lambda chat_id, email: started.append((chat_id, email)))
    yield {"store": store, "owner_conv": owner_conv, "friend_conv": friend_conv,
           "mails": mails, "auto_started": started, "tmp": tmp_path}
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def client(world):
    import routes.chat as chat_mod
    import routes.upload as upload_mod

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)
    app.include_router(upload_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_" + secrets.token_hex(8)
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    return tc


def _as(client, email):
    assert client.post(f"/_login/{email}").status_code == 200


@pytest.fixture
def refresh_calls(monkeypatch):
    """Record every execution the refresh route hands off."""
    import routes.chat as chat_mod
    calls = []

    async def fake(chat_id, code, kind, sid, *, drop_df_keys=None):
        calls.append({"chat_id": chat_id, "code": code, "kind": kind})
        return dict(OK_REFRESH)

    monkeypatch.setattr(chat_mod, "run_item_refresh", fake)
    return calls


def _refresh(client, code, chat_id=CHAT, kind="table"):
    return client.post(f"/api/chat/{chat_id}/refresh_item",
                       json={"code": code, "kind": kind})


def _history(conv_id, chat_id=CHAT):
    return local_store.ChatDataStore(chat_id).get_history(conv_id)


# ===========================================================================
# A. refresh_item executes stored code only
# ===========================================================================
def test_refresh_item_refuses_code_that_is_not_in_the_chat(client, refresh_calls):
    """Posted Python that no answer of this chat ever produced is refused,
    and nothing reaches the executor."""
    r = _refresh(client, "import os\nRESULT = os.listdir('/')")
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["code"] == "CODE_NOT_STORED"
    assert body.get("error")
    assert refresh_calls == []


def test_refresh_item_runs_the_code_of_an_ai_history_row(client, refresh_calls):
    r = _refresh(client, STORED_CODE)
    assert r.status_code == 200, r.text
    assert r.json() == OK_REFRESH
    assert [c["code"] for c in refresh_calls] == [STORED_CODE]


def test_refresh_item_accepts_one_segment_of_a_joined_multi_chart_row(
        client, refresh_calls):
    """Legacy multi-chart rows store the codes joined by the marker; the
    frontend posts ONE segment, which counts as stored."""
    seed_history(CHAT, "fig1 = 1\n\n###NEXT_PLOT###\n\nfig2 = 2")
    r = _refresh(client, "fig2 = 2", kind="chart")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_refresh_item_compares_after_strip_and_crlf_normalization(
        client, refresh_calls):
    seed_history(CHAT, "x = 1\r\ny = 2\r\n")
    r = _refresh(client, "  x = 1\ny = 2  \n")
    assert r.status_code == 200, r.text


def test_refresh_item_refuses_a_near_miss_of_stored_code(client, refresh_calls):
    """Normalization is strip + CRLF only — a changed statement is new code."""
    r = _refresh(client, STORED_CODE + "\nimport os")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "CODE_NOT_STORED"
    assert refresh_calls == []


def test_refresh_item_accepts_the_code_of_a_durable_full_table_record(
        client, world, refresh_calls):
    """Full-table records (`conversations/full/<key>.json`) carry the table
    block's own code — the mixed chart+table answer stores it nowhere else."""
    full = world["store"].conversations_dir / "full"
    full.mkdir(parents=True, exist_ok=True)
    (full / "0123456789abcdef.json").write_text(json.dumps(
        {"columns": ["a"], "rows": [{"a": 1}], "code": "RESULT = {'t': dfs['d.csv']}",
         "result_key": "t"}), encoding="utf-8")
    r = _refresh(client, "RESULT = {'t': dfs['d.csv']}")
    assert r.status_code == 200, r.text


def test_refresh_item_ignores_code_on_a_non_ai_row(client, world, refresh_calls):
    """Only an AI row's `code` is an answer; a code key on a human row is not."""
    conv = world["store"].new_conversation("x")
    world["store"].append_history(conv, {"role": "human", "content": "q",
                                         "code": "RESULT = 42"})
    r = _refresh(client, "RESULT = 42")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "CODE_NOT_STORED"


def test_refresh_item_code_stored_in_another_chat_is_refused(client, refresh_calls):
    """Stored means stored in THIS chat, not anywhere on the volume."""
    _make_chat(OTHER_CHAT, OWNER)
    seed_history(OTHER_CHAT, "RESULT = dfs['d.csv'].tail(1)")
    r = _refresh(client, "RESULT = dfs['d.csv'].tail(1)")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "CODE_NOT_STORED"


def test_refresh_item_accepts_an_in_flight_code_until_it_is_discarded(
        client, refresh_calls):
    """A chart streamed on a `partial` event is refreshable before the turn
    is persisted: the generating worker registers its codes, and clears them
    after the record is written."""
    import routes.chat as chat_mod
    assert hasattr(chat_mod, "_inflight_add"), "in-flight registry is missing"
    assert hasattr(chat_mod, "_inflight_discard"), "in-flight registry is missing"
    code = "fig = px.bar(dfs['d.csv'], x='a')"
    chat_mod._inflight_add(CHAT, code)
    try:
        r = _refresh(client, code, kind="chart")
        assert r.status_code == 200, r.text
    finally:
        chat_mod._inflight_discard(CHAT, [code])
    r = _refresh(client, code, kind="chart")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "CODE_NOT_STORED"


def test_in_flight_codes_are_scoped_to_their_chat(client, refresh_calls):
    import routes.chat as chat_mod
    assert hasattr(chat_mod, "_inflight_add"), "in-flight registry is missing"
    _make_chat(OTHER_CHAT, OWNER)
    code = "fig = px.line(dfs['d.csv'], x='a')"
    chat_mod._inflight_add(OTHER_CHAT, code)
    try:
        r = _refresh(client, code, kind="chart")
        assert r.status_code == 403, r.text
    finally:
        chat_mod._inflight_discard(OTHER_CHAT, [code])


def test_code_binding_is_decided_before_the_role_gate(client, monkeypatch,
                                                      refresh_calls):
    """Unstored code never reaches the role gate (403 CODE_NOT_STORED, not the
    200 ROLE_DENIED contract); stored code still meets the gate unchanged."""
    import routes.chat as chat_mod
    seen = []

    def blocked(email, chat_id, code):
        seen.append(code)
        return frozenset({"d.csv"}), ["d.csv"]

    monkeypatch.setattr(chat_mod, "_role_refresh_block", blocked)
    r = _refresh(client, "RESULT = dfs['d.csv'].describe()")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "CODE_NOT_STORED"
    assert seen == []
    r = _refresh(client, STORED_CODE)
    assert r.status_code == 200
    assert r.json()["code"] == "ROLE_DENIED"


def test_empty_and_joined_code_keep_their_400(client, refresh_calls):
    """The two validation errors come first, stored or not."""
    assert _refresh(client, "   ").status_code == 400
    assert _refresh(client, "a = 1 ###NEXT_PLOT### b = 2").status_code == 400
    assert refresh_calls == []


def test_recipient_can_refresh_stored_code(client, refresh_calls):
    """By design: a share recipient re-runs the chat's stored items."""
    _as(client, FRIEND)
    r = _refresh(client, STORED_CODE)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_recipient_cannot_refresh_unstored_code(client, refresh_calls):
    _as(client, FRIEND)
    r = _refresh(client, "RESULT = 7")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "CODE_NOT_STORED"


def test_stranger_is_denied_before_anything_else(client, refresh_calls):
    _as(client, STRANGER)
    r = _refresh(client, STORED_CODE)
    assert r.status_code == 403
    assert r.json() == {"error": "Access denied"}


def test_full_table_and_download_excel_carry_no_role_gate(client, world,
                                                          monkeypatch):
    """By design: viewing or downloading an existing result is never blocked
    retroactively by a role change — only re-running is — except a live
    fetch, which is gated (see test_live_refresh_paths)."""
    import routes.chat as chat_mod

    def blocked(email, chat_id, code):
        return frozenset({"d.csv"}), ["d.csv"]

    async def fake_reexec(chat_id, code, result_key=None, *, drop_df_keys=None):
        return pd.DataFrame({"a": [1, 2]})

    monkeypatch.setattr(chat_mod, "_role_refresh_block", blocked)
    monkeypatch.setattr(chat_mod, "_reexecute_full_df", fake_reexec)
    full = world["store"].conversations_dir / "full"
    full.mkdir(parents=True, exist_ok=True)
    key = "fedcba9876543210"
    (full / f"{key}.json").write_text(json.dumps(
        {"columns": ["a"], "rows": [{"a": 1}], "code": STORED_CODE}), encoding="utf-8")
    _as(client, FRIEND)
    r = client.get(f"/api/chat/{CHAT}/full_table/{key}")
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [{"a": 1}, {"a": 2}]
    r = client.post(f"/api/chat/{CHAT}/download_excel/{key}", json={})
    assert r.status_code == 200, r.text


# ===========================================================================
# C. owner-only mutations
# ===========================================================================
def test_recipient_cannot_save_the_owners_descriptions(client, world):
    _as(client, FRIEND)
    r = client.post(f"/api/chat/{CHAT}/schema", json={"files": [
        {"file_name": "d.csv", "file_description": "hijacked",
         "fields": {"a": {"description": "hijacked"}}}]})
    assert r.status_code == 403, r.text
    assert r.json() == {"error": "Access denied"}
    meta = world["store"].read_meta()
    assert meta["files"][0]["file_description"] == "orig"


def test_owner_saves_descriptions(client, world):
    r = client.post(f"/api/chat/{CHAT}/schema", json={"files": [
        {"file_name": "d.csv", "file_description": "new"}]})
    assert r.status_code == 200
    assert world["store"].read_meta()["files"][0]["file_description"] == "new"


def test_recipient_cannot_add_data_to_the_owners_chat(client):
    _as(client, FRIEND)
    r = client.post("/add_data_to_chat", json={"chat_id": CHAT})
    assert r.status_code == 403, r.text
    assert r.json() == {"error": "Access denied"}


def test_owner_add_data_is_not_refused_for_authorization(client):
    """The owner reaches the route's own validation (no upload yet → 400)."""
    r = client.post("/add_data_to_chat", json={"chat_id": CHAT})
    assert r.status_code != 403, r.text


def test_recipient_cannot_reshare_the_chat(client, world):
    _as(client, FRIEND)
    r = client.post(f"/api/chat/{CHAT}/share", json={"emails": ["eve@acme.com"]})
    assert r.status_code == 403, r.text
    assert r.json() == {"error": "Access denied"}
    sharing = world["store"].read_meta().get("sharing") or {}
    assert sharing.get("shared_with") == [FRIEND]
    assert world["mails"] == []


def test_owner_shares_the_chat(client, world):
    r = client.post(f"/api/chat/{CHAT}/share", json={"emails": ["eve@acme.com"]})
    assert r.status_code == 200, r.text
    assert "eve@acme.com" in world["store"].read_meta()["sharing"]["shared_with"]


def test_recipient_cannot_start_auto_analytics(client, world):
    _as(client, FRIEND)
    r = client.post(f"/api/chat/{CHAT}/auto_analysis/start", json={})
    assert r.status_code == 403, r.text
    assert r.json() == {"error": "Access denied"}
    assert world["auto_started"] == []
    state = (world["store"].read_meta().get("auto_analysis") or {}).get("status")
    assert state != "processing"


def test_owner_starts_auto_analytics(client, world):
    r = client.post(f"/api/chat/{CHAT}/auto_analysis/start", json={})
    assert r.status_code == 200, r.text
    assert world["auto_started"] == [(CHAT, OWNER)]


def test_schema_get_reports_is_owner(client):
    body = client.get(f"/api/chat/{CHAT}/schema").json()
    assert body.get("is_owner") is True, sorted(body)
    _as(client, FRIEND)
    body = client.get(f"/api/chat/{CHAT}/schema").json()
    assert body.get("is_owner") is False, sorted(body)


# ===========================================================================
# D. a non-owner is bound to their own conversation index
# ===========================================================================
def test_recipient_cannot_edit_regenerate_the_owners_conversation(client, world):
    before = _history(world["owner_conv"])
    _as(client, FRIEND)
    r = client.post(f"/api/chat/{CHAT}/edit-regenerate",
                    json={"edited_question": "overwrite", "conv_id": world["owner_conv"]})
    assert r.status_code == 403, r.text
    assert _history(world["owner_conv"]) == before


def test_recipient_edit_regenerates_their_own_conversation(client, world):
    _as(client, FRIEND)
    r = client.post(f"/api/chat/{CHAT}/edit-regenerate",
                    json={"edited_question": "again", "conv_id": world["friend_conv"]})
    assert r.status_code == 200, r.text


def test_owner_edit_regenerate_is_unrestricted(client, world):
    """The owner is not bound to their index — a recipient's conversation in
    the owner's chat is still the owner's chat."""
    r = client.post(f"/api/chat/{CHAT}/edit-regenerate",
                    json={"edited_question": "again", "conv_id": world["friend_conv"]})
    assert r.status_code == 200, r.text


def test_recipient_cannot_stream_into_the_owners_conversation(client, world):
    before = _history(world["owner_conv"])
    _as(client, FRIEND)
    r = client.post(f"/api/chat/{CHAT}/chat/stream",
                    json={"question": "q2", "conv_id": world["owner_conv"]})
    assert r.status_code == 403, r.text[:300]
    assert _history(world["owner_conv"]) == before


def test_recipient_streams_into_their_own_conversation(client, world):
    _as(client, FRIEND)
    r = client.post(f"/api/chat/{CHAT}/chat/stream",
                    json={"question": "q2", "conv_id": world["friend_conv"]})
    assert r.status_code == 200, r.text[:300]


def test_recipient_starts_a_new_conversation(client):
    """By design: a recipient asks new questions in a shared chat; the new
    conversation lands in THEIR index."""
    _as(client, FRIEND)
    before = {c["conv_id"] for c in local_store.AuthStore().list_conversations(FRIEND)}
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "new q"})
    assert r.status_code == 200, r.text[:300]
    after = {c["conv_id"] for c in local_store.AuthStore().list_conversations(FRIEND)}
    assert len(after - before) == 1


def test_recipient_cannot_stop_the_owners_generation(client, world):
    _as(client, FRIEND)
    r = client.post(f"/api/chat/{CHAT}/conversation/{world['owner_conv']}/stop")
    assert r.status_code == 403, r.text


def test_recipient_stops_their_own_generation(client, world):
    _as(client, FRIEND)
    r = client.post(f"/api/chat/{CHAT}/conversation/{world['friend_conv']}/stop")
    assert r.status_code == 200, r.text


def test_recipient_cannot_read_the_owners_conversation(client, world):
    _as(client, FRIEND)
    r = client.get(f"/api/chat/{CHAT}/conversation/{world['owner_conv']}/history")
    assert r.status_code == 403, r.text


def test_recipient_reads_their_own_conversation(client, world):
    _as(client, FRIEND)
    r = client.get(f"/api/chat/{CHAT}/conversation/{world['friend_conv']}/history")
    assert r.status_code == 200
    assert len(r.json()["history"]) == 2


def test_owner_reads_any_conversation_of_their_chat(client, world):
    r = client.get(f"/api/chat/{CHAT}/conversation/{world['friend_conv']}/history")
    assert r.status_code == 200
    assert len(r.json()["history"]) == 2


def test_legacy_history_is_empty_for_a_non_owner(client):
    """The legacy route returns the NEWEST conversation of the chat, whoever
    wrote it — for a non-owner it answers empty."""
    _as(client, FRIEND)
    r = client.get(f"/api/chat/{CHAT}/history")
    assert r.status_code == 200
    assert r.json().get("history") == []


def test_legacy_history_still_serves_the_owner(client):
    r = client.get(f"/api/chat/{CHAT}/history")
    assert r.status_code == 200
    assert len(r.json()["history"]) == 2


def test_recipient_status_on_a_conversation_of_this_chat_stays_open(client, world):
    """By design: status is a registry lookup; it only needs the conversation
    to belong to the chat in the path."""
    _as(client, FRIEND)
    r = client.get(f"/api/chat/{CHAT}/conversation/{world['owner_conv']}/status")
    assert r.status_code == 200
    assert r.json() == {"generating": False}


# ===========================================================================
# E. stop / status name a conversation of the chat in the path
# ===========================================================================
MISSING_CONV = "cv_" + "0" * 16


def test_status_of_an_unknown_conversation_is_denied_to_the_owner(client):
    r = client.get(f"/api/chat/{CHAT}/conversation/{MISSING_CONV}/status")
    assert r.status_code == 403, r.text
    assert r.json() == {"error": "Access denied"}


def test_stop_of_an_unknown_conversation_is_denied_to_the_owner(client):
    r = client.post(f"/api/chat/{CHAT}/conversation/{MISSING_CONV}/stop")
    assert r.status_code == 403, r.text
    assert r.json() == {"error": "Access denied"}


def test_stop_and_status_refuse_a_conversation_of_another_chat(client, world):
    """The owner of chat Y cannot stop (or probe) a conversation of chat X by
    naming it under Y."""
    other = _make_chat(OTHER_CHAT, STRANGER)
    victim_conv = _conversation(world["store"], OWNER, _EXCHANGE)
    assert other.chat_id == OTHER_CHAT
    _as(client, STRANGER)
    r = client.post(f"/api/chat/{OTHER_CHAT}/conversation/{victim_conv}/stop")
    assert r.status_code == 403, r.text
    r = client.get(f"/api/chat/{OTHER_CHAT}/conversation/{victim_conv}/status")
    assert r.status_code == 403, r.text


def test_stop_and_status_of_an_existing_conversation_answer_as_before(client, world):
    conv = world["owner_conv"]
    r = client.get(f"/api/chat/{CHAT}/conversation/{conv}/status")
    assert r.status_code == 200 and r.json() == {"generating": False}
    r = client.post(f"/api/chat/{CHAT}/conversation/{conv}/stop")
    assert r.status_code == 200 and r.json() == {"ok": True, "stopping": True}


# ===========================================================================
# F. chat-id format
# ===========================================================================
@pytest.mark.parametrize("bad_id", ["a.b", "c" * 65, "a b"])
def test_chat_id_outside_the_charset_is_404(client, bad_id):
    """Even when a directory of that name exists under chatdata/."""
    _make_chat(bad_id, OWNER)
    r = client.get(f"/api/chat/{bad_id}/schema")
    assert r.status_code == 404, (bad_id, r.status_code, r.text[:200])


def test_chat_id_of_64_allowed_characters_is_served(client):
    good = "c_" + "A9-_" * 15 + "zz"
    assert len(good) == 64
    _make_chat(good, OWNER)
    r = client.get(f"/api/chat/{good}/schema")
    assert r.status_code == 200, r.text[:200]


# ===========================================================================
# H. a pending forced password change blocks the API (real app)
# ===========================================================================
FLAGGED = "flagged@acme.com"
FLAGGED_PW = "Temp-passw0rd!"
FLAGGED_CHAT = "c_flagged0000001"


@pytest.fixture
def flagged(tmp_path, monkeypatch):
    """A real session on the REAL app whose account must change its password
    (stored hash + must_change_password, the ladmin-bootstrap shape)."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    local_store._DATAFRAME_CACHE.invalidate()
    import app as app_mod
    import routes.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    # Task 9: the reset mail carries a link, not a temp password.
    monkeypatch.setattr(auth_mod.brain_client, "send_password_reset_email",
                        lambda email, reset_url, **kw: None)
    auth = local_store.AuthStore()
    auth.ensure_user(FLAGGED)
    auth.set_password(FLAGGED, FLAGGED_PW, force_change=True)
    store = _make_chat(FLAGGED_CHAT, FLAGGED)
    conv = store.new_conversation("t")
    auth.record_conversation(FLAGGED, FLAGGED_CHAT, conv, "t")
    seed_history(FLAGGED_CHAT, "RESULT = 1")
    tc = TestClient(app_mod.app, base_url="https://testserver")
    r = tc.post("/auth/login", data=csrf_form(tc, {"email": FLAGGED, "password": FLAGGED_PW}),
                follow_redirects=False)
    assert r.status_code == 302, r.text[:300]
    assert r.headers["location"] == "/auth/change_password"
    yield {"client": tc, "conv": conv}
    local_store._DATAFRAME_CACHE.invalidate()


def _blocked_calls(conv):
    return [
        ("GET", "/auth/active_chats", {}),
        ("GET", "/auth/profile", {}),
        ("POST", "/auth/conversations/rename", {"json": {"conv_id": conv, "title": "x"}}),
        ("GET", f"/api/chat/{FLAGGED_CHAT}/schema", {}),
        ("POST", f"/api/chat/{FLAGGED_CHAT}/refresh_item",
         {"json": {"code": "RESULT = 1", "kind": "table"}}),
        ("POST", "/upload", {"files": {"files": ("t.csv", b"a,b\n1,2\n", "text/csv")}}),
        ("POST", "/new_session", {"headers": JSON_HEADERS}),
        ("GET", "/api/dashboards", {}),
        ("POST", f"/api/chat/{FLAGGED_CHAT}/conversation/{conv}/download_report",
         {"json": {}}),
        ("GET", "/api/db_tables", {}),
    ]


_BLOCKED_IDS = ["active_chats", "profile", "conv_rename", "chat_schema",
                "refresh_item", "upload", "new_session", "dashboards",
                "download_report", "db_tables"]


@pytest.mark.parametrize("index", range(len(_BLOCKED_IDS)), ids=_BLOCKED_IDS)
def test_api_route_refused_while_password_change_is_pending(flagged, index):
    method, path, kwargs = _blocked_calls(flagged["conv"])[index]
    r = flagged["client"].request(method, path, follow_redirects=False, **kwargs)
    assert r.status_code == 403, (path, r.status_code, r.text[:200])
    assert r.json() == {"error": "Password change required",
                        "code": "PASSWORD_CHANGE_REQUIRED"}, path


@pytest.mark.parametrize("path", ["/auth/change_password", "/auth/me", "/health",
                                  "/version", "/static/admin_data_sources.css"])
def test_get_routes_allowed_while_password_change_is_pending(flagged, path):
    r = flagged["client"].get(path, follow_redirects=False)
    assert r.status_code == 200, (path, r.status_code, r.text[:200])


@pytest.mark.parametrize("path", ["/lab", "/"])
def test_pages_keep_redirecting_to_the_change_form(flagged, path):
    r = flagged["client"].get(path, follow_redirects=False)
    assert r.status_code == 302, (path, r.status_code)
    assert r.headers["location"] == "/auth/change_password"


def test_login_reset_and_logout_allowed_while_password_change_is_pending(flagged):
    tc = flagged["client"]
    r = tc.post("/auth/login", data=csrf_form(tc, {"email": FLAGGED, "password": FLAGGED_PW}),
                follow_redirects=False)
    assert r.status_code == 302, r.text[:200]
    r = tc.post("/auth/reset_password", data=csrf_form(tc, {"email": FLAGGED}),
                follow_redirects=False)
    assert r.status_code == 200, r.text[:200]
    r = tc.post("/auth/logout", headers=JSON_HEADERS, follow_redirects=False)
    assert r.status_code == 302


def test_apis_answer_normally_after_the_password_is_changed(flagged):
    tc = flagged["client"]
    r = tc.post("/auth/change_password",
                data=csrf_form(tc, {"new_password": "N3w-passw0rd",
                                    "confirm_password": "N3w-passw0rd"}),
                follow_redirects=False)
    assert r.status_code == 302, r.text[:200]
    assert tc.get("/auth/profile").status_code == 200
    r = tc.get(f"/api/chat/{FLAGGED_CHAT}/schema")
    assert r.status_code == 200, r.text[:200]
    assert tc.get("/api/dashboards").status_code == 200


# ===========================================================================
# I. the chat-level share lists the chat for the recipient
# ===========================================================================
def test_chat_share_records_the_chat_in_the_recipients_sidebar(client, world):
    """The recipient's chat list (`active_chats`) gains the shared chat, marked
    with who shared it, exactly as the conversation-level share does."""
    r = client.post(f"/api/chat/{CHAT}/share", json={"emails": ["eve@acme.com"]})
    assert r.status_code == 200, r.text
    rows = [row for row in local_store.AuthStore().list_active_chats("eve@acme.com")
            if row.get("chat_id") == CHAT]
    assert len(rows) == 1, rows
    assert rows[0].get("shared_by") == OWNER, rows


def test_sharing_the_chat_twice_lists_it_once(client, world):
    for _ in range(2):
        r = client.post(f"/api/chat/{CHAT}/share", json={"emails": ["eve@acme.com"]})
        assert r.status_code == 200, r.text
    rows = [row for row in local_store.AuthStore().list_active_chats("eve@acme.com")
            if row.get("chat_id") == CHAT]
    assert len(rows) == 1, rows


# ===========================================================================
# J. in-flight codes through the real generation paths
# ===========================================================================
INFLIGHT_CODE = "fig = px.bar(dfs['d.csv'], x='a')"


def _chart_events(recorded=None):
    """A one-chart multi-plot run. With `recorded`, the generator notes whether
    the chart's code counts as stored when it is resumed after the partial —
    i.e. while the route is still streaming and nothing is persisted."""
    import routes.chat as chat_mod

    def gen(**kw):
        if recorded is not None:
            recorded.append(("before", chat_mod.code_is_stored(CHAT, INFLIGHT_CODE)))
        yield {"partial": True, "image_base64": "<div>plotly</div>", "answer": "a1",
               "code": INFLIGHT_CODE, "chart_n": 1, "chart_total": 1, "usage": {}}
        if recorded is not None:
            recorded.append(("after_partial", chat_mod.code_is_stored(CHAT, INFLIGHT_CODE)))
        yield {"done": True, "combined_answer": "a1",
               "combined_codes": [INFLIGHT_CODE], "total_usage": {}}

    return gen


def test_stream_registers_a_partial_code_before_the_turn_is_persisted(client, monkeypatch):
    import routes.chat as chat_mod
    recorded = []
    gen = _chart_events(recorded)
    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        lambda **kw: gen(**kw))
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "chart"})
    assert r.status_code == 200, r.text[:300]
    assert '"done": true' in r.text, r.text[-300:]
    assert recorded == [("before", False), ("after_partial", True)], recorded
    # Persisted now, and the registry holds nothing for the chat.
    assert chat_mod.code_is_stored(CHAT, INFLIGHT_CODE) is True
    assert CHAT not in chat_mod._INFLIGHT_CODES, chat_mod._INFLIGHT_CODES.get(CHAT)


def test_each_live_chart_carries_a_reference_and_one_failure_disables_only_that_chart(
        client, monkeypatch):
    """The PNG export takes a reference, never markup. Every streamed chart is
    registered as it is sent; if one registration fails, that chart alone has
    no reference (its Download button is disabled) and the turn completes."""
    import json as _json
    import plotly.graph_objects as go
    import routes.chat as chat_mod
    import routes.report as report_mod

    def gen(**kw):
        for n in (1, 2, 3):
            yield {"partial": True, "image_base64": f"<div>plotly chart {n}</div>",
                   "answer": f"a{n}", "code": INFLIGHT_CODE, "chart_n": n,
                   "chart_total": 3, "usage": {}}
        yield {"done": True, "combined_answer": "a", "combined_codes": [INFLIGHT_CODE],
               "total_usage": {}}

    original = report_mod._is_plotly_html
    calls = {"n": 0}

    def flaky(html):
        if "chart 2" in str(html):
            calls["n"] += 1
            raise RuntimeError("registry unavailable")
        return original(html)

    monkeypatch.setattr(report_mod, "_is_plotly_html", flaky)
    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot", lambda **kw: gen(**kw))
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "charts"})
    assert r.status_code == 200, r.text[:300]
    events = [_json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    partials = [e for e in events if e.get("partial")]
    assert [bool(e.get("chart_ref")) for e in partials] == [True, False, True], partials
    assert any(e.get("done") for e in events)
    assert calls["n"] >= 1
    monkeypatch.setattr(go.Figure, "to_image", lambda self, **k: b"\x89PNG")
    monkeypatch.setattr(report_mod, "_plotly_html_to_png", lambda html, sid: b"\x89PNG" + html.encode())
    ok = client.post(f"/api/chat/{CHAT}/export_plotly_png",
                     json={"chart_ref": partials[2]["chart_ref"]})
    assert ok.status_code == 200 and b"chart 3" in ok.content, ok.text[:200]


def test_edit_regenerate_registers_its_codes_until_the_record_is_written(
        client, world, monkeypatch):
    """Edit-regenerate consumes the whole run first, then persists; its codes
    must count as stored at the moment the AI record is appended (before it
    is on disk), and be released afterwards."""
    import routes.chat as chat_mod
    gen = _chart_events()
    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        lambda **kw: gen(**kw))
    seen = []
    original = local_store.ChatDataStore.append_history

    def spy(self, conv_id, row):
        if isinstance(row, dict) and row.get("role") == "ai":
            seen.append(chat_mod.code_is_stored(self.chat_id, INFLIGHT_CODE))
        return original(self, conv_id, row)

    monkeypatch.setattr(local_store.ChatDataStore, "append_history", spy)
    r = client.post(f"/api/chat/{CHAT}/edit-regenerate",
                    json={"edited_question": "again", "conv_id": world["owner_conv"]})
    assert r.status_code == 200, r.text[:300]
    assert seen == [True], seen
    assert chat_mod.code_is_stored(CHAT, INFLIGHT_CODE) is True
    assert CHAT not in chat_mod._INFLIGHT_CODES, chat_mod._INFLIGHT_CODES.get(CHAT)
