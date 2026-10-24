"""Narrative-template answers: what the AI history record holds, what the
title and the report send to the brain, and what stays local
(docs/DIAGNOSTICS_SPEC.md A5.2 R0, A5.5; Article II).

The rule under test (R0): a string produced by `narrative.render` (or
`narrative.value_display`) is never stored in a record's `content`, never
handed to a `brain_client.*` function and never placed in a field read by
`/v1/title` or `/v1/report`. The rendered text lives in its own record fields
(`narrative_rendered`, `caption_rendered`, `images[i].answer`) for display.

Session 1 emits phase 1 only: every event carries `phase: 1` and no record
carries `phase2`.

Offline: DATA_ROOT is tmp_path; the planner generator, the title thread and
`brain_client.report` are stubbed.
"""
import json
import secrets
from pathlib import Path
from xml.sax import saxutils

import pandas as pd
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import local_store
import narrative
from conftest import JSON_HEADERS
from settings import settings

OWNER = "owner@acme.com"
CHAT = "c_narrrec00000001"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "history_pre_narrative.jsonl"

# The sentinel VALUES of the boundary tests: one number, one category.
V = {"total": 987654321, "region": "SENTINEL_CATEGORY"}
SENTINELS = ("987654321", "987,654,321", "SENTINEL")
T = ("Revenue was {{total:count}} in {{region}}."
     "{{#anomaly}} One month stands out.{{/anomaly}}")
F = {"anomaly": True, "coverage_dim": "region"}
R = narrative.render(T, V, F, sid="test")
CAP = "Top region is {{region}} at {{total:count}}."
CAP_R = narrative.render(CAP, V, F, sid="test")
CODE1 = "fig = px.bar(dfs['sales.csv'], x='region', y='amount')"
CODE2 = "fig = px.line(dfs['sales.csv'], x='region', y='amount')"
STOP_NOTE = "\n\n⏹ Response stopped by user."
NEW_KEYS = ("narrative_template", "narrative_rendered", "values", "findings",
            "caption_template", "caption_rendered", "caption_described",
            "value_display", "table_captions", "table_caption_templates",
            "table_phases", "phase2", "phase2_status")


def _no_sentinel(text, where=""):
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    for bad in SENTINELS:
        assert bad not in s, f"{where}: value {bad!r} leaked into {s[:400]!r}"


def test_fixture_constants_are_what_the_tests_assume():
    """Self-check, so a green run cannot be a blind one: the rendered text
    carries the sentinels, the templates do not."""
    assert "987,654,321" in R and "SENTINEL" in R
    assert "987,654,321" in CAP_R
    _no_sentinel(T)
    _no_sentinel(CAP)
    _no_sentinel(narrative.history_content(T, F))
    _no_sentinel(narrative.brain_view(T, F))


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _make_chat(chat_id=CHAT, owner=OWNER):
    store = local_store.ChatDataStore(chat_id)
    pd.DataFrame({"region": ["North", "South"], "amount": [1, 2]}).to_csv(
        store.files_dir / "sales.csv", index=False)
    meta = store.read_meta()
    meta["owner"] = owner
    meta["title"] = "Sales"
    meta["files"] = [{"file_name": "sales.csv", "file_description": "sales",
                      "schema": {"fields": {"region": {"description": "region"},
                                            "amount": {"description": "amount"}}}}]
    store.write_meta(meta)
    return store


def _conversation(store, rows, email=OWNER):
    conv_id = store.new_conversation("t")
    for row in rows:
        store.append_history(conv_id, row)
    local_store.AuthStore().record_conversation(email, store.chat_id, conv_id, "t")
    return conv_id


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(OWNER)
    store = _make_chat()
    yield {"store": store, "tmp": tmp_path}
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def client(world):
    import routes.chat as chat_mod
    import routes.report as report_mod

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)
    app.include_router(report_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_" + secrets.token_hex(8)
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}", headers=JSON_HEADERS)
    return tc


@pytest.fixture
def events(monkeypatch):
    """`events.set([...])` makes the planner generator yield those events;
    `events.calls` records the kwargs of every run."""
    import routes.chat as chat_mod

    class _Ev:
        seq: list = []
        calls: list = []

        def set(self, seq):
            self.seq = [dict(e) for e in seq]

    ev = _Ev()
    ev.calls = []

    def gen(**kw):
        ev.calls.append(kw)
        for e in ev.seq:
            yield dict(e)

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        lambda **kw: gen(**kw))
    return ev


def _sse(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def _stream(client, question="q", conv_id=None):
    body = {"question": question}
    if conv_id:
        body["conv_id"] = conv_id
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json=body, headers=JSON_HEADERS)
    assert r.status_code == 200, r.text[:300]
    evs = _sse(r.text)
    conv = next(e["conv_id"] for e in evs if e.get("conv_id"))
    return evs, conv


def _history(conv_id):
    return local_store.ChatDataStore(CHAT).get_history(conv_id)


def _last_ai(conv_id):
    rows = [r for r in _history(conv_id) if r.get("role") == "ai"]
    assert rows, "no AI record persisted"
    return rows[-1]


def _chart(n, *, answer, caption_template=None, code=CODE1, total=2):
    ev = {"partial": True, "phase": 1, "block_index": n,
          "image_base64": f"<div>plotly chart {n}</div>", "answer": answer,
          "caption_template": caption_template, "code": code,
          "chart_n": n, "chart_total": total, "usage": {}}
    return ev


def _narr_event():
    return {"narrative": True, "phase": 1, "text": R, "template": T,
            "values": dict(V), "findings": dict(F)}


def _view(with_narrative=True):
    return {"phase1": ({"narrative_template": T, "narrative_rendered": R,
                        "values": dict(V), "findings": dict(F)}
                       if with_narrative else None),
            "phase2": None, "phase2_status": None}


def _done(combined, display, codes=(CODE1, CODE2), *, view=None):
    return {"done": True, "partial": False, "combined_answer": combined,
            "display_answer": display, "combined_codes": list(codes),
            "total_usage": {}, "narrative_view": view or _view(False),
            "table_captions": [], "table_caption_templates": [], "table_phases": []}


def _narrative_turn():
    """Two captioned/uncaptioned charts, the phase-1 narrative, done."""
    return [
        _chart(1, answer=CAP_R, caption_template=CAP, code=CODE1),
        _chart(2, answer="Units peaked in March.", code=CODE2),
        _narr_event(),
        _done(narrative.history_content(T, F), R, view=_view(True)),
    ]


# ---------------------------------------------------------------------------
# record fields
# ---------------------------------------------------------------------------
def test_single_record_fields():
    """A single-flow result carrying the narrative keys persists each of them
    beside a brain-safe `content` (= result["text"], the marker + template)."""
    import routes.chat as chat_mod

    single = {
        "text": narrative.history_content(T, F),
        "image_base64": "<div>plotly</div>", "table": None,
        "code": CODE1, "usage": {},
        "narrative_template": T, "narrative_rendered": R,
        "values": dict(V), "findings": dict(F),
        "caption_template": CAP, "caption_rendered": CAP_R,
    }
    rec = chat_mod._build_ai_history_record(None, single, None, [], {}, [])
    assert rec["content"] == single["text"]
    _no_sentinel(rec["content"], "content")
    for key in ("narrative_template", "narrative_rendered", "values", "findings",
                "caption_template", "caption_rendered"):
        assert rec.get(key) == single[key], (key, rec.get(key))
    assert "phase2" not in rec

    # No narrative, caption missing: the describe text of the single visual.
    described = {"text": narrative.history_content(T, F), "image_base64": None,
                 "table": None, "code": CODE1, "usage": {},
                 "narrative_template": T, "narrative_rendered": R,
                 "values": dict(V), "findings": dict(F),
                 "caption_described": "Sales rose."}
    rec = chat_mod._build_ai_history_record(None, described, None, [], {}, [])
    assert rec.get("caption_described") == "Sales rose."

    # Fallback scalar: the local value line is display-only.
    scalar = {"text": "There are three regions.", "image_base64": None,
              "table": None, "code": CODE1, "usage": {},
              "value_display": "**Result:** 3"}
    rec = chat_mod._build_ai_history_record(None, scalar, None, [], {}, [])
    assert rec["content"] == "There are three regions."
    assert rec.get("value_display") == "**Result:** 3"

    # An old-shape result keeps today's record shape exactly.
    old = {"text": "plain", "image_base64": None, "table": None,
           "code": CODE1, "usage": {}}
    rec = chat_mod._build_ai_history_record(None, old, None, [], {}, [])
    assert not [k for k in NEW_KEYS if k in rec], rec


def test_multi_images_caption_template_and_phase(client, events):
    """Each chart entry keeps its caption template and phase; its `answer` is
    the RENDERED caption (display); the record `content` is the brain-safe
    join of caption templates / describe texts."""
    combined = CAP + "\n\nUnits peaked in March."
    events.set([
        _chart(1, answer=CAP_R, caption_template=CAP, code=CODE1),
        _chart(2, answer="Units peaked in March.", code=CODE2),
        _done(combined, CAP_R + "\n\nUnits peaked in March."),
    ])
    _, conv = _stream(client, "two charts")
    rec = _last_ai(conv)
    assert rec["content"] == combined
    _no_sentinel(rec["content"], "content")
    imgs = rec.get("images") or []
    assert len(imgs) == 2, rec
    assert imgs[0].get("caption_template") == CAP
    assert imgs[0].get("answer") == CAP_R
    assert imgs[0].get("phase") == 1
    assert not imgs[1].get("caption_template")
    assert imgs[1].get("answer") == "Units peaked in March."
    assert imgs[1].get("phase") == 1
    assert "narrative_template" not in rec and "phase2" not in rec


def test_content_is_marker_plus_template(client, events):
    """A multi turn whose phase-1 narrative was used: content = findings marker
    + template (brain-safe), the rendered narrative stored separately."""
    events.set(_narrative_turn())
    _, conv = _stream(client, "explain revenue")
    rec = _last_ai(conv)
    assert rec["content"] == narrative.history_content(T, F)
    assert rec["content"].startswith("[Findings: ")
    _no_sentinel(rec["content"], "content")
    assert rec.get("narrative_template") == T
    assert rec.get("narrative_rendered") == R
    assert rec.get("values") == V
    assert rec.get("findings") == F
    assert "phase2" not in rec


def test_stopped_record_uses_templates(client, events, monkeypatch):
    """A stopped turn's content is built from caption templates (or the
    describe text of an uncaptioned chart), never from the rendered answer;
    after the phase-1 narrative event it is the marker + template + note."""
    import routes.chat as chat_mod

    charts = [
        {"image_base64": "<div>plotly 1</div>", "answer": CAP_R,
         "caption_template": CAP, "phase": 1, "code": CODE1},
        {"image_base64": "<div>plotly 2</div>", "answer": "Units peaked in March.",
         "phase": 1, "code": CODE2},
    ]
    rec = chat_mod._build_stopped_record(None, [CODE1, CODE2], {}, charts)
    assert rec["content"] == CAP + "\n\nUnits peaked in March." + STOP_NOTE, rec["content"]
    _no_sentinel(rec["content"], "stopped content")
    assert rec["stopped"] is True
    assert [i.get("caption_template") for i in rec["images"]][0] == CAP

    # Route level: Stop lands right after the narrative event.
    calls = {"n": 0}

    def stop_after_narrative(conv_id):
        calls["n"] += 1
        return calls["n"] >= 2      # 1st event: the chart; 2nd: the narrative

    monkeypatch.setattr(chat_mod, "_is_cancelled", stop_after_narrative)
    events.set([
        _chart(1, answer=CAP_R, caption_template=CAP, code=CODE1, total=1),
        _narr_event(),
        _done(narrative.history_content(T, F), R, codes=(CODE1,), view=_view(True)),
    ])
    _, conv = _stream(client, "stop me")
    rec = _last_ai(conv)
    assert rec.get("stopped") is True
    assert rec["content"] == narrative.history_content(T, F) + STOP_NOTE, rec["content"]
    _no_sentinel(rec["content"], "stopped content")
    assert rec.get("narrative_template") == T
    assert rec.get("narrative_rendered") == R


def test_edit_regenerate_record_view_fields(client, world, events):
    """Edit-regenerate answers with the persisted record (`record_view`), its
    index in the served history (`ai_index`) and the DISPLAY text as `answer` —
    never the combined (marker + template) answer."""
    conv = _conversation(world["store"], [{"role": "human", "content": "q"},
                                          {"role": "ai", "content": "a"}])
    events.set(_narrative_turn())
    r = client.post(f"/api/chat/{CHAT}/edit-regenerate",
                    json={"edited_question": "again", "conv_id": conv},
                    headers=JSON_HEADERS)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    hist = _history(conv)
    assert data.get("ai_index") == len(hist) - 1, (data.get("ai_index"), len(hist))
    persisted = hist[data["ai_index"]]
    view = data.get("record_view")
    assert isinstance(view, dict), data
    for key in ("role", "content", "narrative_template", "narrative_rendered",
                "values", "findings", "code"):
        assert view.get(key) == persisted.get(key), key
    assert [(i.get("answer"), i.get("caption_template"), i.get("phase"))
            for i in view.get("images") or []] == \
        [(i.get("answer"), i.get("caption_template"), i.get("phase"))
         for i in persisted.get("images") or []]
    assert persisted["content"] == narrative.history_content(T, F)
    assert persisted.get("narrative_rendered") == R
    assert data["answer"] == R
    assert "{{" not in data["answer"] and "[Findings:" not in data["answer"]


def test_old_shape_record_fixture_loads(client, world):
    """History written by the previous release loads byte-identically through
    the history route, the brain-history sanitizer output for it is unchanged,
    and the new narrative-fields helper is a no-op on every old row."""
    import routes.chat as chat_mod

    raw_lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in raw_lines]
    store = world["store"]
    conv = store.new_conversation("old")
    (store.conversations_dir / f"{conv}.jsonl").write_text(
        "\n".join(raw_lines) + "\n", encoding="utf-8")
    local_store.AuthStore().record_conversation(OWNER, CHAT, conv, "old")

    r = client.get(f"/api/chat/{CHAT}/conversation/{conv}/history")
    assert r.status_code == 200, r.text[:300]
    assert r.json()["history"] == rows

    assert brain_client._sanitize_history_rows(rows) == [
        {"role": "ai", "content": "Welcome! Ask me anything about sales.csv."},
        {"role": "human", "content": "Show revenue by region"},
        {"role": "ai", "content": "North leads revenue with 1,200, followed by South.",
         "code": "RESULT = dfs['sales.csv'].groupby('region')['revenue'].sum().reset_index()"},
        {"role": "human", "content": "Chart revenue and units by month"},
        {"role": "ai", "content": "Revenue grew every month.\n\nUnits peaked in March.",
         "code": "fig = px.line(dfs['sales.csv'], x='month', y='revenue')"
                 "\n\n###NEXT_PLOT###\n\n"
                 "fig = px.bar(dfs['sales.csv'], x='month', y='units')"},
        {"role": "human", "content": "How many regions are there?"},
        {"role": "ai", "content": "There are 3 regions.",
         "code": "RESULT = dfs['sales.csv']['region'].nunique()"},
    ]

    for row in rows:
        for src in (None, {}):
            out = chat_mod._apply_narrative_fields(json.loads(json.dumps(row)), src)
            assert out == row, (row, src, out)


@pytest.mark.parametrize("flow", ["single", "multi"])
def test_title_gets_brain_view(client, world, events, monkeypatch, flow):
    """The /v1/title answer is `narrative.brain_view(template, findings)` when a
    template exists — slots as `[name]`, no rendered text, no value."""
    import routes.chat as chat_mod

    seen = []
    monkeypatch.setattr(chat_mod, "_start_title_generation",
                        lambda *a, **k: seen.append((a, k)))
    conv = _conversation(world["store"], [{"role": "human", "content": "first"},
                                          {"role": "ai", "content": "first answer"}])
    if flow == "single":
        events.set([{"single_response": True, "result": {
            "text": narrative.history_content(T, F), "image_base64": None,
            "table": None, "code": CODE1, "usage": {},
            "narrative_template": T, "narrative_rendered": R,
            "values": dict(V), "findings": dict(F)}}])
    else:
        events.set(_narrative_turn())
    _stream(client, "second question", conv_id=conv)
    assert len(seen) == 1, seen
    args, kwargs = seen[0]
    answer = kwargs.get("answer", args[4] if len(args) > 4 else None)
    assert answer == narrative.brain_view(T, F), answer
    _no_sentinel(answer, "title answer")
    assert "{{" not in answer


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def _narrative_images_record():
    return {"role": "ai", "content": narrative.history_content(T, F),
            "image_base64": None, "table": None, "code": CODE1, "usage": {},
            "narrative_template": T, "narrative_rendered": R,
            "values": dict(V), "findings": dict(F),
            "images": [
                {"image_base64": PNG, "answer": CAP_R, "caption_template": CAP,
                 "phase": 1, "code": CODE1},
                {"image_base64": PNG, "answer": "Units peaked in March.",
                 "phase": 1, "code": CODE2},
            ]}


def _narrative_single_record():
    return {"role": "ai", "content": narrative.history_content(T, F),
            "image_base64": PNG, "table": None, "code": CODE1, "usage": {},
            "narrative_template": T, "narrative_rendered": R,
            "values": dict(V), "findings": dict(F),
            "caption_template": CAP, "caption_rendered": CAP_R}


def test_report_brain_text_vs_local_display(client, world, monkeypatch):
    """`_build_qa_pairs`: `content` is the brain-safe `brain_view` text,
    `_display_text` the rendered one (the first image pair carries the
    narrative); the findings posted to /v1/report carry no value."""
    import routes.report as report_mod

    history = [{"role": "human", "content": "q1"}, _narrative_images_record(),
               {"role": "human", "content": "q2"}, _narrative_single_record()]
    pairs = report_mod._build_qa_pairs(history, "sid")
    assert len(pairs) == 3, pairs
    p0, p1, p2 = (p["ai"] for p in pairs)

    bv = narrative.brain_view(T, F)
    assert bv in p0["content"] and narrative.brain_view(CAP, F) in p0["content"]
    assert R in p0["_display_text"] and CAP_R in p0["_display_text"]
    assert p1["content"] == "Units peaked in March."
    assert p1["_display_text"] == "Units peaked in March."
    assert bv in p2["content"]
    assert R in p2["_display_text"] and CAP_R in p2["_display_text"]
    for p in (p0, p1, p2):
        _no_sentinel(p["content"], "pair content")
        assert "{{" not in p["content"]

    # What actually goes to the brain, through the route.
    store = world["store"]
    conv = _conversation(store, history)
    sent = []

    def fake_report(**kw):
        sent.append(kw)
        raise brain_client.BrainError("stop here")

    monkeypatch.setattr(report_mod.brain_client, "report", fake_report)
    r = client.post(f"/api/chat/{CHAT}/conversation/{conv}/download_report",
                    headers=JSON_HEADERS)
    assert r.status_code == 502, r.text[:200]
    assert len(sent) == 1
    findings = sent[0]["qa_pairs"]
    _no_sentinel(findings, "report findings")
    assert all("{{" not in f["answer_text"] for f in findings), findings
    assert bv[:60] in findings[0]["answer_text"]


def test_report_table_columns_only_input_names(client, world, monkeypatch):
    """`table_columns` keeps only names of the chat's input columns: pivot /
    crosstab headers are category values or dates, so they are removed and the
    list may become empty."""
    import routes.report as report_mod

    history = [
        {"role": "human", "content": "pivot"},
        {"role": "ai", "content": "Pivot shown.", "code": "RESULT = p",
         "table": {"columns": ["region", "SENTINEL_CATEGORY", "2024-01"],
                   "rows": [{"region": "North", "SENTINEL_CATEGORY": 1, "2024-01": 2}],
                   "total_rows": 1}},
        {"role": "human", "content": "crosstab"},
        {"role": "ai", "content": "Crosstab shown.", "code": "RESULT = c",
         "table": {"columns": ["North", "South"],
                   "rows": [{"North": 1, "South": 2}], "total_rows": 1}},
    ]
    conv = _conversation(world["store"], history)
    sent = []

    def fake_report(**kw):
        sent.append(kw)
        raise brain_client.BrainError("stop here")

    monkeypatch.setattr(report_mod.brain_client, "report", fake_report)
    r = client.post(f"/api/chat/{CHAT}/conversation/{conv}/download_report",
                    headers=JSON_HEADERS)
    assert r.status_code == 502, r.text[:200]
    findings = sent[0]["qa_pairs"]
    assert [f["table_columns"] for f in findings] == [["region"], []], findings
    _no_sentinel(findings, "report findings")


def test_report_paragraph_text_escaped(monkeypatch):
    """Every text handed to a ReportLab `Paragraph` is XML-escaped
    (`xml.sax.saxutils.escape`): VALUES text may come from a cell. The local
    finding body is the rendered `_display_text` when present."""
    import reportlab.platypus as platypus
    import routes.report as report_mod

    texts = []

    class FakeParagraph:
        def __init__(self, text, *a, **k):
            texts.append(text)

    class FakeDoc:
        def __init__(self, *a, **k):
            pass

        def build(self, flow):
            return None

    monkeypatch.setattr(platypus, "Paragraph", FakeParagraph)
    monkeypatch.setattr(platypus, "SimpleDocTemplate", FakeDoc)

    qa_pairs = [{"human": {"content": "Which region?"},
                 "ai": {"content": "Total was [total].",
                        "_display_text": "Total <b>& more</b> in A&B"},
                 "ai_index": 1, "has_chart": False, "has_table": False}]
    structure = {"report_title": "Sales <i>report</i>",
                 "executive_summary": "Sales & returns <rose>",
                 "findings": [], "key_takeaways": []}
    report_mod._render_pdf(qa_pairs, structure, "sid")

    assert texts, "no Paragraph was built"
    for t in texts:
        assert saxutils.escape(saxutils.unescape(t)) == t, f"unescaped Paragraph text: {t!r}"
    assert any("Total &lt;b&gt;&amp; more&lt;/b&gt; in A&amp;B" in t for t in texts), texts
    assert any("Sales &lt;i&gt;report&lt;/i&gt;" in t for t in texts), texts
