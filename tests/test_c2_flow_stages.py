"""C2 (p29) — the `run_chat_local` flows: keyed stage events, early visuals,
the `caption` event, and describe only where needed (C2_build_spec §1, §2,
§4; tests §6).

Spec under test:
  §1 Keyed progress events `{"progress": True, "message_key", "message"}`
     reach the page through the flow's `on_progress` path:
       lab.stage_planning   — before the plan call ("Planning the analysis…")
       lab.stage_running    — before execution ("Running the analysis…")
       lab.stage_fixing     — before an exec-error `/v1/retry`, with `attempt` n
                              ("Fixing an error (attempt n)…")
       lab.stage_describing — before a describe call
       lab.stage_summary    — before the summary / narrative render
  §2 A chart partial is sent right after execution; its describe text follows
     in a `{"caption": True, "index": n, "text": …}` event. A single table
     answer sends the table as a TABLES partial right after execution; its
     text follows.
  §4 (CORRECTED rule, coordinator 2026-11-22) In a turn WITH a rendered
     narrative a visual gets a describe call ONLY when the plan delivered a
     caption template for it that did not render (dropped / invalid / too
     long / code-in-text / all n/a). A visual the plan gave NO caption gets
     no describe, no caption event and no `lab.stage_describing` (the
     narrative's last sentence explains it). A rendered caption -> no
     describe. Turns WITHOUT a rendered narrative -> describe as today.

Channel-agnostic on purpose: a progress, caption or tables-partial event is
accepted whether the flow YIELDS it or pushes it through `on_progress` — the
spec fixes the event shapes and their order, not the channel (the chart
partial itself is always yielded: the stream worker persists it). Order is
read from ONE timeline that records brain calls, executor calls, yielded
events and `on_progress` events as they happen.

Readings where the spec leaves room:
  * the caption event's `index` is the chart's 1-based position (`chart_n`);
    every scenario here keeps `chart_n == block_index` so either reading holds;
  * "before execution" also means after the plan call (planning, then running);
  * `lab.stage_fixing` is pinned for the EXEC-ERROR retries only (the
    narrative-only retry keeps its own `lab.narrative_retry`).

Offline: every brain call stubbed on `run_chat_local.brain_client`; chart
renders stubbed per code text; single-flow python runs in process (conftest).
Run: `.venv\\Scripts\\python.exe -m pytest tests/test_c2_flow_stages.py -q`.
"""
import pandas as pd
import pytest

import narrative
import run_chat_local
from settings import settings

CAPS = "narrative_v1,diagnostics_v1"

PLANNING_KEY, PLANNING_TEXT = "lab.stage_planning", "Planning the analysis…"
RUNNING_KEY, RUNNING_TEXT = "lab.stage_running", "Running the analysis…"
FIXING_KEY = "lab.stage_fixing"
DESCRIBING_KEY = "lab.stage_describing"
SUMMARY_KEY = "lab.stage_summary"


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def dfs():
    return {"f.csv": pd.DataFrame({"region": ["North", "South", "East"],
                                   "amount": [10, 20, 30]})}


@pytest.fixture(autouse=True)
def _stub_schema(monkeypatch):
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")


@pytest.fixture(autouse=True)
def _caps_on(monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", CAPS)


# ------------------------------------------------------------- the timeline

class Timeline:
    """One ordered record of everything that happens during a turn."""

    def __init__(self):
        self.items = []          # (kind, payload): brain / exec / ev / cb

    # the on_progress callback handed to the flow
    def on_progress(self, ev):
        self.items.append(("cb", dict(ev) if isinstance(ev, dict) else ev))

    # --- queries ---------------------------------------------------------
    def events(self):
        """Yielded events, in order."""
        return [p for k, p in self.items if k == "ev"]

    def _event_entries(self):
        """(index, event) of every event the page could see: yielded or
        pushed through on_progress."""
        return [(i, p) for i, (k, p) in enumerate(self.items)
                if k in ("ev", "cb") and isinstance(p, dict)]

    def progress(self, key):
        return [(i, e) for i, e in self._event_entries()
                if e.get("progress") and e.get("message_key") == key]

    def captions(self):
        return [(i, e) for i, e in self._event_entries() if e.get("caption") is True]

    def tables_partials(self):
        return [(i, e) for i, e in self._event_entries()
                if e.get("partial") and not e.get("image_base64") and e.get("tables")]

    def chart_partials(self):
        return [(i, p) for i, (k, p) in enumerate(self.items)
                if k == "ev" and p.get("partial") and p.get("image_base64")]

    def narratives(self):
        return [(i, p) for i, (k, p) in enumerate(self.items)
                if k == "ev" and p.get("narrative")]

    def final(self):
        """(index, event) of the final answer event (single_response / done)."""
        hits = [(i, p) for i, (k, p) in enumerate(self.items)
                if k == "ev" and (p.get("single_response") or p.get("done"))]
        assert len(hits) == 1, self.events()
        return hits[0]

    def brain(self, name):
        return [(i, p) for i, (k, p) in enumerate(self.items)
                if k == "brain" and p[0] == name]

    def execs(self):
        return [i for i, (k, _p) in enumerate(self.items) if k == "exec"]


class Brain:
    """Recording brain stubs writing into the timeline."""

    def __init__(self, tl, plan, retry=None, describe_text="described",
                 summarize_text="summarized"):
        self.tl = tl
        self.plan_reply = plan
        self.retry_reply = retry
        self.describe_text = describe_text
        self.summarize_text = summarize_text

    def install(self, monkeypatch):
        bc = run_chat_local.brain_client
        tl = self.tl

        def plan(**kw):
            tl.items.append(("brain", ("plan", kw)))
            return dict(self.plan_reply)

        def retry(**kw):
            tl.items.append(("brain", ("retry", kw)))
            r = self.retry_reply
            if callable(r):
                return r(kw)
            return dict(r or {"kind": "NO_CODE", "code": "", "usage": {}})

        def describe(**kw):
            tl.items.append(("brain", ("describe", kw)))
            t = self.describe_text
            return {"text": t(kw) if callable(t) else t, "usage": {}}

        def summarize(**kw):
            tl.items.append(("brain", ("summarize", kw)))
            return {"text": self.summarize_text, "usage": {}}

        monkeypatch.setattr(bc, "plan", plan)
        monkeypatch.setattr(bc, "retry", retry)
        monkeypatch.setattr(bc, "describe", describe)
        monkeypatch.setattr(bc, "summarize", summarize)
        return self


def _install_execs(tl, monkeypatch, charts=None, tables=None, real_python=False):
    """Stub the two executors by code text (recording each call); with
    `real_python` the python executor runs the code in process."""
    real_exec = run_chat_local.safe_execute

    def render(code, dfs, sid, **kw):
        tl.items.append(("exec", code))
        out = (charts or {}).get(code)
        return dict(out) if out is not None else {"ok": False, "error": "ValueError: boom",
                                                  "trace": ""}

    def execute(code, dfs, sid, **kw):
        tl.items.append(("exec", code))
        if real_python:
            return real_exec(code, dfs, sid, **kw)
        out = (tables or {}).get(code)
        return dict(out) if out is not None else {"error": "ValueError: boom"}

    monkeypatch.setattr(run_chat_local, "render_plot_safe", render)
    monkeypatch.setattr(run_chat_local, "safe_execute", execute)


def _drive(tl, dfs, question="show sales"):
    for ev in run_chat_local.run_chat_multi_plot(
            sid="t", dfs=dfs, schema_docs={}, question=question,
            history_rows=[], user_email="alice@acme.com",
            on_progress=tl.on_progress):
        tl.items.append(("ev", ev))
    return tl


def _raw(*blocks):
    body = "\n###NEXT_PLOT###\n".join(f"```{fence}\n{code}\n```" for fence, code in blocks)
    return body + "\n###NEXT_PLOT###\n"


def _multi_plan(*blocks, narrative_t=None, captions=None):
    out = {"raw_text": _raw(*blocks), "kind": "PLOT_CODE", "code": "", "usage": {}}
    if narrative_t is not None:
        out["narrative"] = narrative_t
    if captions is not None:
        out["captions"] = captions
    return out


def _chart(values=None, findings=None, chart_data=None):
    return {"ok": True, "error": None, "is_plotly": True,
            "plotly_html": "<div>chart</div>", "chart_data": chart_data,
            "values": dict(values or {}), "values_dropped": 0,
            "findings": dict(findings or {}), "findings_dropped": 0}


def _first(seq, what):
    assert seq, f"no {what} in the timeline"
    return seq[0]


def _final_text(tl):
    _i, ev = tl.final()
    if ev.get("single_response"):
        return (ev.get("result") or {}).get("text", "")
    return ev.get("display_answer", "")


CODE_A = "fig = chart_a\nVALUES['a'] = 1"
CODE_B = "fig = chart_b\nVALUES['b'] = 2"
# block whose planned caption renders only "n/a" (its value is None): the
# quality gate drops it -> describe is its fallback, even in a narrated turn
CODE_BN = "fig = chart_bn\nVALUES['b'] = None"
CODE_C = "fig = chart_c\nVALUES['c'] = 3"
TABLE_CODE = "RESULT = dfs['f.csv']"
SCALAR_CODE = "RESULT = 42"
FAILING_CODE = "RESULT = undefined_name_xyz"
STILL_FAILING = "RESULT = still_undefined_abc"
FIXED_CODE = "RESULT = dfs['f.csv']"
NARR_T = "N {{a}}."
NARR_R = "N 1."


def _two_chart_narrated(tl, monkeypatch, describe_text="described B"):
    """A narrated turn (renders "N 1."): block 1's planned caption renders;
    block 2's planned caption is DROPPED (all n/a), so block 2 — and only
    block 2 — keeps a describe call (corrected §4 rule)."""
    brain = Brain(tl, _multi_plan(("plot_code", CODE_A), ("plot_code", CODE_BN),
                                  narrative_t=NARR_T,
                                  captions={"1": "cap {{a}}", "2": "{{b}} here"}),
                  describe_text=describe_text).install(monkeypatch)
    _install_execs(tl, monkeypatch, {CODE_A: _chart({"a": 1}), CODE_BN: _chart({"b": None})})
    return brain


# ================================================================ §1 stages

@pytest.mark.parametrize("shape", ["multi_chart", "single_table", "single_scalar"])
def test_planning_stage_precedes_the_plan_call(monkeypatch, dfs, shape):
    tl = Timeline()
    if shape == "multi_chart":
        _two_chart_narrated(tl, monkeypatch)
    else:
        code = TABLE_CODE if shape == "single_table" else SCALAR_CODE
        Brain(tl, {"kind": "PYTHON", "code": code, "raw_text": "", "usage": {}}
              ).install(monkeypatch)
        _install_execs(tl, monkeypatch, real_python=True)
    _drive(tl, dfs)

    i_plan, _ = _first(tl.brain("plan"), "plan call")
    i_stage, ev = _first(tl.progress(PLANNING_KEY), PLANNING_KEY)
    assert i_stage < i_plan, tl.items[:4]
    assert ev["progress"] is True
    assert ev["message"] == PLANNING_TEXT


@pytest.mark.parametrize("shape", ["multi_chart", "single_table"])
def test_running_stage_after_plan_before_first_execution(monkeypatch, dfs, shape):
    tl = Timeline()
    if shape == "multi_chart":
        _two_chart_narrated(tl, monkeypatch)
    else:
        Brain(tl, {"kind": "PYTHON", "code": TABLE_CODE, "raw_text": "", "usage": {}}
              ).install(monkeypatch)
        _install_execs(tl, monkeypatch, real_python=True)
    _drive(tl, dfs)

    i_plan, _ = _first(tl.brain("plan"), "plan call")
    i_exec = _first(tl.execs(), "execution")
    running = [(i, e) for i, e in tl.progress(RUNNING_KEY) if i_plan < i < i_exec]
    assert running, ("no lab.stage_running between the plan call and the first "
                     f"execution: {tl.items}")
    assert running[0][1]["message"] == RUNNING_TEXT
    assert running[0][1]["progress"] is True


def _assert_fixing_before_retries(tl, n_retries):
    retries = tl.brain("retry")
    execs = tl.execs()
    assert len(retries) == n_retries, [p[0] for _i, p in retries]
    for attempt in range(1, n_retries + 1):
        i_retry = retries[attempt - 1][0]
        # the execution that failed right before this retry
        i_failed = max(i for i in execs if i < i_retry)
        hits = [e for i, e in tl.progress(FIXING_KEY)
                if i_failed < i < i_retry and e.get("attempt") == attempt]
        assert hits, (f"no lab.stage_fixing attempt={attempt} between the failed "
                      f"execution and retry {attempt}: {tl.items}")
        msg = hits[0]["message"]
        assert msg.startswith("Fixing an error"), msg
        assert str(attempt) in msg, msg
        assert hits[0]["progress"] is True


def test_fixing_stage_before_each_exec_retry_single_flow(monkeypatch, dfs):
    replies = iter([{"kind": "PYTHON", "code": STILL_FAILING, "usage": {}},
                    {"kind": "PYTHON", "code": FIXED_CODE, "usage": {}}])
    tl = Timeline()
    Brain(tl, {"kind": "PYTHON", "code": FAILING_CODE, "raw_text": "", "usage": {}},
          retry=lambda kw: next(replies)).install(monkeypatch)
    _install_execs(tl, monkeypatch, real_python=True)
    _drive(tl, dfs)

    _assert_fixing_before_retries(tl, 2)
    assert _final_text(tl) == "described"


def test_fixing_stage_before_exec_retry_multi_flow(monkeypatch, dfs):
    fixed = "fig = chart_a_fixed\nVALUES['a'] = 1"
    tl = Timeline()
    Brain(tl, _multi_plan(("plot_code", "fig = chart_broken"), ("plot_code", CODE_B)),
          retry={"kind": "PLOT_CODE", "code": fixed, "usage": {}}).install(monkeypatch)
    _install_execs(tl, monkeypatch, {fixed: _chart({"a": 1}), CODE_B: _chart({"b": 2})})
    _drive(tl, dfs)

    _assert_fixing_before_retries(tl, 1)
    assert len(tl.chart_partials()) == 2


@pytest.mark.parametrize("shape", ["multi_dropped_caption_chart", "single_table"])
def test_describing_stage_precedes_every_describe_call(monkeypatch, dfs, shape):
    tl = Timeline()
    if shape == "multi_dropped_caption_chart":
        _two_chart_narrated(tl, monkeypatch)
    else:
        Brain(tl, {"kind": "PYTHON", "code": TABLE_CODE, "raw_text": "", "usage": {}}
              ).install(monkeypatch)
        _install_execs(tl, monkeypatch, real_python=True)
    _drive(tl, dfs)

    describes = tl.brain("describe")
    assert len(describes) == 1
    i_desc = describes[0][0]
    i_last_exec = max(i for i in tl.execs() if i < i_desc)
    hits = [i for i, _e in tl.progress(DESCRIBING_KEY) if i_last_exec < i < i_desc]
    assert hits, f"no lab.stage_describing right before the describe call: {tl.items}"


def test_summary_stage_precedes_summarize_call(monkeypatch, dfs):
    tl = Timeline()
    Brain(tl, {"kind": "PYTHON", "code": SCALAR_CODE, "raw_text": "", "usage": {}}
          ).install(monkeypatch)
    _install_execs(tl, monkeypatch, real_python=True)
    _drive(tl, dfs, question="what is the total?")

    i_sum, _ = _first(tl.brain("summarize"), "summarize call")
    i_exec = max(i for i in tl.execs() if i < i_sum)
    hits = [i for i, _e in tl.progress(SUMMARY_KEY) if i_exec < i < i_sum]
    assert hits, f"no lab.stage_summary between execution and summarize: {tl.items}"


def test_summary_stage_precedes_the_multi_narrative(monkeypatch, dfs):
    tl = Timeline()
    _two_chart_narrated(tl, monkeypatch)
    _drive(tl, dfs)

    i_narr, ev = _first(tl.narratives(), "narrative event")
    assert ev["text"] == NARR_R
    i_last_exec = max(tl.execs())
    hits = [i for i, _e in tl.progress(SUMMARY_KEY) if i_last_exec < i < i_narr]
    assert hits, f"no lab.stage_summary before the narrative event: {tl.items}"


def test_summary_stage_precedes_the_single_narrative_answer(monkeypatch, dfs):
    tl = Timeline()
    Brain(tl, {"kind": "PYTHON", "code": "VALUES['total'] = 5\nRESULT = 5",
               "raw_text": "", "usage": {}, "narrative": "Total: {{total}}."}
          ).install(monkeypatch)
    _install_execs(tl, monkeypatch, real_python=True)
    _drive(tl, dfs, question="what is the total?")

    i_final, ev = tl.final()
    assert ev.get("single_response")
    assert ev["result"].get("narrative_rendered") == "Total: 5."
    i_exec = max(tl.execs())
    hits = [i for i, _e in tl.progress(SUMMARY_KEY) if i_exec < i < i_final]
    assert hits, f"no lab.stage_summary before the narrated single answer: {tl.items}"


def test_stage_events_carry_only_the_keyed_progress_shape(monkeypatch, dfs):
    """Every stage event the flow emits is a keyed progress event — no
    template, VALUES, FINDINGS or data rides it (Article II: display only)."""
    tl = Timeline()
    _two_chart_narrated(tl, monkeypatch)
    _drive(tl, dfs)

    stage_keys = {PLANNING_KEY, RUNNING_KEY, FIXING_KEY, DESCRIBING_KEY, SUMMARY_KEY}
    seen = set()
    for _i, e in tl._event_entries():
        if e.get("progress") and e.get("message_key") in stage_keys:
            seen.add(e["message_key"])
            assert set(e) <= {"progress", "message_key", "message", "attempt", "phase"}, e
            assert isinstance(e["message"], str) and e["message"]
    assert {PLANNING_KEY, RUNNING_KEY, DESCRIBING_KEY, SUMMARY_KEY} <= seen, seen


def test_a_raising_on_progress_never_breaks_the_answer(monkeypatch, dfs):
    """Article IV: a status line never breaks the answer."""
    tl = Timeline()
    _two_chart_narrated(tl, monkeypatch)

    def boom(ev):
        raise RuntimeError("page gone")

    events = list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question="show sales",
        history_rows=[], user_email="alice@acme.com", on_progress=boom))
    done = [e for e in events if e.get("done")]
    assert len(done) == 1
    assert done[0]["display_answer"] == NARR_R
    assert len([e for e in events if e.get("partial") and e.get("image_base64")]) == 2


# ======================================================== §2 early visuals

def test_described_chart_partial_precedes_describe_and_caption_follows(monkeypatch, dfs):
    tl = Timeline()
    _two_chart_narrated(tl, monkeypatch)
    _drive(tl, dfs)

    parts = tl.chart_partials()
    assert [p.get("chart_n") for _i, p in parts] == [1, 2]
    assert [p.get("block_index") for _i, p in parts] == [1, 2]
    i_p2, p2 = parts[1]
    i_desc, kw = _first(tl.brain("describe"), "describe call")
    assert kw[1]["code"] == CODE_BN
    # the chart reached the page BEFORE its describe call...
    assert i_p2 < i_desc, tl.items
    assert "described B" not in (p2.get("answer") or "")
    # ...and its text follows in a caption event for THAT chart
    caps = tl.captions()
    assert len(caps) == 1, caps
    i_cap, cap = caps[0]
    assert cap["index"] == 2
    assert cap["text"] == "described B"
    assert i_desc < i_cap
    i_narr, _ = _first(tl.narratives(), "narrative event")
    i_done, _ = tl.final()
    assert i_cap < i_narr < i_done


def test_captioned_chart_carries_its_caption_and_gets_no_caption_event(monkeypatch, dfs):
    tl = Timeline()
    _two_chart_narrated(tl, monkeypatch)
    _drive(tl, dfs)

    parts = tl.chart_partials()
    assert parts[0][1]["answer"] == "cap 1"
    assert parts[0][1].get("caption_template") == "cap {{a}}"
    assert all(c["index"] != 1 for _i, c in tl.captions())


def test_done_event_keeps_the_describe_text(monkeypatch, dfs):
    """Moving the describe text to a caption event changes no brain-safe
    text: `combined_answer` stays the template, `display_answer` the
    rendered narrative."""
    tl = Timeline()
    _two_chart_narrated(tl, monkeypatch)
    _drive(tl, dfs)

    _i, done = tl.final()
    assert done["combined_answer"] == narrative.history_content(NARR_T, {})
    assert done["display_answer"] == NARR_R


def test_no_narrative_turn_also_sends_the_caption_event(monkeypatch, dfs):
    tl = Timeline()
    Brain(tl, _multi_plan(("plot_code", "fig = chart_plain"))).install(monkeypatch)
    _install_execs(tl, monkeypatch, {"fig = chart_plain": _chart()})
    _drive(tl, dfs)

    (i_p, _p), = tl.chart_partials()
    i_desc, _ = _first(tl.brain("describe"), "describe call")
    assert i_p < i_desc
    caps = tl.captions()
    assert len(caps) == 1 and caps[0][1]["index"] == 1
    assert caps[0][1]["text"] == "described"
    _i, done = tl.final()
    assert done["combined_answer"] == "described"
    assert done["display_answer"] == "described"


def test_single_table_answer_sends_a_tables_partial_before_its_text(monkeypatch, dfs):
    tl = Timeline()
    Brain(tl, {"kind": "PYTHON", "code": TABLE_CODE, "raw_text": "", "usage": {}}
          ).install(monkeypatch)
    _install_execs(tl, monkeypatch, real_python=True)
    _drive(tl, dfs)

    i_exec = _first(tl.execs(), "execution")
    i_tbl, tbl = _first(tl.tables_partials(), "tables partial")
    i_desc, _ = _first(tl.brain("describe"), "describe call")
    i_final, _ = tl.final()
    assert i_exec < i_tbl < i_desc < i_final, tl.items
    assert len(tbl["tables"]) == 1
    assert list(tbl["tables"][0]["columns"]) == ["region", "amount"]
    assert _final_text(tl) == "described"


def test_single_narrated_captioned_table_partial_precedes_the_answer(monkeypatch, dfs):
    code = "VALUES['total'] = 3\nRESULT = dfs['f.csv']"
    tl = Timeline()
    Brain(tl, {"kind": "PYTHON", "code": code, "raw_text": "", "usage": {},
               "narrative": "Rows counted: {{total}}.",
               "captions": {"1": "This table has {{total}} rows."}}).install(monkeypatch)
    _install_execs(tl, monkeypatch, real_python=True)
    _drive(tl, dfs)

    assert tl.brain("describe") == []
    i_tbl, _tbl = _first(tl.tables_partials(), "tables partial")
    i_final, ev = tl.final()
    assert i_tbl < i_final
    assert ev["result"]["narrative_rendered"] == "Rows counted: 3."
    assert ev["result"]["caption_rendered"] == "This table has 3 rows."


# ===================================================== §4 describe skip

def test_narrated_turn_rendered_captions_make_no_describe_call(monkeypatch, dfs):
    tl = Timeline()
    Brain(tl, _multi_plan(("plot_code", CODE_A), ("plot_code", CODE_B),
                          narrative_t="Sum {{a}} {{b}}",
                          captions={"1": "A is {{a}}.", "2": "B is {{b}}."})
          ).install(monkeypatch)
    _install_execs(tl, monkeypatch, {CODE_A: _chart({"a": 1}), CODE_B: _chart({"b": 2})})
    _drive(tl, dfs)

    assert tl.brain("describe") == []
    assert tl.progress(DESCRIBING_KEY) == []
    assert tl.captions() == []
    assert [p["answer"] for _i, p in tl.chart_partials()] == ["A is 1.", "B is 2."]


def test_narrated_turn_dropped_caption_falls_back_to_describe(monkeypatch, dfs):
    """Caption 1 renders only "n/a" (a None value) and is dropped by the
    quality gate: that visual keeps describe as its fallback; caption 2
    renders and needs none."""
    code_x = "fig = chart_x\nVALUES['x'] = None"
    code_y = "fig = chart_y\nVALUES['y'] = 3"
    tl = Timeline()
    Brain(tl, _multi_plan(("plot_code", code_x), ("plot_code", code_y),
                          narrative_t="Total {{y}}.",
                          captions={"1": "{{x}} here", "2": "Y is {{y}}."}),
          describe_text="described X").install(monkeypatch)
    _install_execs(tl, monkeypatch, {code_x: _chart({"x": None}), code_y: _chart({"y": 3})})
    _drive(tl, dfs)

    describes = tl.brain("describe")
    assert [kw["code"] for _i, (_n, kw) in describes] == [code_x]
    caps = tl.captions()
    assert [(c["index"], c["text"]) for _i, c in caps] == [(1, "described X")]
    assert tl.chart_partials()[1][1]["answer"] == "Y is 3."
    _i, done = tl.final()
    assert done["display_answer"] == "Total 3."


@pytest.mark.parametrize("bad_caption", [
    "x" * (narrative.CAPTION_MAX + 1),          # too long (rejected on receipt)
    "{{#flag}}Only when flagged.{{/flag}}",     # renders "" (section false)
    "See ```x = 1``` for {{a}}.",               # code-in-text gate
], ids=["too_long", "renders_empty", "code_in_text"])
def test_narrated_turn_planned_caption_not_rendered_falls_back_to_describe(
        monkeypatch, dfs, bad_caption):
    """The plan DELIVERED a caption template for the visual and it did not
    render: describe stays its fallback, even in a narrated turn."""
    tl = Timeline()
    Brain(tl, _multi_plan(("plot_code", CODE_A), narrative_t="Total {{a}}.",
                          captions={"1": bad_caption}),
          describe_text="described A").install(monkeypatch)
    _install_execs(tl, monkeypatch, {CODE_A: _chart({"a": 1})})
    _drive(tl, dfs)

    assert [kw["code"] for _i, (_n, kw) in tl.brain("describe")] == [CODE_A]
    assert [(c["index"], c["text"]) for _i, c in tl.captions()] == [(1, "described A")]
    _i, done = tl.final()
    assert done["display_answer"] == "Total 1."


def test_narrated_turn_uncaptioned_visual_gets_no_describe(monkeypatch, dfs):
    """Corrected §4: in a turn WITH a rendered narrative, a visual the plan
    gave NO caption template is explained by the narrative's last sentence —
    no describe call, no caption event, no `lab.stage_describing`."""
    tl = Timeline()
    Brain(tl, _multi_plan(("plot_code", CODE_A), ("plot_code", CODE_B),
                          narrative_t="N {{a}} {{b}}.", captions={"1": "cap {{a}}"})
          ).install(monkeypatch)
    _install_execs(tl, monkeypatch, {CODE_A: _chart({"a": 1}), CODE_B: _chart({"b": 2})})
    _drive(tl, dfs)

    assert tl.brain("describe") == []
    assert tl.captions() == []
    assert tl.progress(DESCRIBING_KEY) == []
    parts = tl.chart_partials()
    assert len(parts) == 2
    assert parts[0][1]["answer"] == "cap 1"
    assert not (parts[1][1].get("answer") or "")
    _i, done = tl.final()
    assert done["display_answer"] == "N 1 2."
    assert done["combined_answer"] == narrative.history_content("N {{a}} {{b}}.", {})


def test_narrated_three_charts_only_the_dropped_caption_is_described(monkeypatch, dfs):
    """Chart 1: planned caption renders. Chart 2: no planned caption. Chart 3:
    planned caption dropped (all n/a). Exactly ONE describe (chart 3) and ONE
    caption event (index 3)."""
    code_3 = "fig = chart_3\nVALUES['z'] = None"
    tl = Timeline()
    Brain(tl, _multi_plan(("plot_code", CODE_A), ("plot_code", CODE_B), ("plot_code", code_3),
                          narrative_t="N {{a}} {{b}}.",
                          captions={"1": "cap {{a}}", "3": "{{z}} here"}),
          describe_text="described 3").install(monkeypatch)
    _install_execs(tl, monkeypatch, {CODE_A: _chart({"a": 1}), CODE_B: _chart({"b": 2}),
                                     code_3: _chart({"z": None})})
    _drive(tl, dfs)

    describes = tl.brain("describe")
    assert [kw["code"] for _i, (_n, kw) in describes] == [code_3]
    caps = tl.captions()
    assert [(c["index"], c["text"]) for _i, c in caps] == [(3, "described 3")]
    parts = tl.chart_partials()
    assert [p.get("chart_n") for _i, p in parts] == [1, 2, 3]
    assert parts[0][1]["answer"] == "cap 1"
    assert not (parts[1][1].get("answer") or "")
    i_p3 = parts[2][0]
    assert i_p3 < describes[0][0] < caps[0][0]
    desc_stages = [i for i, _e in tl.progress(DESCRIBING_KEY)]
    assert len(desc_stages) == 1 and desc_stages[0] < describes[0][0]
    _i, done = tl.final()
    assert done["display_answer"] == "N 1 2."


def test_narrative_not_rendered_uncaptioned_visual_keeps_describe(monkeypatch, dfs):
    """A narrated PLAN whose narrative does not render (first sentence n/a,
    the narrative-only retry brings none) is a turn WITHOUT a rendered
    narrative: its uncaptioned visual is described exactly as today."""
    tl = Timeline()
    Brain(tl, _multi_plan(("plot_code", CODE_A), narrative_t="Total is {{missing}}."),
          retry={"kind": "PLOT_CODE", "code": "fig = never_run", "usage": {}},
          describe_text="described A").install(monkeypatch)
    _install_execs(tl, monkeypatch, {CODE_A: _chart({"a": 1})})
    _drive(tl, dfs)

    assert [kw["code"] for _i, (_n, kw) in tl.brain("describe")] == [CODE_A]
    assert tl.narratives() == []
    assert [(c["index"], c["text"]) for _i, c in tl.captions()] == [(1, "described A")]
    _i, done = tl.final()
    assert done["display_answer"] == "described A"
    assert done["combined_answer"] == "described A"


def test_turn_without_narrative_keeps_describe(monkeypatch, dfs):
    tl = Timeline()
    Brain(tl, _multi_plan(("plot_code", CODE_A), ("plot_code", CODE_B),
                          captions={"1": "cap {{a}}"}),
          describe_text="described B").install(monkeypatch)
    _install_execs(tl, monkeypatch, {CODE_A: _chart({"a": 1}), CODE_B: _chart({"b": 2})})
    _drive(tl, dfs)

    assert [kw["code"] for _i, (_n, kw) in tl.brain("describe")] == [CODE_B]
    assert tl.narratives() == []
    _i, done = tl.final()
    assert done["display_answer"] == "cap 1\n\ndescribed B"


def test_single_narrated_uncaptioned_table_gets_no_describe(monkeypatch, dfs):
    """Corrected §4, single flow: a narrated table answer the plan gave no
    caption is the narrative + the table only — no describe call, no
    `caption_described`, no caption event."""
    code = "VALUES['total'] = 3\nRESULT = dfs['f.csv']"
    tl = Timeline()
    Brain(tl, {"kind": "PYTHON", "code": code, "raw_text": "", "usage": {},
               "narrative": "Rows counted: {{total}}."}).install(monkeypatch)
    _install_execs(tl, monkeypatch, real_python=True)
    _drive(tl, dfs)

    assert tl.brain("describe") == []
    assert tl.captions() == []
    assert tl.progress(DESCRIBING_KEY) == []
    _i, ev = tl.final()
    result = ev["result"]
    assert result["narrative_rendered"] == "Rows counted: 3."
    assert "caption_described" not in result
    assert result.get("table") is not None or result.get("tables")
    # brain-safe text: the marker + template
    assert result["text"] == narrative.history_content("Rows counted: {{total}}.",
                                                       result.get("findings") or {})


def test_single_narrated_dropped_caption_table_keeps_describe(monkeypatch, dfs):
    """Single flow: the plan's caption 1 renders only n/a -> dropped -> the
    describe fallback, stored as `caption_described` beside the narrative."""
    code = "VALUES['total'] = 3\nVALUES['x'] = None\nRESULT = dfs['f.csv']"
    tl = Timeline()
    Brain(tl, {"kind": "PYTHON", "code": code, "raw_text": "", "usage": {},
               "narrative": "Rows counted: {{total}}.",
               "captions": {"1": "{{x}} here"}}).install(monkeypatch)
    _install_execs(tl, monkeypatch, real_python=True)
    _drive(tl, dfs)

    assert len(tl.brain("describe")) == 1
    _i, ev = tl.final()
    result = ev["result"]
    assert result["narrative_rendered"] == "Rows counted: 3."
    assert result.get("caption_described") == "described"
    assert "caption_rendered" not in result
