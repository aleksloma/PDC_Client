"""The enterprise `/lab` page loads nothing from a third-party origin.

WHY: `templates/dashboard.html` is a verbatim copy of the B2C page, so it
still pulls Google Analytics (`googletagmanager.com`) and the Paddle
checkout SDK (`cdn.paddle.com`) on every render. On-prem that is a data-flow
nobody signed off on — an air-gapped or egress-filtered network watching
outbound traffic sees the browser
of every analyst calling Google — and both are dead weight here: there is no
billing backend on-prem (`/paddle/config` does not even exist as a route).

The fix is a GATE, not a deletion: `settings.ENABLE_THIRD_PARTY_SCRIPTS`
(default False) so the demo deployment can keep its analytics. Hence the two
directions below: default renders NEITHER block, the flag ON renders BOTH.
The Paddle gate has to cover the inline initializer too — it calls
`Paddle.Initialize`, which would throw with the CDN script gone.

Both `dashboard.html` routes are exercised (`/lab` and the `/c/{conv_id}`
deep link) because each builds its own context dict, and a gate added to one
of them only would ship half the fix.

Offline: `DATA_ROOT` is `tmp_path`, the login path's brain calls are stubbed,
the TestClient is built WITHOUT the context manager (the lifespan starts the
db_scheduler thread), https base_url because the session cookie is
Secure-flagged by default. Every asserted value is bound to a local first.
"""
import pytest
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
from settings import settings

EMAIL = "third-party@x.com"
PASSWORD = "third-party-pw-123"
CHAT_ID = "chat_third_party"
CONV_ID = "cv_00112233445566aa"

# A stable marker from dashboard.html: the page's own body must survive both
# states, so a gate can never be confused with a broken render.
PAGE_MARKER = 'id="chatInput"'

# One needle per line so a failure names the exact leak.
GA_NEEDLES = ["googletagmanager", "google-analytics", "gtag("]
PADDLE_NEEDLES = ["cdn.paddle.com", "Paddle.Initialize", "/paddle/config"]
THIRD_PARTY_NEEDLES = GA_NEEDLES + PADDLE_NEEDLES

# The two that must come BACK when the flag is on (proving a gate, not a
# deletion): one per vendor block.
GATED_BACK_NEEDLES = ["googletagmanager", "cdn.paddle.com"]

PAGE_PATHS = ["/lab", f"/c/{CONV_ID}"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    local_store._DATAFRAME_CACHE.invalidate()
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    auth = local_store.AuthStore()
    auth.ensure_user(EMAIL)
    auth.set_password(EMAIL, PASSWORD)
    # The deep-link route resolves the conv through the caller's own
    # conversations index; without a row it redirects to /lab and the second
    # TemplateResponse site would never render.
    auth.record_conversation(EMAIL, CHAT_ID, CONV_ID, "Third-party check")
    tc = TestClient(app_mod.app, base_url="https://testserver")
    login = tc.post("/auth/login", data={"email": EMAIL, "password": PASSWORD},
                    follow_redirects=False)
    assert login.status_code == 302, (login.status_code, login.text[:300])
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


def _set_flag(monkeypatch, value) -> None:
    """Explicit `hasattr` first: `Settings` is a pydantic model and assigning
    an undeclared field raises a ValidationError whose text would bury the
    real reason the test is red."""
    assert hasattr(settings, "ENABLE_THIRD_PARTY_SCRIPTS"), (
        "settings must define ENABLE_THIRD_PARTY_SCRIPTS (bool, default "
        "False) — the dashboard.html gate for Google Analytics and Paddle")
    monkeypatch.setattr(settings, "ENABLE_THIRD_PARTY_SCRIPTS", value, raising=False)


def _page(client, path) -> str:
    response = client.get(path, follow_redirects=False)
    status = response.status_code
    assert status == 200, (path, status, response.text[:300])
    text = response.text
    assert PAGE_MARKER in text, (path, text[:300])
    return text


@pytest.mark.parametrize("path", PAGE_PATHS)
@pytest.mark.parametrize("needle", THIRD_PARTY_NEEDLES)
def test_default_render_loads_no_third_party_script(client, path, needle):
    """DEFAULT state: the page must not name any external analytics or
    billing origin. One test per needle so a failure names it."""
    text = _page(client, path)
    assert needle not in text, (path, needle)


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_the_page_still_renders_its_own_markup_by_default(client, path):
    """The gate removes the vendor blocks, not the page."""
    text = _page(client, path)
    assert PAGE_MARKER in text, (path, text[:300])
    assert "PowerDataChat" in text, (path, text[:300])


@pytest.mark.parametrize("path", PAGE_PATHS)
@pytest.mark.parametrize("needle", GATED_BACK_NEEDLES)
def test_the_flag_brings_both_vendor_blocks_back(client, monkeypatch, path, needle):
    """Proves a GATE rather than a deletion: with the setting True the same
    route serves the GA tag and the Paddle SDK again."""
    _set_flag(monkeypatch, True)
    text = _page(client, path)
    assert needle in text, (path, needle, text[:300])


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_the_page_still_renders_its_own_markup_with_the_flag_on(client, monkeypatch, path):
    _set_flag(monkeypatch, True)
    text = _page(client, path)
    assert PAGE_MARKER in text, (path, text[:300])


def test_the_setting_defaults_to_false_on_a_fresh_settings_object(monkeypatch):
    """A customer install must be quiet without setting anything."""
    from settings import Settings

    monkeypatch.delenv("ENABLE_THIRD_PARTY_SCRIPTS", raising=False)
    value = getattr(Settings(), "ENABLE_THIRD_PARTY_SCRIPTS", None)  # local: no Settings repr
    assert value is False


@pytest.mark.parametrize("env,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("", False), ("nonsense", False),
])
def test_the_setting_parses_like_client_llm_debug(monkeypatch, env, expected):
    """Same bool idiom as `CLIENT_LLM_DEBUG` — no new parsing rules."""
    from settings import Settings

    monkeypatch.setenv("ENABLE_THIRD_PARTY_SCRIPTS", env)
    value = getattr(Settings(), "ENABLE_THIRD_PARTY_SCRIPTS", None)  # local: no Settings repr
    assert value is expected
