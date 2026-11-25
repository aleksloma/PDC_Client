"""Who an MCP caller acts as (`mcp_tokens.resolve_caller`).

The contract pinned here (MCP integration, stage 1):

* `resolve_caller(record, header_values, peer=None)` answers a Caller with
  `email` (str|None) and `refusal` (None | "missing_header" |
  "malformed_header" | "not_allowed"); exactly one of the two is set and it
  never raises. `header_values` is the list of X-PDC-User-Email values the
  request carried.
* per_user: exactly one well-formed address (`routes.auth._EMAIL_RE`,
  stripped, lower-cased). An existing account is used as it is — nothing
  about it is written. An unknown address inside an approved sign-in domain
  gets its account through `AuthStore.create_account` (no password, no mail,
  one `mcp.account_created` audit row); any other unknown address and the
  bootstrap admin are "not_allowed", with nothing created.
* shared_account: the header is ignored; the bound account must still exist,
  be the same account (`created_at`) and not be the bootstrap admin.
* No log line carries an unescaped header value.

Offline: DATA_ROOT is tmp_path; the brain mail relays fail the test if called.
"""
import hashlib
import json
import logging
import threading

import pytest

import brain_client
import db_sources
import local_store
import logger_utils
from settings import settings

ADMIN = "ladmin"
APPROVED = "approved.example"
NEWBIE = "new.person@approved.example"
USER = "user@corp.example"
STRANGER = "stranger@other.example"
BOT = "bot@corp.example"
RAW = "pdcmcp_" + "A" * 43
HASH = hashlib.sha256(RAW.encode()).hexdigest()

PER_USER = {"id": "a" * 16, "name": "Helpdesk bot", "mode": "per_user",
            "token_hash": HASH, "created_at": "2026-11-20T10:00:00+00:00",
            "created_by": ADMIN, "last_used_at": None, "revoked_at": None,
            "revoked_by": None}


# ===========================================================================
# helpers
# ===========================================================================
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass


@pytest.fixture
def logs():
    """Every line written through `log_with_sid`, whichever module emits it."""
    logger = logger_utils.get_logger()
    handler = _Capture()
    old_level = logger.level
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _approve(root, *domains):
    (root / "signin_domains.json").write_text(
        json.dumps({"domains": list(domains), "updated_at": "2026-10-15T00:00:00Z",
                    "updated_by": ADMIN}), encoding="utf-8")


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", ADMIN)
    monkeypatch.setattr(settings, "ALLOW_SELF_REGISTRATION", False)
    local_store._SESSION_GEN_CACHE.clear()
    local_store._RESET_TOKEN_INDEX.clear()

    def no_mail(*a, **k):
        pytest.fail("MCP must not send mail")
    monkeypatch.setattr(brain_client, "send_welcome_email", no_mail)
    monkeypatch.setattr(brain_client, "send_password_reset_email", no_mail)
    _approve(tmp_path, APPROVED)
    yield tmp_path
    local_store._SESSION_GEN_CACHE.clear()
    local_store._RESET_TOKEN_INDEX.clear()


@pytest.fixture
def mt(root):
    """The module (a missing module fails the test)."""
    import mcp_tokens
    return mcp_tokens


@pytest.fixture
def store(root):
    return local_store.AuthStore()


@pytest.fixture
def created(root, monkeypatch):
    """Addresses `AuthStore.create_account` was called with (real call kept)."""
    seen = []
    real = local_store.AuthStore.create_account

    def spy(self, email, **kw):
        seen.append(email)
        return real(self, email, **kw)
    monkeypatch.setattr(local_store.AuthStore, "create_account", spy)
    return seen


def _audit_rows():
    return [r for r in db_sources.read_audit_tail(500)
            if r.get("action") == "mcp.account_created"]


def _users(root):
    """Every file under users/ with its content."""
    base = root / "users"
    if not base.exists():
        return {}
    return {str(p.relative_to(base)): p.read_bytes()
            for p in sorted(base.rglob("*")) if p.is_file()}


def _refused(caller, refusal):
    assert caller.email is None
    assert caller.refusal == refusal


def _acts_as(caller, email):
    assert caller.refusal is None
    assert caller.email == email


# ===========================================================================
# per_user: the header
# ===========================================================================
def test_no_header_is_refused(mt, root, created):
    _refused(mt.resolve_caller(PER_USER, []), "missing_header")
    assert created == [] and _users(root) == {}


@pytest.mark.parametrize("value", ["", " ", "\t"], ids=["empty", "space", "tab"])
def test_a_blank_header_is_refused(mt, root, created, value):
    caller = mt.resolve_caller(PER_USER, [value])
    assert caller.email is None
    assert caller.refusal in ("missing_header", "malformed_header")
    assert created == [] and _users(root) == {}


MALFORMED = [
    "not-an-email", "a@b", "@approved.example", "new@", "a b@approved.example",
    "a@approved.example, b@approved.example", "a@approved.example,b@approved.example",
    "a@approved.example;b@approved.example", "Bob <bob@approved.example>",
    "<script>@approved.example", "a/b@approved.example", "../x@approved.example",
    "..\\x@approved.example", "a@b@approved.example",
    "a@approved.example\nFORGED RECORD", "a@approved.example\r\nX-Other: y",
    "a\x00@approved.example", "ü@approved.example",
    "x" * 260 + "@approved.example", "a@approved.example FORGED",
]


@pytest.mark.parametrize("value", MALFORMED, ids=range(len(MALFORMED)))
def test_a_malformed_header_is_refused(mt, root, created, value):
    _refused(mt.resolve_caller(PER_USER, [value]), "malformed_header")
    assert created == [] and _users(root) == {} and _audit_rows() == []


@pytest.mark.parametrize("values", [
    [USER, STRANGER], [USER, USER], [USER, ""], ["", USER], [USER, "junk", USER]],
    ids=["two", "same-twice", "then-blank", "blank-first", "three"])
def test_a_repeated_header_is_refused(mt, store, values):
    """Even when both copies agree: a proxy that appends its own copy must
    not decide who the caller is."""
    store.create_account(USER)
    _refused(mt.resolve_caller(PER_USER, values), "malformed_header")


@pytest.mark.parametrize("values", [None, [None], [5], [["a@approved.example"]],
                                    [b"new.person@approved.example"]],
                         ids=["none", "none-item", "int", "list", "bytes"])
def test_a_junk_header_list_never_raises(mt, root, created, values):
    caller = mt.resolve_caller(PER_USER, values)
    assert caller.email is None and caller.refusal in (
        "missing_header", "malformed_header")
    assert created == []


# ===========================================================================
# per_user: existing accounts
# ===========================================================================
@pytest.mark.parametrize("typed", [USER, "  User@Corp.EXAMPLE  ", "USER@CORP.EXAMPLE"])
def test_an_existing_account_is_used(mt, store, created, typed):
    store.create_account(USER)
    created.clear()
    _acts_as(mt.resolve_caller(PER_USER, [typed]), USER)
    assert created == [], "an existing account is never created again"
    assert _audit_rows() == []


def test_an_existing_account_needs_no_approved_domain(mt, root, store):
    (root / "signin_domains.json").unlink()
    store.create_account(USER)
    _acts_as(mt.resolve_caller(PER_USER, [USER]), USER)


def test_resolving_writes_nothing_to_the_account(mt, root, store):
    store.create_account(USER)
    store.set_password(USER, "User-passw0rd")
    before = _users(root)
    _acts_as(mt.resolve_caller(PER_USER, [USER]), USER)
    _acts_as(mt.resolve_caller(PER_USER, [USER]), USER)
    assert _users(root) == before


def test_the_peer_argument_is_accepted(mt, store):
    store.create_account(USER)
    _acts_as(mt.resolve_caller(PER_USER, [USER], peer="10.0.0.5"), USER)
    _refused(mt.resolve_caller(PER_USER, [], peer=None), "missing_header")


@pytest.mark.parametrize("who", ["placeholder", "sso", "forced", "promoted"])
def test_an_account_without_a_usable_web_password_is_allowed(mt, store, who):
    """A share placeholder, an SSO-only account, a pending forced change and a
    promoted admin: the token vouches for the identity (plan S6)."""
    email = f"{who}@corp.example"
    if who in ("placeholder", "sso"):
        store.ensure_invited_user(email, "boss@corp.example", sso_only=(who == "sso"))
    else:
        store.create_account(email)
    if who == "forced":
        store.set_password(email, "User-passw0rd", force_change=True)
    if who == "promoted":
        store.set_role(email, "admin")
    _acts_as(mt.resolve_caller(PER_USER, [email]), email)


# ===========================================================================
# per_user: unknown addresses
# ===========================================================================
def test_an_unknown_address_in_an_approved_domain_is_created(mt, root, store, created):
    _acts_as(mt.resolve_caller(PER_USER, [" New.Person@Approved.EXAMPLE "]), NEWBIE)
    assert created == [NEWBIE]
    assert store.user_exists(NEWBIE)
    auth = store.get_auth(NEWBIE)
    for key in ("password_hash", "temp_password_hash", "reset_token_hash",
                "sso_provider"):
        assert not auth.get(key), key
    assert store.get_role(NEWBIE) == "user"
    assert not store.get_data_roles(NEWBIE)


def test_the_creation_is_audited_once(mt, root, created):
    mt.resolve_caller(PER_USER, [NEWBIE])
    mt.resolve_caller(PER_USER, [NEWBIE])
    mt.resolve_caller(PER_USER, [NEWBIE.upper()])
    assert created == [NEWBIE]
    (row,) = _audit_rows()
    assert row["actor"] == "mcp:Helpdesk bot"
    assert row["target"] == NEWBIE
    assert row["ok"] is True
    text = json.dumps(row)
    assert HASH not in text and RAW not in text


def test_parallel_first_calls_create_and_audit_once(mt, root):
    barrier = threading.Barrier(6)
    out = []

    def call():
        barrier.wait()
        out.append(mt.resolve_caller(PER_USER, [NEWBIE]))
    threads = [threading.Thread(target=call) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(out) == 6
    for caller in out:
        _acts_as(caller, NEWBIE)
    assert len(_audit_rows()) == 1


def test_an_account_someone_else_created_meanwhile_is_used(mt, monkeypatch):
    """`create_account` answers False when the account already exists by then:
    the call acts as it and audits nothing."""
    real = local_store.AuthStore.create_account

    def raced(self, email, **kw):
        real(self, email, **kw)
        return real(self, email, **kw)
    monkeypatch.setattr(local_store.AuthStore, "create_account", raced)
    _acts_as(mt.resolve_caller(PER_USER, [NEWBIE]), NEWBIE)
    assert _audit_rows() == []


@pytest.mark.parametrize("email", [
    STRANGER, "a@sub.approved.example", "a@approved.example.evil",
    "a@notapproved.example"])
def test_an_unknown_address_elsewhere_is_refused(mt, root, created, email):
    _refused(mt.resolve_caller(PER_USER, [email]), "not_allowed")
    assert created == [] and _users(root) == {} and _audit_rows() == []


@pytest.mark.parametrize("state", ["absent", "empty", "junk"])
def test_no_approved_domain_refuses_every_unknown_address(mt, root, created, state):
    path = root / "signin_domains.json"
    if state == "absent":
        path.unlink()
    else:
        path.write_text("{}" if state == "empty" else "not json", encoding="utf-8")
    _refused(mt.resolve_caller(PER_USER, [NEWBIE]), "not_allowed")
    assert created == [] and _users(root) == {}


def test_self_registration_does_not_open_mcp(mt, root, created, monkeypatch):
    """ALLOW_SELF_REGISTRATION is the sign-in form's flag, not MCP's."""
    monkeypatch.setattr(settings, "ALLOW_SELF_REGISTRATION", True)
    _refused(mt.resolve_caller(PER_USER, [STRANGER]), "not_allowed")
    assert created == []


def test_a_removed_account_is_refused_and_not_written_back(mt, root, store, created):
    store.create_account(USER)
    store.set_password(USER, "User-passw0rd")
    assert store.remove_user(USER) is True
    created.clear()
    _refused(mt.resolve_caller(PER_USER, [USER]), "not_allowed")
    assert created == []
    assert not (root / "users" / USER).exists()


@pytest.mark.parametrize("exists", [True, False], ids=["existing", "never-created"])
def test_the_bootstrap_admin_is_refused_like_an_unknown_address(
        mt, root, store, created, monkeypatch, exists):
    """Even inside an approved domain, and never created."""
    boss = "root@approved.example"
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", boss)
    if exists:
        store.create_account(boss)
        created.clear()
    for typed in (boss, "Root@Approved.EXAMPLE", f" {boss} "):
        _refused(mt.resolve_caller(PER_USER, [typed]), "not_allowed")
    assert created == []
    assert store.user_exists(boss) is exists


def test_a_plain_admin_username_looks_like_any_other_non_address(mt, store):
    store.create_account(ADMIN)
    store.set_role(ADMIN, "admin")
    admin = mt.resolve_caller(PER_USER, [ADMIN])
    other = mt.resolve_caller(PER_USER, ["someone"])
    assert admin.email is None and other.email is None
    assert admin.refusal == other.refusal == "malformed_header"


# ===========================================================================
# failures
# ===========================================================================
def test_a_failing_account_creation_is_refused_and_logged(mt, root, logs, monkeypatch):
    def boom(self, email, **kw):
        raise OSError("disk full\nFORGED RECORD")
    monkeypatch.setattr(local_store.AuthStore, "create_account", boom)
    _refused(mt.resolve_caller(PER_USER, [NEWBIE]), "not_allowed")
    assert any("MCP" in ln and "OSError" in ln for ln in logs), logs
    assert all("\n" not in ln for ln in logs), logs
    assert _audit_rows() == []


def test_a_failing_account_lookup_is_refused_and_logged(mt, root, created, logs,
                                                        monkeypatch):
    def boom(self, email):
        raise PermissionError("no access")
    monkeypatch.setattr(local_store.AuthStore, "user_exists", boom)
    _refused(mt.resolve_caller(PER_USER, [USER]), "not_allowed")
    assert any("MCP" in ln for ln in logs), logs
    assert created == []


@pytest.mark.parametrize("record", [
    None, {}, 5, [], {"name": "x"}, {**PER_USER, "mode": None},
    {**PER_USER, "mode": "admin"}, {**PER_USER, "mode": ["per_user"]}],
    ids=["none", "empty", "int", "list", "no-mode", "mode-none", "mode-other",
         "mode-list"])
def test_a_record_without_a_known_mode_is_refused(mt, store, created, record):
    store.create_account(USER)
    created.clear()
    _refused(mt.resolve_caller(record, [USER]), "not_allowed")
    _refused(mt.resolve_caller(record, [NEWBIE]), "not_allowed")
    assert created == []


# ===========================================================================
# shared_account
# ===========================================================================
@pytest.fixture
def shared(mt, store):
    """A shared_account token, through the store's own create + verify."""
    store.create_account(BOT)
    _, raw = mt.create_token("Shared bot", "shared_account", ADMIN, account=BOT)
    return mt.verify(raw)


@pytest.mark.parametrize("values", [
    [], [USER], [NEWBIE], ["junk"], [""], [USER, STRANGER], [ADMIN],
    ["a@approved.example\nFORGED"]],
    ids=["none", "user", "approved", "junk", "blank", "two", "admin", "newline"])
def test_a_shared_account_ignores_the_header(mt, root, shared, created, values):
    before = _users(root)
    _acts_as(mt.resolve_caller(shared, values), BOT)
    assert created == [] and _users(root) == before


def test_a_removed_bound_account_is_refused(mt, root, store, shared, created):
    assert store.remove_user(BOT) is True
    _refused(mt.resolve_caller(shared, [USER]), "not_allowed")
    assert created == [] and not (root / "users" / BOT).exists()


def test_a_removed_bound_account_in_an_approved_domain_is_not_created(
        mt, root, store, created):
    bot = "bot@approved.example"
    store.create_account(bot)
    _, raw = mt.create_token("Shared bot", "shared_account", ADMIN, account=bot)
    rec = mt.verify(raw)
    store.remove_user(bot)
    created.clear()
    _refused(mt.resolve_caller(rec, [bot]), "not_allowed")
    assert created == [] and not (root / "users" / bot).exists()


def _set_created_at(root, email, value):
    path = root / "users" / email / "profile.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    if value is None:
        doc.pop("created_at", None)
    else:
        doc["created_at"] = value
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_a_recreated_bound_account_is_refused(mt, root, store, shared):
    """The same address after a removal is another account: the old token
    must not act as it."""
    store.remove_user(BOT)
    store.create_account(BOT)
    _set_created_at(root, BOT, "2030-01-01T00:00:00+00:00")
    _refused(mt.resolve_caller(shared, []), "not_allowed")


def test_a_bound_account_that_lost_its_creation_stamp_is_refused(mt, root, shared):
    _set_created_at(root, BOT, None)
    _refused(mt.resolve_caller(shared, []), "not_allowed")


def test_a_bound_account_with_an_unreadable_profile_is_refused(mt, root, shared):
    (root / "users" / BOT / "profile.json").write_text("not json", encoding="utf-8")
    _refused(mt.resolve_caller(shared, []), "not_allowed")


def test_profile_edits_do_not_unbind_the_account(mt, store, shared):
    store.set_role(BOT, "power")
    store.touch_last_login(BOT)
    store.set_password(BOT, "User-passw0rd")
    _acts_as(mt.resolve_caller(shared, []), BOT)


def test_an_account_that_became_the_bootstrap_admin_is_refused(mt, shared, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", BOT)
    _refused(mt.resolve_caller(shared, []), "not_allowed")


@pytest.mark.parametrize("over", [
    {"account": None}, {"account": ""}, {"account": 5}, {"account": "junk"},
    {"account": "../" + BOT}, {"account_created_at": None},
    {"account_created_at": "2030-01-01T00:00:00+00:00"}], ids=range(7))
def test_a_shared_record_without_a_valid_binding_is_refused(mt, shared, created, over):
    _refused(mt.resolve_caller({**shared, **over}, [BOT]), "not_allowed")
    assert created == []


# ===========================================================================
# logs
# ===========================================================================
def test_no_log_line_carries_the_token_or_an_unescaped_header(mt, root, store, logs):
    store.create_account(USER)
    header = "a@approved.example\nFORGED RECORD"
    record = {**PER_USER, "name": "Bot\nFORGED NAME"}
    for values in ([header], [], [USER], [NEWBIE], [STRANGER], [header, USER]):
        mt.resolve_caller(record, values, peer="10.0.0.5\nFORGED PEER")
    text = "\n".join(logs)
    for line in logs:
        assert "\n" not in line and "\r" not in line, line
    assert header not in text
    assert HASH not in text and RAW not in text


def test_a_created_account_is_logged_without_secrets(mt, root, logs):
    mt.resolve_caller(PER_USER, [NEWBIE])
    assert any("MCP" in ln for ln in logs), logs
    assert HASH not in "\n".join(logs)
