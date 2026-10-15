"""Approved sign-in domains store.

One JSON document at DATA_ROOT/signin_domains.json —
`{"domains": [...], "updated_at", "updated_by"}` — managed by the ladmin from
the admin panel's Users section (`/api/admin/signin_domains`). An address
whose domain (the text after the LAST "@", exact match, no subdomains) is on
the list may create its own account at its first sign-in: the local form
(routes/auth.py) or Microsoft SSO (routes/sso.py).

A missing file means an EMPTY list — every install from before this feature
stays invitation-only, nothing is migrated. A junk file reads as empty too;
reads never raise and never create the file.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from local_store import _data_root, _now, _write_json_atomic
from logger_utils import log_safe_value, log_with_sid

_LOCK = threading.RLock()

_DOC_NAME = "signin_domains.json"

# A bare host name with at least one dot: no "@", scheme, path, space or
# empty label can pass.
_DOMAIN_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")

_ENTRY_SHOWN_CHARS = 80


def _path() -> Path:
    return _data_root() / _DOC_NAME


def normalize_domains(values: Any) -> list[str]:
    """A list of str → stripped, lower-cased, de-duplicated (first order
    kept). Raises ValueError on a non-list or on any invalid entry."""
    if not isinstance(values, list):
        raise ValueError("Send a list of domains.")
    out: list[str] = []
    for entry in values:
        clean = entry.strip().lower() if isinstance(entry, str) else None
        if clean is None or not _DOMAIN_RE.fullmatch(clean):
            shown = entry.strip() if isinstance(entry, str) else str(entry)
            raise ValueError(f"Invalid domain: {shown[:_ENTRY_SHOWN_CHARS] or '(empty)'}")
        if clean not in out:
            out.append(clean)
    return out


def load() -> list[str]:
    """The approved domains. Absent file → []; an unreadable or malformed
    file → [] (logged); entries are re-filtered so a hand-edited file cannot
    inject junk. Never raises."""
    try:
        p = _path()
        if not p.exists():
            return []
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("domains"), list):
            return []
        out: list[str] = []
        for entry in raw["domains"]:
            if not isinstance(entry, str):
                continue
            clean = entry.strip().lower()
            if _DOMAIN_RE.fullmatch(clean) and clean not in out:
                out.append(clean)
        return out
    except Exception as e:
        log_with_sid("signin_domains", "warning",
                     f"SIGNIN_DOMAINS_READ_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return []


def save(domains: Any, updated_by: str) -> list[str]:
    """Replace the list (an empty list clears it). Validation runs first, so
    a ValueError writes nothing. Returns the stored list."""
    clean = normalize_domains(domains)
    with _LOCK:
        _write_json_atomic(_path(), {"domains": clean, "updated_at": _now(),
                                     "updated_by": updated_by})
    return clean


def is_configured() -> bool:
    return bool(load())


def allows_email(email: Any) -> bool:
    """True when the address's domain (after the LAST "@") is approved.
    Never raises."""
    try:
        if not isinstance(email, str) or "@" not in email:
            return False
        domain = email.rsplit("@", 1)[1].strip().lower()
        if not domain:
            return False
        return domain in load()
    except Exception as e:
        log_with_sid("signin_domains", "warning",
                     f"SIGNIN_DOMAINS_CHECK_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return False
