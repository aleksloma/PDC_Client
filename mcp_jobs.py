"""The MCP questions being answered, in memory.

`start` accepts a question and runs it on its own daemon thread; `get` reads a
job for the token and the user that started it, and for nobody else. A job is
"running", then "done" or "failed"; its content is there as soon as it is not
running any more.

* One running question per (user, chat) and `MAX_RUNNING` in all: `start`
  takes the reservation before it returns, or raises `JobRefused` with a
  sentence for the user. The reservation is freed in the same step that ends
  the job.
* A finished job is kept `JOB_TTL_S`. Beyond `MAX_JOBS` jobs the caller (token
  and user) with the most finished jobs loses its oldest one; beyond
  `MAX_CONTENT_BYTES` of finished content, judged when a job finishes, the
  caller holding the most bytes does. A running job is never dropped for space,
  nor the one that just finished.
* `drop_token` (a revoked token) hides its jobs. The turn itself runs on, is
  saved, and keeps its reservation until it ends.

A restart forgets the jobs; the answers are in the saved conversations. Correct
only with the one worker process the image runs.

Every collaborator is read off its module at call time, like the constants and
the clock. No log line carries a question, an answer or an exception's text.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Callable, Optional

import brain_client
import chat_turn
import mcp_answer
import mcp_conversations
import routes.chat as _chat
from logger_utils import log_safe_value, log_with_sid

JOB_TTL_S = 1800
MAX_RUNNING = 8
MAX_JOBS = 256
MAX_CONTENT_BYTES = 200_000_000

# The clock of the expiry; a module attribute so the tests can replace it.
_clock = time.monotonic

CHAT_NOT_FOUND_TEXT = "Chat not found. Call list_chats to see the chats of this user."
GENERIC_TEXT = "Something went wrong while processing your request."
PAIR_BUSY_TEXT = ("A question is already running for this user in this chat. "
                  "Wait for its answer, then try again.")
ALL_BUSY_TEXT = "Too many questions are running at the moment. Please try again later."
NOT_STARTED_TEXT = "The question could not be started. Please try again later."
CONVERSATION_BUSY_TEXT = ("Another question is being answered in today's conversation of "
                          "this chat. Please try again when it has finished.")

_ID_RE = re.compile(r"[0-9a-f]{32}")

_LOCK = threading.Lock()
_JOBS: dict[str, "Job"] = {}                    # finished ones in the order they finished
_RUNNING: dict[tuple[str, str], str] = {}       # (email, chat_id) -> job id


class JobRefused(Exception):
    """The question was not accepted; the text is for the user."""


@dataclass
class Job:
    id: str
    token_id: str
    email: str
    chat_id: str
    state: str = "running"              # running | done | failed
    conv_id: Optional[str] = None
    content: Optional[list] = None      # MCP content items
    error_text: Optional[str] = None
    finished_at: float = 0.0
    content_bytes: int = 0


@dataclass
class _Outcome:
    """What a job ends with. `error_text` set: it failed."""
    error_text: Optional[str] = None
    content: Optional[list] = None
    content_bytes: int = 0
    conv_id: Optional[str] = None


def may_open(email: str, chat_id: Any) -> bool:
    """The web's chat access check (owner or share recipient)."""
    _email, err = _chat._require_chat(SimpleNamespace(session={"email": email}), chat_id)
    return err is None


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
def start(token_id: str, token_name: str, email: str, chat_id: str, question: str) -> Job:
    """Accept the question and run it in the background. The caller has
    checked the chat access. Raises JobRefused."""
    job = Job(id=secrets.token_hex(16), token_id=token_id, email=email, chat_id=chat_id)
    pair = (email, chat_id)
    with _LOCK:
        if pair in _RUNNING:
            raise JobRefused(PAIR_BUSY_TEXT)
        if len(_RUNNING) >= MAX_RUNNING:
            raise JobRefused(ALL_BUSY_TEXT)
        _RUNNING[pair] = job.id
        _JOBS[job.id] = job
        _expire()
        _cap_count()
        accepted = replace(job)
    try:
        threading.Thread(target=_run, args=(job, token_name, question),
                         name=f"mcp-job-{job.id[:8]}", daemon=True).start()
    except Exception as e:
        log_with_sid(log_safe_value(email, 254) or "mcp", "error",
                     f"MCP_JOB_START_FAILED error={log_safe_value(type(e).__name__, 80)}",
                     chat_id=log_safe_value(chat_id, 80))
        with _LOCK:
            _JOBS.pop(job.id, None)
            _release(job)
        raise JobRefused(NOT_STARTED_TEXT) from None
    return accepted


def get(job_id: Any, token_id: Any, email: Any) -> Optional[Job]:
    """A copy of the job, for the token and the user that started it only."""
    if not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id):
        return None
    with _LOCK:
        _expire()
        job = _JOBS.get(job_id)
        if job is None or job.token_id != token_id or job.email != email:
            return None
        return replace(job)


def drop_token(token_id: Any) -> None:
    """Forget the jobs of a token. A running turn is not stopped."""
    with _LOCK:
        for job in list(_JOBS.values()):
            if job.token_id == token_id:
                del _JOBS[job.id]


def reset() -> None:
    """Forget everything (tests)."""
    with _LOCK:
        _JOBS.clear()
        _RUNNING.clear()


def _expire() -> None:
    """Drop the jobs that finished too long ago. Called under `_LOCK`."""
    now = _clock()
    for job in list(_JOBS.values()):
        if job.state != "running" and now - job.finished_at > JOB_TTL_S:
            del _JOBS[job.id]


def _cap_count(keep: Optional[str] = None) -> None:
    while len(_JOBS) > MAX_JOBS and _drop_one(keep, lambda job: 1):
        pass


def _evict(keep: str) -> None:
    """Bring the store back within its bounds after the job `keep` finished.
    The bytes are judged only here: a later read must not cost an answer that
    was never read. Called under `_LOCK`."""
    _expire()
    _cap_count(keep)
    while (sum(j.content_bytes for j in _JOBS.values()) > MAX_CONTENT_BYTES
           and _drop_one(keep, lambda job: job.content_bytes)):
        pass


def _drop_one(keep: Optional[str], weight: Callable[[Job], int]) -> bool:
    """Drop the oldest finished job of the caller whose finished jobs weigh
    the most; False when there is none to drop. One caller's burst must not
    cost another caller its answer. Called under `_LOCK`."""
    weights: dict[tuple, int] = {}
    oldest: dict[tuple, Job] = {}       # a caller's oldest job that may go
    for job in _JOBS.values():
        if job.state == "running":
            continue
        caller = (job.token_id, job.email)
        weights[caller] = weights.get(caller, 0) + weight(job)
        if job.id != keep:
            oldest.setdefault(caller, job)
    if not oldest:
        return False
    del _JOBS[oldest[max(oldest, key=weights.get)].id]
    return True


def _release(job: Job) -> None:
    """Free the job's reservation. Called under `_LOCK`."""
    pair = (job.email, job.chat_id)
    if _RUNNING.get(pair) == job.id:
        del _RUNNING[pair]


def _finish(job: Job, outcome: _Outcome) -> None:
    """End the job and free its reservation in one step: whoever reads it as
    finished finds its content and may ask the next question. A failure here
    ends the job as failed."""
    failure = None
    with _LOCK:
        try:
            job.finished_at = _clock()
            job.conv_id = outcome.conv_id
            job.error_text = outcome.error_text
            job.content, job.content_bytes = outcome.content, outcome.content_bytes
            job.state = "failed" if outcome.error_text else "done"
            if _JOBS.pop(job.id, None) is not None:     # not dropped meanwhile
                _JOBS[job.id] = job
            _evict(keep=job.id)
        except Exception as e:
            failure = type(e).__name__
            job.finished_at = time.monotonic()
            job.error_text = GENERIC_TEXT
            job.content, job.content_bytes = _plain_content(GENERIC_TEXT)
            job.state = "failed"
        finally:
            _release(job)
    if failure:
        log_with_sid(log_safe_value(job.email, 254) or "mcp", "error",
                     f"MCP_JOB_FINISH_FAILED error={log_safe_value(failure, 80)}",
                     job_id=log_safe_value(job.id, 32))


def _content_bytes(content: list) -> int:
    """The size of the content's texts and image data."""
    if not isinstance(content, list):
        raise TypeError("the content is not a list")
    return sum(len(value.encode("utf-8", "surrogatepass"))
               for item in content if isinstance(item, dict)
               for value in item.values() if isinstance(value, str))


def _plain_content(text: str) -> tuple[list, int]:
    return [{"type": "text", "text": text}], len(text)


# ---------------------------------------------------------------------------
# the job thread
# ---------------------------------------------------------------------------
def _run(job: Job, token_name: str, question: str) -> None:
    started = time.monotonic()
    try:
        outcome = _turn(job, question)
    except BaseException as e:      # whatever it is, the job must end
        # The type only: the message can quote a column label or a value.
        log_with_sid(log_safe_value(job.email, 254) or "mcp", "error",
                     f"MCP_JOB_FAILED error={log_safe_value(type(e).__name__, 80)}",
                     chat_id=log_safe_value(job.chat_id, 80),
                     job_id=log_safe_value(job.id, 32))
        outcome = _Outcome(error_text=GENERIC_TEXT)
    if outcome.error_text:
        outcome.content, outcome.content_bytes = _error_content(job, outcome.error_text)
    _finish(job, outcome)
    log_with_sid(log_safe_value(job.email, 254) or "mcp", "info",
                 f"MCP_JOB_END state={log_safe_value(job.state, 20)} "
                 f"ms={int((time.monotonic() - started) * 1000)}",
                 token=log_safe_value(token_name, 120),
                 chat_id=log_safe_value(job.chat_id, 80),
                 conv_id=log_safe_value(job.conv_id, 80),
                 job_id=log_safe_value(job.id, 32))


def _turn(job: Job, question: str) -> _Outcome:
    """Run the question and save it, like a web question."""
    today = mcp_conversations.conversation_for_today(job.email, job.chat_id)
    if today and _chat._is_generating(today):
        return _Outcome(error_text=CONVERSATION_BUSY_TEXT)
    # Again: opening the store of a chat deleted meanwhile re-creates its folder.
    if not may_open(job.email, job.chat_id):
        return _Outcome(error_text=CHAT_NOT_FOUND_TEXT)
    turn = chat_turn.prepare_turn(
        job.email, job.chat_id, question,
        new_conversation=mcp_conversations.make_factory(job.email, job.chat_id))
    if isinstance(turn, chat_turn.TurnRefusal):
        text = turn.body.get("error")
        return _Outcome(error_text=text if isinstance(text, str) and text else GENERIC_TEXT)

    result = chat_turn.run_prepared_turn(
        turn, on_event=_activity_poster(job, turn.conv_id), title="never")
    outcome = _Outcome(conv_id=result.conv_id)
    if result.error_text:
        outcome.error_text = result.error_text
    elif result.record is None:
        outcome.error_text = GENERIC_TEXT
    else:
        outcome.content = mcp_answer.build_content(
            result.record, chat_id=job.chat_id, conv_id=result.conv_id,
            email=job.email, sid=result.sid, saved=result.persisted)
        outcome.content_bytes = _content_bytes(outcome.content)
    return outcome


def _activity_poster(job: Job, conv_id: str) -> Callable[[str, Any], None]:
    """The `on_event` of the turn: the activity event the web posts for every
    chart (routes/chat.py, the stream's consumer). It only reads the event."""
    def on_event(tag: str, event: Any) -> None:
        if tag != "event" or not isinstance(event, dict):
            return
        if event.get("narrative") or event.get("progress") or event.get("caption"):
            return
        payload = {"chat_id": job.chat_id, "conv_id": conv_id}
        if event.get("partial"):
            image = event.get("image_base64")
            payload["chart_n"] = event.get("chart_n")
        elif event.get("single_response"):
            image = (event.get("result") or {}).get("image_base64")
        else:
            return
        if not image:
            return
        try:
            brain_client.post_activity("plot_generated", job.email, payload)
        except Exception as e:
            log_with_sid(log_safe_value(job.email, 254) or "mcp", "warning",
                         f"MCP_ACTIVITY_FAILED error={log_safe_value(type(e).__name__, 80)}",
                         job_id=log_safe_value(job.id, 32))
    return on_event


def _error_content(job: Job, text: str) -> tuple[list, int]:
    """The content of a failed job and its size."""
    try:
        content = mcp_answer.error_content(text)
        return content, _content_bytes(content)
    except Exception as e:
        log_with_sid(log_safe_value(job.email, 254) or "mcp", "error",
                     f"MCP_ERROR_CONTENT_FAILED "
                     f"error={log_safe_value(type(e).__name__, 80)}",
                     job_id=log_safe_value(job.id, 32))
        return _plain_content(text)
