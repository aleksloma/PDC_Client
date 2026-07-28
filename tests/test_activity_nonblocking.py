"""post_activity must never block the caller: several call sites live inside
async handlers (login, chat SSE, report, upload), where a synchronous brain
call blocks uvicorn's event loop — a slow /v1/activity froze the whole
platform. The call is queued to a background worker and returns immediately;
the underlying post still happens, and its failure is swallowed with a log."""
import threading
import time

import brain_client


def test_post_activity_returns_immediately_even_when_brain_is_slow(monkeypatch):
    posted = threading.Event()

    def slow_post(path, body, sid=None):
        time.sleep(2.0)          # a slow brain (cold start / network stall)
        posted.set()
        return {"ok": True}

    monkeypatch.setattr(brain_client, "_post", slow_post)
    t0 = time.monotonic()
    out = brain_client.post_activity("login", "u@x.com")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"post_activity blocked the caller for {elapsed:.2f}s"
    assert out.get("ok") is True and out.get("queued") is True
    # ...and the post itself still goes out on the worker.
    assert posted.wait(timeout=5.0), "queued activity post never executed"


def test_post_activity_worker_failure_never_raises(monkeypatch):
    done = threading.Event()

    def failing_post(path, body, sid=None):
        done.set()
        raise RuntimeError("brain down")

    monkeypatch.setattr(brain_client, "_post", failing_post)
    out = brain_client.post_activity("report_exported", "u@x.com", {"k": "v"})
    assert out.get("ok") is True          # queuing succeeded; failure is async
    assert done.wait(timeout=5.0)
    time.sleep(0.1)                        # let the except path finish logging


def test_post_activity_events_stay_ordered(monkeypatch):
    seen = []
    lock = threading.Lock()
    done = threading.Event()

    def record_post(path, body, sid=None):
        with lock:
            seen.append(body["event"])
            if len(seen) == 3:
                done.set()
        return {"ok": True}

    monkeypatch.setattr(brain_client, "_post", record_post)
    for ev in ("login", "file_uploaded", "plot_generated"):
        brain_client.post_activity(ev, "u@x.com")
    assert done.wait(timeout=5.0)
    assert seen == ["login", "file_uploaded", "plot_generated"]
