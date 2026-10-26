"""Oracle python-oracledb THICK mode — opt-in, off by default.

Why this exists: the pinned oracledb runs in THIN mode (pure Python, no
Oracle client library). Thin mode cannot decode NCHAR / NVARCHAR2 / NCLOB
columns of a database whose national character set is not AL16UTF16 — UTF8
(id 871) answers DPY-3012 "national character set id 871 is not supported by
python-oracledb in thin mode" at the first such column. Thick mode loads
Oracle Instant Client (libclntsh) and decodes them, but it changes how the
driver is configured (connect timeout, TLS), so it must stay a deliberate
per-install choice:

- `settings.ORACLE_THICK_MODE` (default FALSE, the `ENABLE_THIRD_PARTY_SCRIPTS`
  bool idiom) is the switch. Off, nothing changes: the driver is never asked
  to load a client library and every connect argument is byte-identical to
  today's thin-mode shape.
- `db_connector.ensure_oracle_thick_mode()` decides ONCE per process.
  `oracledb.init_oracle_client()` may be called at most once in a process
  (a second call raises, and the mode cannot be switched back), so the
  decision is cached behind a lock. A failure is logged with the exception
  TYPE only — the driver's message names library paths of the host — and the
  process stays in thin mode for good, never raising (Article IV).
- `get_engine` asks for the decision only for the Oracle dialect, before it
  computes connect arguments: a Postgres or SQL Server connection must never
  trigger an Oracle client load.
- In thick mode the connect timeout and TLS ride IN THE DSN
  (`transport_connect_timeout`, `wallet_location=SYSTEM`): thick mode ignores
  the thin-only `ssl_context` / `ssl_server_dn_match` keyword arguments, so
  they are not sent at all, and the trust box has no effect there.
- The app lifespan makes the decision before the snapshot scheduler starts,
  so the first scheduled refresh already runs in the chosen mode.
- The web image ships Instant Client Basic (amd64, checksum-verified) in its
  own layer; the sandbox image gets nothing.

Fully offline: `oracledb.init_oracle_client` and `sqlalchemy.create_engine`
are replaced by recorders, so no Instant Client is ever loaded and no engine
is ever built against a real driver.
"""
import asyncio
import ast
import re
import threading
import time
from pathlib import Path

import oracledb
import pytest
import sqlalchemy
import sqlalchemy.event

import db_connector
from settings import settings

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
EXEC_DOCKERFILE = ROOT / "executor" / "Dockerfile"
EXEC_REQUIREMENTS = ROOT / "executor" / "requirements.txt"
ENV_TEMPLATE = ROOT / "client.env.example"
APP_PY = ROOT / "app.py"

APT_CACHE_CLEANUP = "rm -rf /var/lib/apt/lists/*"
IC_VERSION = "23.26.4.0.0"
IC_SHA256 = "cab59aa3b411b3b50898e456fa9aabfe48d67e25cbc1bb23303800390ce7de26"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _reset_mode():
    reset = getattr(db_connector, "_reset_oracle_mode_for_tests", None)
    if reset is not None:
        reset()


@pytest.fixture(autouse=True)
def fresh_oracle_mode():
    """Every test starts from an undecided process and leaves one behind.

    The verdict is cached per process on purpose; without the reset one
    test's decision would leak into every later test of the suite. Tolerant
    of the helper's absence so the Dockerfile pins below still report their
    own failure; `test_the_reset_helper_exists` pins the helper itself."""
    _reset_mode()
    yield
    _reset_mode()


@pytest.fixture
def init_calls(monkeypatch):
    """`oracledb.init_oracle_client` replaced by a no-op recorder — no
    Instant Client is ever loaded by this suite."""
    calls = []

    def _init(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(oracledb, "init_oracle_client", _init)
    return calls


@pytest.fixture
def engine_calls(monkeypatch):
    """`sqlalchemy.create_engine` (imported function-locally by get_engine)
    and `sqlalchemy.event.listen` replaced by recorders, so get_engine runs
    end to end without a DBAPI module."""
    calls = []

    class _FakeEngine:
        pass

    def _create_engine(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return _FakeEngine()

    monkeypatch.setattr(sqlalchemy, "create_engine", _create_engine)
    monkeypatch.setattr(sqlalchemy.event, "listen", lambda *a, **k: None)
    return calls


@pytest.fixture
def logs(monkeypatch):
    """db_connector's `log_with_sid` replaced by a recorder of
    (sid, level, message, fields)."""
    records = []

    def _log(sid, level, message, *args, **kwargs):
        records.append((sid, level, str(message), dict(kwargs)))

    monkeypatch.setattr(db_connector, "log_with_sid", _log)
    return records


def _thick_on(monkeypatch, init_calls):
    """Make thick mode ACTIVE through the public path: the setting on and the
    driver's init a recorder, then the one decision."""
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", True)
    assert db_connector.ensure_oracle_thick_mode() is True
    assert len(init_calls) == 1


def _oracle_args(cfg, timeout=8):
    return db_connector.DIALECTS["oracle"].connect_args(cfg, timeout)


def _log_text(records):
    return "\n".join(f"{lvl} {msg} {fields}" for _sid, lvl, msg, fields in records)


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------

def test_the_setting_defaults_to_false(monkeypatch):
    """Off unless an operator turns it on: thick mode changes how the driver
    connects, so no upgrade may switch an install into it silently."""
    from settings import Settings
    monkeypatch.delenv("ORACLE_THICK_MODE", raising=False)
    assert getattr(Settings(), "ORACLE_THICK_MODE", None) is False


@pytest.mark.parametrize("raw", ["true", "1", "on", "yes", " TRUE ", "On"])
def test_the_setting_reads_the_usual_truthy_spellings(monkeypatch, raw):
    """Same bool idiom as ENABLE_THIRD_PARTY_SCRIPTS (trimmed, any case)."""
    from settings import Settings
    monkeypatch.setenv("ORACLE_THICK_MODE", raw)
    assert getattr(Settings(), "ORACLE_THICK_MODE", None) is True


@pytest.mark.parametrize("raw", ["false", "", "0", "off", "no", "maybe"])
def test_the_setting_reads_anything_else_as_false(monkeypatch, raw):
    from settings import Settings
    monkeypatch.setenv("ORACLE_THICK_MODE", raw)
    assert getattr(Settings(), "ORACLE_THICK_MODE", None) is False


def test_the_reset_helper_exists():
    """The cached decision is per process; the suite needs a way to clear it
    between tests (the autouse fixture above)."""
    assert callable(getattr(db_connector, "_reset_oracle_mode_for_tests", None))


# ---------------------------------------------------------------------------
# ensure_oracle_thick_mode — setting off
# ---------------------------------------------------------------------------

def test_setting_off_never_asks_the_driver(monkeypatch, init_calls):
    """Off is today's behaviour: the driver is never told to load a client
    library, however often the decision is asked for."""
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", False)
    assert db_connector.ensure_oracle_thick_mode() is False
    assert db_connector.ensure_oracle_thick_mode() is False
    assert init_calls == []


def test_the_decision_is_made_once_even_if_the_setting_changes_later(
        monkeypatch, init_calls):
    """One decision per process: init_oracle_client can run at most once and
    the mode cannot be changed afterwards, so a later call answers the
    cached verdict instead of re-reading the setting."""
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", False)
    assert db_connector.ensure_oracle_thick_mode() is False
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", True)
    assert db_connector.ensure_oracle_thick_mode() is False
    assert init_calls == []


def test_setting_off_get_engine_for_oracle_uses_thin_args(
        monkeypatch, init_calls, engine_calls):
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", False)
    db_connector.get_engine({"db_type": "oracle", "host": "h", "port": 1521,
                             "service_name": "svc", "connect_timeout": 8}, "pw")
    assert init_calls == []
    assert len(engine_calls) == 1
    assert engine_calls[0]["kwargs"]["connect_args"] == {"tcp_connect_timeout": 8.0}


# ---------------------------------------------------------------------------
# ensure_oracle_thick_mode — setting on
# ---------------------------------------------------------------------------

def test_setting_on_initialises_the_client_exactly_once(monkeypatch, init_calls, logs):
    """A second init_oracle_client in one process raises in the driver, so
    the call happens once and later calls answer the cached verdict. The
    call carries NO argument: the library is found through the system
    loader path the image configures (ldconfig), never a path in code."""
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", True)
    for _ in range(5):
        assert db_connector.ensure_oracle_thick_mode() is True
    assert init_calls == [((), {})]
    assert "ORACLE_THICK_MODE_ENABLED" in _log_text(logs)


def test_concurrent_first_calls_initialise_once(monkeypatch):
    """Two admin requests can open Oracle connections at the same moment;
    the lock keeps them from both reaching the driver's init."""
    calls = []

    def _slow_init(*args, **kwargs):
        calls.append(1)
        time.sleep(0.05)          # widen the race window

    monkeypatch.setattr(oracledb, "init_oracle_client", _slow_init)
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", True)
    barrier = threading.Barrier(8)
    results = []
    errors = []

    def _worker():
        try:
            barrier.wait()
            results.append(db_connector.ensure_oracle_thick_mode())
        except Exception as e:    # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    assert results == [True] * 8
    assert len(calls) == 1


class DatabaseError(Exception):
    """Stands in for oracledb.DatabaseError (DPI-1047 when libclntsh is
    missing); its message names host paths."""


SECRET_PATH = "/opt/secret-vault/oracle/libclntsh.so"


def test_a_failed_init_is_logged_by_type_only_and_never_raises(monkeypatch, logs):
    """The driver's message quotes library paths of the host — the log line
    carries the exception TYPE only. The process stays thin and the failure
    is remembered: a retry would raise again in the driver anyway."""
    calls = []

    def _failing_init(*args, **kwargs):
        calls.append(1)
        raise DatabaseError(f'DPI-1047: Cannot locate a 64-bit Oracle Client '
                            f'library: "{SECRET_PATH}: cannot open shared object"')

    monkeypatch.setattr(oracledb, "init_oracle_client", _failing_init)
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", True)
    assert db_connector.ensure_oracle_thick_mode() is False
    assert db_connector.ensure_oracle_thick_mode() is False
    assert len(calls) == 1
    hits = [r for r in logs if "ORACLE_THICK_MODE_UNAVAILABLE" in r[2]]
    assert len(hits) == 1, logs
    sid, level, message, fields = hits[0]
    assert level == "error"
    assert "error=DatabaseError" in f"{message} {fields}"
    text = _log_text(logs)
    assert SECRET_PATH not in text
    assert "DPI-1047" not in text
    assert "ORACLE_THICK_MODE_ENABLED" not in text


def test_a_failed_init_leaves_get_engine_on_thin_args(monkeypatch, engine_calls, logs):
    calls = []

    def _failing_init(*args, **kwargs):
        calls.append(1)
        raise DatabaseError(SECRET_PATH)

    monkeypatch.setattr(oracledb, "init_oracle_client", _failing_init)
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", True)
    cfg = {"db_type": "oracle", "host": "h", "port": 1521,
           "service_name": "svc", "connect_timeout": 8}
    db_connector.get_engine(cfg, "pw")
    db_connector.get_engine(cfg, "pw")
    assert len(calls) == 1
    assert [c["kwargs"]["connect_args"] for c in engine_calls] == [
        {"tcp_connect_timeout": 8.0}, {"tcp_connect_timeout": 8.0}]


# ---------------------------------------------------------------------------
# get_engine — when the decision is made
# ---------------------------------------------------------------------------

def test_get_engine_initialises_before_building_the_engine(monkeypatch):
    """The mode must be fixed before the first connection: oracledb picks
    thin mode for good at its first connect if init_oracle_client has not
    run. The thick DSN carries the effective connect timeout."""
    order = []
    monkeypatch.setattr(oracledb, "init_oracle_client",
                        lambda *a, **k: order.append("init"))

    def _create_engine(url, **kwargs):
        order.append(("create_engine", kwargs.get("connect_args")))
        return object()

    monkeypatch.setattr(sqlalchemy, "create_engine", _create_engine)
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", True)
    db_connector.get_engine({"db_type": "oracle", "host": "db.corp", "port": 1522,
                             "service_name": "ORCLPDB", "connect_timeout": 5}, "pw")
    assert order == ["init", ("create_engine", {
        "tcp_connect_timeout": 5.0,
        "dsn": "db.corp:1522/ORCLPDB?transport_connect_timeout=5"})]


def test_get_engine_thick_dsn_uses_the_settings_timeout_by_default(
        monkeypatch, init_calls, engine_calls):
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", True)
    monkeypatch.setattr(settings, "DB_CONNECT_TIMEOUT", 13)
    db_connector.get_engine({"db_type": "oracle", "host": "h",
                             "service_name": "svc"}, "pw")
    assert len(init_calls) == 1
    assert engine_calls[0]["kwargs"]["connect_args"] == {
        "tcp_connect_timeout": 13.0,
        "dsn": "h:1521/svc?transport_connect_timeout=13"}


NON_ORACLE_KEYS = sorted(k for k in db_connector.DIALECTS if k != "oracle")


def test_the_non_oracle_list_covers_the_registry():
    """Guards the parametrization below against a renamed registry."""
    assert set(NON_ORACLE_KEYS) >= {"postgresql", "mysql", "mariadb", "mssql",
                                    "clickhouse", "sqlite"}


@pytest.mark.parametrize("key", NON_ORACLE_KEYS)
def test_other_dialects_never_trigger_an_oracle_client_load(
        monkeypatch, init_calls, engine_calls, key):
    """A Postgres / SQL Server / ... connection never asks for the Oracle
    decision, whatever the setting — loading libclntsh there is pure risk."""
    asked = []
    monkeypatch.setattr(db_connector, "ensure_oracle_thick_mode",
                        lambda: asked.append(1) or True)
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", True)
    cfg = {"db_type": key, "host": "h", "port": 1234, "database": "d",
           "user": "u", "connect_timeout": 8}
    if key == "sqlite":
        cfg = {"db_type": key, "database": "x.db"}
    db_connector.get_engine(cfg, "pw")
    db_connector.get_engine({**cfg, "ssl": True}, "pw")
    assert asked == []
    assert init_calls == []
    assert len(engine_calls) == 2


def test_get_engine_for_oracle_asks_for_the_decision(monkeypatch, engine_calls):
    asked = []
    monkeypatch.setattr(db_connector, "ensure_oracle_thick_mode",
                        lambda: asked.append(1) or False)
    db_connector.get_engine({"db_type": "oracle", "host": "h",
                             "service_name": "svc"}, "pw")
    assert asked == [1]
    assert len(engine_calls) == 1


# ---------------------------------------------------------------------------
# Connect arguments — thin mode is unchanged
# ---------------------------------------------------------------------------

def test_thin_args_plain_are_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", False)
    assert _oracle_args({}) == {"tcp_connect_timeout": 8.0}
    assert _oracle_args({"host": "h", "port": 2484, "service_name": "svc"}) == {
        "tcp_connect_timeout": 8.0}


def test_thin_args_ssl_and_trust_are_unchanged(monkeypatch):
    """The thin TLS shapes pinned in tests/test_db_connector_tls.py stay as
    they are while the setting is off."""
    import ssl
    monkeypatch.setattr(settings, "ORACLE_THICK_MODE", False)
    assert db_connector.ensure_oracle_thick_mode() is False
    assert _oracle_args({"ssl": True, "host": "h", "port": 2484,
                         "service_name": "svc"}) == {
        "tcp_connect_timeout": 8.0, "dsn": "tcps://h:2484/svc",
        "ssl_server_dn_match": True}
    trusted = _oracle_args({"ssl": True, "trust_server_certificate": True,
                            "host": "h", "port": 2484, "service_name": "svc"})
    assert trusted["dsn"] == "tcps://h:2484/svc"
    assert trusted["ssl_server_dn_match"] is False
    assert isinstance(trusted["ssl_context"], ssl.SSLContext)
    assert set(trusted) == {"tcp_connect_timeout", "dsn",
                            "ssl_server_dn_match", "ssl_context"}


# ---------------------------------------------------------------------------
# Connect arguments — thick mode active
# ---------------------------------------------------------------------------

def test_thick_args_plain(monkeypatch, init_calls):
    """Thick mode reads the connect bound from the DSN, not from the thin
    `tcp_connect_timeout` keyword (kept: harmless, and a thin fallback
    would still honour it)."""
    _thick_on(monkeypatch, init_calls)
    assert _oracle_args({"host": "h", "port": 2484, "service_name": "svc"}, 8) == {
        "tcp_connect_timeout": 8.0,
        "dsn": "h:2484/svc?transport_connect_timeout=8"}


def test_thick_args_use_database_when_no_service_name(monkeypatch, init_calls):
    _thick_on(monkeypatch, init_calls)
    assert _oracle_args({"host": "h", "port": 2484, "database": "orcl"}, 8)["dsn"] \
        == "h:2484/orcl?transport_connect_timeout=8"


def test_thick_args_without_a_service_carry_no_path(monkeypatch, init_calls):
    """A connection stored with neither a service name nor a database must not
    get a dangling `/` — the thin DSN SQLAlchemy builds carries no service
    either, so the thick one follows it."""
    _thick_on(monkeypatch, init_calls)
    assert _oracle_args({"host": "h", "port": 2484}, 8)["dsn"] \
        == "h:2484?transport_connect_timeout=8"
    assert _oracle_args({"host": "h", "port": 2484, "service_name": None,
                         "database": None, "ssl": True}, 8)["dsn"] \
        == "tcps://h:2484?transport_connect_timeout=8&wallet_location=SYSTEM"


def test_thick_args_default_the_port_to_1521(monkeypatch, init_calls):
    _thick_on(monkeypatch, init_calls)
    assert _oracle_args({"host": "h", "service_name": "svc"}, 8)["dsn"] \
        == "h:1521/svc?transport_connect_timeout=8"
    assert _oracle_args({"host": "h", "port": "", "service_name": "svc"}, 8)["dsn"] \
        == "h:1521/svc?transport_connect_timeout=8"


def test_thick_args_timeout_is_an_integer_in_the_dsn(monkeypatch, init_calls):
    _thick_on(monkeypatch, init_calls)
    args = _oracle_args({"host": "h", "port": 1521, "service_name": "svc"}, 30)
    assert args == {"tcp_connect_timeout": 30.0,
                    "dsn": "h:1521/svc?transport_connect_timeout=30"}


@pytest.mark.parametrize("trust", [True, False])
def test_thick_args_ssl_use_the_system_wallet_and_no_thin_tls_keywords(
        monkeypatch, init_calls, trust):
    """Thick mode does TLS through the client library: `tcps://` plus the
    system certificate store (`wallet_location=SYSTEM`). The thin-only
    `ssl_context` / `ssl_server_dn_match` keywords are not sent, and the
    trust box changes nothing — identical arguments either way."""
    _thick_on(monkeypatch, init_calls)
    args = _oracle_args({"ssl": True, "trust_server_certificate": trust,
                         "host": "h", "port": 2484, "service_name": "svc"}, 8)
    assert args == {
        "tcp_connect_timeout": 8.0,
        "dsn": "tcps://h:2484/svc?transport_connect_timeout=8&wallet_location=SYSTEM"}
    assert "ssl_context" not in args
    assert "ssl_server_dn_match" not in args


def test_thick_args_ssl_blank_port_and_database(monkeypatch, init_calls):
    _thick_on(monkeypatch, init_calls)
    assert _oracle_args({"ssl": True, "host": "h", "database": "orcl"}, 8)["dsn"] \
        == "tcps://h:1521/orcl?transport_connect_timeout=8&wallet_location=SYSTEM"


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

def _run_lifespan(monkeypatch, tmp_path, ensure, calls):
    """Enter and leave the REAL lifespan with every slow or networked step
    stubbed (DATA_ROOT is tmp_path; no scheduler thread, no sandbox call)."""
    import app as app_mod
    import db_scheduler
    import executor_client
    import plot_utils

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_mod.AuthStore, "ensure_local_admin", lambda self: None)
    monkeypatch.setattr(executor_client, "startup", lambda: None)
    monkeypatch.setattr(plot_utils, "ensure_plotly_js_asset", lambda: None)
    monkeypatch.setattr(db_connector, "ensure_oracle_thick_mode", ensure)
    monkeypatch.setattr(db_scheduler, "start", lambda: calls.append("scheduler_start"))
    monkeypatch.setattr(db_scheduler, "stop",
                        lambda *a, **k: calls.append("scheduler_stop"))
    app_logs = []
    monkeypatch.setattr(app_mod, "log_with_sid",
                        lambda sid, level, message, *a, **k:
                        app_logs.append((level, str(message))))

    async def enter():
        async with app_mod.lifespan(app_mod.app):
            calls.append("serving")

    asyncio.run(enter())
    return app_logs


def test_the_lifespan_decides_the_mode_before_the_scheduler_starts(monkeypatch, tmp_path):
    """The scheduler's first refresh may open an Oracle connection; the mode
    has to be fixed by then (oracledb locks thin mode at its first connect)."""
    calls = []
    _run_lifespan(monkeypatch, tmp_path,
                  lambda: calls.append("ensure") or False, calls)
    assert calls == ["ensure", "scheduler_start", "serving", "scheduler_stop"]


def test_a_raising_decision_does_not_stop_the_boot(monkeypatch, tmp_path):
    """ensure never raises by contract; the lifespan wraps it anyway, so a
    defect there costs Oracle thick mode, never the whole application."""
    calls = []

    def _boom():
        calls.append("ensure")
        raise RuntimeError(f"unexpected {SECRET_PATH}")

    app_logs = _run_lifespan(monkeypatch, tmp_path, _boom, calls)
    assert calls == ["ensure", "scheduler_start", "serving", "scheduler_stop"]
    failed = [m for _lvl, m in app_logs if "ORACLE_THICK_MODE_STARTUP_FAILED" in m]
    assert failed and "RuntimeError" in failed[0], app_logs
    assert all(SECRET_PATH not in m for _lvl, m in app_logs)


def test_the_lifespan_source_calls_ensure_before_the_scheduler():
    """Source-level twin of the ordering test (independent of how app.py
    imports the module)."""
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    body = ast.unparse(fn)
    assert "ensure_oracle_thick_mode(" in body
    assert body.index("ensure_oracle_thick_mode(") < body.index("db_scheduler.start(")


# ---------------------------------------------------------------------------
# Dockerfile — the Instant Client layer (web image only)
# ---------------------------------------------------------------------------

def _instructions(path: Path = None):
    """[(KEYWORD, args)] with backslash-continuations joined and comment lines
    dropped — the same reader as tests/test_container_hardening_config.py."""
    logical = []
    buf = None
    for raw in (path or DOCKERFILE).read_text(encoding="utf-8").splitlines():
        if raw.strip().startswith("#"):
            continue
        if buf is None:
            if not raw.strip():
                continue
            buf = raw.rstrip()
        else:
            buf = buf[:-1].rstrip() + " " + raw.strip()
        if buf.endswith("\\"):
            continue
        logical.append(buf)
        buf = None
    if buf is not None:
        logical.append(buf)
    out = []
    for line in logical:
        m = re.match(r"^\s*([A-Za-z]+)\s*(.*)$", line)
        if m:
            out.append((m.group(1).upper(), m.group(2).strip()))
    return out


def _oracle_layer():
    """(index, args) of the one RUN that installs Instant Client."""
    hits = [(i, args) for i, (kw, args) in enumerate(_instructions())
            if kw == "RUN" and "instantclient" in args]
    assert len(hits) == 1, f"expected one Instant Client RUN layer, found {len(hits)}"
    return hits[0]


# `unzip` in COMMAND position (after &&, ;, || or `then`) — the unpack
# itself, not the package name on an apt-get line.
_UNZIP_CMD = re.compile(r"(?:&&|;|\|\||\bthen)\s*unzip\s")


def _args_named(name: str):
    return [args for kw, args in _instructions() if kw == "ARG"
            and re.match(rf"{name}(=|\s|$)", args)]


def test_dockerfile_declares_the_instant_client_build_args():
    """Version and checksum are build args so a release can move to a newer
    Instant Client without editing the RUN; the install itself can be turned
    off (`--build-arg INSTALL_ORACLE_CLIENT=0`) like the MSSQL driver."""
    assert _args_named("INSTALL_ORACLE_CLIENT") == ["INSTALL_ORACLE_CLIENT=1"]
    assert _args_named("ORACLE_IC_VERSION") == [f"ORACLE_IC_VERSION={IC_VERSION}"]
    assert _args_named("ORACLE_IC_SHA256") == [f"ORACLE_IC_SHA256={IC_SHA256}"]
    ins = _instructions()
    idx, _ = _oracle_layer()
    arg_idx = [i for i, (kw, args) in enumerate(ins) if kw == "ARG" and
               args.split("=")[0] in ("INSTALL_ORACLE_CLIENT", "ORACLE_IC_VERSION",
                                      "ORACLE_IC_SHA256")]
    assert arg_idx and max(arg_idx) < idx


def test_the_layer_is_guarded_by_the_flag_and_amd64():
    """Instant Client for Linux x64 only: arm64 and flag-off builds skip the
    layer and the image still builds (thin mode keeps working)."""
    _, layer = _oracle_layer()
    assert re.search(r'"?\$\{?INSTALL_ORACLE_CLIENT\}?"?\s*=\s*"?1"?', layer), layer
    assert "dpkg --print-architecture" in layer, layer
    assert "amd64" in layer, layer
    assert "arm64" not in layer, layer


def test_the_layer_downloads_basic_from_oracle_and_verifies_the_checksum():
    """Basic, not Basic Light (Light lacks the character sets of older
    servers — the very case thick mode is turned on for); the checksum check
    runs before anything is unpacked."""
    _, layer = _oracle_layer()
    assert "download.oracle.com" in layer, layer
    assert re.search(r"instantclient-basic-linux\.x64-\$\{?ORACLE_IC_VERSION\}?\.zip",
                     layer), layer
    assert re.search(r"curl\s+(?:-\S+\s+)*(?:-[A-Za-z]*f|--fail\b)", layer), \
        "the download must fail the build on an HTTP error (curl -f)"
    assert "sha256sum -c" in layer, layer
    assert re.search(r"\$\{?ORACLE_IC_SHA256\}?", layer), layer
    assert "basiclite" not in DOCKERFILE.read_text(encoding="utf-8")
    unpack = _UNZIP_CMD.search(layer)
    assert unpack, layer
    assert layer.index("sha256sum -c") < unpack.start(), layer


def test_the_layer_unpacks_with_unzip_and_purges_it():
    """`unzip` keeps the libclntsh.so -> libclntsh.so.23.1 symlinks; Python's
    zipfile writes them as plain files and the loader then cannot find the
    library. The tool leaves the image in the same layer."""
    _, layer = _oracle_layer()
    assert "apt-get update" in layer, layer
    install = re.search(r"apt-get install[^&;|]*", layer)
    assert install, layer
    assert "libaio1t64" in install.group(0) and "unzip" in install.group(0), layer
    assert "zipfile" not in layer, layer
    unpack = _UNZIP_CMD.search(layer)
    assert unpack, f"no unzip command in the layer: {layer}"
    purge = re.search(r"apt-get\s+(?:-\S+\s+)*(?:purge|remove)\b[^&;|]*\bunzip\b", layer)
    assert purge, layer
    assert install.end() < unpack.start() < purge.start(), layer
    assert "libxml2" not in layer, layer


def test_the_layer_registers_the_library_and_trims_the_install():
    _, layer = _oracle_layer()
    conf = "/etc/ld.so.conf.d/oracle-instantclient.conf"
    assert conf in layer, layer
    assert "ldconfig" in layer[layer.index(conf):], layer
    assert re.search(r"rm\s+(?:-\S+\s+)*[^&;|]*\*\.jar", layer), \
        "the Java archives are not needed by python-oracledb"
    assert re.search(r"rm\s+(?:-\S+\s+)*[^&;|]*\.zip", layer), \
        "the downloaded zip must not ship in the layer"
    tail = re.sub(r"[\s;]*\bfi\s*$", "", layer).rstrip(" ;")
    assert tail.endswith(APT_CACHE_CLEANUP), layer


def test_the_layer_comes_before_the_runtime_user_and_after_the_base_apt_layer():
    ins = _instructions()
    idx, _ = _oracle_layer()
    base_apt = next(i for i, (kw, args) in enumerate(ins)
                    if kw == "RUN" and "apt-get update" in args)
    user = max(i for i, (kw, args) in enumerate(ins) if kw == "USER")
    assert base_apt < idx < user


def test_the_layer_does_not_call_pip():
    """pip is uninstalled earlier in the build; nothing below may call it."""
    _, layer = _oracle_layer()
    assert not re.search(r"\bpip\d*(\.\d+)?\s", layer), layer


def test_the_image_does_not_turn_thick_mode_on():
    """Thick mode is a runtime decision of each install (client.env), never
    baked into the image."""
    for kw, args in _instructions():
        assert "ORACLE_THICK_MODE" not in args, (kw, args)


def test_the_sandbox_image_gets_no_instant_client_and_no_oracle_driver():
    text = EXEC_DOCKERFILE.read_text(encoding="utf-8").lower()
    assert "instantclient" not in text
    assert "oracle" not in text
    reqs = [ln.split("#", 1)[0].strip().lower()
            for ln in EXEC_REQUIREMENTS.read_text(encoding="utf-8").splitlines()]
    assert not any(ln.startswith("oracledb") for ln in reqs), reqs


# ---------------------------------------------------------------------------
# Install template
# ---------------------------------------------------------------------------

def test_the_env_template_documents_the_switch():
    """Customers find the switch where they configure everything else, with
    the symptom it fixes (DPY-3012) named, commented out (off by default)."""
    text = ENV_TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r"^#\s*ORACLE_THICK_MODE=false\s*$", text, re.M), \
        "client.env.example must carry a commented ORACLE_THICK_MODE=false"
    assert not re.search(r"^ORACLE_THICK_MODE=", text, re.M)
    assert "DPY-3012" in text
