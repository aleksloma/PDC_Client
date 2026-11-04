"""/power/data_sources carries the chat page's profile button (the shared
`partials/profile_button.html`) in a top bar instead of the sidebar footer
controls (2026-11-04 follow-up on personal connections): Back to chat, Change
Password and Logout exist ONCE on the power page; admin mode is untouched.

The admin page is pinned BYTE-IDENTICAL to the render taken before the change
through the golden fixture `tests/fixtures/admin_data_sources_admin_mode.html`
(a fixed context, no request-dependent value). Regenerate it ONLY when the
admin page changes on purpose:

    .venv\\Scripts\\python -c "from tests.test_power_page_profile_menu import regenerate; regenerate()"

The partial's DEFAULT rendering (no flag — what /lab and /dashboards/{id}
include) is pinned the same way by `tests/fixtures/profile_button_power_default.html`.

Uses the REAL app WITHOUT the TestClient context manager (no lifespan, no
scheduler thread — the test_power_user_pages idiom)."""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
from settings import settings
from tests.conftest import csrf_form

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "admin_data_sources_admin_mode.html"
TEMPLATE = (ROOT / "templates" / "admin_data_sources.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "admin_data_sources.js").read_text(encoding="utf-8")
ADMIN_CSS = (ROOT / "static" / "admin_data_sources.css").read_text(encoding="utf-8")
MAIN_CSS = (ROOT / "static" / "main.css").read_text(encoding="utf-8")

POWER = "power@x.com"
ADMIN2 = "admin2@x.com"
PW = "secret-pw"

# The fixed admin-mode context of the golden render: a PROMOTED admin
# (back_to_chat True), every request-dependent value pinned.
GOLDEN_CTX = {
    "request": SimpleNamespace(state=SimpleNamespace(csp_nonce="nonce")),
    "ts": 0, "username": ADMIN2, "build_stamp": "build test",
    "manager_mode": "admin", "back_to_chat": True,
    "subscription_plan": "Enterprise",
    "logged_in": True, "display_name": ADMIN2, "raw_username": ADMIN2,
    "avatar_color": "#2563eb", "initials": "AD",
    "is_power_user": False, "is_admin_user": True, "is_self_service": False,
}


def render_admin_mode() -> str:
    return app_mod.templates.env.get_template("admin_data_sources.html").render(GOLDEN_CTX)


def regenerate() -> None:
    FIXTURE.write_text(render_admin_mode(), encoding="utf-8", newline="")
    print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size} bytes)")


def _render_partial(**extra) -> str:
    ctx = {"logged_in": True, "display_name": POWER, "raw_username": POWER,
           "subscription_plan": "Enterprise", "avatar_color": "#2563eb",
           "initials": "PO", "is_power_user": True, "is_admin_user": False,
           "is_self_service": False}
    ctx.update(extra)
    return app_mod.templates.env.get_template("partials/profile_button.html").render(ctx)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    import routes.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    auth = local_store.AuthStore()
    for email in (POWER, ADMIN2):
        auth.ensure_user(email)
        auth.set_password(email, PW)
    auth.set_role(ADMIN2, "admin")
    auth.set_role(POWER, "power")
    return TestClient(app_mod.app, base_url="https://testserver")


def _login(client, email):
    r = client.post("/auth/login", data=csrf_form(client, {"email": email, "password": PW}),
                    follow_redirects=False)
    assert r.status_code in (302, 303), (r.status_code, r.text)


def test_admin_page_byte_identical_to_golden():
    assert FIXTURE.exists(), "golden fixture missing — see the module docstring"
    # Line endings normalised on both sides: git may check the template and
    # the fixture out with CRLF on Windows and LF on CI; everything else is
    # compared byte for byte.
    expected = FIXTURE.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert render_admin_mode().replace("\r\n", "\n") == expected


def test_power_page_has_profile_button_once_and_no_footer_controls(client):
    _login(client, POWER)
    r = client.get("/power/data_sources")
    assert r.status_code == 200
    html = r.text
    assert 'class="adm-topbar"' in html
    assert html.count('id="btnProfileIcon"') == 1
    assert 'id="profileDropdown"' in html
    assert re.search(r'id="btnBackToChat"[^>]*data-target="/lab"', html)
    assert 'id="btnChangePassword"' in html
    assert 'id="btnLogout"' in html
    assert html.count("Back to chat") == 1
    # The sidebar footer keeps only the build stamp.
    assert 'class="adm-build-stamp"' in html
    for gone in ("btnAdmLogout", "btnAdmChangePw", "adm-user-avatar", "adm-back-link", "btnDbConfig"):
        assert gone not in html, gone
    # The change-password modal the dropdown opens is still on the page.
    assert 'id="pwModal"' in html


def test_admin_page_keeps_footer_and_has_no_profile_button(client):
    _login(client, ADMIN2)
    r = client.get("/admin/data_sources")
    assert r.status_code == 200
    html = r.text
    for kept in ('id="btnAdmLogout"', 'id="btnAdmChangePw"', "adm-back-link", "adm-user-avatar"):
        assert kept in html, kept
    for absent in ("btnProfileIcon", "adm-topbar", "btnBackToChat", "profileDropdown"):
        assert absent not in html, absent


PARTIAL_FIXTURE = ROOT / "tests" / "fixtures" / "profile_button_power_default.html"


def test_partial_default_rendering_unchanged(client):
    plain = _render_partial()
    assert 'id="btnDbConfig"' in plain and "btnBackToChat" not in plain
    # Byte-identical to the pre-change partial for a power user (the new
    # tags are whitespace-trimmed): /lab and /dashboards/{id} serve the same
    # markup as before. The fixture was rendered from the HEAD partial with
    # _render_partial()'s default context.
    expected = PARTIAL_FIXTURE.read_bytes().decode("utf-8").replace(chr(13) + chr(10), chr(10))
    assert plain.replace(chr(13) + chr(10), chr(10)) == expected
    flagged = _render_partial(profile_menu_back_to_chat=True)
    assert 'id="btnBackToChat"' in flagged and "btnDbConfig" not in flagged
    for item in ('id="btnChangePassword"', 'id="btnLogout"', 'id="btnProfileIcon"'):
        assert item in flagged, item
    # /lab is served by the default rendering.
    _login(client, POWER)
    r = client.get("/lab")
    assert r.status_code == 200
    assert 'id="btnDbConfig"' in r.text and "btnBackToChat" not in r.text


def test_admin_js_binds_footer_optionally_and_profile_menu():
    # The footer buttons are absent in power mode: a bare bind would throw
    # inside DOMContentLoaded and loadAll() would never run.
    assert "$('btnAdmLogout')?.addEventListener('click', logout)" in JS
    assert "$('btnAdmChangePw')?.addEventListener('click', openPwModal)" in JS
    assert "$('btnAdmLogout').addEventListener" not in JS
    assert "$('btnAdmChangePw').addEventListener" not in JS
    m = re.search(r"const profBtn = \$\('btnProfileIcon'\);(.*?)\n    \}\n", JS, re.S)
    assert m, "profile-menu wiring block missing"
    block = m.group(1)
    assert "$('profileDropdown')" in block
    assert "profDrop.classList.toggle('hidden')" in block
    assert ".closest('.profile-dropdown-container')" in block
    assert re.search(r"\$\('btnBackToChat'\)\?\.addEventListener\('click'", block)
    assert "dataset.target || '/lab'" in block
    assert re.search(r"\$\('btnChangePassword'\)\?\.addEventListener\('click'", block)
    assert "openPwModal()" in block
    assert "$('btnLogout')?.addEventListener('click', logout)" in block


def test_admin_css_mirrors_main_css_profile_rules():
    start = MAIN_CSS.index("/* Profile Dropdown (shared with dashboard) */")
    end = MAIN_CSS.index("/* ══", start)
    shared = MAIN_CSS[start:end].rstrip()
    assert ".profile-btn {" in shared and ".profile-dropdown .dropdown-item {" in shared
    assert shared in ADMIN_CSS, "admin_data_sources.css must carry main.css's profile block verbatim"
    assert ".adm-topbar {" in ADMIN_CSS
