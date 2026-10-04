"""The Create New Chat / Add Data wizard has no "Share Google Sheet" control
(CLIENT_FIX 2): /upload_from_url answers 400 on-prem, so the button and its
URL box could never work. The wizard offers "Choose Files" and "Select from
DB" only, and the "or" separators never double up. The route itself stays
(400) — pinned in tests/test_upload_no_gcs_branch.py."""
import re
from pathlib import Path

from test_rendering_isolation import _function_body

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "dashboard.html"
JS = ROOT / "static" / "dashboard.js"
CSS = ROOT / "static" / "dashboard.css"
I18N = ROOT / "static" / "i18n.js"

GONE_IDS = ("shareGoogleSheetBtn", "googleSheetInputBox", "googleUrlInput",
            "importFromUrlBtn", "cancelGoogleSheetBtn", "urlImportStatus")
GONE_KEYS = ("wizard.share_google_sheet", "wizard.google_warning",
             "wizard.google_placeholder", "wizard.share_btn", "wizard.cancel_btn")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def _upload_options(html: str) -> str:
    start = html.index('<div class="upload-options">')
    end = html.index('<div id="selectedFilesList"', start)
    return html[start:end]


def test_the_template_has_no_google_sheet_control():
    html = _read(TEMPLATE)
    for element_id in GONE_IDS:
        assert element_id not in html, element_id
    assert "Google Sheet" not in html
    assert "google-btn" not in html and "google-input-box" not in html


def test_the_wizard_offers_choose_files_and_select_from_db_only():
    block = _upload_options(_read(TEMPLATE))
    assert 'data-i18n="wizard.choose_files"' in block
    assert 'id="dbSelectBtn"' in block
    buttons = re.findall(r'<button\b[^>]*\bid="([^"]+)"', block)
    assert buttons == ["dbSelectBtn"], buttons


def test_the_or_separators_never_double_up():
    """Choose Files · or (shown with the DB control) · Select from DB · or drop
    here — and "Choose Files · or drop here" while the DB control is hidden."""
    block = _upload_options(_read(TEMPLATE))
    dividers = re.findall(r'<span[^>]*class="upload-divider[^"]*"[^>]*>', block)
    assert len(dividers) == 2, dividers
    assert 'id="dbSelectDivider"' in dividers[0], "the first 'or' must hide with the DB control"
    assert 'data-i18n="wizard.or_drop"' in dividers[1]
    assert (block.index('data-i18n="wizard.choose_files"')
            < block.index('id="dbSelectDivider"')
            < block.index('id="dbSelectWrap"')
            < block.index('data-i18n="wizard.or_drop"'))


def test_the_script_has_no_google_sheet_handler():
    js = _read(JS)
    for element_id in GONE_IDS:
        assert element_id not in js, element_id
    for name in ("importFromGoogleUrl", "_isGoogleSheet", "skipUploadIfEmpty",
                 "/upload_from_url"):
        assert name not in js, name


def test_db_only_selection_still_reaches_the_flow():
    body = _function_body(_read(JS), "runFrictionlessFlow")
    assert "filesArr.length === 0 && dbTableIds.length === 0" in body


def test_the_google_sheet_strings_and_styles_are_gone():
    src = _read(I18N)
    for key in GONE_KEYS:
        assert f"'{key}':" not in src, key
    assert src.count("'wizard.or':") == 3
    assert src.count("'wizard.or_drop':") == 3
    css = _read(CSS)
    for selector in (".google-btn", ".google-input-box", ".google-warning",
                     ".google-input-row", ".selected-file-row.google-sheet"):
        assert selector not in css, selector
