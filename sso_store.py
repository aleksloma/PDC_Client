"""Microsoft Entra ID SSO configuration store.

One JSON document at DATA_ROOT/sso_config.json, managed entirely from the
ladmin "Single sign-on" panel (no env vars, no settings.py fields, no
restart needed — routes/sso.py re-reads it per request). The client secret
is Fernet-encrypted at rest with the SAME key the DB-source credentials use
(db_sources.encrypt_password / decrypt_password, CLIENT_ENCRYPTION_KEY).
File absent ⇒ SSO disabled and every /auth/microsoft* route 404s.

The enable gate: enabling SSO requires a successful "Test connection" for
the CURRENTLY saved (tenant_id, client_id, client_secret) triple —
record_test_ok() stores the triple's hash, test_is_current() compares it.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Optional

import db_sources
from local_store import _data_root, _now, _write_json_atomic
from logger_utils import log_with_sid

_LOCK = threading.RLock()

_DOC_NAME = "sso_config.json"

_DEFAULTS = {
    "provider": "microsoft",
    "enabled": False,
    "tenant_id": "",
    "client_id": "",
    "client_secret_enc": "",
    "public_base_url": "",
    "auto_redirect": False,
    "last_test_ok_at": None,
    "last_test_hash": None,
    "updated_at": None,
    "updated_by": None,
}


def _path() -> Path:
    return _data_root() / _DOC_NAME


def _read_doc() -> Optional[dict]:
    """The raw stored doc (secret still encrypted), or None when the file is
    absent or unreadable. Unknown keys are dropped; missing keys default."""
    p = _path()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return {k: raw.get(k, v) for k, v in _DEFAULTS.items()}
    except Exception as e:
        log_with_sid("sso", "warning", f"SSO_CONFIG_READ_FAILED: {e}")
        return None


def load() -> Optional[dict]:
    """The config for INTERNAL use: `client_secret` decrypted (None when the
    key is missing/rotated/corrupt), `client_secret_enc` removed. None when
    no config file exists. Never raises."""
    doc = _read_doc()
    if doc is None:
        return None
    out = dict(doc)
    enc = out.pop("client_secret_enc", "")
    out["client_secret"] = db_sources.decrypt_password(enc) if enc else None
    return out


def load_masked() -> dict:
    """The ONLY shape in which the config may leave via an API — the
    db_sources._mask_connection triple: *_set / *_readable / *_masked,
    no secret material (last_test_hash is secret-DERIVED, so it stays
    inside too). Absent file ⇒ the defaults (disabled, empty)."""
    doc = _read_doc() or dict(_DEFAULTS)
    out = {k: v for k, v in doc.items()
           if k not in ("client_secret_enc", "last_test_hash")}
    enc = doc.get("client_secret_enc") or ""
    out["client_secret_set"] = bool(enc)
    out["client_secret_readable"] = bool(enc) and db_sources.decrypt_password(enc) is not None
    out["client_secret_masked"] = "••••••••" if enc else ""
    return out


def save(cfg: dict, updated_by: str) -> dict:
    """Merge the editable fields into the stored doc. A non-empty
    `client_secret` re-encrypts (EncryptionUnavailable propagates — the
    route surfaces 503, NEVER a plaintext fallback); empty keeps the stored
    one. Deliberately never touches `enabled` / `last_test_*` — the hash
    gate makes a stale test record harmless. Returns load_masked()."""
    with _LOCK:
        doc = _read_doc() or dict(_DEFAULTS)
        doc["provider"] = "microsoft"
        doc["tenant_id"] = str(cfg.get("tenant_id") or "").strip()
        doc["client_id"] = str(cfg.get("client_id") or "").strip()
        doc["public_base_url"] = str(cfg.get("public_base_url") or "").strip().rstrip("/")
        doc["auto_redirect"] = bool(cfg.get("auto_redirect"))
        secret = cfg.get("client_secret")
        if secret:
            doc["client_secret_enc"] = db_sources.encrypt_password(str(secret))
        doc["updated_at"] = _now()
        doc["updated_by"] = updated_by
        _write_json_atomic(_path(), doc)
    return load_masked()


def hash_for(tenant_id: str, client_id: str, secret: str) -> Optional[str]:
    """sha256 over one (tenant_id, client_id, secret) triple — the
    enable-gate identity of a configuration."""
    tenant = (tenant_id or "").strip().lower()
    client = (client_id or "").strip()
    if not (tenant and client and secret):
        return None
    joined = "\n".join([tenant, client, secret])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def config_hash() -> Optional[str]:
    """The enable-gate hash of the CURRENTLY saved config. None when any
    part is missing or the secret cannot be decrypted."""
    cfg = load()
    if not cfg:
        return None
    return hash_for(cfg.get("tenant_id"), cfg.get("client_id"),
                    cfg.get("client_secret"))


def record_test_ok(updated_by: str, tested_hash: Optional[str] = None) -> None:
    """Stamp a successful connection test. Pass `tested_hash` (hash_for of
    the values that were ACTUALLY tested) so a save racing the network-long
    test window can never mark untested values as tested; without it the
    currently saved values are stamped."""
    with _LOCK:
        doc = _read_doc()
        if doc is None:
            return
        doc["last_test_ok_at"] = _now()
        doc["last_test_hash"] = tested_hash or config_hash()
        doc["updated_by"] = updated_by
        _write_json_atomic(_path(), doc)


def test_is_current() -> bool:
    """Whether the last successful test matches the currently saved values."""
    try:
        doc = _read_doc()
        if not doc or not doc.get("last_test_hash"):
            return False
        return doc["last_test_hash"] == config_hash()
    except Exception as e:
        log_with_sid("sso", "warning", f"SSO_TEST_CURRENT_FAILED: {e}")
        return False


def set_enabled(enabled: bool, updated_by: str) -> dict:
    """Flip the enabled flag (the test-gate CHECK lives in the route so it
    can answer 409 with a message). Absent-file disable is a no-op."""
    with _LOCK:
        doc = _read_doc()
        if doc is None:
            return load_masked()   # nothing saved yet — stays disabled
        doc["enabled"] = bool(enabled)
        doc["updated_at"] = _now()
        doc["updated_by"] = updated_by
        _write_json_atomic(_path(), doc)
    return load_masked()


def is_enabled() -> bool:
    """SSO is usable: config exists, enabled, and the triple is present.
    Never raises (Article IV) — any failure reads as disabled."""
    try:
        doc = _read_doc()
        return bool(doc and doc.get("enabled") and doc.get("tenant_id")
                    and doc.get("client_id") and doc.get("client_secret_enc"))
    except Exception as e:
        log_with_sid("sso", "warning", f"SSO_ENABLED_CHECK_FAILED: {e}")
        return False


def auto_redirect() -> bool:
    """Whether "/" should send unauthenticated visitors straight to
    Microsoft (the ?local=1 escape hatch is handled by the route)."""
    try:
        doc = _read_doc()
        return is_enabled() and bool(doc and doc.get("auto_redirect"))
    except Exception:
        return False


def redirect_uri(request) -> str:
    """The OIDC redirect URI: <public_base_url or the request's base>
    /auth/microsoft/callback. public_base_url is the reverse-proxy escape
    hatch — behind a proxy request.base_url may be the internal host."""
    base = ""
    try:
        doc = _read_doc()
        if doc:
            base = (doc.get("public_base_url") or "").strip()
    except Exception:
        base = ""
    if not base:
        base = str(request.base_url)
    return base.rstrip("/") + "/auth/microsoft/callback"
