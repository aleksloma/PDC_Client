"""The web app refuses requests coming from the sandbox's own network.

WHY this exists: the analysis sandbox shares an internal Docker network with
the web service so the dispatcher can reach `pdc-executor:8090` — and Docker
networks are BIDIRECTIONAL. Nothing stops generated Python inside the sandbox
from opening `http://pdc-client:8000/auth/login` or the reset flow, which need
no session. The topology cannot express "one direction only", so the app
itself refuses any request whose SOCKET PEER address falls inside the pinned
backend subnet (`settings.EXECUTOR_NETWORK_CIDR`, fed by the same compose
variable as the network's ipam block, so the two can never disagree).

The peer address is the one thing an attacker inside the sandbox cannot
choose — it is not a header. The one exception is a TRUSTED reverse proxy:
uvicorn's `ProxyHeadersMiddleware` rewrites `scope["client"]` from
`X-Forwarded-For` for peers listed in `FORWARDED_ALLOW_IPS`, and it wraps
OUTSIDE the app, so with `FORWARDED_ALLOW_IPS=*` the sandbox can present any
address it likes. That limitation is pinned by the last test here and is why
`CUSTOMER_INSTALL.md` forbids the wildcard.

Offline: `DATA_ROOT` is `tmp_path`, no tenant token (so `/health` never
reaches the brain), and the TestClient is built WITHOUT the context manager
(the lifespan would start the db_scheduler thread — see
tests/test_version_endpoint.py). Every asserted value is bound to a local
first so a failure prints it.
"""
import pytest
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
from settings import settings

CIDR = "192.168.255.240/28"
INSIDE = ("192.168.255.242", 51000)
OUTSIDE = ("10.11.12.13", 51000)
NON_IP = ("testclient", 50000)
PROXY_PRESENTED = "203.0.113.9"

FORBIDDEN_BODY = {"error": "forbidden"}
GUARDED_PATH = "/lab"
HEALTH_PATH = "/health"

BAD_CIDRS = ["not-a-cidr", "10.0.0.0/99", "192.168.255.240/", " ", "::/oops"]


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    local_store._DATAFRAME_CACHE.invalidate()
    yield
    local_store._DATAFRAME_CACHE.invalidate()


def _client(peer) -> TestClient:
    return TestClient(app_mod.app, base_url="https://testserver", client=peer)


def _set_cidr(monkeypatch, value) -> None:
    """The guard reads the setting at REQUEST time.

    `raising=False` because the field does not exist yet; if the
    implementation caches the parsed network at IMPORT time instead, these
    tests fail and that is the correct signal, not something to work around.

    The `hasattr` check is explicit because `Settings` is a pydantic model:
    assigning an undeclared field raises a ValidationError whose text would
    bury the real reason the test is red.
    """
    assert hasattr(settings, "EXECUTOR_NETWORK_CIDR"), (
        "settings must define EXECUTOR_NETWORK_CIDR (str, default \"\"), read "
        "at request time by the backend-network guard")
    monkeypatch.setattr(settings, "EXECUTOR_NETWORK_CIDR", value, raising=False)


def test_a_peer_inside_the_backend_subnet_is_refused(monkeypatch):
    """The sandbox's own address range never gets an answer from the app."""
    _set_cidr(monkeypatch, CIDR)
    response = _client(INSIDE).get(GUARDED_PATH, follow_redirects=False)
    status = response.status_code
    assert status == 403, (status, response.text[:300])
    body = response.json()
    assert body == FORBIDDEN_BODY, body


def test_the_refusal_body_leaks_no_path_no_cidr_and_no_internal_detail(monkeypatch):
    """A 403 that named the path or the subnet would teach the caller the
    topology it is being denied."""
    _set_cidr(monkeypatch, CIDR)
    response = _client(INSIDE).get(GUARDED_PATH, follow_redirects=False)
    text = response.text
    assert response.status_code == 403, (response.status_code, text[:300])
    assert "lab" not in text, text
    assert CIDR not in text, text
    assert "192.168" not in text, text
    assert INSIDE[0] not in text, text
    assert "executor" not in text.lower(), text


def test_the_same_peer_outside_the_subnet_reaches_the_route(monkeypatch):
    """Browser and reverse-proxy traffic arrives over the default network, so
    it is never inside the CIDR."""
    _set_cidr(monkeypatch, CIDR)
    response = _client(OUTSIDE).get(GUARDED_PATH, follow_redirects=False)
    status = response.status_code
    assert status != 403, (status, response.text[:300])
    assert status in (200, 302), (status, response.text[:300])


def test_an_empty_setting_allows_everything(monkeypatch):
    """Unset = inert: a single-container dev run has no backend network."""
    _set_cidr(monkeypatch, "")
    response = _client(INSIDE).get(GUARDED_PATH, follow_redirects=False)
    status = response.status_code
    assert status != 403, (status, response.text[:300])


@pytest.mark.parametrize("value", BAD_CIDRS)
def test_a_malformed_setting_disables_the_guard_and_never_raises(monkeypatch, value):
    """Article IV: a typo in one env var must not turn every request into a
    500 — it logs `BACKEND_CIDR_INVALID` and the guard stands down."""
    _set_cidr(monkeypatch, value)
    client = _client(INSIDE)
    response = client.get(GUARDED_PATH, follow_redirects=False)
    status = response.status_code
    assert status != 403, (value, status, response.text[:300])
    assert status < 500, (value, status, response.text[:300])
    # The app still answers a second request (the guard did not poison state).
    again = client.get(HEALTH_PATH)
    assert again.status_code == 200, (value, again.status_code, again.text[:300])


def test_health_is_refused_from_inside_too(monkeypatch):
    """The guard is outermost and unconditional: there is no allowlisted path,
    because the sandbox never needs to call the app at all. The container's
    own healthcheck runs over loopback, not over the backend network."""
    _set_cidr(monkeypatch, CIDR)
    response = _client(INSIDE).get(HEALTH_PATH)
    status = response.status_code
    assert status == 403, (status, response.text[:300])
    body = response.json()
    assert body == FORBIDDEN_BODY, body


def test_health_from_a_normal_peer_still_answers(monkeypatch):
    _set_cidr(monkeypatch, CIDR)
    response = _client(OUTSIDE).get(HEALTH_PATH)
    status = response.status_code
    assert status == 200, (status, response.text[:300])
    body = response.json()
    assert body.get("status") == "ok", body


def test_a_non_ip_peer_is_allowed(monkeypatch):
    """Starlette's default TestClient peer host is the literal string
    "testclient", and `scope["client"]` can also be None behind some servers
    — neither parses as an address and neither may become a 403."""
    _set_cidr(monkeypatch, CIDR)
    response = _client(NON_IP).get(HEALTH_PATH)
    status = response.status_code
    assert status == 200, (status, response.text[:300])


def test_a_trusted_proxy_can_present_any_address_known_limitation(monkeypatch):
    """PINNED LIMITATION, not a wish.

    uvicorn enables `ProxyHeadersMiddleware` by default and it wraps OUTSIDE
    the ASGI app, rewriting `scope["client"]` from `X-Forwarded-For` for every
    peer in `FORWARDED_ALLOW_IPS`. With the wildcard — the common "behind
    nginx" idiom — a request FROM the backend subnet that carries a header
    pointing elsewhere is what the guard sees, and it is allowed. This is why
    `CUSTOMER_INSTALL.md` forbids `FORWARDED_ALLOW_IPS=*` and any range that
    contains `PDC_BACKEND_SUBNET`.
    """
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    _set_cidr(monkeypatch, CIDR)
    wrapped = ProxyHeadersMiddleware(app_mod.app, trusted_hosts="*")
    client = TestClient(wrapped, base_url="https://testserver", client=INSIDE)
    response = client.get(HEALTH_PATH, headers={"X-Forwarded-For": PROXY_PRESENTED})
    status = response.status_code
    assert status == 200, (status, response.text[:300])
    # And the guard DOES bite the same peer when no proxy rewrites it.
    direct = _client(INSIDE).get(HEALTH_PATH)
    assert direct.status_code == 403, (direct.status_code, direct.text[:300])


def test_the_setting_exists_and_defaults_to_empty():
    """Default OFF so a single-container install behaves exactly as today."""
    from settings import Settings

    value = getattr(Settings(), "EXECUTOR_NETWORK_CIDR", None)   # local: no Settings repr
    assert value == ""


# ---------------------------------------------------------------------------
# the guard's own log line, and the address forms it has to recognise
# ---------------------------------------------------------------------------
# The ASGI server percent-DECODES the request path before the app sees it, so
# `/lab%0A…` arrives carrying a real newline. The refusal line is the one
# place the guard repeats attacker-chosen text, and the caller it exists for
# is assumed hostile, so that text must not be able to forge a line in a
# newline-delimited log file.
FORGED_PATH = "/lab%0A2026-09-17%2000:00:00,000%20%7C%20INFO%20%7C%20forged=1"

MAPPED_INSIDE = ("::ffff:192.168.255.242", 51000)
MAPPED_OUTSIDE = ("::ffff:10.0.0.1", 51000)
PLAIN_IPV6 = ("2001:db8::1", 51000)

REFUSED_EVENT = "BACKEND_REQUEST_REFUSED"


@pytest.fixture(autouse=True)
def isolated_refusal_latch():
    """`app._BACKEND_REFUSED` is a module-level set — the once-per-address
    latch that stops a hostile caller filling the disk by retrying. It is
    process-wide, so it is snapshotted and restored around every test here,
    or the second test in the file would log nothing."""
    saved = set(app_mod._BACKEND_REFUSED)
    app_mod._BACKEND_REFUSED.clear()
    try:
        yield app_mod._BACKEND_REFUSED
    finally:
        app_mod._BACKEND_REFUSED.clear()
        app_mod._BACKEND_REFUSED.update(saved)


@pytest.fixture
def refusal_log(monkeypatch):
    monkeypatch.setattr(app_mod, "log_with_sid",
                        lambda sid, level, message, *a, **k: lines.append(message))
    lines: list = []
    return lines


def test_a_refused_path_cannot_forge_a_log_line(monkeypatch, refusal_log):
    """One line, with the path escaped — not the newline the caller chose."""
    _set_cidr(monkeypatch, CIDR)

    response = _client(INSIDE).get(FORGED_PATH, follow_redirects=False)
    status = response.status_code
    assert status == 403, (status, response.text[:200])

    hits = [str(line) for line in refusal_log if REFUSED_EVENT in str(line)]
    assert len(hits) == 1, refusal_log
    message = hits[0]
    assert "\n" not in message, repr(message)
    assert "\r" not in message, repr(message)
    assert "\\n" in message, message
    # The path is QUOTED as well as escaped, so a reader can see where it ends.
    assert "path='" in message or 'path="' in message, message
    assert INSIDE[0] in message, message


def test_the_refusal_line_is_logged_once_per_address(monkeypatch, refusal_log):
    """A hostile caller retrying in a loop must not be able to fill the log."""
    _set_cidr(monkeypatch, CIDR)
    client = _client(INSIDE)

    for _ in range(3):
        client.get(HEALTH_PATH)

    hits = [str(line) for line in refusal_log if REFUSED_EVENT in str(line)]
    assert len(hits) == 1, refusal_log


def test_an_ipv4_mapped_peer_inside_the_range_is_refused(monkeypatch):
    """An IPv6 listener reports an IPv4 peer as `::ffff:192.168.255.242`, and
    that object is NOT `in` an IPv4 network — so without the mapping the guard
    stands down for the WHOLE range. Inert while the server binds IPv4, which
    is exactly why it needs a test rather than a comment: one `--host ::` in a
    customer's compose override would otherwise switch the guard off in
    silence."""
    _set_cidr(monkeypatch, CIDR)
    response = _client(MAPPED_INSIDE).get(HEALTH_PATH)
    status = response.status_code
    assert status == 403, (status, response.text[:300])
    body = response.json()
    assert body == FORBIDDEN_BODY, body


@pytest.mark.parametrize("peer", [MAPPED_OUTSIDE, PLAIN_IPV6],
                         ids=["mapped_outside", "plain_ipv6"])
def test_other_ipv6_forms_are_not_refused(monkeypatch, peer):
    """The mapping must not become "refuse anything that looks like IPv6": a
    mapped address outside the range and a genuine IPv6 peer both belong to
    ordinary traffic."""
    _set_cidr(monkeypatch, CIDR)
    response = _client(peer).get(HEALTH_PATH)
    status = response.status_code
    assert status == 200, (peer, status, response.text[:300])
