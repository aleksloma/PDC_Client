"""Make the client package root importable when running pytest from PDC_Client/.

It also keeps the whole suite executing generated code IN PROCESS. The two
public entry points (`code_exec.safe_execute`, `plot_utils.render_plot_safe`)
dispatch the job to the analysis-sandbox container over HTTP, which no unit
test has; an autouse fixture rebinds them — and the two names
`run_chat_local` holds — to the in-process functions the sandbox runner
imports, so every existing test behaves exactly as it did when the bodies
were still inline. A test that wants the real dispatching function carries
`@pytest.mark.real_executor_dispatch` and opts out.

A second autouse fixture keeps the suite OFFLINE around the sandbox
reachability cache. `executor_client.reachable()` is what `/health` reads,
and when the cache is stale it starts a short-lived daemon thread that does a
real `GET http://pdc-executor:8090/healthz` — a live DNS lookup from whatever
machine runs pytest. Two reasons that cannot be left on: the suite may make no
network attempt at all, and the thread lands SECONDS LATER and writes the
cached state, so it can race a later test that asserts the cache is still
untouched into an intermittent failure. The fixture therefore stops the
refresh from being claimed and restores the cache after every test; a test
that deliberately exercises the probe carries
`@pytest.mark.executor_probe`.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

# Imported HERE, at conftest MODULE scope, BEFORE any patching can happen.
# `run_chat_local` binds `safe_execute` / `render_plot_safe` into its own
# namespace at ITS import time, so if its first import ever happened while the
# rebinding below was active it would freeze to the in-process function for the
# rest of the session and `monkeypatch.undo` could never restore it — the
# dispatch tests would then "pass" without a single byte reaching the
# transport. Importing all three up front makes the binding happen once, with
# the production functions in place.
import code_exec  # noqa: E402
import executor_client  # noqa: E402
import plot_utils  # noqa: E402
import run_chat_local  # noqa: E402

REAL_DISPATCH_MARKER = "real_executor_dispatch"
INTEGRATION_MARKER = "integration"
NEEDS_BRAIN_MARKER = "needs_brain"
EXECUTOR_PROBE_MARKER = "executor_probe"


def pytest_configure(config):
    # There is no pytest.ini / pyproject.toml in this repo, so an unregistered
    # mark is a warning on every run.
    config.addinivalue_line(
        "markers",
        f"{REAL_DISPATCH_MARKER}: keep the real dispatching public exec "
        "functions instead of the in-process rebinding (the test provides a "
        "transport of its own)",
    )
    # tests/integration/ drives a RUNNING compose stack over HTTP; its own
    # conftest skips every item unless PDC_STACK_URL (and, for needs_brain,
    # PDC_STACK_BRAIN) is set, so the default run stays fully offline.
    config.addinivalue_line(
        "markers",
        f"{INTEGRATION_MARKER}: runs against the live docker-compose stack "
        "(skipped unless PDC_STACK_URL is set)",
    )
    config.addinivalue_line(
        "markers",
        f"{NEEDS_BRAIN_MARKER}: additionally needs a reachable brain "
        "(skipped unless PDC_STACK_BRAIN is set)",
    )
    config.addinivalue_line(
        "markers",
        f"{EXECUTOR_PROBE_MARKER}: let the sandbox reachability refresh start "
        "(the test drives the probe itself and supplies its own stub)",
    )


@pytest.fixture(autouse=True)
def no_sandbox_reachability_probe(request, monkeypatch):
    """Keep the reachability refresh thread from ever starting.

    `reachable()` is called by `/health`, so any test that touches that route
    — directly or through a page — would otherwise spawn a daemon thread
    resolving `pdc-executor` on the real network. Patching the claim function
    is enough: the cache stays byte-identical, so a test asserting "nothing
    has been checked yet" still sees exactly that, and nothing is left running
    after the test returns.

    The cache is a module-level dict, so it is snapshotted and restored too —
    a test that writes into it must not leak that state into the next one.
    """
    saved = dict(executor_client._REACH)
    if not request.node.get_closest_marker(EXECUTOR_PROBE_MARKER):
        monkeypatch.setattr(executor_client, "_claim_refresh", lambda: False)
    try:
        yield
    finally:
        executor_client._REACH.clear()
        executor_client._REACH.update(saved)


@pytest.fixture(autouse=True)
def execute_generated_code_in_process(request, monkeypatch):
    """Point the public exec names at the in-process functions.

    Uses the `monkeypatch` FIXTURE on purpose: an autouse fixture and the
    test's own `monkeypatch` argument are the same function-scoped instance,
    and `undo` runs in reverse order — so a test that patches
    `run_chat_local.safe_execute` with its own fake still wins, and the
    rebinding is restored afterwards.

    `routes/chat.py` imports the two names at CALL time, so patching the
    module attributes covers it; `run_chat_local` imports them at module
    scope, hence the extra two rebindings.
    """
    if request.node.get_closest_marker(REAL_DISPATCH_MARKER):
        return
    in_process = getattr(code_exec, "_execute_in_process", None)
    render_in_process = getattr(plot_utils, "_render_in_process", None)
    if in_process is None or render_in_process is None:
        # The split has not landed yet: the public names ARE the in-process
        # bodies, so there is nothing to rebind. A structural test pins that
        # both names exist, so this branch cannot hide a regression.
        return
    monkeypatch.setattr(code_exec, "safe_execute", in_process)
    monkeypatch.setattr(plot_utils, "render_plot_safe", render_in_process)
    monkeypatch.setattr(run_chat_local, "safe_execute", in_process)
    monkeypatch.setattr(run_chat_local, "render_plot_safe", render_in_process)
