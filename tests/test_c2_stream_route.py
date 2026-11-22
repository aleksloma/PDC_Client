"""C2 (p29) — the chat SSE stream and edit-regenerate with the new events
(C2_build_spec §1, §2, §3; tests §6).

  §1 The opening "Working..." event becomes keyed: `{"progress": True,
     "message_key": "lab.stage_working", "message": "Working...", "conv_id"}`.
     A keyed stage event the flow pushes through `on_progress` reaches the
     browser as `{"progress", "message_key", "message", "conv_id"}` (plus
     `attempt` for `lab.stage_fixing`).
  §2 A `{"caption": True, "index": n, "text"}` event is forwarded as
     `{"caption": True, "index": n, "text", "conv_id"}` (never dropped as
     SSE_EVENT_UNKNOWN); the AI row stores the caption exactly as today
     (the chart's `answer` in `images[]`, `caption_described` for the one
     chart of a narrated turn whose planned caption did not render). Under the
     corrected §4 rule a narrated turn's visual WITHOUT a planned caption gets
     no describe and no text of its own. Stop / persistence paths accept
     the new events: a stopped turn keeps what arrived.
  §3 When no answer event (partial, narrative or done) has arrived after
     `settings.SLOW_ANSWER_NOTICE_SECONDS`, ONE keyed progress event
     `lab.taking_longer` is sent; never when an answer event came first.

Event order pinned: stage -> partial -> caption -> narrative -> done.

Readings:
  * a caption event's `index` is the chart's 1-based position (= `chart_n`);
    the scripts keep `chart_n == block_index`;
  * the generator YIELDS caption events (only yielded events reach the
    worker that persists — the stop path must keep them); stage events come
    through `on_progress`;
  * the slow-notice setting is read at request time from the `settings`
    instance (the suite's monkeypatch idiom), so a tiny value takes effect.

Offline: DATA_ROOT is tmp_path; `run_chat_multi_plot` is a scripted generator
(the tests/test_narrative_followup_sse.py harness), except the end-to-end
tests, which run the REAL flow with every brain call and render stubbed.
"""
import json
import secrets
import time

import pandas as pd
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
import narrative
import run_chat_local
from conftest import JSON_HEADERS
from settings import settings

OWNER = "owner@acme.com"
CHAT = "c_c2stream000001"

WORKING = {"progress": True, "message_key": "lab.stage_working", "message": "Working..."}
PLANNING = {"progress": True, "message_key": "lab.stage_planning",
            "message": "Planning the analysis…"}
RUNNING = {"progress": True, "message_key": "lab.stage_running",
           "message": "Running the analysis…"}
FIXING2 = {"progress": True, "message_key": "lab.stage_fixing",
           "message": "Fixing an error (attempt 2)…", "attempt": 2}
DESCRIBING = {"progress": True, "message_key": "lab.stage_describing",
              "message": "Describing the chart…"}
SUMMARY = {"progress": True, "message_key": "lab.stage_summary",
           "message": "Writing the summary…"}
TAKING_LONGER_KEY = "lab.taking_longer"
TAKING_LONGER_TEXT = "This is taking longer than usual. The analysis is still running."
STOP_NOTE = "Response stopped by user."

TCODE = "RESULT = dfs['sales.csv'].groupby('region')['amount'].sum().reset_index()"
TABLE = {"columns": ["region", "amount"],
         "rows": [{"region": "North", "amount": 3}, {"region": "South", "amount": 4}],
         "total_rows": 2}
NT = "Revenue was {{total}}."
NR = "Revenue was 7."


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(OWNER)
    store = local_store.ChatDataStore(CHAT)
    pd.DataFrame({"region": ["North", "South"], "amount": [1, 2]}).to_csv(
        store.files_dir / "sales.csv", index=False)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["title"] = "Sales"
    meta["files"] = [{"file_name": "sales.csv", "file_description": "sales",
                      "schema": {"fields": {"region": {}, "amount": {}}}}]
    store.write_meta(meta)
    yield {"store": store}
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def client(world):
    import routes.chat as chat_mod

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_" + secrets.token_hex(8)
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}", headers=JSON_HEADERS)
    return tc


@pytest.fixture
def gen(monkeypatch):
    """A scripted `run_chat_multi_plot`: records its kwargs, then runs
    `script(kw)` — a generator function the test sets."""
    import routes.chat as chat_mod

    class _Gen:
        def __init__(self):
            self.calls = []
            self.script = None
            self.yielded = 0

    g = _Gen()

    def fake(**kw):
        g.calls.append(kw)
        for ev in g.script(kw):
            g.yielded += 1
            yield ev

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot", fake)
    return g


@pytest.fixture
def logs(monkeypatch):
    import routes.chat as chat_mod
    seen = []
    real = chat_mod.log_with_sid

    def spy(sid, level, msg="", *a, **kw):
        seen.append(" ".join([str(msg)] + [str(x) for x in a]
                             + [f"{k}={v}" for k, v in kw.items()]))
        return real(sid, level, msg, *a, **kw)

    monkeypatch.setattr(chat_mod, "log_with_sid", spy)
    return seen


def _sse(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def _stream(client, question="q"):
    """ALL events, the opening one included, and the conv id."""
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": question},
                    headers=JSON_HEADERS)
    assert r.status_code == 200, r.text[:300]
    evs = _sse(r.text)
    conv = next(e["conv_id"] for e in evs if e.get("conv_id"))
    return evs, conv


def _last_ai(conv_id):
    rows = [r for r in local_store.ChatDataStore(CHAT).get_history(conv_id)
            if r.get("role") == "ai"]
    assert rows, "no AI record persisted"
    return rows[-1]


def _chart(n, *, answer="", caption_template=None, total=2):
    return {"partial": True, "phase": 1, "block_index": n,
            "image_base64": f"<div>plotly chart {n}</div>", "answer": answer,
            "caption_template": caption_template, "code": f"fig = chart_{n}",
            "chart_n": n, "chart_total": total, "usage": {}}


def _prog(kw, ev):
    """Push a progress event through the flow's on_progress (edit-regenerate
    is not streamed and need not pass one)."""
    cb = kw.get("on_progress")
    if callable(cb):
        cb(ev)


def _caption(n, text):
    return {"caption": True, "index": n, "text": text}


def _narr():
    return {"narrative": True, "phase": 1, "text": NR, "template": NT,
            "values": {"total": 7}, "findings": {}}


def _view(narrated=True):
    return {"phase1": ({"narrative_template": NT, "narrative_rendered": NR,
                        "values": {"total": 7}, "findings": {}} if narrated else None),
            "phase2": None, "phase2_status": None}


def _done(combined, display, codes, *, narrated=True):
    return {"done": True, "partial": False, "combined_answer": combined,
            "display_answer": display, "combined_codes": list(codes),
            "total_usage": {}, "narrative_view": _view(narrated)}


def _single(text="real answer", **extra):
    res = {"text": text, "image_base64": None, "table": None, "code": None, "usage": {}}
    res.update(extra)
    return {"single_response": True, "result": res}


def _idx(evs, pred, what):
    hits = [i for i, e in enumerate(evs) if pred(e)]
    assert hits, f"no {what} in {evs}"
    return hits[0]


def _key(k):
    return lambda e: e.get("progress") and e.get("message_key") == k


def _is_caption(n):
    return lambda e: e.get("caption") is True and e.get("index") == n


def _is_chart(n):
    return lambda e: e.get("partial") and e.get("image_base64") and e.get("chart_n") == n


# ===========================================================================
# §1 keyed stages
# ===========================================================================
def test_opening_event_is_the_keyed_working_stage(client, gen):
    gen.script = lambda kw: iter([_single()])
    evs, conv = _stream(client)
    assert evs[0] == {**WORKING, "conv_id": conv}, evs[0]


def test_stage_events_are_forwarded_keyed_with_conv_id(client, gen):
    def script(kw):
        for ev in (PLANNING, RUNNING, FIXING2, DESCRIBING, SUMMARY):
            _prog(kw, dict(ev))
        yield _single()

    gen.script = script
    evs, conv = _stream(client)
    keyed = [e for e in evs[1:] if e.get("progress")]
    assert [e["message_key"] for e in keyed] == [
        "lab.stage_planning", "lab.stage_running", "lab.stage_fixing",
        "lab.stage_describing", "lab.stage_summary"], keyed
    for sent, got in zip((PLANNING, RUNNING, FIXING2, DESCRIBING, SUMMARY), keyed):
        assert got == {**sent, "conv_id": conv}, got
    done = _idx(evs, lambda e: e.get("done"), "done")
    assert all(evs.index(e) < done for e in keyed)


def test_stage_events_are_live_ux_only(client, gen):
    def script(kw):
        _prog(kw, dict(PLANNING))
        _prog(kw, dict(RUNNING))
        yield _single("the answer")

    gen.script = script
    _evs, conv = _stream(client)
    rec = _last_ai(conv)
    assert rec["content"] == "the answer"
    assert "Planning" not in json.dumps(rec) and "Running the" not in json.dumps(rec)


# ===========================================================================
# §2 the caption event: forwarding, order, persistence
# ===========================================================================
def _two_chart_script(kw):
    # chart 1 stands for a visual whose PLANNED caption did not render (the
    # describe fallback, corrected §4); chart 2's caption rendered
    _prog(kw, dict(PLANNING))
    _prog(kw, dict(RUNNING))
    yield _chart(1)                                   # caption dropped: text follows
    _prog(kw, dict(DESCRIBING))
    yield _caption(1, "Desc one")
    yield _chart(2, answer="Cap two", caption_template="Cap {{x}}")
    _prog(kw, dict(SUMMARY))
    yield _narr()
    yield _done(narrative.history_content(NT, {}), NR, ("fig = chart_1", "fig = chart_2"))


def test_caption_event_forwarded_in_order(client, gen, logs):
    gen.script = _two_chart_script
    evs, conv = _stream(client)

    i_work = _idx(evs, _key("lab.stage_working"), "working")
    i_plan = _idx(evs, _key("lab.stage_planning"), "planning")
    i_run = _idx(evs, _key("lab.stage_running"), "running")
    i_p1 = _idx(evs, _is_chart(1), "chart 1")
    i_desc = _idx(evs, _key("lab.stage_describing"), "describing")
    i_cap = _idx(evs, _is_caption(1), "caption 1")
    i_p2 = _idx(evs, _is_chart(2), "chart 2")
    i_sum = _idx(evs, _key("lab.stage_summary"), "summary")
    i_narr = _idx(evs, lambda e: e.get("narrative") is True, "narrative")
    i_done = _idx(evs, lambda e: e.get("done"), "done")
    assert i_work == 0
    assert i_work < i_plan < i_run < i_p1 < i_desc < i_cap < i_p2 < i_sum < i_narr < i_done

    cap = evs[i_cap]
    assert cap == {"caption": True, "index": 1, "text": "Desc one", "conv_id": conv}, cap
    assert len([e for e in evs if e.get("done")]) == 1
    assert not [m for m in logs if "SSE_EVENT_UNKNOWN" in m], logs


def test_caption_persisted_as_the_charts_answer(client, gen):
    gen.script = _two_chart_script
    _evs, conv = _stream(client)
    rec = _last_ai(conv)
    imgs = rec.get("images")
    assert isinstance(imgs, list) and len(imgs) == 2, rec
    assert imgs[0]["answer"] == "Desc one"
    assert "caption_template" not in imgs[0]
    assert imgs[1]["answer"] == "Cap two"
    assert imgs[1]["caption_template"] == "Cap {{x}}"
    assert rec["content"] == narrative.history_content(NT, {})
    assert rec.get("narrative_rendered") == NR


def test_caption_of_single_chart_narrated_turn_is_caption_described(client, gen):
    def script(kw):
        yield _chart(1, total=1)
        yield _caption(1, "Desc one")
        yield _narr()
        yield _done(narrative.history_content(NT, {}), NR, ("fig = chart_1",))

    gen.script = script
    _evs, conv = _stream(client)
    rec = _last_ai(conv)
    assert not rec.get("images")
    assert rec.get("image_base64") == "<div>plotly chart 1</div>"
    assert rec.get("caption_described") == "Desc one", rec
    assert rec.get("narrative_rendered") == NR


def test_caption_of_no_narrative_turn_reaches_the_record(client, gen):
    def script(kw):
        yield _chart(1, total=1)
        yield _caption(1, "Desc one")
        yield _done("Desc one", "Desc one", ("fig = chart_1",), narrated=False)

    gen.script = script
    evs, conv = _stream(client)
    assert [e for e in evs if e.get("caption") is True]
    rec = _last_ai(conv)
    assert rec["content"] == "Desc one"
    assert rec.get("image_base64") == "<div>plotly chart 1</div>"
    assert "caption_described" not in rec          # no narrative: today's shape


def test_stop_after_caption_keeps_it(client, gen, monkeypatch):
    """Stop right after chart 2 (3 events in: chart 1, its caption, chart 2):
    the stopped record keeps the caption that arrived for chart 1."""
    import routes.chat as chat_mod

    monkeypatch.setattr(chat_mod, "_is_cancelled", lambda conv_id: gen.yielded >= 3)
    gen.script = _two_chart_script
    evs, conv = _stream(client)

    dones = [e for e in evs if e.get("done")]
    assert len(dones) == 1
    assert "Desc one" in dones[0]["answer"], dones[0]["answer"]
    rec = _last_ai(conv)
    assert rec.get("stopped") is True
    imgs = rec.get("images")
    assert isinstance(imgs, list) and len(imgs) == 2, rec
    assert imgs[0]["answer"] == "Desc one"
    assert rec["content"].startswith("Desc one"), rec["content"]
    assert rec["content"].endswith(STOP_NOTE)


def test_stop_single_chart_after_caption_keeps_it(client, gen, monkeypatch):
    import routes.chat as chat_mod

    def script(kw):
        yield _chart(1, total=1)
        yield _caption(1, "Desc one")
        yield _done("Desc one", "Desc one", ("fig = chart_1",), narrated=False)

    monkeypatch.setattr(chat_mod, "_is_cancelled", lambda conv_id: gen.yielded >= 2)
    gen.script = script
    _evs, conv = _stream(client)
    rec = _last_ai(conv)
    assert rec.get("stopped") is True
    assert rec.get("image_base64") == "<div>plotly chart 1</div>"
    assert rec["content"].startswith("Desc one"), rec["content"]


def _tables_partial():
    return {"partial": True, "phase": 1, "tables": [dict(TABLE)],
            "table_codes": [TCODE], "table_result_keys": [None]}


def test_single_table_partial_then_text(client, gen):
    """The table travels as a tables partial right after execution; the text
    follows; the record holds the table ONCE."""
    def script(kw):
        yield _tables_partial()
        yield _single("described", table=dict(TABLE), code=TCODE)

    gen.script = script
    evs, conv = _stream(client)
    i_tbl = _idx(evs, lambda e: e.get("partial") and e.get("tables"), "tables partial")
    i_done = _idx(evs, lambda e: e.get("done"), "done")
    assert i_tbl < i_done
    assert evs[i_tbl]["tables"][0]["columns"] == TABLE["columns"]
    assert evs[i_done]["answer"] == "described"
    rec = _last_ai(conv)
    n_tables = (1 if rec.get("table") else 0) + len(rec.get("tables") or [])
    assert n_tables == 1, rec
    assert rec["content"] == "described"


def test_stop_after_single_table_partial_keeps_the_table(client, gen, monkeypatch):
    import routes.chat as chat_mod

    def script(kw):
        yield _tables_partial()
        yield _single("described", table=dict(TABLE), code=TCODE)

    monkeypatch.setattr(chat_mod, "_is_cancelled", lambda conv_id: gen.yielded >= 1)
    gen.script = script
    _evs, conv = _stream(client)
    rec = _last_ai(conv)
    assert rec.get("stopped") is True
    assert rec.get("tables") and rec["tables"][0]["columns"] == TABLE["columns"], rec
    assert rec.get("full_table_keys") and rec["full_table_keys"][0]


# ===========================================================================
# §2 edit-regenerate accepts the caption event
# ===========================================================================
def _seed_conv(store):
    conv = store.new_conversation("t")
    store.append_history(conv, {"role": "human", "content": "q"})
    store.append_history(conv, {"role": "ai", "content": "a"})
    local_store.AuthStore().record_conversation(OWNER, CHAT, conv, "t")
    return conv


def _edit(client, conv):
    return client.post(f"/api/chat/{CHAT}/edit-regenerate",
                       json={"edited_question": "again", "conv_id": conv},
                       headers=JSON_HEADERS)


def test_edit_regenerate_persists_caption_events(client, world, gen):
    conv = _seed_conv(world["store"])
    gen.script = _two_chart_script
    r = _edit(client, conv)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    view = data["record_view"]
    assert [img["answer"] for img in view["images"]] == ["Desc one", "Cap two"]
    stored = world["store"].get_history(conv)[data["ai_index"]]
    assert [img["answer"] for img in stored["images"]] == ["Desc one", "Cap two"]
    assert data["answer"] == NR


def test_edit_regenerate_single_chart_caption_described(client, world, gen):
    conv = _seed_conv(world["store"])

    def script(kw):
        yield _chart(1, total=1)
        yield _caption(1, "Desc one")
        yield _narr()
        yield _done(narrative.history_content(NT, {}), NR, ("fig = chart_1",))

    gen.script = script
    r = _edit(client, conv)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert data["record_view"].get("caption_described") == "Desc one"
    stored = world["store"].get_history(conv)[data["ai_index"]]
    assert stored.get("caption_described") == "Desc one"


# ===========================================================================
# §3 "taking longer than usual"
# ===========================================================================
def _slow(monkeypatch, seconds):
    monkeypatch.setattr(settings, "SLOW_ANSWER_NOTICE_SECONDS", seconds)


def _notices(evs):
    return [e for e in evs if e.get("progress") and e.get("message_key") == TAKING_LONGER_KEY]


def test_slow_notice_sent_once_when_nothing_arrives(client, gen, monkeypatch):
    _slow(monkeypatch, 0.3)

    def script(kw):
        time.sleep(1.5)
        yield _single("late answer")

    gen.script = script
    evs, conv = _stream(client)
    notes = _notices(evs)
    assert len(notes) == 1, evs
    assert notes[0] == {"progress": True, "message_key": TAKING_LONGER_KEY,
                        "message": TAKING_LONGER_TEXT, "conv_id": conv}, notes[0]
    assert evs.index(notes[0]) < _idx(evs, lambda e: e.get("done"), "done")
    # live UX only: the record is the answer
    assert _last_ai(conv)["content"] == "late answer"


def test_stage_events_do_not_count_as_an_answer(client, gen, monkeypatch):
    _slow(monkeypatch, 0.3)

    def script(kw):
        _prog(kw, dict(PLANNING))
        time.sleep(1.5)
        yield _single("late answer")

    gen.script = script
    evs, _conv = _stream(client)
    assert len(_notices(evs)) == 1, evs
    assert _idx(evs, _key("lab.stage_planning"), "planning") < evs.index(_notices(evs)[0])


def test_no_slow_notice_after_a_partial(client, gen, monkeypatch):
    _slow(monkeypatch, 0.4)

    def script(kw):
        yield _chart(1, answer="Chart one.", total=1)
        time.sleep(1.5)
        yield _done("Chart one.", "Chart one.", ("fig = chart_1",), narrated=False)

    gen.script = script
    evs, _conv = _stream(client)
    assert _notices(evs) == [], evs


def test_no_slow_notice_after_a_narrative(client, gen, monkeypatch):
    _slow(monkeypatch, 0.4)

    def script(kw):
        yield _narr()
        time.sleep(1.5)
        yield _done(narrative.history_content(NT, {}), NR, ())

    gen.script = script
    evs, _conv = _stream(client)
    assert _notices(evs) == [], evs


def test_no_slow_notice_for_a_fast_answer_and_no_wait_for_the_timer(client, gen,
                                                                    monkeypatch):
    _slow(monkeypatch, 30)
    gen.script = lambda kw: iter([_single("fast")])
    started = time.monotonic()
    evs, _conv = _stream(client)
    assert time.monotonic() - started < 10, "the stream waited for the notice timer"
    assert _notices(evs) == []


def test_slow_notice_setting_default_and_env(monkeypatch):
    from settings import Settings
    monkeypatch.delenv("SLOW_ANSWER_NOTICE_SECONDS", raising=False)
    assert Settings().SLOW_ANSWER_NOTICE_SECONDS == 90
    monkeypatch.setenv("SLOW_ANSWER_NOTICE_SECONDS", "15")
    assert Settings().SLOW_ANSWER_NOTICE_SECONDS == 15


# ===========================================================================
# end to end: the REAL flow behind the route
# ===========================================================================
CODE_A = "fig = chart_a\nVALUES['a'] = 1"
CODE_B = "fig = chart_b\nVALUES['b'] = 2"


def _raw(*codes):
    return "\n###NEXT_PLOT###\n".join(f"```plot_code\n{c}\n```" for c in codes) \
        + "\n###NEXT_PLOT###\n"


def _rendered(values):
    return {"ok": True, "error": None, "is_plotly": True,
            "plotly_html": "<div>chart</div>", "chart_data": None,
            "values": dict(values), "values_dropped": 0,
            "findings": {}, "findings_dropped": 0}


CODE_BN = "fig = chart_bn\nVALUES['b'] = None"     # its planned caption drops (n/a)
CODE_3 = "fig = chart_3\nVALUES['z'] = None"


def _install_real_flow(monkeypatch, plan, charts, describe_text):
    bc = run_chat_local.brain_client
    describes = []
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", "narrative_v1,diagnostics_v1")
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")
    monkeypatch.setattr(bc, "plan", lambda **kw: dict(plan))
    monkeypatch.setattr(bc, "retry", lambda **kw: {"kind": "NO_CODE", "code": "", "usage": {}})

    def describe(**kw):
        describes.append(kw)
        return {"text": describe_text, "usage": {}}

    monkeypatch.setattr(bc, "describe", describe)
    monkeypatch.setattr(bc, "summarize", lambda **kw: {"text": "summarized", "usage": {}})
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        lambda code, dfs, sid, **kw: dict(charts[code]))
    return describes


@pytest.fixture
def real_flow(monkeypatch):
    """Brain + renders stubbed under the REAL run_chat_multi_plot, a narrated
    turn ("N 1."): block 1's planned caption renders, block 2's planned
    caption is DROPPED (all n/a) -> block 2 keeps describe (corrected §4)."""
    plan = {"raw_text": _raw(CODE_A, CODE_BN), "kind": "PLOT_CODE", "code": "",
            "usage": {}, "narrative": "N {{a}}.",
            "captions": {"1": "cap {{a}}", "2": "{{b}} here"}}
    return _install_real_flow(monkeypatch, plan,
                              {CODE_A: _rendered({"a": 1}), CODE_BN: _rendered({"b": None})},
                              "described B")


def test_real_flow_event_order_through_the_route(client, real_flow, logs):
    evs, conv = _stream(client, "show sales")

    assert evs[0] == {**WORKING, "conv_id": conv}
    i_plan = _idx(evs, _key("lab.stage_planning"), "planning")
    i_run = _idx(evs, _key("lab.stage_running"), "running")
    i_p1 = _idx(evs, _is_chart(1), "chart 1")
    i_p2 = _idx(evs, _is_chart(2), "chart 2")
    i_cap = _idx(evs, _is_caption(2), "caption 2")
    i_narr = _idx(evs, lambda e: e.get("narrative") is True, "narrative")
    i_done = _idx(evs, lambda e: e.get("done"), "done")
    assert 0 < i_plan < i_run < i_p1 < i_p2 < i_cap < i_narr < i_done
    desc = [i for i, e in enumerate(evs) if _key("lab.stage_describing")(e)]
    assert any(i_p2 < i < i_cap for i in desc), evs
    summ = [i for i, e in enumerate(evs) if _key("lab.stage_summary")(e)]
    assert any(i < i_narr for i in summ), evs

    assert evs[i_cap] == {"caption": True, "index": 2, "text": "described B",
                          "conv_id": conv}
    assert not [e for e in evs if e.get("caption") is True and e.get("index") == 1]
    assert evs[i_p1]["answer"] == "cap 1"
    assert "described B" not in (evs[i_p2].get("answer") or "")
    assert evs[i_done]["answer"] == "N 1."
    assert not [m for m in logs if "SSE_EVENT_UNKNOWN" in m], logs

    rec = _last_ai(conv)
    assert [img["answer"] for img in rec["images"]] == ["cap 1", "described B"]
    assert rec["narrative_rendered"] == "N 1."
    assert rec["content"] == narrative.history_content("N {{a}}.", {})


def test_real_flow_edit_regenerate_record(client, world, real_flow):
    conv = _seed_conv(world["store"])
    r = _edit(client, conv)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert [img["answer"] for img in data["record_view"]["images"]] == \
        ["cap 1", "described B"]
    assert data["answer"] == "N 1."


@pytest.fixture
def three_chart_flow(monkeypatch):
    """Narrated turn ("N 1 2."): chart 1's planned caption renders, chart 2
    had NO planned caption, chart 3's planned caption is dropped (n/a)."""
    plan = {"raw_text": _raw(CODE_A, CODE_B, CODE_3), "kind": "PLOT_CODE", "code": "",
            "usage": {}, "narrative": "N {{a}} {{b}}.",
            "captions": {"1": "cap {{a}}", "3": "{{z}} here"}}
    return _install_real_flow(
        monkeypatch, plan,
        {CODE_A: _rendered({"a": 1}), CODE_B: _rendered({"b": 2}),
         CODE_3: _rendered({"z": None})},
        "described 3")


def test_real_flow_three_charts_only_the_dropped_caption_is_described(
        client, three_chart_flow, logs):
    describes = three_chart_flow
    evs, conv = _stream(client, "show sales")

    assert [kw["code"] for kw in describes] == [CODE_3]
    caps = [e for e in evs if e.get("caption") is True]
    assert caps == [{"caption": True, "index": 3, "text": "described 3", "conv_id": conv}]
    assert len([e for e in evs if _key("lab.stage_describing")(e)]) == 1
    i_p3 = _idx(evs, _is_chart(3), "chart 3")
    assert i_p3 < evs.index(caps[0]) < _idx(evs, lambda e: e.get("done"), "done")
    charts = [e for e in evs if e.get("partial") and e.get("image_base64")]
    assert [c["chart_n"] for c in charts] == [1, 2, 3]
    assert charts[0]["answer"] == "cap 1"
    assert not (charts[1].get("answer") or "")
    done = [e for e in evs if e.get("done")][0]
    assert done["answer"] == "N 1 2."
    assert not [m for m in logs if "SSE_EVENT_UNKNOWN" in m], logs

    # the stored record: no text for chart 2
    rec = _last_ai(conv)
    imgs = rec["images"]
    assert len(imgs) == 3
    assert imgs[0]["answer"] == "cap 1"
    assert not (imgs[1].get("answer") or "")
    assert imgs[2]["answer"] == "described 3"
    assert rec["narrative_rendered"] == "N 1 2."
    assert rec["content"] == narrative.history_content("N {{a}} {{b}}.", {})

    # history reload serves the row without crashing
    r = client.get(f"/api/chat/{CHAT}/conversation/{conv}/history")
    assert r.status_code == 200, r.text[:300]
    served = [row for row in r.json()["history"] if row.get("role") == "ai"][-1]
    assert [bool(i.get("image_base64")) for i in served["images"]] == [True, True, True]
    assert not (served["images"][1].get("answer") or "")
    assert served["narrative_rendered"] == "N 1 2."
    # the legacy history route too
    r = client.get(f"/api/chat/{CHAT}/history")
    assert r.status_code == 200, r.text[:300]


def test_real_flow_three_charts_edit_regenerate(client, world, three_chart_flow):
    describes = three_chart_flow
    conv = _seed_conv(world["store"])
    r = _edit(client, conv)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert [kw["code"] for kw in describes] == [CODE_3]
    answers = [img.get("answer") or "" for img in data["record_view"]["images"]]
    assert answers == ["cap 1", "", "described 3"]
    assert data["answer"] == "N 1 2."
    # reload of the edited conversation
    r = client.get(f"/api/chat/{CHAT}/conversation/{conv}/history")
    assert r.status_code == 200, r.text[:300]
