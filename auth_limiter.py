"""auth_limiter.py — in-memory sign-in / reset attempt limiter.

Four call sites in routes/auth.py (sign-in, reset request, reset link GET and
POST) share one set of counters:

* key spaces `login:<email>` and `reset:<email>` (the address keys, one
  bucket per kind so two reset clicks never count against sign-in) and
  `ip:<peer>` (shared by every kind; the reset-link routes, kind `token`,
  count on this key only);
* each key holds the timestamps of its failures inside `AUTH_FAIL_WINDOW_S`
  plus a NOT-BEFORE stamp; an address key can also carry a lockout.

`begin(kind, email, ip, *, lockout=True)` checks the keys and, when the attempt is allowed,
records it as a failure UP FRONT — a concurrent burst cannot all pass the
check before the first failure lands. `success(kind, email, ip)` retracts it:
the address key is cleared and one failure comes off the IP key. An attempt
refused because it came before its not-before stamp, or during a lockout, is
NOT recorded — the caller answers 429 without evaluating anything.

Address schedule: `AUTH_FAIL_THRESHOLD` failures are free; the next four
attempts are allowed no sooner than 1, 2, 4 and 8 s after the previous one;
the failure after those locks the address key for `AUTH_LOCKOUT_S`. A call
with `lockout=False` (the sign-in of the configured local admin account, so
an anonymous caller cannot lock the operator out) keeps that spacing and,
where the key would lock, only sets another 8 s not-before: never a lockout,
never an `AUTH_LOCKOUT` line. The IP
key spaces attempts beyond `AUTH_FAIL_THRESHOLD_IP` the same way (capped at
8 s) and NEVER locks: behind a proxy that does not forward the client address
everyone shares one peer, and a lockout there would lock out the company.
The worse of the two verdicts applies. A not-before stamp is answered with a
`Retry-After`, never a server-side sleep, so a concurrent burst is refused
rather than queued.

Each space holds at most `MAX_KEYS` keys, so spraying addresses or peers
cannot grow memory. The key evicted is the least recently touched one that
holds no live wait (its lockout and not-before both past); only when every
other key is live does plain least-recently-touched apply, so a spray of
fresh addresses cannot erase a lockout. Thresholds are read from
`settings` at CALL time; `clock` is a module attribute the tests replace.
The counters live in this process: correct because the web server runs ONE
worker. Leaf module: stdlib + settings + logger_utils.
"""
from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import OrderedDict, deque
from typing import NamedTuple

from logger_utils import log_with_sid
from settings import settings

MAX_KEYS = 10_000
KINDS = ("login", "reset", "token")
_SCHEDULE_STEPS = 4          # 1, 2, 4, 8 s

clock = time.monotonic

_LOCK = threading.Lock()
_SPACES: dict = {}


class Verdict(NamedTuple):
    allowed: bool
    retry_after_s: int


class _Key:
    __slots__ = ("failures", "not_before", "locked_until")

    def __init__(self):
        self.failures = deque()
        self.not_before = 0.0
        self.locked_until = 0.0


def _space(name: str) -> OrderedDict:
    space = _SPACES.get(name)
    if space is None:
        space = _SPACES[name] = OrderedDict()
    return space


def _lookup(space_name: str, key: str):
    """The existing key, touched (most recent), or None."""
    space = _space(space_name)
    entry = space.get(key)
    if entry is not None:
        space.move_to_end(key)
    return entry


def _evict_one(space: OrderedDict, keep: str, now: float) -> None:
    """Drop the least recently touched key without a live wait; the least
    recently touched key of all when every one is live. Never `keep`."""
    fallback = None
    for name, entry in space.items():
        if name == keep:
            continue
        if entry.locked_until <= now and entry.not_before <= now:
            del space[name]
            return
        if fallback is None:
            fallback = name
    if fallback is not None:
        del space[fallback]


def _create(space_name: str, key: str, now: float) -> _Key:
    space = _space(space_name)
    entry = space.get(key)
    if entry is None:
        entry = space[key] = _Key()
        limit = max(1, int(MAX_KEYS))
        while len(space) > limit:
            before = len(space)
            _evict_one(space, key, now)
            if len(space) == before:
                break
    else:
        space.move_to_end(key)
    return entry


def _prune(entry: _Key, now: float, window: float) -> None:
    while entry.failures and entry.failures[0] <= now - window:
        entry.failures.popleft()


def _wait(entry, now: float, window: float) -> float:
    """Seconds this key must still wait (0 when it may proceed)."""
    if entry is None:
        return 0.0
    _prune(entry, now, window)
    return max(entry.locked_until - now, entry.not_before - now, 0.0)


def _keys(kind: str, email, ip):
    addr = (email or "").strip().lower()
    peer = (ip or "").strip()
    return ((kind, addr) if addr else None), (("ip", peer) if peer else None)


def _hash_prefix(space_name: str, key: str) -> str:
    return hashlib.sha256(f"{space_name}:{key}".encode("utf-8")).hexdigest()[:12]


def begin(kind: str, email, ip, *, lockout: bool = True) -> Verdict:
    """Check the attempt and record it as a failure when it is allowed.
    `lockout=False` replaces the address lockout with an 8 s spacing step;
    the thresholds and the IP schedule are the same either way."""
    if kind not in KINDS:
        raise ValueError("unknown attempt kind")
    now = float(clock())
    window = float(settings.AUTH_FAIL_WINDOW_S)
    addr_key, ip_key = _keys(kind, email, ip)
    locked = None
    with _LOCK:
        waits = [_wait(_lookup(*k), now, window) for k in (addr_key, ip_key) if k]
        wait = max(waits or [0.0])
        if wait > 0:
            return Verdict(False, max(1, math.ceil(wait)))
        if addr_key:
            entry = _create(*addr_key, now)
            entry.failures.append(now)
            extra = len(entry.failures) - max(1, int(settings.AUTH_FAIL_THRESHOLD))
            if extra >= _SCHEDULE_STEPS and not lockout:
                entry.not_before = now + 2 ** (_SCHEDULE_STEPS - 1)
            elif extra >= _SCHEDULE_STEPS:
                entry.locked_until = now + max(1, int(settings.AUTH_LOCKOUT_S))
                locked = _hash_prefix(*addr_key)
            elif extra >= 0:
                entry.not_before = now + 2 ** extra
        if ip_key:
            entry = _create(*ip_key, now)
            entry.failures.append(now)
            extra = len(entry.failures) - max(1, int(settings.AUTH_FAIL_THRESHOLD_IP))
            if extra >= 0:
                entry.not_before = now + 2 ** min(extra, _SCHEDULE_STEPS - 1)
    if locked:
        log_with_sid("auth_limiter", "warning", f"AUTH_LOCKOUT kind={kind} h={locked}")
    return Verdict(True, 0)


def success(kind: str, email, ip) -> None:
    """Retract the attempt `begin` recorded: clear the address key and take
    one failure off the IP key (its spacing lifts when that brings it back
    under the threshold)."""
    addr_key, ip_key = _keys(kind, email, ip)
    with _LOCK:
        if addr_key:
            _space(addr_key[0]).pop(addr_key[1], None)
        if ip_key:
            entry = _lookup(*ip_key)
            if entry is not None and entry.failures:
                entry.failures.pop()
                if len(entry.failures) < max(1, int(settings.AUTH_FAIL_THRESHOLD_IP)):
                    entry.not_before = 0.0


def reset() -> None:
    """Forget every counter (tests)."""
    with _LOCK:
        _SPACES.clear()
