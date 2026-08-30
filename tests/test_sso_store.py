"""sso_store: encrypt/decrypt/mask round-trip, the enable-gate hash,
redirect_uri derivation, disabled-by-default semantics, and
AuthStore.mark_sso_login's merge-only guarantee."""
import json

import pytest
from cryptography.fernet import Fernet

import db_sources
import local_store
import sso_store
from settings import settings

ADMIN = "ladmin"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY_OLD", "")
    yield


def _cfg(**over):
    base = {"tenant_id": "11111111-2222-3333-4444-555555555555",
            "client_id": "app-client-id", "client_secret": "s3cret-sso",
            "public_base_url": "", "auto_redirect": False}
    base.update(over)
    return base


class _Req:
    """Minimal stand-in for a Starlette Request in redirect_uri tests."""
    def __init__(self, base="http://testserver/"):
        self.base_url = base


def test_secret_roundtrip_and_mask(tmp_path):
    sso_store.save(_cfg(), ADMIN)
    raw = (tmp_path / "sso_config.json").read_text(encoding="utf-8")
    assert "s3cret-sso" not in raw
    assert "client_secret_enc" in raw
    assert json.loads(raw)["updated_by"] == ADMIN

    loaded = sso_store.load()
    assert loaded["client_secret"] == "s3cret-sso"
    assert "client_secret_enc" not in loaded

    masked = sso_store.load_masked()
    assert masked["client_secret_set"] is True
    assert masked["client_secret_readable"] is True
    assert masked["client_secret_masked"] == "••••••••"
    assert "client_secret" not in masked
    assert "client_secret_enc" not in masked
    assert "last_test_hash" not in masked   # secret-derived — stays inside
    assert "s3cret-sso" not in json.dumps(masked)


def test_save_keeps_secret_when_empty():
    sso_store.save(_cfg(), ADMIN)
    sso_store.save(_cfg(client_secret="", client_id="changed-id"), ADMIN)
    loaded = sso_store.load()
    assert loaded["client_id"] == "changed-id"
    assert loaded["client_secret"] == "s3cret-sso"


def test_save_never_touches_enabled():
    sso_store.save(_cfg(), ADMIN)
    sso_store.record_test_ok(ADMIN)
    sso_store.set_enabled(True, ADMIN)
    sso_store.save(_cfg(auto_redirect=True), ADMIN)
    assert sso_store.load()["enabled"] is True


def test_config_hash_changes_with_each_field():
    sso_store.save(_cfg(), ADMIN)
    h0 = sso_store.config_hash()
    assert h0
    sso_store.save(_cfg(client_id="other-client"), ADMIN)
    h1 = sso_store.config_hash()
    sso_store.save(_cfg(client_id="other-client", client_secret="another"), ADMIN)
    h2 = sso_store.config_hash()
    sso_store.save(_cfg(tenant_id="contoso.onmicrosoft.com",
                        client_id="other-client", client_secret="another"), ADMIN)
    h3 = sso_store.config_hash()
    assert len({h0, h1, h2, h3}) == 4


def test_record_test_ok_then_changed_save_invalidates():
    sso_store.save(_cfg(), ADMIN)
    assert sso_store.test_is_current() is False
    sso_store.record_test_ok(ADMIN)
    assert sso_store.test_is_current() is True
    assert sso_store.load()["last_test_ok_at"]
    # Changing any credential field invalidates the recorded test.
    sso_store.save(_cfg(client_id="rotated-app"), ADMIN)
    assert sso_store.test_is_current() is False


def test_load_absent_and_corrupt(tmp_path):
    assert sso_store.load() is None
    assert sso_store.is_enabled() is False
    assert sso_store.auto_redirect() is False
    masked = sso_store.load_masked()
    assert masked["enabled"] is False and masked["client_secret_set"] is False
    (tmp_path / "sso_config.json").write_text("{not json", encoding="utf-8")
    assert sso_store.load() is None
    assert sso_store.is_enabled() is False


def test_is_enabled_requires_complete_triple():
    sso_store.save(_cfg(), ADMIN)
    assert sso_store.is_enabled() is False          # not enabled yet
    sso_store.record_test_ok(ADMIN)
    sso_store.set_enabled(True, ADMIN)
    assert sso_store.is_enabled() is True
    sso_store.set_enabled(False, ADMIN)
    assert sso_store.is_enabled() is False


def test_encryption_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", "")
    with pytest.raises(db_sources.EncryptionUnavailable):
        sso_store.save(_cfg(), ADMIN)
    assert not (tmp_path / "sso_config.json").exists()   # nothing persisted


def test_rotated_key_marks_secret_unreadable(monkeypatch):
    sso_store.save(_cfg(), ADMIN)
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    masked = sso_store.load_masked()
    assert masked["client_secret_set"] is True
    assert masked["client_secret_readable"] is False
    assert sso_store.load()["client_secret"] is None
    assert sso_store.config_hash() is None


def test_redirect_uri_with_and_without_public_base_url():
    assert (sso_store.redirect_uri(_Req("http://testserver/"))
            == "http://testserver/auth/microsoft/callback")
    sso_store.save(_cfg(public_base_url="https://pdc.bank.local/"), ADMIN)
    assert (sso_store.redirect_uri(_Req("http://internal:8091/"))
            == "https://pdc.bank.local/auth/microsoft/callback")


def test_mark_sso_login_preserves_password():
    store = local_store.AuthStore()
    store.ensure_user("user@x.com")
    store.set_password("user@x.com", "pw12345")
    before = store.get_auth("user@x.com")["password_hash"]
    store.mark_sso_login("user@x.com", "microsoft")
    auth = store.get_auth("user@x.com")
    assert auth["sso_provider"] == "microsoft"
    assert auth["sso_last_login"]
    assert auth["password_hash"] == before
