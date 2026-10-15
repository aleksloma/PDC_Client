"""Microsoft SSO and the approved sign-in domains (routes/sso.py callback).

An Entra identity matching no account used to be provisioned only with
SSO_AUTO_PROVISION. It is now also provisioned — and bound to its tid+oid —
when the address's domain is in DATA_ROOT/signin_domains.json, logged
`SSO_DOMAIN_ACCOUNT_PROVISIONED`. An unapproved domain without the setting
is refused as before (401, `SSO_UNKNOWN_ACCOUNT`, nothing created), and the
setting keeps its own `SSO_ACCOUNT_PROVISIONED` line.

The domain list is seeded by writing the store file directly, so these tests
fail on the callback's behaviour, not on the module's import. Offline: the
authlib client is faked (tests/test_sso_routes.py helpers).
"""
import json
import logging
from pathlib import Path

import pytest

import local_store
import logger_utils
from settings import settings
from tests.test_sso_routes import (  # noqa: F401  (pytest fixture import)
    TENANT, _enable, _FakeOAuthClient, _install_fake, _save, client,
)

OID_NEW = "cccccccc-0000-0000-0000-000000000003"
APPROVED = "approved.example"
NEWBIE = "newbie@approved.example"
STRANGER = "stranger@other.example"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass


@pytest.fixture
def logs():
    logger = logger_utils.get_logger()
    handler = _Capture()
    old_level = logger.level
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "SSO_ALLOW_GUESTS", False)
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", False)
    local_store._SSO_INDEX.clear()


def _seed_domains(domains):
    Path(settings.DATA_ROOT, "signin_domains.json").write_text(
        json.dumps({"domains": list(domains), "updated_at": "2026-10-15T00:00:00Z",
                    "updated_by": "ladmin"}),
        encoding="utf-8")


def _sign_in(client, monkeypatch, email, oid=OID_NEW):
    _save()
    _enable()
    userinfo = {"tid": TENANT, "oid": oid, "preferred_username": email}
    _install_fake(monkeypatch, _FakeOAuthClient(token={"userinfo": userinfo}))
    return client.get("/auth/microsoft/callback", follow_redirects=False)


def _who(client):
    return client.get("/_whoami").json()["email"]


def _logged(lines, event):
    return any(event in line for line in lines)


def test_an_approved_domain_provisions_and_binds_the_identity(client, monkeypatch, logs):
    _seed_domains([APPROVED])
    r = _sign_in(client, monkeypatch, NEWBIE)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert r.headers["location"] == "/lab"
    store = local_store.AuthStore()
    assert store.user_exists(NEWBIE)
    assert store.sso_identity(NEWBIE) == (TENANT, OID_NEW)
    assert _who(client) == NEWBIE
    assert _logged(logs, "SSO_DOMAIN_ACCOUNT_PROVISIONED"), logs[-10:]


def test_the_provisioned_account_is_a_standard_user_without_data_roles(client, monkeypatch):
    _seed_domains([APPROVED])
    assert _sign_in(client, monkeypatch, NEWBIE).status_code == 302
    store = local_store.AuthStore()
    assert store.get_role(NEWBIE) == "user"
    assert store.get_data_roles(NEWBIE) == []
    assert not store.has_password(NEWBIE)


def test_the_domain_match_is_case_insensitive(client, monkeypatch):
    _seed_domains([APPROVED])
    r = _sign_in(client, monkeypatch, "NewBie@Approved.EXAMPLE")
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert local_store.AuthStore().sso_identity(NEWBIE) == (TENANT, OID_NEW)


def test_an_unapproved_domain_is_refused_and_nothing_is_created(client, monkeypatch, logs):
    """Unchanged behaviour."""
    _seed_domains([APPROVED])
    r = _sign_in(client, monkeypatch, STRANGER)
    assert r.status_code == 401
    assert not local_store.AuthStore().user_exists(STRANGER)
    assert _who(client) is None
    assert _logged(logs, "SSO_UNKNOWN_ACCOUNT")
    assert not _logged(logs, "SSO_DOMAIN_ACCOUNT_PROVISIONED")


def test_the_auto_provision_setting_keeps_its_own_log_line(client, monkeypatch, logs):
    """Unchanged behaviour: SSO_AUTO_PROVISION provisions any domain and
    logs SSO_ACCOUNT_PROVISIONED, not the domain line."""
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", True)
    r = _sign_in(client, monkeypatch, STRANGER)
    assert r.status_code == 302
    assert local_store.AuthStore().sso_identity(STRANGER) == (TENANT, OID_NEW)
    assert _logged(logs, "SSO_ACCOUNT_PROVISIONED")
    assert not _logged(logs, "SSO_DOMAIN_ACCOUNT_PROVISIONED")
