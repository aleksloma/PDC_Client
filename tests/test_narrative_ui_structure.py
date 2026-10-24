"""Structural pins on the /lab page for narrative-template answers
(docs/DIAGNOSTICS_SPEC.md A5.6 display, A2.2 markdown unescape).

* `renderAiRecord(msg, convId, rowIdx)` is the ONE renderer of a stored AI
  record: history reload, `_tryRecoverResponse` and `editAndRegenerate`
  (which now receives the persisted record as `record_view`) all call it.
* It renders the phase-1 narrative FIRST (`appendNarrativeMessage`), then the
  visuals, then the tables with their captions; a record without any of the
  new keys renders exactly as before.
* Narrative and caption text pass `renderMarkdown` (escape first), which
  turns the renderer's `\\*`, `\\_`, `\\-` escapes back into literal characters
  AFTER the HTML escape, protected from the bold rule.
* The live stream checks `data.narrative` and the tables partial BEFORE the
  chart-partial branch.
* dashboard.css has no generic `.hidden`, so the narrative slot carries its own.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASH = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "dashboard.css").read_text(encoding="utf-8")
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """The tests/test_frontend_cosmetics.py helper: declaration, `name =
    function`, or arrow form; braces matched naively; "" when absent."""
    m = re.search(
        rf"(function\s+{name}\s*\(|\b{name}\s*[:=]\s*(?:async\s+)?function\s*\(|"
        rf"\b{name}\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*=>)", src)
    if not m:
        return ""
    start = src.find("{", m.end())
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return src[start:]


def _calls(body: str, name: str) -> list[str]:
    """Every `name(...)` call in `body`, parentheses matched naively."""
    out = []
    for m in re.finditer(rf"\b{re.escape(name)}\(", body):
        depth, i = 0, m.end() - 1
        while i < len(body):
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
                if depth == 0:
                    out.append(body[m.start():i + 1])
                    break
            i += 1
    return out


def _lang_block(lang: str) -> str:
    starts = {k: I18N.index(f"\n  {k}: {{") for k in ("en", "geo", "ru")}
    order = sorted(starts, key=starts.get)
    i = order.index(lang)
    end = starts[order[i + 1]] if i + 1 < len(order) else len(I18N)
    return I18N[starts[lang]:end]


def test_helpers_find_what_they_look_for():
    """Self-test of the scanners, so a green run cannot be a blind one."""
    assert _function_body(DASH, "renderMarkdown")
    assert _function_body(DASH, "openConversation")
    assert _function_body(DASH, "sendMessage")
    assert len(_calls("a(b(c), d) a(e)", "a")) == 2
    assert "'lab.refresh_no_access'" in _lang_block("geo")


def test_render_ai_record_shared_by_reload_recover_edit():
    rec = _function_body(DASH, "renderAiRecord")
    assert rec, "renderAiRecord(msg, convId, rowIdx) is missing"
    assert re.search(r"function\s+renderAiRecord\s*\(\s*msg\s*,\s*convId\s*,\s*rowIdx\s*\)", DASH)

    reload_ = _function_body(DASH, "openConversation")
    assert re.search(r"renderAiRecord\(\s*msg\s*,\s*convId\s*,\s*rowIdx\s*\)", reload_), \
        "the history reload loop must render AI rows through renderAiRecord"

    recover = _function_body(DASH, "_tryRecoverResponse")
    calls = _calls(recover, "renderAiRecord")
    assert calls and any("history.length - 1" in c for c in calls), calls

    edit = _function_body(DASH, "editAndRegenerate")
    calls = _calls(edit, "renderAiRecord")
    assert calls and any("record_view" in c and "ai_index" in c for c in calls), calls

    # The per-image loop lives in renderAiRecord only, not in its callers.
    for body in (reload_, recover, edit):
        assert "img.answer" not in body


def test_narrative_rendered_before_visuals():
    rec = _function_body(DASH, "renderAiRecord")
    assert rec
    narr = rec.find("appendNarrativeMessage(")
    assert narr >= 0, "renderAiRecord never renders the narrative"
    assert "narrative_rendered" in rec[narr:narr + 120]
    first_visual = rec.find("appendMessage(")
    assert first_visual >= 0
    assert narr < first_visual, "the narrative must come before the charts/tables"


def test_narrative_and_caption_through_render_markdown():
    narr = _function_body(DASH, "appendNarrativeMessage")
    assert narr, "appendNarrativeMessage(text, phase) is missing"
    assert "pdc-narrative" in narr
    assert re.search(r"innerHTML\s*=\s*renderMarkdown\(", narr), narr[:600]
    assert not re.search(r"innerHTML\s*=\s*text\b", narr)

    msg = _function_body(DASH, "appendMessage")
    assert "tableCaptions" in msg
    i = msg.find("pdc-caption")
    assert i >= 0, "appendMessage renders no .pdc-caption"
    window = msg[max(0, i - 400):i + 400]
    assert "renderMarkdown(" in window, window
    assert not re.search(r"innerHTML\s*=\s*(caption|cap)\b", window), window


def test_old_record_path_unchanged():
    """A record without narrative fields: each chart is `appendMessage(
    'assistant', img.answer || '', img.image_base64, ...)` with its stored-row
    reference, legacy joined code split per chart, and a single visual shows
    `msg.content` — unless the record carries narrative fields."""
    rec = _function_body(DASH, "renderAiRecord")
    assert rec
    assert "appendMessage('assistant', img.answer || '', img.image_base64" in rec
    assert "_splitJoinedCode(msg.code)" in rec
    assert "chartRef: { conv_id: convId, ai_index: rowIdx, image_index: idx }" in rec
    assert "chartRef: { conv_id: convId, ai_index: rowIdx, image_index: 0 }" in rec
    assert "msg.narrative_rendered !== undefined" in rec
    assert "msg.content" in rec and "msg.value_display" in rec
    assert "msg.tables" in rec and "msg.full_table_keys" in rec
    # The welcome-with-questions special case stays in the reload loop.
    assert "appendWelcomeMessageWithQuestions" in _function_body(DASH, "openConversation")


def test_stream_checks_narrative_before_partial():
    send = _function_body(DASH, "sendMessage")
    assert send
    chart_branch = re.search(r"if\s*\(\s*data\.partial\s*\)", send)
    assert chart_branch, "the chart-partial branch is gone"
    narr = re.search(r"if\s*\(\s*data\.narrative\b", send)
    assert narr, "sendMessage never handles data.narrative"
    tables = re.search(r"data\.partial\s*&&\s*!data\.image_base64\s*&&\s*data\.tables", send)
    assert tables, "sendMessage never recognises a tables partial"
    assert narr.start() < chart_branch.start()
    assert tables.start() < chart_branch.start()


def test_render_markdown_unescapes_literals():
    body = _function_body(DASH, "renderMarkdown")
    assert body
    esc = body.find("escapeHtml(text)")
    bold = body.find("<strong>")
    assert 0 <= esc < bold
    # JS source spelling of the two-character escape `\*`, `\_`, `\-`:
    # `\\*` in a string, `\\\*` in a regex literal.
    hits = {}
    for ch in ("*", "_", "-"):
        found = [m.start() for m in re.finditer(r"\\\\\\?" + re.escape(ch), body)]
        assert found, f"renderMarkdown never handles the escaped {ch!r}"
        assert all(i > esc for i in found), f"\\{ch} handled before the HTML escape"
        hits[ch] = found
    # Placeholder protection: the escaped asterisk is taken out BEFORE the
    # bold rule runs, so `\*x\*` never becomes <strong>.
    assert min(hits["*"]) < bold, "escaped asterisks are not protected from the bold rule"


def test_hidden_rule_for_narrative_slot():
    assert re.search(r"\.pdc-narrative\.hidden\s*\{[^}]*display\s*:\s*none", CSS), \
        "dashboard.css has no generic .hidden: the narrative slot needs its own rule"
    assert re.search(r"\.pdc-caption\s*[{,]", CSS)
    assert re.search(r"\.pdc-narrative\s+\.message-content\s*[{,]", CSS)


def test_render_ai_record_keys_on_template_presence():
    """Review finding 1: the single-visual text is chosen on the PRESENCE of
    `narrative_template` / `caption_template`, so a template that rendered to
    "" never falls back to `msg.content` (the marker + raw template)."""
    rec = _function_body(DASH, "renderAiRecord")
    assert rec
    assert "msg.narrative_template" in rec, "renderAiRecord ignores narrative_template"
    assert "msg.caption_template" in rec, "renderAiRecord ignores caption_template"
    i_tpl = rec.find("msg.caption_template")
    i_content = rec.find("msg.content")
    assert 0 <= i_tpl < i_content, "the template check must come before the content fallback"


def test_empty_single_visual_is_not_appended():
    """Review finding 2: a narrated single answer with no caption, chart,
    table or tables (a scalar) appends no empty assistant bubble — on reload
    (renderAiRecord) and on the live single done (sendMessage)."""
    helper = _function_body(DASH, "_hasAnswerContent")
    assert helper, "_hasAnswerContent(text, image, table, tables) is missing"

    rec = _function_body(DASH, "renderAiRecord")
    single = [c for c in _calls(rec, "appendMessage") if "msg.image_base64" in c]
    assert len(single) == 1, single
    guard = [m.start() for m in re.finditer(r"_hasAnswerContent\(", rec)]
    assert guard and min(guard) < rec.find(single[0]), "renderAiRecord appends unguarded"

    send = _function_body(DASH, "sendMessage")
    narr = [c for c in _calls(send, "appendMessage") if "data.caption ||" in c]
    assert len(narr) == 1, narr
    at = send.find(narr[0])
    window = send[max(0, at - 500):at]
    assert "_hasAnswerContent(" in window, "the live narrative single done appends unguarded"


def test_edit_response_chart_data_key_reaches_render():
    """Review finding 4: the single visual of a record carrying a live
    `chart_data_key` (only the edit response's record_view does) keeps Show
    data; a stored row never carries one, so reload is unchanged."""
    rec = _function_body(DASH, "renderAiRecord")
    single = [c for c in _calls(rec, "appendMessage") if "msg.image_base64" in c]
    assert single and "chartDataKey: msg.chart_data_key" in single[0], single


def test_i18n_phase2_progress_key():
    for lang in ("en", "geo", "ru"):
        block = _lang_block(lang)
        m = re.search(r"""['"]lab\.phase2_progress['"]\s*:\s*(['"])(.+?)(?<!\\)\1""", block)
        assert m and m.group(2).strip(), f"lab.phase2_progress missing in {lang}"
