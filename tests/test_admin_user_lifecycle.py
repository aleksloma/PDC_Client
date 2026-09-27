"""Task 14 items 3 and 4 — the administrator ends a user's sessions and
removes a user.

The contract pinned here (Task 14 items 3 and 4, D14-1..D14-5):

* `POST /api/admin/users/end_sessions`, body `{email}` (body-carried like
  every user route). Admin-guarded (401 / 403 before the body is read); the
  bootstrap local admin as the target → 400; an unknown address → 404; the
  caller's own account is allowed (D14-2). It writes a fresh session
  generation through `AuthStore.bump_session_generation`, so every session of
  that account ends on its next request (the Task 9b
  `SessionGenerationGate`) — password accounts and SSO-only accounts alike
  (an SSO-only account keeps no password: it stays SSO-only). Audited
  `user.sessions_ended`.
* `POST /api/admin/users/remove`, body `{email}`. Refuses the bootstrap local
  admin AND the caller's own account (400); unknown → 404. Deletes
  `users/{email}/` and EVERY `chatdata/{chat_id}/` whose meta owner is the
  address — a deactivated chat (no `active_chats` row) included, another
  owner's chat untouched. Answers `{ok: true, chats_deleted: N}`; audited
  `user.removed` with the counts. A recipient of the removed owner's shared
  dashboard gets not-found, and their dashboard list drops the pointer.
* Sessions of a removed account end on the next request, including an
  SSO-only session stamped `gen: ""` and after a process restart (the
  generation cache emptied): `AuthStore.session_generation` answers the
  fixed non-hex sentinel `local_store.SESSION_GEN_REMOVED` — UNCACHED — for
  an address with neither profile.json nor auth.json, while a profile-only
  account still answers "" (upgrade back-compat). A re-invited address reads
  "" again, and an SSO account that signs back in after a removal can be
  removed (and signed out) a second time.
* The admin page carries both actions with a confirmation step and no
  inline handler.

Real app (`app.app`, the tests/test_session_generation.py idiom): TestClient
without the context manager (no lifespan), https base_url so the session
cookie comes back. Offline: DATA_ROOT is tmp_path, every brain call stubbed.
"""
import json
import re
from base64 import b64encode
from pathlib import Path

import pytest
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import db_sources
import local_store
import roles_store
from settings import settings

ROOT = Path(__file__).resolve().parent.parent

ADMIN = "ladmin"
ADMIN_PW = "Ladmin-passw0rd"
BOSS = "boss@corp.example"            # a PROMOTED admin
BOSS_PW = "Boss-passw0rd"
USER = "life.user@corp.example"
USER_PW = "Life-user-passw0rd"
OWNER = "life.owner@corp.example"
OWNER_PW = "Life-owner-passw0rd"
RCPT = "life.rcpt@corp.example"
RCPT_PW = "Life-rcpt-passw0rd"
OTHER = "life.other@corp.example"
SSO = "life.entra@corp.example"
GHOST = "nobody.here@corp.example"

CHAT_ACTIVE = "c_lifeowned01"
CHAT_DEACT = "c_lifeowned02"
CHAT_OTHER = "c_lifeother01"

_HEX16 = re.compile(r"[0-9a-f]{16}")


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", ADMIN)
    for name, value in (("ALLOW_SELF_REGISTRATION", False), ("PASSWORD_MIN_LENGTH", 8)):
        if name in type(settings).model_fields:
            monkeypatch.setattr(settings, name, value)
    local_store._DATAFRAME_CACHE.invalidate()
    local_store._SESSION_GEN_CACHE.clear()
    local_store._RESET_TOKEN_INDEX.clear()
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(brain_client, "send_password_reset_email",
                        lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    monkeypatch.setattr(auth_mod, "_run_in_background",
                        lambda fn, *args: fn(*args), raising=False)

    store = local_store.AuthStore()
    for email, pw in ((ADMIN, ADMIN_PW), (BOSS, BOSS_PW), (USER, USER_PW),
                      (OWNER, OWNER_PW), (RCPT, RCPT_PW)):
        store.ensure_user(email)
        store.set_password(email, pw)
    store.set_role(ADMIN, "admin")
    store.set_role(BOSS, "admin")
    store.ensure_user(OTHER)
    store.ensure_user(SSO)
    store.mark_sso_login(SSO, "microsoft")
    roles_store.RolesStore().ensure_base_role()
    yield {"tmp": tmp_path}
    local_store._DATAFRAME_CACHE.invalidate()
    local_store._SESSION_GEN_CACHE.clear()


def _client():
    return TestClient(app_mod.app, base_url="https://testserver",
                      raise_server_exceptions=False)


def _signed_in(email, password, expect="/lab"):
    tc = _client()
    r = tc.post("/auth/login", data={"email": email, "password": password},
                follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert r.headers["location"] == expect, r.headers["location"]
    assert tc.get("/auth/me").status_code == 200, "the sign-in did not hold"
    return tc


def _admin():
    return _signed_in(ADMIN, ADMIN_PW, expect="/admin/data_sources")


def _cookie_session(email):
    """A signed-in session the way the SSO callback leaves one (no password
    involved): `gen` is the account's CURRENT generation — "" for an
    SSO-only account that never had one."""
    session = {"email": email, "sid": "s_0123456789abcdef",
               "gen": local_store.AuthStore().session_generation(email)}
    data = b64encode(json.dumps(session).encode("utf-8"))
    tc = _client()
    tc.cookies.set("session",
                   TimestampSigner(str(settings.SECRET_KEY)).sign(data).decode("utf-8"))
    return tc


def _alive(tc) -> bool:
    r = tc.get("/auth/profile")
    assert r.status_code in (200, 401), (r.status_code, r.text[:200])
    return r.status_code == 200


def _end_sessions(tc, email):
    return tc.post("/api/admin/users/end_sessions", json={"email": email})


def _remove(tc, email):
    return tc.post("/api/admin/users/remove", json={"email": email})


def _audit(action):
    return [r for r in db_sources.read_audit_tail(500) if r.get("action") == action]


def _sentinel():
    value = getattr(local_store, "SESSION_GEN_REMOVED", None)
    if value is None:
        pytest.fail("local_store.SESSION_GEN_REMOVED is missing")
    return value


def _chat(chat_id, owner, *, active=True, shared_with=()):
    store = local_store.ChatDataStore(chat_id)
    (store.files_dir / "d.csv").write_text("x\n1\n2\n", encoding="utf-8")
    meta = store.read_meta()
    meta["owner"] = owner
    meta["files"] = [{"file_name": "d.csv", "file_description": "",
                      "schema": {"file_name": "d.csv", "fields": {}}}]
    store.write_meta(meta)
    if shared_with:
        store.add_share_recipients(list(shared_with))
    if active:
        local_store.AuthStore().record_active_chat(owner, chat_id, chat_id, ["d.csv"])
    return store


@pytest.fixture
def estate(world):
    """OWNER's estate: an active chat shared with RCPT, a DEACTIVATED chat
    (meta owner, no active_chats row) and a dashboard shared with RCPT;
    OTHER owns an unrelated chat."""
    _chat(CHAT_ACTIVE, OWNER, shared_with=[RCPT])
    _chat(CHAT_DEACT, OWNER, active=False)
    _chat(CHAT_OTHER, OTHER)
    ds = local_store.DashboardStore()
    dash = ds.create_dashboard(OWNER, "Owned board")
    assert ds.add_dashboard_share(OWNER, dash["dash_id"], [RCPT]) == [RCPT]
    return {"dash_id": dash["dash_id"], "tmp": world["tmp"]}


# ===========================================================================
# end_sessions
# ===========================================================================
def test_end_sessions_refuses_the_bootstrap_admin(world):
    r = _end_sessions(_admin(), ADMIN)
    assert r.status_code == 400, (r.status_code, r.text[:300])


def test_end_sessions_of_an_unknown_address_is_404(world):
    r = _end_sessions(_admin(), GHOST)
    assert r.status_code == 404, (r.status_code, r.text[:300])
    assert "error" in r.json(), "the route's own 404, not a missing route"
    assert not (world["tmp"] / "users" / GHOST).exists(), \
        "a 404 must not create the account"


def test_end_sessions_ends_every_session_of_a_password_account(world):
    a = _signed_in(USER, USER_PW)
    b = _signed_in(USER, USER_PW)
    assert _alive(a) and _alive(b)
    r = _end_sessions(_admin(), USER)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert r.json().get("ok") is True, r.json()
    assert not _alive(a), "the first session survived end_sessions"
    assert not _alive(b), "the second session survived end_sessions"
    # The password is untouched: the user signs in again.
    assert _alive(_signed_in(USER, USER_PW))


def test_end_sessions_ends_an_sso_only_session_and_keeps_it_sso_only(world):
    tc = _cookie_session(SSO)
    assert _alive(tc), "the SSO-only session did not hold before the change"
    r = _end_sessions(_admin(), SSO)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert not _alive(tc), "an SSO-only session (gen \"\") survived end_sessions"
    store = local_store.AuthStore()
    assert store.is_sso_only(SSO) is True
    assert store.has_password(SSO) is False


def test_an_admin_may_end_their_own_sessions(world):
    boss = _signed_in(BOSS, BOSS_PW)
    other = _signed_in(BOSS, BOSS_PW)
    r = _end_sessions(boss, BOSS)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert not _alive(other)
    assert not _alive(boss)


def test_end_sessions_is_audited(world):
    assert _end_sessions(_admin(), USER).status_code == 200
    rows = [r for r in _audit("user.sessions_ended") if r.get("target") == USER]
    assert rows, db_sources.read_audit_tail(20)
    assert rows[0].get("actor") == ADMIN


def test_end_sessions_is_admin_guarded(world):
    assert _end_sessions(_client(), USER).status_code == 401
    user = _signed_in(USER, USER_PW)
    assert _end_sessions(user, RCPT).status_code == 403
    rcpt = _signed_in(RCPT, RCPT_PW)
    assert _alive(rcpt), "a refused call must end nothing"


# ===========================================================================
# remove — refusals
# ===========================================================================
def test_remove_refuses_the_bootstrap_admin(world):
    r = _remove(_admin(), ADMIN)
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert local_store.AuthStore().user_exists(ADMIN)


def test_remove_refuses_the_callers_own_account(world):
    boss = _signed_in(BOSS, BOSS_PW)
    r = _remove(boss, BOSS)
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert local_store.AuthStore().user_exists(BOSS)
    assert _alive(boss)


def test_remove_of_an_unknown_address_is_404(world):
    r = _remove(_admin(), GHOST)
    assert r.status_code == 404, (r.status_code, r.text[:300])
    assert "error" in r.json(), "the route's own 404, not a missing route"


def test_remove_is_admin_guarded(world, estate):
    assert _remove(_client(), OWNER).status_code == 401
    assert _remove(_signed_in(USER, USER_PW), OWNER).status_code == 403
    assert local_store.AuthStore().user_exists(OWNER)
    assert (estate["tmp"] / "chatdata" / CHAT_ACTIVE).is_dir()


# ===========================================================================
# remove — what is deleted
# ===========================================================================
def test_remove_deletes_the_account_and_every_chat_it_owns(world, estate):
    tmp = estate["tmp"]
    assert (tmp / "users" / OWNER / "dashboards").is_dir()
    r = _remove(_admin(), OWNER)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    body = r.json()
    assert body.get("ok") is True, body
    assert body.get("chats_deleted") == 2, body
    assert not (tmp / "users" / OWNER).exists(), "the account folder survived"
    assert not (tmp / "chatdata" / CHAT_ACTIVE).exists()
    assert not (tmp / "chatdata" / CHAT_DEACT).exists(), \
        "a deactivated chat (no active_chats row) survived the removal"
    assert (tmp / "chatdata" / CHAT_OTHER / "meta.json").is_file(), \
        "another owner's chat was deleted"
    assert local_store.get_chat_meta_owner(CHAT_OTHER) == OTHER
    store = local_store.AuthStore()
    assert not store.user_exists(OWNER)
    # The recipient keeps their own account.
    assert store.user_exists(RCPT)


def test_remove_is_audited_with_its_counts(world, estate):
    assert _remove(_admin(), OWNER).status_code == 200
    rows = [r for r in _audit("user.removed") if r.get("target") == OWNER]
    assert rows, db_sources.read_audit_tail(20)
    assert rows[0].get("actor") == ADMIN
    assert (rows[0].get("detail") or {}).get("chats_deleted") == 2, rows[0]


def test_a_recipient_of_the_removed_owners_dashboard_gets_not_found(world, estate):
    rcpt = _signed_in(RCPT, RCPT_PW)
    dash_id = estate["dash_id"]
    assert rcpt.get(f"/api/dashboards/{dash_id}").status_code == 200
    listed = [d["dash_id"] for d in rcpt.get("/api/dashboards").json()["dashboards"]]
    assert dash_id in listed
    assert rcpt.get(f"/api/chat/{CHAT_ACTIVE}/schema").status_code == 200

    assert _remove(_admin(), OWNER).status_code == 200
    r = rcpt.get(f"/api/dashboards/{dash_id}")
    assert r.status_code == 404, (r.status_code, r.text[:300])
    listed = [d["dash_id"] for d in rcpt.get("/api/dashboards").json()["dashboards"]]
    assert dash_id not in listed, "the recipient's list kept the dead pointer"
    assert rcpt.get(f"/api/chat/{CHAT_ACTIVE}/schema").status_code == 404
    assert _alive(rcpt), "the recipient's own session must survive"


# ===========================================================================
# remove — the removed account's sessions end
# ===========================================================================
def test_a_removed_password_account_is_signed_out_on_its_next_request(world, estate):
    owner = _signed_in(OWNER, OWNER_PW)
    assert _alive(owner)
    assert _remove(_admin(), OWNER).status_code == 200
    assert not _alive(owner), "the removed account's session survived"


def test_a_removed_account_stays_signed_out_after_a_restart(world, estate):
    owner = _signed_in(OWNER, OWNER_PW)
    sso = _cookie_session(SSO)
    assert _alive(owner) and _alive(sso)
    admin = _admin()
    assert _remove(admin, OWNER).status_code == 200
    assert _remove(admin, SSO).status_code == 200
    local_store._SESSION_GEN_CACHE.clear()                  # a restart
    assert not _alive(owner)
    assert not _alive(sso)


def test_a_removed_sso_only_session_ends(world):
    """An SSO-only account's session carries `gen: ""` — the same value a
    record-less address would answer without the sentinel."""
    tc = _cookie_session(SSO)
    assert _alive(tc)
    r = _remove(_admin(), SSO)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert not _alive(tc), "the removed SSO-only session survived"
    assert not (world["tmp"] / "users" / SSO / "profile.json").exists(), \
        "the removed session re-created the account"


def test_an_sso_account_that_signs_back_in_can_be_removed_again(world):
    """SSO legitimately re-creates the account on its next Microsoft sign-in;
    the second removal must sign that new session out too (the sentinel is
    never cached)."""
    admin = _admin()
    first = _cookie_session(SSO)
    assert _remove(admin, SSO).status_code == 200
    assert not _alive(first)
    store = local_store.AuthStore()
    store.ensure_user(SSO)                   # what the SSO callback does
    store.mark_sso_login(SSO, "microsoft")
    second = _cookie_session(SSO)
    assert _alive(second), "a fresh SSO sign-in after removal did not hold"
    assert _remove(admin, SSO).status_code == 200
    assert not _alive(second), "the second removal left the new session alive"


# ===========================================================================
# the generation sentinel
# ===========================================================================
def test_a_never_seen_address_answers_the_non_hex_sentinel(world):
    sentinel = _sentinel()
    assert isinstance(sentinel, str) and sentinel, repr(sentinel)
    assert not _HEX16.fullmatch(sentinel), "the sentinel must never equal a real generation"
    assert local_store.AuthStore().session_generation(GHOST) == sentinel


def test_a_profile_only_account_still_answers_empty(world):
    """Upgrade back-compat: an account created before generations existed
    (profile.json, no auth.json) keeps answering "", so a pre-release cookie
    without `gen` keeps working."""
    local_store.AuthStore().ensure_user("profile.only@corp.example")
    assert local_store.AuthStore().session_generation("profile.only@corp.example") == ""


def test_the_sentinel_is_never_cached(world):
    """A never-seen address answers the sentinel, then — once an account is
    created for it — the real value at once (no cached sentinel)."""
    sentinel = _sentinel()
    store = local_store.AuthStore()
    assert store.session_generation(GHOST) == sentinel
    store.ensure_user(GHOST)
    assert store.session_generation(GHOST) == ""


def test_a_removed_address_answers_the_sentinel_and_a_re_invite_reads_empty(world, estate):
    sentinel = _sentinel()
    store = local_store.AuthStore()
    assert _HEX16.fullmatch(store.session_generation(OWNER))   # cached before removal
    assert _remove(_admin(), OWNER).status_code == 200
    assert store.session_generation(OWNER) == sentinel
    assert store.ensure_invited_user(OWNER, ADMIN) is True
    assert store.session_generation(OWNER) == "", "the sentinel leaked into the re-invited account"


# ===========================================================================
# the admin page
# ===========================================================================
ADMIN_JS = ROOT / "static" / "admin_data_sources.js"
ADMIN_HTML = ROOT / "templates" / "admin_data_sources.html"


def _confirm_before(src: str, path: str) -> bool:
    """A confirmation step (window.confirm or a confirm modal/button) appears
    between the previous /api/admin/users call and the call to `path`."""
    idx = src.find(path)
    prev = max(src.rfind("/api/admin/users", 0, idx - 1), idx - 2500, 0)
    return bool(re.search(r"confirm", src[prev:idx], re.I))


@pytest.mark.parametrize("path", ["/api/admin/users/end_sessions",
                                  "/api/admin/users/remove"])
def test_the_admin_page_calls_the_endpoint_after_a_confirmation(path):
    src = ADMIN_JS.read_text(encoding="utf-8")
    assert path in src, f"{path} is not called from admin_data_sources.js"
    assert _confirm_before(src, path), f"no confirmation step before {path}"


def test_the_remove_confirmation_names_what_is_deleted():
    """The Remove confirmation says the account's chats and dashboards go
    with it (task item 4)."""
    text = (ADMIN_JS.read_text(encoding="utf-8") + "\n"
            + ADMIN_HTML.read_text(encoding="utf-8"))
    assert re.search(r"(?is)chats[^\"'`<>]{0,200}dashboards"
                     r"|dashboards[^\"'`<>]{0,200}chats", text), \
        "no confirmation text naming the chats and dashboards that are deleted"


def test_the_admin_template_carries_no_inline_handler():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    assert not re.search(r"\bon(click|change|submit|load|error|input)\s*=", html, re.I)


# ===========================================================================
# remove — a recipient's reads never re-create the removed owner's folder
# ===========================================================================
def test_a_recipients_reads_do_not_recreate_the_removed_owners_folder(world, estate):
    """After OWNER is removed, RCPT lists dashboards (the stale pointer is
    pruned) and GETs the removed owner's dashboard (404). Neither read may
    create `users/<OWNER>/` again — `DashboardStore._dir` is a path builder,
    only writers create folders."""
    rcpt = _signed_in(RCPT, RCPT_PW)
    dash_id = estate["dash_id"]
    assert _remove(_admin(), OWNER).status_code == 200
    owner_dir = estate["tmp"] / "users" / OWNER
    assert not owner_dir.exists()
    listed = rcpt.get("/api/dashboards")
    assert listed.status_code == 200, listed.text[:300]
    assert dash_id not in [d["dash_id"] for d in listed.json()["dashboards"]]
    assert rcpt.get(f"/api/dashboards/{dash_id}").status_code == 404
    assert not owner_dir.exists(), "a recipient's read re-created the removed account's folder"


def test_dashboard_store_reads_create_nothing_and_writes_create_their_folder(world):
    tmp = world["tmp"]
    ds = local_store.DashboardStore()
    ghost_dir = tmp / "users" / GHOST
    assert ds.list_dashboards(GHOST) == []
    assert ds.resolve_dashboard(GHOST, "0123456789abcdef") == (None, False)
    assert ds.get_dashboard(GHOST, "0123456789abcdef") is None
    assert not ghost_dir.exists(), "a read created the address's folder"

    fresh = "fresh.owner@corp.example"
    assert not (tmp / "users" / fresh).exists()
    row = ds.create_dashboard(fresh, "First board")
    assert (tmp / "users" / fresh / "dashboards" / f"{row['dash_id']}.json").is_file()
    assert [d["dash_id"] for d in ds.list_dashboards(fresh)] == [row["dash_id"]]

    rcpt = "fresh.rcpt@corp.example"
    assert not (tmp / "users" / rcpt).exists()
    assert ds.add_dashboard_share(fresh, row["dash_id"], [rcpt]) == [rcpt]
    pointers = ds.list_dashboards(rcpt)
    assert [(d["dash_id"], d.get("shared_by")) for d in pointers] == [(row["dash_id"], fresh)]
    doc, is_owner = ds.resolve_dashboard(rcpt, row["dash_id"])
    assert doc is not None and is_owner is False


# ===========================================================================
# the target address is validated before any lookup
# ===========================================================================
FOLDED = "alice_bob@corp.example"
FOLDED_PW = "Alice-bob-passw0rd"


@pytest.fixture
def folded(world):
    """An account whose folder name is what `alice/bob@…` and the backslash form
    fold to under users/."""
    store = local_store.AuthStore()
    store.ensure_user(FOLDED)
    store.set_password(FOLDED, FOLDED_PW)
    return world


@pytest.mark.parametrize("route", ["end_sessions", "remove"])
@pytest.mark.parametrize("address", ["alice/bob@corp.example", "alice\\bob@corp.example"],
                         ids=["slash", "backslash"])
def test_an_address_with_a_path_separator_is_refused_and_reaches_nobody(
        folded, route, address):
    victim = _signed_in(FOLDED, FOLDED_PW)
    assert _alive(victim)
    admin = _admin()
    r = (_end_sessions(admin, address) if route == "end_sessions"
         else _remove(admin, address))
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert "error" in r.json()
    assert local_store.AuthStore().user_exists(FOLDED)
    assert (folded["tmp"] / "users" / FOLDED / "profile.json").is_file()
    assert _alive(victim), "the folded account's session ended"
    assert not [row for row in _audit("user.sessions_ended") + _audit("user.removed")]


@pytest.mark.parametrize("route", ["end_sessions", "remove"])
def test_a_non_email_target_is_refused(world, route):
    admin = _admin()
    r = (_end_sessions(admin, "not-an-address") if route == "end_sessions"
         else _remove(admin, "not-an-address"))
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert not (world["tmp"] / "users" / "not-an-address").exists()
