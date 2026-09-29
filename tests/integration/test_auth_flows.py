"""Sign-in, reset link and attempt limiting against the running stack (Task 9).

What only the stack can show: the reset link works end to end through the
real middleware (the page carries the page policy and its own no-store /
no-referrer headers, the forced-change gate lets it through), an uninvited
address leaves no directory on the persistent volume, known and unknown reset
requests are indistinguishable over HTTP, and the limiter answers 429 with a
`Retry-After` through the real proxy-less peer address.

Accounts are pre-created inside the web container (`precreate_account`), and
the reset token is MINTED inside the container through the app's own store
(`AuthStore().create_reset_token`) -- the brain relay is not needed, and the
mailed link is never read. Every address created here is registered in
`session_scoped_extra_emails`, so the package teardown removes it.

The lockout case runs LAST in this module: the per-IP counter lives in the web
process and would otherwise throttle later tests (the lead restarts the web
container after the run to clear it).

Gated like the rest of `tests/integration/`: skipped unless `PDC_STACK_URL`
is set; skipped with a reason when docker is unavailable.
"""
import re
import secrets

import httpx
import pytest

from tests.conftest import csrf_form

from .conftest import (REQUEST_TIMEOUT_S, WEB_CONTAINER, _b64, docker_available,
                       docker_exec, precreate_account)

pytestmark = [pytest.mark.integration]

NEUTRAL_RESET = "If an account exists for this address, a reset link has been sent."
NEUTRAL_FAILURE = "Sign-in failed. Check your email and password"
INVALID_LINK = "This reset link is invalid or has expired."
RESET_DONE = "Your password has been updated."
TOO_MANY = "Too many attempts. Please try again later."
TOKEN_RE = re.compile(r"RESET_TOKEN=([A-Za-z0-9_-]{43})\b")
_NONCE_RE = re.compile(r"""nonce(?:-[A-Za-z0-9_\-]+|\s*=\s*["'][^"']*["'])"""
                       r"""|__CSP_NONCE__\s*=\s*["'][^"']*["']""")

# Runs INSIDE pdc-client: mints a reset token through the app's own store.
_MINT_SCRIPT = (
    "import base64, sys\n"
    "import local_store\n"
    "email = base64.b64decode(sys.argv[1]).decode('utf-8')\n"
    "token = local_store.AuthStore().create_reset_token(email)\n"
    "print('RESET_TOKEN=' + (token or 'NONE'))\n"
)


def _require_docker():
    if not docker_available():
        pytest.skip("docker is not on PATH: accounts and tokens are created inside "
                    "the web container")


def _new_account(extra_emails) -> dict:
    email = f"integration-auth-{secrets.token_hex(4)}@example.invalid"
    password = f"pw-{secrets.token_hex(8)}"
    extra_emails.append(email)
    assert precreate_account(email, password)
    return {"email": email, "password": password}


def _mint(email: str) -> str:
    result = docker_exec(WEB_CONTAINER, "python", "-c", _MINT_SCRIPT, _b64(email))
    assert result.returncode == 0, (result.stdout[-300:], result.stderr[-500:])
    found = TOKEN_RE.findall(result.stdout or "")
    assert found, "no reset token was minted in the container"
    return found[-1]


def _exists_in_container(path: str) -> bool:
    return docker_exec(WEB_CONTAINER, "test", "-e", path).returncode == 0


def _client(base_url):
    return httpx.Client(base_url=base_url, follow_redirects=False, timeout=REQUEST_TIMEOUT_S)


def _login(base_url, email, password):
    with _client(base_url) as c:
        return c.post("/auth/login", data=csrf_form(c, {"email": email, "password": password}))


def _normalise(text: str, email: str) -> str:
    return _NONCE_RE.sub("nonce", text.replace(email, "EMAIL"))


# ---------------------------------------------------------------------------
# the reset link, end to end
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def reset_account(base_url, session_scoped_extra_emails):
    _require_docker()
    return _new_account(session_scoped_extra_emails)


@pytest.fixture(scope="module")
def used_token(base_url, reset_account):
    token = _mint(reset_account["email"])
    new_password = f"new-{secrets.token_hex(8)}"
    with _client(base_url) as c:
        page = c.get(f"/auth/reset/{token}")
        post = c.post(f"/auth/reset/{token}",
                      data=csrf_form(c, {"new_password": new_password,
                                         "confirm_password": new_password}))
    return {"token": token, "page": page, "post": post, "new_password": new_password}


def test_the_reset_form_is_served_under_the_page_policy(used_token):
    page = used_token["page"]
    assert page.status_code == 200, (page.status_code, page.text[:300])
    assert "Set a new password" in page.text
    assert f'action="/auth/reset/{used_token["token"]}"' in page.text
    assert page.headers.get("content-security-policy"), dict(page.headers)
    assert "no-store" in page.headers.get("cache-control", "").lower()
    assert page.headers.get("referrer-policy", "").lower() == "no-referrer"


def test_setting_the_password_redirects_to_the_done_page(base_url, used_token):
    post = used_token["post"]
    assert post.status_code == 302, (post.status_code, post.text[:300])
    assert post.headers.get("location") == "/?reset=done"
    with _client(base_url) as c:
        landing = c.get("/?reset=done")
    assert landing.status_code == 200 and RESET_DONE in landing.text


def test_the_new_password_signs_in(base_url, reset_account, used_token):
    r = _login(base_url, reset_account["email"], used_token["new_password"])
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert r.headers.get("location") == "/lab"
    old = _login(base_url, reset_account["email"], reset_account["password"])
    assert old.status_code == 401


def test_a_used_link_is_refused(base_url, used_token):
    with _client(base_url) as c:
        again = c.get(f"/auth/reset/{used_token['token']}")
        post = c.post(f"/auth/reset/{used_token['token']}",
                      data=csrf_form(c, {"new_password": "x-pw-12345",
                                         "confirm_password": "x-pw-12345"}))
    assert again.status_code == 404 and INVALID_LINK in again.text
    assert post.status_code == 404


# ---------------------------------------------------------------------------
# invitation-only sign-in and the neutral reset answer
# ---------------------------------------------------------------------------
def test_an_uninvited_address_cannot_sign_in_and_leaves_no_directory(
        base_url, session_scoped_extra_emails):
    _require_docker()
    email = f"integration-uninvited-{secrets.token_hex(4)}@example.invalid"
    session_scoped_extra_emails.append(email)          # removed even if created
    r = _login(base_url, email, f"pw-{secrets.token_hex(8)}")
    assert r.status_code == 401, (r.status_code, r.headers.get("location"))
    assert NEUTRAL_FAILURE in r.text
    assert not _exists_in_container(f"/data/client/users/{email}")


def test_unknown_and_known_reset_requests_answer_alike(base_url, session_scoped_extra_emails):
    _require_docker()
    known = _new_account(session_scoped_extra_emails)["email"]
    unknown = f"integration-unknown-{secrets.token_hex(4)}@example.invalid"
    session_scoped_extra_emails.append(unknown)
    with _client(base_url) as c:
        a = c.post("/auth/reset_password", data=csrf_form(c, {"email": known}))
        b = c.post("/auth/reset_password", data=csrf_form(c, {"email": unknown}))
    assert (a.status_code, b.status_code) == (200, 200)
    assert NEUTRAL_RESET in a.text
    assert _normalise(a.text, known) == _normalise(b.text, unknown)
    assert not _exists_in_container(f"/data/client/users/{unknown}")


def test_a_reset_ends_a_session_that_was_already_open(base_url, session_scoped_extra_emails):
    """A browser signed in before the reset is signed out by it: its next
    API call answers 401 and the response clears the session cookie."""
    _require_docker()
    account = _new_account(session_scoped_extra_emails)
    with _client(base_url) as open_session:
        signed_in = open_session.post("/auth/login",
                                      data=csrf_form(open_session,
                                                     {"email": account["email"],
                                                      "password": account["password"]}))
        assert signed_in.status_code == 302, (signed_in.status_code, signed_in.text[:300])
        assert open_session.get("/auth/profile").status_code == 200
        token = _mint(account["email"])
        new_password = f"new-{secrets.token_hex(8)}"
        with _client(base_url) as c:
            done = c.post(f"/auth/reset/{token}",
                          data=csrf_form(c, {"new_password": new_password,
                                             "confirm_password": new_password}))
        assert done.status_code == 302, (done.status_code, done.text[:300])
        after = open_session.get("/auth/profile")
    assert after.status_code == 401, (after.status_code, after.text[:300])
    assert "session=null" in after.headers.get("set-cookie", ""), dict(after.headers)


# ---------------------------------------------------------------------------
# accounts that are not plain password accounts
# ---------------------------------------------------------------------------
# Runs INSIDE pdc-client: an account WITHOUT a password that has signed in
# with Microsoft (the SSO provenance stamp, nothing else).
_SSO_ACCOUNT_SCRIPT = (
    "import base64, sys\n"
    "import local_store\n"
    "email = base64.b64decode(sys.argv[1]).decode('utf-8')\n"
    "store = local_store.AuthStore()\n"
    "store.ensure_user(email)\n"
    "store.mark_sso_login(email, 'microsoft')\n"
    "print('SSO_ONLY=' + str(store.is_sso_only(email)))\n"
)

# Runs INSIDE pdc-client: an account whose password must be changed at the
# next sign-in.
_FORCED_ACCOUNT_SCRIPT = (
    "import base64, sys\n"
    "import local_store\n"
    "email = base64.b64decode(sys.argv[1]).decode('utf-8')\n"
    "password = base64.b64decode(sys.argv[2]).decode('utf-8')\n"
    "store = local_store.AuthStore()\n"
    "store.ensure_user(email)\n"
    "store.set_password(email, password, force_change=True)\n"
    "print('ok')\n"
)

# Runs INSIDE pdc-client: whether the account's auth record holds a reset
# link hash (the value itself is never printed).
_HAS_RESET_HASH_SCRIPT = (
    "import base64, sys\n"
    "import local_store\n"
    "email = base64.b64decode(sys.argv[1]).decode('utf-8')\n"
    "auth = local_store.AuthStore().get_auth(email)\n"
    "print('HAS_RESET_HASH=' + str(bool(auth.get('reset_token_hash'))))\n"
)

RULE_RE = re.compile(r"Password must be at least \d+ characters")


def test_an_sso_only_account_gets_the_neutral_answer_and_no_link(
        base_url, session_scoped_extra_emails):
    """A reset request for an account that signs in with Microsoft and has
    no password answers exactly like one for an unknown address, and no
    reset link is minted for it."""
    import time
    _require_docker()
    email = f"integration-sso-{secrets.token_hex(4)}@example.invalid"
    session_scoped_extra_emails.append(email)
    made = docker_exec(WEB_CONTAINER, "python", "-c", _SSO_ACCOUNT_SCRIPT, _b64(email))
    assert made.returncode == 0, (made.stdout[-300:], made.stderr[-500:])
    assert "SSO_ONLY=True" in (made.stdout or ""), made.stdout[-300:]
    unknown = f"integration-unknown-{secrets.token_hex(4)}@example.invalid"
    session_scoped_extra_emails.append(unknown)
    with _client(base_url) as c:
        a = c.post("/auth/reset_password", data=csrf_form(c, {"email": email}))
        b = c.post("/auth/reset_password", data=csrf_form(c, {"email": unknown}))
    assert (a.status_code, b.status_code) == (200, 200)
    assert NEUTRAL_RESET in a.text
    assert _normalise(a.text, email) == _normalise(b.text, unknown)
    time.sleep(2.0)                    # the mail hand-off runs off the request
    check = docker_exec(WEB_CONTAINER, "python", "-c", _HAS_RESET_HASH_SCRIPT, _b64(email))
    assert check.returncode == 0, (check.stdout[-300:], check.stderr[-500:])
    assert "HAS_RESET_HASH=False" in (check.stdout or ""), check.stdout[-300:]


def test_the_forced_change_applies_the_password_rule(base_url, session_scoped_extra_emails):
    _require_docker()
    email = f"integration-forced-{secrets.token_hex(4)}@example.invalid"
    password = f"pw-{secrets.token_hex(8)}"
    session_scoped_extra_emails.append(email)
    made = docker_exec(WEB_CONTAINER, "python", "-c", _FORCED_ACCOUNT_SCRIPT,
                       _b64(email), _b64(password))
    assert made.returncode == 0, (made.stdout[-300:], made.stderr[-500:])
    with _client(base_url) as c:
        signed_in = c.post("/auth/login", data=csrf_form(c, {"email": email, "password": password}))
        assert signed_in.status_code == 302, (signed_in.status_code, signed_in.text[:300])
        assert signed_in.headers.get("location") == "/auth/change_password"
        r = c.post("/auth/change_password",
                   data=csrf_form(c, {"new_password": "Abcde-1", "confirm_password": "Abcde-1"}))
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert RULE_RE.search(r.text), r.text[:600]


# ---------------------------------------------------------------------------
# the lockout -- LAST in this module
# ---------------------------------------------------------------------------
def test_zz_the_lockout_engages_after_the_ninth_failure(base_url, session_scoped_extra_emails):
    """Five failures, then 429 + Retry-After for an attempt that comes too
    early; honouring 1, 2, 4 and 8 s admits the sixth to ninth; then even the
    correct password is refused."""
    import time
    _require_docker()
    acct = _new_account(session_scoped_extra_emails)
    for i in range(5):
        r = _login(base_url, acct["email"], f"wrong-{i}")
        assert r.status_code == 401, (i + 1, r.status_code)
    early = _login(base_url, acct["email"], "wrong-early")
    assert early.status_code == 429, early.status_code
    assert TOO_MANY in early.text
    assert early.headers.get("retry-after") == "1", early.headers
    for gap in (1, 2, 4, 8):
        time.sleep(gap + 0.3)
        r = _login(base_url, acct["email"], f"wrong-after-{gap}")
        assert r.status_code == 401, (gap, r.status_code)
    time.sleep(8.5)
    locked = _login(base_url, acct["email"], acct["password"])
    assert locked.status_code == 429, locked.status_code
    wait = int(locked.headers.get("retry-after", "0"))
    assert 800 <= wait <= 900, wait
