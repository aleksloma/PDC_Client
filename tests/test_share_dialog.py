"""The share dialog lists, per CONVERSATION, who it was shared with, and its
Remove takes back exactly what that conversation share gave.

A conversation share (`POST /auth/conversations/{conv_id}/share`) hands each
recipient a snapshot copy and access to the chat. Until now nothing recorded
which conversation an address was given, so the dialog could only show the
chat-wide list and "Remove" there took away everything. Pinned here:

* the chat meta records `sharing.conv_shares` = {source conv: {address:
  [copy ids]}} and `sharing.conv_granted` = [addresses whose place on
  `shared_with` a conversation share created]; both keys exist only when
  non-empty, and a meta without them reads as an empty record (old metas);
* `GET /api/chat/{chat}/conversation/{conv}/share` (owner only) lists the
  addresses THAT conversation was shared with, sorted;
* `DELETE /api/chat/{chat}/conversation/{conv}/share/{address}` (owner only)
  removes the recipient's copies of that conversation, and takes the chat
  access away only when a conversation share created it and nothing else
  (another shared conversation, a Share Chat share, a pre-release entry, a
  shared dashboard) still needs it;
* Share Chat's Remove, a dashboard unshare that revokes the chat grant and the
  admin's user removal (`purge_address_grants`) drop the address's records;
* everything the existing share routes answered stays as it was;
* the dialog scrolls inside `#shareModal` and `handleShare` uses the
  conversation routes for a conversation, the chat routes for a chat.

Offline: DATA_ROOT is tmp_path, the mail relay is a capturing stub, the
activity worker is stubbed by tests/conftest.py.
"""
import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
from local_store import AuthStore
from settings import settings
from tests.conftest import JSON_HEADERS

A = "anna@acme.com"        # the chat's owner
B = "boris@acme.com"       # recipient
C = "clara@acme.com"       # recipient
D = "dmitri@acme.com"      # no access to the chat at all
CHAT = "c_sharedialog0001"
TITLE = "Sales"
UNKNOWN_CONV = "cv_0000000000000000"

STATIC = Path(__file__).resolve().parents[1] / "static"
DASHBOARD_JS = STATIC / "dashboard.js"
DASHBOARD_CSS = STATIC / "dashboard.css"
MAIN_CSS = STATIC / "main.css"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SHARE_ALLOWED_DOMAINS", "", raising=False)
    auth = AuthStore()
    for email in (A, B, C, D):
        auth.ensure_user(email)
        auth.set_password(email, "pw-" + email)

    store = local_store.ChatDataStore(CHAT)
    meta = store.read_meta()
    meta["owner"] = A
    meta["title"] = TITLE
    store.write_meta(meta)
    auth.record_active_chat(A, CHAT, TITLE, [])
    convs = []
    for n in (1, 2):
        conv = store.new_conversation(f"conv {n}")
        store.append_history(conv, {"role": "human", "content": f"question {n}"})
        store.append_history(conv, {"role": "ai", "content": f"answer {n}"})
        auth.record_conversation(A, CHAT, conv, f"conv {n}")
        convs.append(conv)

    import routes.auth as auth_mod
    import routes.chat as chat_mod
    import routes.dashboards as dash_mod

    mails = []

    def fake_mail(**kw):
        mails.append(kw)
        return {"smtp_configured": True, "sent": kw.get("to") or [], "failed": []}

    for mod in (auth_mod, chat_mod, dash_mod):
        monkeypatch.setattr(mod.brain_client, "send_share_email", fake_mail)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)
    app.include_router(chat_mod.router)
    app.include_router(dash_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    def client_for(email):
        tc = TestClient(app)
        assert tc.post(f"/_login/{email}", json={}).status_code == 200
        return tc

    return {"a": client_for(A), "b": client_for(B), "c": client_for(C),
            "d": client_for(D), "anon": TestClient(app),
            "conv1": convs[0], "conv2": convs[1],
            "dash_store": dash_mod._dash_store, "mails": mails, "tmp": tmp_path}


# --- helpers -----------------------------------------------------------------

def _share_conv(world, conv_id, emails):
    r = world["a"].post(f"/auth/conversations/{conv_id}/share",
                        json={"allowed_emails": list(emails)})
    assert r.status_code == 200, r.text
    return r.json()


def _share_chat(world, emails):
    r = world["a"].post(f"/api/chat/{CHAT}/share", json={"emails": list(emails)})
    assert r.status_code == 200, r.text
    return r.json()


def _conv_share_url(conv_id, recipient=None):
    url = f"/api/chat/{CHAT}/conversation/{conv_id}/share"
    return url if recipient is None else f"{url}/{recipient}"


def _listed(world, conv_id):
    """Who the owner sees this conversation shared with."""
    r = world["a"].get(_conv_share_url(conv_id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("shared_with"), list), body
    return body["shared_with"]


def _remove(world, conv_id, recipient):
    r = world["a"].delete(_conv_share_url(conv_id, recipient), headers=JSON_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True, body
    assert isinstance(body.get("chat_access_removed"), bool), body
    return body


def _meta_file(tmp):
    return tmp / "chatdata" / CHAT / "meta.json"


def _meta(world):
    return json.loads(_meta_file(world["tmp"]).read_text(encoding="utf-8"))


def _rewrite_meta(world, change):
    """Edit meta.json as plain JSON (an older release's shape, written as is)."""
    meta = _meta(world)
    change(meta)
    _meta_file(world["tmp"]).write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _sharing(world):
    return _meta(world).get("sharing") or {}


def _chat_recipients(world):
    """shared_with as the owner's Share Chat dialog reads it."""
    r = world["a"].get(f"/api/chat/{CHAT}/share")
    assert r.status_code == 200, r.text
    return r.json()["shared_with"]


def _lists_chat(world, who):
    r = world[who].get("/auth/active_chats")
    assert r.status_code == 200, r.text
    return any(row.get("chat_id") == CHAT for row in r.json()["active_chats"])


def _conversations(world, who):
    r = world[who].get("/auth/conversations")
    assert r.status_code == 200, r.text
    return [row for row in r.json()["conversations"] if row.get("chat_id") == CHAT]


def _copies(world, who):
    """The conversation ids this user received through a share of the chat."""
    return [row["conv_id"] for row in _conversations(world, who) if row.get("shared_by")]


def _conv_file(world, conv_id):
    return world["tmp"] / "chatdata" / CHAT / "conversations" / f"{conv_id}.jsonl"


def _history_status(world, who, conv_id):
    return world[who].get(f"/api/chat/{CHAT}/conversation/{conv_id}/history").status_code


def _address_in_records(world, address):
    sharing = _sharing(world)
    in_shares = any(address in (per_conv or {})
                    for per_conv in (sharing.get("conv_shares") or {}).values())
    return in_shares or address in (sharing.get("conv_granted") or [])


def _assert_no_empty_record_keys(world):
    """Both record keys exist only when non-empty."""
    sharing = _sharing(world)
    for key in ("conv_shares", "conv_granted"):
        if key in sharing:
            assert sharing[key], f"sharing.{key} is stored although empty: {sharing}"
    for conv_id, per_conv in (sharing.get("conv_shares") or {}).items():
        assert per_conv, f"conv_shares[{conv_id}] is stored although empty"
        for address, copies in per_conv.items():
            assert copies, f"conv_shares[{conv_id}][{address}] is stored although empty"


def _dashboard_with_tile(world, name="D"):
    """One of A's dashboards holding a tile from the chat (the store-level
    build of tests/test_share_policy.py)."""
    store = world["dash_store"]
    dash_id = store.create_dashboard(A, name)["dash_id"]
    doc = store.get_dashboard(A, dash_id)
    doc["tiles"] = [{"tile_id": "t1", "kind": "chart", "chat_id": CHAT, "code": "x",
                     "snapshot": {}}]
    store._write_doc(A, doc)
    return dash_id


def _share_dashboard(world, dash_id, emails):
    r = world["a"].post(f"/api/dashboards/{dash_id}/share", json={"emails": list(emails)})
    assert r.status_code == 200, r.text


def _unshare_dashboard(world, dash_id, email):
    r = world["a"].post(f"/api/dashboards/{dash_id}/unshare", json={"email": email})
    assert r.status_code == 200, r.text


# ===========================================================================
# 1: the list is per conversation
# ===========================================================================
def test_each_conversation_lists_only_the_addresses_it_was_shared_with(world):
    _share_conv(world, world["conv1"], [B, C])
    assert _listed(world, world["conv1"]) == [B, C]
    assert _listed(world, world["conv2"]) == []

    _share_conv(world, world["conv2"], [C])
    assert _listed(world, world["conv2"]) == [C]
    assert _listed(world, world["conv1"]) == [B, C]


def test_conversation_share_list_is_sorted(world):
    _share_conv(world, world["conv1"], [C])
    _share_conv(world, world["conv1"], [B])
    assert _listed(world, world["conv1"]) == [B, C]


def test_conversation_share_writes_the_record_into_the_chat_meta(world):
    out = _share_conv(world, world["conv1"], [B, C])
    sharing = _sharing(world)
    per_conv = sharing["conv_shares"][world["conv1"]]
    assert set(per_conv) == {B, C}
    assert per_conv[B] == [out["snapshot_conv_ids"][B]]
    assert per_conv[C] == [out["snapshot_conv_ids"][C]]
    assert set(sharing["conv_granted"]) == {B, C}
    assert world["conv2"] not in sharing["conv_shares"]


# ===========================================================================
# 2: a second share of the same conversation with the same address
# ===========================================================================
def test_sharing_twice_lists_the_address_once_and_records_both_copies(world):
    first = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    second = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    assert first != second

    assert _listed(world, world["conv1"]) == [B]
    # as before this change: the recipient gets a second copy row
    assert sorted(_copies(world, "b")) == sorted([first, second])
    recorded = _sharing(world)["conv_shares"][world["conv1"]][B]
    assert sorted(recorded) == sorted([first, second])


# ===========================================================================
# 3: Remove when the recipient has nothing else of the chat
# ===========================================================================
def test_remove_takes_the_copy_and_the_chat_access_when_nothing_else_holds_it(world):
    copy = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    assert _history_status(world, "b", copy) == 200
    assert _conv_file(world, copy).is_file()

    out = _remove(world, world["conv1"], B)

    assert out["chat_access_removed"] is True
    assert B not in out["shared_with"]
    assert copy not in [row["conv_id"] for row in _conversations(world, "b")]
    assert not _conv_file(world, copy).exists()
    assert B not in _chat_recipients(world)
    assert not _lists_chat(world, "b")
    assert _history_status(world, "b", copy) == 403
    assert B not in _listed(world, world["conv1"])


def test_remove_of_the_last_recipient_leaves_no_empty_record_keys(world):
    _share_conv(world, world["conv1"], [B])
    _remove(world, world["conv1"], B)
    sharing = _sharing(world)
    assert "conv_shares" not in sharing
    assert "conv_granted" not in sharing


def test_remove_after_a_double_share_takes_both_copies(world):
    first = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    second = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]

    out = _remove(world, world["conv1"], B)

    assert out["chat_access_removed"] is True
    assert _copies(world, "b") == []
    assert not _conv_file(world, first).exists()
    assert not _conv_file(world, second).exists()
    assert _listed(world, world["conv1"]) == []
    assert B not in _chat_recipients(world)


def test_remove_leaves_the_other_recipients_of_the_conversation_alone(world):
    out = _share_conv(world, world["conv1"], [B, C])
    c_copy = out["snapshot_conv_ids"][C]

    res = _remove(world, world["conv1"], B)

    assert res["shared_with"] == [C]
    assert _listed(world, world["conv1"]) == [C]
    assert C in _chat_recipients(world)
    assert _lists_chat(world, "c")
    assert _copies(world, "c") == [c_copy]
    assert _history_status(world, "c", c_copy) == 200


# ===========================================================================
# 4: another shared conversation keeps the chat access
# ===========================================================================
def test_remove_keeps_chat_access_while_another_conversation_is_shared(world):
    c_conv1 = _share_conv(world, world["conv1"], [B, C])["snapshot_conv_ids"][C]
    c_conv2 = _share_conv(world, world["conv2"], [C])["snapshot_conv_ids"][C]

    out = _remove(world, world["conv1"], C)

    assert out["chat_access_removed"] is False
    assert out["shared_with"] == [B]
    assert C in _chat_recipients(world)
    assert _lists_chat(world, "c")
    assert _copies(world, "c") == [c_conv2]
    assert not _conv_file(world, c_conv1).exists()
    assert _history_status(world, "c", c_conv2) == 200
    assert _listed(world, world["conv2"]) == [C]

    # the last conversation share goes: now the access goes with it
    out = _remove(world, world["conv2"], C)

    assert out["chat_access_removed"] is True
    assert out["shared_with"] == []
    assert C not in _chat_recipients(world)
    assert not _lists_chat(world, "c")
    assert _copies(world, "c") == []
    # B was never touched
    assert B in _chat_recipients(world)
    assert _listed(world, world["conv1"]) == [B]


# ===========================================================================
# 5: a Share Chat share keeps the chat access
# ===========================================================================
def test_remove_keeps_access_given_by_an_earlier_chat_share(world):
    _share_chat(world, [B])
    copy = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    assert B not in (_sharing(world).get("conv_granted") or [])

    out = _remove(world, world["conv1"], B)

    assert out["chat_access_removed"] is False
    assert _copies(world, "b") == []
    assert not _conv_file(world, copy).exists()
    assert B in _chat_recipients(world)
    assert _lists_chat(world, "b")
    assert _listed(world, world["conv1"]) == []


def test_a_later_chat_share_makes_the_access_deliberate(world):
    copy = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    assert B in _sharing(world)["conv_granted"]

    _share_chat(world, [B])
    assert B not in (_sharing(world).get("conv_granted") or [])
    _assert_no_empty_record_keys(world)
    # the chat share does not forget which conversation B was given
    assert _listed(world, world["conv1"]) == [B]

    out = _remove(world, world["conv1"], B)

    assert out["chat_access_removed"] is False
    assert not _conv_file(world, copy).exists()
    assert B in _chat_recipients(world)
    assert _lists_chat(world, "b")


# ===========================================================================
# 6: an entry from before the record keeps the chat access
# ===========================================================================
def test_remove_keeps_a_pre_release_shared_with_entry(world):
    _rewrite_meta(world, lambda meta: meta.__setitem__("sharing", {"shared_with": [B]}))
    assert AuthStore().record_shared_chat(B, CHAT, TITLE, [], shared_by=A)

    copy = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    assert B not in (_sharing(world).get("conv_granted") or [])
    assert _listed(world, world["conv1"]) == [B]

    out = _remove(world, world["conv1"], B)

    assert out["chat_access_removed"] is False
    assert B in _chat_recipients(world)
    assert _lists_chat(world, "b")
    assert _copies(world, "b") == []
    assert not _conv_file(world, copy).exists()


# ===========================================================================
# 7: a shared dashboard with a tile from the chat keeps the chat access
# ===========================================================================
def test_remove_hands_the_chat_access_to_a_shared_dashboard_that_needs_it(world):
    copy = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    dash_id = _dashboard_with_tile(world)
    _share_dashboard(world, dash_id, [B])

    out = _remove(world, world["conv1"], B)

    assert out["chat_access_removed"] is False
    assert B in _chat_recipients(world)
    assert not _conv_file(world, copy).exists()
    assert _listed(world, world["conv1"]) == []

    # the grant now belongs to the dashboard: its unshare ends it
    _unshare_dashboard(world, dash_id, B)
    assert B not in _chat_recipients(world)


# ===========================================================================
# 8: the other ways an address leaves the chat drop its records
# ===========================================================================
def test_share_chat_remove_drops_the_conversation_records(world):
    _share_conv(world, world["conv1"], [B])

    r = world["a"].delete(f"/api/chat/{CHAT}/share/{B}", headers=JSON_HEADERS)
    assert r.status_code == 200, r.text
    assert B not in r.json()["shared_with"]

    assert _listed(world, world["conv1"]) == []
    assert not _address_in_records(world, B)
    sharing = _sharing(world)
    assert "conv_shares" not in sharing
    assert "conv_granted" not in sharing


def test_share_chat_remove_keeps_the_records_of_other_addresses(world):
    _share_conv(world, world["conv1"], [B, C])

    r = world["a"].delete(f"/api/chat/{CHAT}/share/{B}", headers=JSON_HEADERS)
    assert r.status_code == 200, r.text

    assert _listed(world, world["conv1"]) == [C]
    assert not _address_in_records(world, B)
    assert C in _sharing(world)["conv_granted"]
    _assert_no_empty_record_keys(world)


def test_dashboard_unshare_that_revokes_the_chat_grant_drops_the_records(world):
    dash_id = _dashboard_with_tile(world)
    _share_dashboard(world, dash_id, [B])          # the dashboard creates the grant
    assert B in _chat_recipients(world)
    _share_conv(world, world["conv1"], [B])
    assert _listed(world, world["conv1"]) == [B]

    _unshare_dashboard(world, dash_id, B)

    assert B not in _chat_recipients(world)
    assert _listed(world, world["conv1"]) == []
    assert not _address_in_records(world, B)
    _assert_no_empty_record_keys(world)


def test_purge_address_grants_drops_the_conversation_records(world):
    _share_conv(world, world["conv1"], [B, C])

    local_store.purge_address_grants(B)

    assert B not in _chat_recipients(world)
    assert _listed(world, world["conv1"]) == [C]
    assert not _address_in_records(world, B)
    _assert_no_empty_record_keys(world)

    local_store.purge_address_grants(C)

    assert _listed(world, world["conv1"]) == []
    sharing = _sharing(world)
    assert "conv_shares" not in sharing
    assert "conv_granted" not in sharing


# ===========================================================================
# 9: an empty record, and removing what was never shared
# ===========================================================================
def test_a_meta_without_record_keys_lists_nobody(world):
    assert "sharing" not in _meta(world)
    r = world["a"].get(_conv_share_url(world["conv1"]))
    assert r.status_code == 200, r.text
    assert r.json() == {"shared_with": []}


def test_an_old_shape_sharing_block_lists_nobody(world):
    _rewrite_meta(world, lambda meta: meta.__setitem__("sharing", {"shared_with": [B]}))
    r = world["a"].get(_conv_share_url(world["conv1"]))
    assert r.status_code == 200, r.text
    assert r.json() == {"shared_with": []}


def test_removing_an_address_never_shared_changes_nothing(world):
    out = _remove(world, world["conv1"], B)
    assert out["shared_with"] == []
    assert out["chat_access_removed"] is False
    sharing = _sharing(world)
    assert "conv_shares" not in sharing
    assert "conv_granted" not in sharing
    assert (sharing.get("shared_with") or []) == []


def test_removing_a_never_shared_conversation_keeps_a_chat_share(world):
    _share_chat(world, [B])
    before = _sharing(world)

    out = _remove(world, world["conv1"], B)

    assert out["shared_with"] == []
    assert out["chat_access_removed"] is False
    assert _sharing(world) == before
    assert "conv_shares" not in before and "conv_granted" not in before
    assert B in _chat_recipients(world)
    assert _lists_chat(world, "b")


def test_remove_is_idempotent(world):
    _share_conv(world, world["conv1"], [B, C])
    assert _remove(world, world["conv1"], B)["chat_access_removed"] is True
    after_first = _sharing(world)

    again = _remove(world, world["conv1"], B)

    assert again["chat_access_removed"] is False
    assert again["shared_with"] == [C]
    assert _sharing(world) == after_first


def test_remove_on_another_conversation_does_not_touch_this_ones_share(world):
    copy = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]

    out = _remove(world, world["conv2"], B)       # conv2 was never shared with B

    assert out["shared_with"] == []
    assert out["chat_access_removed"] is False
    assert _listed(world, world["conv1"]) == [B]
    assert _copies(world, "b") == [copy]
    assert _history_status(world, "b", copy) == 200
    assert B in _chat_recipients(world)


# ===========================================================================
# 9b: the store methods directly
# ===========================================================================
def test_store_conv_share_recipients_reads_an_empty_record(world):
    store = local_store.ChatDataStore(CHAT)
    assert store.conv_share_recipients(world["conv1"]) == []


def test_store_record_and_read_conv_shares(world):
    store = local_store.ChatDataStore(CHAT)
    store.add_share_recipients([B, C])
    store.record_conv_shares(world["conv1"], {C: "cv_" + "c" * 16, B: "cv_" + "b" * 16}, [B])
    store.record_conv_shares(world["conv1"], {B: "cv_" + "d" * 16}, [])

    assert store.conv_share_recipients(world["conv1"]) == [B, C]
    assert store.conv_share_recipients(world["conv2"]) == []
    sharing = _sharing(world)
    assert sorted(sharing["conv_shares"][world["conv1"]][B]) == sorted(
        ["cv_" + "b" * 16, "cv_" + "d" * 16])
    assert sharing["conv_shares"][world["conv1"]][C] == ["cv_" + "c" * 16]
    assert sharing["conv_granted"] == [B]


def test_store_remove_conv_share_answers_the_remaining_list_and_the_verdict(world):
    store = local_store.ChatDataStore(CHAT)
    store.add_share_recipients([B, C])
    store.record_conv_shares(world["conv1"], {B: "cv_" + "b" * 16, C: "cv_" + "c" * 16}, [B])

    out = store.remove_conv_share(world["conv1"], B, A)
    assert out == {"shared_with": [C], "chat_access_removed": True}
    assert store.conv_share_recipients(world["conv1"]) == [C]

    # C was on shared_with without a conversation share having put it there
    out = store.remove_conv_share(world["conv1"], C, A)
    assert out == {"shared_with": [], "chat_access_removed": False}
    assert (_sharing(world).get("shared_with") or []) == [C]
    _assert_no_empty_record_keys(world)


def test_store_remove_conv_share_of_an_unknown_address_is_a_no_op(world):
    store = local_store.ChatDataStore(CHAT)
    before = _meta(world)
    out = store.remove_conv_share(world["conv1"], B, A)
    assert out == {"shared_with": [], "chat_access_removed": False}
    assert _meta(world) == before


# ===========================================================================
# 10: guards
# ===========================================================================
def test_conversation_share_routes_require_a_session(world):
    _share_conv(world, world["conv1"], [B])
    anon = world["anon"]
    assert anon.get(_conv_share_url(world["conv1"])).status_code == 401
    assert anon.delete(_conv_share_url(world["conv1"], B),
                       headers=JSON_HEADERS).status_code == 401
    assert _listed(world, world["conv1"]) == [B]


def test_a_share_recipient_is_refused_on_the_owners_conversation(world):
    copy = _share_conv(world, world["conv1"], [B, C])["snapshot_conv_ids"][B]
    before = _meta(world)

    r = world["b"].get(_conv_share_url(world["conv1"]))
    assert r.status_code == 403
    assert r.json() == {"error": "Access denied"}

    for target in (B, C):
        r = world["b"].delete(_conv_share_url(world["conv1"], target), headers=JSON_HEADERS)
        assert r.status_code == 403
        assert r.json() == {"error": "Access denied"}

    # nothing changed
    assert _meta(world) == before
    assert _copies(world, "b") == [copy]
    assert _conv_file(world, copy).is_file()
    assert _lists_chat(world, "b") and _lists_chat(world, "c")
    assert _listed(world, world["conv1"]) == [B, C]


def test_a_share_recipient_is_refused_on_their_own_copy(world):
    copy = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    before = _meta(world)

    r = world["b"].get(_conv_share_url(copy))
    assert r.status_code == 403
    assert r.json() == {"error": "Access denied"}

    r = world["b"].delete(_conv_share_url(copy, B), headers=JSON_HEADERS)
    assert r.status_code == 403
    assert r.json() == {"error": "Access denied"}

    assert _meta(world) == before
    assert _copies(world, "b") == [copy]
    assert _history_status(world, "b", copy) == 200


def test_a_stranger_is_refused(world):
    _share_conv(world, world["conv1"], [B])
    before = _meta(world)

    assert world["d"].get(_conv_share_url(world["conv1"])).status_code == 403
    assert world["d"].delete(_conv_share_url(world["conv1"], B),
                             headers=JSON_HEADERS).status_code == 403

    assert _meta(world) == before
    assert _listed(world, world["conv1"]) == [B]


def test_the_owner_gets_404_for_a_conversation_outside_their_own_index(world):
    copy = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    before = _meta(world)

    for conv_id in (copy, UNKNOWN_CONV):
        r = world["a"].get(_conv_share_url(conv_id))
        assert r.status_code == 404, (conv_id, r.text)
        assert r.json() == {"error": "Conversation not found"}

        r = world["a"].delete(_conv_share_url(conv_id, B), headers=JSON_HEADERS)
        assert r.status_code == 404, (conv_id, r.text)
        assert r.json() == {"error": "Conversation not found"}

    assert _meta(world) == before
    assert _copies(world, "b") == [copy]
    assert _conv_file(world, copy).is_file()


def test_remove_refuses_a_recipient_that_is_not_an_address(world):
    _share_conv(world, world["conv1"], [B])
    before = _meta(world)

    r = world["a"].delete(_conv_share_url(world["conv1"], "not-an-address"),
                          headers=JSON_HEADERS)

    assert r.status_code == 400, r.text
    assert _meta(world) == before
    assert _listed(world, world["conv1"]) == [B]


# ===========================================================================
# 11: what the existing routes answer is unchanged
# ===========================================================================
def test_pin_chat_share_get_keeps_its_keys_and_lists_everyone_with_access(world):
    _share_conv(world, world["conv1"], [B, C])
    r = world["a"].get(f"/api/chat/{CHAT}/share")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"shared_with", "owner", "is_owner"}
    assert set(body["shared_with"]) == {B, C}
    assert body["owner"] == A
    assert body["is_owner"] is True


def test_pin_conversation_share_response_and_the_recipients_copy(world):
    out = _share_conv(world, world["conv1"], [B])
    assert out["ok"] is True
    assert out["shared_with"] == [B]
    assert out["added"] == [B]
    copy = out["snapshot_conv_ids"][B]
    assert re.fullmatch(r"cv_[0-9a-f]{16}", copy)
    assert copy != world["conv1"]
    assert _conv_file(world, copy).is_file()

    rows = [row for row in _conversations(world, "b") if row["conv_id"] == copy]
    assert len(rows) == 1
    assert rows[0]["shared_by"] == A
    assert rows[0]["title"].startswith("(Shared) ")
    assert rows[0]["chat_id"] == CHAT

    assert B in (_sharing(world).get("shared_with") or [])
    assert _lists_chat(world, "b")
    assert _history_status(world, "b", copy) == 200
    # the owner's conversation is not in the recipient's index
    assert _history_status(world, "b", world["conv1"]) == 403


def test_pin_share_chat_remove_still_ends_the_access(world):
    copy = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    r = world["a"].delete(f"/api/chat/{CHAT}/share/{B}", headers=JSON_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert B not in body["shared_with"]
    assert not _lists_chat(world, "b")
    assert _history_status(world, "b", copy) == 403


@pytest.mark.parametrize("old_sharing", [None, {"shared_with": []}],
                         ids=["no_sharing_key", "empty_shared_with"])
def test_pin_conversation_share_works_on_an_old_shape_meta(world, old_sharing):
    def to_old_shape(meta):
        meta.pop("sharing", None)
        if old_sharing is not None:
            meta["sharing"] = dict(old_sharing)
    _rewrite_meta(world, to_old_shape)

    out = _share_conv(world, world["conv1"], [B])

    copy = out["snapshot_conv_ids"][B]
    assert _copies(world, "b") == [copy]
    assert _sharing(world)["shared_with"] == [B]
    assert _lists_chat(world, "b")
    assert _history_status(world, "b", copy) == 200


def test_remove_never_touches_the_owners_own_conversations(world):
    _share_conv(world, world["conv1"], [B, C])
    _share_conv(world, world["conv2"], [C])
    rows_before = _conversations(world, "a")
    files_before = {c: _conv_file(world, world[c]).read_bytes() for c in ("conv1", "conv2")}
    history_before = world["a"].get(
        f"/api/chat/{CHAT}/conversation/{world['conv1']}/history").json()

    _remove(world, world["conv1"], B)
    _remove(world, world["conv1"], C)
    _remove(world, world["conv2"], C)

    assert _conversations(world, "a") == rows_before
    for c in ("conv1", "conv2"):
        assert _conv_file(world, world[c]).read_bytes() == files_before[c]
    r = world["a"].get(f"/api/chat/{CHAT}/conversation/{world['conv1']}/history")
    assert r.status_code == 200
    assert r.json() == history_before
    assert len(r.json()["history"]) == 2
    assert _lists_chat(world, "a")
    assert _meta(world)["owner"] == A


# ===========================================================================
# 12: structure -- the dialog scrolls inside itself; handleShare's routes
# ===========================================================================
def _css_rules(css: str) -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _declarations(path: Path, selector: str) -> list[dict]:
    """One {property: value} dict per rule whose (comma-separated) selector
    list holds exactly `selector`; whitespace-insensitive."""
    out = []
    for sel, body in _css_rules(path.read_text(encoding="utf-8")):
        if _norm(selector) not in [_norm(s) for s in sel.split(",")]:
            continue
        decls = {}
        for decl in body.split(";"):
            if ":" in decl:
                prop, value = decl.split(":", 1)
                decls[_norm(prop).lower()] = _norm(value).lower()
        out.append(decls)
    return out


def _merged(path: Path, selector: str) -> dict:
    merged = {}
    for decls in _declarations(path, selector):
        merged.update(decls)
    return merged


def test_the_share_modal_body_scrolls_inside_the_dialog():
    decls = _merged(DASHBOARD_CSS, "#shareModal .modal-body")
    assert decls, "no `#shareModal .modal-body` rule in dashboard.css"
    assert decls.get("overflow-y") == "auto"
    assert decls.get("min-height") in ("0", "0px")


@pytest.mark.parametrize("selector", ["#shareModal .modal-header", "#shareModal .modal-actions"])
def test_the_share_modal_header_and_actions_never_shrink(selector):
    decls = _merged(DASHBOARD_CSS, selector)
    assert decls, f"no `{selector}` rule in dashboard.css"
    assert decls.get("flex") == "0 0 auto"


def test_pin_the_generic_modal_body_rule_only_sets_its_padding():
    rules = _declarations(DASHBOARD_CSS, ".modal-body")
    assert rules, "the generic `.modal-body` rule is gone from dashboard.css"
    assert _merged(DASHBOARD_CSS, ".modal-body") == {"padding": "16px"}


def test_pin_main_css_modal_content_is_a_height_capped_flex_column():
    rules = _declarations(MAIN_CSS, ".modal-content")
    assert any(d.get("max-height") == "85vh" and d.get("display") == "flex" for d in rules), rules


def test_pin_main_css_modal_overlay_declares_no_overflow():
    rules = _declarations(MAIN_CSS, ".modal")
    assert rules, "no `.modal` rule in main.css"
    for decls in rules:
        assert not [p for p in decls if p.startswith("overflow")], decls


def _handle_share_body() -> str:
    src = DASHBOARD_JS.read_text(encoding="utf-8")
    assert src.count("async function handleShare(") == 1
    body = src[src.index("async function handleShare("):]
    return body[:body.index("\n}\n")]


def test_handle_share_uses_the_conversation_routes():
    assert "/conversation/${convId}/share" in _handle_share_body()


def test_pin_handle_share_still_uses_the_chat_routes():
    assert "/api/chat/${chatId}/share" in _handle_share_body()


def test_pin_handle_share_calls_no_bare_fetch():
    body = _handle_share_body()
    assert "pdcFetch(" in body
    assert not re.search(r"(?<![A-Za-z0-9_$.])fetch\s*\(", body)


# ===========================================================================
# 13: a damaged record deletes nothing but the recipient's copies in THIS chat
# ===========================================================================
OTHER_CHAT = "c_sharedialog0002"


def _history_rows(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_a_damaged_record_never_deletes_the_owners_conversation(world):
    """The record for B on conv1 names the OWNER's conv2 as B's "copy" (a
    hand-damaged meta). Remove must not follow it to conv2's file: an id is a
    copy only when it is a conversation of the recipient in this chat."""
    conv1, conv2 = world["conv1"], world["conv2"]

    def damage(meta):
        meta["sharing"] = {"shared_with": [B],
                           "conv_shares": {conv1: {B: [conv2]}},
                           "conv_granted": [B]}
    _rewrite_meta(world, damage)
    assert AuthStore().record_shared_chat(B, CHAT, TITLE, [], shared_by=A)
    bytes_before = _conv_file(world, conv2).read_bytes()
    rows_before = _conversations(world, "a")
    assert _listed(world, conv1) == [B]

    out = _remove(world, conv1, B)

    assert out["shared_with"] == []
    assert _conv_file(world, conv2).is_file(), "the owner's conversation file was deleted"
    assert _conv_file(world, conv2).read_bytes() == bytes_before
    assert [r["content"] for r in _history_rows(_conv_file(world, conv2))] == [
        "question 2", "answer 2"]
    assert _conversations(world, "a") == rows_before
    assert conv2 in [row["conv_id"] for row in _conversations(world, "a")]
    r = world["a"].get(f"/api/chat/{CHAT}/conversation/{conv2}/history")
    assert r.status_code == 200
    assert len(r.json()["history"]) == 2
    # the record for B is gone
    assert _listed(world, conv1) == []
    assert not any(B in (per or {})
                   for per in (_sharing(world).get("conv_shares") or {}).values())
    _assert_no_empty_record_keys(world)


def test_a_damaged_record_never_deletes_the_recipients_conversation_in_another_chat(world):
    """The record names a conversation B owns in ANOTHER chat. Remove on this
    chat must leave B's index row and that chat's file alone."""
    auth = AuthStore()
    other = local_store.ChatDataStore(OTHER_CHAT)
    meta = other.read_meta()
    meta["owner"] = B
    meta["title"] = "Other"
    other.write_meta(meta)
    auth.record_active_chat(B, OTHER_CHAT, "Other", [])
    cv_other = other.new_conversation("other")
    other.append_history(cv_other, {"role": "human", "content": "other question"})
    other.append_history(cv_other, {"role": "ai", "content": "other answer"})
    auth.record_conversation(B, OTHER_CHAT, cv_other, "other")
    other_file = world["tmp"] / "chatdata" / OTHER_CHAT / "conversations" / f"{cv_other}.jsonl"
    assert other_file.is_file()
    bytes_before = other_file.read_bytes()

    def b_other_rows():
        r = world["b"].get("/auth/conversations")
        assert r.status_code == 200, r.text
        return [row for row in r.json()["conversations"] if row.get("conv_id") == cv_other]

    rows_before = b_other_rows()
    assert len(rows_before) == 1 and rows_before[0]["chat_id"] == OTHER_CHAT

    conv1 = world["conv1"]

    def damage(meta):
        meta["sharing"] = {"shared_with": [B],
                           "conv_shares": {conv1: {B: [cv_other]}},
                           "conv_granted": [B]}
    _rewrite_meta(world, damage)
    assert auth.record_shared_chat(B, CHAT, TITLE, [], shared_by=A)

    out = _remove(world, conv1, B)

    assert out["shared_with"] == []
    assert b_other_rows() == rows_before, "the recipient's row in another chat was deleted"
    assert other_file.is_file(), "a conversation file of another chat was deleted"
    assert other_file.read_bytes() == bytes_before
    r = world["b"].get(f"/api/chat/{OTHER_CHAT}/conversation/{cv_other}/history")
    assert r.status_code == 200
    assert len(r.json()["history"]) == 2
    assert _listed(world, conv1) == []


# ===========================================================================
# 14: a mixed-case entry from before the record keeps the chat access
# ===========================================================================
def test_remove_keeps_a_mixed_case_pre_release_shared_with_entry(world):
    """An older release stored the address as typed. The conversation share
    (lower-case) must recognise it as already having access, so its Remove
    does not take away what a chat share gave."""
    mixed = "Boris@Acme.com"
    assert mixed.lower() == B and mixed != B
    _rewrite_meta(world, lambda meta: meta.__setitem__("sharing", {"shared_with": [mixed]}))
    assert AuthStore().record_shared_chat(B, CHAT, TITLE, [], shared_by=A)
    assert world["b"].get(f"/api/chat/{CHAT}/share").status_code == 200

    copy = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    assert B not in [str(a).lower() for a in (_sharing(world).get("conv_granted") or [])]
    assert _listed(world, world["conv1"]) == [B]

    out = _remove(world, world["conv1"], B)

    assert out["chat_access_removed"] is False
    assert B in [str(a).strip().lower() for a in _chat_recipients(world)]
    assert _lists_chat(world, "b")
    # the access probe: `_require_chat` still lets B in
    assert world["b"].get(f"/api/chat/{CHAT}/share").status_code == 200
    assert _copies(world, "b") == []
    assert not _conv_file(world, copy).exists()


# ===========================================================================
# 15: documented limits, pinned as the current behaviour
# ===========================================================================
def test_remove_after_the_recipient_deleted_their_own_copy(world):
    copy = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]

    r = world["b"].post("/auth/conversations/delete", json={"conv_id": copy})
    assert r.status_code == 200, r.text
    assert _copies(world, "b") == []
    assert not _conv_file(world, copy).exists()

    # Documented limit: the recipient deleting their copy does not touch the
    # owner's record, so the conversation still lists them.
    assert _listed(world, world["conv1"]) == [B]

    out = _remove(world, world["conv1"], B)

    assert out["shared_with"] == []
    assert out["chat_access_removed"] is True      # B had nothing else of the chat
    assert _listed(world, world["conv1"]) == []
    assert B not in _chat_recipients(world)
    assert not _lists_chat(world, "b")
    assert not _address_in_records(world, B)
    _assert_no_empty_record_keys(world)


def test_pin_deleting_a_source_conversation_leaves_its_record(world):
    """Documented limit, pinned as CURRENT behaviour (not a requirement): the
    owner deleting a conversation they shared does not clear its record; the
    recipient keeps the chat access and their copy until a Share Chat Remove
    (or any other way the address leaves the chat) drops the record."""
    conv2 = world["conv2"]
    copy = _share_conv(world, conv2, [B])["snapshot_conv_ids"][B]

    r = world["a"].post("/auth/conversations/delete", json={"conv_id": conv2})
    assert r.status_code == 200, r.text
    assert conv2 not in [row["conv_id"] for row in _conversations(world, "a")]

    sharing = _sharing(world)
    assert B in sharing["conv_shares"][conv2]
    # the conversation left the owner's index: its share routes answer 404
    r = world["a"].get(_conv_share_url(conv2))
    assert r.status_code == 404
    assert r.json() == {"error": "Conversation not found"}
    # B still has the chat and the copy
    assert B in _chat_recipients(world)
    assert _lists_chat(world, "b")
    assert _history_status(world, "b", copy) == 200

    r = world["a"].delete(f"/api/chat/{CHAT}/share/{B}", headers=JSON_HEADERS)
    assert r.status_code == 200, r.text
    assert B not in r.json()["shared_with"]
    assert not _address_in_records(world, B)
    sharing = _sharing(world)
    assert "conv_shares" not in sharing
    assert "conv_granted" not in sharing


# ===========================================================================
# 16: dashboard unshare first, the conversation Remove second
# ===========================================================================
def test_dashboard_unshare_then_conversation_remove_ends_the_access(world):
    copy = _share_conv(world, world["conv1"], [B])["snapshot_conv_ids"][B]
    assert B in _sharing(world)["conv_granted"]
    dash_id = _dashboard_with_tile(world)
    _share_dashboard(world, dash_id, [B])       # B already on the list: no grant recorded

    _unshare_dashboard(world, dash_id, B)

    # the conversation share still holds the access
    assert B in _chat_recipients(world)
    assert _lists_chat(world, "b")
    assert _listed(world, world["conv1"]) == [B]
    assert _history_status(world, "b", copy) == 200

    out = _remove(world, world["conv1"], B)

    assert out["chat_access_removed"] is True
    assert out["shared_with"] == []
    assert B not in _chat_recipients(world)
    assert not _lists_chat(world, "b")
    assert not _conv_file(world, copy).exists()
    assert not _address_in_records(world, B)


# ===========================================================================
# 17: the share mail is what it was
# ===========================================================================
def test_pin_conversation_share_sends_one_mail_to_the_recipients(world):
    mails = world["mails"]
    assert mails == []

    _share_conv(world, world["conv1"], [B, C])

    assert len(mails) == 1
    assert list(mails[0]["to"]) == [B, C]
    assert mails[0]["chat_title"] == TITLE
    assert mails[0]["sender_email"] == A

    _share_conv(world, world["conv2"], [C])

    assert len(mails) == 2
    assert list(mails[1]["to"]) == [C]
    assert mails[1]["chat_title"] == TITLE

    # a Remove sends nothing
    _remove(world, world["conv1"], B)
    assert len(mails) == 2


# ===========================================================================
# 18: structure -- handleShare's Remove request and its choice of routes
# ===========================================================================
def test_handle_share_remove_request_declares_the_json_content_type():
    """A DELETE without the JSON header is a 415 (app.JsonContentTypeGate)."""
    body = _handle_share_body()
    assert re.search(
        r"method\s*:\s*['\"]DELETE['\"]\s*,[^}]*?headers\s*:\s*\{[^}]*"
        r"['\"]Content-Type['\"]\s*:\s*['\"]application/json['\"]", body, flags=re.S), \
        "the DELETE request options in handleShare carry no JSON Content-Type"


def test_handle_share_has_both_list_and_both_remove_urls():
    body = _handle_share_body()
    # the lists
    assert re.search(r"`/api/chat/\$\{chatId\}/share`", body)
    assert re.search(r"`/api/chat/\$\{chatId\}/conversation/\$\{convId\}/share`", body)
    # the removes
    assert re.search(r"`/api/chat/\$\{chatId\}/share/\$\{", body)
    assert re.search(r"`/api/chat/\$\{chatId\}/conversation/\$\{convId\}/share/\$\{", body)


def test_handle_share_picks_the_conversation_urls_for_a_conversation_only():
    body = _handle_share_body()
    cond = re.search(r"type\s*===\s*['\"]conversation['\"]", body)
    assert cond, "handleShare has no `type === 'conversation'` condition"
    # the condition is stated before the conversation URLs are built ...
    assert cond.start() < body.index("/conversation/${convId}/share")
    # ... and each conversation URL is one branch of a choice whose other
    # branch is the chat URL (ternary or if/else, within a few lines).
    for m in re.finditer(r"/api/chat/\$\{chatId\}/conversation/\$\{convId\}/share", body):
        nearby = body[max(0, m.start() - 400): m.end() + 400]
        assert re.search(r"type\s*===\s*['\"]conversation['\"]|isConvShare", nearby), nearby
        assert re.search(r"/api/chat/\$\{chatId\}/share(?![\w/]|\$)", nearby) or \
            re.search(r"/api/chat/\$\{chatId\}/share/\$\{", nearby), \
            "a conversation URL without the chat URL as its alternative"
