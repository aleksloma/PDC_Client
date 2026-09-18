"""Gating and fixtures for the live-stack integration tests.

WHY a package of its own: everything here needs a RUNNING two-container
compose stack (`pdc-client` on a host port, `pdc-executor` on the internal
network) and, for the LLM matrix, a reachable brain. None of that exists on a
devbox running `pytest tests/ -q`, so every item is skipped unless
`PDC_STACK_URL` is set — and `PDC_STACK_BRAIN` on top for the `needs_brain`
items.

The gate is `pytest_collection_modifyitems`, NOT a module-level
`pytest.skip(allow_module_level=True)` in this conftest: pytest 9 catches
`Skipped` while IMPORTING a conftest and silently drops the plugin, which
would leave the tests collected WITHOUT their fixtures and red for the wrong
reason.

Everything the tests create is thrown away, and that sentence is
load-bearing: these tests run against a PERSISTENT customer-shaped volume, so
anything not removed accumulates there forever. Three kinds of directory are
created — the account `integration-<hex>@example.invalid` (by the real login
route, which sets the password a genuinely new email arrives with), the chats
`/generate_chatdata` promotes, and the UPLOAD SESSION directory `/new_session`
opens, which holds the uploaded fixture file and its parquet cache. The app
has no delete endpoint for any of them, so teardown removes all three with
`docker exec pdc-client rm -rf` and, when `docker` is not on PATH, PRINTS
exactly what it left behind.
"""
import base64
import json
import os
import secrets
import shutil
import subprocess

import httpx
import pytest

STACK_URL_ENV = "PDC_STACK_URL"
STACK_BRAIN_ENV = "PDC_STACK_BRAIN"
WEB_CONTAINER = "pdc-client"
EXECUTOR_CONTAINER = "pdc-executor"

SKIP_NO_STACK = f"set {STACK_URL_ENV} to run the stack integration tests"
SKIP_NO_BRAIN = f"set {STACK_BRAIN_ENV} to run the brain-backed matrix"

REQUEST_TIMEOUT_S = 300.0
FIXTURE_CSV = "sample_sales.csv"
SESSION_COOKIE = "session"


def session_id(client) -> str:
    """The upload-session id, decoded from the session cookie.

    `/new_session` answers `{"ok": true}` and no endpoint reports the id, but
    the cookie carries it in clear: the middleware b64-encodes the session
    dict and SIGNS it, and a signature protects integrity rather than
    confidentiality, so the payload before the first `.` decodes without the
    secret. Read from the real source rather than guessed, because a
    directory on the persistent volume is named after it. Returns "" when it
    cannot be read — teardown then reports what it could not remove instead
    of deleting something else.
    """
    raw = client.cookies.get(SESSION_COOKIE) or ""
    payload = raw.split(".")[0]
    if not payload:
        return ""
    padded = payload + "=" * (-len(payload) % 4)
    for decode in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            data = json.loads(decode(padded))
        except Exception:
            continue
        if isinstance(data, dict):
            sid = data.get("sid")
            if isinstance(sid, str) and sid:
                return sid
    return ""


def pytest_collection_modifyitems(config, items):
    """Skip-mark every item in this package unless the stack is configured."""
    no_stack = not (os.environ.get(STACK_URL_ENV) or "").strip()
    no_brain = not (os.environ.get(STACK_BRAIN_ENV) or "").strip()
    here = os.path.dirname(os.path.abspath(__file__))
    for item in items:
        path = str(getattr(item, "fspath", ""))
        if not os.path.abspath(path).startswith(here):
            continue
        if no_stack:
            item.add_marker(pytest.mark.skip(reason=SKIP_NO_STACK))
            continue
        if no_brain and item.get_closest_marker("needs_brain") is not None:
            item.add_marker(pytest.mark.skip(reason=SKIP_NO_BRAIN))


def docker_available() -> bool:
    return shutil.which("docker") is not None


def docker_exec(container: str, *argv: str, check: bool = False):
    """One `docker exec` — returns the CompletedProcess, never raises on a
    non-zero exit unless asked (several checks here EXPECT a failure)."""
    return subprocess.run(
        ["docker", "exec", container, *argv],
        capture_output=True, text=True, timeout=120, check=check)


@pytest.fixture(scope="session")
def base_url() -> str:
    url = (os.environ.get(STACK_URL_ENV) or "").strip().rstrip("/")
    assert url, SKIP_NO_STACK
    return url


@pytest.fixture(scope="session")
def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="session")
def account() -> dict:
    """A throwaway identity. `.invalid` is reserved by RFC 2606, so the
    welcome mail can never reach a real mailbox."""
    suffix = secrets.token_hex(4)
    return {"email": f"integration-{suffix}@example.invalid",
            "password": f"pw-{secrets.token_hex(8)}"}


@pytest.fixture(scope="session")
def session(base_url, account):
    """A logged-in HTTP session against the stack.

    The login route sets the password for a genuinely new email, so no
    out-of-band account creation is needed.
    """
    client = httpx.Client(base_url=base_url, follow_redirects=True,
                          timeout=REQUEST_TIMEOUT_S)
    response = client.post("/auth/login",
                           data={"email": account["email"],
                                 "password": account["password"]})
    status = response.status_code
    assert status == 200, (
        f"login for {account['email']} failed: {status} {response.text[:300]}")
    yield client
    client.close()


@pytest.fixture(scope="session", autouse=True)
def cleanup(account, session_scoped_chat_ids, session_scoped_upload_sids):
    """Remove the throwaway user, chat AND upload-session directories.

    The upload session is the one that used to be missed: it is not named in
    any response, it holds the uploaded file and its parquet cache, and it
    outlives the run on a volume that is never wiped.
    """
    yield
    email = account["email"]
    targets = [f"/data/client/users/{email}"]
    targets += [f"/data/client/chatdata/{cid}" for cid in session_scoped_chat_ids]
    unreadable = sorted(sid for sid in session_scoped_upload_sids if not sid)
    targets += [f"/data/client/sessions/{sid}"
                for sid in sorted(session_scoped_upload_sids) if sid]
    if unreadable:
        print("\nintegration cleanup could not read an upload-session id; "
              "check /data/client/sessions by hand")
    if not docker_available():
        print("\nintegration cleanup SKIPPED (no docker on PATH). Left behind:")
        for target in targets:
            print(f"  {WEB_CONTAINER}:{target}")
        return
    for target in targets:
        result = docker_exec(WEB_CONTAINER, "rm", "-rf", target)
        if result.returncode != 0:
            print(f"\nintegration cleanup could not remove {target}: "
                  f"{result.stderr[:200]}")


@pytest.fixture(scope="session")
def session_scoped_chat_ids() -> list:
    """Chat ids created during the run, collected for teardown."""
    return []


@pytest.fixture(scope="session")
def session_scoped_upload_sids() -> set:
    """Upload-session ids seen during the run, collected for teardown.

    A SET because the id rotates on every `/new_session` and each value it
    ever held left a directory behind.
    """
    return set()


@pytest.fixture(scope="session")
def chat(session, repo_root, session_scoped_chat_ids,
         session_scoped_upload_sids) -> str:
    """One real chat over the sample CSV: /new_session -> /upload -> chatdata.

    `generate_chatdata` calls the brain for the chat name, but it degrades
    when the brain is absent, so this fixture works for the no-brain group
    too — which is what makes the deterministic sandbox probes runnable
    without an LLM.
    """
    reset = session.post("/new_session")
    assert reset.status_code == 200, (reset.status_code, reset.text[:300])
    # Recorded here, and again after the upload: the id rotates on
    # `/new_session`, and every value it held owns a directory.
    session_scoped_upload_sids.add(session_id(session))

    csv_path = os.path.join(repo_root, "tools", "fixtures", FIXTURE_CSV)
    assert os.path.isfile(csv_path), csv_path
    with open(csv_path, "rb") as handle:
        upload = session.post(
            "/upload",
            files={"files": (FIXTURE_CSV, handle.read(), "text/csv")})
    assert upload.status_code == 200, (upload.status_code, upload.text[:300])
    uploaded = upload.json()
    assert uploaded.get("ok") is True, uploaded
    frames = uploaded.get("dataframes") or []
    assert frames, uploaded
    session_scoped_upload_sids.add(session_id(session))

    created = session.post("/generate_chatdata", json={})
    assert created.status_code == 200, (created.status_code, created.text[:300])
    body = created.json()
    chat_id = body.get("chat_id")
    assert chat_id, body
    session_scoped_chat_ids.append(chat_id)
    return chat_id
