"""The two-container stack, proven from the outside.

WHY this file exists and the unit suite is not enough: every claim this
repository makes about the sandbox — a different uid, no egress, no
DB drivers, no customer data volume, no route back into the web app, a job
that cannot run forever — is a property of the COMPOSE TOPOLOGY, not of any
Python function. A unit test with a mocked transport cannot falsify a missing
`networks:` key or a volume that was mounted by accident. These tests can,
because they ask the running sandbox to describe itself.

The deterministic group needs no LLM: `POST /api/chat/{id}/refresh_item` with
`kind: "table"` executes POSTED code against the chat frames in the sandbox
and returns the resulting one-row table (`routes/chat.run_item_refresh`). So
each probe is a normal product request, not a back door.

Every test is marked `integration` (skipped unless `PDC_STACK_URL` is set);
the LLM matrix is additionally `needs_brain`. Each probe gets its OWN test and
its own posted code so a failure names itself. Generous timeouts throughout —
a cold sandbox plus a Brain round trip is slow, and a flaky timeout here would
train people to ignore the file.
"""
import base64
import json
import struct
import subprocess
import threading
import time

import httpx
import pytest

from .conftest import (EXECUTOR_CONTAINER, WEB_CONTAINER, docker_available,
                       docker_exec)

pytestmark = pytest.mark.integration

SANDBOX_UID = 10002
WEB_UID = 10001
JOBS_ROOT = "/jobs"
JOBS_MODE = "2770"

TIMEOUT_TEXT_SECONDS = 60
TIMEOUT_CEILING_S = 60 + 15 + 30

SECRET_ENV_RE = r"BRAIN|SECRET|ENCRYPTION|ADMIN|TOKEN"
LOCAL_PLOTLY_URL = "/static/vendor/plotly/plotly.min.js"
CDN_PLOTLY_HOST = "cdn.plot.ly"
PDF_MAGIC = b"%PDF"
PPTX_MAGIC = b"PK"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
TRACEBACK_MARKER = "Traceback (most recent call last)"

# Questions must name columns the fixture ACTUALLY has
# (tools/fixtures/sample_sales.csv = order_id, customer_id, region, product,
# revenue, order_date). An earlier draft asked for "revenue by city": the
# planner correctly answered "the column 'city' does not exist in the loaded
# data" — prose, no table, no chart — so the tests were measuring their own
# typo instead of the sandbox.
TABLE_QUESTION = "Show total revenue by region as a table."
CHART_QUESTION = "Draw a bar chart of total revenue by region."
# The region values in the fixture. A rendered chart / returned table that
# contains one of these was computed from the REAL rows inside the sandbox;
# an LLM apology or an empty placeholder contains none of them.
FIXTURE_REGIONS = ("North", "South", "East", "West")

# A deliberately matplotlib-only figure, posted as code (no LLM involved): the
# groupby raises when the chat frames are missing or empty, so the PNG proves
# the fixture rows were read AND rendered inside the sandbox.
MATPLOTLIB_CHART_CODE = """df = dfs['sample_sales.csv']
totals = df.groupby('region')['revenue'].sum()
if len(totals) < 2:
    raise ValueError('the chat frames did not reach the sandbox')
fig, ax = plt.subplots(figsize=(6, 4))
ax.bar(totals.index.astype(str), totals.values)
ax.set_title('Revenue by region')
"""

DOCKER_REASON = "docker is not on PATH"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _probe(session, chat_id: str, body_code: str, timeout: float = 180.0) -> dict:
    """Run one probe inside the sandbox and return its single-row table.

    `body_code` must assign a dict to `values`; the wrapper turns it into the
    one-row `RESULT` frame `refresh_item` knows how to render.
    """
    code = (
        "import pandas as pd\n"
        f"{body_code}\n"
        "RESULT = pd.DataFrame([values])\n"
    )
    response = session.post(f"/api/chat/{chat_id}/refresh_item",
                            json={"kind": "table", "code": code},
                            timeout=timeout)
    status = response.status_code
    assert status == 200, (status, response.text[:500])
    payload = response.json()
    assert payload.get("ok") is True, payload
    table = payload.get("table") or {}
    rows = table.get("rows") or []
    assert len(rows) == 1, table
    return rows[0]


def _refresh_raw(session, chat_id: str, code: str, timeout: float) -> dict:
    response = session.post(f"/api/chat/{chat_id}/refresh_item",
                            json={"kind": "table", "code": code},
                            timeout=timeout)
    status = response.status_code
    assert status == 200, (status, response.text[:500])
    return response.json()


def _stream_events(session, chat_id: str, question: str,
                   timeout: float = 300.0) -> list:
    """Drive one chat turn over SSE and return EVERY event.

    The chart tests need the whole list, not just the tail: a chart answer
    always runs through the per-chart pipeline (`run_chat_local.py:1123` routes
    even a LONE `PLOT_CODE` block into it), so the rendered figure arrives on a
    `partial` event and the final `done` event carries `image_base64: None` and
    `table: None` BY DESIGN (`routes/chat.py:1071-1080`) — exactly what
    `static/dashboard.js` consumes (it renders `data.image_base64` in the
    `data.partial` branch, line 3092, and only renders the done event when
    `chartCount === 0`, line 3119).
    """
    events = []
    with session.stream("POST", f"/api/chat/{chat_id}/chat/stream",
                        json={"question": question}, timeout=timeout) as response:
        status = response.status_code
        assert status == 200, (status, response.read()[:500])
        for line in response.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[len("data: "):])
            except ValueError:
                continue
            events.append(event)
            if event.get("done"):
                break
    assert events, f"the stream produced no events for: {question}"
    done = [event for event in events if event.get("done")]
    assert len(done) == 1, [sorted(event) for event in events]
    return events


def _stream(session, chat_id: str, question: str, timeout: float = 300.0) -> dict:
    """Drive one chat turn over SSE and return the `done` event."""
    events = _stream_events(session, chat_id, question, timeout)
    return [event for event in events if event.get("done")][0]


def _labels(table) -> set:
    """Every column name and cell value of a {columns, rows} table as strings.

    Used to prove REAL rows came back: the fixture's region names can only
    appear here if the grouping ran on the uploaded CSV in the sandbox. The
    multi-subplot chart-data shape ({tables: [...]}, `plot_utils.
    _extract_plotly_chart_data`) is flattened the same way.
    """
    out = set()
    if not isinstance(table, dict):
        return out
    for column in table.get("columns") or []:
        out.add(str(column))
    for row in table.get("rows") or []:
        if isinstance(row, dict):
            out.update(str(value) for value in row.values())
        else:
            out.add(str(row))
    for nested in table.get("tables") or []:
        out |= _labels(nested)
    return out


def _has_fixture_region(haystack) -> bool:
    values = haystack if isinstance(haystack, set) else {str(haystack)}
    return any(any(region in value for value in values)
               for region in FIXTURE_REGIONS)


def _refresh_chart(session, chat_id: str, code: str, timeout: float = 240.0) -> dict:
    """Render ONE posted chart in the sandbox (`kind: "chart"` →
    `plot_utils.render_plot_safe`) and return the route payload."""
    response = session.post(f"/api/chat/{chat_id}/refresh_item",
                            json={"kind": "chart", "code": code},
                            timeout=timeout)
    status = response.status_code
    assert status == 200, (status, response.text[:500])
    return response.json()


# ===========================================================================
# 1. the sandbox describes itself (no LLM)
# ===========================================================================
def test_generated_code_runs_as_the_sandbox_uid(session, chat):
    """The analysis process must not be the web uid. 10002 in group
    10001 is what lets it share the jobs volume and nothing else."""
    row = _probe(session, chat, "import os\nvalues = {'uid': int(os.getuid())}")
    uid = int(row["uid"])
    assert uid == SANDBOX_UID, row
    assert uid != WEB_UID, row


def test_generated_code_runs_in_a_different_container(session, chat):
    """A hostname equal to the web container would mean the dispatch never
    left the app — the single most important negative result here."""
    row = _probe(session, chat, "import socket\nvalues = {'host': socket.gethostname()}")
    sandbox_host = str(row["host"])
    assert sandbox_host, row
    if docker_available():
        result = docker_exec(WEB_CONTAINER, "hostname")
        web_host = (result.stdout or "").strip()
        assert web_host, result.stderr[:200]
        assert sandbox_host != web_host, (sandbox_host, web_host)
    else:
        assert sandbox_host != WEB_CONTAINER, sandbox_host


def test_the_sandbox_has_no_internet_egress(session, chat):
    """`backend` is `internal: true`: no default route off the host."""
    row = _probe(session, chat,
                 "import socket\n"
                 "try:\n"
                 "    socket.create_connection(('1.1.1.1', 443), 3)\n"
                 "    values = {'outcome': 'CONNECTED'}\n"
                 "except Exception as exc:\n"
                 "    values = {'outcome': type(exc).__name__}",
                 timeout=240.0)
    outcome = str(row["outcome"])
    assert outcome != "CONNECTED", row


@pytest.mark.parametrize("module", ["psycopg2", "sqlalchemy"])
def test_the_sandbox_has_no_database_drivers(session, chat, module):
    """TWO claims, because only the second one is the boundary.

    The import RAISES — but `sandbox_guard` denies these names before the
    interpreter looks for them, so the exception type is its `ImportError`,
    not `ModuleNotFoundError`; the denylist is additive defense, never the
    boundary (constitution Art. VII r8-9). The boundary is that the packages
    are ABSENT FROM THE IMAGE, which `find_spec` answers without going
    through the guarded `__import__`.
    """
    row = _probe(session, chat,
                 "import importlib.util\n"
                 "try:\n"
                 f"    import {module}\n"
                 "    outcome = 'IMPORTED'\n"
                 "except BaseException as exc:\n"
                 "    outcome = type(exc).__name__\n"
                 "try:\n"
                 f"    spec = importlib.util.find_spec('{module}')\n"
                 "except BaseException as exc:\n"
                 "    spec = None\n"
                 "values = {'outcome': outcome, 'installed': spec is not None}")
    outcome = str(row["outcome"])
    assert outcome != "IMPORTED", row
    assert outcome in ("ImportError", "ModuleNotFoundError"), row
    assert bool(row["installed"]) is False, row


def test_the_sandbox_cannot_call_back_into_the_web_app(session, chat):
    """Docker networks are bidirectional, so the sandbox CAN open a
    socket to `pdc-client:8000` — the app refuses it by source address.
    Either an exception or a 403 is a pass; a 200 is the finding."""
    row = _probe(session, chat,
                 "import urllib.request\n"
                 "try:\n"
                 "    resp = urllib.request.urlopen("
                 "'http://pdc-client:8000/health', timeout=3)\n"
                 "    outcome = 'HTTP_%d' % resp.status\n"
                 "except Exception as exc:\n"
                 "    outcome = type(exc).__name__ + ':' + str(exc)[:80]\n"
                 "values = {'outcome': outcome}",
                 timeout=240.0)
    outcome = str(row["outcome"])
    assert outcome != "HTTP_200", row
    refused = outcome.startswith("HTTP_403") or not outcome.startswith("HTTP_2")
    assert refused, row


def test_the_sandbox_cannot_see_the_customer_data_volume(session, chat):
    """The jobs volume is the ONLY shared storage: no users, auth hashes,
    chats, data_sources, roles, sso config or logs."""
    row = _probe(session, chat,
                 "import os\n"
                 "values = {'data': os.path.exists('/data'),\n"
                 "          'client': os.path.exists('/data/client'),\n"
                 "          'jobs': os.path.isdir('/jobs')}")
    assert bool(row["data"]) is False, row
    assert bool(row["client"]) is False, row
    assert bool(row["jobs"]) is True, row


def test_the_sandbox_environment_carries_no_secret(session, chat):
    """The runner env is an ALLOWLIST built from scratch, and the service
    refuses to START when a secret-shaped variable is present at all."""
    row = _probe(session, chat,
                 "import os, re\n"
                 f"pattern = re.compile(r'{SECRET_ENV_RE}')\n"
                 "hits = sorted(k for k in os.environ if pattern.search(k))\n"
                 "values = {'hits': ','.join(hits)}")
    hits = str(row["hits"])
    assert hits == "", hits


def test_a_matplotlib_figure_comes_back_as_a_real_base64_png(session, chat):
    """The OTHER render branch of the sandbox hop: `plot_utils` returns a
    base64 PNG (`is_plotly: false`) instead of Plotly HTML, and both shapes
    ride the SAME `image_base64` key — `dashboard.js:2234` switches on the
    value itself (`<` plus "plotly" -> iframe, else `data:image/png;base64,`).

    Posted code, no LLM: the brain's planner prompt mandates interactive
    Plotly for every standard chart and keeps matplotlib for the exceptions
    only (decision trees, venn, upset, wordcloud, networkx, missingno,
    calplot, KDE/pair plots), so ASKING for "a static matplotlib histogram"
    returns a Plotly figure — correctly. The PNG branch therefore has to be
    driven the way the product itself drives it on a refresh: `refresh_item`
    with `kind: "chart"` -> `plot_utils.render_plot_safe` in the sandbox.
    """
    body = _refresh_chart(session, chat, MATPLOTLIB_CHART_CODE)
    assert body.get("ok") is True, body
    assert body.get("is_plotly") is False, body
    image = str(body.get("image_base64") or "")
    assert image, sorted(body)
    assert not image.lstrip().startswith("<"), image[:200]
    raw = base64.b64decode(image, validate=True)
    assert raw.startswith(PNG_MAGIC), raw[:16]
    # A real raster, not a 1x1 stub: IHDR is the first chunk and carries
    # width/height as big-endian uint32s at offset 16.
    width, height = struct.unpack(">II", raw[16:24])
    assert width >= 200 and height >= 100, (width, height)


# ===========================================================================
# 2. a runaway job cannot take the stack with it
# ===========================================================================
def test_an_infinite_loop_is_killed_and_health_keeps_answering(session, chat,
                                                              base_url):
    """Before the sandbox, `while True: pass` burned a web worker.

    Three claims in one test because they are one event: the job dies at the
    budget, `/health` answers DURING it (the app is not blocked), and the very
    next probe succeeds immediately (no lingering GIL starvation).
    """
    health_results = []
    stop = threading.Event()

    def poll_health():
        with httpx.Client(base_url=base_url, timeout=10.0) as probe:
            while not stop.is_set():
                try:
                    health_results.append(probe.get("/health").status_code)
                except Exception as exc:
                    health_results.append(type(exc).__name__)
                stop.wait(3)

    poller = threading.Thread(target=poll_health, daemon=True)
    poller.start()
    began = time.monotonic()
    try:
        body = _refresh_raw(session, chat, "while True:\n    pass\n",
                            timeout=TIMEOUT_CEILING_S + 60)
    finally:
        stop.set()
        poller.join(15)
    elapsed = time.monotonic() - began

    assert body.get("ok") is False, body
    assert elapsed >= TIMEOUT_TEXT_SECONDS - 5, elapsed
    assert elapsed < TIMEOUT_CEILING_S, elapsed
    # The route reports a generic sentence (the executor timeout text stays in
    # the log), so the CONTRACT asserted here is the 200 {ok: false} shape.
    error = str(body.get("error") or "")
    assert error, body
    assert set(health_results) == {200}, health_results

    row = _probe(session, chat, "values = {'alive': 1}")
    assert int(row["alive"]) == 1, row


# ===========================================================================
# 3. docker-level isolation (skipped without docker)
# ===========================================================================
@pytest.mark.skipif(not docker_available(), reason=DOCKER_REASON)
def test_the_executor_container_runs_as_10002():
    result = docker_exec(EXECUTOR_CONTAINER, "id")
    assert result.returncode == 0, result.stderr[:300]
    text = result.stdout
    assert f"uid={SANDBOX_UID}" in text, text


@pytest.mark.skipif(not docker_available(), reason=DOCKER_REASON)
def test_the_executor_rootfs_is_read_only():
    result = docker_exec(EXECUTOR_CONTAINER, "touch", "/app/x")
    assert result.returncode != 0, (result.stdout, result.stderr)


@pytest.mark.skipif(not docker_available(), reason=DOCKER_REASON)
def test_the_jobs_root_is_root_owned_and_group_writable():
    """The permission EDGE: the root must be `root:pdc 2770` before the web
    process creates its first job dir, or the sandbox sweep cannot clear
    what it does not own."""
    result = docker_exec(EXECUTOR_CONTAINER, "stat", "-c", "%U %G %a", JOBS_ROOT)
    assert result.returncode == 0, result.stderr[:300]
    fields = (result.stdout or "").strip().split()
    assert len(fields) == 3, result.stdout
    owner, group, mode = fields
    assert owner == "root", result.stdout
    assert group == "pdc", result.stdout
    assert mode == JOBS_MODE, result.stdout


@pytest.mark.skipif(not docker_available(), reason=DOCKER_REASON)
def test_the_executor_publishes_no_port_and_joins_only_the_internal_network():
    """`/execute` is unauthenticated by decision, so its reachability
    IS the access control."""
    inspect = subprocess.run(
        ["docker", "inspect", EXECUTOR_CONTAINER],
        capture_output=True, text=True, timeout=120)
    assert inspect.returncode == 0, inspect.stderr[:300]
    doc = json.loads(inspect.stdout)[0]
    ports = doc["NetworkSettings"]["Ports"] or {}
    published = {port: binding for port, binding in ports.items() if binding}
    assert published == {}, published
    networks = sorted(doc["NetworkSettings"]["Networks"])
    assert len(networks) == 1, networks
    assert "backend" in networks[0], networks


# ===========================================================================
# 4. the LLM matrix (needs the brain)
# ===========================================================================
@pytest.mark.needs_brain
def test_a_scalar_question_answers(session, chat):
    done = _stream(session, chat, "How many rows are in the data?")
    answer = str(done.get("answer") or "")
    assert answer.strip(), done
    assert TRACEBACK_MARKER not in answer, answer[:400]


@pytest.mark.needs_brain
def test_a_table_question_answers_and_the_full_table_resolves(session, chat):
    """THE TABLE CONTRACT: a tabular single-shot answer rides the `done` event
    as the capped PREVIEW `table` ({columns, rows, total_rows}) plus the
    durable `full_table_key`; `GET /api/chat/{id}/full_table/{key}` re-executes
    the stored code in the sandbox and returns the UNCAPPED rows
    (`routes/chat.py:1568`). dashboard.js renders exactly this pair
    (`appendMessage(..., data.table, data.full_table_key, ...)`, line 3125).
    """
    done = _stream(session, chat, TABLE_QUESTION)
    table = done.get("table")
    assert isinstance(table, dict), sorted(done)
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    assert columns, table
    assert rows, table
    # Real rows, computed in the sandbox — not an apology and not a stub.
    assert _has_fixture_region(_labels(table)), table

    key = done.get("full_table_key")
    assert key, sorted(done)
    full = session.get(f"/api/chat/{chat}/full_table/{key}")
    assert full.status_code == 200, (full.status_code, full.text[:300])
    body = full.json()
    full_rows = body.get("rows") or []
    assert full_rows, sorted(body)
    # The uncapped re-execution can never return FEWER rows than its preview.
    assert len(full_rows) >= len(rows), (len(full_rows), len(rows))
    assert _has_fixture_region(_labels(body)), body


@pytest.mark.needs_brain
def test_a_plotly_chart_uses_the_local_bundle_never_the_cdn(session, chat):
    """Prompt 21: a CDN script src renders every chart iframe blank on an
    air-gapped LAN, and the sandbox has no egress to fetch it either.

    THE CHART CONTRACT: the figure arrives on a `partial` event, never on
    `done` — see `_stream_events`. So this asserts the partial's HTML (what the
    browser puts in the iframe) and then pins the done event's deliberate
    emptiness, which is the part the earlier draft had backwards.
    """
    events = _stream_events(session, chat, CHART_QUESTION)
    charts = [event for event in events
              if event.get("partial") and event.get("image_base64")]
    assert charts, [sorted(event) for event in events]
    html = str(charts[0]["image_base64"])
    # dashboard.js:2234 iframes the value only when it is HTML mentioning plotly.
    assert html.lstrip().startswith("<"), html[:200]
    assert "plotly" in html, html[:400]
    assert LOCAL_PLOTLY_URL in html, html[:400]
    assert CDN_PLOTLY_HOST not in html, html[:400]
    # The figure's own data is inlined in the document, so a chart rendered
    # from the real rows names the fixture's regions; a placeholder does not.
    assert _has_fixture_region(html), html[:400]

    done = [event for event in events if event.get("done")][0]
    assert done.get("image_base64") is None, sorted(done)
    assert str(done.get("answer") or "").strip(), done

    # "Show data" behind the chart: the partial's chart_data_key resolves
    # through the same full_table endpoint and carries the plotted rows.
    key = charts[0].get("chart_data_key")
    assert key, sorted(charts[0])
    resolved = session.get(f"/api/chat/{chat}/full_table/{key}")
    assert resolved.status_code == 200, (resolved.status_code, resolved.text[:300])
    chart_data = resolved.json()
    assert (chart_data.get("rows") or chart_data.get("tables")), sorted(chart_data)
    assert _has_fixture_region(_labels(chart_data)), chart_data


@pytest.mark.needs_brain
def test_a_multi_plot_question_emits_several_charts(session, chat):
    events = []
    with session.stream("POST", f"/api/chat/{chat}/chat/stream",
                        json={"question": "Build me a dashboard of this data."},
                        timeout=300.0) as response:
        assert response.status_code == 200, response.read()[:300]
        for line in response.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[len("data: "):])
            except ValueError:
                continue
            events.append(event)
            if event.get("done"):
                break
    done = [event for event in events if event.get("done")]
    assert len(done) == 1, [sorted(event) for event in events]
    partials = [event for event in events if event.get("partial")]
    combined = str(done[0].get("combined_answer") or done[0].get("answer") or "")
    assert partials or combined.strip(), [sorted(event) for event in events]
    assert TRACEBACK_MARKER not in combined, combined[:400]


@pytest.mark.needs_brain
def test_an_error_then_retry_turn_never_shows_a_traceback(session, chat):
    """A question likely to fail first: the retry loop must produce prose,
    never the sandbox traceback (Article II and plain dignity)."""
    done = _stream(session, chat,
                   "Compute the correlation between the customer loyalty index "
                   "and the lunar phase column.")
    text = " ".join(str(done.get(key) or "")
                    for key in ("answer", "combined_answer", "error"))
    assert text.strip(), sorted(done)
    assert TRACEBACK_MARKER not in text, text[:400]
    assert "File \"<string>\"" not in text, text[:400]


@pytest.mark.needs_brain
def test_dashboard_create_pin_refresh_delete(session, chat):
    created = session.post("/api/dashboards", json={"name": "integration board"})
    assert created.status_code in (200, 201), (created.status_code, created.text[:300])
    dash = created.json()
    dash_id = dash.get("dash_id") or dash.get("id")
    assert dash_id, dash

    done = _stream(session, chat, TABLE_QUESTION)
    table = done.get("table")
    assert isinstance(table, dict), sorted(done)
    assert table.get("rows"), table
    pinned = session.post(f"/api/dashboards/{dash_id}/tiles",
                          json={"chat_id": chat, "kind": "table",
                                "table": table,
                                "code": done.get("code") or "",
                                "full_table_key": done.get("full_table_key")})
    assert pinned.status_code == 200, (pinned.status_code, pinned.text[:300])
    tile = pinned.json().get("tile") or pinned.json()
    tile_id = tile.get("tile_id")
    assert tile_id, pinned.json()
    assert (tile.get("snapshot") or {}).get("table"), tile

    # The tile refresh re-executes the tile's stored code in the SANDBOX
    # (routes/dashboards.py:452 -> routes.chat.run_item_refresh), so a green
    # refresh carrying the fixture's regions is end-to-end proof, not a 200.
    refreshed = session.post(
        f"/api/dashboards/{dash_id}/tiles/{tile_id}/refresh", json={})
    assert refreshed.status_code == 200, (refreshed.status_code,
                                          refreshed.text[:300])
    body = refreshed.json()
    assert body.get("ok") is True, body
    assert body.get("kind") == "table", body
    fresh_table = body.get("table") or {}
    assert fresh_table.get("rows"), body
    assert _has_fixture_region(_labels(fresh_table)), fresh_table

    deleted = session.post(f"/api/dashboards/{dash_id}/delete", json={})
    assert deleted.status_code == 200, (deleted.status_code, deleted.text[:300])
    assert deleted.json().get("deleted") is True, deleted.json()
    gone = session.get(f"/api/dashboards/{dash_id}")
    assert gone.status_code == 404, (gone.status_code, gone.text[:200])


@pytest.mark.needs_brain
def test_the_pdf_report_renders(session, chat):
    done = _stream(session, chat, "Give me the headline numbers.")
    conv_id = done.get("conv_id")
    assert conv_id, sorted(done)
    response = session.post(
        f"/api/chat/{chat}/conversation/{conv_id}/download_report", json={})
    status = response.status_code
    assert status == 200, (status, response.text[:300])
    content = response.content
    assert content.startswith(PDF_MAGIC), content[:16]


@pytest.mark.needs_brain
def test_the_pptx_report_renders(session, chat):
    done = _stream(session, chat, "Summarize the data in two sentences.")
    conv_id = done.get("conv_id")
    assert conv_id, sorted(done)
    response = session.post(
        f"/api/chat/{chat}/conversation/{conv_id}/download_pptx", json={})
    status = response.status_code
    assert status == 200, (status, response.text[:300])
    content = response.content
    assert content.startswith(PPTX_MAGIC), content[:16]
