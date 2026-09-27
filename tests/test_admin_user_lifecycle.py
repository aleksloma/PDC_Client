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
* Sessions of a removed account end on the next request, including a
  legacy session stamped `gen: ""` and after a process restart (the
  generation cache emptied): `AuthStore.session_generation` answers the
  fixed non-hex sentinel `local_store.SESSION_GEN_REMOVED` — UNCACHED — for
  an address with neither profile.json nor auth.json, while an EXISTING
  profile-only account (written before generations existed) still answers
  "" (upgrade back-compat). An SSO account that signs back in after a
  removal can be removed (and signed out) a second time.
* Task 14b item 1 (D14b-1, D14b-3b): EVERY new account is created through
  `AuthStore.create_account(email, *, invited_by=None) -> bool` — profile.json
  first, then auth.json holding a fresh 16-hex `session_generation`, cached.
  `ensure_user` / `ensure_invited_user` delegate to it. An existing account
  is left byte-identical (an auth-only folder gets a minimal profile.json,
  its generation untouched). So a re-created address (share placeholder,
  admin invite, Microsoft sign-in) reads a fresh generation — a `gen: ""`
  session of the removed account never revives, and `/auth/password` can
  never set a first password through it.
* Task 14b item 2 (D14b-3, D14-4 reversed): removal also strips the address
  from every OTHER owner's `shared_with` (chats and dashboards —
  `local_store.purge_address_grants`) and POPS `registered_by` from the
  tables it registered (`DataSourceStore.release_registrations`, connectors
  included; `descriptions_confirmed_by` untouched). The response and the
  `user.removed` audit row carry `chats_unshared`, `dashboards_unshared`,
  `registrations_released`; a `table.registered_by_released` row holds the
  count. A re-created address inherits nothing and can be shared to again.
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
CHAT_USER = "c_lifeuser001"

# OWNER as another registrant might have typed it: `registered_by` is
# stored verbatim, the release compares lower-cased (Task 14b item 2).
OWNER_MIXED = "Life.OWNER@Corp.Example"
FAKE_CONN = "0123456789abcdef"      # upsert_table validates no connection

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


def _register(display: str, *, registered_by: str, connector: bool = False) -> str:
    """A registry table doc the way the wizard leaves one: `registered_by`
    stamped from the registrant's session, descriptions confirmed by the
    admin. `upsert_table` validates no connection and needs no encryption
    key, so a fake 16-hex connection id is enough."""
    row = db_sources.DataSourceStore().upsert_table({
        "connection_id": FAKE_CONN, "schema": "s",
        "table_name": display.replace(" ", "_"), "display_name": display,
        "description": "", "is_connector": connector, "relations": [],
        "columns": [], "registered_by": registered_by,
        "descriptions_confirmed_by": ADMIN}, actor=ADMIN)
    return row["id"]


@pytest.fixture
def estate(world):
    """OWNER's estate: an active chat shared with RCPT, a DEACTIVATED chat
    (meta owner, no active_chats row) and a dashboard shared with RCPT.
    Plus the REVERSE grants OWNER holds elsewhere (Task 14b item 2): OTHER's
    chat and OTHER's dashboard are shared WITH OWNER, and OWNER (in mixed
    case) is the registrant of one normal table and one connector; RCPT
    registered a third table."""
    _chat(CHAT_ACTIVE, OWNER, shared_with=[RCPT])
    _chat(CHAT_DEACT, OWNER, active=False)
    _chat(CHAT_OTHER, OTHER, shared_with=[OWNER])
    ds = local_store.DashboardStore()
    dash = ds.create_dashboard(OWNER, "Owned board")
    assert ds.add_dashboard_share(OWNER, dash["dash_id"], [RCPT]) == [RCPT]
    other = ds.create_dashboard(OTHER, "Other's board")
    assert ds.add_dashboard_share(OTHER, other["dash_id"], [OWNER]) == [OWNER]
    tids = {
        "owner_plain": _register("owner plain", registered_by=OWNER_MIXED),
        "owner_connector": _register("owner connector", registered_by=OWNER_MIXED,
                                     connector=True),
        "rcpt_plain": _register("rcpt plain", registered_by=RCPT),
    }
    return {"dash_id": dash["dash_id"], "other_dash_id": other["dash_id"],
            "tids": tids, "tmp": world["tmp"]}


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
    """An SSO-only account's session carries the account's current
    generation (Task 14b: a hex value from creation; before it, `gen: ""` —
    the same value a record-less address would answer without the
    sentinel). Either way the removed session must end. The legacy `""`
    shape itself is covered by the Task 14b re-creation tests below."""
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


def _profile_only_account(tmp, email):
    """An account written by a release BEFORE generations existed:
    profile.json and nothing else. Built by hand on purpose — since Task
    14b `ensure_user` creates an auth.json with a generation, so the store
    can no longer produce this (upgrade) shape."""
    d = tmp / "users" / email
    d.mkdir(parents=True, exist_ok=True)
    (d / "profile.json").write_text(
        json.dumps({"email": email, "created_at": "2026-01-01T00:00:00"}),
        encoding="utf-8")


def test_a_profile_only_account_still_answers_empty(world):
    """Upgrade back-compat: an account created before generations existed
    (profile.json, no auth.json) keeps answering "", so a pre-release cookie
    without `gen` keeps working. Task 14b: the account is written by hand
    (that IS the upgrade case) instead of through `ensure_user`, which now
    creates a generation — the intent of the pin is unchanged."""
    _profile_only_account(world["tmp"], "profile.only@corp.example")
    assert local_store.AuthStore().session_generation("profile.only@corp.example") == ""


def test_the_sentinel_is_never_cached(world):
    """A never-seen address answers the sentinel, then — once an account
    exists for it — the real value at once (no cached sentinel). Task 14b:
    the account is a hand-written profile-only one so the real value is
    still "", which is what tells a cached sentinel from a read."""
    sentinel = _sentinel()
    store = local_store.AuthStore()
    assert store.session_generation(GHOST) == sentinel
    _profile_only_account(world["tmp"], GHOST)
    assert store.session_generation(GHOST) == ""


def test_a_removed_address_answers_the_sentinel_and_a_re_invite_reads_a_fresh_generation(
        world, estate):
    """Renamed from `..._and_a_re_invite_reads_empty` (Task 14b item 1): the
    re-invited account is CREATED, so it carries a fresh 16-hex generation —
    not "" (which a `gen: ""` cookie of the removed account would match) and
    not the sentinel (which must never be written)."""
    sentinel = _sentinel()
    store = local_store.AuthStore()
    assert _HEX16.fullmatch(store.session_generation(OWNER))   # cached before removal
    assert _remove(_admin(), OWNER).status_code == 200
    assert store.session_generation(OWNER) == sentinel
    assert store.ensure_invited_user(OWNER, ADMIN) is True
    gen = store.session_generation(OWNER)
    assert gen != sentinel, "the sentinel leaked into the re-invited account"
    assert gen != "", "the re-invited account has no generation — a gen \"\" cookie would match"
    assert _HEX16.fullmatch(gen), repr(gen)


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


# ===========================================================================
# Task 14b item 1 — the ONE creation function writes a session generation
# ===========================================================================
def _create_account(store, email, **kw) -> bool:
    fn = getattr(store, "create_account", None)
    if fn is None:
        pytest.fail("AuthStore.create_account is missing (Task 14b item 1)")
    return fn(email, **kw)


def _user_dir(tmp, email):
    return tmp / "users" / email


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_create_account_writes_profile_and_a_fresh_generation_for_a_new_address(world):
    tmp = world["tmp"]
    store = local_store.AuthStore()
    assert _create_account(store, GHOST) is True
    d = _user_dir(tmp, GHOST)
    assert (d / "profile.json").is_file(), "no profile.json written"
    assert (d / "auth.json").is_file(), "no auth.json written — the account has no generation"
    profile = _read_json(d / "profile.json")
    assert profile.get("email") == GHOST and profile.get("created_at")
    assert "invited_by" not in profile, profile
    gen = _read_json(d / "auth.json").get("session_generation")
    assert isinstance(gen, str) and _HEX16.fullmatch(gen), repr(gen)
    assert not (_read_json(d / "auth.json").get("password_hash")), "a credential was invented"
    assert store.session_generation(GHOST) == gen
    # Cached by the writer, like set_password's.
    assert local_store._SESSION_GEN_CACHE.get(local_store._session_gen_key(GHOST)) == gen


def test_create_account_leaves_an_existing_account_byte_identical(world):
    tmp = world["tmp"]
    d = _user_dir(tmp, USER)
    before = ((d / "profile.json").read_bytes(), (d / "auth.json").read_bytes())
    gen = local_store.AuthStore().session_generation(USER)
    assert _create_account(local_store.AuthStore(), USER) is False
    assert ((d / "profile.json").read_bytes(), (d / "auth.json").read_bytes()) == before, \
        "an existing account's files were rewritten"
    assert local_store.AuthStore().session_generation(USER) == gen


def test_create_account_repairs_a_missing_profile_on_an_auth_only_folder(world):
    """D14b-3b: an auth-only folder (no profile.json) is an EXISTING account
    — False — but the missing profile is written (minimal: email +
    created_at, no invitation stamp) so the address stays listed in User
    management; auth.json and its generation are untouched."""
    tmp = world["tmp"]
    addr = "auth.only@corp.example"
    d = _user_dir(tmp, addr)
    d.mkdir(parents=True)
    raw = json.dumps({"session_generation": "0011223344556677",
                      "sso_provider": "microsoft"}).encode("utf-8")
    (d / "auth.json").write_bytes(raw)
    store = local_store.AuthStore()
    assert _create_account(store, addr, invited_by=ADMIN) is False
    assert (d / "profile.json").is_file(), "the missing profile.json was not repaired"
    profile = _read_json(d / "profile.json")
    assert profile.get("email") == addr and profile.get("created_at"), profile
    assert "invited_by" not in profile and "invited_at" not in profile, profile
    assert (d / "auth.json").read_bytes() == raw, "auth.json was rewritten"
    assert store.session_generation(addr) == "0011223344556677"


def test_ensure_invited_user_stamps_the_invitation_and_a_generation(world):
    tmp = world["tmp"]
    store = local_store.AuthStore()
    assert store.ensure_invited_user(GHOST, ADMIN) is True
    d = _user_dir(tmp, GHOST)
    profile = _read_json(d / "profile.json")
    assert profile.get("invited_by") == ADMIN and profile.get("invited_at"), profile
    assert (d / "auth.json").is_file(), "the placeholder has no auth.json — no generation"
    gen = _read_json(d / "auth.json").get("session_generation")
    assert isinstance(gen, str) and _HEX16.fullmatch(gen), repr(gen)
    assert store.session_generation(GHOST) == gen
    assert store.has_password(GHOST) is False
    # Second call: an existing account, nothing created.
    assert store.ensure_invited_user(GHOST, BOSS) is False
    assert _read_json(d / "profile.json").get("invited_by") == ADMIN


def test_ensure_user_creates_a_generation_for_a_new_address(world):
    """The SSO callback's store call (`ensure_user`) is a creation too."""
    tmp = world["tmp"]
    store = local_store.AuthStore()
    profile = store.ensure_user(GHOST)
    assert isinstance(profile, dict) and profile.get("email") == GHOST
    d = _user_dir(tmp, GHOST)
    assert (d / "auth.json").is_file(), "ensure_user created no auth.json — no generation"
    gen = _read_json(d / "auth.json").get("session_generation")
    assert isinstance(gen, str) and _HEX16.fullmatch(gen), repr(gen)
    assert store.session_generation(GHOST) == gen


def test_ensure_user_never_rewrites_an_unreadable_profile(world):
    """An unreadable profile.json is an EXISTING account with a damaged
    record: `ensure_user` answers a minimal dict and logs, it does not
    replace the customer's file (Article IV — never overwrite state)."""
    tmp = world["tmp"]
    addr = "broken.profile@corp.example"
    d = _user_dir(tmp, addr)
    d.mkdir(parents=True)
    garbage = b'{"email": "broken.profile@corp.example", "created_at": '   # truncated
    (d / "profile.json").write_bytes(garbage)
    out = local_store.AuthStore().ensure_user(addr)
    assert isinstance(out, dict), out
    assert (d / "profile.json").read_bytes() == garbage, \
        "ensure_user replaced an unreadable profile.json"


def test_a_hand_written_profile_only_account_still_answers_empty(world):
    """The upgrade rule is unchanged: the generation is written at CREATION
    only, so an account that already exists without one keeps reading ""."""
    _profile_only_account(world["tmp"], "legacy.profile@corp.example")
    assert local_store.AuthStore().session_generation("legacy.profile@corp.example") == ""


# ===========================================================================
# Task 14b item 1 — a `gen ""` session of a removed legacy account
# must not revive when the address is created again
# ===========================================================================
LEGACY_SSO = "life.legacy.sso@corp.example"
LEGACY_PW_ACCT = "life.legacy.pw@corp.example"
LEGACY_PW = "Legacy-passw0rd"
HIJACK_PW = "Hijacked-passw0rd"


def _legacy_account(tmp, email, auth: dict):
    """An account written by a release BEFORE generations existed:
    profile.json plus an auth.json WITHOUT `session_generation`."""
    d = tmp / "users" / email
    d.mkdir(parents=True)
    (d / "profile.json").write_text(
        json.dumps({"email": email, "created_at": "2026-01-01T00:00:00"}),
        encoding="utf-8")
    (d / "auth.json").write_text(json.dumps(auth), encoding="utf-8")


def _build_legacy(tmp, shape) -> str:
    if shape == "sso_only":
        _legacy_account(tmp, LEGACY_SSO, {"sso_provider": "microsoft",
                                          "sso_last_login": "2026-01-02T00:00:00"})
        return LEGACY_SSO
    from password_utils import generate_password_hash
    _legacy_account(tmp, LEGACY_PW_ACCT, {"password_hash": generate_password_hash(LEGACY_PW),
                                          "must_change_password": False})
    return LEGACY_PW_ACCT


def _recreate_by_share(addr, monkeypatch):
    """(a) another owner shares a chat with the address — the route's
    `ensure_invited_user` creates the placeholder. The brain relay is a stub
    answering "nothing configured", so the route answers 200."""
    monkeypatch.setattr(brain_client, "send_share_email",
                        lambda **kw: {"smtp_configured": False, "sent": [], "failed": []})
    _chat(CHAT_USER, USER)
    user = _signed_in(USER, USER_PW)
    r = user.post(f"/api/chat/{CHAT_USER}/share", json={"emails": [addr]})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert addr in (r.json().get("added") or []), r.json()


def _recreate_by_invite(addr):
    """(b) the admin invites the address (mail may be unsent — no
    PUBLIC_BASE_URL in the test env — the account is created all the same)."""
    r = _admin().post("/api/admin/users/invite", json={"email": addr})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert r.json().get("created") is True, r.json()


def _recreate_by_sso(addr):
    """(c) the Microsoft callback's two store calls."""
    store = local_store.AuthStore()
    store.ensure_user(addr)
    store.mark_sso_login(addr, "microsoft")


@pytest.mark.parametrize("path", ["share", "invite", "sso"])
@pytest.mark.parametrize("shape", ["sso_only", "pre_9b_password"])
def test_a_gen_empty_session_of_a_removed_legacy_account_never_revives(
        world, monkeypatch, shape, path):
    """A legacy account (auth.json without the key)
    reads generation "", so its cookie carries `gen: ""`. It is removed and
    makes NO request until the address is created again through one of the
    three creation paths. Before Task 14b the new account was profile-only
    and read "" too, so the old cookie matched again and `/auth/password`
    set a first password with no current-password check. Now every creation
    writes a fresh generation: the old cookie is signed out (401), the
    password change is refused and writes no hash, and — the positive
    control — a cookie built from the NEW generation is alive, proving the
    refusal is the generation gate and not a broken account."""
    tmp = world["tmp"]
    addr = _build_legacy(tmp, shape)
    store = local_store.AuthStore()
    assert store.session_generation(addr) == "", "the legacy shape must read the empty generation"
    old = _cookie_session(addr)
    assert _alive(old), "the legacy session did not hold before the removal"

    assert _remove(_admin(), addr).status_code == 200
    assert not (tmp / "users" / addr).exists()
    # No request from `old` here — that is the whole point.

    if path == "share":
        _recreate_by_share(addr, monkeypatch)
    elif path == "invite":
        _recreate_by_invite(addr)
    else:
        _recreate_by_sso(addr)
    assert store.user_exists(addr), "the re-creation path did not create the account"

    assert not _alive(old), \
        "the removed account's gen-\"\" session revived on the re-created address"
    r = old.post("/auth/password", json={"current_password": "",
                                        "new_password": HIJACK_PW})
    if path == "sso":
        # SSO-only: refused either way (the SSO refusal or the gate's 401).
        assert r.status_code != 200, (r.status_code, r.text[:300])
    else:
        assert r.status_code == 401, (r.status_code, r.text[:300])
    assert not store.get_auth(addr).get("password_hash"), \
        "the revived session set a first password on the re-created account"

    gen = store.session_generation(addr)
    assert gen != "" and gen != _sentinel(), repr(gen)
    assert _HEX16.fullmatch(gen), repr(gen)
    fresh = _cookie_session(addr)
    assert _alive(fresh), "a session built from the account's NEW generation must be alive"


# ===========================================================================
# Task 14b item 2 — removal strips the address from every grant it holds
# elsewhere (D14b-3, D14-4 reversed)
# ===========================================================================
def _lower(values):
    return [str(v or "").strip().lower() for v in values]


def test_remove_strips_the_address_from_other_owners_share_lists(world, estate):
    ds = local_store.DashboardStore()
    assert OWNER in _lower(local_store.ChatDataStore(CHAT_OTHER).read_meta()
                           ["sharing"]["shared_with"])
    assert OWNER in _lower(ds.get_dashboard(OTHER, estate["other_dash_id"])
                           ["sharing"]["shared_with"])
    assert _remove(_admin(), OWNER).status_code == 200
    shared = local_store.ChatDataStore(CHAT_OTHER).read_meta()["sharing"]["shared_with"]
    assert OWNER not in _lower(shared), \
        f"the removed address stayed in another owner's chat share list: {shared}"
    doc = ds.get_dashboard(OTHER, estate["other_dash_id"])
    assert doc is not None, "OTHER's dashboard vanished"
    dash_shared = (doc.get("sharing") or {}).get("shared_with") or []
    assert OWNER not in _lower(dash_shared), \
        f"the removed address stayed in another owner's dashboard share list: {dash_shared}"
    assert local_store.get_chat_meta_owner(CHAT_OTHER) == OTHER


def test_remove_releases_the_registrations_of_the_address(world, estate):
    tids = estate["tids"]
    store = db_sources.DataSourceStore()
    assert store.get_table(tids["owner_plain"]).get("registered_by") == OWNER_MIXED
    assert _remove(_admin(), OWNER).status_code == 200
    for name in ("owner_plain", "owner_connector"):
        t = store.get_table(tids[name])
        assert t is not None, f"{name} vanished from the registry"
        assert "registered_by" not in t, \
            f"{name} still carries registered_by={t.get('registered_by')!r}"
        assert t.get("descriptions_confirmed_by") == ADMIN, \
            f"{name} lost its confirmation: {t}"
        assert t.get("display_name"), t
    rcpt = store.get_table(tids["rcpt_plain"])
    assert rcpt.get("registered_by") == RCPT, "another user's registration was released"


def test_remove_answers_and_audits_the_strip_counts(world, estate):
    r = _remove(_admin(), OWNER)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    body = r.json()
    assert body.get("ok") is True and body.get("chats_deleted") == 2, body
    assert body.get("chats_unshared") == 1, body
    assert body.get("dashboards_unshared") == 1, body
    assert body.get("registrations_released") == 2, body
    rows = [x for x in _audit("user.removed") if x.get("target") == OWNER]
    assert rows, db_sources.read_audit_tail(20)
    detail = rows[0].get("detail") or {}
    assert detail.get("chats_deleted") == 2, detail
    assert detail.get("chats_unshared") == 1, detail
    assert detail.get("dashboards_unshared") == 1, detail
    assert detail.get("registrations_released") == 2, detail
    released = _audit("table.registered_by_released")
    assert released, "no table.registered_by_released audit row"
    assert (released[0].get("detail") or {}).get("count") == 2, released[0]
    assert released[0].get("actor") == ADMIN, released[0]


def test_a_re_created_address_inherits_nothing_and_can_be_shared_to_again(world, estate):
    """After the strips a fresh placeholder for the same address holds no
    grant: the other owner's chat answers 403, no registry table is readable
    through the ownership read, the other owner's dashboard does not
    resolve — and re-sharing WORKS (the stale entries used to make it a
    silent no-op)."""
    tids = estate["tids"]
    other_id = estate["other_dash_id"]
    assert _remove(_admin(), OWNER).status_code == 200
    store = local_store.AuthStore()
    assert store.ensure_invited_user(OWNER, ADMIN) is True
    r = _cookie_session(OWNER).get(f"/api/chat/{CHAT_OTHER}/schema")
    assert r.status_code == 403, (r.status_code, r.text[:300])
    allowed = roles_store.allowed_table_ids_for(OWNER)
    assert not (allowed & set(tids.values())), \
        f"the re-created address still reads a released registration: {allowed}"
    ds = local_store.DashboardStore()
    assert ds.resolve_dashboard(OWNER, other_id) == (None, False)
    assert ds.add_dashboard_share(OTHER, other_id, [OWNER]) == [OWNER], \
        "re-sharing the dashboard was a silent no-op (stale shared_with entry)"
    assert local_store.ChatDataStore(CHAT_OTHER).add_share_recipients([OWNER]) == [OWNER], \
        "re-sharing the chat was a silent no-op (stale shared_with entry)"
    assert _cookie_session(OWNER).get(f"/api/chat/{CHAT_OTHER}/schema").status_code == 200


# --- purge_address_grants, unit level ---------------------------------------
def _purge(email):
    fn = getattr(local_store, "purge_address_grants", None)
    if fn is None:
        pytest.fail("local_store.purge_address_grants is missing (Task 14b item 2)")
    return fn(email)


def test_purge_leaves_an_unreadable_meta_byte_identical_and_still_counts(world):
    """A corrupt meta.json is skipped as it is — never replaced by an empty
    one (the `read_meta` fallback shape) — and the purge goes on with the
    other chats, answering the counts without raising."""
    tmp = world["tmp"]
    garbage = b"\xff\xfe{ not json at all"
    d = tmp / "chatdata" / "c_garbage0001"
    d.mkdir(parents=True)
    (d / "meta.json").write_bytes(garbage)
    _chat("c_purgeok0001", OTHER, shared_with=[OWNER])
    out = _purge(OWNER)
    assert (d / "meta.json").read_bytes() == garbage, "the unreadable meta.json was rewritten"
    assert isinstance(out, dict), out
    assert out.get("chats_unshared") == 1, out
    assert out.get("dashboards_unshared") == 0, out
    assert OWNER not in _lower(local_store.ChatDataStore("c_purgeok0001").read_meta()
                               ["sharing"]["shared_with"])


def test_purge_keeps_the_other_recipients_with_their_casing_and_order(world):
    """Only the target address goes (matched `strip().lower()`, so a mixed-
    case duplicate goes too); every other entry keeps its casing and its
    position."""
    store = _chat("c_purgemix0001", OTHER)
    meta = store.read_meta()
    meta["sharing"] = {"shared_with": ["Zed.Last@corp.example", OWNER, RCPT,
                                       "Life.OWNER@corp.example"]}
    store.write_meta(meta)
    out = _purge(OWNER)
    assert out.get("chats_unshared") == 1, out
    assert store.read_meta()["sharing"]["shared_with"] == ["Zed.Last@corp.example", RCPT]
    assert local_store.get_chat_meta_owner("c_purgemix0001") == OTHER


def test_purge_of_an_address_that_holds_nothing_changes_nothing(world, estate):
    before = local_store.ChatDataStore(CHAT_OTHER).meta_path.read_bytes()
    out = _purge(GHOST)
    assert isinstance(out, dict), out
    assert out.get("chats_unshared") == 0 and out.get("dashboards_unshared") == 0, out
    assert local_store.ChatDataStore(CHAT_OTHER).meta_path.read_bytes() == before
    assert not (world["tmp"] / "users" / GHOST).exists(), "the purge created the address's folder"


def test_a_failing_strip_step_still_answers_200_and_audits_the_removal(
        world, estate, monkeypatch):
    """Once the account folder is gone the removal has happened: a strip step
    that raises is logged and counts 0, and the route still answers 200 and
    writes the `user.removed` audit row with the counts achieved."""
    import routes.admin_users as admin_users

    def _boom(email):
        raise RuntimeError("purge failed")
    monkeypatch.setattr(admin_users, "purge_address_grants", _boom)
    r = _remove(_admin(), OWNER)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    body = r.json()
    assert body.get("ok") is True, body
    assert body.get("chats_deleted") == 2, body
    assert body.get("chats_unshared") == 0 and body.get("dashboards_unshared") == 0, body
    assert body.get("registrations_released") == 2, body
    assert not local_store.AuthStore().user_exists(OWNER)
    rows = [x for x in _audit("user.removed") if x.get("target") == OWNER]
    assert rows, db_sources.read_audit_tail(20)
    detail = rows[0].get("detail") or {}
    assert detail.get("chats_unshared") == 0 and detail.get("dashboards_unshared") == 0, detail
    assert detail.get("chats_deleted") == 2 and detail.get("registrations_released") == 2, detail
