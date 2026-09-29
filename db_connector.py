"""SQLAlchemy connector for admin-registered database sources.

Dialect REGISTRY design: every DB-type-specific detail (URL drivername,
default port, SELECT-1 probe, statement-timeout mechanism, row-count / size
catalog estimates) lives in ONE `Dialect` entry. Adding a future DB type
(DB2, HANA, Snowflake, ...) is one `Dialect(...)` literal plus one pinned
driver package — nothing else changes. ClickHouse was added exactly that
way; its one wrinkle is that the native driver discards `connect_args`, so
its bounds ride in `query_args` instead.

Identifier quoting and row limiting are deliberately NOT in the registry:
every statement this module builds from introspected names is a SQLAlchemy
construct (`_select_stmt`), so the dialect itself decides quoting and
LIMIT/TOP/FETCH FIRST syntax. Hand-quoting them broke Oracle — SQLAlchemy
normalizes Oracle's folded identifiers to lowercase on the way out of the
Inspector, and a lowercase name in double quotes is a DIFFERENT object than
the uppercase one the server stores (ORA-00942).

PHYSICALLY case-sensitive names (created quoted, e.g. by a pandas `to_sql`
pipeline) are the mirror image: the Inspector returns them as
`quoted_name(name, quote=True)`, and that flag is the ONLY thing
distinguishing them from an ordinary fold-case name — the plain string is
ambiguous by itself (`prediction` may be physical lowercase or folded
UPPERCASE). `quoted_name` subclasses `str`, so the flag silently dies at
every JSON hop and every `str()` coercion; compiling the bare string then
renders unquoted, Oracle folds it back to uppercase, ORA-00904. The rule:
the flag is PERSISTED from introspection (per-column `quote: true` and
top-level `schema_quote`/`table_quote` on the registry doc, emitted only
when true) and REBUILT via `qname`/`col_ident` at every query-construction
point; the dialect decides at compile time what it means. Never inferred,
never an Oracle-only branch.

Security invariants (docs/AI_CONSTITUTION.md Article VII + DB_TABLES_PLAN):
  - This module only ever issues SELECT / introspection statements.
    `assert_read_only_query` is the one read-only gate: the connector's own
    constructs pass it through `_compiled_sql` (no CTE, a parser gap only
    warns), and free-form SELECT text is executed only by `run_live_select`
    (CTE allowed, strict parse), inside the `wrap_with_row_limit` cap.
  - Credentials arrive as function arguments, are embedded via
    `sqlalchemy.engine.URL.create` (whose str()/repr() masks the password),
    and never touch module scope, logs, or brain payloads. Driver exception
    text is scrubbed via `_scrub` before logging or returning.
  - Engines use NullPool and are disposed in `finally` — no live socket or
    credential is held between requests.

The hidden `sqlite` entry drives the OFFLINE pytest suite through the exact
same code path (URL build → engine → Inspector → chunked pd.read_sql →
ParquetWriter); it is never offered in the admin UI.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from settings import settings
from logger_utils import log_with_sid


# ---------------------------------------------------------------------------
# Dialect registry
# ---------------------------------------------------------------------------

def _no_op_timeout(conn, seconds: int) -> None:
    return None


@dataclass(frozen=True)
class Dialect:
    key: str
    label: str
    drivername: str
    driver_module: str
    default_port: Optional[int]
    needs: tuple  # extra connection fields the admin form must collect
    supports_schemas: bool
    select1_sql: str
    connect_args: Callable[[dict, int], dict] = field(default=lambda cfg, t: {})
    query_args: Callable[[dict], dict] = field(default=lambda cfg: {})
    apply_stmt_timeout: Callable = field(default=_no_op_timeout)
    row_count_sql: Optional[str] = None
    table_size_sql: Optional[str] = None
    exact_count_fallback: bool = False
    allow_url_override: bool = False
    hidden: bool = False
    # The port the server speaks WITHOUT TLS, for dialects where that differs
    # from `default_port` (ClickHouse: 9000 plaintext, 9440 TLS). A blank
    # stored port with SSL off resolves to it, so an existing row keeps the
    # port it has always reached.
    plaintext_port: Optional[int] = None
    # The admin form pre-ticks SSL for a NEW connection of this dialect.
    ssl_default: bool = False
    # sqlglot's name for this dialect — the read-only guard parses with it.
    # Not part of `list_dialects()`.
    sqlglot_dialect: Optional[str] = None

    def available(self) -> tuple[bool, Optional[str]]:
        if importlib.util.find_spec(self.driver_module) is None:
            return False, f"Python driver '{self.driver_module}' is not installed"
        if self.key == "mssql":
            try:
                import pyodbc  # noqa: PLC0415
                if "ODBC Driver 18 for SQL Server" not in pyodbc.drivers():
                    return False, "msodbcsql18 (ODBC Driver 18 for SQL Server) is not installed"
            except Exception as e:
                return False, f"pyodbc unavailable: {type(e).__name__}"
        return True, None


def _pg_connect_args(cfg: dict, timeout: int) -> dict:
    args = {"connect_timeout": timeout,
            # Session-level statement timeout (ms) as a second layer under the
            # per-snapshot SET; SELECT-only workload.
            # default_transaction_read_only makes every transaction of the
            # session read-only (defence in depth under the SELECT-only grant).
            "options": (f"-c statement_timeout={int(cfg.get('statement_timeout') or settings.DB_STATEMENT_TIMEOUT) * 1000}"
                        " -c default_transaction_read_only=on")}
    if cfg.get("ssl"):
        args["sslmode"] = "require"
    else:
        args["sslmode"] = "prefer"
    return args


def _pg_stmt_timeout(conn, seconds: int) -> None:
    from sqlalchemy import text
    conn.execute(text(f"SET statement_timeout = {int(seconds) * 1000}"))


class TLSNotNegotiated(Exception):
    """The connection was configured to require TLS but the session came up
    in plaintext."""


_TLS_NOT_NEGOTIATED_MSG = ("TLS is required for this connection but the "
                           "server did not offer it")


class ReadOnlySessionRefused(Exception):
    """The MySQL/MariaDB session could not be made read-only."""


_READ_ONLY_REFUSED_MSG = ("The database session could not be made read-only; "
                          "the connection was not used")


def _mysql_require_tls(dbapi_connection, connection_record) -> None:
    """`connect` listener for MySQL/MariaDB with SSL ticked. PyMySQL
    negotiates TLS only when the server advertises it and otherwise carries
    on in PLAINTEXT without an error, so the session is asked for its cipher
    and a plaintext one is refused. The DBAPI connection is closed BEFORE the
    raise: SQLAlchemy does not close a connection whose `connect` event
    raised. This checks that the channel is encrypted; it does not verify the
    server's identity."""
    cipher = None
    try:
        cur = dbapi_connection.cursor()
        try:
            cur.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            row = cur.fetchone()
            if row and len(row) > 1:
                cipher = row[1]
        finally:
            try:
                cur.close()
            except Exception as e:
                log_with_sid("db", "warning",
                             f"DB_TLS_PROBE_CURSOR_CLOSE_FAILED error={type(e).__name__}")
    except Exception as e:
        # The probe itself failed: the channel's state is unknown, so the
        # connection is refused like a plaintext one rather than leaked.
        log_with_sid("db", "warning",
                     f"DB_TLS_PROBE_FAILED error={type(e).__name__}")
        cipher = None
    if not cipher:
        try:
            dbapi_connection.close()
        except Exception as e:
            log_with_sid("db", "warning",
                         f"DB_TLS_PROBE_CLOSE_FAILED error={type(e).__name__}")
        raise TLSNotNegotiated(_TLS_NOT_NEGOTIATED_MSG)


def _mysql_read_only(dbapi_connection, connection_record) -> None:
    """`connect` listener for MySQL/MariaDB: every session is made read-only
    before SQLAlchemy uses it (defence in depth under the SELECT-only grant;
    the grant stays the real guarantee). A session that refuses the setting is
    closed and refused like a plaintext one — never used as it is."""
    try:
        cur = dbapi_connection.cursor()
        try:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
        finally:
            try:
                cur.close()
            except Exception as e:
                log_with_sid("db", "warning",
                             f"DB_READ_ONLY_CURSOR_CLOSE_FAILED error={type(e).__name__}")
    except Exception as e:
        errno = e.args[0] if e.args and isinstance(e.args[0], int) else None
        log_with_sid("db", "warning",
                     f"DB_READ_ONLY_SET_FAILED error={type(e).__name__} errno={errno}")
        try:
            dbapi_connection.close()
        except Exception as close_error:
            log_with_sid("db", "warning",
                         f"DB_READ_ONLY_CLOSE_FAILED error={type(close_error).__name__}")
        raise ReadOnlySessionRefused(_READ_ONLY_REFUSED_MSG) from None


def _mysql_connect_args(cfg: dict, timeout: int) -> dict:
    args = {"connect_timeout": timeout}
    if cfg.get("ssl"):
        args["ssl"] = {"ssl": True}
    return args


def _mysql_stmt_timeout(conn, seconds: int) -> None:
    from sqlalchemy import text
    # MySQL >= 5.7.8; applies to SELECT only — exactly our workload.
    conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME = {int(seconds) * 1000}"))


def _mariadb_stmt_timeout(conn, seconds: int) -> None:
    from sqlalchemy import text
    conn.execute(text(f"SET SESSION max_statement_time = {int(seconds)}"))


def _mssql_connect_args(cfg: dict, timeout: int) -> dict:
    # pyodbc's `connect(..., timeout=)` keyword is the CONNECTION timeout
    # ("managed by the driver, not all drivers support this"), NOT a query
    # bound: the real login bound is the URL's LoginTimeout
    # (`_mssql_query_args`), and the QUERY bound is the DBAPI connection's
    # `timeout` attribute, set per session by `_mssql_stmt_timeout`. This
    # keyword is kept as it always was (a value the driver may ignore).
    return {"timeout": int(cfg.get("statement_timeout") or settings.DB_STATEMENT_TIMEOUT)}


def _mssql_stmt_timeout(conn, seconds: int) -> None:
    """SQL Server's statement bound. pyodbc has no session SET for it: the
    query timeout is the DBAPI Connection's `timeout` attribute ("the
    timeout in seconds for SQL queries"), so it is set on the raw pyodbc
    connection under SQLAlchemy's pool proxy (2.x: `dbapi_connection`;
    older: the proxy itself). Measured before this existed: with the
    `connect()` keyword alone a 2 s live query ran 140 s."""
    proxied = conn.connection
    raw = getattr(proxied, "dbapi_connection", proxied)
    raw.timeout = int(seconds)


def _mssql_query_args(cfg: dict) -> dict:
    return {
        "driver": "ODBC Driver 18 for SQL Server",
        "Encrypt": "yes" if cfg.get("ssl") else "no",
        "TrustServerCertificate": "yes" if cfg.get("trust_server_certificate") else "no",
        "LoginTimeout": str(int(cfg.get("connect_timeout") or settings.DB_CONNECT_TIMEOUT)),
        # Declares a read-only workload. Enforced by an Always On
        # availability group (routes to / only admits a readable secondary);
        # advisory on a standalone instance, where the grant is the control.
        "ApplicationIntent": "ReadOnly",
    }


def _oracle_query_args(cfg: dict) -> dict:
    # Thin-mode oracledb: service name (or SID) rides in the URL query.
    if cfg.get("service_name"):
        return {"service_name": cfg["service_name"]}
    if cfg.get("database"):
        return {"service_name": cfg["database"]}
    return {}


def _oracle_connect_args(cfg: dict, timeout: int) -> dict:
    # Thin-mode oracledb: without this the connect falls back to the OS TCP
    # timeout (~127s on Linux), the one dialect that could outlast a click.
    args = {"tcp_connect_timeout": float(timeout)}
    if cfg.get("ssl"):
        # TLS: a `tcps://` DSN replaces the plain-TCP one the dialect builds
        # from the URL (create_engine applies connect_args after the
        # dialect's own). The server certificate's DN is matched unless the
        # trust box is ticked, which — like the SQL Server and ClickHouse
        # trust flags — also skips certificate verification.
        port = cfg.get("port") or 1521
        service = cfg.get("service_name") or cfg.get("database") or ""
        trust = bool(cfg.get("trust_server_certificate"))
        args["dsn"] = f"tcps://{cfg.get('host') or ''}:{port}/{service}"
        args["ssl_server_dn_match"] = not trust
        if trust:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            args["ssl_context"] = ctx
    return args


def _oracle_stmt_timeout(conn, seconds: int) -> None:
    try:
        conn.connection.dbapi_connection.call_timeout = int(seconds) * 1000
    except Exception:
        pass


def _clickhouse_query_args(cfg: dict) -> dict:
    # Every bound rides in the URL QUERY, not connect_args: the native
    # dialect's create_connect_args returns ((url_string,), {}) and
    # Connection.__init__ does `Client.from_url(args[0])`, DISCARDING **kwargs
    # — a connect_args timeout would look right and bound nothing (the Oracle
    # gap again). clickhouse-driver's parse_url types the timeouts (float) and
    # secure/verify (bool); every key it does not recognize becomes a server
    # SETTING, which is how max_execution_time gets there.
    stmt = int(cfg.get("statement_timeout") or settings.DB_STATEMENT_TIMEOUT)
    args = {
        "connect_timeout": str(int(cfg.get("connect_timeout")
                                   or settings.DB_CONNECT_TIMEOUT)),
        # The socket read bound must OUTLAST the server-side kill, or the
        # two expire together and which error surfaces is a race. With the
        # margin the admin reliably gets ClickHouse's own "Timeout exceeded"
        # rather than a bare socket timeout.
        "send_receive_timeout": str(stmt + 30),
        # This IS the statement timeout for this dialect — a session-level
        # SET cannot replace it (see the registry entry).
        "max_execution_time": str(stmt),
        # Server-side read-only mode: level 2 refuses every write and DDL but
        # still lets the session carry settings (the bounds above are sent as
        # settings with every query). 2 rather than 1 also keeps the standard
        # ClickHouse read-only login working: a profile already at readonly=2
        # refuses a client asking for readonly=1, while 2 is a no-op there.
        "readonly": "2",
    }
    if cfg.get("ssl"):
        args["secure"] = "true"
        if cfg.get("trust_server_certificate"):
            args["verify"] = "false"      # self-signed; mirrors the mssql flag
    return args


DIALECTS: dict[str, Dialect] = {d.key: d for d in [
    Dialect(
        key="postgresql", sqlglot_dialect="postgres", label="PostgreSQL",
        drivername="postgresql+psycopg2", driver_module="psycopg2",
        default_port=5432, needs=("database",),
        supports_schemas=True, select1_sql="SELECT 1",
        connect_args=_pg_connect_args, apply_stmt_timeout=_pg_stmt_timeout,
        row_count_sql=("SELECT c.reltuples::bigint FROM pg_class c "
                       "JOIN pg_namespace n ON n.oid = c.relnamespace "
                       "WHERE n.nspname = :schema AND c.relname = :table"),
        table_size_sql=("SELECT pg_total_relation_size("
                        "format('%I.%I', CAST(:schema AS text), CAST(:table AS text)))"),
    ),
    Dialect(
        key="mysql", sqlglot_dialect="mysql", label="MySQL",
        drivername="mysql+pymysql", driver_module="pymysql",
        default_port=3306, needs=("database",),
        supports_schemas=False, select1_sql="SELECT 1",
        connect_args=_mysql_connect_args, apply_stmt_timeout=_mysql_stmt_timeout,
        row_count_sql=("SELECT table_rows FROM information_schema.tables "
                       "WHERE table_schema = :schema AND table_name = :table"),
        table_size_sql=("SELECT data_length + index_length FROM information_schema.tables "
                        "WHERE table_schema = :schema AND table_name = :table"),
    ),
    Dialect(
        key="mariadb", sqlglot_dialect="mysql", label="MariaDB",
        drivername="mysql+pymysql", driver_module="pymysql",
        default_port=3306, needs=("database",),
        supports_schemas=False, select1_sql="SELECT 1",
        connect_args=_mysql_connect_args, apply_stmt_timeout=_mariadb_stmt_timeout,
        row_count_sql=("SELECT table_rows FROM information_schema.tables "
                       "WHERE table_schema = :schema AND table_name = :table"),
        table_size_sql=("SELECT data_length + index_length FROM information_schema.tables "
                        "WHERE table_schema = :schema AND table_name = :table"),
    ),
    Dialect(
        key="mssql", sqlglot_dialect="tsql", label="Microsoft SQL Server",
        drivername="mssql+pyodbc", driver_module="pyodbc",
        default_port=1433, needs=("database",),
        supports_schemas=True, select1_sql="SELECT 1",
        connect_args=_mssql_connect_args, query_args=_mssql_query_args,
        apply_stmt_timeout=_mssql_stmt_timeout,
        row_count_sql=("SELECT SUM(p.rows) FROM sys.partitions p "
                       "JOIN sys.objects o ON o.object_id = p.object_id "
                       "JOIN sys.schemas s ON s.schema_id = o.schema_id "
                       "WHERE s.name = :schema AND o.name = :table AND p.index_id IN (0, 1)"),
        table_size_sql=("SELECT SUM(au.total_pages) * 8 * 1024 FROM sys.allocation_units au "
                        "JOIN sys.partitions p ON p.partition_id = au.container_id "
                        "JOIN sys.objects o ON o.object_id = p.object_id "
                        "JOIN sys.schemas s ON s.schema_id = o.schema_id "
                        "WHERE s.name = :schema AND o.name = :table"),
    ),
    Dialect(
        key="oracle", sqlglot_dialect="oracle", label="Oracle",
        drivername="oracle+oracledb", driver_module="oracledb",
        default_port=1521, needs=("service_name",),
        supports_schemas=True, select1_sql="SELECT 1 FROM DUAL",
        connect_args=_oracle_connect_args, query_args=_oracle_query_args,
        apply_stmt_timeout=_oracle_stmt_timeout,
        row_count_sql=("SELECT num_rows FROM all_tables "
                       "WHERE owner = :schema AND table_name = :table"),
        table_size_sql=("SELECT SUM(bytes) FROM all_segments "
                        "WHERE owner = :schema AND segment_name = :table"),
    ),
    Dialect(
        key="clickhouse", sqlglot_dialect="clickhouse", label="ClickHouse",
        drivername="clickhouse+native", driver_module="clickhouse_driver",
        # A new connection is offered the TLS port with SSL ticked; 9000 is
        # the plaintext port, kept so a blank-port row with SSL off still
        # resolves to it (see build_url).
        default_port=9440, plaintext_port=9000, ssl_default=True,
        needs=("database",),
        # ClickHouse "databases" are what the SQLAlchemy dialect exposes as
        # schemas, so the schema browser lists them like any other dialect.
        supports_schemas=True, select1_sql="SELECT 1",
        # connect_args deliberately left at its default — see
        # _clickhouse_query_args for why the native driver ignores it. There
        # is likewise NO apply_stmt_timeout: a session `SET
        # max_execution_time` does not survive over the native protocol,
        # because clickhouse-driver re-sends its OWN settings with every
        # query and they win. Measured: after `SET 7`, system.settings still
        # reads the URL value. The bound is real, it just lives in the
        # connection settings instead. (SQL Server is NOT this shape: its
        # bound is the pyodbc connection's `timeout` attribute, set per
        # session by `_mssql_stmt_timeout`.)
        query_args=_clickhouse_query_args,
        row_count_sql=("SELECT total_rows FROM system.tables "
                       "WHERE database = :schema AND name = :table"),
        table_size_sql=("SELECT total_bytes FROM system.tables "
                        "WHERE database = :schema AND name = :table"),
    ),
    # Hidden test-only entry: the offline pytest suite drives the SAME code
    # path through in-process SQLite. Never offered in the admin UI.
    Dialect(
        key="sqlite", sqlglot_dialect="sqlite", label="SQLite (tests only)",
        drivername="sqlite+pysqlite", driver_module="sqlite3",
        default_port=None, needs=(),
        supports_schemas=False, select1_sql="SELECT 1",
        row_count_sql=None, table_size_sql=None,
        exact_count_fallback=True, allow_url_override=True, hidden=True,
    ),
]}


def get_dialect(db_type: str) -> Dialect:
    d = DIALECTS.get((db_type or "").strip().lower())
    if d is None:
        raise ValueError(f"Unknown database type: {db_type!r}")
    return d


def list_dialects() -> list[dict]:
    """UI-facing dialect list (hidden entries filtered)."""
    out = []
    for d in DIALECTS.values():
        if d.hidden:
            continue
        ok, reason = d.available()
        out.append({"key": d.key, "label": d.label, "default_port": d.default_port,
                    "needs": list(d.needs), "supports_schemas": d.supports_schemas,
                    "available": ok, "unavailable_reason": reason,
                    "plaintext_port": d.plaintext_port,
                    "ssl_default": d.ssl_default})
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scrub(text_val: str, *secrets_: Optional[str]) -> str:
    """Replace secret values with *** and cap length before a message may be
    logged or returned."""
    out = str(text_val or "")
    for s in secrets_:
        if s:
            out = out.replace(s, "***")
    return out[:300]


# Anchored on timeout PHRASES, not the bare word: a connect error that
# merely echoes the DSN would otherwise match (postgres embeds
# `options=-c statement_timeout=…` in every connection). Oracle's DPY-6005
# is deliberately absent — it is the generic "cannot connect" (listener
# down, refused, bad DNS), and relabelling those as a timeout would destroy
# the very diagnosability this helper exists for.
_TIMEOUT_PAT = re.compile(
    r"timed[ _-]?out"
    r"|timeout (?:expired|exceeded)"
    # SPACE-separated only: `statement_timeout=…` is a DSN parameter psycopg2
    # echoes back in unrelated connect errors, not a timeout report.
    r"|(?:login|connection|connect|statement|query|lock|read|write) timeout"
    r"|timeouterror|HYT00", re.I)


def _friendly_db_error(exc: Exception, phrase: str, *secrets_: Optional[str]) -> str:
    """One clear sentence when a driver error is timeout-shaped, so the admin
    reads "the database did not answer" instead of a 300-char driver dump.
    Each of the six drivers words it differently (psycopg2 "timeout expired",
    pyodbc HYT00, oracledb DPY-6005, clickhouse-driver SocketTimeoutError /
    "Code: 159. … Timeout exceeded"), so the whole cause chain is checked.
    Anything not timeout-shaped keeps its scrubbed driver text — no error is
    ever replaced by a guess."""
    if _is_timeout(exc):
        return phrase
    return _scrub(f"{type(exc).__name__}: {exc}", *secrets_)


def _is_timeout(exc: BaseException) -> bool:
    """True when `exc` or anything in its `__cause__`/`__context__` chain
    (at most five links, so a self-referential chain terminates) is
    timeout-shaped: a TimeoutError / socket.timeout, or a message matching
    `_TIMEOUT_PAT`. The one timeout classifier — `_friendly_db_error` and
    `count_rows` share it."""
    seen = 0
    e: Optional[BaseException] = exc
    while e is not None and seen < 5:
        if isinstance(e, (TimeoutError, socket.timeout)) or \
                _TIMEOUT_PAT.search(f"{type(e).__name__} {e}"):
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    # A driver that reports the timeout by CODE rather than by wording
    # (MySQL 3024, PostgreSQL 57014, pyodbc HYT00, ORA-01013, ClickHouse 159)
    # is a timeout for every caller too — the count verdict included.
    return _code_error_class(exc) == "timeout"


# The error classes a failed live query is reduced to before anything leaves
# this function's caller: the class (never the driver's message, which quotes
# literals and cell values) is what the retry flow and the planner see. One
# fixed sentence per class is the user-facing / brain-facing text.
# CANONICAL vocabulary — `brain_client.SQL_ERROR_CLASSES` (the wire shape's
# copy) must stay identical to it.
ERROR_CLASSES = ("syntax", "unknown_column", "unknown_table", "timeout",
                 "permission", "guard", "other")
_ERROR_CLASS_TEXT = {
    "syntax": "The query has a syntax error.",
    "unknown_column": "The query names a column that does not exist.",
    "unknown_table": "The query names a table that does not exist.",
    "timeout": "The live query timed out.",
    "permission": "The database login may not read this.",
    "other": "The database could not run the query.",
}
# Per-driver codes. psycopg2: `pgcode` (SQLSTATE); PyMySQL: errno in
# `args[0]`; pyodbc: SQLSTATE in `args[0]`; oracledb: `args[0].full_code`
# ("ORA-nnnnn"); clickhouse-driver: `ServerException.code`. SQLAlchemy wraps
# every driver exception as `DBAPIError` with the original on `.orig`.
_PG_ERROR_CLASSES = {"42601": "syntax", "42703": "unknown_column",
                     "42P01": "unknown_table", "42501": "permission",
                     "57014": "timeout"}
_MYSQL_ERROR_CLASSES = {1064: "syntax", 1054: "unknown_column",
                        1146: "unknown_table", 1142: "permission",
                        1044: "permission", 1045: "permission", 3024: "timeout"}
_ODBC_ERROR_CLASSES = {"42000": "syntax", "42S22": "unknown_column",
                       "42S02": "unknown_table", "HYT00": "timeout",
                       "28000": "permission"}
_ORACLE_ERROR_CLASSES = {"ORA-00900": "syntax", "ORA-00907": "syntax",
                         "ORA-00936": "syntax", "ORA-00904": "unknown_column",
                         "ORA-00942": "unknown_table", "ORA-01013": "timeout",
                         "ORA-01031": "permission"}
_CLICKHOUSE_ERROR_CLASSES = {62: "syntax", 47: "unknown_column",
                             60: "unknown_table", 159: "timeout",
                             497: "permission"}
# sqlite (the hidden test dialect) has no codes: its message is the signal.
_MESSAGE_ERROR_CLASSES = (("no such column", "unknown_column"),
                          ("no such table", "unknown_table"),
                          ("syntax error", "syntax"))


def error_class_text(error_class) -> str:
    """The fixed sentence for an error class (unknown classes read as
    `other`). The `guard` class carries the guard's own message instead."""
    return _ERROR_CLASS_TEXT.get(error_class) or _ERROR_CLASS_TEXT["other"]


def _classify_one(obj) -> Optional[str]:
    """The class one exception-like object declares through its driver
    code, or None when it declares nothing this helper knows."""
    pgcode = getattr(obj, "pgcode", None)
    if isinstance(pgcode, str) and pgcode in _PG_ERROR_CLASSES:
        return _PG_ERROR_CLASSES[pgcode]
    full = getattr(obj, "full_code", None)
    if isinstance(full, str) and full.upper() in _ORACLE_ERROR_CLASSES:
        return _ORACLE_ERROR_CLASSES[full.upper()]
    args = getattr(obj, "args", None)
    first = args[0] if isinstance(args, tuple) and args else None
    if first is not None and not isinstance(first, (str, int, bool)):
        full = getattr(first, "full_code", None)
        if isinstance(full, str) and full.upper() in _ORACLE_ERROR_CLASSES:
            return _ORACLE_ERROR_CLASSES[full.upper()]
    if isinstance(first, int) and not isinstance(first, bool) \
            and first in _MYSQL_ERROR_CLASSES:
        return _MYSQL_ERROR_CLASSES[first]
    if isinstance(first, str) and first.strip().upper() in _ODBC_ERROR_CLASSES:
        return _ODBC_ERROR_CLASSES[first.strip().upper()]
    code = getattr(obj, "code", None)
    if isinstance(code, int) and not isinstance(code, bool) \
            and not hasattr(obj, "full_code") and code in _CLICKHOUSE_ERROR_CLASSES:
        return _CLICKHOUSE_ERROR_CLASSES[code]
    try:
        message = str(obj).lower()
    except Exception:
        message = ""
    for needle, cls in _MESSAGE_ERROR_CLASSES:
        if needle in message:
            return cls
    return None


def _code_error_class(exc) -> Optional[str]:
    """The class the driver CODE of `exc`, of its `.orig` (SQLAlchemy's
    DBAPIError) or of anything down the `__cause__` / `__context__` chain
    (at most five links) declares; None when nothing is recognised. Never
    raises."""
    try:
        seen = 0
        e = exc
        while e is not None and seen < 5:
            orig = getattr(e, "orig", None)
            for candidate in ((orig, e) if orig is not None else (e,)):
                cls = _classify_one(candidate)
                if cls:
                    return cls
            e = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
            seen += 1
        return None
    except Exception:
        return None


def classify_db_error(exc) -> str:
    """Reduce a failed query's exception to one of `syntax`,
    `unknown_column`, `unknown_table`, `timeout`, `permission` or `other`
    (`guard` is the caller's own class for a refusal by the SQL gate).

    Timeout-shaped errors (`_is_timeout`: the wording of the whole cause
    chain, or a timeout by driver code) come first; then the driver code
    (`_code_error_class`). The two agree by construction: whatever this
    function calls `timeout`, `_is_timeout` answers True for. Never raises;
    anything unrecognised is `other`."""
    try:
        if exc is None:
            return "other"
        if isinstance(exc, BaseException) and _is_timeout(exc):
            return "timeout"
        return _code_error_class(exc) or "other"
    except Exception:
        return "other"


_FORBIDDEN_TOKENS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|"
    r"EXEC|EXECUTE|CALL|INTO|ATTACH|PRAGMA|VACUUM|COPY)\b", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    """Remove `--` line comments and `/* */` block comments (optimizer hints
    `/*+ */` included), each replaced by one space, in ONE left-to-right scan
    that copies quoted text verbatim: single-quoted strings, double-quoted,
    backtick and [bracket] identifiers (a doubled closing character is an
    escape inside them). A comment marker inside quotes is text, not a
    comment. An unterminated quote or block comment runs to the end."""
    out = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"`[":
            close = "]" if c == "[" else c
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if j + 1 < n and sql[j + 1] == close:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            out.append(" ")
            i = n if j == -1 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            out.append(" ")
            i = n if j == -1 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


_MSG_ONLY_SELECT = "Only SELECT statements are permitted."
_MSG_MULTIPLE = "Multiple SQL statements are not permitted."
_MSG_CTE = "CTEs are not permitted."
_MSG_UNPARSEABLE = "SQL could not be parsed."

# Functions no read-only query needs: sleeps and delays; file, network,
# mail, directory and shell access; Java and XML primitives that reach a URL
# or run a nested query; remote-server links; ClickHouse table functions that
# read outside the connection (object stores, HDFS, other databases, lake
# formats); advisory and named locks, sequence writers and file stat. Matched
# lowercased against a function's own name AND against every part of a dotted
# qualifier (`dbms_lock.sleep`, `utl_http.request`); a name that STARTS with
# one of `_DENIED_FUNCTION_PREFIXES` is refused too (every dblink variant,
# every ClickHouse iceberg / deltaLake / hudi table function). No prefix rule
# for `url` or `file`: ClickHouse's URLHash, URLHierarchy and
# filesystemAvailable are ordinary read-only functions.
_DENIED_FUNCTIONS = frozenset({
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "sleep", "benchmark",
    "waitfor", "xp_cmdshell", "xp_regread", "xp_dirtree", "sp_executesql",
    "openrowset", "opendatasource", "openquery", "load_file", "sys_eval",
    "sys_exec", "pg_read_file", "pg_read_binary_file", "pg_ls_dir",
    "pg_terminate_backend", "pg_cancel_backend", "lo_import", "lo_export",
    "utl_http", "utl_file", "utl_inaddr", "dbms_lock", "dbms_pipe",
    "dbms_scheduler", "dbms_sql", "dbms_xmlgen", "file", "url", "s3",
    "remote", "remotesecure", "mysql", "postgresql", "odbc", "jdbc",
    "executable", "input",
    "httpuritype", "utl_tcp", "utl_smtp", "utl_mail", "dbms_ldap",
    "dbms_java", "dbms_advisor", "dbms_xslprocessor", "extractvalue",
    "xmltype",
    "s3cluster", "hdfs", "hdfscluster", "azureblobstorage", "gcs",
    "urlcluster", "mongodb", "redis", "sqlite", "filecluster",
    "azureblobstoragecluster", "oss", "cosn", "sleepeachrow",
    "pg_advisory_lock", "pg_advisory_xact_lock", "pg_advisory_lock_shared",
    "pg_advisory_xact_lock_shared", "pg_try_advisory_lock",
    "pg_try_advisory_xact_lock", "pg_try_advisory_lock_shared",
    "pg_try_advisory_xact_lock_shared", "nextval", "setval", "pg_stat_file",
    "get_lock", "bfilename",
    "query_to_xml", "query_to_xmlschema",
    "query_to_xml_and_xmlschema", "cursor_to_xml", "cursor_to_xmlschema",
    "table_to_xml",
    # Notifications, server control, WAL, large objects, session settings,
    # named locks, audit / trace file readers, network ACLs, web-service calls.
    "pg_notify", "pg_reload_conf", "pg_switch_wal", "pg_create_restore_point",
    "lo_unlink", "set_config", "release_lock", "fn_xe_file_target_read_file",
    "fn_get_audit_file", "dbms_network_acl_admin", "utl_dbws",
    # PostgreSQL large-object readers/writers: lo_get(oid) returns an
    # object's content from outside the registered table.
    "lo_get", "loread", "lo_put",
})

_DENIED_FUNCTION_PREFIXES = ("dblink", "iceberg", "deltalake", "hudi")

# T-SQL table-hint keywords. Written without WITH, `t (NOLOCK)` parses as a
# table function and `t AS x (TABLOCKX)` as an alias column list.
_TSQL_TABLE_HINTS = frozenset({
    "NOLOCK", "READUNCOMMITTED", "READCOMMITTED", "READCOMMITTEDLOCK",
    "REPEATABLEREAD", "SERIALIZABLE", "SNAPSHOT", "READPAST", "ROWLOCK",
    "PAGLOCK", "TABLOCK", "TABLOCKX", "UPDLOCK", "XLOCK", "HOLDLOCK",
    "NOWAIT", "NOEXPAND", "FORCESEEK", "FORCESCAN", "INDEX", "KEEPIDENTITY",
    "KEEPDEFAULTS", "IGNORE_CONSTRAINTS", "IGNORE_TRIGGERS",
})

# T-SQL command words. SQL Server ends a statement at one of these without a
# semicolon, while the parser reads it as a bare alias (`SELECT 1 SHUTDOWN`).
_TSQL_COMMAND_WORDS = frozenset({
    "SHUTDOWN", "CHECKPOINT", "RECONFIGURE", "KILL", "BACKUP", "RESTORE",
    "DBCC", "WAITFOR", "EXEC", "EXECUTE", "USE", "GO", "DENY", "REVERT",
    "SETUSER", "BULK", "RAISERROR", "PRINT", "THROW", "DECLARE", "OPEN",
    "FETCH", "CLOSE", "DEALLOCATE",
})

# sqlglot node classes that are statements of their own (resolved on
# `sqlglot.exp` at call time — sqlglot is imported function-locally).
_STATEMENT_NODE_NAMES = (
    "Command", "Insert", "Update", "Delete", "Merge", "Create", "Drop",
    "Alter", "Execute", "Copy", "LoadData", "Set", "Transaction", "Pragma",
    "Commit", "Rollback", "Use", "Grant", "Revoke", "TruncateTable", "Show",
    "Describe",
)


def _registry_dialect(dialect) -> Optional[Dialect]:
    """The registry entry for a registry key, a `Dialect`, or a SQLAlchemy
    dialect object (its `.name`). None when there is none."""
    if dialect is None:
        return None
    if isinstance(dialect, Dialect):
        return dialect
    name = dialect if isinstance(dialect, str) else getattr(dialect, "name", None)
    if not isinstance(name, str):
        return None
    return DIALECTS.get(name.strip().lower())


def _sqlglot_read(dialect) -> Optional[str]:
    """sqlglot's dialect name for `dialect`; None = sqlglot's generic parser."""
    d = _registry_dialect(dialect)
    return d.sqlglot_dialect if d is not None else None


def _assert_regex_layer(sql: str, allow_cte: bool) -> None:
    """The original text checks, on the comment-stripped text: the anchor, no
    chained statements, no CTE unless allowed, no DML/DDL tokens."""
    stripped = _strip_sql_comments(sql).strip()
    if not allow_cte and re.match(r"^WITH\b", stripped, re.IGNORECASE):
        raise ValueError(_MSG_CTE)
    anchor = r"^(SELECT|WITH)\b" if allow_cte else r"^SELECT\b"
    if not re.match(anchor, stripped, re.IGNORECASE):
        raise ValueError(_MSG_ONLY_SELECT)
    if ";" in stripped.rstrip().rstrip(";"):
        raise ValueError(_MSG_MULTIPLE)
    if _FORBIDDEN_TOKENS.search(stripped):
        raise ValueError(_MSG_ONLY_SELECT)


def _function_names(node, exp) -> list:
    """Lowercased names a function node answers to: its own name plus every
    identifier of a dotted qualifier around it."""
    own = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
    names = [str(own or "").lower()]
    parent = node.parent
    if isinstance(parent, exp.Dot) and parent.expression is node:
        qualifier = parent.this
        if qualifier is not None:
            if isinstance(qualifier, exp.Identifier):
                names.append(str(qualifier.name).lower())
            for ident in qualifier.find_all(exp.Identifier):
                names.append(str(ident.name).lower())
    return names


def _is_denied_function(name: str) -> bool:
    n = str(name or "").lower()
    return n in _DENIED_FUNCTIONS or n.startswith(_DENIED_FUNCTION_PREFIXES)


def _tsql_hint_without_with(node, exp) -> bool:
    """True for a T-SQL table hint written without WITH: `t (NOLOCK)` (read
    as a table function whose arguments are bare hint keywords) or a column
    list on a plain table's alias (`t AS x (TABLOCKX)`) — on a plain table
    such a list can only be hints."""
    target = node.this
    if isinstance(target, exp.Anonymous):
        for arg in target.expressions:
            if isinstance(arg, (exp.Column, exp.Identifier)) and                     str(arg.name).upper() in _TSQL_TABLE_HINTS:
                return True
    alias = node.args.get("alias")
    if isinstance(target, exp.Identifier) and isinstance(alias, exp.TableAlias)             and alias.args.get("columns"):
        return True
    return False


def _tsql_command_alias(node, exp) -> bool:
    """True for an unquoted alias spelled like a T-SQL command word."""
    if isinstance(node, exp.Alias):
        ident = node.args.get("alias")
    elif isinstance(node, exp.TableAlias):
        ident = node.this
    else:
        return False
    return isinstance(ident, exp.Identifier) and not ident.quoted and         str(ident.name).upper() in _TSQL_COMMAND_WORDS


def _cte_aliases(root, exp) -> set:
    """Lowercased alias of every CTE in the tree (every `With`, nested ones
    included): a table reference by such a name is the CTE, not a table."""
    names = set()
    for node in root.find_all(exp.With):
        for cte in node.expressions:
            alias = getattr(cte, "alias", None)
            if alias:
                names.add(str(alias).lower())
    return names


def _assert_parse_layer(statements: list, allow_cte: bool,
                        allowed_schemas, exp, read=None,
                        allowed_tables=None) -> None:
    """Refuse whatever the parsed statement list shows to be not read-only."""
    statements = [s for s in statements if s is not None]
    if not statements:
        raise ValueError(_MSG_ONLY_SELECT)
    if len(statements) != 1:
        raise ValueError(_MSG_MULTIPLE)
    root = statements[0]
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        raise ValueError(_MSG_ONLY_SELECT)
    statement_classes = tuple(getattr(exp, n) for n in _STATEMENT_NODE_NAMES)
    if root.args.get("into") is not None:
        raise ValueError("SELECT ... INTO is not permitted.")
    if root.args.get("locks"):
        raise ValueError("Row locks are not permitted.")
    schemas = None
    if allowed_schemas:
        schemas = {str(s).strip().lower() for s in allowed_schemas}
    tables = None
    cte_names: set = set()
    if allowed_tables is not None:
        tables = {(str(s or "").strip().lower(), str(t or "").strip().lower())
                  for s, t in allowed_tables}
        cte_names = _cte_aliases(root, exp)
    tsql = read == "tsql"
    for node in root.walk():
        if isinstance(node, statement_classes):
            raise ValueError(_MSG_ONLY_SELECT)
        if isinstance(node, exp.With) and not allow_cte:
            raise ValueError(_MSG_CTE)
        if isinstance(node, exp.Into):
            raise ValueError("SELECT ... INTO is not permitted.")
        if isinstance(node, exp.Lock):
            raise ValueError("Row locks are not permitted.")
        if isinstance(node, exp.WithTableHint):
            # T-SQL table hints (TABLOCKX, HOLDLOCK, UPDLOCK, ...) take locks.
            raise ValueError("Table hints are not permitted.")
        if tsql and isinstance(node, exp.Table) and                 _tsql_hint_without_with(node, exp):
            raise ValueError("Table hints are not permitted.")
        if tsql and _tsql_command_alias(node, exp):
            raise ValueError(_MSG_ONLY_SELECT)
        if isinstance(node, (exp.Select, exp.SetOperation)) and \
                node.args.get("settings"):
            # ClickHouse query-level SETTINGS override server limits.
            raise ValueError("Query settings are not permitted.")
        if isinstance(node, exp.Func):
            for name in _function_names(node, exp):
                if _is_denied_function(name):
                    raise ValueError(f"Function '{name}' is not permitted.")
        if schemas is not None and isinstance(node, exp.Table):
            catalog = node.catalog
            if catalog:
                raise ValueError(f"Schema '{catalog}' is not permitted.")
            db = node.db
            if db and db.lower() not in schemas:
                raise ValueError(f"Schema '{db}' is not permitted.")
        if tables is not None and isinstance(node, exp.Table):
            name = str(node.name or "")
            db = str(node.db or "")
            if not db and name.lower() in cte_names:
                continue
            if (db.lower(), name.lower()) not in tables:
                raise ValueError(f"Table '{name}' is not permitted.")


def assert_read_only_query(sql: str, *, allow_cte: bool, strict_parse: bool,
                           dialect=None, allowed_schemas=None,
                           allowed_tables=None) -> None:
    """The read-only SQL gate. Raises ValueError on refusal; the message may
    name an identifier (a function, a schema), never the SQL text.

    Two independent layers, in this order:

    1. The regex layer on the comment-stripped text: starts with SELECT (or
       WITH when `allow_cte`), no chained statement, no CTE unless allowed,
       no DML/DDL token.
    2. A sqlglot parse of the ORIGINAL text under `dialect` (a registry key,
       a `Dialect` or a SQLAlchemy dialect object; unknown = the generic
       parser). Refused: anything but exactly one statement; a root that is
       not a SELECT or a set operation (UNION / INTERSECT / EXCEPT); a
       statement node anywhere in the tree; a CTE when not `allow_cte`;
       `SELECT ... INTO`; row locks and T-SQL table hints (with or without
       WITH); on T-SQL, an unquoted alias spelled like a command word
       (`SELECT 1 SHUTDOWN`); ClickHouse query-level SETTINGS; a function in
       `_DENIED_FUNCTIONS` or starting with a `_DENIED_FUNCTION_PREFIXES`
       entry; and,
       only when `allowed_schemas` is a non-empty collection, a table whose
       schema is not in it (case-insensitive) or that names a catalog.
       Without a schema list a UNION to a system catalog passes — the
       SELECT-only database login is the guarantee there.

    `allowed_tables` (None = unrestricted) is a collection of
    `(schema, table)` pairs, compared lowercased with `''` for an
    unqualified reference: every table in the tree that is not a CTE alias
    (aliases of every `With`, nested ones included) must match one pair,
    else "Table '<name>' is not permitted." The caller lists the forms it
    accepts — the registered schema, the unqualified name and the
    connection's database name (MySQL / ClickHouse registrations carry no
    schema). Case-insensitive on purpose: Oracle folds unquoted names to
    upper case, PostgreSQL to lower, and the registry stores the normalized
    name.

    `strict_parse=True` (free-form live queries) refuses text sqlglot cannot
    parse. `strict_parse=False` (the connector's own constructs) logs one
    `SQL_GUARD_PARSE_WARN` line (dialect + exception type, never the SQL) and
    leaves the verdict to the regex layer, so a parser gap on one dialect
    cannot stop a snapshot. Anything the parse DOES show refuses in both modes."""
    if not isinstance(sql, str):
        raise ValueError(_MSG_ONLY_SELECT)
    _assert_regex_layer(sql, allow_cte)
    read = _sqlglot_read(dialect)
    try:
        import sqlglot
        from sqlglot import exp
        statements = sqlglot.parse(sql, read=read)
    except Exception as e:
        if strict_parse:
            raise ValueError(_MSG_UNPARSEABLE) from None
        from exec_transport import log_safe_text
        log_with_sid("db", "warning",
                     f"SQL_GUARD_PARSE_WARN dialect={log_safe_text(read or 'generic')} "
                     f"error={log_safe_text(type(e).__name__)}")
        return
    _assert_parse_layer(statements, allow_cte, allowed_schemas, exp, read,
                        allowed_tables)


def _live_inner(sql: str) -> str:
    """The text a live query runs: comments stripped, trailing whitespace and
    semicolons removed."""
    inner = _strip_sql_comments(sql).strip()
    while inner.endswith(";"):
        inner = inner[:-1].rstrip()
    return inner


def _mssql_needs_offset(inner: str) -> bool:
    """True when the inner query (a SELECT or a set operation) has a
    top-level ORDER BY and neither TOP / LIMIT, OFFSET nor FETCH — SQL Server refuses such an ORDER BY inside a
    derived table unless `OFFSET 0 ROWS` follows it. A window's ORDER BY is
    not top-level. An unparseable inner is left as it is."""
    try:
        import sqlglot
        from sqlglot import exp
        root = sqlglot.parse_one(inner, read="tsql")
    except Exception as e:
        from exec_transport import log_safe_text
        log_with_sid("db", "warning",
                     f"LIVE_WRAP_PARSE_WARN error={log_safe_text(type(e).__name__)}")
        return False
    # A trailing ORDER BY of a set operation sits on the operation or, as
    # T-SQL parses it, on its last unparenthesized SELECT.
    node = root
    while isinstance(node, exp.SetOperation) and not node.args.get("order"):
        node = node.expression
    if not isinstance(node, (exp.Select, exp.SetOperation)) or \
            not node.args.get("order"):
        return False
    return not any(node.args.get(k) for k in ("limit", "offset", "fetch"))


_MSG_CTE_SPLIT = ("The CTE block could not be separated; write the query "
                  "without a CTE.")


def _split_leading_cte(inner: str, read) -> tuple[str, str]:
    """Split `WITH <ctes> <body>` into (`WITH <ctes> `, `<body>`), both taken
    VERBATIM from the text by token offsets — nothing is re-rendered.

    The tokenizer walks from the leading WITH (a RECURSIVE keyword rides
    with the block): per CTE the first depth-0 AS, then either its
    parenthesized body up to the matching `)` or — a ClickHouse expression
    CTE (`WITH 1 AS x SELECT`) — the bare alias name; a depth-0 `,` starts
    the next CTE and anything else starts the body. Cross-checked against a
    real parse (the CTE count, and the body parsing as a SELECT or a set
    operation); any mismatch raises ValueError(_MSG_CTE_SPLIT)."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.tokens import Tokenizer, TokenType

    toks = Tokenizer().tokenize(inner)
    if not toks or toks[0].token_type != TokenType.WITH:
        raise ValueError(_MSG_CTE_SPLIT)
    i = 1
    if i < len(toks) and toks[i].text.upper() == "RECURSIVE":
        i += 1
    n_ctes = 0
    body_start = None
    while i < len(toks):
        depth = 0
        as_idx = None
        while i < len(toks):
            tt = toks[i].token_type
            if tt == TokenType.L_PAREN:
                depth += 1
            elif tt == TokenType.R_PAREN:
                depth -= 1
            elif depth == 0 and tt == TokenType.ALIAS:
                as_idx = i
                break
            i += 1
        if as_idx is None:
            raise ValueError(_MSG_CTE_SPLIT)
        i = as_idx + 1
        depth = 0
        entered = False
        closed = False
        while i < len(toks):
            tt = toks[i].token_type
            if tt == TokenType.L_PAREN:
                depth += 1
                entered = True
            elif tt == TokenType.R_PAREN:
                depth -= 1
                if depth == 0 and entered:
                    closed = True
                    i += 1
                    break
            elif depth == 0 and not entered and \
                    tt in (TokenType.COMMA, TokenType.SELECT, TokenType.WITH):
                closed = i > as_idx + 1        # the bare alias name was read
                break
            i += 1
        if not closed:
            raise ValueError(_MSG_CTE_SPLIT)
        n_ctes += 1
        if i >= len(toks):
            raise ValueError(_MSG_CTE_SPLIT)
        if toks[i].token_type == TokenType.COMMA:
            i += 1
            continue
        body_start = toks[i].start
        break
    if body_start is None or n_ctes == 0:
        raise ValueError(_MSG_CTE_SPLIT)
    prefix = inner[:body_start].rstrip() + " "
    body = inner[body_start:].strip()
    try:
        root = sqlglot.parse_one(inner, read=read)
        with_node = root.args.get("with_") or root.args.get("with")
        parsed_ctes = len(with_node.expressions) if with_node is not None else 0
        body_root = sqlglot.parse_one(body, read=read)
    except Exception:
        raise ValueError(_MSG_CTE_SPLIT) from None
    if parsed_ctes != n_ctes or \
            not isinstance(body_root, (exp.Select, exp.SetOperation)):
        raise ValueError(_MSG_CTE_SPLIT)
    return prefix, body


def wrap_with_row_limit(sql: str, dialect, n: int) -> str:
    """Wrap a SELECT as a derived table under the dialect's outer row limit,
    so a LIMIT / TOP inside it can never exceed `n`. The inner text is kept
    VERBATIM (comments stripped, trailing `;` removed) — never re-rendered,
    which could alter dialect-specific syntax.

      postgresql / mysql / mariadb / clickhouse / sqlite:
          SELECT * FROM (<inner>) AS pdc_q LIMIT n
      mssql:  SELECT TOP n * FROM (<inner>) AS pdc_q
              (` OFFSET 0 ROWS` appended to an inner top-level ORDER BY)
      oracle: SELECT * FROM (<inner>) pdc_q FETCH FIRST n ROWS ONLY
              (no AS on the alias; 12c and later)

    A leading CTE block is HOISTED above the outer SELECT for every dialect
    (`WITH <ctes> SELECT * FROM (<body>) AS pdc_q LIMIT n`): SQL Server
    rejects a CTE inside a derived table, and the hoisted form is the same
    query everywhere. The block and the body are split by token offsets
    (`_split_leading_cte`) and kept verbatim; a split the parser does not
    confirm raises ValueError. The mssql ORDER BY rule applies to the body.

    Raises ValueError for a non-positive or non-int `n`, an unknown
    dialect, or a CTE block that could not be separated."""
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        raise ValueError("The row cap must be a positive integer.")
    d = _registry_dialect(dialect)
    if d is None:
        raise ValueError("Unknown database type for the row limit.")
    inner = _live_inner(sql if isinstance(sql, str) else "")
    prefix = ""
    if re.match(r"^WITH\b", inner, re.IGNORECASE):
        prefix, inner = _split_leading_cte(inner, d.sqlglot_dialect)
    if d.key == "mssql":
        if _mssql_needs_offset(inner):
            inner = f"{inner} OFFSET 0 ROWS"
        return f"{prefix}SELECT TOP {n} * FROM ({inner}) AS pdc_q"
    if d.key == "oracle":
        return f"{prefix}SELECT * FROM ({inner}) pdc_q FETCH FIRST {n} ROWS ONLY"
    if d.key in ("postgresql", "mysql", "mariadb", "clickhouse", "sqlite"):
        return f"{prefix}SELECT * FROM ({inner}) AS pdc_q LIMIT {n}"
    raise ValueError("Unknown database type for the row limit.")


def build_url(cfg: dict, password: str):
    """sqlalchemy.engine.URL for a connection config. ALWAYS URL.create —
    str()/repr() of the result masks the password, and escaping is handled."""
    from sqlalchemy.engine import URL, make_url
    d = get_dialect(cfg.get("db_type"))
    if d.allow_url_override and cfg.get("url_override"):
        return make_url(cfg["url_override"])
    database = cfg.get("database")
    if d.key == "oracle":
        database = None  # service_name rides in the query args
    # A blank port resolves to the dialect's plaintext port when SSL is off
    # and the dialect has one, else to its default: a stored row saved
    # without a port keeps reaching the port it always reached.
    fallback_port = (d.plaintext_port
                     if (d.plaintext_port and not cfg.get("ssl"))
                     else d.default_port)
    port = cfg.get("port") or fallback_port
    return URL.create(
        drivername=d.drivername,
        username=cfg.get("user") or None,
        password=password or None,
        host=cfg.get("host") or None,
        port=int(port) if port else None,
        database=database,
        query={k: str(v) for k, v in d.query_args(cfg).items()},
    )


def get_engine(cfg: dict, password: str, *, connect_timeout: Optional[int] = None):
    """NullPool engine — no held sockets/credentials between requests. Caller
    disposes in `finally`."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool
    d = get_dialect(cfg.get("db_type"))
    timeout = int(connect_timeout or cfg.get("connect_timeout") or settings.DB_CONNECT_TIMEOUT)
    url = build_url(cfg, password)
    kwargs: dict = {"poolclass": NullPool}
    ca = d.connect_args(cfg, timeout)
    if ca:
        kwargs["connect_args"] = ca
    engine = create_engine(url, **kwargs)
    if d.key in ("mysql", "mariadb"):
        from sqlalchemy import event
        if cfg.get("ssl"):
            # Registered FIRST: a plaintext session is refused before any
            # statement of ours is sent on it.
            event.listen(engine, "connect", _mysql_require_tls)
        event.listen(engine, "connect", _mysql_read_only)
    return engine


def _catalog_name(dialect, name: Optional[str]) -> Optional[str]:
    """Bind an introspected identifier into a catalog query the way the SERVER
    stores it. SQLAlchemy normalizes Oracle's case-folded names to lowercase on
    the way out of the Inspector, but `all_tables` / `all_segments` hold them
    UPPERCASE — binding the normalized form matches no row, so the estimate
    comes back NULL and is not even reported as degraded. Identity on every
    dialect that does not normalize (all of them but Oracle); this is what
    SQLAlchemy's own Oracle dialect does for its catalog lookups."""
    if name is None or not getattr(dialect, "requires_name_normalize", False):
        return name
    return dialect.denormalize_name(name)


def qname(name, quote=None):
    """Rebuild a persisted identifier for query construction. `quote` truthy
    marks a PHYSICALLY case-sensitive (created-quoted) name — the flag comes
    from introspection and is persisted, never inferred, because the plain
    string is ambiguous by itself. Falsy/None keeps today's behavior exactly:
    a plain str gets dialect-default quoting, an in-process `quoted_name`
    keeps its own flag."""
    if name is None or not quote:
        return name
    from sqlalchemy.sql import quoted_name
    return quoted_name(str(name), True)


def col_ident(col: dict):
    """Identifier from a stored/introspected column dict `{name, quote?}` —
    the persisted `quote` key wins; an in-process `quoted_name` value keeps
    its own flag through `qname`'s passthrough."""
    return qname(col.get("name"), col.get("quote"))


def _select_stmt(schema: Optional[str], table: str,
                 columns: Optional[list] = None, where: Optional[str] = None,
                 row_cap: Optional[int] = None):
    """The ONE SELECT builder — a SQLAlchemy construct, never a hand-quoted
    string, so the DIALECT owns both identifier quoting and the row limit.

    Quoting: an Inspector-normalized lowercase Oracle name renders UNQUOTED
    and the server folds it back to OFFERING_ALL; a mixed-case, reserved or
    otherwise case-sensitive name is still quoted, per dialect. Hand-quoting
    the normalized name was the ORA-00942 bug. Identifiers arrive as plain
    `str` OR `quoted_name` (rebuilt from the persisted flag via
    `qname`/`col_ident`) and MUST pass through un-coerced — `str()` here was
    the ORA-00904 bug: it silently stripped `quote=True` from physically
    case-sensitive columns, so they compiled unquoted and Oracle folded them
    to names that don't exist.

    Row limit: `.limit()` emits FETCH FIRST or a ROWNUM wrapper by the live
    Oracle server version, TOP on mssql, LIMIT elsewhere.

    `sqlalchemy.table()/column()` are the lightweight clause constructs — no
    MetaData, no reflection round-trip (this runs inside `fingerprint_table`,
    the cheap change-detection probe).

    `where` is the admin-authored filter and stays raw text; the compiled
    statement is what `_compiled_sql` gates."""
    from sqlalchemy import (column as sa_column, literal_column, select,
                            table as sa_table, text as sa_text)
    cols = list(columns or [])
    t = sa_table(table, *[sa_column(c) for c in cols], schema=schema or None)
    if cols:
        stmt = select(*[t.c[c] for c in cols])
    else:
        stmt = select(literal_column("*")).select_from(t)
    if where:
        stmt = stmt.where(sa_text(where))
    if row_cap:
        stmt = stmt.limit(int(row_cap))
    return stmt


def _compiled_sql(stmt, dialect) -> str:
    """Render a construct through the SELECT-only gate and return the SQL.
    Compiled against the LIVE dialect (post-connect, so Oracle's limit syntax
    matches the real server version) with literal binds, so
    `assert_read_only_query` (no CTE; a parser gap only warns) sees exactly
    what will run."""
    sql = str(stmt.compile(dialect=dialect,
                           compile_kwargs={"literal_binds": True}))
    assert_read_only_query(sql, allow_cte=False, strict_parse=False,
                           dialect=dialect)
    return sql


# ---------------------------------------------------------------------------
# test_connection / introspection / preview
# ---------------------------------------------------------------------------

def test_connection(cfg: dict, password: str, *, sid: str) -> dict:
    """SELECT-1 probe with a short connect timeout. Never raises."""
    from sqlalchemy import text
    d = None
    engine = None
    t0 = time.monotonic()
    try:
        d = get_dialect(cfg.get("db_type"))
        engine = get_engine(cfg, password)
        with engine.connect() as conn:
            conn.execute(text(d.select1_sql))
            ver = getattr(conn.dialect, "server_version_info", None)
        elapsed = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "error": None,
                "server_version": ".".join(map(str, ver)) if ver else None,
                "elapsed_ms": elapsed}
    except Exception as e:
        err = _friendly_db_error(e, "Database connection timed out.", password)
        log_with_sid(sid, "warning",
                     f"DB_TEST_FAILED type={cfg.get('db_type')} host={cfg.get('host')} err={err}")
        return {"ok": False, "error": err, "server_version": None,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def list_schemas(cfg: dict, password: str, *, sid: str) -> dict:
    from sqlalchemy import inspect as sa_inspect
    engine = None
    try:
        engine = get_engine(cfg, password)
        insp = sa_inspect(engine)
        schemas = sorted(insp.get_schema_names())
        default = getattr(insp, "default_schema_name", None) or (schemas[0] if schemas else None)
        return {"ok": True, "schemas": schemas, "default_schema": default}
    except Exception as e:
        err = _friendly_db_error(e, "Database connection timed out.", password)
        log_with_sid(sid, "warning", f"DB_LIST_SCHEMAS_FAILED err={err}")
        return {"ok": False, "error": err, "schemas": [], "default_schema": None}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def list_tables(cfg: dict, password: str, schema: Optional[str], *, sid: str) -> dict:
    from sqlalchemy import inspect as sa_inspect
    engine = None
    try:
        engine = get_engine(cfg, password)
        insp = sa_inspect(engine)
        out = [{"name": n, "kind": "table"} for n in insp.get_table_names(schema=schema)]
        try:
            out += [{"name": n, "kind": "view"} for n in insp.get_view_names(schema=schema)]
        except Exception:
            pass
        out.sort(key=lambda r: r["name"])
        return {"ok": True, "tables": out}
    except Exception as e:
        err = _friendly_db_error(e, "Database connection timed out.", password)
        log_with_sid(sid, "warning", f"DB_LIST_TABLES_FAILED schema={schema} err={err}")
        return {"ok": False, "error": err, "tables": []}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def _resolve_live_idents(insp, schema, table, *, sid: str = "db_introspect"):
    """Match the caller's plain-string schema/table against the live Inspector
    listings so they come back carrying the dialect's case-sensitivity flag
    (`quoted_name` — equality with the plain string is plain str equality).
    The live catalog is the only possible source once a name has crossed a
    JSON boundary, and a physically case-sensitive table is otherwise
    un-introspectable: the bare string gets denormalized (UPPERCASE on
    Oracle) and the Inspector finds nothing. Best-effort — on any failure
    the inputs pass through unchanged (Article IV)."""
    try:
        if schema:
            for s in insp.get_schema_names():
                if s == schema:
                    schema = s
                    break
        if table:
            names = list(insp.get_table_names(schema=schema))
            try:
                names += list(insp.get_view_names(schema=schema))
            except Exception:
                pass
            for n in names:
                if n == table:
                    table = n
                    break
    except Exception as e:
        log_with_sid(sid, "warning",
                     f"DB_IDENT_RESOLVE_FAILED table={schema}.{table} "
                     f"err={type(e).__name__}")
    return schema, table


def _column_type_info(sa_type) -> dict:
    """Transportable summary of a live SQLAlchemy column type, feeding the
    snapshot's canonical Arrow schema: {"py", "precision", "scale",
    "timezone"}. Never raises — `python_type` raises on exotic types, which
    map to py=None and fall back to chunk-1 inference downstream."""
    info: dict = {"py": None, "precision": None, "scale": None, "timezone": False}
    try:
        info["py"] = sa_type.python_type.__name__
    except Exception:
        pass
    try:
        p = getattr(sa_type, "precision", None)
        info["precision"] = int(p) if p is not None else None
    except Exception:
        pass
    try:
        s = getattr(sa_type, "scale", None)
        info["scale"] = int(s) if s is not None else None
    except Exception:
        pass
    try:
        info["timezone"] = bool(getattr(sa_type, "timezone", False))
    except Exception:
        pass
    return info


def introspect(cfg: dict, password: str, schema: Optional[str], table: str,
               *, sid: str) -> dict:
    """Columns + dtypes / PK / FK / indexes / comment via Inspector, plus
    catalog-estimate row count and size (never COUNT(*) on a customer table —
    the sqlite test entry's exact_count_fallback is the sole exception).
    Individually degraded on missing catalog privileges. Returned identifiers
    carry the case-sensitivity flag: `schema`/`table` are resolved against the
    live listings (in-process they are `quoted_name`; over JSON the flag rides
    the additive `schema_quote`/`table_quote` keys) and each column dict gains
    `quote: true` when physically case-sensitive — absent otherwise, so legacy
    consumers see byte-identical shapes."""
    from sqlalchemy import (func, inspect as sa_inspect,
                            select as sa_select, table as sa_table, text)
    d = get_dialect(cfg.get("db_type"))
    engine = None
    degraded: list[str] = []
    try:
        engine = get_engine(cfg, password)
        insp = sa_inspect(engine)
        schema, table = _resolve_live_idents(insp, schema, table, sid=sid)
        cols_raw = insp.get_columns(table, schema=schema)
        try:
            pk = insp.get_pk_constraint(table, schema=schema).get("constrained_columns") or []
        except Exception:
            pk = []
        try:
            fks = insp.get_foreign_keys(table, schema=schema) or []
        except Exception:
            fks = []
        try:
            indexes = insp.get_indexes(table, schema=schema) or []
        except Exception:
            indexes = []
        try:
            comment = (insp.get_table_comment(table, schema=schema) or {}).get("text")
        except Exception:
            comment = None
        indexed_cols = set(pk)
        for ix in indexes:
            indexed_cols.update(ix.get("column_names") or [])
        columns = []
        for c in cols_raw:
            try:
                py_type = c["type"].python_type.__name__
            except Exception:
                py_type = None
            entry = {
                "name": c.get("name"),
                "dtype": str(c.get("type")),
                "py_type": py_type,
                "type_info": _column_type_info(c.get("type")),
                "nullable": bool(c.get("nullable", True)),
                "comment": c.get("comment"),
                "pk": c.get("name") in pk,
                "indexed": c.get("name") in indexed_cols,
            }
            if getattr(c.get("name"), "quote", None):
                entry["quote"] = True
            columns.append(entry)

        row_count = None
        size_bytes = None
        with engine.connect() as conn:
            # The catalog estimates are the slow part here (Oracle's
            # all_tables/all_segments especially) and introspect runs behind
            # an admin click, so bound the statements as preview/snapshot do.
            d.apply_stmt_timeout(
                conn, int(cfg.get("statement_timeout")
                          or settings.DB_STATEMENT_TIMEOUT))
            if d.row_count_sql:
                try:
                    row_count = conn.execute(
                        text(d.row_count_sql),
                        {"schema": _catalog_name(conn.dialect, schema),
                         "table": _catalog_name(conn.dialect, table)}
                    ).scalar()
                    row_count = int(row_count) if row_count is not None and int(row_count) >= 0 else None
                except Exception:
                    degraded.append("row_count")
            elif d.exact_count_fallback:
                try:
                    stmt = sa_select(func.count()).select_from(
                        sa_table(table, schema=schema or None))
                    _compiled_sql(stmt, conn.dialect)
                    row_count = int(conn.execute(stmt).scalar() or 0)
                except Exception:
                    degraded.append("row_count")
            else:
                degraded.append("row_count")
            if d.table_size_sql:
                try:
                    size_bytes = conn.execute(
                        text(d.table_size_sql),
                        {"schema": _catalog_name(conn.dialect, schema),
                         "table": _catalog_name(conn.dialect, table)}
                    ).scalar()
                    size_bytes = int(size_bytes) if size_bytes is not None else None
                except Exception:
                    degraded.append("size")
            else:
                degraded.append("size")

        out = {"ok": True, "schema": schema, "table": table,
                "columns": columns, "primary_key": pk,
                "foreign_keys": [{
                    "constrained_columns": f.get("constrained_columns") or [],
                    "referred_schema": f.get("referred_schema"),
                    "referred_table": f.get("referred_table"),
                    "referred_columns": f.get("referred_columns") or [],
                } for f in fks],
                "indexes": [{"name": i.get("name"),
                             "columns": i.get("column_names") or [],
                             "unique": bool(i.get("unique"))} for i in indexes],
                "table_comment": comment,
                "row_count_estimate": row_count,
                "size_bytes_estimate": size_bytes,
                "degraded": degraded}
        if getattr(schema, "quote", None):
            out["schema_quote"] = True
        if getattr(table, "quote", None):
            out["table_quote"] = True
        return out
    except Exception as e:
        err = _friendly_db_error(e, "Database connection timed out.", password)
        log_with_sid(sid, "warning", f"DB_INTROSPECT_FAILED table={schema}.{table} err={err}")
        return {"ok": False, "error": err}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def preview_rows(cfg: dict, password: str, schema: Optional[str], table: str,
                 *, limit: Optional[int] = None, where: Optional[str] = None,
                 columns: Optional[list] = None, sid: str) -> dict:
    """First rows for the ladmin registration preview. Values go only to the
    admin's browser — never to the brain, never into logs.

    Pass `columns` (the introspected names) to make the SELECT explicit: the
    frame then comes back keyed by THOSE names on every dialect. Without it the
    keys are whatever the driver's cursor reports — UPPERCASE on Oracle, which
    matches neither the registry nor the AI-draft column map."""
    d = get_dialect(cfg.get("db_type"))
    engine = None
    try:
        limit = int(limit or settings.DB_PREVIEW_ROWS)
        stmt = _select_stmt(schema, table, columns=columns, where=where,
                            row_cap=limit)
        engine = get_engine(cfg, password)
        with engine.connect() as conn:
            d.apply_stmt_timeout(conn, int(cfg.get("statement_timeout") or settings.DB_STATEMENT_TIMEOUT))
            _compiled_sql(stmt, conn.dialect)
            df = pd.read_sql(stmt, conn)
        df = df.head(limit)
        raw_rows = df.astype(object).where(pd.notnull(df), None).values.tolist()
        json_rows = [[v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
                      for v in row] for row in raw_rows]
        return {"ok": True, "columns": [str(c) for c in df.columns], "rows": json_rows}
    except Exception as e:
        err = _friendly_db_error(e, "Database connection timed out.", password)
        log_with_sid(sid, "warning", f"DB_PREVIEW_FAILED table={schema}.{table} err={err}")
        return {"ok": False, "error": err, "columns": [], "rows": []}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Live-mode sizing and profiling
# ---------------------------------------------------------------------------

# The row count runs behind an interactive click (the register wizard's
# introspect step), so the connection's statement timeout is capped here.
COUNT_TIMEOUT_CAP_S = 60
# A live table is profiled from a bounded sample, never a full read.
LIVE_PROFILE_SAMPLE_ROWS = 10_000


def count_rows(cfg: dict, password: str, schema: Optional[str], table: str,
               *, where: Optional[str] = None, timeout_s: Optional[int] = None,
               sid: str) -> dict:
    """`SELECT COUNT(*)` over the table (with the admin's WHERE filter),
    built as a construct over `_select_stmt(...)` as a subquery — the
    fingerprint idiom — so the dialect quotes the identifiers and the compiled
    statement passes the SELECT-only gate.

    Bounded by the connection's statement timeout capped at
    `COUNT_TIMEOUT_CAP_S` (or the explicit `timeout_s`, whichever is lower).
    The bound goes into a COPY of the cfg the engine is built from, because
    ClickHouse (URL `max_execution_time`) takes it from the connection
    settings, not a session statement; the caller's cfg is never modified.
    Every other dialect (SQL Server included, through the pyodbc
    connection's `timeout` attribute) is bounded by `apply_stmt_timeout` on
    the session.

    Returns {ok, count, timed_out, error}. Never raises (Article IV)."""
    engine = None
    try:
        from sqlalchemy import func, select as sa_select
        d = get_dialect(cfg.get("db_type"))
        bound = min(int(cfg.get("statement_timeout") or settings.DB_STATEMENT_TIMEOUT),
                    int(timeout_s or COUNT_TIMEOUT_CAP_S))
        bound = max(1, bound)
        inner = _select_stmt(schema, table, where=where).subquery("pdc_c")
        stmt = sa_select(func.count().label("pdc_n")).select_from(inner)
        engine = get_engine({**cfg, "statement_timeout": bound}, password)
        with engine.connect() as conn:
            d.apply_stmt_timeout(conn, bound)
            _compiled_sql(stmt, conn.dialect)
            df = pd.read_sql(stmt, conn)
        count = int(df.iloc[0]["pdc_n"])
        return {"ok": True, "count": count, "timed_out": False, "error": None}
    except Exception as e:
        timed_out = _is_timeout(e)
        err = _friendly_db_error(e, "Counting the rows timed out.", password)
        from exec_transport import log_safe_text
        log_with_sid(sid, "warning",
                     f"DB_COUNT_FAILED table={log_safe_text(str(schema))}."
                     f"{log_safe_text(str(table))} timed_out={timed_out} "
                     f"err={log_safe_text(err)}")
        return {"ok": False, "count": None, "timed_out": timed_out,
                "error": err or "Row count failed."}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def sample_rows(cfg: dict, password: str, schema: Optional[str], table: str,
                *, columns: Optional[list] = None, where: Optional[str] = None,
                limit: Optional[int] = None, timeout_s: Optional[int] = None,
                log_driver_text: bool = True, sid: str) -> dict:
    """A bounded sample of the table as a raw DataFrame — the `preview_rows`
    twin used to profile a live table (no parquet is written). The dialect
    renders the row limit (`_select_stmt(row_cap=...)`). The sample runs
    behind an interactive click, so — like `count_rows` — the statement
    timeout is the connection's capped at `COUNT_TIMEOUT_CAP_S` (or the
    explicit `timeout_s`), put into a COPY of the cfg the engine is built
    from and applied to the session. Values feed only the local profile and
    technical descriptions — never the brain, never a log line.

    `log_driver_text=False` (the chat path's default live read) keeps the
    scrubbed driver text off the failure log line — type and class only,
    like `run_live_select` — because a driver quotes the value it choked on.

    Returns {ok, df, error, error_class} (`error_class` per
    `classify_db_error`, None on success). Never raises (Article IV)."""
    engine = None
    try:
        d = get_dialect(cfg.get("db_type"))
        limit = int(limit or LIVE_PROFILE_SAMPLE_ROWS)
        bound = max(1, min(int(cfg.get("statement_timeout")
                               or settings.DB_STATEMENT_TIMEOUT),
                           int(timeout_s or COUNT_TIMEOUT_CAP_S)))
        stmt = _select_stmt(schema, table, columns=columns, where=where,
                            row_cap=limit)
        engine = get_engine({**cfg, "statement_timeout": bound}, password)
        with engine.connect() as conn:
            d.apply_stmt_timeout(conn, bound)
            _compiled_sql(stmt, conn.dialect)
            df = pd.read_sql(stmt, conn)
        return {"ok": True, "df": df.head(limit), "error": None,
                "error_class": None}
    except Exception as e:
        err = _friendly_db_error(e, "Database connection timed out.", password)
        cls = classify_db_error(e)
        from exec_transport import log_safe_text
        detail = f" err={log_safe_text(err)}" if log_driver_text else ""
        log_with_sid(sid, "warning",
                     f"DB_SAMPLE_FAILED table={log_safe_text(str(schema))}."
                     f"{log_safe_text(str(table))} class={cls} "
                     f"error_type={log_safe_text(type(e).__name__)}{detail}")
        return {"ok": False, "df": None, "error": err or "Sample query failed.",
                "error_class": cls}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def _live_max_bytes() -> int:
    """The result size cap in bytes (`LIVE_RESULT_MAX_MB`, read at call time)."""
    try:
        return max(1, int(settings.LIVE_RESULT_MAX_MB)) * 1024 * 1024
    except Exception:
        return 256 * 1024 * 1024


def _frame_bytes(df: pd.DataFrame) -> int:
    try:
        return int(df.memory_usage(deep=True).sum())
    except Exception:
        return 0


def default_live_fetch(cfg: dict, password: str, doc: dict, *,
                       cap: Optional[int] = None, sid: str) -> dict:
    """The capped default read of a live table — what a question gets when
    the planner sent no SELECT for it, or when the registration carries an
    admin row filter (brain-written SQL could not honour that filter, so the
    connector's own construct is the only statement that runs). Built
    exactly like the live profile's sample: the persisted identifiers
    (`qname` / `col_ident`, case flags kept), the doc's `where_filter`, the
    dialect's own row limit at `cap + 1` so truncation is detectable, then
    trimmed to `cap`. The statement timeout is `LIVE_QUERY_TIMEOUT_S` (the
    connection's wins when lower); the in-memory size cap
    (`LIVE_RESULT_MAX_MB`) trims the frame too.

    Returns {ok, df, truncated, truncated_by, rows, error, error_class,
    elapsed_ms, timed_out} — `error` is the class sentence (never the
    driver's text), `error_class` per `classify_db_error`, `elapsed_ms` the
    wall time of the read (like `run_live_select`), `timed_out` True when
    the class is `timeout`. Never raises (Article IV)."""
    t0 = time.perf_counter()

    def _elapsed() -> int:
        return int((time.perf_counter() - t0) * 1000)

    try:
        schema = qname(doc.get("schema") or None, doc.get("schema_quote"))
        table = qname(doc.get("table_name"), doc.get("table_quote"))
        cols = [col_ident(c) for c in (doc.get("columns") or [])
                if isinstance(c, dict) and c.get("name")]
        cap = max(1, int(cap or settings.LIVE_RESULT_ROW_CAP))
        t0 = time.perf_counter()
        res = sample_rows(cfg, password, schema, table, columns=cols or None,
                          where=doc.get("where_filter") or None,
                          limit=cap + 1, timeout_s=settings.LIVE_QUERY_TIMEOUT_S,
                          log_driver_text=False, sid=sid)
        elapsed_ms = _elapsed()
        if not res.get("ok") or res.get("df") is None:
            cls = res.get("error_class") or "other"
            return {"ok": False, "df": None, "truncated": False,
                    "truncated_by": None, "rows": 0,
                    "error": error_class_text(cls), "error_class": cls,
                    "elapsed_ms": elapsed_ms, "timed_out": cls == "timeout"}
        df = res["df"]
        df.columns = [str(c) for c in df.columns]
        truncated_by = None
        if len(df) > cap:
            df = df.head(cap).reset_index(drop=True)
            truncated_by = "rows"
        max_bytes = _live_max_bytes()
        size = _frame_bytes(df)
        if size > max_bytes and len(df) > 1:
            keep = max(1, int(len(df) * max_bytes / size))
            df = df.head(keep).reset_index(drop=True)
            truncated_by = "bytes"
        return {"ok": True, "df": df, "truncated": truncated_by is not None,
                "truncated_by": truncated_by, "rows": int(len(df)),
                "error": None, "error_class": None,
                "elapsed_ms": elapsed_ms, "timed_out": False}
    except Exception as e:
        from exec_transport import log_safe_text
        log_with_sid(sid, "warning",
                     f"LIVE_DEFAULT_FETCH_FAILED error={log_safe_text(type(e).__name__)}")
        return {"ok": False, "df": None, "truncated": False, "truncated_by": None,
                "rows": 0, "error": error_class_text("other"),
                "error_class": "other", "elapsed_ms": _elapsed(),
                "timed_out": False}


# ---------------------------------------------------------------------------
# Live queries (free-form SELECT text)
# ---------------------------------------------------------------------------

# A colon SQLAlchemy's `text()` would read as a bind parameter: not preceded
# by a colon, a backslash or a word character, and followed by a word
# character. `::int` casts, `'10:30'` and an already escaped `\:` do not
# match; `':x'` does and becomes `'\:x'`, which `text()` sends as `:x`.
_BIND_COLON = re.compile(r"(?<![:\\\w]):(?=\w)")
_LIVE_READ_CHUNK_ROWS = 50_000


def run_live_select(cfg: dict, password: str, sql: str, *, dialect=None,
                    row_cap: Optional[int] = None,
                    timeout_s: Optional[int] = None,
                    allowed_schemas=None, allowed_tables=None,
                    sid: str) -> dict:
    """Run one free-form SELECT against a live table's connection.

    The text passes `assert_read_only_query` (CTE allowed, strict parse,
    `allowed_schemas` / `allowed_tables` forwarded — the table allowlist is
    how a SELECT is bound to the one table it was written for) — both as
    given and as it will run, comments stripped — then `wrap_with_row_limit`
    at `row_cap + 1` (default `settings.LIVE_RESULT_ROW_CAP`); reading stops
    at that row and `truncated` says whether it was reached
    (`truncated_by: "rows"`). The frames read so far are also weighed
    (`memory_usage(deep=True)`) against `settings.LIVE_RESULT_MAX_MB`; past
    it the last chunk is trimmed proportionally and `truncated_by` is
    `"bytes"`. The statement timeout is min(the connection's, `timeout_s` or
    `settings.LIVE_QUERY_TIMEOUT_S`), put into a COPY of the cfg the engine
    is built from and applied to the session. `dialect` defaults to the
    connection's; a different one is an error.

    Bind-parameter escaping (`_BIND_COLON`): a colon `text()` would read as
    a bind parameter — not preceded by a colon, a backslash or a word
    character and followed by a word character — becomes `\\:`, so `':x'`
    reaches the database as `:x` while `::int` and `'10:30'` stay as they
    are. Two edges of that regex, documented rather than handled: a
    backslash-colon written inside a literal (`'a\\:b'`) is left alone and
    therefore loses its backslash on the way through `text()`; and a
    colon-word followed by another colon (`':x:'`) keeps the second colon
    verbatim, since it is not followed by a word character.

    Logs carry a hash of the SQL, row counts, timings, an exception TYPE and
    the error CLASS — never the SQL text and never a driver message (both
    can quote literals).

    Returns {ok, df, truncated, truncated_by, rows, elapsed_ms, timed_out,
    error, error_class, error_detail}, plus `guard: True` when the guard
    refused the text (no engine is built then). On failure `error` is a
    FIXED sentence for the class (`classify_db_error`: syntax /
    unknown_column / unknown_table / timeout / permission / other, or the
    guard's own message for `guard`) — the value the chat flow may show and
    the retry flow may send; `error_detail` is the scrubbed driver text,
    which can quote literals and cell values and therefore stays LOCAL —
    it is never logged and must never be sent anywhere. Both are None on
    success. Never raises (Article IV)."""
    t0 = time.monotonic()
    raw = sql if isinstance(sql, str) else ""
    sql_hash = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]
    from exec_transport import log_safe_text
    log_sid = log_safe_text(str(sid))
    engine = None
    rows_iter = None

    def _elapsed() -> int:
        return int((time.monotonic() - t0) * 1000)

    def _fail(error: str, *, error_class: str, timed_out: bool = False,
              guard: bool = False, error_detail: Optional[str] = None) -> dict:
        out = {"ok": False, "df": None, "truncated": False, "truncated_by": None,
               "rows": 0, "elapsed_ms": _elapsed(), "timed_out": timed_out,
               "error": error, "error_class": error_class,
               "error_detail": error_detail}
        if guard:
            out["guard"] = True
        return out

    try:
        from sqlalchemy import text
        d = get_dialect(cfg.get("db_type"))
        if dialect is not None:
            wanted = _registry_dialect(dialect)
            if wanted is None or wanted.key != d.key:
                log_with_sid(log_sid, "warning",
                             f"LIVE_QUERY_REJECTED sql_hash={sql_hash} "
                             f"reason=dialect_mismatch")
                return _fail("The query dialect does not match the connection.",
                             error_class="other")
        cap = max(1, int(row_cap or settings.LIVE_RESULT_ROW_CAP))
        try:
            assert_read_only_query(sql, allow_cte=True, strict_parse=True,
                                   dialect=d, allowed_schemas=allowed_schemas,
                                   allowed_tables=allowed_tables)
            assert_read_only_query(_live_inner(raw), allow_cte=True,
                                   strict_parse=True, dialect=d,
                                   allowed_schemas=allowed_schemas,
                                   allowed_tables=allowed_tables)
            wrapped = wrap_with_row_limit(raw, d.key, cap + 1)
        except ValueError as ge:
            log_with_sid(log_sid, "warning",
                         f"LIVE_QUERY_REJECTED sql_hash={sql_hash} "
                         f"reason={log_safe_text(str(ge))}")
            return _fail(str(ge), error_class="guard", guard=True)
        bound = max(1, min(int(cfg.get("statement_timeout")
                               or settings.DB_STATEMENT_TIMEOUT),
                           int(timeout_s or settings.LIVE_QUERY_TIMEOUT_S)))
        clause = text(_BIND_COLON.sub(r"\\:", wrapped))
        engine = get_engine({**cfg, "statement_timeout": bound}, password)
        max_bytes = _live_max_bytes()
        frames = []
        read = 0
        size = 0
        truncated_by = None
        with engine.connect() as conn:
            d.apply_stmt_timeout(conn, bound)
            rows_iter = pd.read_sql(clause, conn,
                                    chunksize=min(cap + 1, _LIVE_READ_CHUNK_ROWS))
            for chunk in rows_iter:
                chunk.columns = [str(c) for c in chunk.columns]
                chunk_bytes = _frame_bytes(chunk)
                if size + chunk_bytes > max_bytes and len(chunk) > 0:
                    # Trim the chunk to the share of it that still fits;
                    # never an empty result when this is the first chunk.
                    room = max(0, max_bytes - size)
                    keep = int(len(chunk) * room / chunk_bytes) if chunk_bytes else 0
                    if keep <= 0 and not frames:
                        keep = 1
                    if keep > 0:
                        frames.append(chunk.head(keep))
                        read += keep
                    truncated_by = "bytes"
                    break
                frames.append(chunk)
                read += len(chunk)
                size += chunk_bytes
                if read > cap:
                    truncated_by = "rows"
                    break
            close = getattr(rows_iter, "close", None)
            rows_iter = None
            if callable(close):
                close()
        df = (pd.concat(frames, ignore_index=True) if frames
              else pd.DataFrame())
        if truncated_by == "rows" or len(df) > cap:
            df = df.head(cap).reset_index(drop=True)
            truncated_by = truncated_by or "rows"
        truncated = truncated_by is not None
        elapsed = _elapsed()
        log_with_sid(log_sid, "info",
                     f"LIVE_QUERY_OK sql_hash={sql_hash} rows={len(df)} "
                     f"truncated={truncated} truncated_by={truncated_by} "
                     f"elapsed_ms={elapsed}")
        return {"ok": True, "df": df, "truncated": truncated,
                "truncated_by": truncated_by, "rows": len(df),
                "elapsed_ms": elapsed, "timed_out": False, "error": None,
                "error_class": None, "error_detail": None}
    except Exception as e:
        cls = classify_db_error(e)
        timed_out = _is_timeout(e) or cls == "timeout"
        detail = _friendly_db_error(e, "The live query timed out.", password)
        log_with_sid(log_sid, "warning",
                     f"LIVE_QUERY_ERROR sql_hash={sql_hash} "
                     f"error_type={log_safe_text(type(e).__name__)} "
                     f"class={cls} timed_out={timed_out} elapsed_ms={_elapsed()}")
        return _fail(error_class_text(cls), error_class=cls,
                     timed_out=timed_out, error_detail=detail or None)
    finally:
        if rows_iter is not None:
            close = getattr(rows_iter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as ce:
                    log_with_sid(log_sid, "warning",
                                 f"LIVE_QUERY_READER_CLOSE_FAILED sql_hash={sql_hash} "
                                 f"error={log_safe_text(type(ce).__name__)}")
        if engine is not None:
            try:
                engine.dispose()
            except Exception as de:
                log_with_sid(log_sid, "warning",
                             f"LIVE_QUERY_DISPOSE_FAILED sql_hash={sql_hash} "
                             f"error={log_safe_text(type(de).__name__)}")


FINGERPRINT_MAX_NUMERIC = 4
FINGERPRINT_MAX_TEMPORAL = 2


def _classify_fp_column(sa_type) -> str:
    """"numeric" | "temporal" | "" from a SQLAlchemy column type. python_type
    raises on exotic types — those simply don't join the fingerprint."""
    import datetime as _dt
    import decimal as _dec
    try:
        py = sa_type.python_type
    except Exception:
        return ""
    if py is bool:
        return ""
    if py in (int, float, _dec.Decimal):
        return "numeric"
    if py in (_dt.date, _dt.datetime):
        return "temporal"
    return ""


def fingerprint_table(cfg: dict, password: str, schema: Optional[str],
                      table: str, *, where: Optional[str] = None,
                      row_cap: Optional[int] = None,
                      preferred_order: Optional[list] = None,
                      sid: str) -> dict:
    """Cheap change-detection probe (Prompt 13 Part C): ONE SQL aggregate
    query — COUNT(*), SUM+AVG of up to 4 numeric columns, MAX of up to 2
    date/timestamp columns (chosen deterministically: `preferred_order`, the
    registry column order, wins; introspection order breaks ties) — plus the
    live column name+type list from introspection. No data pull. The WHERE
    filter / row cap are applied INSIDE a subquery so a filtered table
    compares like for like with its snapshot. Values go into a hash, never
    to the brain and never into logs.

    Returns {ok, columns: [{name, dtype}], agg: {count, sums, avgs, maxes}}
    or {ok: False, error}. Callers treat any failure as "cannot skip" and
    fall through to the full snapshot (Article IV — the optimization can
    never block a refresh)."""
    from sqlalchemy import func, inspect as sa_inspect, select as sa_select
    d = get_dialect(cfg.get("db_type"))
    engine = None
    try:
        engine = get_engine(cfg, password)
        with engine.connect() as conn:
            d.apply_stmt_timeout(conn, int(cfg.get("statement_timeout")
                                           or settings.DB_STATEMENT_TIMEOUT))
            insp = sa_inspect(conn)
            cols_raw = insp.get_columns(table, schema=schema)
            # `idents` keeps the Inspector's OWN name objects (quoted_name for
            # physically case-sensitive columns) for query construction; the
            # returned `columns` entries stay plain-str `name`+`dtype` — the
            # exact pair compose_fingerprint hashes, so the additive
            # `quote`/`type_info` keys can never invalidate a stored hash.
            idents = {}
            columns = []
            for c in cols_raw:
                nm = c.get("name")
                if not nm:
                    continue
                idents[str(nm)] = nm
                entry = {"name": str(nm), "dtype": str(c.get("type")),
                         "type_info": _column_type_info(c.get("type"))}
                if getattr(nm, "quote", None):
                    entry["quote"] = True
                columns.append(entry)
            kinds = {}
            for c in cols_raw:
                if c.get("name"):
                    kinds[str(c["name"])] = _classify_fp_column(c.get("type"))
            order = {str(n): i for i, n in enumerate(preferred_order or [])}
            ranked = sorted((c["name"] for c in columns),
                            key=lambda n: (order.get(n, len(order) + 1),))
            numeric = [n for n in ranked if kinds.get(n) == "numeric"][:FINGERPRINT_MAX_NUMERIC]
            temporal = [n for n in ranked if kinds.get(n) == "temporal"][:FINGERPRINT_MAX_TEMPORAL]

            picked = numeric + temporal
            inner = _select_stmt(schema, table,
                                 columns=[idents[n] for n in picked] or None,
                                 where=where, row_cap=row_cap).subquery("fp_sub")
            parts = [func.count().label("fp_count")]
            for i, n in enumerate(numeric):
                parts.append(func.sum(inner.c[n]).label(f"fp_s{i}"))
                parts.append(func.avg(inner.c[n]).label(f"fp_a{i}"))
            for i, n in enumerate(temporal):
                parts.append(func.max(inner.c[n]).label(f"fp_m{i}"))
            # Executed as a CONSTRUCT, not a string: an unquoted `fp_count`
            # alias comes back as FP_COUNT from Oracle's cursor, and only
            # SQLAlchemy's result mapping puts our own label back on it.
            stmt = sa_select(*parts).select_from(inner)
            _compiled_sql(stmt, conn.dialect)
            df = pd.read_sql(stmt, conn)
        row = df.iloc[0]

        def _s(v):
            return None if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v) else str(v)

        agg = {"count": int(row["fp_count"]),
               "sums": {n: _s(row[f"fp_s{i}"]) for i, n in enumerate(numeric)},
               "avgs": {n: _s(row[f"fp_a{i}"]) for i, n in enumerate(numeric)},
               "maxes": {n: _s(row[f"fp_m{i}"]) for i, n in enumerate(temporal)}}
        return {"ok": True, "columns": columns, "agg": agg}
    except Exception as e:
        err = _friendly_db_error(e, "Database connection timed out.", password)
        log_with_sid(sid, "warning",
                     f"DB_FINGERPRINT_FAILED table={schema}.{table} err={err}")
        return {"ok": False, "error": err}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

_CATEGORY_MAX_UNIQUE = 100_000
_CATEGORY_MAX_RATIO = 0.5

_INT_DOWNCASTS = {"int8", "int16", "int32"}
_FLOAT_DOWNCASTS = {"float32"}


class _SnapshotCastError(Exception):
    """A chunk's values cannot be represented in the snapshot's canonical
    Arrow schema without data loss (overflow / incompatible values)."""

    def __init__(self, col: str, target, cause: Exception):
        self.col = col
        self.target = target
        super().__init__(
            f"Column '{col}' cannot be converted to {target} without data "
            f"loss; snapshot aborted, previous snapshot kept.")


def _normalize_arrow_type(t):
    """Chunk-1-inference fallback normalization: strip the artifacts that vary
    per chunk. `null` (an all-NULL column) -> string; any timestamp -> the
    canonical microsecond resolution (tz kept); dictionary -> its normalized
    value type (categorical parquet is forbidden downstream)."""
    import pyarrow as pa
    if pa.types.is_null(t):
        return pa.string()
    if pa.types.is_timestamp(t):
        return pa.timestamp("us", tz=t.tz)
    if pa.types.is_dictionary(t):
        return _normalize_arrow_type(t.value_type)
    return t


def _base_arrow_type(info: Optional[dict], chunk_dtype):
    """Canonical Arrow type for one column from its introspected `type_info`,
    or None to fall back to normalized chunk-1 inference. The introspected
    LOGICAL type decides — deterministic across runs, so NULL distribution
    and chunk order can never flip it; the chunk-1 pandas DTYPE (never its
    values) refines only the driver-representation cases: bool-as-int,
    Decimal-vs-float, timestamp tz. Timestamps are `us`, not `ns` — year-9999
    sentinel dates (common in bank DBs) overflow ns."""
    import pyarrow as pa
    py = (info or {}).get("py")
    if py == "int":
        return pa.int64()
    if py == "float":
        return pa.float64()
    if py == "bool":
        # MySQL tinyint(1) introspects as bool but the driver may hand back
        # ints beyond 0/1 — keep those int64, no false cast failures.
        if chunk_dtype is not None and pd.api.types.is_integer_dtype(chunk_dtype):
            return pa.int64()
        return pa.bool_()
    if py == "str":
        return pa.string()
    if py == "bytes":
        return pa.binary()
    if py == "datetime":
        tz = None
        if isinstance(chunk_dtype, pd.DatetimeTZDtype):
            tz = str(chunk_dtype.tz)
        elif chunk_dtype is not None and pd.api.types.is_datetime64_any_dtype(chunk_dtype):
            tz = None
        elif (info or {}).get("timezone"):
            tz = "UTC"
        return pa.timestamp("us", tz=tz)
    if py == "date":
        return pa.date32()
    if py == "time":
        return pa.time64("us")
    if py == "timedelta":
        return pa.duration("us")
    if py == "Decimal":
        if chunk_dtype is not None and pd.api.types.is_float_dtype(chunk_dtype):
            return pa.float64()  # the driver already hands back floats (oracledb)
        prec = (info or {}).get("precision")
        scale = (info or {}).get("scale")
        try:
            if prec and 1 <= int(prec) <= 38 and scale is not None and 0 <= int(scale) <= int(prec):
                return pa.decimal128(int(prec), int(scale))
        except Exception:
            pass
        # Bare NUMBER without precision: float64 via the pandas precast —
        # per-chunk decimal precision inference was this bug's third face.
        return pa.float64()
    return None


def _canonical_base_schema(names: list, column_types: Optional[dict],
                           chunk: Optional[pd.DataFrame], sid: str):
    """ONE Arrow schema per snapshot, fields in SELECT order. Introspection-
    driven where the type maps (`_base_arrow_type`); normalized chunk-1
    inference for exotics; `string` for a column that is both
    un-introspectable AND all-NULL in chunk 1 (the doubly-unknown case —
    logged, later real values get stringified rather than failing)."""
    import pyarrow as pa
    inferred = None
    if chunk is not None:
        try:
            inferred = pa.Schema.from_pandas(chunk, preserve_index=False)
        except Exception as e:
            # Type name only — from_pandas error text can embed cell values.
            log_with_sid(sid, "warning",
                         f"DB_SNAPSHOT_INFER_FAILED err={type(e).__name__}")
    fields = []
    for name in names:
        info = None
        if column_types:
            info = column_types.get(name)
            if info is None:
                # Oracle's cursor may case-fold the frame's keys — match the
                # introspected map case-insensitively before giving up.
                low = str(name).lower()
                for k, v in column_types.items():
                    if str(k).lower() == low:
                        info = v
                        break
        chunk_dtype = None
        if chunk is not None and name in chunk.columns:
            chunk_dtype = chunk[name].dtype
        t = _base_arrow_type(info, chunk_dtype)
        if t is None:
            if inferred is not None and inferred.get_field_index(name) >= 0:
                t = _normalize_arrow_type(inferred.field(name).type)
            else:
                t = pa.string()
                log_with_sid(sid, "warning",
                             f"DB_SNAPSHOT_TYPE_UNKNOWN col={name} -> string")
        fields.append(pa.field(name, t))
    return pa.schema(fields)


def _compute_dtype_plan(chunk: pd.DataFrame, base) -> dict:
    """From the FIRST chunk, record 'category' markers for low-cardinality
    string columns. Category entries are RECORDED ONLY — neither baked into
    the file (per-chunk categoricals destabilize the Arrow schema) nor
    applied by the loader: categorical frames make generated groupby code
    (pandas < 3.0 observed=False default) emit the full cartesian product of
    ALL categories, which once put every city/product on a chart axis.

    Value-derived NUMERIC downcasts are deliberately no longer recorded: a
    plan computed from whichever values chunk 1 happened to hold made the
    parquet schema depend on chunk size and NULL distribution — the schema
    must be a function of the TABLE DEFINITION alone, so refresh comparison
    and profiles stay stable across runs. Numeric entries in a STORED legacy
    plan are still honored by `_refine_schema_with_plan` (same-family), so
    existing snapshots keep their schema."""
    import pyarrow as pa
    plan: dict[str, str] = {}
    n = max(1, len(chunk))
    for f in base:
        if f.name not in chunk.columns:
            continue
        s = chunk[f.name]
        try:
            if pa.types.is_string(f.type) and (
                    pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)):
                nunique = s.nunique(dropna=True)
                # Ratio guards big chunks; the 1000-floor keeps the plan sane
                # when chunk 1 is tiny (category on a small table is harmless).
                if nunique <= _CATEGORY_MAX_UNIQUE and nunique <= max(1000, n * _CATEGORY_MAX_RATIO):
                    plan[f.name] = "category"
        except Exception:
            continue
    return plan


def _refine_schema_with_plan(base, plan: dict, sid: str):
    """Fold the numeric downcast plan into the canonical schema — the cast
    then happens inside the single from_pandas conversion (the per-chunk
    pandas astype with its later-chunk 'relax' path was one of the two
    schema-mismatch root causes). Same-family entries only; a stale
    mismatched entry (e.g. a float32 recorded for an int column by an older
    build) is pruned from the plan IN PLACE — the caller persists the cleaned
    plan, so the registry self-heals. Category entries stay recorded-only:
    the schema keeps plain string."""
    import pyarrow as pa
    fields = []
    for f in base:
        dtype = plan.get(f.name)
        if dtype and dtype != "category":
            if ((dtype in _INT_DOWNCASTS and pa.types.is_integer(f.type)) or
                    (dtype in _FLOAT_DOWNCASTS and pa.types.is_floating(f.type))):
                f = pa.field(f.name, pa.type_for_alias(dtype))
            else:
                log_with_sid(sid, "warning", f"DB_SNAPSHOT_DTYPE_RELAXED col={f.name}")
                plan.pop(f.name, None)
        fields.append(f)
    return pa.schema(fields)


def _precast_chunk(chunk: pd.DataFrame, schema, *, warned: set, sid: str) -> pd.DataFrame:
    """Pandas-side preparation for the canonical-schema conversion — the only
    two casts Arrow refuses to do itself. (1) Sub-microsecond timestamps are
    TRUNCATED to the canonical `us` resolution — deliberate: hard-failing
    would brick Oracle TIMESTAMP(9) tables, and `us` (not `ns`) is canonical
    because year-9999 sentinel dates overflow ns. Logged once per column.
    (2) Object columns of Decimals headed for a float64 field are cast via
    pandas (Arrow refuses Decimal->double); a failing precast is left for
    the Arrow conversion to reject WITH the column named."""
    import pyarrow as pa
    for f in schema:
        if f.name not in chunk.columns:
            continue
        s = chunk[f.name]
        try:
            if pa.types.is_timestamp(f.type) and pd.api.types.is_datetime64_any_dtype(s):
                if getattr(s.dt, "unit", "ns") == "ns":
                    floored = s.dt.floor("us")
                    if f.name not in warned and not floored.equals(s):
                        warned.add(f.name)
                        log_with_sid(sid, "warning",
                                     f"DB_SNAPSHOT_TS_TRUNCATED col={f.name} ns->us")
                    chunk[f.name] = floored
            elif pa.types.is_floating(f.type) and pd.api.types.is_object_dtype(s):
                chunk[f.name] = s.astype("float64")
        except Exception:
            continue
    return chunk


_CAST_COL_RE = re.compile(r"Conversion failed for column (.+?) with type")


def _chunk_to_arrow(chunk: pd.DataFrame, schema, *, warned: set, sid: str):
    """Convert one chunk against the canonical schema. from_pandas with an
    explicit target schema is the mechanism on purpose: it treats NaN as null
    for int targets and raises (naming the column) on genuinely lossy casts —
    `.cast()` would reject NaN outright."""
    import pyarrow as pa
    chunk = _precast_chunk(chunk, schema, warned=warned, sid=sid)
    try:
        return pa.Table.from_pandas(chunk, schema=schema, preserve_index=False)
    except Exception as e:
        m = _CAST_COL_RE.search(str(e))
        col = m.group(1).strip() if m else None
        if col is None:
            # Arrow didn't name the column — probe one field at a time.
            for f in schema:
                if f.name in chunk.columns:
                    try:
                        pa.Table.from_pandas(chunk[[f.name]],
                                             schema=pa.schema([f]),
                                             preserve_index=False)
                    except Exception:
                        col = f.name
                        break
        col = col or "?"
        try:
            target = schema.field(col).type
        except Exception:
            target = "?"
        raise _SnapshotCastError(col, target, e) from e


def snapshot_table(cfg: dict, password: str, *, schema: Optional[str], table: str,
                   columns: Optional[list] = None, where: Optional[str] = None,
                   row_cap: Optional[int] = None, dtype_plan: Optional[dict] = None,
                   column_types: Optional[dict] = None,
                   dest: Path, sid: str, chunk_rows: Optional[int] = None) -> dict:
    """Chunked SELECT → parquet with an atomic os.replace. Never raises; on
    failure the tmp file is removed and the PREVIOUS snapshot (if any) stays
    in place, so chats keep serving the last good data (Article IV).

    ONE canonical Arrow schema per snapshot (`_canonical_base_schema` from
    the introspected `column_types` — pass `{name: type_info}` — refined by
    the numeric downcast plan), and EVERY chunk is converted against it.
    Pinning the writer to chunk-1 pandas inference was the production schema-
    mismatch bug: pandas re-infers per chunk, so an all-NULL leading column,
    an int growing NULLs, or a timestamp resolution flip made a later
    `write_table` raise. Chunk order / NULL distribution can no longer change
    the resulting schema; a genuinely lossy cast (overflow, incompatible
    values) fails the snapshot naming the column instead of writing wrong
    values, and prunes a stale downcast from the returned plan so the next
    run self-heals."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    d = get_dialect(cfg.get("db_type"))
    engine = None
    writer = None
    tmp = dest.with_name(dest.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    t0 = time.monotonic()
    rows_total = 0
    out_columns: list[str] = []
    plan = dict(dtype_plan or {})
    try:
        stmt = _select_stmt(schema, table, columns=columns, where=where,
                            row_cap=row_cap)
        chunk_size = int(chunk_rows or settings.DB_SNAPSHOT_CHUNK_ROWS)
        engine = get_engine(cfg, password)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with engine.connect() as conn:
            d.apply_stmt_timeout(conn, int(cfg.get("statement_timeout") or settings.DB_STATEMENT_TIMEOUT))
            _compiled_sql(stmt, conn.dialect)
            first = True
            canonical = None
            warned: set = set()
            for chunk in pd.read_sql(stmt, conn, chunksize=chunk_size):
                chunk.columns = [str(c) for c in chunk.columns]
                if first:
                    out_columns = [str(c) for c in chunk.columns]
                    base = _canonical_base_schema(out_columns, column_types,
                                                  chunk, sid)
                    if not plan:
                        plan = _compute_dtype_plan(chunk, base)
                    canonical = _refine_schema_with_plan(base, plan, sid)
                    # Chunk-1 conversion with repair: a mapping miss on an
                    # exotic driver type must never brick a table inference
                    # handled yesterday — swap the field for chunk-1's own
                    # normalized inference, once per column (a field already
                    # at its inferred type raises, bounding the loop).
                    while True:
                        try:
                            arrow = _chunk_to_arrow(chunk, canonical,
                                                    warned=warned, sid=sid)
                            break
                        except _SnapshotCastError as ce:
                            fixed = None
                            if ce.col in chunk.columns:
                                try:
                                    fixed = _normalize_arrow_type(
                                        pa.Schema.from_pandas(
                                            chunk[[ce.col]],
                                            preserve_index=False).field(ce.col).type)
                                except Exception:
                                    fixed = None
                            if fixed is None or fixed == ce.target:
                                log_with_sid(sid, "error",
                                             f"DB_SNAPSHOT_CAST_FAILED table={schema}.{table} "
                                             f"col={ce.col} target={ce.target}")
                                raise
                            log_with_sid(sid, "warning",
                                         f"DB_SNAPSHOT_TYPE_FALLBACK table={schema}.{table} "
                                         f"col={ce.col} from={ce.target} to={fixed}")
                            plan.pop(ce.col, None)
                            canonical = pa.schema(
                                [pa.field(f.name, fixed) if f.name == ce.col else f
                                 for f in canonical])
                    writer = pq.ParquetWriter(str(tmp), canonical,
                                              compression="snappy", use_dictionary=True)
                    first = False
                else:
                    try:
                        arrow = _chunk_to_arrow(chunk, canonical,
                                                warned=warned, sid=sid)
                    except _SnapshotCastError as ce:
                        # A planned downcast a later chunk outgrew: prune it
                        # so the persisted plan lets the NEXT run succeed.
                        plan.pop(ce.col, None)
                        log_with_sid(sid, "error",
                                     f"DB_SNAPSHOT_CAST_FAILED table={schema}.{table} "
                                     f"col={ce.col} target={ce.target}")
                        raise
                writer.write_table(arrow)
                rows_total += len(chunk)
            if first:
                # Zero rows — a TYPED empty parquet from the canonical schema
                # (introspected types when known, string otherwise), never
                # arrow-null columns.
                out_columns = [str(c) for c in (columns or [])]
                base = _canonical_base_schema(out_columns, column_types, None, sid)
                canonical = _refine_schema_with_plan(base, plan, sid)
                writer = pq.ParquetWriter(str(tmp), canonical,
                                          compression="snappy", use_dictionary=True)
                writer.write_table(canonical.empty_table())
        writer.close()
        writer = None
        os.replace(tmp, dest)
        size = dest.stat().st_size
        elapsed = round(time.monotonic() - t0, 2)
        log_with_sid(sid, "info",
                     f"DB_SNAPSHOT_OK table={schema}.{table} rows={rows_total} bytes={size} elapsed_s={elapsed}")
        return {"ok": True, "rows": rows_total, "bytes": size, "elapsed_s": elapsed,
                "columns": out_columns, "dtype_plan": plan,
                "truncated": bool(row_cap and rows_total >= int(row_cap)), "error": None}
    except Exception as e:
        err = _friendly_db_error(e, "Database snapshot timed out.", password)
        log_with_sid(sid, "error", f"DB_SNAPSHOT_FAILED table={schema}.{table} err={err}")
        return {"ok": False, "rows": rows_total, "bytes": None,
                "elapsed_s": round(time.monotonic() - t0, 2),
                "columns": out_columns, "dtype_plan": plan, "truncated": False,
                "error": err}
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
