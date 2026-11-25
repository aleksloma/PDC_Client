"""MCP stage 4 — the byte cap of the finished jobs (`mcp_jobs.MAX_CONTENT_BYTES`).

A finished job keeps its answer, PNG images included, for 30 minutes. The
answers of all finished jobs together stay within MAX_CONTENT_BYTES: the
caller (token id + address) that holds the most finished bytes gives way, its
OLDEST finished job first, again and again until the rest fits.

Pinned readings of the brief:
* who holds the most is judged anew after each dropped job; a caller whose only
  job is the one that just finished gives nothing, the next one does;
* the cap is applied when a job finishes and is read at that moment;
* the job that just finished is never dropped, not even when its answer alone
  is over the cap, and not even when it was STARTED first;
* a running job is never dropped; a dropped or expired answer counts no more;
* size is bytes (UTF-8), texts and image data together.

Offline: the turn and the content builder are fakes.
"""
import importlib
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

from settings import settings

TOK, TOK2 = "a" * 16, "b" * 16
CONV = "cv_0123456789abcdef"
MIB = 1024 * 1024


def text_item(n_bytes, ch="x"):
    return [{"type": "text", "text": ch * (n_bytes // len(ch.encode()))}]


def image_item(n_bytes):
    return [{"type": "text", "text": "t"},
            {"type": "image", "data": "A" * n_bytes, "mimeType": "image/png"}]


class World:
    """Keyed by chat id: `build_content` answers `contents[chat]`, `gates[chat]`
    holds a turn, `errors[chat]` fails it."""

    def __init__(self):
        self.contents, self.gates, self.errors = {}, {}, {}

    def prepare_turn(self, email, chat_id, question, *, conv_id=None, new_conversation=None,
                     sid=None):
        return SimpleNamespace(email=email, chat_id=chat_id, conv_id=CONV, sid="sid-1")

    def run_prepared_turn(self, turn, *, on_event=None, title="auto"):
        gate = self.gates.get(turn.chat_id)
        if gate is not None:
            assert gate.wait(20)
        return SimpleNamespace(record={"role": "ai", "content": "42", "ts": 1.0},
                               error_text=self.errors.get(turn.chat_id), error_key=None,
                               cancelled=False, persisted=True, conv_id=CONV, sid="sid-1")

    def build_content(self, record, *, chat_id, conv_id, email, sid, saved=True):
        return self.contents[chat_id]

    def error_content(self, text):
        return [{"type": "text", "text": text}]


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    w = World()
    ct = importlib.import_module("chat_turn")
    monkeypatch.setattr(ct, "prepare_turn", w.prepare_turn)
    monkeypatch.setattr(ct, "run_prepared_turn", w.run_prepared_turn)
    # the chats are not on disk: the job's own access check passes
    monkeypatch.setattr(importlib.import_module("routes.chat"), "_require_chat",
                        lambda request, chat_id: (request.session["email"], None))
    try:
        mod = importlib.import_module("mcp_answer")
    except ImportError:
        mod = types.ModuleType("mcp_answer")
        monkeypatch.setitem(sys.modules, "mcp_answer", mod)
    monkeypatch.setattr(mod, "build_content", w.build_content, raising=False)
    monkeypatch.setattr(mod, "error_content", w.error_content, raising=False)
    return w


@pytest.fixture
def jobs(world):
    mod = importlib.import_module("mcp_jobs")
    mod.reset()
    yield mod
    for gate in world.gates.values():
        gate.set()
    deadline = time.monotonic() + 15
    for t in threading.enumerate():
        if t.name.startswith("mcp-job"):
            t.join(max(0.1, deadline - time.monotonic()))
    mod.reset()


@pytest.fixture
def small(jobs, monkeypatch):
    """Room for three answers of 1000 bytes, not for four."""
    assert hasattr(jobs, "MAX_CONTENT_BYTES")
    monkeypatch.setattr(jobs, "MAX_CONTENT_BYTES", 3600)
    return jobs


class Start:
    """One chat per job; `who` and `token` are the caller."""

    def __init__(self, jobs, world):
        self.jobs, self.world, self.ids = jobs, world, {}

    def start(self, name, content, *, who="u", token=TOK, gate=False):
        email, chat = f"{who}@acme.com", f"c_{name}"
        self.world.contents[chat] = content
        if gate:
            self.world.gates[chat] = threading.Event()
        self.ids[name] = (self.jobs.start(token, "bot", email, chat, "q").id, token, email)

    def get(self, name):
        return self.jobs.get(*self.ids[name])

    def wait(self, name):
        _until(lambda: self.get(name) is None or self.get(name).state != "running", name)
        job = self.get(name)
        assert job is not None, f"{name} was dropped as it finished"
        return job

    def run(self, name, content, **kw):
        self.start(name, content, **kw)
        return self.wait(name)

    def there(self):
        return [n for n in self.ids if self.get(n) is not None]

    def settles(self, *names):
        """The kept jobs, now and a moment later."""
        _until(lambda: self.there() == list(names), f"{names}, got {self.there()}")
        time.sleep(0.05)
        assert self.there() == list(names)


@pytest.fixture
def s(jobs, world):
    return Start(jobs, world)


def _until(predicate, what):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out: {what}")


def test_the_constant_holds_a_few_full_answers(jobs):
    """Six 2400x1600 PNGs are several MB; the cap is a bound, not a nuisance."""
    cap = jobs.MAX_CONTENT_BYTES
    assert type(cap) is int and 32 * MIB <= cap <= 1024 * MIB


def test_the_oldest_finished_jobs_go_first(small, s):
    for name in "abcde":
        s.run(name, text_item(1000))
    s.settles("c", "d", "e")


def test_within_the_cap_nothing_goes(small, s):
    for name in "abc":
        s.run(name, text_item(1000))
    s.settles("a", "b", "c")


def test_image_data_counts(small, s):
    for name in "abcde":
        s.run(name, image_item(1000))
    s.settles("c", "d", "e")


def test_bytes_count_not_characters(small, s):
    """500 Georgian letters are 1500 bytes."""
    for name in "abc":
        s.run(name, text_item(1500, "ა"))
    _until(lambda: s.get("a") is None, "the oldest to go")
    assert s.get("c") is not None


def test_one_big_answer_takes_the_place_of_several(small, s):
    for name in "abc":
        s.run(name, text_item(1000))
    s.run("big", text_item(2900))
    s.settles("big")


def test_the_newest_is_kept_even_alone_over_the_cap(small, s):
    s.run("a", text_item(1000))
    big = s.run("big", image_item(50_000))
    s.settles("big")
    assert big.state == "done" and big.content == image_item(50_000)
    s.run("b", text_item(1000))
    s.settles("b")


def test_the_job_that_just_finished_is_the_newest_whenever_it_started(small, s, world):
    s.start("first", text_item(3000), gate=True)
    s.run("b", text_item(1000))
    s.run("c", text_item(1000))
    world.gates["c_first"].set()
    assert s.wait("first").content == text_item(3000)
    s.settles("first")


def test_a_running_job_is_never_dropped(small, s, world):
    s.start("running", text_item(1000), gate=True)
    for i in range(4):
        s.run(f"big{i}", text_item(5000))
    assert s.get("running").state == "running"
    s.settles("running", "big3")
    world.gates["c_running"].set()
    assert s.wait("running").content == text_item(1000)
    s.settles("running")


# --- whose job goes ---------------------------------------------------------
def test_the_caller_holding_the_most_gives_way(small, s):
    """The small caller's answer is the oldest of all, and stays."""
    s.run("light", text_item(400), who="light")
    for name in "abcde":
        s.run(name, text_item(1000), who="heavy")
    s.settles("light", "c", "d", "e")


def test_a_light_callers_new_answer_pushes_out_the_heavy_ones_oldest(small, s):
    for name in "abc":
        s.run(name, text_item(1000), who="heavy")
    s.run("light", text_item(900), who="light")
    s.settles("b", "c", "light")


def test_a_caller_pays_for_its_own_big_answer(small, s):
    s.run("other", text_item(1000), who="other")
    s.run("mine", text_item(1200), who="u")
    s.run("big", text_item(2000), who="u")
    s.settles("other", "big")


def test_who_holds_the_most_is_judged_after_each_drop(small, s):
    s.run("a1", text_item(1500), who="a")
    s.run("a2", text_item(200), who="a")
    s.run("b1", text_item(800), who="b")
    s.run("b2", text_item(700), who="b")
    s.settles("a1", "a2", "b1", "b2")
    # the newest caller holds the most but its only job just finished
    s.run("c1", text_item(2600), who="c")
    s.settles("a2", "b2", "c1")


@pytest.mark.parametrize("light,heavy", [
    ({"who": "u", "token": TOK}, {"who": "u", "token": TOK2}),
    ({"who": "u", "token": TOK}, {"who": "v", "token": TOK})], ids=["token", "address"])
def test_a_caller_is_a_token_and_an_address(small, s, light, heavy):
    s.run("light", text_item(900), **light)
    s.run("a", text_item(1300), **heavy)
    s.run("b", text_item(1300), **heavy)
    s.run("last", text_item(800), who="third")
    s.settles("light", "b", "last")


def test_a_running_jobs_caller_is_judged_by_its_finished_answers(small, s, world):
    s.start("running", text_item(3000), who="light", gate=True)
    s.run("light", text_item(500), who="light")
    for name in "abcd":
        s.run(name, text_item(1000), who="heavy")
    s.settles("running", "light", "b", "c", "d")


def test_oldest_means_the_first_to_finish(small, s, world):
    s.start("slow", text_item(1000), gate=True)
    s.run("a", text_item(1000))
    s.run("b", text_item(1000))
    world.gates["c_slow"].set()
    s.wait("slow")
    s.run("c", text_item(1000))
    s.settles("slow", "b", "c")


def test_the_cap_is_read_when_a_job_finishes(small, s, monkeypatch):
    for name in "abc":
        s.run(name, text_item(1000))
    s.settles("a", "b", "c")
    monkeypatch.setattr(small, "MAX_CONTENT_BYTES", 2200)
    s.run("d", text_item(1000))
    s.settles("c", "d")
    monkeypatch.setattr(small, "MAX_CONTENT_BYTES", 10_000)
    s.run("e", text_item(1000))
    s.settles("c", "d", "e")


def test_failed_jobs_stay_readable_and_count_what_they_hold(small, s, world):
    world.errors["c_bad"] = "Analysis service is temporarily unavailable."
    s.run("a", text_item(1000))
    assert s.run("bad", None).state == "failed"
    s.run("b", text_item(1000))
    s.run("c", text_item(1000))
    s.settles("a", "bad", "b", "c")
    s.run("d", text_item(1000))
    _until(lambda: s.get("a") is None, "the oldest to go")
    assert s.get("d") is not None and s.get("bad") is not None


def test_the_answers_of_a_dropped_token_count_no_more(small, s):
    s.run("a", text_item(1500), token=TOK2)
    s.run("b", text_item(1500), token=TOK2)
    small.drop_token(TOK2)
    for name in "cde":
        s.run(name, text_item(1000))
    s.settles("c", "d", "e")


def test_a_token_dropped_mid_run_leaves_no_bytes_behind(small, s, world):
    s.start("gone", text_item(3400), token=TOK2, gate=True)
    small.drop_token(TOK2)
    world.gates["c_gone"].set()
    _until(lambda: not [t for t in threading.enumerate() if t.name.startswith("mcp-job")],
           "the turn to end")
    for name in "abc":
        s.run(name, text_item(1000))
    s.settles("a", "b", "c")


def test_expired_answers_count_no_more(small, s, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(small, "_clock", lambda: now["t"])
    s.run("a", text_item(3400))
    now["t"] += small.JOB_TTL_S + 1
    for name in "bcd":
        s.run(name, text_item(1000))
    s.settles("b", "c", "d")


def test_the_job_cap_still_holds(small, s, monkeypatch):
    monkeypatch.setattr(small, "MAX_JOBS", 2)
    for name in "abcd":
        s.run(name, text_item(10))
    s.settles("c", "d")


def test_content_of_an_odd_shape_never_fails_a_job(small, s):
    odd = [{"type": "text", "text": None}, {"type": "image"}, "x", None, {"data": 5}]
    assert s.run("odd", odd).state in ("done", "failed")
    for name in "abc":
        s.run(name, text_item(1000))
    assert s.get("c").state == "done"


def test_many_jobs_at_once_stay_within_the_cap(small, s, world, monkeypatch):
    monkeypatch.setattr(small, "MAX_RUNNING", 64)
    names = [f"u{i}" for i in range(24)]
    for name in names:
        s.start(name, text_item(1000), who=name, gate=True)
    for name in names:
        world.gates[f"c_{name}"].set()
    _until(lambda: not [t for t in threading.enumerate() if t.name.startswith("mcp-job")],
           "the jobs to finish")
    kept = [s.get(n) for n in names if s.get(n) is not None]
    assert 1 <= len(kept) <= 3 and all(j.state == "done" for j in kept)
