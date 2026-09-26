"""Authentication hygiene: limiter memory, the reset-link index, the
background hand-off, and what reaches the logs.

The contract pinned here:

* `auth_limiter._create` — when a key space is over `MAX_KEYS`, the key
  evicted is the least recently touched one that is NOT live (its
  `locked_until` and `not_before` both at or before now); plain LRU only when
  every other key is live. A lockout or a pending spacing wait therefore
  cannot be erased by spraying fresh addresses. The key just created is never
  the one evicted.
* `auth_limiter.begin` records an allowed attempt up front under one lock, so
  a concurrent burst at the spacing point admits exactly one attempt.
* `AuthStore().load_reset_token_index()` scans `users/*/auth.json` once
  (the app lifespan calls it), fills `local_store._RESET_TOKEN_INDEX` and
  never raises (returns the number of indexed tokens, or None on failure).
  A miss still scans the records, before and after the fill, so a token
  another process wrote (an operator script, the integration suite) is
  found. Tokens minted after the fill are indexed at mint time.
* An unreadable `auth.json` met by the scan logs `AUTH_RECORD_UNREADABLE`
  with the exception TYPE, once per path, never the file content.
* A 429 from `GET`/`POST /auth/reset/{token}` carries the reset-link page
  headers (`Cache-Control: no-store`, `Referrer-Policy: no-referrer`) next
  to `Retry-After`.
* `routes.auth._run_in_background(fn, *args)` submits to a module
  single-worker `ThreadPoolExecutor` `_AUTH_BG_EXEC` (thread name prefix
  `auth_bg`, shut down through `atexit`); a raising `fn` is logged with its
  exception type and never propagates.
* `brain_client._post` escapes an error body before it reaches the
  newline-delimited log; for the two mail paths the body is not logged at
  all (the status code is). The raised exception types are unchanged.
* `logger_utils.ResetLinkRedactor`, attached to `uvicorn.access`, rewrites
  the segment after `/auth/reset/` in uvicorn's access record to
  `<redacted>` (query string included); any other record passes unchanged;
  the filter never drops a record.
* A legacy account holding both `password_hash` and `temp_password_hash`
  costs TWO verifications on a wrong password — the accepted, legacy-only
  difference from the one-verification rule; the answer is still the neutral
  401.

Offline: DATA_ROOT is tmp_path, the brain is stubbed, the limiter clock is
fake. Every assertion binds values to locals — never a Settings object.
"""
import ast
import hashlib
import importlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import local_store
from settings import settings

ROOT = Path(__file__).resolve().parent.parent

KNOWN = "known.user@corp.example"
KNOWN_PW = "Known-passw0rd-long"
BASE = "https://pdc.corp.example"
NEUTRAL_FAILURE = ("Sign-in failed. Check your email and password, or use "
                   "“Reset password” if you have not set one yet.")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ===========================================================================
# the limiter: live-key-aware eviction, a concurrent burst
# ===========================================================================
class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def lim(monkeypatch):
    mod = importlib.import_module("auth_limiter")
    clock = FakeClock()
    for name, value in (("AUTH_FAIL_THRESHOLD", 5), ("AUTH_FAIL_THRESHOLD_IP", 20),
                        ("AUTH_FAIL_WINDOW_S", 900), ("AUTH_LOCKOUT_S", 900)):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(mod, "clock", clock)
    mod.reset()
    yield mod, clock
    mod.reset()


def _begin(mod, email):
    # No peer: only the address space is exercised.
    return mod.begin("login", email, None)


def _lock_address(mod, clock, email):
    """Drive `email` through the free attempts and the spacing steps into
    the lockout, then confirm it is refused for (about) the lockout time."""
    for i in range(9):
        v = _begin(mod, email)
        assert v.allowed, (i + 1, v)
        clock.advance(8)
    locked = _begin(mod, email)
    assert locked.allowed is False and locked.retry_after_s > 8, locked


def test_a_locked_address_survives_a_spray_of_new_addresses(lim, monkeypatch):
    """Filling the space with fresh addresses must not erase a lockout:
    the evicted keys are the stale ones, never the live lockout."""
    mod, clock = lim
    monkeypatch.setattr(mod, "MAX_KEYS", 5)
    mod.reset()
    _lock_address(mod, clock, "locked@x.com")
    for i in range(5):
        assert _begin(mod, f"spray{i}@x.com").allowed
    v = _begin(mod, "locked@x.com")
    assert v.allowed is False, "the lockout was evicted by fresh addresses"
    assert v.retry_after_s > 8, v


def test_a_pending_spacing_wait_survives_while_stale_keys_exist(lim, monkeypatch):
    """A key whose not-before stamp is still in the future is live too."""
    mod, clock = lim
    monkeypatch.setattr(mod, "MAX_KEYS", 5)
    mod.reset()
    for i in range(5):
        assert _begin(mod, "spaced@x.com").allowed, i
    for i in range(5):
        assert _begin(mod, f"stale{i}@x.com").allowed
    v = _begin(mod, "spaced@x.com")
    assert v.allowed is False, "the pending wait was evicted by fresh addresses"
    assert v.retry_after_s == 1, v


def test_the_oldest_stale_key_is_the_one_evicted(lim, monkeypatch):
    """With a live key at the LRU end, the next-oldest stale key goes."""
    mod, clock = lim
    monkeypatch.setattr(mod, "MAX_KEYS", 3)
    mod.reset()
    for i in range(5):
        assert _begin(mod, "live@x.com").allowed
    for name in ("s1@x.com", "s2@x.com", "s3@x.com"):      # s3 overflows
        assert _begin(mod, name).allowed
    assert "live@x.com" in mod._space("login"), "the live key was evicted"
    assert "s1@x.com" not in mod._space("login"), "the oldest stale key survived"
    assert "s3@x.com" in mod._space("login"), "the key just created was evicted"


def test_when_every_key_is_live_the_least_recent_goes(lim, monkeypatch):
    """Plain LRU is the fallback, and the key being created is never the one
    evicted even though it is the only non-live key."""
    mod, clock = lim
    monkeypatch.setattr(mod, "MAX_KEYS", 3)
    mod.reset()
    for name in ("e1@x.com", "e2@x.com", "e3@x.com"):
        for i in range(5):
            assert _begin(mod, name).allowed, (name, i)
    assert _begin(mod, "e4@x.com").allowed
    space = mod._space("login")
    assert "e4@x.com" in space, "the key just created was evicted"
    assert "e1@x.com" not in space, "the least recently touched live key survived"
    assert _begin(mod, "e1@x.com").allowed is True


def test_a_concurrent_burst_at_the_spacing_point_admits_exactly_one(lim):
    """Recorded up front under one lock: twenty simultaneous attempts at the
    moment the next attempt becomes due -> one is allowed, the others see the
    not-before stamp it recorded. (A pin of existing behaviour.)"""
    mod, clock = lim
    email = "burst@x.com"
    for i in range(5):
        assert _begin(mod, email).allowed, i
    clock.advance(1)                                  # the 6th is now due
    n = 20
    barrier = threading.Barrier(n)
    verdicts = []
    guard = threading.Lock()

    def attempt(k):
        barrier.wait()
        v = mod.begin("login", email, f"10.0.0.{k}")
        with guard:
            verdicts.append(v)

    threads = [threading.Thread(target=attempt, args=(k,)) for k in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(verdicts) == n
    allowed = [v for v in verdicts if v.allowed]
    refused = [v for v in verdicts if not v.allowed]
    assert len(allowed) == 1, verdicts
    assert all(v.retry_after_s == 2 for v in refused), refused


# ===========================================================================
# the reset-token index
# ===========================================================================
@pytest.fixture
def store_world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    index = local_store._RESET_TOKEN_INDEX
    saved = dict(index)
    index.clear()
    store = local_store.AuthStore()
    store.ensure_user(KNOWN)
    store.set_password(KNOWN, KNOWN_PW)
    yield {"tmp": tmp_path, "store": store}
    index.clear()
    index.update(saved)


def _loader(store):
    fn = getattr(store, "load_reset_token_index", None)
    if fn is None:
        pytest.fail("AuthStore.load_reset_token_index missing")
    return fn


def _plant_token(tmp, email) -> str:
    """Write a live reset record straight to disk, bypassing the store (and
    therefore the index)."""
    token = "P" * 20 + "q" * 23
    udir = tmp / "users" / email
    udir.mkdir(parents=True, exist_ok=True)
    p = udir / "auth.json"
    rec = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    rec.update({"reset_token_hash": _sha(token),
                "reset_expires_at": int(time.time()) + 1800,
                "reset_used": False})
    p.write_text(json.dumps(rec), encoding="utf-8")
    return token


def test_before_the_fill_a_miss_still_scans_the_records(store_world):
    """The restart case for router-only apps: the token is on disk, the
    index is empty and was never filled -> found by the scan. (A pin of
    existing behaviour.)"""
    store = store_world["store"]
    token = store.create_reset_token(KNOWN)
    local_store._RESET_TOKEN_INDEX.clear()
    found = store.find_reset_token(token)
    assert found is not None and found[0] == KNOWN


def test_the_fill_indexes_every_outstanding_token(store_world):
    store = store_world["store"]
    other = "other.user@corp.example"
    store.ensure_user(other)
    t1 = store.create_reset_token(KNOWN)
    t2 = store.create_reset_token(other)
    local_store._RESET_TOKEN_INDEX.clear()          # a restart
    count = _loader(store)()
    assert count == 2, count
    index = dict(local_store._RESET_TOKEN_INDEX)
    assert index.get(_sha(t1)) == KNOWN and index.get(_sha(t2)) == other, sorted(index.values())
    assert store.find_reset_token(t1)[0] == KNOWN
    assert store.find_reset_token(t2)[0] == other


def test_after_the_fill_a_token_written_by_another_process_is_found(store_world):
    """A record written on disk behind this process's back after the fill
    (an operator script, the integration suite minting in the container) is
    still found: a miss scans the records. Making a miss final would ignore
    such a token until the next restart."""
    store = store_world["store"]
    _loader(store)()
    token = _plant_token(store_world["tmp"], KNOWN)
    found = store.find_reset_token(token)
    assert found is not None and found[0] == KNOWN


def test_a_token_minted_after_the_fill_is_found(store_world):
    store = store_world["store"]
    _loader(store)()
    token = store.create_reset_token(KNOWN)
    found = store.find_reset_token(token)
    assert found is not None and found[0] == KNOWN


def test_a_used_token_stays_recognised_after_a_restart_and_fill(store_world):
    """A consumed link keeps its hash (marked used); after a restart the
    fill must index it too, or a reuse would read as an unknown link."""
    store = store_world["store"]
    token = store.create_reset_token(KNOWN)
    assert store.consume_reset_token(token, "Brand-new-pw-123") == KNOWN
    local_store._RESET_TOKEN_INDEX.clear()
    _loader(store)()
    found = store.find_reset_token(token)
    assert found is not None and found[1].get("reset_used") is True


def test_the_fill_never_raises(store_world, monkeypatch):
    store = store_world["store"]
    loader = _loader(store)

    def boom(self):
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "iterdir", boom)
    monkeypatch.setattr(Path, "glob", lambda self, pattern: boom(self))
    result = loader()
    assert result is None or isinstance(result, int), result


def test_the_fill_skips_an_unreadable_record(store_world):
    store = store_world["store"]
    token = store.create_reset_token(KNOWN)
    local_store._RESET_TOKEN_INDEX.clear()
    bad = store_world["tmp"] / "users" / "broken.user@corp.example"
    bad.mkdir(parents=True)
    (bad / "auth.json").write_text("{not json", encoding="utf-8")
    count = _loader(store)()
    assert count == 1, count
    assert store.find_reset_token(token)[0] == KNOWN


def test_the_app_lifespan_fills_the_index():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    lifespans = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                 and n.name == "lifespan"]
    assert lifespans, "app.py has no lifespan function"
    body = ast.unparse(lifespans[0])
    assert "load_reset_token_index(" in body, "the lifespan does not fill the index"


# ===========================================================================
# an unreadable auth record is logged, once per path, type only
# ===========================================================================
@pytest.fixture
def store_log(monkeypatch):
    lines = []

    def capture(sid, level, message, **context):
        lines.append(" ".join([str(sid), str(level), str(message)]
                              + [f"{k}={v}" for k, v in context.items()]))

    monkeypatch.setattr(local_store, "log_with_sid", capture)
    return lines


CORRUPT_CONTENT = '{"password_hash": "SECRET-HASH-CONTENT", '


def _write_corrupt(tmp, email):
    d = tmp / "users" / email
    d.mkdir(parents=True, exist_ok=True)
    (d / "auth.json").write_text(CORRUPT_CONTENT, encoding="utf-8")


def _unreadable(lines):
    return [ln for ln in lines if "AUTH_RECORD_UNREADABLE" in ln]


def test_an_unreadable_record_is_logged_once_across_two_scans(store_world, store_log):
    _write_corrupt(store_world["tmp"], "broken.user@corp.example")
    store = store_world["store"]
    probe = "Q" * 43
    assert store.find_reset_token(probe) is None
    assert store.find_reset_token(probe) is None
    hits = _unreadable(store_log)
    assert len(hits) == 1, hits
    assert "JSONDecodeError" in hits[0], hits[0]
    assert "SECRET-HASH-CONTENT" not in hits[0], hits[0]
    assert "password_hash" not in hits[0], hits[0]


def test_each_unreadable_record_gets_its_own_line(store_world, store_log):
    for email in ("broken.one@corp.example", "broken.two@corp.example"):
        _write_corrupt(store_world["tmp"], email)
    store = store_world["store"]
    store.find_reset_token("Q" * 43)
    store.find_reset_token("Q" * 43)
    hits = _unreadable(store_log)
    assert len(hits) == 2, hits


def test_the_fill_logs_an_unreadable_record_too(store_world, store_log):
    _write_corrupt(store_world["tmp"], "broken.user@corp.example")
    _loader(store_world["store"])()
    hits = _unreadable(store_log)
    assert len(hits) == 1, hits
    assert "SECRET-HASH-CONTENT" not in hits[0], hits[0]


# ===========================================================================
# the auth routes: 429 on the reset-link routes, the background hand-off,
# the legacy two-hash case
# ===========================================================================
@pytest.fixture
def auth_app(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE)
    monkeypatch.setattr(settings, "ALLOW_SELF_REGISTRATION", False)
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    local_store._RESET_TOKEN_INDEX.clear()
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "send_password_reset_email",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    monkeypatch.setattr(auth_mod, "_run_in_background",
                        lambda fn, *args: fn(*args), raising=False)
    store = local_store.AuthStore()
    store.ensure_user(KNOWN)
    store.set_password(KNOWN, KNOWN_PW)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)
    return {"tmp": tmp_path, "client": lambda: TestClient(app, raise_server_exceptions=False),
            "auth_mod": auth_mod, "store": store}


@pytest.fixture
def refuse_everything(monkeypatch):
    import auth_limiter
    monkeypatch.setattr(auth_limiter, "begin",
                        lambda *a, **k: auth_limiter.Verdict(False, 3))


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_refused_reset_link_request_keeps_the_link_page_headers(auth_app, refuse_everything,
                                                                  method):
    token = auth_app["store"].create_reset_token(KNOWN)
    tc = auth_app["client"]()
    if method == "GET":
        r = tc.get(f"/auth/reset/{token}", follow_redirects=False)
    else:
        r = tc.post(f"/auth/reset/{token}",
                    data={"new_password": "Brand-new-pw-123",
                          "confirm_password": "Brand-new-pw-123"},
                    follow_redirects=False)
    status = r.status_code
    headers = {k.lower(): v for k, v in r.headers.items()}
    assert status == 429, status
    assert headers.get("retry-after") == "3", headers
    assert headers.get("cache-control") == "no-store", headers
    assert headers.get("referrer-policy") == "no-referrer", headers


def test_the_sign_in_429_still_carries_retry_after(auth_app, refuse_everything):
    r = auth_app["client"]().post("/auth/login",
                                  data={"email": KNOWN, "password": "whatever-pw-1"},
                                  follow_redirects=False)
    assert r.status_code == 429
    assert r.headers.get("retry-after") == "3"


def _real_auth_module():
    import routes.auth as auth_mod
    return auth_mod


def _bg_executor(auth_mod):
    ex = getattr(auth_mod, "_AUTH_BG_EXEC", None)
    if ex is None:
        pytest.fail("routes.auth._AUTH_BG_EXEC missing")
    return ex


def test_the_background_executor_is_a_single_worker_pool():
    ex = _bg_executor(_real_auth_module())
    assert isinstance(ex, ThreadPoolExecutor), type(ex)
    workers = ex._max_workers
    prefix = ex._thread_name_prefix
    assert workers == 1, workers
    assert prefix.startswith("auth_bg"), prefix


def test_the_background_hand_off_runs_on_the_executor(monkeypatch):
    auth_mod = _real_auth_module()
    ex = _bg_executor(auth_mod)
    submitted = []
    real_submit = ex.submit

    def spy(fn, *args, **kwargs):
        submitted.append(fn)
        return real_submit(fn, *args, **kwargs)

    monkeypatch.setattr(ex, "submit", spy)
    done = threading.Event()
    seen = {}

    def job(value):
        seen["thread"] = threading.current_thread().name
        seen["value"] = value
        done.set()

    auth_mod._run_in_background(job, 42)
    assert done.wait(5), "the job never ran"
    assert submitted, "the hand-off did not go through _AUTH_BG_EXEC"
    assert seen["value"] == 42
    assert seen["thread"].startswith(ex._thread_name_prefix + "_"), seen["thread"]


def test_a_failing_background_job_is_logged_and_never_propagates(monkeypatch):
    auth_mod = _real_auth_module()
    _bg_executor(auth_mod)
    lines = []

    def capture(sid, level, message, **context):
        lines.append(f"{level} {message}")

    monkeypatch.setattr(auth_mod, "log_with_sid", capture)

    def job():
        raise ZeroDivisionError("private-detail")

    auth_mod._run_in_background(job)              # must not raise
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any("ZeroDivisionError" in ln for ln in lines):
        time.sleep(0.02)
    hits = [ln for ln in lines if "ZeroDivisionError" in ln]
    assert hits, lines
    assert "private-detail" not in hits[0], hits[0]


def test_the_background_executor_is_shut_down_at_exit():
    src = (ROOT / "routes" / "auth.py").read_text(encoding="utf-8")
    assert re.search(r"_AUTH_BG_EXEC\s*=\s*ThreadPoolExecutor\(", src), \
        "no module-level _AUTH_BG_EXEC executor"
    assert re.search(r"atexit\.register\([^\n]*_AUTH_BG_EXEC", src), \
        "no atexit shutdown for _AUTH_BG_EXEC"


@pytest.fixture
def verifications(monkeypatch):
    import password_utils
    import routes.auth as auth_mod
    real = password_utils.check_password_hash
    calls = []

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(password_utils, "check_password_hash", counting)
    for mod in (auth_mod, local_store):
        if hasattr(mod, "check_password_hash"):
            monkeypatch.setattr(mod, "check_password_hash", counting)
    return calls


def test_a_legacy_two_hash_account_costs_two_verifications(auth_app, verifications):
    """ACCEPTED, LEGACY-ONLY difference: an account still holding the
    previous release's `temp_password_hash` next to its own `password_hash`
    verifies both on a wrong password, so its failure costs two PBKDF2 runs
    where every other failure costs one. Nothing writes the temp slot any
    more, so the case disappears as those accounts sign in; the page stays
    the neutral 401. (A pin of existing behaviour.)"""
    from password_utils import generate_password_hash
    p = auth_app["tmp"] / "users" / KNOWN / "auth.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["temp_password_hash"] = generate_password_hash("Old-temp-passw0rd")
    rec["must_change_password"] = True
    p.write_text(json.dumps(rec), encoding="utf-8")
    verifications.clear()
    r = auth_app["client"]().post("/auth/login",
                                  data={"email": KNOWN, "password": "Wrong-passw0rd-x"},
                                  follow_redirects=False)
    assert r.status_code == 401, r.status_code
    assert NEUTRAL_FAILURE in r.text
    assert len(verifications) == 2, len(verifications)


# ===========================================================================
# brain error bodies in the log
# ===========================================================================
FORGED_BODY = "line1\nFORGED 2026-01-01 | INFO | x"


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.text = FORGED_BODY

    def json(self):
        return {"error": FORGED_BODY}


@pytest.fixture
def brain_log(monkeypatch):
    lines = []

    def capture(sid, level, message, **context):
        lines.append(" ".join([str(message)] + [f"{k}={v}" for k, v in context.items()]))

    monkeypatch.setattr(brain_client, "log_with_sid", capture)
    monkeypatch.setattr(brain_client.settings, "BRAIN_TENANT_TOKEN", "tkn", raising=False)
    monkeypatch.setattr(brain_client.settings, "CLIENT_LLM_DEBUG", False, raising=False)
    return lines


def _answer(monkeypatch, status):
    class _Client:
        def post(self, path, json=None, headers=None, timeout=None):
            return _Resp(status)

    monkeypatch.setattr(brain_client, "_get_client", lambda: _Client())


def _expected_error(status):
    return brain_client.TenantRevokedError if status in (401, 403) else brain_client.BrainError


@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_an_error_body_reaches_the_log_escaped(monkeypatch, brain_log, status):
    _answer(monkeypatch, status)
    with pytest.raises(_expected_error(status)) as exc:
        brain_client._post("/v1/plan", {"q": "x"}, sid="t")
    if status not in (401, 403):
        assert not isinstance(exc.value, brain_client.TenantRevokedError)
    joined = "\n".join(brain_log)
    assert brain_log, "nothing was logged"
    for line in brain_log:
        assert "\n" not in line and "\r" not in line, repr(line)
    assert "line1" in joined, joined
    assert str(status) in joined, joined


@pytest.mark.parametrize("path", ["/v1/send_password_reset_email", "/v1/send_welcome_email"])
@pytest.mark.parametrize("status", [400, 401, 500])
def test_a_mail_path_error_body_is_not_logged(monkeypatch, brain_log, path, status):
    _answer(monkeypatch, status)
    with pytest.raises(_expected_error(status)):
        brain_client._post(path, {"email": KNOWN}, sid="t")
    joined = "\n".join(brain_log)
    assert brain_log, "nothing was logged"
    assert "line1" not in joined and "FORGED" not in joined, joined
    assert str(status) in joined, joined


# ===========================================================================
# the reset token in the access log
# ===========================================================================
ACCESS_FMT = '%s - "%s %s HTTP/%s" %d'
TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE"


def _redactor_cls():
    import logger_utils
    cls = getattr(logger_utils, "ResetLinkRedactor", None)
    if cls is None:
        pytest.fail("logger_utils.ResetLinkRedactor missing")
    return cls


def _record(msg, args):
    return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, msg, args, None)


def test_the_token_fixture_is_link_shaped():
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", TOKEN), len(TOKEN)


def test_the_redactor_is_attached_to_the_access_logger(tmp_path, monkeypatch):
    import logger_utils
    cls = _redactor_cls()
    monkeypatch.setattr(logger_utils.settings, "DATA_ROOT", str(tmp_path))
    logger_utils.get_logger()
    filters = list(logging.getLogger("uvicorn.access").filters)
    assert any(isinstance(f, cls) for f in filters), [type(f).__name__ for f in filters]
    logger_utils.get_logger()
    again = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, cls)]
    assert len(again) == 1, "attached more than once"


@pytest.mark.parametrize("path", [f"/auth/reset/{TOKEN}", f"/auth/reset/{TOKEN}?utm=mail&x=1"])
def test_the_reset_token_is_redacted(path):
    rec = _record(ACCESS_FMT, ("1.2.3.4:5", "GET", path, "1.1", 200))
    assert _redactor_cls()().filter(rec) is True
    msg = rec.getMessage()
    assert "/auth/reset/<redacted>" in msg, msg
    assert TOKEN not in msg, msg
    assert "utm=" not in msg and "x=1" not in msg, msg
    assert msg.startswith("1.2.3.4:5 - \"GET ") and msg.endswith("HTTP/1.1\" 200"), msg


@pytest.mark.parametrize("path", ["/auth/reset_password", "/lab", "/auth/login?next=/lab",
                                  "/static/app.js"])
def test_other_paths_pass_unchanged(path):
    args = ("1.2.3.4:5", "POST", path, "1.1", 302)
    rec = _record(ACCESS_FMT, args)
    assert _redactor_cls()().filter(rec) is True
    assert rec.getMessage() == ACCESS_FMT % args


@pytest.mark.parametrize("msg,args", [
    ("plain message /auth/reset/" + TOKEN, None),
    ("%s %s %s", ("GET", "/auth/reset/" + TOKEN, "x")),
    ("%s", ("/auth/reset/" + TOKEN,)),
])
def test_an_unexpected_record_shape_passes_through(msg, args):
    rec = _record(msg, args)
    before = rec.getMessage()
    assert _redactor_cls()().filter(rec) is True
    assert rec.getMessage() == before


def test_the_background_executor_shutdown_comment_says_the_worker_is_joined():
    """By the time `atexit` handlers run, the interpreter has already joined
    the pool's worker thread, so queued mails have run to completion; the
    shutdown is a no-op safety net. The comment above the registration must
    say so rather than suggest it cancels anything."""
    lines = (ROOT / "routes" / "auth.py").read_text(encoding="utf-8").splitlines()
    idx = [i for i, ln in enumerate(lines)
           if re.search(r"atexit\.register\([^\n]*_AUTH_BG_EXEC", ln)]
    assert idx, "no atexit shutdown for _AUTH_BG_EXEC"
    above = "\n".join(lines[max(0, idx[0] - 3):idx[0]])
    assert "joined" in above.lower(), above
