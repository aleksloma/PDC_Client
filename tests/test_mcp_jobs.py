"""MCP stage 3 — the in-memory job store (`mcp_jobs`).

    start(token_id, token_name, email, chat_id, question) -> Job
    get(job_id, token_id, email) -> Job | None
    drop_token(token_id); reset() (tests)

`start` takes the (email, chat) reservation at once and raises `JobRefused`
when that pair already runs or `MAX_RUNNING` jobs do; the turn runs on one
daemon thread (named `mcp-job…`): the day's conversation must not be
generating, then `chat_turn.prepare_turn` with
`mcp_conversations.make_factory(email, chat_id)`, `run_prepared_turn(...,
title="never")`, and `mcp_answer.build_content(record, *, chat_id, conv_id,
email, sid, saved=True)`; a failed turn gets `mcp_answer.error_content(text)`.
Right before `prepare_turn` the thread checks the chat access again
(`routes.chat._require_chat`): `ChatDataStore` would re-create a deleted chat.

Pinned readings of the brief:
* every collaborator is read off its module at CALL time
  (`chat_turn.prepare_turn`, `mcp_conversations.…`, `mcp_answer.…`,
  `routes.chat._is_generating`), and the constants and `_clock` too;
* a job reads "running" until its content is there, and the reservation is
  already free when it first reads "done" or "failed";
* a turn without a record and without an error text is "failed" with the
  generic web text; a stopped turn is "done" with its stopped answer; a record
  that could not be saved is still answered, with `saved=False`;
* a job holds its content, never the turn's result (no `Job.result`): the
  record is in the conversation;
* for space, a job is taken from the caller (token and user) holding the most
  finished ones, that caller's oldest first: one caller's burst never costs
  another caller their only answer;
* the 30 minutes count from the end of the job; a running job never expires
  and is never evicted;
* `drop_token` hides the job and nothing else: the turn runs on and is saved,
  and its reservation is held until it ends.

Offline: DATA_ROOT is tmp_path, the turn and the content builder are fakes.
"""
import importlib
import logging
import re
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

import logger_utils
from settings import settings

OWNER = "owner@acme.com"
FRIEND = "friend@acme.com"
CHAT = "c_mcpjobs0000001"
CHAT2 = "c_mcpjobs0000002"
TOK, TOK2 = "a" * 16, "b" * 16
QUESTION = "How did sales go?"
GENERIC = "Something went wrong while processing your request."
CONV = "cv_0123456789abcdef"


@pytest.fixture
def logs():
    """Every line written through `log_with_sid`, whichever module emits it."""
    class _Capture(logging.Handler):
        lines: list = []

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


def _join_job_threads():
    deadline = time.monotonic() + 15
    for t in threading.enumerate():
        if t.name.startswith("mcp-job"):
            t.join(max(0.1, deadline - time.monotonic()))


class Content:
    """`mcp_answer`: what was asked of it."""

    def __init__(self):
        self.calls, self.errors, self.raises = [], [], None
        self.gate = threading.Event()
        self.gate.set()

    def build_content(self, record, *, chat_id, conv_id, email, sid, saved=True):
        self.calls.append({"record": record, "chat_id": chat_id, "conv_id": conv_id,
                           "email": email, "sid": sid, "saved": saved})
        assert self.gate.wait(15)
        if self.raises is not None:
            raise self.raises
        return [{"type": "text", "text": f"answer {len(self.calls)}"}]

    def error_content(self, text):
        self.errors.append(text)
        return [{"type": "text", "text": text}]


@pytest.fixture
def content(monkeypatch):
    c = Content()
    try:
        mod = importlib.import_module("mcp_answer")
    except ImportError:
        mod = types.ModuleType("mcp_answer")
        monkeypatch.setitem(sys.modules, "mcp_answer", mod)
    monkeypatch.setattr(mod, "build_content", c.build_content, raising=False)
    monkeypatch.setattr(mod, "error_content", c.error_content, raising=False)
    return c


@pytest.fixture
def access(monkeypatch):
    """`routes.chat._require_chat`: every chat is open, or what `refuse` says
    (the chats of this file do not exist on disk)."""
    from fastapi.responses import JSONResponse

    class _Access:
        calls: list = []
        refuse = raises = None

    a = _Access()
    a.calls = []

    def require(request, chat_id):
        a.calls.append((request.session["email"], chat_id))
        if a.raises is not None:
            raise a.raises
        if a.refuse is not None:
            return None, JSONResponse({"error": "Chat not found"}, status_code=a.refuse)
        return request.session["email"], None

    monkeypatch.setattr(importlib.import_module("routes.chat"), "_require_chat", require)
    return a


@pytest.fixture
def jobs(tmp_path, monkeypatch, content, access):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    mod = importlib.import_module("mcp_jobs")
    mod.reset()
    yield mod
    content.gate.set()
    _join_job_threads()
    mod.reset()


class Turns:
    """`chat_turn` replaced: `prepares` / `runs` record the calls, `gate`
    holds a run, `outcome` decides it."""

    def __init__(self):
        self.prepares, self.runs, self.finished, self.threads = [], [], [], []
        self.gate = threading.Event()
        self.gate.set()
        self.refusal = self.prepare_raises = self.run_raises = None
        self.over = {}

    def prepare_turn(self, email, chat_id, question, *, conv_id=None,
                     new_conversation=None, sid=None):
        self.prepares.append({"email": email, "chat_id": chat_id, "question": question,
                              "conv_id": conv_id, "new_conversation": new_conversation})
        if self.prepare_raises is not None:
            raise self.prepare_raises
        return self.refusal or SimpleNamespace(email=email, chat_id=chat_id,
                                               question=question, conv_id=CONV, sid="sid-1")

    def run_prepared_turn(self, turn, *, on_event=None, title="auto"):
        self.runs.append({"turn": turn, "title": title})
        self.threads.append(threading.current_thread())
        assert self.gate.wait(15)
        if self.run_raises is not None:
            raise self.run_raises
        res = {"record": {"role": "ai", "content": "42", "ts": 1.0}, "error_text": None,
               "error_key": None, "cancelled": False, "persisted": True,
               "conv_id": CONV, "sid": "sid-1"}
        res.update(self.over)
        self.finished.append(turn)
        return SimpleNamespace(**res)


@pytest.fixture
def ct():
    return importlib.import_module("chat_turn")


@pytest.fixture
def turns(ct, jobs, monkeypatch):
    t = Turns()
    monkeypatch.setattr(ct, "prepare_turn", t.prepare_turn)
    monkeypatch.setattr(ct, "run_prepared_turn", t.run_prepared_turn)
    yield t
    t.gate.set()


@pytest.fixture
def clock(jobs, monkeypatch):
    now = {"t": 50_000.0}
    monkeypatch.setattr(jobs, "_clock", lambda: now["t"])
    return now


def _start(jobs, email=OWNER, chat=CHAT, token=TOK, question=QUESTION, name="Helpdesk bot"):
    return jobs.start(token, name, email, chat, question)


def _until(predicate, what):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out: {what}")


def _finished(jobs, job, token=TOK, email=OWNER):
    _until(lambda: jobs.get(job.id, token, email).state != "running", "the job to finish")
    return jobs.get(job.id, token, email)


def _sentence(text):
    assert isinstance(text, str) and " " in text and text[0].isupper() \
        and text.endswith("."), text
    assert "Traceback" not in text and "Error" not in text, text


# ===========================================================================
# the module
# ===========================================================================
def test_the_constants(jobs):
    assert (jobs.JOB_TTL_S, jobs.MAX_RUNNING, jobs.MAX_JOBS) == (1800, 8, 256)
    assert issubclass(jobs.JobRefused, Exception)
    assert isinstance(jobs._clock(), (int, float))


# ===========================================================================
# one job
# ===========================================================================
def test_start_answers_at_once_with_a_running_job(jobs, turns):
    turns.gate.clear()
    before = time.monotonic()
    job = _start(jobs)
    assert time.monotonic() - before < 5
    assert re.fullmatch(r"[0-9a-f]{32}", job.id)
    assert job.state == "running" and job.chat_id == CHAT
    assert not job.content and job.error_text is None
    again = jobs.get(job.id, TOK, OWNER)
    assert again.id == job.id and again.state == "running"


def test_a_finished_job(jobs, turns, content):
    job = _finished(jobs, _start(jobs))
    assert job.state == "done" and job.error_text is None
    assert job.conv_id == CONV and job.chat_id == CHAT
    assert job.content == [{"type": "text", "text": "answer 1"}]
    assert content.calls == [{"record": {"role": "ai", "content": "42", "ts": 1.0},
                              "chat_id": CHAT, "conv_id": CONV, "email": OWNER,
                              "sid": "sid-1", "saved": True}]


@pytest.mark.parametrize("over", [{}, {"error_text": "Analysis service is busy."},
                                  {"record": None}], ids=["done", "failed", "no-record"])
def test_a_job_keeps_no_turn_result(jobs, turns, over):
    """The record, charts and tables included, is in the conversation; a kept
    job must not hold it in memory for half an hour."""
    turns.over = {"record": {"role": "ai", "content": "HEAVYTEXT",
                             "image_base64": "<div>HEAVYIMAGE</div>",
                             "table": {"rows": [{"a": "HEAVYCELL"}]}}, **over}
    job = _finished(jobs, _start(jobs))
    assert not hasattr(job, "result")
    assert "HEAVY" not in repr(vars(job))


def test_the_turn_is_prepared_with_the_days_factory_and_never_titled(jobs, turns, monkeypatch):
    mc = importlib.import_module("mcp_conversations")
    made, factory = [], lambda store: None
    monkeypatch.setattr(mc, "make_factory",
                        lambda email, chat_id: made.append((email, chat_id)) or factory)
    _finished(jobs, _start(jobs))
    assert made == [(OWNER, CHAT)]
    assert turns.prepares == [{"email": OWNER, "chat_id": CHAT, "question": QUESTION,
                               "conv_id": None, "new_conversation": factory}]
    assert [r["title"] for r in turns.runs] == ["never"]
    assert len(turns.finished) == 1, "the turn must run exactly once"


def test_the_turn_runs_on_its_own_daemon_thread(jobs, turns):
    _finished(jobs, _start(jobs))
    thread = turns.threads[0]
    assert thread is not threading.main_thread()
    assert thread.daemon is True, "a running job must not keep the process alive"
    assert thread.name.startswith("mcp-job")


def test_the_job_reads_running_until_its_content_is_there(jobs, turns, content):
    """Never "done" with nothing to deliver."""
    content.gate.clear()
    job = _start(jobs)
    _until(lambda: content.calls, "the content builder")
    assert jobs.get(job.id, TOK, OWNER).state == "running"
    content.gate.set()
    assert _finished(jobs, job).content == [{"type": "text", "text": "answer 1"}]


def test_a_stopped_turn_is_done_with_its_stopped_answer(jobs, turns, content):
    turns.over = {"cancelled": True, "record": {"role": "ai", "stopped": True,
                                                "content": "Response stopped by user."}}
    job = _finished(jobs, _start(jobs))
    assert job.state == "done" and job.content
    assert content.calls[0]["record"]["stopped"] is True


def test_an_answer_that_could_not_be_saved_is_still_answered(jobs, turns, content):
    """The builder is told: there is no conversation row a link could open."""
    turns.over = {"persisted": False}
    job = _finished(jobs, _start(jobs))
    assert job.state == "done" and job.content
    assert [c["saved"] for c in content.calls] == [False]


# ===========================================================================
# failures
# ===========================================================================
def test_a_failed_turn_carries_the_webs_text(jobs, turns, content):
    text = "The analysis is taking longer than expected. Please try again."
    turns.over = {"error_text": text, "error_key": "lab.brain_timeout",
                  "record": {"role": "ai", "content": text, "ts": 1.0}}
    job = _finished(jobs, _start(jobs))
    assert job.state == "failed" and job.error_text == text
    assert job.content == [{"type": "text", "text": text}]
    assert content.errors == [text] and content.calls == []


def test_a_turn_without_a_record_is_failed_with_the_generic_text(jobs, turns, content):
    turns.over = {"record": None, "persisted": False}
    job = _finished(jobs, _start(jobs))
    assert (job.state, job.error_text) == ("failed", GENERIC)
    assert content.errors == [GENERIC] and content.calls == []


@pytest.mark.parametrize("kind,status,text", [
    ("auto_analysis_busy", 409, "Auto Analytics is currently running for this chat."),
    ("empty_dataset", 400, "Chat dataset is empty."),
    ("no_conversation", 409, "The conversation could not be created."),
])
def test_a_refused_turn_carries_the_refusals_text(jobs, turns, ct, content, kind, status, text):
    turns.refusal = ct.TurnRefusal(kind, status, {"error": text, "code": "X"})
    job = _finished(jobs, _start(jobs))
    assert (job.state, job.error_text) == ("failed", text)
    assert job.content == [{"type": "text", "text": text}]
    assert turns.runs == [] and content.calls == []


def test_a_refusal_without_a_text_gets_the_generic_one(jobs, turns, ct):
    turns.refusal = ct.TurnRefusal("empty_dataset", 400, {})
    job = _finished(jobs, _start(jobs))
    assert (job.state, job.error_text) == ("failed", GENERIC)


def _break(where, jobs, turns, content, access, monkeypatch):
    exc = KeyError("column 'SECRET'\nFORGED RECORD")

    def boom(*a, **k):
        raise exc

    if where == "prepare":
        turns.prepare_raises = exc
    elif where == "run":
        turns.run_raises = exc
    elif where == "content":
        content.raises = exc
    elif where == "access":
        access.raises = exc
    elif where == "factory":
        monkeypatch.setattr(importlib.import_module("mcp_conversations"),
                            "make_factory", boom)
    else:
        monkeypatch.setattr(importlib.import_module("mcp_conversations"),
                            "conversation_for_today", boom)


@pytest.mark.parametrize("where", ["lookup", "access", "factory", "prepare", "run",
                                   "content"])
def test_anything_raised_is_failed_with_the_generic_text(jobs, turns, content, access,
                                                         monkeypatch, logs, where):
    with monkeypatch.context() as m:
        _break(where, jobs, turns, content, access, m)
        job = _finished(jobs, _start(jobs))
    assert (job.state, job.error_text) == ("failed", GENERIC)
    assert job.content == [{"type": "text", "text": GENERIC}]
    assert [ln for ln in logs if "KeyError" in ln], "must be logged (Article IV)"
    assert not [ln for ln in logs if "\n" in ln or "SECRET" in ln], logs
    # the reservation is free again
    turns.prepare_raises = turns.run_raises = content.raises = access.raises = None
    assert _finished(jobs, _start(jobs)).state == "done"


def test_an_error_content_that_raises_still_finishes_the_job(jobs, turns, content, monkeypatch):
    def boom(text):
        raise RuntimeError("x")

    monkeypatch.setattr(sys.modules["mcp_answer"], "error_content", boom)
    turns.over = {"error_text": "Analysis service is temporarily unavailable."}
    job = _finished(jobs, _start(jobs))
    assert job.state == "failed" and job.error_text


# --- the last step: storing the outcome -------------------------------------
def _ended(jobs, job):
    """The job ended as failed, with a text to deliver, and freed its pair."""
    job = _finished(jobs, job)
    assert (job.state, job.error_text) == ("failed", GENERIC)
    assert job.content == [{"type": "text", "text": GENERIC}]


def test_a_failing_size_count_ends_the_job(jobs, turns, monkeypatch, logs):
    def boom(*a, **k):
        raise KeyError("column 'SECRET'")

    with monkeypatch.context() as m:
        m.setattr(jobs, "_content_bytes", boom)
        _ended(jobs, _start(jobs))
        _ended(jobs, _start(jobs))          # the reservation is free
    assert [ln for ln in logs if "KeyError" in ln], "must be logged (Article IV)"
    assert not [ln for ln in logs if "SECRET" in ln]
    assert _finished(jobs, _start(jobs)).state == "done"


@pytest.mark.parametrize("odd", [5, object(), 1.5, True], ids=["int", "object", "float",
                                                                "bool"])
def test_content_of_an_unexpected_type_ends_the_job(jobs, turns, monkeypatch, odd):
    with monkeypatch.context() as m:
        m.setattr(importlib.import_module("mcp_answer"), "build_content",
                  lambda *a, **k: odd)
        _ended(jobs, _start(jobs))
    assert _finished(jobs, _start(jobs)).state == "done"


@pytest.mark.parametrize("name", ["_clock", "_evict"])
def test_a_storing_step_that_fails_once_ends_the_job(jobs, turns, monkeypatch, logs, name):
    """Raised inside the step that ends the job (on the job thread)."""
    real, raised = getattr(jobs, name), []

    def once(*a, **k):
        if threading.current_thread().name.startswith("mcp-job") and not raised:
            raised.append(1)
            raise KeyError("SECRET")
        return real(*a, **k)

    with monkeypatch.context() as m:
        m.setattr(jobs, name, once)
        _ended(jobs, _start(jobs))
    assert raised, "the fault was never injected"
    assert not [ln for ln in logs if "SECRET" in ln]
    assert _finished(jobs, _start(jobs)).state == "done"


# --- the chat is gone when the thread runs ----------------------------------
@pytest.mark.parametrize("status", [403, 404])
def test_the_chat_access_is_checked_again_on_the_thread(jobs, turns, access, status):
    """Deleted or unshared between `ask` and the thread."""
    access.refuse = status
    job = _finished(jobs, _start(jobs))
    text = importlib.import_module("routes.mcp").CHAT_NOT_FOUND_TEXT
    assert text.startswith("Chat not found")
    assert (job.state, job.error_text) == ("failed", text)
    assert job.content == [{"type": "text", "text": text}]
    assert access.calls == [(OWNER, CHAT)]
    assert turns.prepares == [], "prepare_turn would re-create the chat folder"
    access.refuse = None
    assert _finished(jobs, _start(jobs)).state == "done"


def test_the_access_check_comes_right_before_the_turn_is_prepared(jobs, turns, access):
    _finished(jobs, _start(jobs, email=FRIEND, chat=CHAT2), email=FRIEND)
    assert access.calls == [(FRIEND, CHAT2)]
    assert [p["chat_id"] for p in turns.prepares] == [CHAT2]


def test_a_thread_that_cannot_start_is_a_refusal_and_frees_the_reservation(
        jobs, turns, monkeypatch):
    def boom(self):
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as m:
        m.setattr(threading.Thread, "start", boom)
        with pytest.raises(jobs.JobRefused) as ei:
            _start(jobs)
    _sentence(str(ei.value))
    assert "thread" not in str(ei.value)
    assert _finished(jobs, _start(jobs)).state == "done"


# ===========================================================================
# the day's conversation is already generating (a web question runs in it)
# ===========================================================================
def test_a_generating_conversation_refuses(jobs, turns, monkeypatch):
    chat_mod = importlib.import_module("routes.chat")
    mc = importlib.import_module("mcp_conversations")
    asked = []
    monkeypatch.setattr(mc, "conversation_for_today",
                        lambda email, chat_id: asked.append((email, chat_id)) or CONV)
    chat_mod._mark_generating(CONV)
    try:
        job = _finished(jobs, _start(jobs))
        assert job.state == "failed"
        _sentence(job.error_text)
        assert job.error_text != GENERIC
        assert job.content == [{"type": "text", "text": job.error_text}]
        assert turns.prepares == [], "the question must not be saved"
        assert asked == [(OWNER, CHAT)]
        assert chat_mod._is_generating(CONV), "the web turn's own mark was taken away"
    finally:
        chat_mod._unmark_generating(CONV)
    assert _finished(jobs, _start(jobs)).state == "done"


# ===========================================================================
# the reservation and the global cap
# ===========================================================================
def test_one_running_question_per_user_and_chat(jobs, turns):
    turns.gate.clear()
    first = _start(jobs)
    for token in (TOK, TOK2):            # another token acts as the same user
        with pytest.raises(jobs.JobRefused) as ei:
            _start(jobs, token=token)
        _sentence(str(ei.value))
        assert CHAT not in str(ei.value) and OWNER not in str(ei.value)
    _start(jobs, chat=CHAT2)             # another chat
    _start(jobs, email=FRIEND)           # another user
    turns.gate.set()
    assert _finished(jobs, first).state == "done"
    assert _finished(jobs, _start(jobs)).state == "done"


def test_a_finished_job_has_already_freed_its_reservation(jobs, turns):
    """As soon as get() says finished, the next question is accepted."""
    for i in range(50):
        job = _start(jobs, question=f"q{i}")
        while jobs.get(job.id, TOK, OWNER).state == "running":
            pass
    assert len(turns.finished) == 50


def test_two_asks_at_the_same_moment_start_one_job(jobs, turns):
    turns.gate.clear()
    barrier, out, lock = threading.Barrier(12), [], threading.Lock()

    def go():
        barrier.wait(10)
        try:
            got = _start(jobs)
        except jobs.JobRefused:
            got = None
        with lock:
            out.append(got)

    threads = [threading.Thread(target=go) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert len(out) == 12 and len([j for j in out if j]) == 1


def test_the_number_of_running_jobs_is_capped(jobs, turns):
    turns.gate.clear()
    running = [_start(jobs, email=f"u{i}@acme.com") for i in range(8)]
    with pytest.raises(jobs.JobRefused) as ei:
        _start(jobs, email="u8@acme.com")
    _sentence(str(ei.value))
    turns.gate.set()
    for i, job in enumerate(running):
        assert _finished(jobs, job, email=f"u{i}@acme.com").state == "done"
    # the refused one left no reservation behind
    assert _finished(jobs, _start(jobs, email="u8@acme.com"),
                     email="u8@acme.com").state == "done"


def test_the_cap_is_read_at_call_time(jobs, turns, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_RUNNING", 1)
    turns.gate.clear()
    _start(jobs)
    with pytest.raises(jobs.JobRefused):
        _start(jobs, email=FRIEND)


# ===========================================================================
# who may read a job
# ===========================================================================
@pytest.mark.parametrize("state", ["running", "done"])
def test_only_the_same_token_and_user_read_a_job(jobs, turns, state):
    if state == "running":
        turns.gate.clear()
    job = _start(jobs)
    if state == "done":
        _finished(jobs, job)
    assert jobs.get(job.id, TOK, OWNER).state == state
    for token, email in ((TOK2, OWNER), (TOK, FRIEND), (TOK2, FRIEND), ("", OWNER),
                         (None, OWNER), (TOK, ""), (TOK, None), (TOK[:8], OWNER)):
        assert jobs.get(job.id, token, email) is None, (token, email)


@pytest.mark.parametrize("bad", [None, "", 7, True, 1.5, [], {}, ["x"], b"x" * 32,
                                 "0" * 32, "g" * 32, "../../etc/passwd", "x" * 100_000,
                                 "\ud800", "0" * 31 + "\n"], ids=lambda v: repr(v)[:30])
def test_a_malformed_or_unknown_id_reads_nothing(jobs, turns, bad):
    _finished(jobs, _start(jobs))
    assert jobs.get(bad, TOK, OWNER) is None


def test_a_job_id_is_matched_exactly(jobs, turns):
    job = _finished(jobs, _start(jobs))
    for bad in (job.id + "\n", " " + job.id, job.id[:-1], job.id + "0", job.id.encode()):
        assert jobs.get(bad, TOK, OWNER) is None, bad


def test_ids_are_not_guessable_from_each_other(jobs, turns):
    ids = {_finished(jobs, _start(jobs)).id for _ in range(5)}
    assert len(ids) == 5 and len({i[:16] for i in ids}) == 5


# ===========================================================================
# expiry and eviction
# ===========================================================================
def test_a_finished_job_lives_thirty_minutes(jobs, turns, clock):
    job = _finished(jobs, _start(jobs))
    clock["t"] += 1799
    assert jobs.get(job.id, TOK, OWNER).state == "done"
    clock["t"] += 2
    assert jobs.get(job.id, TOK, OWNER) is None
    clock["t"] -= 1000
    assert jobs.get(job.id, TOK, OWNER) is None, "an expired job came back"


def test_reading_a_job_does_not_prolong_it(jobs, turns, clock):
    job = _finished(jobs, _start(jobs))
    for _ in range(3):
        clock["t"] += 599
        assert jobs.get(job.id, TOK, OWNER) is not None
    clock["t"] += 10
    assert jobs.get(job.id, TOK, OWNER) is None


def test_the_thirty_minutes_count_from_the_end(jobs, turns, clock):
    turns.gate.clear()
    job = _start(jobs)
    _until(lambda: turns.runs, "the turn to start")
    clock["t"] += 100_000                     # a running job never expires
    assert jobs.get(job.id, TOK, OWNER).state == "running"
    turns.gate.set()
    _finished(jobs, job)
    clock["t"] += 1799
    assert jobs.get(job.id, TOK, OWNER).state == "done"


def test_the_ttl_is_read_at_call_time(jobs, turns, clock, monkeypatch):
    job = _finished(jobs, _start(jobs))
    monkeypatch.setattr(jobs, "JOB_TTL_S", 5)
    clock["t"] += 6
    assert jobs.get(job.id, TOK, OWNER) is None


def test_the_oldest_finished_jobs_are_evicted_first(jobs, turns, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_JOBS", 3)
    ids = [_finished(jobs, _start(jobs, question=f"q{i}")).id for i in range(5)]
    assert [bool(jobs.get(i, TOK, OWNER)) for i in ids] == [False, False, True, True, True]


def test_a_running_job_is_never_evicted(jobs, turns, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_JOBS", 3)
    turns.gate.clear()
    running = _start(jobs, email=FRIEND)
    _until(lambda: turns.runs, "the turn to start")
    gate, turns.gate = turns.gate, threading.Event()
    turns.gate.set()
    ids = [_finished(jobs, _start(jobs, question=f"q{i}")).id for i in range(6)]
    assert jobs.get(running.id, TOK, FRIEND).state == "running"
    assert jobs.get(ids[-1], TOK, OWNER) is not None
    gate.set()
    assert _finished(jobs, running, email=FRIEND).state == "done"


# --- whose job goes ---------------------------------------------------------
def _burst(jobs, n, **who):
    return [_finished(jobs, _start(jobs, question=f"q{i}", **who),
                      token=who.get("token", TOK), email=who.get("email", OWNER)).id
            for i in range(n)]


def _kept(jobs, ids, token=TOK, email=OWNER):
    return [bool(jobs.get(i, token, email)) for i in ids]


@pytest.mark.parametrize("victim", [{"email": FRIEND}, {"token": TOK2},
                                    {"token": TOK2, "email": FRIEND}],
                         ids=["other-user", "other-token", "both"])
def test_a_burst_never_evicts_another_callers_only_job(jobs, turns, monkeypatch, victim):
    """Callers are told apart by token AND user; the burst pays for itself."""
    monkeypatch.setattr(jobs, "MAX_JOBS", 4)
    only = _burst(jobs, 1, **victim)                  # the oldest of all
    mine = _burst(jobs, 8)
    assert _kept(jobs, only, **victim) == [True]
    assert _kept(jobs, mine) == [False] * 5 + [True] * 3


def test_the_caller_with_the_most_finished_jobs_pays(jobs, turns, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_JOBS", 6)
    small = _burst(jobs, 1, email=FRIEND)
    big = _burst(jobs, 4)
    other = _burst(jobs, 1, email="third@acme.com")
    more = _burst(jobs, 1, email="third@acme.com")    # over the cap now
    assert _kept(jobs, big) == [False, True, True, True]
    assert _kept(jobs, small, email=FRIEND) == [True]
    assert _kept(jobs, other + more, email="third@acme.com") == [True, True]


def test_a_starting_job_is_paid_for_by_the_same_rule(jobs, turns, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_JOBS", 4)
    only = _burst(jobs, 1, email=FRIEND)
    mine = _burst(jobs, 3)
    turns.gate.clear()
    running = _start(jobs, email="third@acme.com")
    assert _kept(jobs, only, email=FRIEND) == [True]
    assert _kept(jobs, mine) == [False, True, True]
    assert jobs.get(running.id, TOK, "third@acme.com").state == "running"


def test_the_job_that_just_finished_is_never_the_one_dropped(jobs, turns, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_JOBS", 1)
    first = _burst(jobs, 1, email=FRIEND)
    second = _burst(jobs, 1)
    assert _kept(jobs, second) == [True] and _kept(jobs, first, email=FRIEND) == [False]
    third = _burst(jobs, 1)
    assert _kept(jobs, second + third) == [False, True]


def test_running_jobs_do_not_make_a_caller_pay(jobs, turns, monkeypatch):
    """Only FINISHED jobs count for who holds the most."""
    monkeypatch.setattr(jobs, "MAX_JOBS", 5)
    friends = _burst(jobs, 2, email=FRIEND)
    mine = _burst(jobs, 1)
    turns.gate.clear()
    running = [_start(jobs, chat=f"c_run{i}") for i in range(3)]      # OWNER's: 1 + 3 running
    assert _kept(jobs, friends, email=FRIEND) == [False, True]
    assert _kept(jobs, mine) == [True]
    assert all(jobs.get(j.id, TOK, OWNER).state == "running" for j in running)


# ===========================================================================
# a revoked token
# ===========================================================================
def test_dropping_a_token_drops_its_jobs_only(jobs, turns):
    mine = _finished(jobs, _start(jobs))
    other = _finished(jobs, _start(jobs, token=TOK2), token=TOK2)
    jobs.drop_token(TOK)
    assert jobs.get(mine.id, TOK, OWNER) is None
    assert jobs.get(other.id, TOK2, OWNER).state == "done"


@pytest.mark.parametrize("bad", [None, "", "unknown", 7, [], "x" * 5000],
                         ids=lambda v: repr(v)[:20])
def test_dropping_an_unknown_token_is_harmless(jobs, turns, bad):
    job = _finished(jobs, _start(jobs))
    jobs.drop_token(bad)
    assert jobs.get(job.id, TOK, OWNER).state == "done"


def test_a_token_dropped_mid_run_hides_the_job_and_the_turn_is_still_saved(jobs, turns):
    turns.gate.clear()
    job = _start(jobs)
    _until(lambda: turns.runs, "the turn to start")
    jobs.drop_token(TOK)
    assert jobs.get(job.id, TOK, OWNER) is None
    # the question is still running: no second one beside it
    with pytest.raises(jobs.JobRefused):
        _start(jobs, token=TOK2)
    turns.gate.set()
    _until(lambda: turns.finished, "the turn to be saved")
    _join_job_threads()
    assert jobs.get(job.id, TOK, OWNER) is None, "the finished turn brought the job back"
    assert _finished(jobs, _start(jobs, token=TOK2), token=TOK2).state == "done"


def test_reset_forgets_jobs_and_reservations(jobs, turns):
    job = _finished(jobs, _start(jobs))
    turns.gate.clear()
    _start(jobs)
    jobs.reset()
    assert jobs.get(job.id, TOK, OWNER) is None
    turns.gate.set()
    _join_job_threads()
    assert _finished(jobs, _start(jobs)).state == "done"


# ===========================================================================
# the log
# ===========================================================================
def test_no_log_line_carries_question_or_answer_or_breaks(jobs, turns, logs):
    turns.over = {"record": {"role": "ai", "content": "ANSWERTEXT 4242", "ts": 1.0}}
    _finished(jobs, _start(jobs, question="SECRETQ " + "z" * 300 + "\nTAIL",
                           name="bot\n[sid=x] FORGED"))
    assert not [ln for ln in logs if "\n" in ln], logs
    assert not [ln for ln in logs if "ANSWERTEXT" in ln or "TAIL" in ln or "z" * 121 in ln]
