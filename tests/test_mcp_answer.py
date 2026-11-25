"""MCP stage 4 — the content of an answer (`mcp_answer`).

    build_content(record, *, chat_id, conv_id, email, sid, saved=True) -> list[dict]
    error_content(text) -> list[dict]
    MAX_IMAGES 6, RENDER_TIMEOUT_S 60, MAX_STUCK_RENDERS 3

A saved AI record -> MCP content items: `{"type": "text", "text"}` and
`{"type": "image", "data": <base64>, "mimeType": "image/png"}`. DISPLAY text
only, in the order static/dashboard.js `renderAiRecord` draws a record.

Pinned readings of the prompt:
* how the text is split into items is free; only the sequence of texts and
  images counts. A chart's caption stands above its image, a table's caption
  and title above the table;
* the record is the one in MEMORY, not yet passed through the JSON of the
  history file: its cells can be pandas / numpy values, its text can hold a
  lone surrogate; every item must be JSON- and UTF-8-encodable;
* the PRESENCE of a template decides, never the truthiness of its rendering:
  a narrative or caption rendered to "" shows nothing, never the raw content;
* a stopped record always says "Response stopped by user." exactly once, also
  where the web page shows only its charts;
* a table cell is readable after the Markdown escapes are undone and can never
  break its row; a raw HTML tag in it is not left active;
* a number need not look like on the web, but is right to two decimals,
  never in exponent form; an identifier is its plain digits;
* the render timeout counts from the start of the render, not from the moment
  it was queued behind other answers; renders run one at a time; a stuck
  render thread is a daemon (it must not keep the process alive), at most
  MAX_STUCK_RENDERS of them exist, and rendering resumes when one comes back;
* `saved=False` (the turn could not be stored): no link and no place to look,
  anywhere in the answer; one fixed sentence says so instead;
* nothing of the record is changed, run again, or sent anywhere.

Offline: DATA_ROOT is tmp_path, the renderer is a fake (never kaleido).
"""
import base64
import copy
import datetime as dt
import decimal
import html
import importlib
import inspect
import json
import logging
import re
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

import brain_client
import local_store
import logger_utils
import narrative
from settings import settings

ADMIN = "ladmin"
OWNER = "owner@acme.com"
FRIEND = "friend@acme.com"
CHAT = "c_mcpanswer000001"
CONV = "cv_0123456789abcdef"
CONV_TITLE = "MCP 2026-11-25"
BASE = "https://pdc.bank.example"
GENERIC = "Something went wrong while processing your request."
STOP = "Response stopped by user."
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "history_pre_narrative.jsonl"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
assert PNG.startswith(PNG_MAGIC)

CODE = "RESULT = dfs['sales.csv'].CODESENTINEL()"
T = ("Revenue was {{total:count}} in {{region}}."
     "{{#anomaly}} One month stands out.{{/anomaly}}")
V = {"total": 1234567, "region": "North"}
F = {"anomaly": True, "coverage_dim": "region"}
R = narrative.render(T, V, F, sid="test")
NCONTENT = narrative.history_content(T, F)
CAP = "Top region is {{region}}."
CAP_R = "Top region is North."


def png(label):
    """A real PNG that says which chart it is."""
    return PNG + b"#" + str(label).encode()


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


def plotly(label):
    return f"<div>plotly chart={label}=</div>"


# ===========================================================================
# the world
# ===========================================================================
@pytest.fixture
def logs():
    class _Capture(logging.Handler):
        def emit(self, record):
            self.lines.append(record.getMessage())

    logger = logger_utils.get_logger()
    handler = _Capture(level=logging.DEBUG)
    handler.lines = []
    old_level = logger.level
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.fixture
def world(tmp_path, monkeypatch):
    assert "MCP_TABLE_MAX_ROWS" in type(settings).model_fields, \
        "settings must define MCP_TABLE_MAX_ROWS"
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", ADMIN)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(settings, "MCP_TABLE_MAX_ROWS", 50)
    local_store._DATAFRAME_CACHE.invalidate()
    auth = local_store.AuthStore()
    for email in (OWNER, FRIEND):
        auth.ensure_user(email)
    store = local_store.ChatDataStore(CHAT)
    pd.DataFrame({"region": ["North", "South"], "amount": [1, 2]}).to_csv(
        store.files_dir / "sales.csv", index=False)
    meta = store.read_meta()
    meta.update({"owner": OWNER, "title": "Sales",
                 "files": [{"file_name": "sales.csv", "file_description": "sales",
                            "schema": {"fields": {"region": {}, "amount": {}}}}],
                 "sharing": {"shared_with": [FRIEND]}})
    store.write_meta(meta)
    auth.record_active_chat(OWNER, CHAT, "Sales", ["sales.csv"])
    auth.record_conversation(OWNER, CHAT, CONV, CONV_TITLE)
    yield store
    local_store._DATAFRAME_CACHE.invalidate()


class Renderer:
    """`routes.report._resolve_chart_image` replaced. A Plotly document
    renders to the PNG of its label, anything else passes through."""

    def __init__(self):
        self.calls, self.threads = [], []
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.block = {}             # label -> Event held by that render
        self.delay = 0.0
        self.raises = None
        self.answer = None          # label -> the (image, flag) returned

    def __call__(self, image_b64, sid):
        with self.lock:
            self.calls.append((image_b64, sid))
            self.threads.append(threading.current_thread())
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            m = re.search(r"chart=(.*?)=<", str(image_b64))
            label = m.group(1) if m else None
            gate = self.block.get(label)
            if gate is not None:
                assert gate.wait(60)
            if self.delay:
                time.sleep(self.delay)
            if self.raises is not None:
                raise self.raises
            if self.answer is not None:
                return self.answer(label)
            return (b64(png(label)), True) if m else (image_b64, True)
        finally:
            with self.lock:
                self.active -= 1

    def labels(self):
        return [re.search(r"chart=(.*?)=<", c[0]).group(1) for c in self.calls]


@pytest.fixture
def ans(world):
    return importlib.import_module("mcp_answer")


@pytest.fixture
def renderer(ans, monkeypatch):
    r = Renderer()
    report = importlib.import_module("routes.report")
    monkeypatch.setattr(report, "_resolve_chart_image", r)
    # never the real one, whatever the module calls
    monkeypatch.setattr(report, "_plotly_html_to_png",
                        lambda *a, **k: pytest.fail("a real chart render"))
    yield r
    for gate in r.block.values():
        gate.set()
    _until(lambda: r.active == 0, "the held renders to end")
    if r.block:
        time.sleep(0.1)


@pytest.fixture
def chat_mod():
    return importlib.import_module("routes.chat")


def _until(predicate, what, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out: {what}")


# ===========================================================================
# reading the items
# ===========================================================================
def check(items):
    """What holds for every answer."""
    assert isinstance(items, list) and items, items
    for item in items:
        assert isinstance(item, dict), item
        if item.get("type") == "text":
            assert set(item) == {"type", "text"}, item
            assert isinstance(item["text"], str) and item["text"].strip(), item
        else:
            assert set(item) == {"type", "data", "mimeType"}, set(item)
            assert item["type"] == "image" and item["mimeType"] == "image/png"
            raw = base64.b64decode(item["data"], validate=True)
            assert raw.startswith(PNG_MAGIC), raw[:20]
    json.dumps(items, allow_nan=False, ensure_ascii=False).encode("utf-8")
    out = text_of(items)
    assert "[Findings" not in out, out
    assert "{{" not in out and "}}" not in out, out
    assert "CODESENTINEL" not in out, "the code is not part of an answer"
    assert "Traceback" not in out
    return items


def text_of(items):
    return "\n\n".join(i["text"] for i in items if i["type"] == "text")


def images(items):
    """The labels of the images, in order."""
    out = []
    for i in items:
        if i["type"] == "image":
            raw = base64.b64decode(i["data"])
            out.append(raw[len(PNG) + 1:].decode() if raw.startswith(PNG + b"#") else "?")
    return out


def sequence(items):
    """The whole answer as one string, an image as «label»."""
    labels = iter(images(items))
    return "\n\n".join(i["text"] if i["type"] == "text" else f"«{next(labels)}»" for i in items)


def in_order(items, *parts):
    seq, at = sequence(items), -1
    for p in parts:
        nxt = seq.find(p, at + 1)
        assert nxt > at, (p, seq)
        at = nxt


def last_text(items):
    return [i["text"] for i in items if i["type"] == "text"][-1]


def build(ans, record, **kw):
    args = {"chat_id": CHAT, "conv_id": CONV, "email": OWNER, "sid": "sid-1"}
    args.update(kw)
    return check(ans.build_content(record, **args))


def once(items, part):
    out = text_of(items)
    assert out.count(part) == 1, (part, out)


SEPARATOR = re.compile(r"^\|(\s*:?-{3,}:?\s*\|)+\s*$")


def split_row(line):
    """The raw cells of a Markdown row; `\\x` is one escaped character."""
    line = line.strip()
    assert line.startswith("|") and line.endswith("|"), line
    cells, cur, i = [], "", 1
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur += line[i:i + 2]
            i += 2
            continue
        if ch == "|":
            cells.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    assert cur == "", f"the row does not end with a bare pipe: {line!r}"
    return cells


def tables(items):
    """Every Markdown table as (header, rows) of RAW cells."""
    out = []
    for item in items:
        if item["type"] != "text":
            continue
        lines = re.split(r"\r\n|\r|\n", item["text"])
        for i in range(1, len(lines)):
            if not SEPARATOR.match(lines[i]) or not lines[i - 1].startswith("|"):
                continue
            if SEPARATOR.match(lines[i - 1]):
                continue
            assert i == 1 or not lines[i - 2].strip(), \
                f"no blank line above the table: {lines[i - 2]!r}"
            header = split_row(lines[i - 1])
            assert len(split_row(lines[i])) == len(header)
            rows = []
            for line in lines[i + 1:]:
                if not line.startswith("|"):
                    break
                rows.append(split_row(line))
            assert all(len(r) == len(header) for r in rows), (header, rows)
            out.append((header, rows))
    return out


def one_table(items):
    found = tables(items)
    assert len(found) == 1, (len(found), text_of(items))
    return found[0]


def plain(cell):
    """A raw cell read back."""
    cell = re.sub(r"<br\s*/?>", " ", cell)
    return html.unescape(re.sub(r"\\(.)", r"\1", cell))


def number(cell):
    raw = plain(cell)
    assert "e" not in raw.lower(), raw
    return float(raw.replace(",", ""))


def table(rows, columns=None, **extra):
    t = {"columns": columns or list(rows[0]), "rows": rows, "total_rows": len(rows)}
    t.update(extra)
    return t


# ===========================================================================
# records, through the real builders
# ===========================================================================
def single(chat_mod, text="the answer", **extra):
    res = {"text": text, "image_base64": None, "table": None, "code": CODE, "usage": {}}
    res.update(extra)
    return chat_mod._build_ai_history_record(None, res, None, [], {}, [])


def chart(label, answer="", **extra):
    c = {"image_base64": plotly(label), "answer": answer, "code": CODE, "phase": 1}
    c.update(extra)
    return c


def narr(text=R, template=T):
    return {"narrative_template": template, "narrative_rendered": text,
            "values": dict(V), "findings": dict(F)}


def multi(chat_mod, charts, combined, *, phase1=None, phase2=None, tables_=None,
          captions=None, templates=None, phases=None, headline=None):
    view = {"phase1": phase1, "phase2": phase2, "phase2_status": None}
    if headline:
        view["headline_display"] = headline
    src = {"narrative_view": view}
    if tables_ and captions is not None:
        src.update({"table_captions": captions,
                    "table_caption_templates": templates or [""] * len(tables_),
                    "table_phases": phases or [1] * len(tables_)})
    return chat_mod._build_ai_history_record(
        None, None, combined, [CODE], {}, charts, combined_tables=tables_,
        full_table_keys=["k"] * len(tables_) if tables_ else None, narrative_src=src)


T1 = table([{"region": "Alpha", "n": 1}])
T2 = table([{"region": "Beta", "n": 2}])


# ===========================================================================
# the module
# ===========================================================================
def test_the_constants_and_the_signatures(ans):
    assert (ans.MAX_IMAGES, ans.RENDER_TIMEOUT_S, ans.MAX_STUCK_RENDERS) == (6, 60, 3)
    params = inspect.signature(ans.build_content).parameters
    assert list(params) == ["record", "chat_id", "conv_id", "email", "sid", "saved"]
    assert params["saved"].default is True
    assert all(params[p].kind is inspect.Parameter.KEYWORD_ONLY
               for p in list(params)[1:])
    assert list(inspect.signature(ans.error_content).parameters) == ["text"]


def test_error_content_is_the_text_and_nothing_else(ans):
    text = "The analysis is taking longer than expected. Please try again."
    assert ans.error_content(text) == [{"type": "text", "text": text}]


@pytest.mark.parametrize("text", [None, "", "   ", 5, [], {"error": "x"}, "x \ud800 y"],
                         ids=ascii)
def test_error_content_is_always_an_encodable_text(ans, text):
    items = check(ans.error_content(text))
    assert [i["type"] for i in items] == ["text"]
    if not (isinstance(text, str) and text.strip()):
        assert items[0]["text"] == GENERIC


def test_no_record_is_the_error_text(ans):
    assert build(ans, None)[0] == {"type": "text", "text": GENERIC}
    assert images(build(ans, None)) == []


@pytest.mark.parametrize("record", [{}, [], "x", 5, True, {"role": "ai"},
                                    {"role": "ai", "content": None, "images": None}],
                         ids=ascii)
def test_an_odd_record_never_raises(ans, renderer, record):
    items = build(ans, record)
    assert images(items) == []


# ===========================================================================
# every record shape: texts
# ===========================================================================
def test_a_single_text(ans, chat_mod):
    once(build(ans, single(chat_mod, "There are 3 regions.")), "There are 3 regions.")


def test_a_scalar_with_its_local_value_line(ans, chat_mod):
    rec = single(chat_mod, "There are three regions.", value_display="**Result:** 3")
    items = build(ans, rec)
    in_order(items, "There are three regions.", "**Result:** 3")
    once(items, "**Result:** 3")


def test_a_dropped_narratives_headline_stands_first(ans, chat_mod):
    rec = single(chat_mod, "Sales by region.", headline_display="Total: 1.2 mln.",
                 table=table([{"a": "Alpha"}]))
    items = build(ans, rec)
    in_order(items, "Total: 1.2 mln.", "Sales by region.", "Alpha")
    once(items, "Total: 1.2 mln.")


def test_a_narrated_single_answer_shows_the_rendered_narrative_only(ans, chat_mod):
    items = build(ans, single(chat_mod, NCONTENT, **narr()))
    once(items, R)
    assert "region]" not in text_of(items), "a template view is not the answer"


def test_a_narrated_table_answer(ans, chat_mod):
    rec = single(chat_mod, NCONTENT, **narr(), table=T1, caption_template=CAP,
                 caption_rendered=CAP_R)
    in_order(build(ans, rec), R, CAP_R, "Alpha")


def test_a_narrated_answer_with_a_described_visual(ans, chat_mod, renderer):
    rec = single(chat_mod, NCONTENT, **narr(), image_base64=plotly("1"),
                 caption_described="Sales rose.")
    in_order(build(ans, rec), R, "Sales rose.", "«1»")


def test_a_caption_without_a_narrative(ans, chat_mod):
    items = build(ans, single(chat_mod, CAP, caption_template=CAP, caption_rendered=CAP_R,
                              table=T1))
    in_order(items, CAP_R, "Alpha")
    once(items, CAP_R)


@pytest.mark.parametrize("fields", [
    {"narrative_template": T, "narrative_rendered": ""},
    {"narrative_template": T},
    {"caption_template": CAP, "caption_rendered": ""},
    {"caption_template": CAP},
    {"narrative_template": T, "narrative_rendered": None, "caption_template": CAP,
     "caption_rendered": None},
], ids=lambda f: ",".join(f))
def test_a_template_rendered_to_nothing_shows_nothing(ans, fields):
    """Never the content: it holds the marker and the raw template."""
    rec = {"role": "ai", "content": NCONTENT + "\n\n" + CAP, "ts": 1.0, **fields}
    items = build(ans, rec)
    assert "Revenue was" not in text_of(items) and "Top region" not in text_of(items)


@pytest.mark.parametrize("fields", [
    {"narrative_template": "SLOTLESS", "narrative_rendered": ""},
    {"narrative_rendered": ""},
    {"caption_template": "SLOTLESS", "caption_rendered": ""},
    {"narrative_template": "SLOTLESS", "narrative_rendered": "Shown."},
    {"caption_template": "SLOTLESS", "caption_rendered": "Shown."},
], ids=lambda f: ",".join(f.values()))
def test_a_template_without_slots_is_not_shown_either(ans, fields):
    items = build(ans, {"role": "ai", "content": "SLOTLESS", "ts": 1.0, **fields})
    assert "SLOTLESS" not in text_of(items)
    if "Shown." in fields.values():
        once(items, "Shown.")


def test_a_rendered_narrative_replaces_the_content_without_a_template_too(ans):
    items = build(ans, {"role": "ai", "content": "RAWCONTENT", "narrative_rendered": "Shown.",
                        "ts": 1.0})
    assert "RAWCONTENT" not in text_of(items)
    once(items, "Shown.")


def test_the_markdown_escapes_of_values_are_removed(ans, chat_mod, renderer):
    """A value's own *, _ and leading - arrive as they are, in every display
    field."""
    esc, want = r"a\*b\_c \- d", "a*b_c - d"
    records = [
        single(chat_mod, NCONTENT, **narr(esc)),
        single(chat_mod, "x", caption_template=CAP, caption_rendered=esc),
        single(chat_mod, NCONTENT, **narr("Shown."), caption_described=esc, table=T1),
        single(chat_mod, "x", value_display="**Result:** " + esc),
        single(chat_mod, "x", headline_display=esc),
        multi(chat_mod, [chart("1", esc), chart("2", "two")], "x"),
        multi(chat_mod, [chart("1", "one"), chart("2", "two")], "x", tables_=[T1],
              captions=[esc]),
        multi(chat_mod, [chart("1", "one"), chart("2", "two", phase=2)], "x",
              phase2=narr(esc)),
    ]
    for n, rec in enumerate(records):
        out = text_of(build(ans, rec))
        assert want in out, (n, out)
        assert r"\*" not in out and r"\_" not in out, (n, out)


def test_bold_text_is_kept(ans, chat_mod):
    items = build(ans, single(chat_mod, "**North** leads.\n\n- one\n- two"))
    assert "**North** leads." in text_of(items)
    assert "- one\n- two" in text_of(items)


def test_the_role_denied_sentence(ans, chat_mod):
    text = "You no longer have access to Loans; ask your administrator."
    event = next(iter(chat_mod._denied_events(text)))
    once(build(ans, single(chat_mod, **event["result"])), text)


def test_an_error_record(ans, chat_mod):
    rec = chat_mod._build_ai_history_record(chat_mod.BRAIN_TIMEOUT_TEXT, None, None, [], {}, [])
    assert set(rec) == {"role", "content", "ts"}
    once(build(ans, rec), chat_mod.BRAIN_TIMEOUT_TEXT)


# ===========================================================================
# every record shape: charts and tables
# ===========================================================================
def test_a_single_chart(ans, chat_mod, renderer):
    items = build(ans, single(chat_mod, "Sales by month.", image_base64=plotly("1"),
                              chart_data={"rows": [{"secret": "CHARTDATA"}]}))
    in_order(items, "Sales by month.", "«1»")
    assert "CHARTDATA" not in text_of(items) and "plotly" not in text_of(items)


def test_the_images_array(ans, chat_mod, renderer):
    rec = multi(chat_mod, [chart("1", "First."), chart("2", "Second.")],
                "First.\n\nSecond.")
    items = build(ans, rec)
    in_order(items, "First.", "«1»", "Second.", "«2»")
    once(items, "First.")          # the joined content is not shown again
    assert images(items) == ["1", "2"]


def test_a_narrated_dashboard(ans, chat_mod, renderer):
    """Narrative, charts with their captions, then the tables with theirs."""
    rec = multi(chat_mod,
                [chart("1", CAP_R, caption_template=CAP), chart("2", "Units peaked.")],
                NCONTENT, phase1=narr(), tables_=[{**T1, "title": "Table one"},
                                                  {**T2, "title": "Table two"}],
                captions=["Caption one.", "Caption two."], templates=[CAP, ""])
    items = build(ans, rec)
    in_order(items, R, CAP_R, "«1»", "Units peaked.", "«2»", "Caption one.", "Table one",
             "Alpha", "Caption two.", "Table two", "Beta")
    assert len(tables(items)) == 2
    for part in (R, CAP_R, "Units peaked.", "Caption one.", "Caption two."):
        once(items, part)


def test_phase_two_comes_after_everything_of_phase_one(ans, chat_mod, renderer):
    rec = multi(chat_mod,
                [chart("late", "Late chart.", phase=2), chart("early", "Early chart.")],
                NCONTENT, phase1=narr(), phase2=narr("Second narrative."),
                tables_=[T2, T1], captions=["Late caption.", "Early caption."],
                phases=[2, 1])
    items = build(ans, rec)
    in_order(items, R, "Early chart.", "«early»", "Early caption.", "Alpha",
             "Second narrative.", "Late chart.", "«late»", "Late caption.", "Beta")
    for part in (R, "Early caption.", "Alpha", "Second narrative.", "Late caption.", "Beta"):
        once(items, part)
    assert images(items) == ["early", "late"]


@pytest.mark.parametrize("field,extra", [
    ("caption_rendered", {"caption_template": CAP}), ("caption_described", {})])
def test_one_chart_of_a_multi_turn(ans, chat_mod, renderer, field, extra):
    rec = multi(chat_mod, [chart("1", CAP_R, **extra)], NCONTENT, phase1=narr())
    assert rec[field] == CAP_R and "images" not in rec
    items = build(ans, rec)
    in_order(items, R, CAP_R, "«1»")
    once(items, CAP_R)


def test_several_tables_without_charts(ans, chat_mod):
    rec = single(chat_mod, "Two tables.", tables=[{**T1, "title": "One"},
                                                  {**T2, "title": "Two"}])
    in_order(build(ans, rec), "Two tables.", "One", "Alpha", "Two", "Beta")


def test_a_dropped_narratives_headline_of_a_dashboard(ans, chat_mod, renderer):
    rec = multi(chat_mod, [chart("1", "First."), chart("2", "Second.")], "First.\n\nSecond.",
                headline="Total: 5.")
    in_order(build(ans, rec), "Total: 5.", "First.", "«1»")


def test_a_caption_template_inside_the_content_is_never_shown(ans, chat_mod, renderer):
    """A turn whose narrative was dropped, one uncaptioned chart and a
    captioned table: its `content` joins the describe text and the raw caption
    TEMPLATE of the table, and no field of the record says so."""
    rec = multi(chat_mod, [chart("1", "Units peaked in March.")],
                "Units peaked in March.\n\n" + CAP, tables_=[T1], captions=[CAP_R],
                templates=[CAP])
    items = build(ans, rec)                     # check(): no "{{"
    in_order(items, "Units peaked in March.", "«1»", CAP_R, "Alpha")
    assert "Top region is [" not in text_of(items)
    only_tables = multi(chat_mod, [], CAP, tables_=[T1], captions=[CAP_R], templates=[CAP])
    items = build(ans, only_tables)
    in_order(items, CAP_R, "Alpha")
    once(items, "Top region")


@pytest.mark.parametrize("content,captions,templates,want", [
    (CAP, [CAP_R], [CAP], [CAP_R]),
    ("Plain caption.", ["Plain caption."], ["Plain caption."], ["Plain caption."]),
    (CAP + "\n\nOther {{total}}.", [CAP_R, "Other 5."], [CAP, "Other {{total}}."],
     [CAP_R, "Other 5."]),
    ("One {{region}}.\n\nTwo {{region}}.", ["One North.\n\nTwo North."],
     ["One {{region}}.\n\nTwo {{region}}."], ["One North.", "Two North."]),
    ("Described.", ["Described."], [""], ["Described."]),
    ("Described.\n\n" + CAP, ["Described.", CAP_R], ["", CAP], ["Described.", CAP_R]),
    (CAP + "\n\n" + CAP, [CAP_R, "Top region is South."], [CAP, CAP],
     [CAP_R, "Top region is South."]),
], ids=["one", "no slot", "two", "paragraphs", "described", "mixed", "same template"])
def test_a_tables_only_turn_shows_each_caption_once(ans, chat_mod, content, captions,
                                                    templates, want):
    """Its `content` joins the caption templates and the describe texts, and
    no narrative field says so."""
    rec = multi(chat_mod, [], content, tables_=[T1, T2][:len(captions)], captions=captions,
                templates=templates)
    assert rec["content"] == content and "narrative_template" not in rec
    items = build(ans, rec)                     # check(): no "{{"
    in_order(items, *want, "Beta" if len(captions) == 2 else "Alpha")
    for part in want:
        once(items, part)
    assert len(tables(items)) == len(captions)
    assert "[region]" not in text_of(items) and "[total]" not in text_of(items)


def test_the_records_of_the_previous_release(ans, renderer):
    rows = [json.loads(ln) for ln in FIXTURE.read_text(encoding="utf-8").splitlines()]
    out = [build(ans, r) for r in rows if r["role"] == "ai"]
    once(out[0], "Welcome! Ask me anything about sales.csv.")
    in_order(out[1], "North leads revenue with 1,200, followed by South.", "North")
    header, body = one_table(out[1])
    assert header == ["region", "revenue"]
    assert [(r[0], number(r[1])) for r in body] == [("North", 1200), ("South", 800)]
    in_order(out[2], "Revenue grew every month.", "«?»", "Units peaked in March.", "«?»")
    assert len(images(out[2])) == 2
    once(out[2], "Revenue grew every month.")
    once(out[3], "There are 3 regions.")


# --- stopped ----------------------------------------------------------------
def _stopped(chat_mod):
    two = [chart("1", CAP_R, caption_template=CAP), chart("2", "Second.")]
    event = {"narrative": True, "phase": 1, "text": R, "template": T, "values": V,
             "findings": F}
    one = [chart("1", "First.")]
    return {
        "nothing": (chat_mod._build_stopped_record(None, [], {}, []), []),
        "one chart": (chat_mod._build_stopped_record(None, [], {}, one), ["1"]),
        "one captioned": (chat_mod._build_stopped_record(None, [], {}, two[:1]), ["1"]),
        "two charts": (chat_mod._build_stopped_record(None, [], {}, two), ["1", "2"]),
        "narrated": (chat_mod._build_stopped_record(None, [], {}, two[:1],
                                                    narr_event=event), ["1"]),
        "narrated two": (chat_mod._build_stopped_record(None, [], {}, two,
                                                        narr_event=event), ["1", "2"]),
        "narrative only": (chat_mod._build_stopped_record(None, [], {}, [],
                                                          narr_event=event), []),
        "a table": (chat_mod._build_stopped_record(
            None, [], {}, [], combined_tables=[T1], full_table_keys=["k"]), []),
    }


@pytest.mark.parametrize("name", ["nothing", "one chart", "one captioned", "two charts",
                                  "narrated", "narrated two", "narrative only", "a table"])
def test_a_stopped_record_keeps_what_there_was_and_says_so(ans, chat_mod, renderer, name):
    rec, labels = _stopped(chat_mod)[name]
    items = build(ans, rec)
    assert images(items) == labels
    once(items, STOP)
    if name.startswith("narr"):
        once(items, R)
    if "captioned" in name or "two" in name:
        once(items, CAP_R)
    if name == "a table":
        assert one_table(items)[1][0][0] == "Alpha"


# ===========================================================================
# Markdown tables
# ===========================================================================
def rows_of(ans, chat_mod, rows, columns=None, **extra):
    return one_table(build(ans, single(chat_mod, "Text.", table=table(rows, columns, **extra))))


def test_header_separator_and_rows(ans, chat_mod):
    header, rows = rows_of(ans, chat_mod, [{"region": "North", "n": 1},
                                           {"region": "South", "n": 2}])
    assert header == ["region", "n"] and rows == [["North", "1"], ["South", "2"]]


HOSTILE = ["a|b", "|", "||", "a\\b", "a\\|b", "ends\\", "\\", "\\\\", "C:\\temp\\",
           "x\ny", "x\r\ny", "x\ry", "\n| forged | row |\n", "`", "```", "a`|`b",
           "```\ncode\n```", "<script>alert(1)</script>", "<img src=x onerror=alert(1)>",
           "<!-- x -->", "</table>", "a < b", "a & b", "---", "| --- | --- |", "# heading",
           "a\tb", "  padded  ", "თბილისი", "😀"]


def _squash(text):
    return re.sub(r"\s+", " ", text).strip()


@pytest.mark.parametrize("value", HOSTILE, ids=ascii)
def test_a_cell_never_breaks_its_row(ans, chat_mod, value):
    header, rows = rows_of(ans, chat_mod, [{"k": "row1", "v": value},
                                           {"k": "row2", "v": "after"}])
    assert header == ["k", "v"]
    assert [r[0] for r in rows] == ["row1", "row2"] and rows[1][1] == "after"
    assert _squash(plain(rows[0][1])) == _squash(value)
    # a raw tag is not left active
    raw = re.sub(r"<br\s*/?>", "", rows[0][1])
    assert not re.search(r"(?<!\\)<(?=[A-Za-z/!?])", raw), raw


@pytest.mark.parametrize("label", ["a|b", "a\nb", "a\\", "a\r\nb", "`", "<b>x</b>", ""],
                         ids=ascii)
def test_a_column_label_never_breaks_the_table(ans, chat_mod, label):
    header, rows = rows_of(ans, chat_mod, [{label: "v1", "z": "v2"}])
    assert len(header) == 2 and header[1] == "z"
    assert _squash(plain(header[0])) == _squash(label)
    assert rows == [["v1", "v2"]]


@pytest.mark.parametrize("value", [None, float("nan"), np.nan, pd.NaT, pd.NA,
                                   np.datetime64("NaT")], ids=repr)
@pytest.mark.parametrize("column", ["v", "client_id"])
def test_a_missing_value_is_an_empty_cell(ans, chat_mod, value, column):
    _h, rows = rows_of(ans, chat_mod, [{"k": "x", column: value}])
    assert rows == [["x", ""]]


@pytest.mark.parametrize("value", [True, False, np.True_, np.False_], ids=repr)
def test_a_boolean(ans, chat_mod, value):
    _h, rows = rows_of(ans, chat_mod, [{"v": value}])
    assert rows[0][0].lower() == str(bool(value)).lower()


@pytest.mark.parametrize("value", [0, 7, -7, 1200, 1234567, 0.5, -0.5, 1200.0, 0.1 + 0.2,
                                   15018.369999999999, 1e16, 123456789.125, np.int64(5),
                                   np.float64(2.5), np.int32(1200), decimal.Decimal("1.50")],
                         ids=repr)
def test_a_number(ans, chat_mod, value):
    _h, rows = rows_of(ans, chat_mod, [{"v": value}])
    assert abs(number(rows[0][0]) - float(value)) <= 0.005 + 1e-9
    assert len(rows[0][0]) <= 22 and "999999" not in rows[0][0].replace(",", "")
    assert "Decimal" not in rows[0][0] and "np." not in rows[0][0]


@pytest.mark.parametrize("value", ["2024-01-31", "2024-01-31T00:00:00",
                                   pd.Timestamp("2024-01-31"), dt.date(2024, 1, 31),
                                   dt.datetime(2024, 1, 31), np.datetime64("2024-01-31")],
                         ids=repr)
def test_a_date(ans, chat_mod, value):
    _h, rows = rows_of(ans, chat_mod, [{"when": value}])
    assert rows[0][0].startswith("2024-01-31"), rows
    assert "Timestamp" not in rows[0][0] and "datetime" not in rows[0][0]


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), [1, 2], {"a": 1}, (1,),
                                   b"x", "x \ud800 y", "\x00\x07", 1 + 2j, "x" * 5000],
                         ids=lambda v: ascii(v)[:20])
def test_an_odd_value_costs_at_most_its_cell(ans, chat_mod, value):
    _h, rows = rows_of(ans, chat_mod, [{"k": "row1", "v": value}, {"k": "row2", "v": "after"}])
    assert [r[0] for r in rows] == ["row1", "row2"] and rows[1][1] == "after"


# --- identifiers ------------------------------------------------------------
@pytest.mark.parametrize("column,value,digits", [
    ("client_id", 1234567.0, "1234567"), ("client_id", 1234567, "1234567"),
    ("year", 2024.0, "2024"), ("account", 40817810099910004312, "40817810099910004312"),
    ("personalNumber", 1001012345.0, "1001012345"), ("client_id", "007", "007"),
    ("client_id", np.int64(1234567), "1234567"), ("client_id", np.float64(77001), "77001"),
])
def test_an_identifier_by_name_is_plain_digits(ans, chat_mod, column, value, digits):
    """Also in a table stored without the list."""
    _h, rows = rows_of(ans, chat_mod, [{column: value}])
    assert rows == [[digits]]


def test_a_listed_identifier_is_plain_digits(ans, chat_mod):
    _h, rows = rows_of(ans, chat_mod, [{"ref_x": 1234567.0, "amount": 1234567.0}],
                       identifier_columns=["ref_x"])
    assert rows[0][0] == "1234567" and number(rows[0][1]) == 1234567


@pytest.mark.parametrize("listed", [None, "ref_x", 5, {}, [None, 5, ["x"]]], ids=repr)
def test_a_broken_identifier_list_is_ignored(ans, chat_mod, listed):
    _h, rows = rows_of(ans, chat_mod, [{"client_id": 1234567.0, "amount": 5}],
                       identifier_columns=listed)
    assert rows[0][0] == "1234567"


def test_identifiers_of_each_of_several_tables(ans, chat_mod):
    rec = single(chat_mod, "x", tables=[
        table([{"ref_x": 1234567.0}], identifier_columns=["ref_x"]),
        table([{"client_id": 7654321.0}])])
    assert [rows for _h, rows in tables(build(ans, rec))] == [[["1234567"]], [["7654321"]]]


# --- shapes -----------------------------------------------------------------
def test_duplicate_labels_stay_aligned(ans, chat_mod):
    header, rows = rows_of(ans, chat_mod, [{"a": "v1", "b": "v2"}], ["a", "a", "b"])
    assert header == ["a", "a", "b"]
    assert len(rows) == 1 and rows[0][2] == "v2"


def test_ragged_and_odd_rows_keep_the_columns(ans, chat_mod):
    rows = [{"a": "v1", "b": "v2"}, {"a": "only a"}, {}, {"b": "only b", "zz": "extra"},
            {"a": "last", "b": "row"}]
    _h, got = rows_of(ans, chat_mod, rows, ["a", "b"])
    assert got[0] == ["v1", "v2"] and got[1] == ["only a", ""] and got[2] == ["", ""]
    assert got[3] == ["", "only b"] and got[4] == ["last", "row"]
    items = build(ans, single(chat_mod, "Text.", table={
        "columns": ["a", "b"], "total_rows": 5,
        "rows": [None, "x", 5, ["v1", "v2", "v3"], {"a": "kept", "b": "x"}]}))
    assert "kept" in text_of(items)
    tables(items)                       # every printed row has two cells


@pytest.mark.parametrize("bad", [
    {"columns": ["a"], "rows": [], "total_rows": 0},
    {"columns": [], "rows": [], "total_rows": 0},
    {"columns": [], "rows": [{"a": 1}], "total_rows": 1},
    {}, {"columns": None, "rows": None}, {"columns": "ab", "rows": "cd"},
    {"columns": ["a"]}, {"rows": [{"a": 1}]}, [], "x", 5, True,
    {"columns": {"a": 1}, "rows": {"b": 2}},
], ids=ascii)
def test_an_empty_or_broken_table_costs_nothing_else(ans, chat_mod, bad):
    for rec in (single(chat_mod, "Kept text.", table=bad),
                single(chat_mod, "Kept text.", tables=[bad, T1])):
        items = build(ans, rec)
        once(items, "Kept text.")
        assert "Showing 0 of 0" not in text_of(items)
        if "tables" in rec:
            assert "Alpha" in text_of(items)


def test_the_styled_markup_is_ignored(ans, chat_mod):
    styled = "<style>#T_x{color:red}</style><table id='T_x'><tr><td>STYLED</td></tr></table>"
    items = build(ans, single(chat_mod, "x", table=table([{"a": "raw"}], styled_html=styled)))
    assert "STYLED" not in text_of(items) and "T_x" not in text_of(items)
    assert one_table(items)[1] == [["raw"]]


# --- the row limit ----------------------------------------------------------
def _numbered(n, **extra):
    return table([{"k": f"row{i}"} for i in range(n)], **extra)


def _note_line(items):
    lines = [ln for ln in text_of(items).split("\n") if "Showing " in ln]
    assert len(lines) == 1, lines
    return lines[0]


@pytest.mark.parametrize("limit,rows,total,shown", [
    (50, 50, 1234, 50), (3, 5, 5, 3), (3, 50, 1234, 3), (1, 2, 2, 1), (50, 10, 11, 10)])
def test_a_cut_table_says_how_many_rows_there_are(ans, chat_mod, monkeypatch,
                                                  limit, rows, total, shown):
    monkeypatch.setattr(settings, "MCP_TABLE_MAX_ROWS", limit)
    items = build(ans, single(chat_mod, "x", table=_numbered(rows, total_rows=total)))
    _h, body = one_table(items)
    assert [r[0] for r in body] == [f"row{i}" for i in range(shown)]
    assert f"Showing {shown} of {total} rows." in _note_line(items)
    in_order(items, f"row{shown - 1}", "Showing ")


@pytest.mark.parametrize("limit,rows,total", [(50, 50, 50), (3, 3, 3), (50, 1, 1)])
def test_a_whole_table_says_nothing(ans, chat_mod, monkeypatch, limit, rows, total):
    monkeypatch.setattr(settings, "MCP_TABLE_MAX_ROWS", limit)
    items = build(ans, single(chat_mod, "x", table=_numbered(rows, total_rows=total)))
    assert len(one_table(items)[1]) == rows
    assert "Showing" not in text_of(items)


@pytest.mark.parametrize("total", [None, "x", -1, 0, 2, float("nan"), [], "missing"], ids=repr)
def test_an_unusable_total_falls_back_to_the_rows_there_are(ans, chat_mod, monkeypatch, total):
    monkeypatch.setattr(settings, "MCP_TABLE_MAX_ROWS", 3)
    t = _numbered(5, total_rows=total)
    if total == "missing":
        del t["total_rows"]
    items = build(ans, single(chat_mod, "x", table=t))
    assert "Showing 3 of 5 rows." in _note_line(items)


def test_the_pointer_follows_the_link(ans, chat_mod, monkeypatch):
    rec = single(chat_mod, "x", table=_numbered(5, total_rows=500))
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE)
    after = _note_line(build(ans, rec)).split(" rows.", 1)[1]
    assert "link" in after.lower() or BASE in after, after
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")
    after = _note_line(build(ans, rec)).split(" rows.", 1)[1]
    assert "PowerDataChat" in after, after
    assert "link" not in after.lower() and "http" not in after, after


def test_each_table_has_its_own_note(ans, chat_mod, monkeypatch):
    monkeypatch.setattr(settings, "MCP_TABLE_MAX_ROWS", 2)
    rec = single(chat_mod, "x", tables=[_numbered(3, total_rows=30), _numbered(2),
                                        _numbered(4, total_rows=400)])
    items = build(ans, rec)
    notes = re.findall(r"Showing \d+ of \d+ rows\.", text_of(items))
    assert notes == ["Showing 2 of 30 rows.", "Showing 2 of 400 rows."]
    assert [len(rows) for _h, rows in tables(items)] == [2, 2, 2]


# ===========================================================================
# charts
# ===========================================================================
def test_a_plotly_chart_is_rendered_from_the_record(ans, chat_mod, renderer):
    items = build(ans, single(chat_mod, "x", image_base64=plotly("7")))
    assert images(items) == ["7"]
    assert renderer.calls == [(plotly("7"), "sid-1")]


def test_a_matplotlib_image_passes_through(ans, chat_mod):
    """The REAL resolver: no render happens for a stored PNG."""
    items = build(ans, single(chat_mod, "x", image_base64=b64(png("m"))))
    assert images(items) == ["m"]


@pytest.mark.parametrize("image", [
    "not base64 !!", b64(b"\xff\xd8\xff\xe0JPEG"), b64(b"GIF89a"), "AAAA",
    b64(b"<svg xmlns='x'/>"), b64(PNG_MAGIC)[:7], "iVBORw0KGgo", "ბაზა64",
    "<div>an html fragment</div>", "<svg></svg>", 5, ["x"], {"a": 1}, b"bytes", True,
    "\ud800"], ids=lambda v: ascii(v)[:24])
def test_anything_but_a_png_is_a_note(ans, chat_mod, monkeypatch, image):
    """The REAL resolver (its Plotly branch answers nothing)."""
    report = importlib.import_module("routes.report")
    monkeypatch.setattr(report, "_plotly_html_to_png", lambda *a, **k: None)
    rec = single(chat_mod, "Kept text.", image_base64=b64(png("ok")))
    good = build(ans, rec)
    rec["image_base64"] = image
    items = build(ans, rec)
    assert images(items) == []
    _is_note(items, good)
    once(items, "Kept text.")
    assert "fragment" not in text_of(items) and "svg" not in text_of(items)


def _is_note(items, good):
    """Exactly one short plain sentence about the chart was added."""
    known = set(text_of(good).split("\n"))
    extra = [ln for ln in text_of(items).split("\n") if ln.strip() and ln not in known]
    assert len(extra) == 1, extra
    assert re.search(r"(?i)\b(charts?|images?)\b", extra[0]), extra
    assert len(extra[0]) < 300 and "Error" not in extra[0], extra
    return extra[0]


@pytest.mark.parametrize("answer", [(None, True), (None, False), None, ("", True), (), "x",
                                    (5, True), (b64(b"\xff\xd8\xff\xe0"), True),
                                    ("!!!", True), (png("1"), True)], ids=lambda v: ascii(v)[:24])
def test_a_render_without_a_png_is_a_note(ans, chat_mod, renderer, answer):
    rec = multi(chat_mod, [chart("1", "First."), chart("2", "Second.")], "x")
    good = build(ans, rec)
    renderer.answer = lambda label: answer if label == "1" else (b64(png(label)), True)
    items = build(ans, rec)
    _is_note(items, good)
    assert images(items) == ["2"]
    in_order(items, "First.", "Second.", "«2»")


def test_a_raising_renderer_is_a_note_and_one_safe_log_line(ans, chat_mod, renderer, logs):
    rec = single(chat_mod, "Kept text.", image_base64=plotly("1"))
    good = build(ans, rec)
    renderer.raises = ValueError("SECRETVALUE\n[sid=x] FORGED")
    items = build(ans, rec)
    _is_note(items, good)
    once(items, "Kept text.")
    assert [ln for ln in logs if "ValueError" in ln], "must be logged (Article IV)"
    assert not [ln for ln in logs if "SECRETVALUE" in ln or len(ln.splitlines()) > 1], logs
    # and the next one works
    renderer.raises = None
    assert images(build(ans, rec)) == ["1"]


def test_the_first_six_charts_only(ans, chat_mod, renderer, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE)
    rec = multi(chat_mod, [chart(str(i), f"Text {i}.") for i in range(1, 9)], "x")
    items = build(ans, rec)
    assert images(items) == ["1", "2", "3", "4", "5", "6"]
    assert renderer.labels() == ["1", "2", "3", "4", "5", "6"], "more was rendered than sent"
    for i in range(1, 9):
        once(items, f"Text {i}.")
    lines = [ln for ln in text_of(items).split("\n") if re.search(r"\b8\b", ln)
             and not ln.startswith("Text ")]
    assert len(lines) == 1, lines
    assert re.search(r"(?i)charts|images", lines[0]), lines
    assert "link" in lines[0].lower() or BASE in lines[0], lines


def test_six_charts_need_no_sentence(ans, chat_mod, renderer):
    rec = multi(chat_mod, [chart(str(i), f"Text {i}.") for i in range(1, 7)], "x")
    items = build(ans, rec)
    assert len(images(items)) == 6
    assert not re.search(r"(?i)\bfirst\b|\bonly\b", text_of(items))


def test_the_image_limit_is_read_per_call(ans, chat_mod, renderer, monkeypatch):
    rec = multi(chat_mod, [chart(str(i), f"T{i}.") for i in range(1, 4)], "x")
    assert len(images(build(ans, rec))) == 3
    monkeypatch.setattr(ans, "MAX_IMAGES", 2)
    items = build(ans, rec)
    assert images(items) == ["1", "2"]
    assert re.search(r"\b3\b", text_of(items))


def test_a_failed_chart_does_not_bring_in_the_seventh(ans, chat_mod, renderer):
    renderer.answer = lambda label: (None, True) if label == "1" else (b64(png(label)), True)
    rec = multi(chat_mod, [chart(str(i), f"T{i}.") for i in range(1, 8)], "x")
    assert images(build(ans, rec)) == ["2", "3", "4", "5", "6"]
    assert "7" not in renderer.labels()


def test_an_entry_without_an_image_is_only_its_text(ans, chat_mod, renderer):
    rec = multi(chat_mod, [chart("1", "First."), chart("2", "Second.")], "x")
    rec["images"] += [{"answer": "No image."}, None, "x", 5, {"image_base64": None}]
    items = build(ans, rec)
    assert images(items) == ["1", "2"]
    in_order(items, "«1»", "«2»", "No image.")


# --- the timeout ------------------------------------------------------------
def _hang(renderer, *labels):
    for label in labels:
        renderer.block[label] = threading.Event()


def _one(chat_mod, label):
    return single(chat_mod, f"Text of {label}.", image_base64=plotly(label))


def test_a_render_that_hangs_is_a_note(ans, chat_mod, renderer, monkeypatch, logs):
    good = build(ans, _one(chat_mod, "x"))
    monkeypatch.setattr(ans, "RENDER_TIMEOUT_S", 0.3)
    _hang(renderer, "x")
    started = time.monotonic()
    items = build(ans, _one(chat_mod, "x"))
    assert time.monotonic() - started < 5
    _is_note(items, good)
    once(items, "Text of x.")
    assert [ln for ln in logs if "TIMEOUT" in ln.upper()], "must be logged (Article IV)"


def test_the_next_answer_is_not_blocked_by_a_stuck_render(ans, chat_mod, renderer, monkeypatch):
    monkeypatch.setattr(ans, "RENDER_TIMEOUT_S", 0.5)
    _hang(renderer, "stuck")
    assert images(build(ans, _one(chat_mod, "stuck"))) == []
    for label in ("a", "b"):
        assert images(build(ans, _one(chat_mod, label))) == [label]
    # the late result is nobody's image
    renderer.block["stuck"].set()
    _until(lambda: renderer.active == 0, "the stuck render to end")
    for label in ("c", "d"):
        assert images(build(ans, _one(chat_mod, label))) == [label]


def test_a_hang_costs_only_its_own_chart(ans, chat_mod, renderer, monkeypatch):
    monkeypatch.setattr(ans, "RENDER_TIMEOUT_S", 0.5)
    _hang(renderer, "2")
    rec = multi(chat_mod, [chart(str(i), f"T{i}.") for i in (1, 2, 3)], "x", tables_=[T1])
    items = build(ans, rec)
    assert images(items) == ["1", "3"]
    in_order(items, "T1.", "«1»", "T2.", "T3.", "«3»", "Alpha")


def test_the_number_of_stuck_renders_is_bounded(ans, chat_mod, renderer, monkeypatch):
    monkeypatch.setattr(ans, "RENDER_TIMEOUT_S", 0.2)
    labels = [f"s{i}" for i in range(8)]
    _hang(renderer, *labels)
    before = threading.active_count()
    good = None
    for label in labels:
        items = build(ans, _one(chat_mod, label))
        assert images(items) == []
        once(items, f"Text of {label}.")
        good = good or items
    assert renderer.labels() == labels[:3], renderer.labels()
    assert threading.active_count() <= before + 4
    # over the cap nothing waits
    monkeypatch.setattr(ans, "RENDER_TIMEOUT_S", 30)
    started = time.monotonic()
    rec = multi(chat_mod, [chart(str(i), f"T{i}.") for i in range(1, 7)], "x")
    items = build(ans, rec)
    assert images(items) == [] and time.monotonic() - started < 5
    assert len(renderer.calls) == 3
    assert re.search(r"(?i)\b(charts?|images?)\b", text_of(items)), "a skipped chart says so"
    # a stored PNG needs no render
    assert images(build(ans, single(chat_mod, "x", image_base64=b64(png("m"))))) == ["m"]
    # it recovers when the stuck ones come back
    for gate in renderer.block.values():
        gate.set()
    _until(lambda: renderer.active == 0, "the stuck renders to end")
    monkeypatch.setattr(ans, "RENDER_TIMEOUT_S", 10)
    _until(lambda: images(build(ans, _one(chat_mod, "again"))) == ["again"],
           "rendering to resume")


def test_the_cap_is_read_per_call(ans, chat_mod, renderer, monkeypatch):
    monkeypatch.setattr(ans, "RENDER_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ans, "MAX_STUCK_RENDERS", 1)
    _hang(renderer, "s1", "s2")
    for label in ("s1", "s2"):
        assert images(build(ans, _one(chat_mod, label))) == []
    assert renderer.labels() == ["s1"]


def test_a_render_thread_never_keeps_the_process_alive(ans, chat_mod, renderer):
    build(ans, _one(chat_mod, "x"))
    assert renderer.threads[0] is not threading.current_thread()
    assert renderer.threads[0].daemon is True, \
        "a hung render would be joined at interpreter exit"


def test_renders_run_one_at_a_time_and_waiting_is_not_a_timeout(ans, chat_mod, renderer,
                                                               monkeypatch):
    """Four answers at once: each of their renders fits the limit, the sum
    does not."""
    monkeypatch.setattr(ans, "RENDER_TIMEOUT_S", 1.5)
    renderer.delay = 0.6
    out, lock, barrier = {}, threading.Lock(), threading.Barrier(4)

    def go(label):
        rec = _one(chat_mod, label)
        barrier.wait(10)
        got = ans.build_content(rec, chat_id=CHAT, conv_id=CONV, email=OWNER, sid="s")
        with lock:
            out[label] = images(got)

    threads = [threading.Thread(target=go, args=(f"c{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert out == {f"c{i}": [f"c{i}"] for i in range(4)}
    assert renderer.peak == 1, "two renders ran at the same time"


# ===========================================================================
# nothing else happens
# ===========================================================================
def test_the_record_is_not_changed(ans, chat_mod, renderer):
    rec = multi(chat_mod, [chart("1", r"a\*b"), chart("2", "Second.")], NCONTENT,
                phase1=narr(), tables_=[{**T1, "styled_html": "<table></table>"}],
                captions=["Caption."])
    before = copy.deepcopy(rec)
    first = build(ans, rec)
    assert rec == before
    assert build(ans, rec) == first


def test_nothing_is_run_again_read_again_or_sent(ans, chat_mod, renderer, monkeypatch, tmp_path):
    import code_exec
    import executor_client
    import plot_utils
    import run_chat_local
    hit = []

    def trap(name):
        return lambda *a, **k: hit.append(name)

    for mod, names in ((code_exec, ["safe_execute"]), (plot_utils, ["render_plot_safe"]),
                       (run_chat_local, ["run_chat_multi_plot", "run_chat", "safe_execute",
                                         "render_plot_safe", "run_live_select"]),
                       (executor_client, ["execute"]),
                       (chat_mod, ["_reexecute_full_df", "_load_full_table_record",
                                   "run_item_refresh"]),
                       (local_store.ChatDataStore, ["load_dataframes"])):
        for name in names:
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, trap(name))
    for name, fn in list(vars(brain_client).items()):
        if inspect.isfunction(fn) and fn.__module__ == "brain_client" and not name.startswith("__"):
            monkeypatch.setattr(brain_client, name, trap(name))
    rec = multi(chat_mod, [chart("1", "First."), chart("2", "Second.")], NCONTENT,
                phase1=narr(), tables_=[{**T1, "total_rows": 900}], captions=["Caption."])

    def files():
        return {p.relative_to(tmp_path).as_posix(): p.read_bytes() if p.is_file() else None
                for p in tmp_path.rglob("*") if "logs" not in p.parts}

    before = files()
    build(ans, rec)
    assert hit == []
    assert files() == before


def test_a_removed_chat_or_account_is_not_created_again(ans, chat_mod, tmp_path):
    for kw in ({"chat_id": "c_gone000000000001"}, {"email": "gone@acme.com"},
               {"chat_id": "../../users/x"}, {"chat_id": None}, {"email": None}):
        before = sorted(str(p) for p in tmp_path.rglob("*") if "logs" not in p.parts)
        items = build(ans, single(chat_mod, "Kept text."), **kw)
        once(items, "Kept text.")
        after = sorted(str(p) for p in tmp_path.rglob("*") if "logs" not in p.parts)
        assert after == before, kw


# ===========================================================================
# one failing part never costs the rest
# ===========================================================================
class Broken:
    """A value nothing can be read from."""

    def __str__(self):
        raise RuntimeError("SECRETCELL")

    __repr__ = __str__


def test_an_unreadable_cell_costs_at_most_its_table(ans, chat_mod, logs):
    rec = single(chat_mod, "Kept text.", tables=[table([{"a": Broken()}]), T1])
    items = build(ans, rec)
    in_order(items, "Kept text.", "Alpha")
    assert "SECRETCELL" not in text_of(items) + "".join(logs)


@pytest.mark.parametrize("where", ["identifiers", "title", "conversation", "base", "plain"])
def test_a_failing_helper_costs_at_most_its_part(ans, chat_mod, renderer, monkeypatch, where):
    def boom(*a, **k):
        raise KeyError("SECRETHELPER")

    auth = importlib.import_module("routes.auth")
    ident = importlib.import_module("ident_rule")
    target = {"identifiers": (ident, "identifier_columns"),
              "title": (local_store.AuthStore, "active_chat_title"),
              "conversation": (local_store.AuthStore, "list_conversations"),
              "base": (auth, "_public_base"), "plain": (narrative, "plain")}[where]
    monkeypatch.setattr(*target, boom)
    rec = multi(chat_mod, [chart("1", "First."), chart("2", "Second.")], "x", tables_=[T1],
                captions=["Caption."])
    items = build(ans, rec)
    assert images(items) == ["1", "2"]
    assert "SECRETHELPER" not in text_of(items)
    if where != "identifiers":
        assert "Alpha" in text_of(items)
    if where != "plain":
        in_order(items, "First.", "«1»", "Second.", "«2»")


@pytest.mark.parametrize("field,value", [
    ("narrative_rendered", 5), ("narrative_rendered", ["x"]), ("content", {"a": 1}),
    ("content", 5), ("value_display", ["x"]), ("headline_display", 5), ("images", "x"),
    ("images", {"a": 1}), ("tables", "x"), ("tables", {"a": 1}), ("table_captions", "x"),
    ("table_captions", [5, None]), ("table_phases", "x"), ("table_phases", [None]),
    ("phase2", "x"), ("phase2", {"narrative_rendered": 5}), ("caption_rendered", 5),
    ("caption_described", {}), ("narrative_template", 5), ("caption_template", ["x"]),
    ("stopped", "x"), ("image_base64", 5)], ids=lambda v: ascii(v)[:24])
def test_a_field_of_another_type_never_raises(ans, chat_mod, renderer, field, value):
    rec = single(chat_mod, "Kept text.", table=T1)
    rec[field] = value
    items = build(ans, rec)
    assert "Alpha" in text_of(items)


def test_text_that_cannot_be_encoded_is_still_an_answer(ans, chat_mod, renderer):
    rec = multi(chat_mod, [chart("1", "First \ud800."), chart("2", "Second \udfff.")], "x",
                phase1=narr("Narrative \ud83d."), tables_=[
                    {**table([{"a\ud800": "c\ud800"}]), "title": "T\ud800"}],
                captions=["Cap \ud800"])
    items = build(ans, rec)             # check(): encodable
    assert "Narrative" in text_of(items) and images(items) == ["1", "2"]


def test_a_hostile_sid_cannot_forge_a_log_line(ans, chat_mod, renderer, logs):
    renderer.raises = ValueError("x")
    build(ans, single(chat_mod, "x", image_base64=plotly("1")),
          sid="s\n[sid=x] FORGED", chat_id="c\nFORGED", email="e\u2028FORGED", conv_id="cv\nF")
    assert not [ln for ln in logs if len(ln.splitlines()) > 1], logs


# ===========================================================================
# the link
# ===========================================================================
@pytest.mark.parametrize("base", [BASE, BASE + "/", "http://127.0.0.1:8091", " " + BASE + " ",
                                  BASE + "/pdc"])
def test_the_link_is_in_the_last_text(ans, chat_mod, renderer, monkeypatch, base):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", base)
    url = base.strip().rstrip("/") + "/c/" + CONV
    for rec in (single(chat_mod, "x"), single(chat_mod, "x", table=T1),
                single(chat_mod, "x", image_base64=plotly("1")),
                chat_mod._build_stopped_record(None, [], {}, [])):
        items = build(ans, rec)
        assert re.search(re.escape(url) + r"(?![\w/])", last_text(items)), last_text(items)
        assert text_of(items).count("/c/") == 1


@pytest.mark.parametrize("base", ["", "   ", "pdc.bank.example", "ftp://pdc.bank.example",
                                  "javascript:alert(1)"])
def test_without_a_base_the_place_is_named(ans, chat_mod, monkeypatch, base):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", base)
    items = build(ans, single(chat_mod, "x"))
    place = last_text(items)
    assert "PowerDataChat" in place and "Sales" in place and CONV_TITLE in place
    out = text_of(items)
    assert "/c/" not in out and "http" not in out and CONV not in out
    assert "pdc.bank" not in out and "javascript" not in out


def test_the_setting_is_read_per_call(ans, chat_mod, monkeypatch):
    rec = single(chat_mod, "x")
    assert "/c/" not in text_of(build(ans, rec))
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE)
    assert BASE + "/c/" + CONV in last_text(build(ans, rec))
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")
    assert "/c/" not in text_of(build(ans, rec))


def test_the_chat_is_named_as_the_user_named_it(ans, chat_mod):
    auth = local_store.AuthStore()
    auth.rename_active_chat(OWNER, CHAT, "My numbers")
    place = last_text(build(ans, single(chat_mod, "x")))
    assert "My numbers" in place and "Sales" not in place
    # the sharer's name is not the recipient's: no sidebar row -> the meta title
    place = last_text(build(ans, single(chat_mod, "x"), email=FRIEND))
    assert "Sales" in place and "My numbers" not in place


def test_a_chat_without_any_title_is_a_chat(ans, chat_mod, world):
    meta = world.read_meta()
    meta["title"] = None
    world.write_meta(meta)
    place = last_text(build(ans, single(chat_mod, "x"), email=FRIEND))
    assert re.search(r"\bChat\b", place.replace("PowerDataChat", "")), place
    assert "None" not in place


def test_the_conversation_is_named_as_the_users_index_names_it(ans, chat_mod):
    auth = local_store.AuthStore()
    auth.record_conversation(FRIEND, CHAT, CONV, "Friend's own title")
    assert CONV_TITLE in last_text(build(ans, single(chat_mod, "x")))
    place = last_text(build(ans, single(chat_mod, "x"), email=FRIEND))
    assert "Friend's own title" in place and CONV_TITLE not in place


@pytest.mark.parametrize("title", [None, 5, ["x"], "", "missing"], ids=repr)
def test_an_unusable_conversation_title_never_shows(ans, chat_mod, tmp_path, title):
    path = tmp_path / "users" / OWNER / "conversations.jsonl"
    row = {"conv_id": CONV, "chat_id": CHAT, "title": title}
    if title == "missing":
        del row["title"]
    path.write_text("{bad\n" + json.dumps(row) + "\n", encoding="utf-8")
    items = build(ans, single(chat_mod, "Kept text."))
    once(items, "Kept text.")
    assert "Sales" in last_text(items) and "None" not in last_text(items)


@pytest.mark.parametrize("conv", ["", None, 5, "cv_x", "../../etc", "cv_0123456789abcdeg",
                                  "cv_0123456789abcdef/../x", "cv_0123456789abcdef\n",
                                  "cv_0123456789abcdef?x=1", "x" * 5000],
                         ids=lambda v: ascii(v)[:24])
def test_a_malformed_conversation_id_makes_no_link(ans, chat_mod, monkeypatch, conv):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE)
    items = build(ans, single(chat_mod, "Kept text."), conv_id=conv)
    once(items, "Kept text.")
    assert "/c/" not in text_of(items)
    if conv:
        assert str(conv).strip() not in text_of(items)


def _unsaved(ans, record):
    items = build(ans, record, saved=False)
    out = text_of(items)
    assert not re.search(r"(?i)https?:|www\.|/c/|\blink\b|conversation", out), out
    assert CONV not in out and CONV_TITLE not in out and "Sales" not in out, out
    return items


@pytest.mark.parametrize("base", [BASE, ""])
def test_an_answer_that_was_not_saved_has_no_place_to_look(ans, chat_mod, renderer,
                                                           monkeypatch, base):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", base)
    last = set()
    for rec in (single(chat_mod, "x"), single(chat_mod, "Two.", table=T1),
                single(chat_mod, "Three.", image_base64=plotly("1")),
                chat_mod._build_stopped_record(None, [], {}, [])):
        last.add(last_text(_unsaved(ans, rec)))
    assert len(last) == 1, "one fixed sentence"
    sentence = last.pop()
    assert "could not be saved" in sentence and "PowerDataChat" in sentence, sentence
    assert sentence[0].isupper() and sentence.endswith(".") and "\n" not in sentence
    once(_unsaved(ans, single(chat_mod, "x")), sentence)
    # a saved one does not say it
    assert sentence not in text_of(build(ans, single(chat_mod, "x")))
    assert build(ans, single(chat_mod, "x"), saved=True) == build(ans, single(chat_mod, "x"))


def test_the_answer_itself_is_the_same_unsaved(ans, chat_mod, renderer):
    rec = multi(chat_mod, [chart("1", "First."), chart("2", "Second.")], "x", tables_=[T1],
                captions=["Caption."])
    items = _unsaved(ans, rec)
    in_order(items, "First.", "«1»", "Second.", "«2»", "Caption.", "Alpha", "could not be saved")


def test_the_cut_notes_of_an_unsaved_answer_point_nowhere(ans, chat_mod, renderer, monkeypatch):
    """There is nothing to open: the counts stay, the pointers go."""
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE)
    monkeypatch.setattr(settings, "MCP_TABLE_MAX_ROWS", 2)
    items = _unsaved(ans, single(chat_mod, "x", table=_numbered(5, total_rows=500)))
    assert "Showing 2 of 500 rows." in _note_line(items)
    items = _unsaved(ans, multi(chat_mod, [chart(str(i), f"T{i}.") for i in range(1, 9)], "x"))
    assert len(images(items)) == 6 and re.search(r"\b8\b", text_of(items))


def test_the_module_logs_safely():
    """The structural log guard of the other MCP modules, applied here; the
    module also belongs in its LOG_SAFE_MODULES list."""
    guard = importlib.import_module("tests.test_executor_infra_short_circuit")
    guard.test_no_log_line_carries_an_unescaped_value("mcp_answer.py")
    guard.test_the_guard_can_see_every_call_site_in_the_module("mcp_answer.py")


# ===========================================================================
# end to end: POST /mcp with the real content builder
# ===========================================================================
@pytest.fixture
def served(world, renderer, monkeypatch):
    """`served.events` is what the pipeline yields."""
    app_mod = importlib.import_module("app")
    endpoint = importlib.import_module("routes.mcp")
    jobs = importlib.import_module("mcp_jobs")
    mcp_tokens = importlib.import_module("mcp_tokens")
    chat = importlib.import_module("routes.chat")
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "EXECUTOR_NETWORK_CIDR", "")
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 15.0)
    endpoint.reset_limiter()
    jobs.reset()

    class Served:
        events: list = []

        def __init__(self):
            _row, self.raw = mcp_tokens.create_token("Helpdesk bot", "per_user", ADMIN)
            self.client = TestClient(app_mod.app, base_url="https://testserver")

        def call(self, tool, args, **headers):
            r = self.client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": args}},
                headers={"Authorization": f"Bearer {self.raw}", "X-PDC-User-Email": OWNER,
                         **headers})
            assert r.status_code == 200, (r.status_code, r.text[:300])
            return r.json()["result"]

        def answer(self, events, **headers):
            self.events = events
            job = self.call("ask", {"chat_id": CHAT, "question": "q?"}, **headers)
            job_id = job["structuredContent"]["job_id"]
            for _ in range(5):
                res = self.call("get_answer", {"job_id": job_id}, **headers)
                if res["structuredContent"]["state"] != "running":
                    return res
            raise AssertionError("the job never finished")

    s = Served()

    def pipeline(**kw):
        yield from copy.deepcopy(s.events)

    monkeypatch.setattr(chat.run_chat_local, "run_chat_multi_plot", pipeline)
    yield s
    for t in threading.enumerate():
        if t.name.startswith("mcp-job"):
            t.join(15)
    jobs.reset()
    endpoint.reset_limiter()


def _result(**res):
    return [{"single_response": True, "result": {
        "text": "the answer", "image_base64": None, "table": None, "code": CODE,
        "usage": {}, **res}}]


def test_an_answer_over_the_wire(served, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE)
    res = served.answer(_result(text=NCONTENT, **narr(), image_base64=plotly("1"),
                                caption_template=CAP, caption_rendered=CAP_R,
                                table=table([{"region": "Alpha", "when": pd.Timestamp(
                                    "2024-01-31"), "n": np.int64(3)}], total_rows=900)))
    assert not res.get("isError")
    items = check(res["content"])
    in_order(items, R, CAP_R, "Alpha")
    assert images(items) == ["1"]
    assert "Showing 1 of 900 rows." in text_of(items)
    conv = local_store.AuthStore().list_conversations(OWNER)[0]["conv_id"]
    assert conv != CONV and BASE + "/c/" + conv in last_text(items)


@pytest.mark.parametrize("base", [BASE, ""])
def test_the_link_is_never_derived_from_the_request(served, monkeypatch, base):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", base)
    res = served.answer(_result(), **{"X-Forwarded-Host": "evil.example",
                                      "Forwarded": "host=evil.example",
                                      "Host": "evil.example"})
    out = text_of(check(res["content"]))
    assert "evil" not in out and "testserver" not in out
    if not base:
        assert re.search(r"MCP \d{4}-\d\d-\d\d", out) and "Sales" in out


def test_an_answer_that_could_not_be_saved_over_the_wire(served, monkeypatch):
    """`persisted` of the turn decides."""
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE)
    real = local_store.ChatDataStore.append_history

    def append(self, conv_id, record):
        if record.get("role") == "ai" and "usage" in record:
            raise OSError("disk full")
        return real(self, conv_id, record)

    monkeypatch.setattr(local_store.ChatDataStore, "append_history", append)
    res = served.answer(_result(text="Kept text."))
    assert not res.get("isError")
    out = text_of(check(res["content"]))
    assert "Kept text." in out and "could not be saved" in out
    assert "http" not in out and "/c/" not in out


def test_an_unencodable_answer_over_the_wire(served):
    res = served.answer(_result(text="Total \ud800 is 5."))
    assert not res.get("isError")
    assert "is 5." in text_of(check(res["content"]))


def test_a_failed_answer_is_the_webs_text_alone(served, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE)
    res = served.answer([])
    assert res["isError"] is True
    assert res["content"] == [{"type": "text", "text": GENERIC}]


def test_a_hanging_render_still_answers(served, ans, renderer, monkeypatch):
    monkeypatch.setattr(ans, "RENDER_TIMEOUT_S", 0.3)
    _hang(renderer, "1")
    res = served.answer(_result(text="Kept text.", image_base64=plotly("1")))
    assert not res.get("isError") and images(check(res["content"])) == []
    once(res["content"], "Kept text.")
