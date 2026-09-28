"""Task 14c — no AuthStore writer re-creates a removed account (D14c-1).

Every AuthStore writer that can create `users/<email>/` — except the creator,
`AuthStore.create_account` — refuses an address that has neither
profile.json nor auth.json: it writes NOTHING (no folder, no file, no cache
entry), logs `ACCOUNT_WRITE_REFUSED writer=<name>` (sid escaped, no path), and

* `set_password` / `bump_session_generation` RAISE `local_store.AccountMissing`
  (their callers stamp the returned generation into a session; a silent None
  would be stamped),
* `mark_sso_login`, `set_role`, `set_data_roles`, `set_data_role`,
  `touch_last_login`, `update_profile`, `record_active_chat`,
  `record_conversation`, `record_shared_chat` return False and never raise.

The guard is `AuthStore._account_present(email, writer)` (= `user_exists`),
run inside each writer's existing `_LOCK` section. `set_role` checks it BEFORE
its unchanged-role early return. On an existing account every writer still
writes as before (keep-green twins). `create_account` still creates. The
writers that were already safe stay no-ops on a missing account
(`create_reset_token` → None; `clear_reset_token` / `clear_temp_password`
write nothing).

Two missing shapes are pinned: a NEVER-SEEN address and a REMOVED one
(`remove_user`) — the latter is the race the task closes.

Offline: DATA_ROOT is tmp_path; nothing reaches the brain.
"""
import re

import pytest

import local_store
from settings import settings

GHOST = "guard.ghost@corp.example"
LIVE = "guard.live@corp.example"
SHARER = "guard.sharer@corp.example"
CHAT = "c_guardchat01"
CONV = "cv_0123456789abcdef"

_HEX16 = re.compile(r"[0-9a-f]{16}")


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    local_store._SESSION_GEN_CACHE.clear()
    local_store._RESET_TOKEN_INDEX.clear()
    yield tmp_path
    local_store._SESSION_GEN_CACHE.clear()
    local_store._RESET_TOKEN_INDEX.clear()


@pytest.fixture
def logs(monkeypatch):
    """Every `log_with_sid` line local_store writes, flattened to one string
    (message plus any `key=value` context), so the assertion does not depend
    on whether the writer name is in the message or a context field."""
    lines = []

    def _rec(sid, level, message, **context):
        extra = " ".join(f"{k}={v}" for k, v in context.items())
        lines.append(f"[sid={sid}] {level} {message} {extra}".rstrip())
    monkeypatch.setattr(local_store, "log_with_sid", _rec)
    return lines


def _account_missing():
    exc = getattr(local_store, "AccountMissing", None)
    if exc is None:
        pytest.fail("local_store.AccountMissing is missing (Task 14c, D14c-1)")
    assert isinstance(exc, type) and issubclass(exc, Exception), exc
    return exc


def _missing_address(root, shape) -> str:
    """A never-seen address, or an account that existed and was removed."""
    store = local_store.AuthStore()
    if shape == "never_seen":
        return GHOST
    store.ensure_user(GHOST)
    store.set_password(GHOST, "Guard-passw0rd-1")
    store.record_active_chat(GHOST, CHAT, "t", [])
    assert store.user_exists(GHOST)
    assert store.remove_user(GHOST) is True
    assert not (root / "users" / GHOST).exists()
    return GHOST


def _refusals(lines, writer):
    return [ln for ln in lines
            if "ACCOUNT_WRITE_REFUSED" in ln and f"writer={writer}" in ln]


def _assert_nothing_written(root, email):
    store = local_store.AuthStore()
    assert not (root / "users" / email).exists(), \
        "a refused writer created the account's folder"
    assert not store.user_exists(email), "a refused writer re-created the account"
    assert local_store._SESSION_GEN_CACHE.get(local_store._session_gen_key(email)) is None, \
        "a refused writer cached a generation for the missing account"
    assert store.session_generation(email) == local_store.SESSION_GEN_REMOVED


def _no_raise(fn, *args, **kw):
    """Call a writer that must never raise; a raise is a test FAILURE (the
    current behaviour of `update_profile` on a missing folder), not an
    error."""
    try:
        return fn(*args, **kw)
    except Exception as e:                      # noqa: BLE001 — reported
        pytest.fail(f"{getattr(fn, '__name__', fn)} raised {type(e).__name__}: {e}")


# The writers that answer False on a missing account: (writer name used in
# the log line, call).
_FALSE_WRITERS = {
    "mark_sso_login": lambda s, e: s.mark_sso_login(e, "microsoft"),
    "set_role": lambda s, e: s.set_role(e, "power"),
    "set_data_roles": lambda s, e: s.set_data_roles(e, ["aa11bb22cc33dd44"]),
    "set_data_role": lambda s, e: s.set_data_role(e, "aa11bb22cc33dd44"),
    "touch_last_login": lambda s, e: s.touch_last_login(e),
    "update_profile": lambda s, e: s.update_profile(e),
    "record_active_chat": lambda s, e: s.record_active_chat(e, CHAT, "Title", ["d.csv"]),
    "record_conversation": lambda s, e: s.record_conversation(e, CHAT, CONV, "Title"),
    "record_shared_chat": lambda s, e: s.record_shared_chat(e, CHAT, "Title", ["d.csv"],
                                                            SHARER),
}


# ===========================================================================
# the guard helper itself
# ===========================================================================
def test_account_present_helper_answers_and_logs(root, logs):
    store = local_store.AuthStore()
    helper = getattr(store, "_account_present", None)
    if helper is None:
        pytest.fail("AuthStore._account_present is missing (Task 14c, D14c-1)")
    store.ensure_user(LIVE)
    assert helper(LIVE, "probe_writer") is True
    assert not _refusals(logs, "probe_writer"), "a present account was logged as refused"
    assert helper(GHOST, "probe_writer") is False
    assert _refusals(logs, "probe_writer"), logs
    assert not (root / "users" / GHOST).exists()


# ===========================================================================
# the two raising writers
# ===========================================================================
@pytest.mark.parametrize("shape", ["never_seen", "removed"])
def test_set_password_refuses_a_missing_account(root, logs, shape):
    exc = _account_missing()
    email = _missing_address(root, shape)
    logs.clear()
    with pytest.raises(exc):
        local_store.AuthStore().set_password(email, "Hijack-passw0rd-1")
    _assert_nothing_written(root, email)
    assert _refusals(logs, "set_password"), logs
    assert not [ln for ln in logs if "USER_PASSWORD_SET" in ln], \
        "a refused set_password logged a password set"


@pytest.mark.parametrize("shape", ["never_seen", "removed"])
def test_bump_session_generation_refuses_a_missing_account(root, logs, shape):
    exc = _account_missing()
    email = _missing_address(root, shape)
    logs.clear()
    with pytest.raises(exc):
        local_store.AuthStore().bump_session_generation(email)
    _assert_nothing_written(root, email)
    assert _refusals(logs, "bump_session_generation"), logs
    assert not [ln for ln in logs if "USER_SESSIONS_ENDED" in ln], \
        "a refused bump logged ended sessions"


# ===========================================================================
# the writers that answer False
# ===========================================================================
@pytest.mark.parametrize("shape", ["never_seen", "removed"])
@pytest.mark.parametrize("writer", sorted(_FALSE_WRITERS))
def test_writer_refuses_a_missing_account_and_answers_false(root, logs, writer, shape):
    email = _missing_address(root, shape)
    logs.clear()
    out = _no_raise(_FALSE_WRITERS[writer], local_store.AuthStore(), email)
    _assert_nothing_written(root, email)
    assert out is False, f"{writer} answered {out!r} on a missing account (expected False)"
    # set_data_role delegates to set_data_roles: either name satisfies the
    # prefix match `writer=set_data_role`.
    assert _refusals(logs, writer), f"no ACCOUNT_WRITE_REFUSED writer={writer} line: {logs}"


@pytest.mark.parametrize("role", ["user", "power", "admin"])
def test_set_role_refuses_before_its_unchanged_role_early_return(root, logs, role):
    """The guard comes BEFORE the unchanged-role shortcut: whatever the role —
    the default "user" included — a missing account is refused and logged,
    never silently skipped or written."""
    logs.clear()
    out = _no_raise(local_store.AuthStore().set_role, GHOST, role)
    assert out is False, out
    _assert_nothing_written(root, GHOST)
    assert _refusals(logs, "set_role"), logs


def test_refusal_log_line_carries_no_path(root, logs):
    local_store.AuthStore().touch_last_login(GHOST)
    lines = _refusals(logs, "touch_last_login")
    assert lines, logs
    for ln in lines:
        assert str(root) not in ln and "profile.json" not in ln and "auth.json" not in ln, ln
        assert "\n" not in ln


# ===========================================================================
# keep-green: every writer still writes on an existing account
# ===========================================================================
@pytest.fixture
def live(root):
    local_store.AuthStore().ensure_user(LIVE)
    return root


def _no_refusal(logs):
    assert not [ln for ln in logs if "ACCOUNT_WRITE_REFUSED" in ln], logs


def test_set_password_still_writes_on_an_existing_account(live, logs):
    store = local_store.AuthStore()
    gen = store.set_password(LIVE, "Live-passw0rd-1")
    assert isinstance(gen, str) and _HEX16.fullmatch(gen), repr(gen)
    assert store.has_password(LIVE)
    assert store.verify_password(LIVE, "Live-passw0rd-1") == "ok"
    assert store.session_generation(LIVE) == gen
    _no_refusal(logs)


def test_bump_session_generation_still_writes_on_an_existing_account(live, logs):
    store = local_store.AuthStore()
    before = store.session_generation(LIVE)
    gen = store.bump_session_generation(LIVE)
    assert isinstance(gen, str) and _HEX16.fullmatch(gen) and gen != before, repr(gen)
    assert store.get_auth(LIVE).get("session_generation") == gen
    _no_refusal(logs)


def test_mark_sso_login_still_writes_on_an_existing_account(live, logs):
    store = local_store.AuthStore()
    out = store.mark_sso_login(LIVE, "microsoft")
    assert out is not False, out
    auth = store.get_auth(LIVE)
    assert auth.get("sso_provider") == "microsoft" and auth.get("sso_last_login"), auth
    _no_refusal(logs)


def test_set_role_still_writes_on_an_existing_account(live, logs):
    store = local_store.AuthStore()
    assert store.set_role(LIVE, "power") is not False
    assert store.get_role(LIVE) == "power"
    # Unchanged role: still a no-op, never a refusal.
    assert store.set_role(LIVE, "power") is not False
    assert store.get_role(LIVE) == "power"
    _no_refusal(logs)


def test_set_data_roles_and_the_shim_still_write_on_an_existing_account(live, logs):
    store = local_store.AuthStore()
    assert store.set_data_roles(LIVE, ["aa11bb22cc33dd44", "bb22cc33dd44ee55"]) is not False
    assert store.get_data_roles(LIVE) == ["aa11bb22cc33dd44", "bb22cc33dd44ee55"]
    assert store.set_data_role(LIVE, "cc33dd44ee55ff66") is not False
    assert store.get_data_role(LIVE) == "cc33dd44ee55ff66"
    _no_refusal(logs)


def test_touch_last_login_still_writes_on_an_existing_account(live, logs):
    store = local_store.AuthStore()
    assert store.get_profile(LIVE).get("last_login_at") is None
    assert store.touch_last_login(LIVE) is not False
    assert store.get_profile(LIVE).get("last_login_at")
    _no_refusal(logs)


def test_update_profile_still_writes_on_an_existing_account(live, logs):
    store = local_store.AuthStore()
    out = store.update_profile(LIVE, new_email="someone.else@corp.example")
    assert out is not False, out
    assert store.get_profile(LIVE).get("email") == LIVE
    _no_refusal(logs)


def test_the_sidebar_writers_still_write_on_an_existing_account(live, logs):
    store = local_store.AuthStore()
    assert store.record_active_chat(LIVE, CHAT, "Mine", ["d.csv"]) is not False
    assert [r["chat_id"] for r in store.list_active_chats(LIVE)] == [CHAT]
    assert store.record_conversation(LIVE, CHAT, CONV, "Conv") is not False
    assert [r["conv_id"] for r in store.list_conversations(LIVE)] == [CONV]
    assert store.record_shared_chat(LIVE, "c_guardchat02", "Theirs", [], SHARER) is not False
    rows = {r["chat_id"]: r for r in store.list_active_chats(LIVE)}
    assert rows["c_guardchat02"].get("shared_by") == SHARER
    _no_refusal(logs)


def test_a_legacy_profile_only_account_is_present(root, logs):
    """An account written before generations existed (profile.json only) is
    an EXISTING account: the writers accept it."""
    import json
    d = root / "users" / LIVE
    d.mkdir(parents=True)
    (d / "profile.json").write_text(json.dumps({"email": LIVE, "created_at": "2026-01-01"}),
                                    encoding="utf-8")
    store = local_store.AuthStore()
    assert store.touch_last_login(LIVE) is not False
    assert store.get_profile(LIVE).get("last_login_at")
    gen = store.set_password(LIVE, "Legacy-passw0rd-1")
    assert _HEX16.fullmatch(gen)
    _no_refusal(logs)


def test_an_auth_only_account_is_present(root, logs):
    """auth.json without profile.json is an existing account too (the
    user_exists rule)."""
    import json
    d = root / "users" / LIVE
    d.mkdir(parents=True)
    (d / "auth.json").write_text(json.dumps({"session_generation": "0011223344556677"}),
                                 encoding="utf-8")
    store = local_store.AuthStore()
    assert store.mark_sso_login(LIVE, "microsoft") is not False
    assert store.get_auth(LIVE).get("sso_provider") == "microsoft"
    _no_refusal(logs)


# ===========================================================================
# the creator and the already-safe writers
# ===========================================================================
def test_create_account_still_creates_a_never_seen_address(root, logs):
    store = local_store.AuthStore()
    assert store.create_account(GHOST) is True
    assert (root / "users" / GHOST / "profile.json").is_file()
    assert (root / "users" / GHOST / "auth.json").is_file()
    assert store.user_exists(GHOST)
    assert not _refusals(logs, "create_account")


def test_create_account_re_creates_a_removed_address(root, logs):
    email = _missing_address(root, "removed")
    store = local_store.AuthStore()
    assert store.create_account(email) is True
    assert store.user_exists(email)
    assert _HEX16.fullmatch(store.session_generation(email))


@pytest.mark.parametrize("shape", ["never_seen", "removed"])
def test_the_already_safe_writers_stay_no_ops_on_a_missing_account(root, shape):
    email = _missing_address(root, shape)
    store = local_store.AuthStore()
    assert store.create_reset_token(email) is None
    _no_raise(store.clear_reset_token, email)
    _no_raise(store.clear_temp_password, email)
    assert not (root / "users" / email).exists()
    assert not store.user_exists(email)
