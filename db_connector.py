"""SQLAlchemy connector for admin-registered database sources.

Dialect REGISTRY design: every DB-type-specific detail (URL drivername,
default port, identifier quoting, SELECT-1 probe, statement-timeout mechanism,
row-count / size catalog estimates, LIMIT syntax) lives in ONE `Dialect`
entry. Adding a future DB type (DB2, HANA, Snowflake, ClickHouse, ...) is one
`Dialect(...)` literal plus one pinned driver package — nothing else changes.

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
    quote: tuple  # (open_char, close_char)
    needs: tuple  # extra connection fields the admin form must collect
    supports_schemas: bool
    select1_sql: str
    connect_args: Callable[[dict, int], dict] = field(default=lambda cfg, t: {})
    query_args: Callable[[dict], dict] = field(default=lambda cfg: {})
    apply_stmt_timeout: Callable = field(default=_no_op_timeout)
    limit_clause: Callable[[str, int], str] = field(
        default=lambda sql, n: f"{sql} LIMIT {int(n)}")
    row_count_sql: Optional[str] = None
    table_size_sql: Optional[str] = None
    exact_count_fallback: bool = False
    allow_url_override: bool = False
    hidden: bool = False

    def q(self, ident: str) -> str:
        """Quote an identifier verbatim (Inspector-returned names are used
        as-is, so e.g. Oracle's UPPERCASE folding can never mis-case)."""
        o, c = self.quote
        return f"{o}{ident.replace(c, c + c)}{c}"

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


DIALECTS: dict[str, Dialect] = {d.key: d for d in [
    Dialect(
        key="postgresql", label="PostgreSQL",
        drivername="postgresql+psycopg2", driver_module="psycopg2",
        default_port=5432, quote=('"', '"'), needs=("database",),
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
        default_port=3306, quote=('`', '`'), needs=("database",),
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
        default_port=3306, quote=('`', '`'), needs=("database",),
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
        default_port=1433, quote=('[', ']'), needs=("database",),
        supports_schemas=True, select1_sql="SELECT 1",
        connect_args=_mssql_connect_args, query_args=_mssql_query_args,
        limit_clause=lambda sql, n: re.sub(r"^SELECT ", f"SELECT TOP {int(n)} ", sql, count=1),
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
        default_port=1521, quote=('"', '"'), needs=("service_name",),
        supports_schemas=True, select1_sql="SELECT 1 FROM DUAL",
        connect_args=_oracle_connect_args, query_args=_oracle_query_args,
        apply_stmt_timeout=_oracle_stmt_timeout,
        limit_clause=lambda sql, n: f"{sql} FETCH FIRST {int(n)} ROWS ONLY",
        row_count_sql=("SELECT num_rows FROM all_tables "
                       "WHERE owner = :schema AND table_name = :table"),
        table_size_sql=("SELECT SUM(bytes) FROM all_segments "
                        "WHERE owner = :schema AND segment_name = :table"),
    ),
    # Hidden test-only entry: the offline pytest suite drives the SAME code
    # path through in-process SQLite. Never offered in the admin UI.
    Dialect(
        key="sqlite", label="SQLite (tests only)",
        drivername="sqlite+pysqlite", driver_module="sqlite3",
        default_port=None, quote=('"', '"'), needs=(),
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
    Each of the five drivers words it differently (psycopg2 "timeout expired",
    pyodbc HYT00, oracledb DPY-6005, …), so the whole cause chain is checked.
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


def introspect(cfg: dict, password: str, schema: Optional[str], table: str,
               *, sid: str) -> dict:
    """Columns + dtypes / PK / FK / indexes / comment via Inspector, plus
    catalog-estimate row count and size (never COUNT(*) on a customer table —
    the sqlite test entry's exact_count_fallback is the sole exception).
    Individually degraded on missing catalog privileges."""
    from sqlalchemy import inspect as sa_inspect, text
    d = get_dialect(cfg.get("db_type"))
    engine = None
    degraded: list[str] = []
    try:
        engine = get_engine(cfg, password)
        insp = sa_inspect(engine)
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
            columns.append({
                "name": c.get("name"),
                "dtype": str(c.get("type")),
                "py_type": py_type,
                "nullable": bool(c.get("nullable", True)),
                "comment": c.get("comment"),
                "pk": c.get("name") in pk,
                "indexed": c.get("name") in indexed_cols,
            })

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
                        text(d.row_count_sql), {"schema": schema, "table": table}
                    ).scalar()
                    row_count = int(row_count) if row_count is not None and int(row_count) >= 0 else None
                except Exception:
                    degraded.append("row_count")
            elif d.exact_count_fallback:
                try:
                    qname = f"{d.q(schema)}.{d.q(table)}" if schema else d.q(table)
                    row_count = int(conn.execute(
                        text(f"SELECT COUNT(*) FROM {qname}")).scalar() or 0)
                except Exception:
                    degraded.append("row_count")
            else:
                degraded.append("row_count")
            if d.table_size_sql:
                try:
                    size_bytes = conn.execute(
                        text(d.table_size_sql), {"schema": schema, "table": table}
                    ).scalar()
                    size_bytes = int(size_bytes) if size_bytes is not None else None
                except Exception:
                    degraded.append("size")
            else:
                degraded.append("size")

        return {"ok": True, "schema": schema, "table": table,
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


def _build_select(d: Dialect, schema: Optional[str], table: str,
                  columns: Optional[list] = None, where: Optional[str] = None,
                  row_cap: Optional[int] = None) -> str:
    cols = ", ".join(d.q(c) for c in columns) if columns else "*"
    qname = f"{d.q(schema)}.{d.q(table)}" if schema else d.q(table)
    sql = f"SELECT {cols} FROM {qname}"
    if where:
        _assert_single_select(f"SELECT 1 FROM {qname} WHERE {where}")
        sql += f" WHERE {where}"
    if row_cap:
        sql = d.limit_clause(sql, int(row_cap))
    _assert_single_select(sql)
    return sql


def preview_rows(cfg: dict, password: str, schema: Optional[str], table: str,
                 *, limit: Optional[int] = None, where: Optional[str] = None,
                 sid: str) -> dict:
    """First rows for the ladmin registration preview. Values go only to the
    admin's browser — never to the brain, never into logs."""
    from sqlalchemy import text
    d = get_dialect(cfg.get("db_type"))
    engine = None
    try:
        limit = int(limit or settings.DB_PREVIEW_ROWS)
        sql = _build_select(d, schema, table, where=where, row_cap=limit)
        engine = get_engine(cfg, password)
        with engine.connect() as conn:
            d.apply_stmt_timeout(conn, int(cfg.get("statement_timeout") or settings.DB_STATEMENT_TIMEOUT))
            df = pd.read_sql(text(sql), conn)
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
# Snapshot
# ---------------------------------------------------------------------------

_CATEGORY_MAX_UNIQUE = 100_000
_CATEGORY_MAX_RATIO = 0.5


def _compute_dtype_plan(chunk: pd.DataFrame) -> dict:
    """From the FIRST chunk, decide per-column optimization: numeric downcast
    (applied on write — pins the Arrow schema across chunks) and 'category'
    for low-cardinality strings. Category entries are RECORDED ONLY — neither
    baked into the file (per-chunk categoricals destabilize the Arrow schema)
    nor applied by the loader: categorical frames make generated groupby code
    (pandas < 3.0 observed=False default) emit the full cartesian product of
    ALL categories, which once put every city/product on a chart axis."""
    plan: dict[str, str] = {}
    n = max(1, len(chunk))
    for col in chunk.columns:
        s = chunk[col]
        try:
            if pd.api.types.is_integer_dtype(s):
                down = pd.to_numeric(s, downcast="integer")
                if str(down.dtype) != str(s.dtype):
                    plan[str(col)] = str(down.dtype)
            elif pd.api.types.is_float_dtype(s):
                down = pd.to_numeric(s, downcast="float")
                if str(down.dtype) != str(s.dtype):
                    plan[str(col)] = str(down.dtype)
            elif pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s):
                nunique = s.nunique(dropna=True)
                # Ratio guards big chunks; the 1000-floor keeps the plan sane
                # when chunk 1 is tiny (category on a small table is harmless).
                if nunique <= _CATEGORY_MAX_UNIQUE and nunique <= max(1000, n * _CATEGORY_MAX_RATIO):
                    plan[str(col)] = "category"
        except Exception:
            continue
    return plan


def _apply_numeric_plan(chunk: pd.DataFrame, plan: dict) -> pd.DataFrame:
    """Cast the numeric entries of the plan onto a chunk; a failing cast (a
    value outgrew the planned type on refresh) drops that column from the plan
    for this snapshot and logs it — never aborts the snapshot."""
    for col, dtype in list(plan.items()):
        if dtype == "category" or col not in chunk.columns:
            continue
        try:
            chunk[col] = chunk[col].astype(dtype)
        except Exception:
            log_with_sid("db_snapshot", "warning", f"DB_SNAPSHOT_DTYPE_RELAXED col={col}")
            plan.pop(col, None)
    return chunk


def snapshot_table(cfg: dict, password: str, *, schema: Optional[str], table: str,
                   columns: Optional[list] = None, where: Optional[str] = None,
                   row_cap: Optional[int] = None, dtype_plan: Optional[dict] = None,
                   dest: Path, sid: str, chunk_rows: Optional[int] = None) -> dict:
    """Chunked SELECT → parquet with an atomic os.replace. Never raises; on
    failure the tmp file is removed and the PREVIOUS snapshot (if any) stays
    in place, so chats keep serving the last good data (Article IV)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from sqlalchemy import text

    d = get_dialect(cfg.get("db_type"))
    engine = None
    writer = None
    tmp = dest.with_name(dest.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    t0 = time.monotonic()
    rows_total = 0
    out_columns: list[str] = []
    plan = dict(dtype_plan or {})
    try:
        sql = _build_select(d, schema, table, columns=columns, where=where, row_cap=row_cap)
        chunk_size = int(chunk_rows or settings.DB_SNAPSHOT_CHUNK_ROWS)
        engine = get_engine(cfg, password)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with engine.connect() as conn:
            d.apply_stmt_timeout(conn, int(cfg.get("statement_timeout") or settings.DB_STATEMENT_TIMEOUT))
            first = True
            for chunk in pd.read_sql(text(sql), conn, chunksize=chunk_size):
                chunk.columns = [str(c) for c in chunk.columns]
                if first:
                    if not plan:
                        plan = _compute_dtype_plan(chunk)
                    out_columns = [str(c) for c in chunk.columns]
                chunk = _apply_numeric_plan(chunk, plan)
                arrow = pa.Table.from_pandas(chunk, preserve_index=False)
                if first:
                    writer = pq.ParquetWriter(str(tmp), arrow.schema,
                                              compression="snappy", use_dictionary=True)
                    first = False
                writer.write_table(arrow)
                rows_total += len(chunk)
            if first:
                # Zero rows — write an empty parquet with the introspected shape.
                empty = pd.DataFrame(columns=[c for c in (columns or [])])
                arrow = pa.Table.from_pandas(empty, preserve_index=False)
                writer = pq.ParquetWriter(str(tmp), arrow.schema,
                                          compression="snappy", use_dictionary=True)
                writer.write_table(arrow)
                out_columns = [str(c) for c in empty.columns]
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
