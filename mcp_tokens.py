"""MCP access tokens: the token store and the caller identity.

One JSON document at DATA_ROOT/mcp_tokens.json —
`{"version": 1, "tokens": [...]}` — managed by the local admin. A missing file
means no token exists, so the MCP feature is off; reads never create it. Only
sha256(raw token) is stored; the raw token is returned once, by
`create_token`. Revoked tokens stay in the file.

Reads fail closed: an unreadable or wrong-shape file verifies no token. Writes
refuse such a file (`TokenStoreError`), so the stored tokens are never
replaced by an empty document.

`resolve_caller` decides which account a verified token acts as:
* per_user — the one X-PDC-User-Email value the request carried; an unknown
  address is created only inside an approved sign-in domain
  (signin_domains.py);
* shared_account — the account bound at creation; the header is ignored.

No log line carries a token, a hash or an unescaped request value.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional

import db_sources
import signin_domains
from local_store import AuthStore, _data_root, _write_json_atomic
from logger_utils import log_safe_value, log_with_sid

_LOCK = threading.RLock()

_DOC_NAME = "mcp_tokens.json"

# The clock of every stamp; a module attribute so the tests can replace it.
_clock = time.time

PER_USER = "per_user"
SHARED_ACCOUNT = "shared_account"
MODES = (PER_USER, SHARED_ACCOUNT)

NAME_MAX_CHARS = 60
TOUCH_INTERVAL_S = 60

_TOKEN_PREFIX = "pdcmcp_"
# Exactly what create_token produces (prefix + secrets.token_urlsafe(32)).
_TOKEN_RE = re.compile(r"pdcmcp_[A-Za-z0-9_-]{43}")
_ID_RE = re.compile(r"[0-9a-f]{16}")

# Control, invisible format, lone surrogate, line and paragraph separator:
# none may be part of a name (two names could look the same, or break a line).
_NAME_BAD_CATEGORIES = ("Cc", "Cf", "Cs", "Zl", "Zp")

MISSING_HEADER = "missing_header"
MALFORMED_HEADER = "malformed_header"
NOT_ALLOWED = "not_allowed"


class TokenStoreError(Exception):
    """The token file could not be read or written. Deliberately not a
    ValueError: a ValueError carries a sentence about the admin's input."""


class Caller(NamedTuple):
    """Exactly one of the two is set."""
    email: Optional[str]
    refusal: Optional[str]


# ---------------------------------------------------------------------------
# the file
# ---------------------------------------------------------------------------
def _path() -> Path:
    return _data_root() / _DOC_NAME


def _iso_now() -> str:
    return datetime.fromtimestamp(_clock(), timezone.utc).isoformat()


def _read_doc() -> Optional[dict]:
    """The stored document, None when there is no file. Anything unreadable or
    of another shape is logged and raises TokenStoreError. Called under
    `_LOCK`."""
    try:
        try:
            raw = _path().read_bytes()
        except FileNotFoundError:
            return None
        doc = json.loads(raw.decode("utf-8"))
    except Exception as e:
        # The type only: a decoder quotes the content it choked on.
        log_with_sid("mcp", "error",
                     f"MCP_TOKENS_READ_FAILED error={log_safe_value(type(e).__name__, 80)}")
        raise TokenStoreError("The token file could not be read.") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("tokens"), list):
        log_with_sid("mcp", "error", "MCP_TOKENS_READ_FAILED error=wrong_shape")
        raise TokenStoreError("The token file could not be read.")
    return doc


def _write_doc(doc: dict) -> None:
    """Atomic replace; a failure is logged (type only) and raises
    TokenStoreError with the previous file in place. Called under `_LOCK`."""
    try:
        _write_json_atomic(_path(), doc)
    except Exception as e:
        log_with_sid("mcp", "error",
                     f"MCP_TOKENS_WRITE_FAILED error={log_safe_value(type(e).__name__, 80)}")
        raise TokenStoreError("The token file could not be written.") from None


def _records(doc: Optional[dict]) -> list[dict]:
    """The usable records. One without a valid id is never usable: it could
    not be revoked."""
    return [r for r in (doc or {}).get("tokens", [])
            if isinstance(r, dict) and _valid_id(r.get("id"))]


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _find(doc: Optional[dict], token_id: str) -> Optional[dict]:
    for rec in _records(doc):
        if rec["id"] == token_id:
            return rec
    return None


def _is_active(rec: dict) -> bool:
    # Anything but "no stamp" reads as revoked.
    return rec.get("revoked_at") is None


def _public(rec: dict) -> dict:
    """The only shape a token leaves this module in through the API."""
    def text(key):
        value = rec.get(key)
        return value if isinstance(value, str) else None
    return {"id": rec["id"], "name": text("name") or "", "mode": text("mode") or "",
            "account": text("account"), "created_at": text("created_at") or "",
            "created_by": text("created_by") or "",
            "last_used_at": text("last_used_at"), "revoked_at": text("revoked_at"),
            "revoked_by": text("revoked_by"), "active": _is_active(rec)}


# ---------------------------------------------------------------------------
# admin: create / list / revoke
# ---------------------------------------------------------------------------
def _clean_name(name: Any) -> str:
    clean = name.strip() if isinstance(name, str) else ""
    if not clean:
        raise ValueError("Enter a name for the token.")
    if len(clean) > NAME_MAX_CHARS:
        raise ValueError(f"The name can be at most {NAME_MAX_CHARS} characters long.")
    if any(unicodedata.category(ch) in _NAME_BAD_CATEGORIES for ch in clean):
        raise ValueError("The name must not contain control characters.")
    return clean


def _clean_email(value: Any, strip_chars: Optional[str] = None) -> Optional[str]:
    """The lower-cased address, or None when it is not one. Matched BEFORE
    lower-casing: a non-ASCII letter can lower-case into an ASCII one."""
    from routes.auth import _EMAIL_RE     # lazy: routes.auth must not be a load-time need
    if not isinstance(value, str):
        return None
    clean = value.strip(strip_chars)
    return clean.lower() if _EMAIL_RE.fullmatch(clean) else None


def _account_created_at(store: AuthStore, email: str) -> Optional[str]:
    profile = store.get_profile(email)
    stamp = profile.get("created_at") if isinstance(profile, dict) else None
    return stamp if isinstance(stamp, str) and stamp else None


def _bind_account(account: Any) -> tuple[str, str]:
    """(address, created_at) of the account a shared token acts as."""
    email = _clean_email(account)
    if email is None:
        raise ValueError("Enter the email address of an existing account.")
    store = AuthStore()
    if store.is_bootstrap_admin(email):
        raise ValueError("The local admin account cannot be used for a token.")
    if not store.user_exists(email):
        raise ValueError("There is no account with this email address.")
    stamp = _account_created_at(store, email)
    if stamp is None:
        # Without it a removed and re-created address would look the same.
        raise ValueError("This account has no creation date, so a token cannot be bound to it.")
    return email, stamp


def create_token(name: Any, mode: Any, created_by: str,
                 account: Any = None) -> tuple[dict, str]:
    """(public row, raw token). The raw token exists only in this return
    value. Bad input raises ValueError with a plain sentence, an unreadable
    or unwritable file TokenStoreError; nothing is written in either case."""
    clean = _clean_name(name)
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("Choose one of the two token modes.")
    bound = _bind_account(account) if mode == SHARED_ACCOUNT else None
    raw = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    with _LOCK:
        doc = _read_doc()
        if doc is None:
            doc = {"version": 1, "tokens": []}
        for rec in _records(doc):
            taken = rec.get("name")
            if _is_active(rec) and isinstance(taken, str) \
                    and taken.casefold() == clean.casefold():
                raise ValueError("An active token with this name already exists.")
        rec = {"id": secrets.token_hex(8), "name": clean, "mode": mode}
        if bound is not None:
            rec["account"], rec["account_created_at"] = bound
        rec.update({"token_hash": hashlib.sha256(raw.encode("ascii")).hexdigest(),
                    "created_at": _iso_now(), "created_by": created_by,
                    "last_used_at": None, "revoked_at": None, "revoked_by": None})
        doc["tokens"].append(rec)
        _write_doc(doc)
    log_with_sid(log_safe_value(created_by, 254) or "mcp", "info",
                 f"MCP_TOKEN_CREATED id={log_safe_value(rec['id'], 32)} "
                 f"mode={log_safe_value(mode, 20)} name={log_safe_value(clean, 120)}")
    return _public(rec), raw


def list_tokens() -> list[dict]:
    """Public rows, newest first. An unreadable file answers [] (logged).
    Never raises."""
    try:
        with _LOCK:
            rows = [_public(rec) for rec in _records(_read_doc())]
        rows.reverse()          # the later record first when two stamps are equal
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)
    except TokenStoreError:
        return []               # logged by _read_doc
    except Exception as e:
        log_with_sid("mcp", "error",
                     f"MCP_TOKENS_LIST_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return []


def revoke_token(token_id: Any, revoked_by: str) -> Optional[dict]:
    """The public row of the revoked token; None for an unknown id. A second
    revoke keeps the first stamps. Raises TokenStoreError when the file
    cannot be read or written."""
    if not _valid_id(token_id):
        return None
    with _LOCK:
        doc = _read_doc()
        rec = _find(doc, token_id)
        if rec is None:
            return None
        if not _is_active(rec):
            return _public(rec)
        rec["revoked_at"] = _iso_now()
        rec["revoked_by"] = revoked_by
        _write_doc(doc)
    log_with_sid(log_safe_value(revoked_by, 254) or "mcp", "info",
                 f"MCP_TOKEN_REVOKED id={log_safe_value(token_id, 32)}")
    return _public(rec)


# ---------------------------------------------------------------------------
# requests: verify / touch
# ---------------------------------------------------------------------------
def verify(raw: Any) -> Optional[dict]:
    """The stored record of an ACTIVE token, else None. Every record is
    compared, in constant time. Never raises."""
    try:
        if not isinstance(raw, str) or not _TOKEN_RE.fullmatch(raw):
            return None
        wanted = hashlib.sha256(raw.encode("ascii")).hexdigest().encode("ascii")
        with _LOCK:
            records = _records(_read_doc())
        found = None
        for rec in records:
            stored = rec.get("token_hash")
            stored = stored.encode("utf-8", "replace") if isinstance(stored, str) else b""
            if hmac.compare_digest(stored, wanted) and _is_active(rec):
                found = rec
        return found
    except TokenStoreError:
        return None             # logged by _read_doc
    except Exception as e:
        log_with_sid("mcp", "error",
                     f"MCP_TOKEN_VERIFY_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return None


def _stamp_epoch(value: Any) -> Optional[float]:
    """Epoch seconds of a stored stamp; None for anything that is not an ISO
    stamp with a timezone."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except Exception as e:
        log_with_sid("mcp", "warning",
                     f"MCP_TOKEN_STAMP_INVALID error={log_safe_value(type(e).__name__, 80)}")
        return None


def touch(token_id: Any) -> None:
    """Stamp `last_used_at`, at most once a minute per token. Never raises."""
    try:
        if not _valid_id(token_id):
            return
        with _LOCK:
            doc = _read_doc()
            rec = _find(doc, token_id)
            if rec is None:
                return
            last = _stamp_epoch(rec.get("last_used_at"))
            if last is not None and 0 <= _clock() - last < TOUCH_INTERVAL_S:
                return
            rec["last_used_at"] = _iso_now()
            _write_doc(doc)
    except TokenStoreError:
        return                  # logged by _read_doc / _write_doc
    except Exception as e:
        log_with_sid("mcp", "warning",
                     f"MCP_TOKEN_TOUCH_FAILED error={log_safe_value(type(e).__name__, 80)}")


# ---------------------------------------------------------------------------
# the caller's identity
# ---------------------------------------------------------------------------
def _refuse(refusal: str, cause: str, record: Any, peer: Any) -> Caller:
    """A refusal, logged with its cause. The header value is never logged."""
    name = record.get("name") if isinstance(record, dict) else None
    log_with_sid("mcp", "warning",
                 f"MCP_CALLER_REFUSED reason={log_safe_value(refusal, 40)} "
                 f"cause={log_safe_value(cause, 40)} token={log_safe_value(name, 120)} "
                 f"peer={log_safe_value(peer, 64)}")
    return Caller(None, refusal)


def _resolve_per_user(record: dict, header_values: Any, peer: Any) -> Caller:
    if header_values is None:
        header_values = []
    if not isinstance(header_values, (list, tuple)):
        return _refuse(MALFORMED_HEADER, "not_a_list", record, peer)
    if not header_values:
        return _refuse(MISSING_HEADER, "no_header", record, peer)
    if len(header_values) > 1:
        # Even when the copies agree: which one a proxy added is unknown.
        return _refuse(MALFORMED_HEADER, "repeated_header", record, peer)
    value = header_values[0]
    if isinstance(value, str) and not value.strip():
        return _refuse(MISSING_HEADER, "blank_header", record, peer)
    email = _clean_email(value, " \t")
    if email is None:
        return _refuse(MALFORMED_HEADER, "not_an_address", record, peer)

    store = AuthStore()
    if store.is_bootstrap_admin(email):
        return _refuse(NOT_ALLOWED, "bootstrap_admin", record, peer)
    if store.user_exists(email):
        return Caller(email, None)
    if not signin_domains.allows_email(email):
        return _refuse(NOT_ALLOWED, "unknown_address", record, peer)
    try:
        created = store.create_account(email)
    except Exception as e:
        log_with_sid(log_safe_value(email, 254), "error",
                     f"MCP_ACCOUNT_CREATE_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return _refuse(NOT_ALLOWED, "create_failed", record, peer)
    if created:
        # False = someone else created it meanwhile; nothing to record.
        name = log_safe_value(record.get("name"), 120)
        db_sources.audit(f"mcp:{name}", "mcp.account_created", target=email)
        log_with_sid(log_safe_value(email, 254), "info",
                     f"MCP_ACCOUNT_CREATED token={log_safe_value(name, 120)}")
    return Caller(email, None)


def _resolve_shared(record: dict, peer: Any) -> Caller:
    account = record.get("account")
    email = _clean_email(account)
    if email is None:
        return _refuse(NOT_ALLOWED, "no_bound_account", record, peer)
    store = AuthStore()
    if store.is_bootstrap_admin(email):
        return _refuse(NOT_ALLOWED, "bootstrap_admin", record, peer)
    if not store.user_exists(email):
        return _refuse(NOT_ALLOWED, "account_removed", record, peer)
    stamp = _account_created_at(store, email)
    if stamp is None or stamp != record.get("account_created_at"):
        # The same address after a removal is another account.
        return _refuse(NOT_ALLOWED, "account_changed", record, peer)
    return Caller(email, None)


def resolve_caller(record: Any, header_values: Any, peer: Any = None) -> Caller:
    """The account a verified token acts as. `header_values` is the list of
    X-PDC-User-Email values the request carried; `peer` only goes to the log.
    An existing account is never written to. Never raises: a failure is
    "not_allowed"."""
    try:
        mode = record.get("mode") if isinstance(record, dict) else None
        if mode == PER_USER:
            return _resolve_per_user(record, header_values, peer)
        if mode == SHARED_ACCOUNT:
            return _resolve_shared(record, peer)
        return _refuse(NOT_ALLOWED, "unknown_mode", record, peer)
    except Exception as e:
        log_with_sid("mcp", "error",
                     f"MCP_CALLER_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return Caller(None, NOT_ALLOWED)
