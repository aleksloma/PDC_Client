"""Re-assessment probe: a forged session id used as a filesystem path.

The original probe (V/sidforge*.py) re-signed the session cookie with the
server's key and put a path into `sid`, which the upload routes joined under
DATA_ROOT/sessions/ — reaching another account's folder. The probe scripts
are not in the repository; this test re-creates them against the REAL app:
a validly SIGNED cookie (the attacker is assumed to hold the key, the
strongest case) whose sid is a path. Every route that turns the sid into a
folder must refuse it, clear the session, and touch nothing.
"""
import json
import time
from base64 import b64encode

import pytest
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

import app as app_mod
import local_store
from settings import settings
from tests.conftest import JSON_HEADERS

USER = "prober@acme.com"
VICTIM = "victim@acme.com"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    store = local_store.AuthStore()
    for email in (USER, VICTIM):
        store.create_account(email)
        store.set_password(email, "a-good-password-1")
    marker = tmp_path / "users" / VICTIM / "keep.txt"
    marker.write_text("victim data", encoding="utf-8")
    return tmp_path, marker


def _forged_client(sid):
    session = {"email": USER, "sid": sid,
               "gen": local_store.AuthStore().session_generation(USER),
               "iat": int(time.time())}
    data = b64encode(json.dumps(session).encode("utf-8"))
    cookie = TimestampSigner(str(settings.SECRET_KEY)).sign(data).decode("utf-8")
    tc = TestClient(app_mod.app, base_url="https://testserver")
    tc.cookies.set("session", cookie)
    return tc


FORGED = ["../users/victim@acme.com", "../../etc", "s_../../users", "/tmp/x", "s_0000"]


@pytest.mark.parametrize("sid", FORGED)
@pytest.mark.parametrize("call", [
    ("post", "/upload", {"files": {"files": ("a.csv", b"a\n1\n", "text/csv")}}),
    ("post", "/new_session", {"headers": JSON_HEADERS}),
    ("post", "/generate_chatdata", {"json": {}}),
    ("post", "/schema_autofill_full", {"json": {}}),
    ("get", "/schema_details", {}),
])
def test_a_forged_sid_is_refused_and_touches_nothing(world, sid, call):
    tmp, marker = world
    before = sorted(p.relative_to(tmp).as_posix() for p in tmp.rglob("*"))
    method, path, kwargs = call
    tc = _forged_client(sid)
    r = getattr(tc, method)(path, **kwargs)
    if path == "/new_session":
        # This route REPLACES the sid: the forged value is discarded (its
        # folder is not deleted — `UserStore.destroy` refuses the shape) and
        # a fresh, well-formed one is issued.
        assert r.status_code == 200, (sid, r.status_code, r.text[:200])
        issued = [h.split(";", 1)[0].split("=", 1)[1] for h in r.headers.get_list("set-cookie")
                  if h.startswith("session=")]
        assert issued, r.headers
        data = TimestampSigner(str(settings.SECRET_KEY)).unsign(issued[-1].encode("utf-8"))
        from base64 import b64decode
        assert local_store.valid_sid(json.loads(b64decode(data))["sid"])
    else:
        assert r.status_code in (401, 400), (path, sid, r.status_code, r.text[:200])
    assert marker.read_text(encoding="utf-8") == "victim data"
    after = sorted(p.relative_to(tmp).as_posix() for p in tmp.rglob("*"))
    created = [p for p in after if p not in before and not (p.startswith(("logs", "sessions/s_")) or p == "sessions")]
    assert created == [], created
