"""Personal connections — structural pins on the Data-sources page (task
section 2.8). There is no JS test runner in this repo, so these are
text-level pins on the served template and script (the
tests/test_admin_ui_structure.py idiom): the read-only indicator in the
connection modal, the "Add connection" gate for self-service users, the
owner/"Personal" rendering, the wizard hiding role sharing and the connector
choice on a personal connection, the roles tree excluding personal sources,
the Users-window "Own connections" switch, and every new request going
through the JSON `api()` helper."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "templates" / "admin_data_sources.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "admin_data_sources.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "admin_data_sources.css").read_text(encoding="utf-8")
PARTIAL = (ROOT / "templates" / "partials" / "profile_button.html").read_text(encoding="utf-8")

NOTE = ("PowerDataChat only ever reads from a database. Writes are refused by "
        "the platform even when the login has write privileges.")


def _function_body(src: str, name: str) -> str:
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"function {name} not found"
    i = m.end()
    depth = 1
    while depth and i < len(src):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return src[m.end():i - 1]


def _conn_modal() -> str:
    start = HTML.index('id="connModal"')
    end = HTML.index('id="btnSaveConn"', start)
    return HTML[start:end]


def test_read_only_checkbox_checked_disabled_with_note_in_conn_modal():
    modal = _conn_modal()
    m = re.search(r"<input[^>]*\bid=\"connReadOnly\"[^>]*>", modal)
    assert m, "#connReadOnly missing from the connection modal"
    tag = m.group(0)
    assert re.search(r"\bchecked\b", tag) and re.search(r"\bdisabled\b", tag), tag
    assert "Read-only access" in modal
    assert NOTE in re.sub(r"\s+", " ", modal)


def test_conn_modal_is_outside_every_mode_block():
    """The modal renders in BOTH modes: no `manager_mode` block is open
    around it."""
    before = HTML[:HTML.index('id="connModal"')]
    opens = len(re.findall(r"{%\s*if\b", before))
    closes = len(re.findall(r"{%\s*endif\s*%}", before))
    assert opens == closes, "the connection modal sits inside a Jinja if-block"


def test_read_only_box_is_never_posted():
    assert "connReadOnly" not in _function_body(JS, "connFormBody")


def test_add_connection_gate_allows_self_service():
    m = re.search(r"{%\s*if ([^%]*)%}\s*<button id=\"btnAddConnection\"", HTML)
    assert m, "btnAddConnection is not behind a Jinja condition"
    cond = m.group(1)
    assert "manager_mode != 'power'" in cond and "self_service" in cond, cond
    assert "window.__SELF_SERVICE__" in HTML


def test_connection_rows_render_personal_chip_owner_and_owned_actions():
    body = _function_body(JS, "renderConnections")
    assert "adm-chip personal" in body
    assert "Owner:" in body
    assert "c.owned" in body and "c.personal" in body
    assert ".adm-chip.personal" in CSS


def test_tables_list_gives_admin_no_actions_on_personal_tables():
    body = _function_body(JS, "renderTables")
    assert "_isPersonalConn" in body
    assert "Owner:" in body


def test_wizard_hides_sharing_and_connector_on_personal_connection():
    apply_body = _function_body(JS, "_applyPersonalWizard")
    assert "twAccessWrap" in apply_body and "twIsConnector" in apply_body
    assert "_applyPersonalWizard" in _function_body(JS, "setWizardStep")
    save = _function_body(JS, "saveTable")
    assert "_isPersonalConn(wizardConnId)" in save
    assert re.search(r"is_connector:\s*personal\s*\?\s*false", save), save[:400]
    assert re.search(r"_wizAccessReady\s*&&\s*!personal", save)


def test_recommendation_accept_posts_normal_on_personal_connection():
    run = _function_body(JS, "_runAcceptRecommendation")
    assert "_isPersonalConn" in run and "'normal'" in run


def test_roles_tree_excludes_personal_sources():
    body = _function_body(JS, "renderRoleTree")
    assert "!c.personal" in body
    assert "_isPersonalConn" in body


def test_users_window_own_connections_switch_posts_json():
    body = _function_body(JS, "renderUsers")
    assert "Own connections" in body
    assert "adm-user-selfsvc" in body
    assert "Administrators manage data sources from this page" in body
    m = re.search(r"api\(\s*'/api/admin/users/set_self_service'\s*,\s*\{([^}]*)\}", body)
    assert m, "the switch must POST through api()"
    assert "method: 'POST'" in m.group(1) and "JSON.stringify" in m.group(1)
    assert "allowed" in m.group(1)


def test_new_requests_go_through_api_helper_only():
    """api() sets the JSON content type; the page keeps exactly one bare
    fetch (logout) and no new one."""
    assert len(re.findall(r"(?<![\w.])fetch\(", JS)) == len(
        re.findall(r"(?<![\w.])fetch\(", _function_body(JS, "api"))) + 1
    assert "'Content-Type': 'application/json'" in _function_body(JS, "api") or \
        '"Content-Type": "application/json"' in _function_body(JS, "api")


def test_template_has_no_inline_handlers():
    assert not re.search(r"\son[a-z]+\s*=", HTML)


def test_profile_partial_shows_db_config_for_self_service():
    m = re.search(r"{%\s*if ([^%]*)%}\s*{#[^#]*#}\s*<button class=\"dropdown-item\" id=\"btnDbConfig\"",
                  PARTIAL, re.S)
    assert m, PARTIAL[:600]
    cond = m.group(1)
    assert "is_power_user" in cond and "is_admin_user" in cond and "is_self_service" in cond
