"""A shared chat arrives under the name the OWNER currently uses for it.

A chat's name is stored twice: the chat meta `title` (written once, at
creation) and the `title` of each user's own `active_chats.jsonl` row, which
is what `POST /auth/active_chats/rename` rewrites. Both share routes -- the
chat share (`POST /api/chat/{chat_id}/share`) and the conversation share
(`POST /auth/conversations/{conv_id}/share`) -- used to hand the recipient the
stale META title and mail it as `chat_title`, so a chat the owner had renamed
arrived under its old name. The fix pinned here:

* both routes use the owner's CURRENT row title (meta title only when the
  owner no longer lists the chat) for the recipient's row and for the mail;
* the recipient's new row is made unique against the titles they already
  list (`name`, `name_2`, `name_3`); the mail always carries the owner's name;
* a re-share REPAIRS a shared row still carrying the original (meta) title,
  and never touches a row the recipient renamed or a row of their own;
* `GET /auth/active_chats` rows carry `is_shared`, and the sidebar menu lets a
  recipient rename a shared chat (their own row only).

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

OWNER = "owner@acme.com"
FRIEND = "friend@acme.com"
THIRD = "third@acme.com"
NOBODY = "nobody@acme.com"
CHAT = "c_sharenames0001"
OTHER_CHAT = "c_sharenames0002"
THIRD_CHAT = "c_sharenames0003"
META_TITLE = "Sales"
RENAMED = "Q3 Review"

ROUTES = ["chat", "conversation"]

DASHBOARD_JS = Path(__file__).resolve().parents[1] / "static" / "dashboard.js"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    auth = AuthStore()
    for email in (OWNER, FRIEND, THIRD):
        auth.ensure_user(email)
        auth.set_password(email, "pw-" + email)

    store = local_store.ChatDataStore(CHAT)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["title"] = META_TITLE
    store.write_meta(meta)
    # The creation flow lists the chat in the owner's sidebar under the name
    # it was created with.
    auth.record_active_chat(OWNER, CHAT, META_TITLE, [])
    owner_conv = store.new_conversation("o")
    store.append_history(owner_conv, {"role": "human", "content": "q"})
    auth.record_conversation(OWNER, CHAT, owner_conv, "o")

    import routes.auth as auth_mod
    import routes.chat as chat_mod

    mails = []

    def fake_mail(**kw):
        mails.append(kw)
        return {"smtp_configured": True, "sent": kw.get("to") or [], "failed": []}

    for mod in (auth_mod, chat_mod):
        monkeypatch.setattr(mod.brain_client, "send_share_email", fake_mail)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    def client_for(email):
        tc = TestClient(app)
        assert tc.post(f"/_login/{email}", json={}).status_code == 200
        return tc

    return {"owner": client_for(OWNER), "friend": client_for(FRIEND),
            "owner_conv": owner_conv, "mails": mails, "tmp": tmp_path}


# --- helpers -----------------------------------------------------------------

def _share(world, route, emails=(FRIEND,)):
    tc = world["owner"]
    if route == "chat":
        r = tc.post(f"/api/chat/{CHAT}/share", json={"emails": list(emails)})
    else:
        r = tc.post(f"/auth/conversations/{world['owner_conv']}/share",
                    json={"emails": list(emails)})
    assert r.status_code == 200, r.text
    return r


def _rename(tc, title, chat_id=CHAT):
    r = tc.post("/auth/active_chats/rename", json={"chat_id": chat_id, "title": title})
    assert r.status_code == 200, r.text
    return r


def _rows(email, chat_id=CHAT):
    return [r for r in AuthStore().list_active_chats(email) if r.get("chat_id") == chat_id]


def _row(email, chat_id=CHAT):
    rows = _rows(email, chat_id)
    assert len(rows) == 1, f"expected exactly one row for {chat_id} of {email}: {rows}"
    return rows[0]


def _meta_title():
    return local_store.ChatDataStore(CHAT).read_meta().get("title")


def _chats_file(tmp, email):
    return tmp / "users" / email / "active_chats.jsonl"


# ===========================================================================
# 1 + 2: the recipient gets the owner's CURRENT name, and so does the mail
# ===========================================================================
def test_chat_share_uses_the_owners_renamed_title_for_row_and_mail(world):
    _rename(world["owner"], RENAMED)
    _share(world, "chat")
    assert _row(FRIEND)["title"] == RENAMED
    assert len(world["mails"]) == 1
    assert world["mails"][0]["chat_title"] == RENAMED


def test_conversation_share_uses_the_owners_renamed_title_for_row_and_mail(world):
    _rename(world["owner"], RENAMED)
    _share(world, "conversation")
    row = _row(FRIEND)
    assert row["title"] == RENAMED
    assert row["shared_by"] == OWNER
    assert len(world["mails"]) == 1
    assert world["mails"][0]["chat_title"] == RENAMED


# ===========================================================================
# 3: a name the recipient already lists gets a number; the mail does not
# ===========================================================================
@pytest.mark.parametrize("route", ROUTES)
def test_share_numbers_the_title_when_the_recipient_already_lists_that_name(world, route):
    AuthStore().record_active_chat(FRIEND, OTHER_CHAT, RENAMED, [])
    _rename(world["owner"], RENAMED)
    _share(world, route)
    assert _row(FRIEND)["title"] == RENAMED + "_2"
    # the recipient's other chat and the owner's own row keep their names
    assert _row(FRIEND, OTHER_CHAT)["title"] == RENAMED
    assert _row(OWNER)["title"] == RENAMED
    assert world["mails"][-1]["chat_title"] == RENAMED


@pytest.mark.parametrize("route", ROUTES)
def test_share_takes_the_next_free_number_counting_own_and_shared_rows(world, route):
    auth = AuthStore()
    auth.record_active_chat(FRIEND, OTHER_CHAT, RENAMED, [])                 # own row
    auth.record_shared_chat(FRIEND, THIRD_CHAT, RENAMED + "_2", [], shared_by=THIRD)
    assert _row(FRIEND, THIRD_CHAT)["title"] == RENAMED + "_2"
    _rename(world["owner"], RENAMED)
    _share(world, route)
    assert _row(FRIEND)["title"] == RENAMED + "_3"
    assert _row(OWNER)["title"] == RENAMED
    assert world["mails"][-1]["chat_title"] == RENAMED


def test_title_collision_is_exact_and_case_sensitive(world):
    AuthStore().record_active_chat(FRIEND, OTHER_CHAT, RENAMED.lower(), [])
    _rename(world["owner"], RENAMED)
    _share(world, "chat")
    assert _row(FRIEND)["title"] == RENAMED


# ===========================================================================
# 4: a re-share repairs a shared row that still carries the original title
# ===========================================================================
@pytest.mark.parametrize("route", ROUTES)
def test_reshare_repairs_a_shared_row_still_holding_the_original_title(world, route):
    auth = AuthStore()
    # what the bug wrote: the META title, although the owner uses another name
    assert auth.record_shared_chat(FRIEND, CHAT, META_TITLE, [], shared_by=OWNER)
    assert auth.set_chat_pinned(FRIEND, CHAT, True)
    created_at = _row(FRIEND).get("created_at")
    _rename(world["owner"], RENAMED)

    _share(world, route)

    row = _row(FRIEND)                     # still exactly one row for the chat
    assert row["title"] == RENAMED
    assert row["shared_by"] == OWNER
    assert row.get("pinned") is True
    assert row.get("created_at") == created_at


@pytest.mark.parametrize("route", ROUTES)
def test_repair_numbers_the_title_on_a_collision(world, route):
    auth = AuthStore()
    auth.record_shared_chat(FRIEND, CHAT, META_TITLE, [], shared_by=OWNER)
    auth.record_active_chat(FRIEND, OTHER_CHAT, RENAMED, [])
    _rename(world["owner"], RENAMED)

    _share(world, route)

    assert _row(FRIEND)["title"] == RENAMED + "_2"
    assert _row(FRIEND, OTHER_CHAT)["title"] == RENAMED


# ===========================================================================
# 5: what the recipient named, and what the recipient owns, is never touched
# ===========================================================================
@pytest.mark.parametrize("route", ROUTES)
def test_reshare_leaves_a_row_the_recipient_renamed_untouched(world, route):
    _share(world, route)
    assert _row(FRIEND)["title"] == META_TITLE
    _rename(world["friend"], "My own name")
    _rename(world["owner"], RENAMED)

    _share(world, route)

    row = _row(FRIEND)
    assert row["title"] == "My own name"
    assert row["shared_by"] == OWNER


def test_record_shared_chat_never_touches_the_recipients_own_row(world):
    auth = AuthStore()
    auth.record_active_chat(FRIEND, CHAT, META_TITLE, [])      # no shared_by
    before = _chats_file(world["tmp"], FRIEND).read_text(encoding="utf-8")

    assert auth.record_shared_chat(FRIEND, CHAT, RENAMED, [], shared_by=OWNER,
                                   original_title=META_TITLE) is True

    assert _chats_file(world["tmp"], FRIEND).read_text(encoding="utf-8") == before
    row = _row(FRIEND)
    assert row["title"] == META_TITLE
    assert "shared_by" not in row


# ===========================================================================
# 6 + 7: without a rename, or without an owner row, the meta title is used
# ===========================================================================
@pytest.mark.parametrize("route", ROUTES)
def test_never_renamed_chat_is_shared_under_the_meta_title(world, route):
    _share(world, route)
    row = _row(FRIEND)
    assert row["title"] == META_TITLE
    assert row["shared_by"] == OWNER
    assert len(world["mails"]) == 1
    assert world["mails"][0]["chat_title"] == META_TITLE


@pytest.mark.parametrize("route", ROUTES)
def test_meta_title_is_the_fallback_when_the_owner_no_longer_lists_the_chat(world, route):
    _rename(world["owner"], RENAMED)
    assert AuthStore().deactivate_chat(OWNER, CHAT)
    assert _rows(OWNER) == []

    _share(world, route)

    assert _row(FRIEND)["title"] == META_TITLE
    assert world["mails"][-1]["chat_title"] == META_TITLE


# ===========================================================================
# 8: store level
# ===========================================================================
def test_active_chat_title_returns_the_listed_rows_title(world):
    auth = AuthStore()
    assert auth.active_chat_title(OWNER, CHAT) == META_TITLE
    auth.rename_active_chat(OWNER, CHAT, RENAMED)
    assert auth.active_chat_title(OWNER, CHAT) == RENAMED


def test_active_chat_title_is_none_for_a_chat_not_listed(world):
    auth = AuthStore()
    assert auth.active_chat_title(OWNER, OTHER_CHAT) is None
    assert auth.active_chat_title(FRIEND, CHAT) is None      # no file at all


def test_active_chat_title_is_none_for_an_empty_title(world):
    auth = AuthStore()
    auth.record_active_chat(OWNER, OTHER_CHAT, "", [])
    assert auth.active_chat_title(OWNER, OTHER_CHAT) is None


def test_active_chat_title_survives_a_garbage_line(world):
    path = _chats_file(world["tmp"], OWNER)
    path.write_text("{not json at all\n" + path.read_text(encoding="utf-8") + "[1, 2\n",
                    encoding="utf-8")
    auth = AuthStore()
    assert auth.active_chat_title(OWNER, CHAT) == META_TITLE
    assert auth.active_chat_title(OWNER, OTHER_CHAT) is None


def test_active_chat_title_is_none_for_an_unknown_user_and_creates_nothing(world):
    assert AuthStore().active_chat_title(NOBODY, CHAT) is None
    assert not (world["tmp"] / "users" / NOBODY).exists()


def test_record_shared_chat_without_original_title_leaves_a_listed_chat_alone(world):
    auth = AuthStore()
    auth.record_shared_chat(FRIEND, CHAT, META_TITLE, [], shared_by=OWNER)
    before = _chats_file(world["tmp"], FRIEND).read_text(encoding="utf-8")

    assert auth.record_shared_chat(FRIEND, CHAT, RENAMED, [], shared_by=OWNER) is True

    assert _chats_file(world["tmp"], FRIEND).read_text(encoding="utf-8") == before
    assert _row(FRIEND)["title"] == META_TITLE


def test_record_shared_chat_refuses_an_address_without_an_account(world):
    assert AuthStore().record_shared_chat(NOBODY, CHAT, RENAMED, [], shared_by=OWNER,
                                          original_title=META_TITLE) is False
    assert not (world["tmp"] / "users" / NOBODY).exists()


def test_record_shared_chat_new_row_shape(world):
    """Stored shape: the new row keeps the keys older releases wrote."""
    assert AuthStore().record_shared_chat(FRIEND, CHAT, RENAMED, ["a.csv"],
                                          shared_by=OWNER, original_title=META_TITLE) is True
    lines = _chats_file(world["tmp"], FRIEND).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["chat_id"] == CHAT
    assert row["title"] == RENAMED
    assert row["files"] == ["a.csv"]
    assert row["shared_by"] == OWNER
    assert row.get("created_at")


# ===========================================================================
# 9: a rename stays in the renamer's own row
# ===========================================================================
def test_recipients_rename_changes_only_the_recipients_row(world):
    _share(world, "chat")
    _rename(world["friend"], "Friend's name")
    assert _row(FRIEND)["title"] == "Friend's name"
    assert _row(OWNER)["title"] == META_TITLE
    assert _meta_title() == META_TITLE


def test_owners_rename_changes_only_the_owners_row_until_a_reshare(world):
    _share(world, "chat")
    _rename(world["owner"], RENAMED)
    assert _row(OWNER)["title"] == RENAMED
    assert _meta_title() == META_TITLE
    assert _row(FRIEND)["title"] == META_TITLE     # nothing pushes the rename


# ===========================================================================
# 10: GET /auth/active_chats marks shared rows
# ===========================================================================
def test_active_chats_listing_marks_shared_rows(world):
    _rename(world["owner"], RENAMED)
    _share(world, "chat")

    r = world["friend"].get("/auth/active_chats")
    assert r.status_code == 200
    friend_rows = [x for x in r.json()["active_chats"] if x["chat_id"] == CHAT]
    assert len(friend_rows) == 1
    assert friend_rows[0]["is_shared"] is True
    assert friend_rows[0]["title"] == RENAMED
    assert friend_rows[0]["name"] == friend_rows[0]["title"]

    r = world["owner"].get("/auth/active_chats")
    assert r.status_code == 200
    owner_rows = [x for x in r.json()["active_chats"] if x["chat_id"] == CHAT]
    assert len(owner_rows) == 1
    assert owner_rows[0]["is_shared"] is False
    assert owner_rows[0]["name"] == owner_rows[0]["title"] == RENAMED


def test_active_chats_listing_marks_every_row(world):
    AuthStore().record_active_chat(FRIEND, OTHER_CHAT, "Mine", [])
    _share(world, "chat")
    rows = world["friend"].get("/auth/active_chats").json()["active_chats"]
    assert {x["chat_id"]: x["is_shared"] for x in rows} == {CHAT: True, OTHER_CHAT: False}


# ===========================================================================
# 11: the sidebar menu offers Rename on a shared chat, Share still does not
# ===========================================================================
def _balanced(text, start, open_ch, close_ch):
    """The text between the bracket at `start` and its match (exclusive)."""
    assert text[start] == open_ch
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    raise AssertionError(f"unbalanced {open_ch}{close_ch} from offset {start}")


def _show_item_menu_body():
    src = DASHBOARD_JS.read_text(encoding="utf-8")
    heads = list(re.finditer(r"function\s+showItemMenu\s*\([^)]*\)\s*\{", src))
    assert len(heads) == 1, "expected exactly one showItemMenu definition"
    return _balanced(src, heads[0].end() - 1, "{", "}")


def _guard_of(body, action):
    """The condition of the nearest `if (` in front of the block that emits
    the menu item `data-action="<action>"`."""
    marker = f'data-action="{action}"'
    assert body.count(marker) == 1, f"{marker} must be emitted exactly once in showItemMenu"
    at = body.index(marker)
    ifs = [m for m in re.finditer(r"\bif\s*\(", body[:at])]
    assert ifs, f"no condition in front of {marker}"
    cond = _balanced(body, ifs[-1].end() - 1, "(", ")")
    # the item must sit in that condition's own block, not after it
    between = body[ifs[-1].end() + len(cond) + 1:at]
    assert between.count("{") - between.count("}") == 1, (
        f"{marker} is not directly inside its nearest `if` block")
    return cond


def test_show_item_menu_is_found_with_its_menu_items():
    body = _show_item_menu_body()
    assert "isShared" in body and "isPublished" in body
    for action in ("rename", "share", "delete"):
        assert f'data-action="{action}"' in body


def test_rename_menu_item_is_not_gated_on_is_shared():
    cond = _guard_of(_show_item_menu_body(), "rename")
    assert "isShared" not in cond, f"Rename is still hidden for shared chats: if ({cond})"
    assert "!isPublished" in cond


def test_share_menu_item_is_still_gated_on_is_shared():
    cond = _guard_of(_show_item_menu_body(), "share")
    assert "!isShared" in cond, f"Share must stay owner-only: if ({cond})"
    assert "!isPublished" in cond
