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
  - This module only ever issues SELECT / introspection statements. There is
    no code path that accepts free-form SQL; `_assert_single_select` guards
    the one assembled statement (snapshot SELECT + optional admin WHERE).
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

import importlib.util
import os
import re
import socket
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
            "options": f"-c statement_timeout={int(cfg.get('statement_timeout') or settings.DB_STATEMENT_TIMEOUT) * 1000}"}
    if cfg.get("ssl"):
        args["sslmode"] = "require"
    else:
        args["sslmode"] = "prefer"
    return args


def _pg_stmt_timeout(conn, seconds: int) -> None:
    from sqlalchemy import text
    conn.execute(text(f"SET statement_timeout = {int(seconds) * 1000}"))


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
    # pyodbc: `timeout` is the QUERY timeout (seconds); login timeout goes in
    # the URL query (LoginTimeout).
    return {"timeout": int(cfg.get("statement_timeout") or settings.DB_STATEMENT_TIMEOUT)}


def _mssql_query_args(cfg: dict) -> dict:
    return {
        "driver": "ODBC Driver 18 for SQL Server",
        "Encrypt": "yes" if cfg.get("ssl") else "no",
        "TrustServerCertificate": "yes" if cfg.get("trust_server_certificate") else "no",
        "LoginTimeout": str(int(cfg.get("connect_timeout") or settings.DB_CONNECT_TIMEOUT)),
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
    return {"tcp_connect_timeout": float(timeout)}


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
    }
    if cfg.get("ssl"):
        args["secure"] = "true"
        if cfg.get("trust_server_certificate"):
            args["verify"] = "false"      # self-signed; mirrors the mssql flag
    return args


DIALECTS: dict[str, Dialect] = {d.key: d for d in [
    Dialect(
        key="postgresql", label="PostgreSQL",
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
        key="mysql", label="MySQL",
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
        key="mariadb", label="MariaDB",
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
        key="mssql", label="Microsoft SQL Server",
        drivername="mssql+pyodbc", driver_module="pyodbc",
        default_port=1433, needs=("database",),
        supports_schemas=True, select1_sql="SELECT 1",
        connect_args=_mssql_connect_args, query_args=_mssql_query_args,
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
        key="oracle", label="Oracle",
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
        key="clickhouse", label="ClickHouse",
        drivername="clickhouse+native", driver_module="clickhouse_driver",
        default_port=9000, needs=("database",),
        # ClickHouse "databases" are what the SQLAlchemy dialect exposes as
        # schemas, so the schema browser lists them like any other dialect.
        supports_schemas=True, select1_sql="SELECT 1",
        # connect_args deliberately left at its default — see
        # _clickhouse_query_args for why the native driver ignores it. There
        # is likewise NO apply_stmt_timeout (mssql is the same shape): a
        # session `SET max_execution_time` does not survive over the native
        # protocol, because clickhouse-driver re-sends its OWN settings with
        # every query and they win. Measured: after `SET 7`,
        # system.settings still reads the URL value. The bound is real, it
        # just lives in the connection settings instead.
        query_args=_clickhouse_query_args,
        row_count_sql=("SELECT total_rows FROM system.tables "
                       "WHERE database = :schema AND name = :table"),
        table_size_sql=("SELECT total_bytes FROM system.tables "
                        "WHERE database = :schema AND name = :table"),
    ),
    # Hidden test-only entry: the offline pytest suite drives the SAME code
    # path through in-process SQLite. Never offered in the admin UI.
    Dialect(
        key="sqlite", label="SQLite (tests only)",
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
                    "available": ok, "unavailable_reason": reason})
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
    seen = 0
    e: Optional[BaseException] = exc
    while e is not None and seen < 5:
        if isinstance(e, (TimeoutError, socket.timeout)) or \
                _TIMEOUT_PAT.search(f"{type(e).__name__} {e}"):
            return phrase
        e = e.__cause__ or e.__context__
        seen += 1
    return _scrub(f"{type(exc).__name__}: {exc}", *secrets_)


_FORBIDDEN_TOKENS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|"
    r"EXEC|EXECUTE|CALL|INTO|ATTACH|PRAGMA|VACUUM|COPY)\b", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _assert_single_select(sql: str) -> None:
    """The connector's SELECT-only guard: single statement, starts with
    SELECT, no DML/DDL tokens, no chained statements, no CTE (Phase 1)."""
    stripped = _strip_sql_comments(sql).strip()
    if not re.match(r"^SELECT\b", stripped, re.IGNORECASE):
        raise ValueError("Only SELECT statements are permitted.")
    if ";" in stripped.rstrip().rstrip(";"):
        raise ValueError("Multiple SQL statements are not permitted.")
    if re.match(r"^WITH\b", stripped, re.IGNORECASE):
        raise ValueError("CTEs are not permitted.")
    if _FORBIDDEN_TOKENS.search(stripped):
        raise ValueError("Only SELECT statements are permitted.")


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
    return URL.create(
        drivername=d.drivername,
        username=cfg.get("user") or None,
        password=password or None,
        host=cfg.get("host") or None,
        port=int(cfg.get("port") or d.default_port) if (cfg.get("port") or d.default_port) else None,
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
    return create_engine(url, **kwargs)


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
    `_assert_single_select` sees exactly what will run."""
    sql = str(stmt.compile(dialect=dialect,
                           compile_kwargs={"literal_binds": True}))
    _assert_single_select(sql)
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
