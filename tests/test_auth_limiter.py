"""auth_limiter — the in-memory sign-in / reset attempt limiter (Task 9, R-08).

The contract pinned here (plan D9-12 .. D9-14):

* `auth_limiter.begin(kind, email, ip) -> Verdict(allowed, retry_after_s)`
  with `kind` in {"login", "reset", "token"}. The attempt is RECORDED AS A
  FAILURE UP FRONT (a concurrent burst cannot all pass the check before the
  first failure lands); `success(kind, email, ip)` retracts it — it clears
  the address key and takes ONE failure off the IP key; `reset()` empties
  everything (tests).
* Key spaces: `login:<email>`, `reset:<email>` (independent buckets, so two
  reset clicks never count against sign-in) and `ip:<peer>` shared by every
  kind. At most `MAX_KEYS` (10 000) keys per space, the least recently
  touched evicted.
* Address schedule: `AUTH_FAIL_THRESHOLD` (5) failures inside
  `AUTH_FAIL_WINDOW_S` (900) are free; the 6th, 7th, 8th and 9th attempts are
  allowed no sooner than 1, 2, 4 and 8 s after the previous one (a
  NOT-BEFORE stamp answered with `retry_after_s`, never a server-side sleep);
  from the 10th attempt on, the address key is LOCKED for `AUTH_LOCKOUT_S`
  (900). An attempt refused because it came early (or during the lockout) is
  NOT recorded as a new failure — it is refused before any evaluation.
* IP schedule: beyond `AUTH_FAIL_THRESHOLD_IP` (20) failures the same 1, 2,
  4, 8 s spacing (capped at 8), but the IP key NEVER locks (an unforwarded
  proxy address would otherwise let one caller lock out everyone).
* The worse of the address and IP verdicts applies.
* The thresholds are read from `settings` at CALL time; `clock` is a module
  attribute (a zero-argument callable returning seconds) the tests replace,
  so nothing here ever waits.

Offline, pure unit: no app, no storage.
"""
import importlib

import pytest

from settings import settings

EMAIL = "limited@x.com"
IP = "10.9.8.7"


def _mod():
    try:
        return importlib.import_module("auth_limiter")
    except ImportError as e:
        pytest.fail(f"auth_limiter module missing: {e}")


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _set(monkeypatch, name, value):
    if name not in type(settings).model_fields:
        pytest.fail(f"settings.{name} missing")
    monkeypatch.setattr(settings, name, value)


class _Missing:
    """Stands in for the module before it exists, so a test FAILS (not
    errors in its fixture) on its first use."""

    def __getattr__(self, name):
        pytest.fail("auth_limiter module missing")


@pytest.fixture
def lim(monkeypatch):
    try:
        mod = importlib.import_module("auth_limiter")
    except ImportError:
        mod = None
    clock = FakeClock()
    for name, value in (("AUTH_FAIL_THRESHOLD", 5), ("AUTH_FAIL_THRESHOLD_IP", 20),
                        ("AUTH_FAIL_WINDOW_S", 900), ("AUTH_LOCKOUT_S", 900)):
        if name in type(settings).model_fields:
            monkeypatch.setattr(settings, name, value)
    if mod is None:
        yield _Missing(), clock
        return
    monkeypatch.setattr(mod, "clock", clock)
    mod.reset()
    yield mod, clock
    mod.reset()


def _begin(mod, kind="login", email=EMAIL, ip=IP):
    v = mod.begin(kind, email, ip)
    assert hasattr(v, "allowed") and hasattr(v, "retry_after_s"), v
    return v


def _five_failures(mod, **kw):
    for i in range(5):
        v = _begin(mod, **kw)
        assert v.allowed, (i + 1, v)


# ---------------------------------------------------------------------------
# shape
# ---------------------------------------------------------------------------
def test_the_module_exposes_its_api():
    mod = _mod()
    for name in ("begin", "success", "reset", "clock"):
        assert hasattr(mod, name), name
    assert getattr(mod, "MAX_KEYS", None) == 10_000


def test_an_allowed_verdict_carries_no_wait(lim):
    mod, _ = lim
    v = _begin(mod)
    assert v.allowed is True
    assert v.retry_after_s == 0
    assert isinstance(v.retry_after_s, int)


def test_settings_defaults(monkeypatch):
    from settings import Settings
    for name in ("AUTH_FAIL_THRESHOLD", "AUTH_FAIL_THRESHOLD_IP",
                 "AUTH_FAIL_WINDOW_S", "AUTH_LOCKOUT_S"):
        monkeypatch.delenv(name, raising=False)
    fresh = Settings()
    values = {name: getattr(fresh, name, None) for name in (
        "AUTH_FAIL_THRESHOLD", "AUTH_FAIL_THRESHOLD_IP",
        "AUTH_FAIL_WINDOW_S", "AUTH_LOCKOUT_S")}
    assert values == {"AUTH_FAIL_THRESHOLD": 5, "AUTH_FAIL_THRESHOLD_IP": 20,
                      "AUTH_FAIL_WINDOW_S": 900, "AUTH_LOCKOUT_S": 900}, values


@pytest.mark.parametrize("raw", ["abc", "", "-3"])
def test_a_garbled_threshold_falls_back_to_a_safe_value(monkeypatch, raw):
    """The `_int_env` idiom: a typo in the env never crashes Settings()."""
    from settings import Settings
    monkeypatch.setenv("AUTH_FAIL_THRESHOLD", raw)
    value = getattr(Settings(), "AUTH_FAIL_THRESHOLD", None)
    assert isinstance(value, int) and value >= 1, value


# ---------------------------------------------------------------------------
# the address schedule
# ---------------------------------------------------------------------------
def test_under_the_threshold_every_attempt_is_allowed(lim):
    mod, _ = lim
    _five_failures(mod)


def test_the_sixth_attempt_must_wait_one_second(lim):
    mod, clock = lim
    _five_failures(mod)
    v = _begin(mod)
    assert v.allowed is False
    assert v.retry_after_s == 1, v
    clock.advance(1)
    assert _begin(mod).allowed is True


def test_the_schedule_is_one_two_four_eight_seconds(lim):
    mod, clock = lim
    _five_failures(mod)
    for gap in (1, 2, 4, 8):
        early = _begin(mod)
        assert early.allowed is False, gap
        assert early.retry_after_s == gap, (gap, early)
        clock.advance(gap - 0.5)
        half = _begin(mod)
        assert half.allowed is False, gap
        assert half.retry_after_s == 1, (gap, half)
        clock.advance(0.5)
        assert _begin(mod).allowed is True, gap


def test_an_early_attempt_is_not_recorded_as_a_failure(lim):
    """Refused before evaluation: hammering the key while it waits does not
    advance the schedule. After any number of early refusals the 6th attempt
    still needs only 1 s, and the 7th then needs 2 s (not 4 or 8)."""
    mod, clock = lim
    _five_failures(mod)
    for _ in range(10):
        assert _begin(mod).allowed is False
    clock.advance(1)
    assert _begin(mod).allowed is True          # the 6th
    v = _begin(mod)
    assert v.allowed is False and v.retry_after_s == 2, v
    clock.advance(2)
    assert _begin(mod).allowed is True          # the 7th


def _nine_failures(mod, clock, **kw):
    _five_failures(mod, **kw)
    for gap in (1, 2, 4, 8):
        clock.advance(gap)
        v = _begin(mod, **kw)
        assert v.allowed, (gap, v)


def test_the_tenth_attempt_is_locked_out(lim):
    mod, clock = lim
    _nine_failures(mod, clock)
    clock.advance(16)            # past any spacing: only the lockout refuses
    v = _begin(mod)
    assert v.allowed is False
    assert 880 <= v.retry_after_s <= 900, v


def test_the_lockout_refuses_immediately_with_the_remaining_time(lim):
    mod, clock = lim
    _nine_failures(mod, clock)
    clock.advance(1)
    first = _begin(mod)
    assert first.allowed is False and first.retry_after_s > 16, first
    clock.advance(300)
    later = _begin(mod)
    assert later.allowed is False
    assert later.retry_after_s <= first.retry_after_s - 299, (first, later)
    assert later.retry_after_s >= 1


def test_the_lockout_expires(lim):
    mod, clock = lim
    _nine_failures(mod, clock)
    clock.advance(1)
    locked = _begin(mod)
    assert locked.allowed is False
    clock.advance(locked.retry_after_s + 1)
    v = _begin(mod)
    assert v.allowed is True, v


def test_the_lockout_follows_the_setting(lim, monkeypatch):
    mod, clock = lim
    _set(monkeypatch, "AUTH_LOCKOUT_S", 60)
    _nine_failures(mod, clock)
    clock.advance(16)
    v = _begin(mod)
    assert v.allowed is False and v.retry_after_s <= 60, v


def test_failures_leave_the_window(lim):
    mod, clock = lim
    _five_failures(mod)
    clock.advance(901)
    _five_failures(mod)                         # a fresh five
    v = _begin(mod)
    assert v.allowed is False and v.retry_after_s == 1, v


def test_the_threshold_is_read_at_call_time(lim, monkeypatch):
    mod, _ = lim
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD", 2)
    assert _begin(mod).allowed and _begin(mod).allowed
    v = _begin(mod)
    assert v.allowed is False and v.retry_after_s == 1, v


# ---------------------------------------------------------------------------
# success / buckets / reset
# ---------------------------------------------------------------------------
def test_success_clears_the_address_key(lim):
    mod, _ = lim
    _five_failures(mod)
    assert _begin(mod).allowed is False
    mod.success("login", EMAIL, IP)
    _five_failures(mod)                         # counting starts over


def test_success_takes_one_failure_off_the_ip_key(lim, monkeypatch):
    mod, _ = lim
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 3)
    assert _begin(mod, email="a1@x.com").allowed
    assert _begin(mod, email="a2@x.com").allowed
    mod.success("login", "a2@x.com", IP)        # the IP key is back to one
    assert _begin(mod, email="a3@x.com").allowed
    assert _begin(mod, email="a4@x.com").allowed, "the retracted failure still counts"
    v = _begin(mod, email="a5@x.com")           # now three: spacing applies
    assert v.allowed is False and v.retry_after_s == 1, v


def test_login_and_reset_buckets_are_independent(lim):
    mod, _ = lim
    _five_failures(mod, kind="login")
    assert _begin(mod, kind="login").allowed is False
    _five_failures(mod, kind="reset")           # the reset bucket is untouched
    assert _begin(mod, kind="reset").allowed is False
    mod.success("login", EMAIL, IP)
    assert _begin(mod, kind="login").allowed is True
    assert _begin(mod, kind="reset").allowed is False


def test_addresses_are_independent(lim):
    mod, _ = lim
    _five_failures(mod)
    assert _begin(mod).allowed is False
    assert _begin(mod, email="someone-else@x.com").allowed is True


def test_the_token_kind_counts_per_ip_without_an_address(lim, monkeypatch):
    mod, _ = lim
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 3)
    for _ in range(3):
        assert _begin(mod, kind="token", email=None).allowed
    v = _begin(mod, kind="token", email=None)
    assert v.allowed is False and v.retry_after_s == 1, v
    # The IP key is shared by every kind.
    assert _begin(mod, kind="login", email="fresh@x.com").allowed is False


def test_reset_empties_every_key(lim):
    mod, clock = lim
    _nine_failures(mod, clock)
    assert _begin(mod).allowed is False
    mod.reset()
    _five_failures(mod)


# ---------------------------------------------------------------------------
# the IP key: spacing, never a lockout
# ---------------------------------------------------------------------------
def test_the_ip_key_spaces_attempts_beyond_its_threshold(lim, monkeypatch):
    mod, clock = lim
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 3)
    for i in range(3):
        assert _begin(mod, email=f"s{i}@x.com").allowed
    for n, gap in enumerate((1, 2, 4, 8)):
        early = _begin(mod, email=f"early{n}@x.com")
        assert early.allowed is False and early.retry_after_s == gap, (gap, early)
        clock.advance(gap)
        assert _begin(mod, email=f"t{n}@x.com").allowed, gap


def test_the_ip_key_never_locks(lim, monkeypatch):
    """Twenty spaced attempts beyond the IP threshold, each from a fresh
    address: every one is served after at most 8 s, none is locked out."""
    mod, clock = lim
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 3)
    for i in range(3):
        assert _begin(mod, email=f"s{i}@x.com").allowed
    for i in range(20):
        early = _begin(mod, email=f"probe{i}@x.com")
        if not early.allowed:
            assert early.retry_after_s <= 8, (i, early)
        clock.advance(8)
        v = _begin(mod, email=f"spaced{i}@x.com")
        assert v.allowed, (i, v)


def test_the_default_ip_threshold_is_twenty(lim):
    mod, _ = lim
    for i in range(20):
        assert _begin(mod, email=f"spray{i}@x.com").allowed, i
    v = _begin(mod, email="spray-late@x.com")
    assert v.allowed is False and v.retry_after_s == 1, v


def test_another_peer_is_not_affected(lim, monkeypatch):
    mod, _ = lim
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 3)
    for i in range(3):
        assert _begin(mod, email=f"s{i}@x.com").allowed
    assert _begin(mod, email="next@x.com").allowed is False
    assert _begin(mod, email="next@x.com", ip="10.0.0.99").allowed is True


def test_the_worse_verdict_applies(lim, monkeypatch):
    """A locked address stays refused from a fresh peer; a throttled peer is
    refused for a fresh address."""
    mod, clock = lim
    _nine_failures(mod, clock)
    clock.advance(16)
    assert _begin(mod, ip="10.1.1.1").allowed is False
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 3)
    mod.reset()
    for i in range(3):
        assert _begin(mod, email=f"s{i}@x.com").allowed
    assert _begin(mod, email="clean@x.com").allowed is False


# ---------------------------------------------------------------------------
# bounded memory
# ---------------------------------------------------------------------------
def test_the_key_cap_evicts_the_least_recently_touched(lim, monkeypatch):
    mod, clock = lim
    mod.MAX_KEYS  # noqa: B018 -- fails cleanly while the module is missing
    monkeypatch.setattr(mod, "MAX_KEYS", 3)
    mod.reset()
    _five_failures(mod, email="e1@x.com")
    # Past e1's one-second wait: e1 is now the least recently touched key and
    # holds no live wait, so it is the one a new key evicts. (A key that is
    # still waiting or locked is never evicted while a stale one exists.)
    clock.advance(2)
    assert _begin(mod, email="e2@x.com").allowed
    assert _begin(mod, email="e3@x.com").allowed
    assert _begin(mod, email="e4@x.com").allowed    # evicts e1 (least recent, stale)
    # Evicted: e1 starts from scratch, so two quick attempts are both free.
    # Kept, it would carry six failures and the second would have to wait.
    assert _begin(mod, email="e1@x.com").allowed is True
    assert _begin(mod, email="e1@x.com").allowed is True, "e1 was not evicted"


def test_a_recently_touched_key_survives_the_cap(lim, monkeypatch):
    mod, clock = lim
    mod.MAX_KEYS  # noqa: B018 -- fails cleanly while the module is missing
    monkeypatch.setattr(mod, "MAX_KEYS", 3)
    mod.reset()
    _five_failures(mod, email="e1@x.com")
    assert _begin(mod, email="e2@x.com").allowed
    assert _begin(mod, email="e3@x.com").allowed
    clock.advance(1)
    assert _begin(mod, email="e1@x.com").allowed     # e1 touched: now the newest
    assert _begin(mod, email="e4@x.com").allowed     # evicts e2, not e1
    v = _begin(mod, email="e1@x.com")
    assert v.allowed is False and v.retry_after_s == 2, v


def test_the_ip_space_is_capped_too(lim, monkeypatch):
    mod, clock = lim
    mod.MAX_KEYS  # noqa: B018 -- fails cleanly while the module is missing
    monkeypatch.setattr(mod, "MAX_KEYS", 3)
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 2)
    mod.reset()
    assert _begin(mod, email="a@x.com", ip="1.1.1.1").allowed
    assert _begin(mod, email="b@x.com", ip="1.1.1.1").allowed
    assert _begin(mod, email="c@x.com", ip="1.1.1.1").allowed is False
    # Past the peer's wait: 1.1.1.1 is now stale and the least recently touched.
    clock.advance(2)
    for i, peer in enumerate(("2.2.2.2", "3.3.3.3", "4.4.4.4")):
        assert _begin(mod, email=f"p{i}@x.com", ip=peer).allowed
    # Evicted: the peer starts from scratch, so two quick attempts are free.
    # Kept, its third failure would impose a wait on the next attempt.
    assert _begin(mod, email="d@x.com", ip="1.1.1.1").allowed is True
    assert _begin(mod, email="e@x.com", ip="1.1.1.1").allowed is True, \
        "the least recently touched peer was not evicted"


# ---------------------------------------------------------------------------
# lockout=False: spaced, never locked (the configured local admin account)
#
# `begin(kind, email, ip, *, lockout=True)`. With lockout=False the address
# key keeps the 1, 2, 4, 8 s spacing and, where it would lock, sets another
# 8 s not-before instead: never a lockout, never an AUTH_LOCKOUT line. The
# thresholds and the IP schedule are unchanged.
# ---------------------------------------------------------------------------
def _begin_nolock(mod, email=EMAIL, ip=IP, kind="login"):
    v = mod.begin(kind, email, ip, lockout=False)
    assert hasattr(v, "allowed") and hasattr(v, "retry_after_s"), v
    return v


def _capture_limiter_logs(mod, monkeypatch):
    lines = []

    def rec(sid, level, message, **ctx):
        lines.append(f"{sid} {level} {message} {ctx}")

    monkeypatch.setattr(mod, "log_with_sid", rec, raising=False)
    return lines


def test_without_lockout_the_threshold_and_first_steps_are_unchanged(lim):
    mod, clock = lim
    for i in range(5):
        assert _begin_nolock(mod).allowed, i + 1
    for gap in (1, 2, 4, 8):
        early = _begin_nolock(mod)
        assert early.allowed is False and early.retry_after_s == gap, (gap, early)
        clock.advance(gap)
        assert _begin_nolock(mod).allowed is True, gap


def test_without_lockout_the_address_is_spaced_at_eight_seconds_and_never_locked(lim, monkeypatch):
    mod, clock = lim
    lines = _capture_limiter_logs(mod, monkeypatch)
    for i in range(5):
        assert _begin_nolock(mod).allowed, i + 1
    admitted = 0
    for n in range(14):                       # far past the point that locks by default
        early = _begin_nolock(mod)
        assert early.allowed is False, n
        assert 1 <= early.retry_after_s <= 8, (n, early)
        clock.advance(early.retry_after_s)
        v = _begin_nolock(mod)
        assert v.allowed is True, (n, v, "locked instead of spaced")
        admitted += 1
    assert admitted == 14
    # From the would-be lockout on, the step stays at 8 s.
    early = _begin_nolock(mod)
    assert early.allowed is False and early.retry_after_s == 8, early
    assert not [ln for ln in lines if "AUTH_LOCKOUT" in ln], lines


def test_without_lockout_the_would_be_lock_point_is_an_eight_second_wait(lim):
    mod, clock = lim
    for i in range(5):
        assert _begin_nolock(mod).allowed
    for gap in (1, 2, 4, 8):
        clock.advance(gap)
        assert _begin_nolock(mod).allowed, gap
    clock.advance(8)
    assert _begin_nolock(mod).allowed is True, "the 10th attempt was locked"
    v = _begin_nolock(mod)
    assert v.allowed is False and v.retry_after_s == 8, v
    clock.advance(7)
    v = _begin_nolock(mod)
    assert v.allowed is False and v.retry_after_s == 1, v
    clock.advance(1)
    assert _begin_nolock(mod).allowed is True


def test_the_default_still_locks(lim, monkeypatch):
    mod, clock = lim
    lines = _capture_limiter_logs(mod, monkeypatch)
    for i in range(5):
        assert mod.begin("login", EMAIL, IP, lockout=True).allowed
    for gap in (1, 2, 4, 8):
        clock.advance(gap)
        assert mod.begin("login", EMAIL, IP, lockout=True).allowed, gap
    clock.advance(16)
    v = mod.begin("login", EMAIL, IP)
    assert v.allowed is False and v.retry_after_s > 8, v
    assert len([ln for ln in lines if "AUTH_LOCKOUT" in ln]) == 1, lines


def test_without_lockout_the_ip_schedule_still_applies(lim, monkeypatch):
    mod, clock = lim
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 3)
    for i in range(3):
        assert _begin_nolock(mod, email=f"n{i}@x.com").allowed
    v = _begin_nolock(mod, email="fresh@x.com")
    assert v.allowed is False and v.retry_after_s == 1, v


def test_a_no_lockout_key_does_not_shield_another_address(lim):
    """The flag is per call: a normal address from the same peer still locks."""
    mod, clock = lim
    for _ in range(5):
        assert _begin_nolock(mod, email="admin-id").allowed
    _nine_failures(mod, clock)
    clock.advance(16)
    v = _begin(mod)
    assert v.allowed is False and v.retry_after_s > 8, v


# ---------------------------------------------------------------------------
# lockout=False: the would-be lock point is reported, once per window
#
# Where a `lockout=True` key would lock, a `lockout=False` key logs one
# warning `AUTH_ADMIN_SPACED kind=<kind> h=<12 hex>` -- once per
# AUTH_FAIL_WINDOW_S per key, never the address, never `AUTH_LOCKOUT`.
# `reset()` forgets the latch too.
# ---------------------------------------------------------------------------
_SPACED_RE = r"AUTH_ADMIN_SPACED kind=(\w+) h=([0-9a-f]{12})\b"


def _spaced(lines):
    import re
    return [ln for ln in lines if re.search(_SPACED_RE, ln)]


def _to_the_would_be_lock_point(mod, clock, email=EMAIL, kind="login"):
    """Five free attempts, the four spaced ones, then the 10th -- the
    attempt at which a lockout=True key locks."""
    for i in range(5):
        assert _begin_nolock(mod, email=email, kind=kind).allowed, i + 1
    for gap in (1, 2, 4, 8):
        clock.advance(gap)
        assert _begin_nolock(mod, email=email, kind=kind).allowed, gap
    clock.advance(8)
    assert _begin_nolock(mod, email=email, kind=kind).allowed, "the 10th attempt"


def test_without_lockout_the_would_be_lock_point_is_logged(lim, monkeypatch):
    import re
    mod, clock = lim
    lines = _capture_limiter_logs(mod, monkeypatch)
    for i in range(5):
        assert _begin_nolock(mod).allowed
    for gap in (1, 2, 4, 8):
        clock.advance(gap)
        assert _begin_nolock(mod).allowed
    assert not _spaced(lines), ("logged before the would-be lock point", lines)
    clock.advance(8)
    assert _begin_nolock(mod).allowed
    hits = _spaced(lines)
    assert len(hits) == 1, lines
    m = re.search(_SPACED_RE, hits[0])
    assert m.group(1) == "login", hits[0]
    assert "warning" in hits[0], hits[0]
    assert EMAIL not in hits[0] and "limited" not in hits[0], hits[0]
    assert not [ln for ln in lines if "AUTH_LOCKOUT" in ln], lines


def test_the_spaced_line_carries_the_kind(lim, monkeypatch):
    import re
    mod, clock = lim
    lines = _capture_limiter_logs(mod, monkeypatch)
    _to_the_would_be_lock_point(mod, clock, kind="reset")
    hits = _spaced(lines)
    assert len(hits) == 1, lines
    assert re.search(_SPACED_RE, hits[0]).group(1) == "reset", hits[0]


def test_the_spaced_line_is_logged_once_per_window(lim, monkeypatch):
    mod, clock = lim
    lines = _capture_limiter_logs(mod, monkeypatch)
    _to_the_would_be_lock_point(mod, clock)
    assert len(_spaced(lines)) == 1, lines
    for n in range(20):                       # 160 s of further spaced attempts
        clock.advance(8)
        assert _begin_nolock(mod).allowed, n
    assert len(_spaced(lines)) == 1, ("logged again inside the window", lines)
    assert not [ln for ln in lines if "AUTH_LOCKOUT" in ln], lines


def test_the_spaced_line_is_logged_again_after_the_window(lim, monkeypatch):
    mod, clock = lim
    lines = _capture_limiter_logs(mod, monkeypatch)
    _to_the_would_be_lock_point(mod, clock)
    assert len(_spaced(lines)) == 1, lines
    clock.advance(int(settings.AUTH_FAIL_WINDOW_S) + 1)
    _to_the_would_be_lock_point(mod, clock)
    assert len(_spaced(lines)) == 2, lines


def test_the_spaced_line_is_per_key(lim, monkeypatch):
    mod, clock = lim
    lines = _capture_limiter_logs(mod, monkeypatch)
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 1000)
    _to_the_would_be_lock_point(mod, clock, email="first@x.com")
    _to_the_would_be_lock_point(mod, clock, email="second@x.com")
    hits = _spaced(lines)
    assert len(hits) == 2, lines
    import re
    assert re.search(_SPACED_RE, hits[0]).group(2) != re.search(_SPACED_RE, hits[1]).group(2)


def test_reset_forgets_the_spaced_latch(lim, monkeypatch):
    mod, clock = lim
    lines = _capture_limiter_logs(mod, monkeypatch)
    _to_the_would_be_lock_point(mod, clock)
    assert len(_spaced(lines)) == 1, lines
    mod.reset()
    _to_the_would_be_lock_point(mod, clock)
    assert len(_spaced(lines)) == 2, lines


def test_a_locking_key_logs_no_spaced_line(lim, monkeypatch):
    mod, clock = lim
    lines = _capture_limiter_logs(mod, monkeypatch)
    for i in range(5):
        assert mod.begin("login", EMAIL, IP).allowed
    for gap in (1, 2, 4, 8):
        clock.advance(gap)
        assert mod.begin("login", EMAIL, IP).allowed
    clock.advance(8)
    mod.begin("login", EMAIL, IP)
    assert not _spaced(lines), lines
    assert len([ln for ln in lines if "AUTH_LOCKOUT" in ln]) == 1, lines
