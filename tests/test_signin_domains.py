"""Approved sign-in domains (`signin_domains.py`, the admin API, the login
branch and the landing footer).

The contract pinned here:

* Store: `DATA_ROOT/signin_domains.json` = `{"domains": [...], "updated_at",
  "updated_by"}`. A missing file is an empty list (every install from before
  this feature — the old-shape regression); a junk file reads as empty and
  never raises. `normalize_domains` takes a LIST of str, strips, lower-cases
  and de-duplicates (first order kept) and raises ValueError on any invalid
  entry or on a non-list; `save` writes nothing when it raises.
  `allows_email` compares the text after the LAST "@" (exact domain, no
  subdomain match) and never raises.
* Admin API: `GET/POST /api/admin/signin_domains`, ladmin only (401 without a
  session, 403 + `admin.denied` otherwise), POST replaces the list, audited
  `signin_domains.set` with the list; an invalid entry or a non-list is a
  400 with nothing persisted.
* `/auth/login`: a genuinely NEW address (no account folder) in an approved
  domain gets its account from the typed password — the same branch as
  ALLOW_SELF_REGISTRATION (password rule first, welcome mail, 302 /lab),
  logged `USER_LOGIN_DOMAIN_ACCOUNT_CREATED`; the account is a Standard user
  with no data roles. An unapproved domain, and an EXISTING password-less
  placeholder in an approved domain, keep the neutral 401 page.
* Landing footer: "Accounts are created by invitation." while no domain is
  configured; the "approved company domain" sentence once one is — on both
  render paths (app.py's `GET /` and routes.auth `_landing`).

Offline: DATA_ROOT is tmp_path, the brain relays are stubbed, router-only app
(the tests/test_admin_invite.py idiom) plus the real app for `GET /`. The
login / landing tests seed the domain list by writing the JSON file
directly, so they fail on behaviour rather than on the module's import.
"""
import json
import logging

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
from tests.conftest import csrf_form

ADMIN = "ladmin"
USER = "user@corp.example"
USER_PW = "User-passw0rd"
APPROVED = "approved.example"
NEWBIE = "new.person@approved.example"
NEW_PW = "chosen-passw0rd"
STRANGER = "stranger@other.example"
BASE = "https://pdc.corp.example"

INVITATION_TEXT = "Accounts are created by invitation."
DOMAIN_TEXT = "approved company domain"
EVENT = "USER_LOGIN_DOMAIN_ACCOUNT_CREATED"


# ===========================================================================
# helpers
# ===========================================================================
def _domains_file(tmp):
    return tmp / "signin_domains.json"


def _seed_domains(tmp, domains):
    """Write the store file the way the module stores it (independent of the
    module, so the behaviour tests do not depend on its import)."""
    _domains_file(tmp).write_text(
        json.dumps({"domains": list(domains), "updated_at": "2026-10-15T00:00:00Z",
                    "updated_by": ADMIN}),
        encoding="utf-8")


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
    """Every line written through `log_with_sid` (the `datachat` logger),
    whichever module emits it."""
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


def _logged(lines, event):
    return any(event in line for line in lines)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def sd(isolated):
    """The store module, imported here (a missing module fails the test)."""
    import signin_domains
    return signin_domains


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    for name, value in (("PUBLIC_BASE_URL", BASE), ("ALLOW_SELF_REGISTRATION", False)):
        if name in type(settings).model_fields:
            monkeypatch.setattr(settings, name, value)
    index = getattr(local_store, "_RESET_TOKEN_INDEX", None)
    if isinstance(index, dict):
        index.clear()
    store = local_store.AuthStore()
    store.ensure_user(ADMIN)
    store.set_role(ADMIN, "admin")
    store.ensure_user(USER)
    store.set_password(USER, USER_PW)
    roles_store.RolesStore().ensure_base_role()

    sent = []
    welcomed = []

    def fake_reset_mail(email, reset_url, **kwargs):
        sent.append((email, reset_url, dict(kwargs)))
        return {"ok": True}

    monkeypatch.setattr(brain_client, "send_password_reset_email", fake_reset_mail)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)

    import routes.admin_users as users_mod
    import routes.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async",
                        lambda email: welcomed.append(email))
    monkeypatch.setattr(auth_mod, "_run_in_background",
                        lambda fn, *args: fn(*args), raising=False)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(users_mod.router)
    app.include_router(auth_mod.router)

    @app.get("/")
    async def _landing(request: Request):
        # The real app's landing mints the form token (tests.conftest.csrf_form).
        auth_mod.csrf_token(request)
        return {"ok": True}

    @app.get("/_landing_page")
    async def _landing_page(request: Request):
        return auth_mod._landing(request)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session.pop("must_change_password", None)
        return {"ok": True}

    def client(who=None):
        tc = TestClient(app, raise_server_exceptions=False)
        if who:
            tc.post(f"/_login/{who}")
        return tc

    return {"tmp": tmp_path, "sent": sent, "welcomed": welcomed, "client": client}


def _login(tc, email, password):
    return tc.post("/auth/login",
                   data=csrf_form(tc, {"email": email, "password": password}),
                   follow_redirects=False)


def _audit_rows(action):
    return [r for r in db_sources.read_audit_tail(500) if r.get("action") == action]


# ===========================================================================
# the store
# ===========================================================================
def test_an_absent_file_is_an_empty_list(sd, isolated):
    assert not _domains_file(isolated).exists()
    assert sd.load() == []
    assert sd.is_configured() is False
    assert sd.allows_email("bob@example.com") is False
    assert not _domains_file(isolated).exists(), "a read must not create the file"


def test_old_shape_install_without_the_file_still_loads(sd, isolated):
    """Old-shape regression: a DATA_ROOT written by the previous release
    (accounts, no signin_domains.json) reads as "no domain approved" — the
    invitation-only behaviour it had — and nothing is written by reading."""
    store = local_store.AuthStore()
    store.ensure_user(USER)
    store.set_password(USER, USER_PW)
    before = sorted(p.name for p in isolated.iterdir())
    assert sd.load() == []
    assert sd.is_configured() is False
    assert sd.allows_email(USER) is False
    assert sorted(p.name for p in isolated.iterdir()) == before


def test_save_then_reload(sd, isolated):
    out = sd.save(["Example.COM", " corp.example ", "example.com"], ADMIN)
    assert out == ["example.com", "corp.example"]
    assert sd.load() == ["example.com", "corp.example"]
    assert sd.is_configured() is True
    doc = json.loads(_domains_file(isolated).read_text(encoding="utf-8"))
    assert doc["domains"] == ["example.com", "corp.example"]
    assert doc.get("updated_by") == ADMIN
    assert doc.get("updated_at")


def test_saving_an_empty_list_clears_the_configuration(sd, isolated):
    sd.save(["example.com"], ADMIN)
    assert sd.save([], ADMIN) == []
    assert sd.load() == []
    assert sd.is_configured() is False
    assert sd.allows_email("bob@example.com") is False


def test_normalize_trims_lowercases_and_dedupes_in_first_order(sd):
    assert sd.normalize_domains(
        ["B.example", "a.example", " b.EXAMPLE ", "A.Example", "c.example"]) == \
        ["b.example", "a.example", "c.example"]
    assert sd.normalize_domains([]) == []


INVALID = [
    "bob@example.com", "https://example.com", "example.com/a", "x .com",
    "a b.com", "a..com", ".com", "localhost", "", 5, None,
]


@pytest.mark.parametrize("bad", INVALID, ids=[
    "at-sign", "scheme", "path", "space-before-dot", "inner-space",
    "empty-label", "leading-dot", "no-dot", "empty-string", "int", "none"])
def test_an_invalid_entry_raises_and_writes_nothing(sd, isolated, bad):
    with pytest.raises(ValueError):
        sd.normalize_domains(["good.example", bad])
    with pytest.raises(ValueError):
        sd.save(["good.example", bad], ADMIN)
    assert not _domains_file(isolated).exists()
    assert sd.load() == []


@pytest.mark.parametrize("bad", ["a.com", {"domains": ["a.com"]}, None, 5])
def test_a_non_list_raises_and_writes_nothing(sd, isolated, bad):
    with pytest.raises(ValueError):
        sd.normalize_domains(bad)
    with pytest.raises(ValueError):
        sd.save(bad, ADMIN)
    assert not _domains_file(isolated).exists()


def test_a_failed_save_keeps_the_previous_list(sd, isolated):
    sd.save(["example.com"], ADMIN)
    before = _domains_file(isolated).read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        sd.save(["corp.example", "not a domain"], ADMIN)
    assert _domains_file(isolated).read_text(encoding="utf-8") == before
    assert sd.load() == ["example.com"]


@pytest.mark.parametrize("email,expected", [
    ("bob@example.com", True),
    ("Bob@EXAMPLE.COM", True),
    ("  bob@example.com  ", True),
    ("a@b@example.com", True),           # the domain is after the LAST "@"
    ("a@example.com@evil.example", False),
    ("bob@corp.example", True),
    ("bob@sub.example.com", False),      # exact domain, no subdomain match
    ("bob@example.com.evil", False),
    ("bob@other.example", False),
    ("example.com", False),              # no "@"
    ("", False),
    ("bob@", False),
])
def test_allows_email(sd, email, expected):
    sd.save(["example.com", "corp.example"], ADMIN)
    assert sd.allows_email(email) is expected


@pytest.mark.parametrize("value", [None, 5, ["bob@example.com"]])
def test_allows_email_never_raises(sd, value):
    sd.save(["example.com"], ADMIN)
    assert sd.allows_email(value) is False


@pytest.mark.parametrize("content", [
    "not json at all",
    json.dumps(["example.com"]),                 # not a dict
    json.dumps({"domains": "example.com"}),      # domains not a list
    json.dumps({"domains": [5, None, "bad domain", "x@y.com", ""]}),
    json.dumps(None),
    "",
], ids=["not-json", "list", "string-domains", "junk-entries", "null", "empty"])
def test_a_junk_file_reads_as_empty_and_never_raises(sd, isolated, content):
    _domains_file(isolated).write_text(content, encoding="utf-8")
    assert sd.load() == []
    assert sd.is_configured() is False
    assert sd.allows_email("bob@example.com") is False


def test_junk_entries_never_appear_in_the_loaded_list(sd, isolated):
    _domains_file(isolated).write_text(
        json.dumps({"domains": ["example.com", 5, "bad domain", "x@y.com"]}),
        encoding="utf-8")
    loaded = sd.load()
    assert isinstance(loaded, list)
    for junk in (5, "bad domain", "x@y.com"):
        assert junk not in loaded
    assert sd.allows_email("x@bad domain") is False


# ===========================================================================
# the admin API
# ===========================================================================
def test_the_api_requires_a_session(world):
    tc = world["client"]()
    assert tc.get("/api/admin/signin_domains").status_code == 401
    r = tc.post("/api/admin/signin_domains", json={"domains": ["example.com"]})
    assert r.status_code == 401
    assert not _domains_file(world["tmp"]).exists()


def test_the_api_refuses_a_non_admin_and_audits_it(world):
    tc = world["client"](USER)
    assert tc.get("/api/admin/signin_domains").status_code == 403
    r = tc.post("/api/admin/signin_domains", json={"domains": ["example.com"]})
    assert r.status_code == 403
    assert not _domains_file(world["tmp"]).exists()
    denied = _audit_rows("admin.denied")
    assert len(denied) == 2, denied
    assert not _audit_rows("signin_domains.set")


def test_get_answers_an_empty_list_on_a_fresh_install(world):
    r = world["client"](ADMIN).get("/api/admin/signin_domains")
    assert r.status_code == 200, r.text[:300]
    assert r.json() == {"domains": []}


def test_post_then_get_round_trip(world):
    tc = world["client"](ADMIN)
    r = tc.post("/api/admin/signin_domains",
                json={"domains": ["Example.com", " corp.example", "example.com"]})
    assert r.status_code == 200, r.text[:300]
    assert r.json() == {"ok": True, "domains": ["example.com", "corp.example"]}
    g = tc.get("/api/admin/signin_domains")
    assert g.status_code == 200
    assert g.json() == {"domains": ["example.com", "corp.example"]}
    assert json.loads(_domains_file(world["tmp"]).read_text(encoding="utf-8"))[
        "domains"] == ["example.com", "corp.example"]


def test_post_replaces_the_list(world):
    tc = world["client"](ADMIN)
    assert tc.post("/api/admin/signin_domains",
                   json={"domains": ["example.com"]}).status_code == 200
    r = tc.post("/api/admin/signin_domains", json={"domains": ["corp.example"]})
    assert r.status_code == 200
    assert tc.get("/api/admin/signin_domains").json() == {"domains": ["corp.example"]}
    r = tc.post("/api/admin/signin_domains", json={"domains": []})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "domains": []}
    assert tc.get("/api/admin/signin_domains").json() == {"domains": []}


def test_post_is_audited_with_the_list(world):
    tc = world["client"](ADMIN)
    assert tc.post("/api/admin/signin_domains",
                   json={"domains": ["Example.com", "corp.example"]}).status_code == 200
    rows = _audit_rows("signin_domains.set")
    assert len(rows) == 1, rows
    assert rows[0].get("actor") == ADMIN
    assert (rows[0].get("detail") or {}).get("domains") == ["example.com", "corp.example"]


@pytest.mark.parametrize("bad", ["bob@example.com", "https://example.com",
                                 "example.com/a", "a b.com", "a..com", "localhost",
                                 "", 5, None])
def test_post_with_an_invalid_entry_is_a_400_and_persists_nothing(world, bad):
    tc = world["client"](ADMIN)
    r = tc.post("/api/admin/signin_domains", json={"domains": ["example.com", bad]})
    assert r.status_code == 400, (r.status_code, r.text[:300])
    body = r.json()
    assert isinstance(body.get("error"), str) and body["error"], body
    assert not _domains_file(world["tmp"]).exists()
    assert not _audit_rows("signin_domains.set")


@pytest.mark.parametrize("body", [{"domains": "example.com"}, {"domains": None},
                                  {"domains": {"example.com": True}}, {}],
                         ids=["string", "null", "object", "missing"])
def test_post_with_a_non_list_is_a_400_and_persists_nothing(world, body):
    tc = world["client"](ADMIN)
    r = tc.post("/api/admin/signin_domains", json=body)
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert isinstance(r.json().get("error"), str)
    assert not _domains_file(world["tmp"]).exists()


def test_a_rejected_post_keeps_the_previous_list(world):
    tc = world["client"](ADMIN)
    assert tc.post("/api/admin/signin_domains",
                   json={"domains": ["example.com"]}).status_code == 200
    r = tc.post("/api/admin/signin_domains", json={"domains": ["corp.example", "a b.com"]})
    assert r.status_code == 400
    assert tc.get("/api/admin/signin_domains").json() == {"domains": ["example.com"]}


# ===========================================================================
# /auth/login — the approved-domain branch
# ===========================================================================
def test_a_new_address_in_an_approved_domain_creates_its_account(world, logs):
    _seed_domains(world["tmp"], [APPROVED])
    tc = world["client"]()
    r = _login(tc, NEWBIE, NEW_PW)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert r.headers["location"] == "/lab"
    store = local_store.AuthStore()
    assert store.user_exists(NEWBIE)
    assert store.has_password(NEWBIE)
    assert store.verify_password(NEWBIE, NEW_PW) is not None
    assert tc.get("/auth/me").status_code == 200, "the new session did not hold"
    assert world["welcomed"] == [NEWBIE], world["welcomed"]
    assert _logged(logs, EVENT), [l for l in logs if "USER_LOGIN" in l]


def test_the_domain_account_is_a_standard_user_without_data_roles(world):
    _seed_domains(world["tmp"], [APPROVED])
    assert _login(world["client"](), NEWBIE, NEW_PW).status_code == 302
    store = local_store.AuthStore()
    assert store.get_role(NEWBIE) == "user"
    assert not store.is_admin(NEWBIE) and not store.is_power(NEWBIE)
    assert store.get_data_roles(NEWBIE) == []


def test_the_domain_account_signs_in_again_with_its_password(world):
    _seed_domains(world["tmp"], [APPROVED])
    assert _login(world["client"](), NEWBIE, NEW_PW).status_code == 302
    again = _login(world["client"](), NEWBIE, NEW_PW)
    assert again.status_code == 302 and again.headers["location"] == "/lab"
    wrong = _login(world["client"](), NEWBIE, "another-passw0rd")
    assert wrong.status_code == 401


def test_the_domain_match_is_case_insensitive(world):
    _seed_domains(world["tmp"], [APPROVED])
    r = _login(world["client"](), "New.Person@APPROVED.Example", NEW_PW)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert local_store.AuthStore().has_password(NEWBIE)


def test_a_short_password_in_an_approved_domain_is_a_400_and_creates_nothing(
        world, monkeypatch):
    monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 8)
    _seed_domains(world["tmp"], [APPROVED])
    r = _login(world["client"](), NEWBIE, "short")
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert "Password must be at least 8 characters" in r.text
    assert not (world["tmp"] / "users" / NEWBIE).exists()
    assert not local_store.AuthStore().user_exists(NEWBIE)
    assert world["welcomed"] == []


def test_an_unapproved_domain_keeps_the_neutral_refusal(world, logs):
    """Unchanged behaviour: the domain list does not open other domains."""
    _seed_domains(world["tmp"], [APPROVED])
    r = _login(world["client"](), STRANGER, NEW_PW)
    assert r.status_code == 401, (r.status_code, r.headers.get("location"))
    assert 'data-i18n="auth.signin_failed"' in r.text
    assert not (world["tmp"] / "users" / STRANGER).exists()
    assert not _logged(logs, EVENT)


def test_a_subdomain_of_an_approved_domain_is_refused(world):
    _seed_domains(world["tmp"], [APPROVED])
    r = _login(world["client"](), "bob@sub.approved.example", NEW_PW)
    assert r.status_code == 401
    assert not (world["tmp"] / "users" / "bob@sub.approved.example").exists()


def test_an_empty_domain_list_keeps_sign_in_invitation_only(world):
    _seed_domains(world["tmp"], [])
    r = _login(world["client"](), NEWBIE, NEW_PW)
    assert r.status_code == 401
    assert not (world["tmp"] / "users" / NEWBIE).exists()


def test_an_invited_placeholder_in_an_approved_domain_is_still_refused(world):
    """The typed password never becomes a placeholder's: the mailbox owner
    sets it through the invitation / reset link."""
    _seed_domains(world["tmp"], [APPROVED])
    store = local_store.AuthStore()
    assert store.ensure_invited_user(NEWBIE, ADMIN)
    r = _login(world["client"](), NEWBIE, NEW_PW)
    assert r.status_code == 401, (r.status_code, r.headers.get("location"))
    assert 'data-i18n="auth.signin_failed"' in r.text
    assert store.has_password(NEWBIE) is False
    assert world["welcomed"] == []


def test_an_existing_account_in_an_approved_domain_keeps_its_password(world):
    _seed_domains(world["tmp"], [APPROVED])
    store = local_store.AuthStore()
    store.ensure_user(NEWBIE)
    store.set_password(NEWBIE, "original-passw0rd")
    r = _login(world["client"](), NEWBIE, NEW_PW)
    assert r.status_code == 401
    assert store.verify_password(NEWBIE, "original-passw0rd") is not None
    assert store.verify_password(NEWBIE, NEW_PW) is None


def test_self_registration_without_domains_is_unchanged_and_not_logged_as_domain(
        world, monkeypatch, logs):
    """ALLOW_SELF_REGISTRATION alone still creates the account (the hosted
    demo), and that branch does not log the domain event."""
    monkeypatch.setattr(settings, "ALLOW_SELF_REGISTRATION", True)
    assert not _domains_file(world["tmp"]).exists()
    r = _login(world["client"](), STRANGER, NEW_PW)
    assert r.status_code == 302 and r.headers["location"] == "/lab"
    assert local_store.AuthStore().has_password(STRANGER)
    assert not _logged(logs, EVENT)


# ===========================================================================
# password reset for a domain-created account
# ===========================================================================
def test_a_domain_created_account_can_request_a_reset_link(world):
    _seed_domains(world["tmp"], [APPROVED])
    assert _login(world["client"](), NEWBIE, NEW_PW).status_code == 302
    tc = world["client"]()
    r = tc.post("/auth/reset_password", data=csrf_form(tc, {"email": NEWBIE}),
                follow_redirects=False)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert [to for to, _, _ in world["sent"]] == [NEWBIE], world["sent"]
    url = world["sent"][0][1]
    assert url.startswith(BASE + "/auth/reset/"), url
    assert world["sent"][0][2].get("kind", "reset") == "reset"


# ===========================================================================
# the landing footer
# ===========================================================================
def test_the_landing_says_invitation_only_without_domains(world):
    page = world["client"]().get("/_landing_page")
    assert page.status_code == 200
    assert INVITATION_TEXT in page.text
    assert DOMAIN_TEXT not in page.text


def test_the_landing_names_approved_domains_once_configured(world):
    _seed_domains(world["tmp"], [APPROVED])
    page = world["client"]().get("/_landing_page")
    assert page.status_code == 200
    assert DOMAIN_TEXT in page.text
    assert INVITATION_TEXT not in page.text


def test_the_refusal_page_footer_follows_the_configuration(world):
    """The 401 page is rendered by routes.auth `_landing` too."""
    r = _login(world["client"](), STRANGER, NEW_PW)
    assert r.status_code == 401
    assert INVITATION_TEXT in r.text and DOMAIN_TEXT not in r.text
    _seed_domains(world["tmp"], [APPROVED])
    r = _login(world["client"](), STRANGER, NEW_PW)
    assert r.status_code == 401
    assert DOMAIN_TEXT in r.text and INVITATION_TEXT not in r.text


@pytest.fixture
def real_app(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    if "ALLOW_SELF_REGISTRATION" in type(settings).model_fields:
        monkeypatch.setattr(settings, "ALLOW_SELF_REGISTRATION", False)
    import app as app_mod
    return TestClient(app_mod.app, base_url="https://testserver",
                      raise_server_exceptions=False)


def test_the_app_landing_says_invitation_only_without_domains(real_app):
    page = real_app.get("/")
    assert page.status_code == 200
    assert INVITATION_TEXT in page.text
    assert DOMAIN_TEXT not in page.text


def test_the_app_landing_names_approved_domains_once_configured(real_app, tmp_path):
    _seed_domains(tmp_path, [APPROVED])
    page = real_app.get("/")
    assert page.status_code == 200
    assert DOMAIN_TEXT in page.text
    assert INVITATION_TEXT not in page.text
