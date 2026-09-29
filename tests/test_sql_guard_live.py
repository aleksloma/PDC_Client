"""The read-only SQL guard, the row-cap wrapper and the live query function.

Free-form SELECT text (the live-table query path) passes through ONE gate,
`db_connector.assert_read_only_query`, which runs two independent layers:

* the regex layer — the connector's original checks on the comment-stripped
  text (anchor, stacked statements, CTE, forbidden tokens), with the three
  messages kept byte-identical;
* the parse layer — `sqlglot` on the ORIGINAL text under the connection's
  dialect: exactly one statement, a SELECT or a set operation at the root, no
  statement node anywhere in the tree, no `SELECT ... INTO`, no row locks, no
  denied function (sleep / file / network / shell primitives), no CTE unless
  allowed, and — only when a schema list is configured — no table outside it
  and no catalog-qualified table.

`strict_parse=True` (live queries) refuses text sqlglot cannot parse;
`strict_parse=False` (the connector's own constructs) logs one
`SQL_GUARD_PARSE_WARN` line (dialect + exception type, never the SQL) and lets
the regex layer decide. Every POSITIVE refusal of the parse layer refuses in
both modes.

`wrap_with_row_limit` keeps the query verbatim inside a per-dialect outer
limit, so an inner LIMIT/TOP can never exceed the cap.

`run_live_select` re-asserts the guard, wraps at `row_cap + 1`, builds the
engine from a cfg copy carrying the bounded statement timeout, escapes only a
colon `text()` would read as a bind parameter, flags truncation, never raises,
and logs a hash and timings — never the SQL text or a literal from it.

Offline: every live execution is a tmp-file sqlite database reached through
the hidden sqlite dialect; every other dialect is covered by compiling
against the real SQLAlchemy dialect objects.
"""
import ast
import logging
import re
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.dialects import mssql, mysql, oracle, postgresql
from sqlalchemy.dialects import sqlite as sa_sqlite

import db_connector
from settings import settings

ROOT = Path(__file__).resolve().parent.parent


def _guard(sql, *, allow_cte=True, strict_parse=True, dialect=None,
           allowed_schemas=None, **extra):
    return db_connector.assert_read_only_query(
        sql, allow_cte=allow_cte, strict_parse=strict_parse, dialect=dialect,
        allowed_schemas=allowed_schemas, **extra)


def _records(caplog, marker):
    return [r for r in caplog.records if marker in r.getMessage()]


def _set_setting(monkeypatch, name, value):
    """Patch one setting. Fails with a clear message (not a pydantic error)
    while the setting does not exist."""
    if not hasattr(settings, name):
        pytest.fail(f"settings.{name} is missing")
    monkeypatch.setattr(settings, name, value)


# ===========================================================================
# the negative corpus — every entry refuses under strict parsing
# ===========================================================================
# (id, sql, dialect, allow_cte, allowed_schemas)
NEGATIVE = [
    ("comment-split-keyword", "SEL/**/ECT 1", None, True, None),
    ("stacked-drop", "SELECT 1; DROP TABLE t", None, True, None),
    ("stacked-newline-delete", "SELECT 1;\nDELETE FROM t", None, True, None),
    ("stacked-after-line-comment", "SELECT 1 -- note\n; DROP TABLE t",
     None, True, None),
    ("stacked-after-block-comment", "SELECT 1 /* ok */; DELETE FROM t",
     None, True, None),
    ("insert-select", "INSERT INTO t2 SELECT * FROM t", None, True, None),
    ("select-into", "SELECT a INTO t2 FROM t", "mssql", True, None),
    ("into-outfile", "SELECT a FROM t INTO OUTFILE '/tmp/x'", "mysql", True, None),
    ("create-table-as", "CREATE TABLE t2 AS SELECT * FROM t", None, True, None),
    ("for-update", "SELECT a FROM t FOR UPDATE", "postgresql", True, None),
    ("lock-tables", "LOCK TABLES t WRITE", "mysql", True, None),
    ("call", "CALL p()", None, True, None),
    ("exec-xp-cmdshell", "EXEC xp_cmdshell 'dir'", "mssql", True, None),
    ("waitfor", "WAITFOR DELAY '00:00:05'", "mssql", True, None),
    ("copy", "COPY t TO '/tmp/x'", "postgresql", True, None),
    ("load-data", "LOAD DATA INFILE '/x' INTO TABLE t", "mysql", True, None),
    ("pg-sleep", "SELECT pg_sleep(10)", "postgresql", True, None),
    ("pg-sleep-upper-case", "select PG_SLEEP(10)", "postgresql", True, None),
    ("pg-sleep-in-where", "SELECT a FROM t WHERE pg_sleep(1) IS NULL",
     "postgresql", True, None),
    ("mysql-sleep", "SELECT sleep(10), a FROM t", "mysql", True, None),
    ("benchmark", "SELECT BENCHMARK(1000000, MD5('x'))", "mysql", True, None),
    ("dbms-lock-sleep", "SELECT dbms_lock.sleep(5) FROM dual", "oracle", True, None),
    ("openrowset", "SELECT * FROM OPENROWSET('SQLNCLI','x','SELECT 1')",
     "mssql", True, None),
    ("xp-cmdshell-function", "SELECT xp_cmdshell('dir')", None, True, None),
    ("dblink", "SELECT * FROM dblink('x','SELECT 1') AS t(a int)",
     "postgresql", True, None),
    ("pg-read-file", "SELECT pg_read_file('/etc/passwd')", "postgresql", True, None),
    ("load-file", "SELECT load_file('/etc/passwd')", "mysql", True, None),
    ("clickhouse-file", "SELECT * FROM file('x.csv')", "clickhouse", True, None),
    ("clickhouse-url", "SELECT * FROM url('http://x', CSV)", "clickhouse", True, None),
    ("update", "UPDATE t SET a=1", None, True, None),
    ("delete", "DELETE FROM t", None, True, None),
    ("drop", "DROP TABLE t", None, True, None),
    ("truncate", "TRUNCATE t", None, True, None),
    ("grant", "GRANT SELECT ON t TO u", None, True, None),
    ("merge", "MERGE INTO t USING s ON (1=1) WHEN MATCHED THEN DELETE",
     None, True, None),
    ("alter", "ALTER TABLE t ADD c int", None, True, None),
    ("union-then-drop", "SELECT 1 UNION SELECT 2; DROP TABLE t", None, True, None),
    ("empty", "", None, True, None),
    ("whitespace-only", "   \n\t  ", None, True, None),
    ("comment-only", "-- only a comment", None, True, None),
    ("cte-not-allowed", "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
     "postgresql", False, None),
    ("nested-cte-not-allowed",
     "SELECT * FROM (WITH c AS (SELECT 1 AS a) SELECT a FROM c) q",
     "postgresql", False, None),
    ("data-modifying-cte",
     "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
     "postgresql", True, None),
    ("schema-outside-list", "SELECT a FROM hr.emp", "postgresql", True, {"sales"}),
    ("join-to-schema-outside-list",
     "SELECT t.a FROM sales.t JOIN hr.emp e ON t.a = e.a",
     "postgresql", True, {"sales"}),
    ("union-to-catalog-with-list",
     "SELECT a FROM sales.t UNION SELECT relname FROM pg_catalog.pg_class",
     "postgresql", True, {"sales"}),
    ("catalog-part-with-list", "SELECT a FROM db1.sales.t",
     "postgresql", True, {"sales"}),
]


def test_negative_corpus_is_large():
    assert len(NEGATIVE) >= 40
    assert len({c[0] for c in NEGATIVE}) == len(NEGATIVE)


@pytest.mark.parametrize("sql, dialect, allow_cte, schemas",
                         [c[1:] for c in NEGATIVE], ids=[c[0] for c in NEGATIVE])
def test_negative_corpus_refuses(sql, dialect, allow_cte, schemas):
    with pytest.raises(ValueError):
        _guard(sql, allow_cte=allow_cte, strict_parse=True, dialect=dialect,
               allowed_schemas=schemas)


# ===========================================================================
# the positive corpus — none of these raise under strict parsing
# ===========================================================================
POSITIVE = [
    ("join", "SELECT o.a, c.b FROM orders o JOIN clients c ON o.cid = c.id",
     "postgresql", True, None),
    ("left-join-group-having",
     "SELECT c.b, COUNT(*) AS n FROM orders o LEFT JOIN clients c "
     "ON o.cid = c.id GROUP BY c.b HAVING COUNT(*) > 1",
     "postgresql", True, None),
    ("window-function",
     "SELECT a, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS rn FROM t",
     "postgresql", True, None),
    ("subquery-in-from", "SELECT q.a FROM (SELECT a FROM t WHERE a > 1) q",
     "postgresql", True, None),
    ("subquery-in-where", "SELECT a FROM t WHERE a IN (SELECT a FROM u)",
     "postgresql", True, None),
    ("union-all", "SELECT a FROM t UNION ALL SELECT a FROM u",
     "postgresql", True, None),
    ("cte-allowed", "WITH x AS (SELECT a FROM t) SELECT a FROM x",
     "postgresql", True, None),
    ("like-percent", "SELECT a FROM t WHERE b LIKE '%x%'", "postgresql", True, None),
    ("case-expression",
     "SELECT CASE WHEN a > 1 THEN 'hi' ELSE 'lo' END AS k FROM t",
     "postgresql", True, None),
    ("line-comments", "-- leading note\nSELECT a FROM t -- trailing note",
     "postgresql", True, None),
    ("block-comments", "/* note */ SELECT a /* inline */ FROM t",
     "postgresql", True, None),
    ("commented-out-second-statement", "SELECT a FROM t\n-- ;DROP TABLE t",
     "postgresql", True, None),
    ("mssql-top-brackets", "SELECT TOP 10 * FROM [s].[t]", "mssql", True, None),
    ("oracle-fetch-first", "SELECT a FROM t FETCH FIRST 10 ROWS ONLY",
     "oracle", True, None),
    ("oracle-quoted-rownum",
     'SELECT "A" FROM "BSREP"."OFFERING_ALL" WHERE ROWNUM <= 5',
     "oracle", True, None),
    ("mysql-backticks", "SELECT `a`, `b` FROM `s`.`t` LIMIT 5", "mysql", True, None),
    ("postgres-cast", "SELECT a::int FROM t", "postgresql", True, None),
    ("clickhouse-aggregate",
     "SELECT a, count() AS n FROM db.events GROUP BY a ORDER BY n DESC LIMIT 10",
     "clickhouse", True, None),
    ("sqlite-order-limit", "SELECT a FROM t WHERE b = 'x' ORDER BY a LIMIT 3",
     "sqlite", True, None),
    ("union-to-catalog-without-list",
     "SELECT a FROM sales.t UNION SELECT relname FROM pg_catalog.pg_class",
     "postgresql", True, None),
    ("schema-list-case-insensitive", "SELECT a FROM sales.t",
     "postgresql", True, {"SALES"}),
    ("join-inside-schema-list",
     "SELECT t.a FROM sales.t JOIN sales.u ON t.a = u.a",
     "postgresql", True, {"sales"}),
]


def test_positive_corpus_is_large():
    assert len(POSITIVE) >= 15
    assert len({c[0] for c in POSITIVE}) == len(POSITIVE)


@pytest.mark.parametrize("sql, dialect, allow_cte, schemas",
                         [c[1:] for c in POSITIVE], ids=[c[0] for c in POSITIVE])
def test_positive_corpus_passes(sql, dialect, allow_cte, schemas):
    assert _guard(sql, allow_cte=allow_cte, strict_parse=True, dialect=dialect,
                  allowed_schemas=schemas) is None


def test_the_dialect_may_be_a_registry_entry_or_a_sqlalchemy_dialect():
    sql = "SELECT TOP 10 * FROM [s].[t]"
    _guard(sql, dialect="mssql")
    _guard(sql, dialect=db_connector.DIALECTS["mssql"])
    _guard(sql, dialect=mssql.dialect())


@pytest.mark.parametrize("key, expected", [
    ("postgresql", "postgres"), ("mysql", "mysql"), ("mariadb", "mysql"),
    ("mssql", "tsql"), ("oracle", "oracle"), ("clickhouse", "clickhouse"),
    ("sqlite", "sqlite"),
])
def test_registry_carries_the_sqlglot_dialect_name(key, expected):
    assert getattr(db_connector.DIALECTS[key], "sqlglot_dialect", None) == expected


def test_list_dialects_shape_is_unchanged():
    for r in db_connector.list_dialects():
        assert "sqlglot_dialect" not in r


# ===========================================================================
# messages
# ===========================================================================
def test_message_only_select():
    with pytest.raises(ValueError) as ei:
        _guard("UPDATE t SET a=1", strict_parse=True)
    assert str(ei.value) == "Only SELECT statements are permitted."


def test_message_multiple_statements():
    with pytest.raises(ValueError) as ei:
        _guard("SELECT 1; SELECT 2", strict_parse=True)
    assert str(ei.value) == "Multiple SQL statements are not permitted."


def test_message_cte():
    with pytest.raises(ValueError) as ei:
        _guard("WITH x AS (SELECT 1 AS a) SELECT a FROM x", allow_cte=False,
               strict_parse=True)
    assert str(ei.value) == "CTEs are not permitted."


@pytest.mark.parametrize("sql, dialect", [
    ("UPDATE t SET a = 'ZQMARK_4471'", None),
    ("SELECT a FROM t WHERE b = 'ZQMARK_4471' FOR UPDATE", "postgresql"),
    ("SELECT a FROM t WHERE pg_sleep(1) IS NULL AND b = 'ZQMARK_4471'",
     "postgresql"),
    ("SELECT a FROM hr.emp WHERE b = 'ZQMARK_4471'", "postgresql"),
    ("SELECT a FROM t WHERE (b = 'ZQMARK_4471'", "postgresql"),
])
def test_a_refusal_never_quotes_the_sql(sql, dialect):
    with pytest.raises(ValueError) as ei:
        _guard(sql, strict_parse=True, dialect=dialect, allowed_schemas={"sales"})
    assert "ZQMARK_4471" not in str(ei.value)
    assert str(ei.value)


# ===========================================================================
# strict_parse
# ===========================================================================
UNPARSEABLE = "SELECT zq_parse_marker FROM t WHERE (zq_parse_marker"


def test_unparseable_text_is_accepted_by_the_regex_layer_today():
    """Precondition of the strict_parse tests: the regex layer alone lets this
    text through, so any refusal of it comes from the parse layer."""
    stripped = db_connector._strip_sql_comments(UNPARSEABLE).strip()
    assert re.match(r"^SELECT\b", stripped, re.IGNORECASE)
    assert ";" not in stripped
    assert not db_connector._FORBIDDEN_TOKENS.search(stripped)


def test_strict_parse_refuses_unparseable_text():
    with pytest.raises(ValueError):
        _guard(UNPARSEABLE, strict_parse=True, dialect="postgresql")


def test_lenient_parse_warns_once_without_the_sql(caplog):
    with caplog.at_level(logging.INFO):
        assert _guard(UNPARSEABLE, allow_cte=False, strict_parse=False,
                      dialect="postgresql") is None
    recs = _records(caplog, "SQL_GUARD_PARSE_WARN")
    assert len(recs) == 1, [r.getMessage() for r in recs]
    msg = recs[0].getMessage()
    assert "postgres" in msg
    assert "Error" in msg                    # the exception TYPE name
    assert recs[0].levelno == logging.WARNING
    for r in caplog.records:
        assert "zq_parse_marker" not in r.getMessage()


@pytest.mark.parametrize("sql, dialect, allow_cte, schemas", [
    ("SELECT pg_sleep(10)", "postgresql", False, None),
    ("SELECT * FROM (WITH c AS (SELECT 1 AS a) SELECT a FROM c) q",
     "postgresql", False, None),
    ("SELECT a INTO t2 FROM t", "mssql", False, None),
    ("SELECT a FROM t FOR UPDATE", "postgresql", False, None),
    ("SELECT a FROM hr.emp", "postgresql", False, {"sales"}),
    ("SELECT 1; SELECT 2", "postgresql", False, None),
], ids=["denied-function", "nested-cte", "select-into", "row-lock",
        "schema-outside-list", "two-statements"])
def test_lenient_parse_still_refuses_every_positive_refusal(sql, dialect,
                                                            allow_cte, schemas):
    with pytest.raises(ValueError):
        _guard(sql, allow_cte=allow_cte, strict_parse=False, dialect=dialect,
               allowed_schemas=schemas)


# ===========================================================================
# the connector's own shapes pass the STRICT variant — a snapshot never warns
# ===========================================================================
def _sa_dialects():
    out = [("postgresql", postgresql.dialect()), ("mysql", mysql.dialect()),
           ("mssql", mssql.dialect()), ("oracle", oracle.dialect()),
           ("sqlite", sa_sqlite.dialect())]
    try:
        from clickhouse_sqlalchemy.drivers.native.base import \
            ClickHouseDialect_native
        out.append(("clickhouse", ClickHouseDialect_native()))
    except Exception:
        pass
    return out


def _shapes():
    return [
        db_connector._select_stmt("s", "t", row_cap=20),
        db_connector._select_stmt(db_connector.qname("BSREP", True),
                                  db_connector.qname("OFFERING_ALL", True),
                                  row_cap=20),
        db_connector._select_stmt("s", "t", columns=["a", "MixedCol"],
                                  where="a = 'x'", row_cap=5),
        select(func.count().label("pdc_n")).select_from(
            db_connector._select_stmt("s", "t", where="a > 1").subquery("pdc_c")),
    ]


@pytest.mark.parametrize("name, dialect", _sa_dialects(),
                         ids=[n for n, _ in _sa_dialects()])
def test_connector_shapes_pass_the_strict_variant(name, dialect, caplog):
    with caplog.at_level(logging.INFO):
        for stmt in _shapes():
            sql = str(stmt.compile(dialect=dialect,
                                   compile_kwargs={"literal_binds": True}))
            _guard(sql, allow_cte=False, strict_parse=True, dialect=dialect)
            assert db_connector._compiled_sql(stmt, dialect) == sql
    assert _records(caplog, "SQL_GUARD_PARSE_WARN") == []


@pytest.mark.parametrize("version, marker", [((11, 2), "ROWNUM"),
                                             ((19, 0), "FETCH FIRST")])
def test_oracle_limit_forms_pass_the_strict_variant(version, marker):
    d = oracle.dialect()
    d.server_version_info = version
    if version < (12,):
        d._supports_offset_fetch = False
    for stmt in _shapes():
        sql = str(stmt.compile(dialect=d, compile_kwargs={"literal_binds": True}))
        _guard(sql, allow_cte=False, strict_parse=True, dialect=d)
        assert db_connector._compiled_sql(stmt, d) == sql
    limited = str(_shapes()[0].compile(dialect=d,
                                       compile_kwargs={"literal_binds": True}))
    assert marker in limited


# ===========================================================================
# wrap_with_row_limit
# ===========================================================================
INNER = "SELECT a, b FROM s.t WHERE a > 1"


@pytest.mark.parametrize("key, expected", [
    ("postgresql", f"SELECT * FROM ({INNER}) AS pdc_q LIMIT 11"),
    ("mysql", f"SELECT * FROM ({INNER}) AS pdc_q LIMIT 11"),
    ("mariadb", f"SELECT * FROM ({INNER}) AS pdc_q LIMIT 11"),
    ("clickhouse", f"SELECT * FROM ({INNER}) AS pdc_q LIMIT 11"),
    ("sqlite", f"SELECT * FROM ({INNER}) AS pdc_q LIMIT 11"),
    ("mssql", f"SELECT TOP 11 * FROM ({INNER}) AS pdc_q"),
    ("oracle", f"SELECT * FROM ({INNER}) pdc_q FETCH FIRST 11 ROWS ONLY"),
])
def test_wrap_per_dialect(key, expected):
    assert db_connector.wrap_with_row_limit(INNER, key, 11) == expected


def test_wrap_keeps_an_inner_limit_inside():
    out = db_connector.wrap_with_row_limit("SELECT a FROM t LIMIT 5",
                                           "postgresql", 11)
    assert out == "SELECT * FROM (SELECT a FROM t LIMIT 5) AS pdc_q LIMIT 11"


def test_wrap_strips_a_trailing_semicolon_and_whitespace():
    out = db_connector.wrap_with_row_limit("SELECT a FROM t;  \n", "sqlite", 11)
    assert out == "SELECT * FROM (SELECT a FROM t) AS pdc_q LIMIT 11"


def test_wrap_strips_comments():
    out = db_connector.wrap_with_row_limit(
        "SELECT a /* inline */ FROM t -- trailing", "postgresql", 11)
    assert "/*" not in out and "--" not in out
    assert "inline" not in out and "trailing" not in out
    assert out.startswith("SELECT * FROM (SELECT a")
    assert out.endswith(") AS pdc_q LIMIT 11")


def test_wrap_mssql_order_by_gets_offset_inside():
    out = db_connector.wrap_with_row_limit("SELECT a, b FROM s.t ORDER BY b",
                                           "mssql", 11)
    assert out == ("SELECT TOP 11 * FROM (SELECT a, b FROM s.t ORDER BY b "
                   "OFFSET 0 ROWS) AS pdc_q")


def test_wrap_mssql_inner_top_is_unchanged():
    inner = "SELECT TOP 5 a FROM t ORDER BY a"
    assert db_connector.wrap_with_row_limit(inner, "mssql", 11) == \
        f"SELECT TOP 11 * FROM ({inner}) AS pdc_q"


def test_wrap_mssql_window_order_by_is_not_a_top_level_order():
    inner = "SELECT a, ROW_NUMBER() OVER (ORDER BY b) AS rn FROM t"
    assert db_connector.wrap_with_row_limit(inner, "mssql", 11) == \
        f"SELECT TOP 11 * FROM ({inner}) AS pdc_q"


@pytest.mark.parametrize("key", ["postgresql", "mysql", "mariadb", "mssql",
                                 "oracle", "clickhouse", "sqlite"])
def test_wrap_leaves_a_double_colon_cast_untouched(key):
    inner = "SELECT a::int AS a FROM t"
    out = db_connector.wrap_with_row_limit(inner, key, 11)
    assert f"({inner})" in out
    assert "\\:" not in out


@pytest.mark.parametrize("n", [0, -1, "5", 1.5, None])
def test_wrap_rejects_a_non_positive_or_non_int_cap(n):
    with pytest.raises(ValueError):
        db_connector.wrap_with_row_limit(INNER, "postgresql", n)


def test_wrap_rejects_an_unknown_dialect():
    with pytest.raises(ValueError):
        db_connector.wrap_with_row_limit(INNER, "db2", 11)


# ===========================================================================
# run_live_select on sqlite
# ===========================================================================
@pytest.fixture
def live_cfg(tmp_path):
    """A tmp-file sqlite DB, table r(n, s): 25 rows; s holds ':x', '10:30'
    and 'a%b' on rows 1-3 (bind-parameter and percent edge cases)."""
    db = tmp_path / "live.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    special = {1: ":x", 2: "10:30", 3: "a%b"}
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE r (n INTEGER PRIMARY KEY, s TEXT)"))
        for i in range(1, 26):
            conn.execute(text("INSERT INTO r VALUES (:n, :s)"),
                         {"n": i, "s": special.get(i, f"v{i}")})
    eng.dispose()
    return {"db_type": "sqlite", "url_override": f"sqlite+pysqlite:///{db}"}


def _live(cfg, sql, **kw):
    kw.setdefault("sid", "t")
    return db_connector.run_live_select(cfg, "", sql, **kw)


def _engine_spy(monkeypatch):
    seen = []
    real = db_connector.get_engine

    def spy(cfg, *a, **kw):
        seen.append(dict(cfg))
        return real(cfg, *a, **kw)
    monkeypatch.setattr(db_connector, "get_engine", spy)
    return seen


def test_run_live_select_response_shape(live_cfg):
    """Re-pinned when the error was reduced: a success carries `error_class`
    and `error_detail` too, both None."""
    res = _live(live_cfg, "SELECT n, s FROM r", row_cap=100)
    assert {"ok", "df", "truncated", "rows", "elapsed_ms", "timed_out",
            "error", "error_class", "error_detail"} <= set(res)
    assert res["ok"] is True
    assert isinstance(res["df"], pd.DataFrame)
    assert list(res["df"].columns) == ["n", "s"]
    assert len(res["df"]) == 25 and res["rows"] == 25
    assert res["truncated"] is False and res["timed_out"] is False
    assert res["error"] is None
    assert res["error_class"] is None and res["error_detail"] is None
    assert isinstance(res["elapsed_ms"], (int, float)) and res["elapsed_ms"] >= 0


def test_run_live_select_truncates_above_the_cap(live_cfg):
    res = _live(live_cfg, "SELECT n FROM r ORDER BY n", row_cap=10)
    assert res["ok"] is True
    assert len(res["df"]) == 10
    assert res["truncated"] is True
    assert list(res["df"]["n"]) == list(range(1, 11))


def test_run_live_select_exactly_the_cap_is_not_truncated(live_cfg):
    res = _live(live_cfg, "SELECT n FROM r", row_cap=25)
    assert res["ok"] is True
    assert len(res["df"]) == 25 and res["rows"] == 25
    assert res["truncated"] is False


def test_run_live_select_an_inner_limit_cannot_exceed_the_cap(live_cfg):
    res = _live(live_cfg, "SELECT n FROM r LIMIT 1000", row_cap=5)
    assert res["ok"] is True
    assert len(res["df"]) == 5
    assert res["truncated"] is True


def test_run_live_select_colon_literals_are_not_bind_parameters(live_cfg):
    res = _live(live_cfg, "SELECT n FROM r WHERE s = ':x'", row_cap=100)
    assert res["ok"] is True, res.get("error")
    assert list(res["df"]["n"]) == [1]
    res = _live(live_cfg, "SELECT n FROM r WHERE s = '10:30'", row_cap=100)
    assert res["ok"] is True, res.get("error")
    assert list(res["df"]["n"]) == [2]


def test_run_live_select_cast(live_cfg):
    res = _live(live_cfg, "SELECT CAST(n AS INTEGER) AS k FROM r WHERE n <= 4",
                row_cap=100)
    assert res["ok"] is True, res.get("error")
    assert sorted(res["df"]["k"]) == [1, 2, 3, 4]


def test_run_live_select_escapes_only_bind_shaped_colons(live_cfg, monkeypatch):
    """What reaches the driver: `::` casts and '10:30' unchanged, a
    colon-word inside a literal escaped as `\\:`."""
    captured = []

    def fake_read_sql(sql, con, *a, **kw):
        captured.append(getattr(sql, "text", sql))
        frame = pd.DataFrame({"n": [1]})
        return iter([frame]) if kw.get("chunksize") else frame

    monkeypatch.setattr(db_connector.pd, "read_sql", fake_read_sql)
    res = _live(live_cfg,
                "SELECT n::int AS n FROM r WHERE s = ':x' OR s = '10:30'",
                row_cap=100)
    assert res["ok"] is True, res.get("error")
    assert captured, "pd.read_sql was never called"
    sent = str(captured[-1])
    assert "n::int" in sent
    assert "\\:\\:" not in sent
    assert "'\\:x'" in sent
    assert "'10:30'" in sent


def test_run_live_select_like_with_percent(live_cfg):
    res = _live(live_cfg, "SELECT s FROM r WHERE s LIKE '%b%'", row_cap=100)
    assert res["ok"] is True, res.get("error")
    assert list(res["df"]["s"]) == ["a%b"]


def test_run_live_select_accepts_a_cte(live_cfg):
    res = _live(live_cfg,
                "WITH c AS (SELECT n FROM r WHERE n <= 3) SELECT n FROM c",
                row_cap=100)
    assert res["ok"] is True, res.get("error")
    assert sorted(res["df"]["n"]) == [1, 2, 3]


def test_run_live_select_guard_refusal_builds_no_engine(live_cfg, monkeypatch):
    calls = []

    def no_engine(*a, **kw):
        calls.append(1)
        raise RuntimeError("the engine must not be built")
    monkeypatch.setattr(db_connector, "get_engine", no_engine)
    res = _live(live_cfg, "DELETE FROM r", row_cap=10)
    assert res["ok"] is False
    assert res.get("guard") is True
    assert res["error"]
    assert res["timed_out"] is False
    assert calls == []


def test_run_live_select_bad_column_logs_the_type_only(live_cfg, caplog):
    """Re-pinned when the error was reduced: `error` is the class sentence,
    the driver's text (which quotes the literal) lives in `error_detail`
    ONLY, and the log line carries the class beside the exception type."""
    with caplog.at_level(logging.INFO):
        res = _live(live_cfg,
                    "SELECT no_such_col FROM r WHERE s = 'ZQMARK_ERR_9021'",
                    row_cap=10)
    assert res["ok"] is False
    assert res["error"] == "The query names a column that does not exist."
    assert res["error_class"] == "unknown_column"
    assert "ZQMARK_ERR_9021" not in res["error"]
    assert "ZQMARK_ERR_9021" in res["error_detail"]
    recs = _records(caplog, "LIVE_QUERY_ERROR")
    assert recs, "no LIVE_QUERY_ERROR line"
    assert "error_type=" in recs[0].getMessage()
    assert "class=unknown_column" in recs[0].getMessage()
    for r in caplog.records:
        assert "ZQMARK_ERR_9021" not in r.getMessage()
        assert "no_such_col" not in r.getMessage()


def test_run_live_select_success_logs_a_hash_not_the_sql(live_cfg, caplog):
    with caplog.at_level(logging.INFO):
        res = _live(live_cfg, "SELECT n FROM r WHERE s <> 'ZQMARK_OK_5510'",
                    row_cap=100)
    assert res["ok"] is True
    recs = _records(caplog, "LIVE_QUERY_OK")
    assert recs, "no LIVE_QUERY_OK line"
    assert re.search(r"sql_hash=[0-9a-f]{16}\b", recs[0].getMessage())
    for r in caplog.records:
        assert "ZQMARK_OK_5510" not in r.getMessage()
        assert "SELECT n FROM r" not in r.getMessage()


def test_run_live_select_timeout_is_flagged(live_cfg, monkeypatch):
    def boom(*a, **kw):
        raise TimeoutError("statement timed out")
    monkeypatch.setattr(db_connector, "get_engine", boom)
    res = _live(live_cfg, "SELECT n FROM r", row_cap=10)
    assert res["ok"] is False
    assert res["timed_out"] is True
    assert res["error"]


def test_run_live_select_bounds_the_statement_timeout(live_cfg, monkeypatch):
    monkeypatch.setattr(db_connector.settings, "DB_STATEMENT_TIMEOUT", 300)
    _set_setting(monkeypatch, "LIVE_QUERY_TIMEOUT_S", 60)
    seen = _engine_spy(monkeypatch)
    assert _live(live_cfg, "SELECT n FROM r", row_cap=10)["ok"] is True
    assert seen and seen[-1]["statement_timeout"] == 60
    assert "statement_timeout" not in live_cfg          # caller's cfg untouched
    _live(live_cfg, "SELECT n FROM r", row_cap=10, timeout_s=5)
    assert seen[-1]["statement_timeout"] == 5
    low = dict(live_cfg, statement_timeout=10)
    _live(low, "SELECT n FROM r", row_cap=10)
    assert seen[-1]["statement_timeout"] == 10


def test_run_live_select_dialect_mismatch_is_an_error(live_cfg):
    res = _live(live_cfg, "SELECT n FROM r", dialect="postgresql", row_cap=10)
    assert res["ok"] is False
    assert res["error"]


def test_run_live_select_default_cap_comes_from_settings(live_cfg, monkeypatch):
    _set_setting(monkeypatch, "LIVE_RESULT_ROW_CAP", 3)
    res = _live(live_cfg, "SELECT n FROM r")
    assert res["ok"] is True
    assert len(res["df"]) == 3
    assert res["truncated"] is True


@pytest.mark.parametrize("cfg, sql", [
    ({}, "SELECT 1"),
    ({"db_type": "nope"}, "SELECT 1"),
    ({"db_type": "sqlite", "url_override": "sqlite+pysqlite:///"}, None),
])
def test_run_live_select_never_raises(cfg, sql):
    res = db_connector.run_live_select(cfg, "", sql, sid="t")
    assert isinstance(res, dict)
    assert res["ok"] is False


# ===========================================================================
# settings
# ===========================================================================
def test_live_query_settings_defaults(monkeypatch):
    from settings import Settings
    monkeypatch.delenv("LIVE_RESULT_ROW_CAP", raising=False)
    monkeypatch.delenv("LIVE_QUERY_TIMEOUT_S", raising=False)
    s = Settings()
    assert getattr(s, "LIVE_RESULT_ROW_CAP", None) == 200_000
    assert getattr(s, "LIVE_QUERY_TIMEOUT_S", None) == 60


def test_live_query_settings_env_values_are_read(monkeypatch):
    from settings import Settings
    monkeypatch.setenv("LIVE_RESULT_ROW_CAP", "5000")
    monkeypatch.setenv("LIVE_QUERY_TIMEOUT_S", "7")
    s = Settings()
    assert getattr(s, "LIVE_RESULT_ROW_CAP", None) == 5000
    assert getattr(s, "LIVE_QUERY_TIMEOUT_S", None) == 7


@pytest.mark.parametrize("raw", ["abc", "", "1e6"])
def test_live_query_settings_garbage_falls_back(monkeypatch, raw):
    from settings import Settings
    monkeypatch.setenv("LIVE_RESULT_ROW_CAP", raw)
    monkeypatch.setenv("LIVE_QUERY_TIMEOUT_S", raw)
    s = Settings()
    assert getattr(s, "LIVE_RESULT_ROW_CAP", None) == 200_000
    assert getattr(s, "LIVE_QUERY_TIMEOUT_S", None) == 60


@pytest.mark.parametrize("raw", ["0", "-3"])
def test_live_query_settings_floor_is_one(monkeypatch, raw):
    from settings import Settings
    monkeypatch.setenv("LIVE_RESULT_ROW_CAP", raw)
    monkeypatch.setenv("LIVE_QUERY_TIMEOUT_S", raw)
    s = Settings()
    assert getattr(s, "LIVE_RESULT_ROW_CAP", None) == 1
    assert getattr(s, "LIVE_QUERY_TIMEOUT_S", None) == 1


# ===========================================================================
# structural
# ===========================================================================
def test_the_old_guard_name_is_gone():
    assert not hasattr(db_connector, "_assert_single_select")
    assert callable(getattr(db_connector, "assert_read_only_query", None))


def test_sqlglot_is_imported_function_locally():
    tree = ast.parse(Path(db_connector.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] == "sqlglot" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "sqlglot"


_REGISTRY_SQL_FIELDS = {"select1_sql", "row_count_sql", "table_size_sql"}


def _free_form_text_sites(path: Path) -> set:
    """Enclosing function names of every SQLAlchemy `text(<expression>)` call
    whose argument is built at run time. Excluded: a string constant, an
    f-string (the SET helpers — pinned by the literal guard in
    test_db_connector_sqlite.py) and a registry literal (`d.select1_sql` etc.)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and \
                (node.module or "").split(".")[0] == "sqlalchemy":
            for a in node.names:
                if a.name == "text":
                    aliases.add(a.asname or a.name)
    sites = set()

    def visit(node, fn_name):
        for child in ast.iter_child_nodes(node):
            name = fn_name
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Call) and child.args:
                f = child.func
                is_text = (isinstance(f, ast.Name) and f.id in aliases) or (
                    isinstance(f, ast.Attribute) and f.attr == "text"
                    and isinstance(f.value, ast.Name) and f.value.id in
                    ("sqlalchemy", "sa"))
                arg = child.args[0]
                literal = isinstance(arg, (ast.Constant, ast.JoinedStr)) or (
                    isinstance(arg, ast.Attribute)
                    and arg.attr in _REGISTRY_SQL_FIELDS)
                if is_text and not literal:
                    sites.add(name)
            visit(child, name)

    visit(tree, "<module>")
    return sites


def test_run_live_select_is_the_only_free_form_sql_executor():
    """Outside the admin WHERE fragment (`_select_stmt`, compiled through the
    strict guard), the one place a run-time SQL string becomes an executable
    SQLAlchemy `text()` is `run_live_select`."""
    skip = {"tests", ".venv", "venv", "node_modules", ".git", "__pycache__"}
    found = {}
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).parts
        if set(rel) & skip:
            continue
        sites = _free_form_text_sites(path)
        if sites:
            found[path.relative_to(ROOT).as_posix()] = sites
    assert found == {"db_connector.py": {"_select_stmt", "run_live_select"}}, found


def test_sqlglot_stays_out_of_the_executor_image():
    lines = (ROOT / "executor" / "requirements.txt").read_text(
        encoding="utf-8").splitlines()
    pins = [ln.strip() for ln in lines
            if ln.strip() and not ln.strip().startswith("#")]
    assert not any(re.match(r"sqlglot\b", p, re.IGNORECASE) for p in pins)


# ===========================================================================
# further refusals: server settings, table hints, network / file primitives
# ===========================================================================
_CH_TABLE_FUNCTIONS = ("s3Cluster", "hdfs", "hdfsCluster", "azureBlobStorage",
                       "gcs", "urlCluster", "mongodb", "redis", "sqlite",
                       "deltaLake", "iceberg", "hudi")

# (id, sql, dialect) — every entry parses under the pinned sqlglot, so each
# must refuse under BOTH strict_parse values.
NEGATIVE_MORE = [
    ("ch-settings", "SELECT * FROM t SETTINGS max_execution_time = 0",
     "clickhouse"),
    ("ch-settings-in-subquery",
     "SELECT * FROM (SELECT a FROM t SETTINGS readonly = 0) q", "clickhouse"),
    ("tsql-tablockx-holdlock", "SELECT * FROM t WITH (TABLOCKX, HOLDLOCK)",
     "mssql"),
    ("tsql-updlock", "SELECT a FROM t WITH (UPDLOCK)", "mssql"),
    ("oracle-httpuritype",
     "SELECT HTTPURITYPE('http://x').getclob() FROM dual", "oracle"),
    ("oracle-utl-tcp", "SELECT UTL_TCP.open_connection('x', 80) FROM dual",
     "oracle"),
    ("oracle-utl-smtp", "SELECT UTL_SMTP.open_connection('x') FROM dual",
     "oracle"),
    ("oracle-utl-mail", "SELECT UTL_MAIL.send('a','b') FROM dual", "oracle"),
    ("oracle-dbms-ldap", "SELECT DBMS_LDAP.init('x', 389) FROM dual", "oracle"),
    ("oracle-dbms-java", "SELECT DBMS_JAVA.runjava('x') FROM dual", "oracle"),
    ("oracle-dbms-advisor",
     "SELECT DBMS_ADVISOR.CREATE_FILE('x','D','f') FROM dual", "oracle"),
    ("oracle-dbms-xslprocessor",
     "SELECT DBMS_XSLPROCESSOR.read2clob('D','f') FROM dual", "oracle"),
    ("oracle-extractvalue-xmltype",
     "SELECT EXTRACTVALUE(XMLTYPE('<x/>'), '/x') FROM dual", "oracle"),
] + [(f"ch-{fn}", f"SELECT * FROM {fn}('x')", "clickhouse")
     for fn in _CH_TABLE_FUNCTIONS] + [
    ("pg-dblink-connect", "SELECT dblink_connect('x')", "postgresql"),
    ("pg-dblink-send-query", "SELECT * FROM dblink_send_query('c','SELECT 1')",
     "postgresql"),
    ("pg-dblink-open", "SELECT dblink_open('c','SELECT 1')", "postgresql"),
    ("pg-dblink-fetch", "SELECT * FROM dblink_fetch('c', 10) AS t(a int)",
     "postgresql"),
    ("pg-query-to-xml", "SELECT query_to_xml('SELECT 1', true, false, '')",
     "postgresql"),
    ("pg-query-to-xmlschema",
     "SELECT query_to_xmlschema('SELECT 1', true, false, '')", "postgresql"),
    ("pg-cursor-to-xml", "SELECT cursor_to_xml('c', 10, true, false, '')",
     "postgresql"),
]


# T-SQL table hints written without WITH, and T-SQL command words that the
# parser would otherwise read as a bare alias.
NEGATIVE_MORE += [
    ("tsql-hint-no-with-tablockx",
     "SELECT * FROM t (TABLOCKX, HOLDLOCK)", "mssql"),
    ("tsql-hint-no-with-nolock", "SELECT * FROM t (NOLOCK)", "mssql"),
    ("tsql-hint-after-as-alias", "SELECT * FROM t AS x (TABLOCKX)", "mssql"),
    ("tsql-hint-after-bare-alias", "SELECT a FROM t x (UPDLOCK)", "mssql"),
    ("tsql-alias-shutdown", "SELECT 1 SHUTDOWN", "mssql"),
    ("tsql-alias-checkpoint", "SELECT 1 CHECKPOINT", "mssql"),
    ("tsql-alias-reconfigure", "SELECT 1 RECONFIGURE", "mssql"),
    ("tsql-table-alias-shutdown", "SELECT a FROM t SHUTDOWN", "mssql"),
    ("tsql-table-alias-kill", "SELECT a FROM t KILL", "mssql"),
]

# Function families: dblink variants, advisory locks, sequence writers, file
# stat, named locks, BFILE reads, and the ClickHouse cluster / object-store
# table functions plus a per-row sleep.
NEGATIVE_MORE += [
    ("pg-dblink-connect-u", "SELECT dblink_connect_u('c','x')", "postgresql"),
    ("pg-dblink-get-result", "SELECT dblink_get_result('c')", "postgresql"),
    ("pg-advisory-lock", "SELECT pg_advisory_lock(1)", "postgresql"),
    ("pg-advisory-xact-lock", "SELECT pg_advisory_xact_lock(1)", "postgresql"),
    ("pg-try-advisory-lock", "SELECT pg_try_advisory_lock(1)", "postgresql"),
    ("pg-nextval", "SELECT nextval('s')", "postgresql"),
    ("pg-setval", "SELECT setval('s', 1)", "postgresql"),
    ("pg-stat-file", "SELECT pg_stat_file('x')", "postgresql"),
    ("mysql-get-lock", "SELECT GET_LOCK('x', 10)", "mysql"),
    ("oracle-bfilename",
     "SELECT DBMS_LOB.substr(BFILENAME('D','f')) FROM dual", "oracle"),
] + [(f"ch-{fn}", f"SELECT * FROM {fn}('x')", "clickhouse")
     for fn in ("icebergS3", "icebergAzure", "icebergHDFS", "icebergS3Cluster",
                "deltaLakeCluster", "hudiCluster", "azureBlobStorageCluster",
                "fileCluster", "oss", "cosn")] + [
    ("ch-sleep-each-row", "SELECT sleepEachRow(3) FROM t", "clickhouse"),
]

# Look-alikes that must keep passing: aliases without a hint, a quoted alias
# spelled like a command word, and ClickHouse functions whose names merely
# start with url / file.
POSITIVE_MORE = [
    ("tsql-as-alias", "SELECT a FROM t AS x", "mssql"),
    ("tsql-bare-alias", "SELECT a FROM dbo.t x", "mssql"),
    ("tsql-quoted-command-word-alias", "SELECT 1 AS [shutdown]", "mssql"),
    ("ch-urlhash", "SELECT URLHash(u) FROM t", "clickhouse"),
    ("ch-urlhierarchy", "SELECT URLHierarchy(u) FROM t", "clickhouse"),
    ("ch-filesystem-available", "SELECT filesystemAvailable()", "clickhouse"),
]

def test_every_further_refusal_parses_under_the_pinned_sqlglot():
    """Precondition: the parse layer sees each of these, so the lenient mode
    cannot let one through as a mere parse warning."""
    import sqlglot
    names = {"postgresql": "postgres", "mssql": "tsql"}
    for _id, sql, dialect in NEGATIVE_MORE:
        assert sqlglot.parse(sql, read=names.get(dialect, dialect)), _id


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "lenient"])
@pytest.mark.parametrize("sql, dialect", [c[1:] for c in NEGATIVE_MORE],
                         ids=[c[0] for c in NEGATIVE_MORE])
def test_further_refusals(sql, dialect, strict):
    with pytest.raises(ValueError):
        _guard(sql, allow_cte=True, strict_parse=strict, dialect=dialect)


# ===========================================================================
# comment stripping is literal-aware
# ===========================================================================
@pytest.mark.parametrize("sql", [
    "SELECT 1 WHERE 'a' = '/*' AND 'b' = '*/'",
    "SELECT 'it''s /* not */ a comment'",
    "SELECT 'x -- y' AS s FROM t",
], ids=["block-markers-in-literals", "escaped-quote", "line-marker-in-literal"])
def test_strip_keeps_comment_markers_inside_string_literals(sql):
    assert db_connector._strip_sql_comments(sql) == sql


def test_strip_keeps_a_literal_and_drops_the_trailing_comment():
    out = db_connector._strip_sql_comments("SELECT 'a--b' AS s -- trailing")
    assert "'a--b'" in out
    assert "trailing" not in out


def test_strip_keeps_a_quoted_identifier():
    out = db_connector._strip_sql_comments('SELECT "x--y" FROM t')
    assert '"x--y"' in out
    assert "FROM t" in out


def test_strip_still_removes_real_comments():
    out = db_connector._strip_sql_comments(
        "SELECT a /* block */ , 'k' AS b FROM t -- line\nWHERE a > 1")
    assert "block" not in out and "line" not in out
    assert "/*" not in out and "--" not in out
    assert "'k'" in out and "WHERE a > 1" in out


def test_wrap_keeps_comment_markers_inside_literals_verbatim():
    inner = "SELECT 1 AS n WHERE 'a' = '/*' AND 'b' = '*/'"
    out = db_connector.wrap_with_row_limit(inner, "postgresql", 11)
    assert f"({inner})" in out


def test_wrap_removes_a_mysql_optimizer_hint():
    out = db_connector.wrap_with_row_limit(
        "SELECT /*+ MAX_EXECUTION_TIME(0) */ a FROM t", "mysql", 5)
    assert "MAX_EXECUTION_TIME" not in out
    assert out.endswith(") AS pdc_q LIMIT 5")


def test_run_live_select_double_dash_inside_a_literal(live_cfg):
    eng = create_engine(live_cfg["url_override"])
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO r VALUES (:n, :s)"),
                     {"n": 26, "s": "a--b"})
    eng.dispose()
    res = _live(live_cfg, "SELECT s FROM r WHERE s = 'a--b'", row_cap=100)
    assert res["ok"] is True, res.get("error")
    assert list(res["df"]["s"]) == ["a--b"]


# ===========================================================================
# mssql: an ORDER BY closing a UNION also needs OFFSET inside a subquery
# ===========================================================================
def test_wrap_mssql_union_with_order_by_gets_offset_inside():
    out = db_connector.wrap_with_row_limit(
        "SELECT a FROM t UNION SELECT a FROM u ORDER BY a", "mssql", 5)
    assert out == ("SELECT TOP 5 * FROM (SELECT a FROM t UNION SELECT a FROM u "
                   "ORDER BY a OFFSET 0 ROWS) AS pdc_q")


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "lenient"])
@pytest.mark.parametrize("sql, dialect", [c[1:] for c in POSITIVE_MORE],
                         ids=[c[0] for c in POSITIVE_MORE])
def test_further_look_alikes_pass(sql, dialect, strict):
    assert _guard(sql, allow_cte=True, strict_parse=strict,
                  dialect=dialect) is None


# ===========================================================================
# the table allowlist: a live SELECT may read ONLY the table it was written for
# ===========================================================================
_ALLOWED = {("shop", "t"), ("", "t")}

ALLOWLIST_OK = [
    ("schema-qualified", "SELECT a FROM shop.t"),
    ("unqualified", "SELECT a FROM t"),
    ("case-insensitive", "SELECT a FROM SHOP.T"),
    ("cte-alias-shadows-a-disallowed-name",
     "WITH u AS (SELECT a FROM shop.t) SELECT a FROM u"),
    ("nested-with",
     "SELECT * FROM (WITH c AS (SELECT a FROM shop.t) SELECT a FROM c) q"),
    ("self-join", "SELECT x.a FROM shop.t x JOIN t y ON x.a = y.a"),
    ("subquery-same-table",
     "SELECT a FROM shop.t WHERE a IN (SELECT a FROM t)"),
]

ALLOWLIST_REFUSED = [
    ("other-table", "SELECT a FROM shop.u"),
    ("join-to-other-table", "SELECT t.a FROM shop.t JOIN shop.u ON t.a = u.a"),
    ("subquery-other-table",
     "SELECT a FROM shop.t WHERE a IN (SELECT a FROM shop.u)"),
    ("union-to-catalog",
     "SELECT a FROM shop.t UNION SELECT relname FROM pg_catalog.pg_class"),
    ("same-name-other-schema", "SELECT a FROM hr.t"),
    ("cte-body-names-other-table",
     "WITH c AS (SELECT a FROM shop.u) SELECT a FROM c"),
]


@pytest.mark.parametrize("sql", [c[1] for c in ALLOWLIST_OK],
                         ids=[c[0] for c in ALLOWLIST_OK])
def test_allowlist_accepts_the_registered_table(sql):
    assert _guard(sql, dialect="postgresql", allowed_tables=_ALLOWED) is None


@pytest.mark.parametrize("sql", [c[1] for c in ALLOWLIST_REFUSED],
                         ids=[c[0] for c in ALLOWLIST_REFUSED])
def test_allowlist_refuses_any_other_table(sql):
    with pytest.raises(ValueError) as ei:
        _guard(sql, dialect="postgresql", allowed_tables=_ALLOWED)
    assert re.fullmatch(r"Table '[^']+' is not permitted\.", str(ei.value)), \
        str(ei.value)


def test_allowlist_message_names_the_table_only():
    with pytest.raises(ValueError) as ei:
        _guard("SELECT a FROM shop.u WHERE b = 'ZQMARK_4471'",
               dialect="postgresql", allowed_tables=_ALLOWED)
    assert str(ei.value) == "Table 'u' is not permitted."


def test_allowlist_accepts_the_database_qualified_form_when_listed():
    """MySQL / ClickHouse registrations carry no schema and the planner may
    qualify with the database name — the caller lists that pair too."""
    with pytest.raises(ValueError):
        _guard("SELECT a FROM mydb.t", dialect="mysql", allowed_tables={("", "t")})
    assert _guard("SELECT a FROM mydb.t", dialect="mysql",
                  allowed_tables={("", "t"), ("mydb", "t")}) is None


def test_allowlist_none_leaves_the_guard_unrestricted():
    assert _guard("SELECT a FROM shop.u UNION SELECT relname FROM pg_catalog.pg_class",
                  dialect="postgresql", allowed_tables=None) is None


def test_allowlist_refuses_in_lenient_mode_too():
    with pytest.raises(ValueError):
        _guard("SELECT a FROM shop.u", strict_parse=False, dialect="postgresql",
               allowed_tables=_ALLOWED)


# ===========================================================================
# classify_db_error: a driver failure becomes one of the error classes
# ===========================================================================
class _Orig(Exception):
    """A DBAPI exception look-alike; the driver-specific code rides on it."""


class _Wrapped(Exception):
    """A SQLAlchemy DBAPIError look-alike: the driver exception on `.orig`."""

    def __init__(self, orig):
        super().__init__("wrapped")
        self.orig = orig


def _pg(code):
    o = _Orig("driver text")
    o.pgcode = code
    return _Wrapped(o)


def _mysql(errno):
    return _Wrapped(_Orig(errno, "driver text"))


def _odbc(state):
    return _Wrapped(_Orig(state, "driver text"))


def _ora(full_code):
    class _OracleError:
        pass
    e = _OracleError()
    e.full_code = full_code
    e.message = "driver text"
    return _Wrapped(_Orig(e))


def _ch(code):
    o = _Orig("driver text")
    o.code = code
    return _Wrapped(o)


def _chained_timeout():
    e = _Wrapped(_Orig("driver text"))
    e.__cause__ = TimeoutError("socket timed out")
    return e


CLASSIFY = [
    ("pg-unknown-column", _pg("42703"), "unknown_column"),
    ("pg-unknown-table", _pg("42P01"), "unknown_table"),
    ("pg-syntax", _pg("42601"), "syntax"),
    ("pg-permission", _pg("42501"), "permission"),
    ("pg-timeout", _pg("57014"), "timeout"),
    ("mysql-unknown-column", _mysql(1054), "unknown_column"),
    ("mysql-unknown-table", _mysql(1146), "unknown_table"),
    ("mysql-syntax", _mysql(1064), "syntax"),
    ("mysql-permission-1142", _mysql(1142), "permission"),
    ("mysql-permission-1044", _mysql(1044), "permission"),
    ("mysql-permission-1045", _mysql(1045), "permission"),
    ("mysql-timeout", _mysql(3024), "timeout"),
    ("odbc-unknown-column", _odbc("42S22"), "unknown_column"),
    ("odbc-unknown-table", _odbc("42S02"), "unknown_table"),
    ("odbc-syntax", _odbc("42000"), "syntax"),
    ("odbc-timeout", _odbc("HYT00"), "timeout"),
    ("odbc-permission", _odbc("28000"), "permission"),
    ("ora-unknown-column", _ora("ORA-00904"), "unknown_column"),
    ("ora-unknown-table", _ora("ORA-00942"), "unknown_table"),
    ("ora-syntax-00900", _ora("ORA-00900"), "syntax"),
    ("ora-syntax-00907", _ora("ORA-00907"), "syntax"),
    ("ora-syntax-00936", _ora("ORA-00936"), "syntax"),
    ("ora-timeout", _ora("ORA-01013"), "timeout"),
    ("ora-permission", _ora("ORA-01031"), "permission"),
    ("ch-unknown-column", _ch(47), "unknown_column"),
    ("ch-unknown-table", _ch(60), "unknown_table"),
    ("ch-syntax", _ch(62), "syntax"),
    ("ch-timeout", _ch(159), "timeout"),
    ("ch-permission", _ch(497), "permission"),
    ("sqlite-unknown-column", _Wrapped(_Orig("no such column: nope")),
     "unknown_column"),
    ("sqlite-unknown-table", _Wrapped(_Orig("no such table: zz")),
     "unknown_table"),
    ("sqlite-syntax", _Wrapped(_Orig('near "FROM": syntax error')), "syntax"),
    ("chained-timeout", _chained_timeout(), "timeout"),
    ("bare-timeout", TimeoutError("timed out"), "timeout"),
    ("other-driver-text", _Wrapped(_Orig("something else happened")), "other"),
    ("plain-exception", RuntimeError("plain"), "other"),
    ("none", None, "other"),
]


@pytest.mark.parametrize("exc, expected", [c[1:] for c in CLASSIFY],
                         ids=[c[0] for c in CLASSIFY])
def test_classify_db_error(exc, expected):
    assert db_connector.classify_db_error(exc) == expected


def test_classify_db_error_vocabulary_is_closed():
    assert {c[2] for c in CLASSIFY} == {"syntax", "unknown_column",
                                        "unknown_table", "timeout",
                                        "permission", "other"}


# ===========================================================================
# run_live_select: the reduced error shape
# ===========================================================================
def test_run_live_select_unknown_table_is_classified(live_cfg):
    res = _live(live_cfg, "SELECT n FROM zz_missing WHERE s = 'ZQMARK_TBL_77'",
                row_cap=10)
    assert res["ok"] is False
    assert res["error_class"] == "unknown_table"
    assert res["error"] and "ZQMARK_TBL_77" not in res["error"]
    assert "zz_missing" not in res["error"]
    assert "zz_missing" in res["error_detail"]


def test_run_live_select_guard_refusal_is_class_guard(live_cfg, monkeypatch):
    def no_engine(*a, **kw):
        raise RuntimeError("the engine must not be built")
    monkeypatch.setattr(db_connector, "get_engine", no_engine)
    res = _live(live_cfg, "DELETE FROM r", row_cap=10)
    assert res["ok"] is False
    assert res["guard"] is True
    assert res["error_class"] == "guard"
    assert res["error"] == "Only SELECT statements are permitted."


def test_run_live_select_timeout_is_class_timeout(live_cfg, monkeypatch):
    def boom(*a, **kw):
        raise TimeoutError("statement timed out")
    monkeypatch.setattr(db_connector, "get_engine", boom)
    res = _live(live_cfg, "SELECT n FROM r", row_cap=10)
    assert res["ok"] is False
    assert res["timed_out"] is True
    assert res["error_class"] == "timeout"
    assert res["error"]


def test_run_live_select_allowed_tables_refusal(live_cfg, monkeypatch):
    calls = []

    def no_engine(*a, **kw):
        calls.append(1)
        raise RuntimeError("the engine must not be built")
    monkeypatch.setattr(db_connector, "get_engine", no_engine)
    res = _live(live_cfg, "SELECT n FROM r", row_cap=10,
                allowed_tables={("", "other")})
    assert res["ok"] is False
    assert res["guard"] is True
    assert res["error_class"] == "guard"
    assert res["error"] == "Table 'r' is not permitted."
    assert calls == []


def test_run_live_select_allowed_tables_accepts_the_listed_table(live_cfg):
    res = _live(live_cfg, "SELECT n FROM r", row_cap=100,
                allowed_tables={("", "r"), ("main", "r")})
    assert res["ok"] is True, res.get("error")
    assert len(res["df"]) == 25


def test_sample_rows_carries_an_error_class(live_cfg, monkeypatch):
    def boom(*a, **kw):
        raise TimeoutError("statement timed out")
    monkeypatch.setattr(db_connector, "get_engine", boom)
    res = db_connector.sample_rows(live_cfg, "", None, "r", sid="t")
    assert res["ok"] is False
    assert res["error_class"] == "timeout"


# ===========================================================================
# wrap_with_row_limit hoists a leading CTE block above the outer SELECT
# ===========================================================================
CTE_INNER = "WITH c AS (SELECT a FROM t) SELECT a FROM c"


@pytest.mark.parametrize("key, expected", [
    ("postgresql", "WITH c AS (SELECT a FROM t) SELECT * FROM (SELECT a FROM c) "
                   "AS pdc_q LIMIT 11"),
    ("mysql", "WITH c AS (SELECT a FROM t) SELECT * FROM (SELECT a FROM c) "
              "AS pdc_q LIMIT 11"),
    ("mariadb", "WITH c AS (SELECT a FROM t) SELECT * FROM (SELECT a FROM c) "
                "AS pdc_q LIMIT 11"),
    ("clickhouse", "WITH c AS (SELECT a FROM t) SELECT * FROM (SELECT a FROM c) "
                   "AS pdc_q LIMIT 11"),
    ("sqlite", "WITH c AS (SELECT a FROM t) SELECT * FROM (SELECT a FROM c) "
               "AS pdc_q LIMIT 11"),
    ("mssql", "WITH c AS (SELECT a FROM t) SELECT TOP 11 * FROM (SELECT a FROM c) "
              "AS pdc_q"),
    ("oracle", "WITH c AS (SELECT a FROM t) SELECT * FROM (SELECT a FROM c) "
               "pdc_q FETCH FIRST 11 ROWS ONLY"),
])
def test_wrap_hoists_a_cte_per_dialect(key, expected):
    assert db_connector.wrap_with_row_limit(CTE_INNER, key, 11) == expected


def test_wrap_hoists_a_recursive_cte():
    inner = ("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r "
             "WHERE n < 3) SELECT n FROM r")
    out = db_connector.wrap_with_row_limit(inner, "postgresql", 11)
    assert out == ("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r "
                   "WHERE n < 3) SELECT * FROM (SELECT n FROM r) AS pdc_q LIMIT 11")


def test_wrap_hoists_two_ctes():
    inner = "WITH a AS (SELECT 1 AS x), b AS (SELECT 2 AS y) SELECT x FROM a"
    out = db_connector.wrap_with_row_limit(inner, "postgresql", 11)
    assert out == ("WITH a AS (SELECT 1 AS x), b AS (SELECT 2 AS y) "
                   "SELECT * FROM (SELECT x FROM a) AS pdc_q LIMIT 11")


def test_wrap_hoisted_cte_mssql_order_by_gets_offset_inside():
    out = db_connector.wrap_with_row_limit(
        "WITH c AS (SELECT a FROM t) SELECT a FROM c ORDER BY a", "mssql", 11)
    assert out == ("WITH c AS (SELECT a FROM t) SELECT TOP 11 * FROM "
                   "(SELECT a FROM c ORDER BY a OFFSET 0 ROWS) AS pdc_q")


def test_wrap_hoisted_cte_text_is_verbatim():
    """Odd spacing and case inside the CTE block survive: neither half is
    re-rendered."""
    inner = "with  c   as (select a   from t)  SELECT a FROM c"
    out = db_connector.wrap_with_row_limit(inner, "postgresql", 11)
    assert out.startswith("with  c   as (select a   from t)")
    assert out.endswith("SELECT * FROM (SELECT a FROM c) AS pdc_q LIMIT 11")


def test_wrap_hoists_a_clickhouse_expression_cte():
    out = db_connector.wrap_with_row_limit("WITH 1 AS x SELECT x", "clickhouse", 11)
    assert out == "WITH 1 AS x SELECT * FROM (SELECT x) AS pdc_q LIMIT 11"


def test_wrap_hoists_a_cte_whose_body_is_a_union():
    inner = "WITH c AS (SELECT a FROM t) SELECT a FROM c UNION SELECT a FROM u"
    out = db_connector.wrap_with_row_limit(inner, "postgresql", 11)
    assert out == ("WITH c AS (SELECT a FROM t) SELECT * FROM "
                   "(SELECT a FROM c UNION SELECT a FROM u) AS pdc_q LIMIT 11")


def test_wrap_without_a_cte_is_unchanged_by_the_hoist():
    assert db_connector.wrap_with_row_limit(INNER, "postgresql", 11) == \
        f"SELECT * FROM ({INNER}) AS pdc_q LIMIT 11"


def test_run_live_select_sends_the_hoisted_form(live_cfg, monkeypatch):
    captured = []

    def fake_read_sql(sql, con, *a, **kw):
        captured.append(str(getattr(sql, "text", sql)))
        frame = pd.DataFrame({"n": [1]})
        return iter([frame]) if kw.get("chunksize") else frame

    monkeypatch.setattr(db_connector.pd, "read_sql", fake_read_sql)
    res = _live(live_cfg, "WITH c AS (SELECT n FROM r WHERE n <= 3) SELECT n FROM c",
                row_cap=10)
    assert res["ok"] is True, res.get("error")
    assert captured, "pd.read_sql was never called"
    assert captured[-1] == ("WITH c AS (SELECT n FROM r WHERE n <= 3) "
                            "SELECT * FROM (SELECT n FROM c) AS pdc_q LIMIT 11")


def test_run_live_select_cte_on_sqlite_still_returns_the_rows(live_cfg):
    res = _live(live_cfg,
                "WITH c AS (SELECT n FROM r WHERE n <= 3) SELECT n FROM c ORDER BY n",
                row_cap=100)
    assert res["ok"] is True, res.get("error")
    assert list(res["df"]["n"]) == [1, 2, 3]


# ===========================================================================
# the result size cap (LIVE_RESULT_MAX_MB)
# ===========================================================================
def test_live_result_max_mb_default(monkeypatch):
    from settings import Settings
    monkeypatch.delenv("LIVE_RESULT_MAX_MB", raising=False)
    assert getattr(Settings(), "LIVE_RESULT_MAX_MB", None) == 256


def test_live_result_max_mb_env_value_is_read(monkeypatch):
    from settings import Settings
    monkeypatch.setenv("LIVE_RESULT_MAX_MB", "3")
    assert getattr(Settings(), "LIVE_RESULT_MAX_MB", None) == 3


@pytest.mark.parametrize("raw", ["abc", "", "1e6"])
def test_live_result_max_mb_garbage_falls_back(monkeypatch, raw):
    from settings import Settings
    monkeypatch.setenv("LIVE_RESULT_MAX_MB", raw)
    assert getattr(Settings(), "LIVE_RESULT_MAX_MB", None) == 256


@pytest.mark.parametrize("raw", ["0", "-3"])
def test_live_result_max_mb_floor_is_one(monkeypatch, raw):
    from settings import Settings
    monkeypatch.setenv("LIVE_RESULT_MAX_MB", raw)
    assert getattr(Settings(), "LIVE_RESULT_MAX_MB", None) == 1


@pytest.fixture
def wide_cfg(tmp_path):
    """sqlite table w(n, s): 3000 rows of 1000-character text (about 3 MB in
    memory) — under the row cap, over a 1 MB size cap."""
    db = tmp_path / "wide.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE w (n INTEGER PRIMARY KEY, s TEXT)"))
        payload = "x" * 1000
        for i in range(1, 3001):
            conn.execute(text("INSERT INTO w VALUES (:n, :s)"),
                         {"n": i, "s": payload})
    eng.dispose()
    return {"db_type": "sqlite", "url_override": f"sqlite+pysqlite:///{db}"}


def test_run_live_select_truncates_by_bytes(wide_cfg, monkeypatch):
    _set_setting(monkeypatch, "LIVE_RESULT_MAX_MB", 1)
    res = _live(wide_cfg, "SELECT n, s FROM w ORDER BY n", row_cap=100_000)
    assert res["ok"] is True, res.get("error")
    assert res["truncated"] is True
    assert res["truncated_by"] == "bytes"
    assert 0 < len(res["df"]) < 3000
    assert res["rows"] == len(res["df"])
    assert res["df"]["s"].map(len).eq(1000).all()


def test_run_live_select_truncated_by_names_the_row_cap(live_cfg):
    res = _live(live_cfg, "SELECT n FROM r ORDER BY n", row_cap=10)
    assert res["truncated"] is True
    assert res["truncated_by"] == "rows"
    ok = _live(live_cfg, "SELECT n FROM r", row_cap=100)
    assert ok["truncated"] is False
    assert not ok.get("truncated_by")


# ===========================================================================
# denylist additions: notifications, server control, WAL, large objects,
# session settings, named locks, audit / trace file readers, network ACLs,
# web-service calls
# ===========================================================================
NEGATIVE_MORE += [
    ("pg-notify", "SELECT pg_notify('c', 'p')", "postgresql"),
    ("pg-reload-conf", "SELECT pg_reload_conf()", "postgresql"),
    ("pg-switch-wal", "SELECT pg_switch_wal()", "postgresql"),
    ("pg-create-restore-point", "SELECT pg_create_restore_point('x')",
     "postgresql"),
    ("pg-lo-unlink", "SELECT lo_unlink(1)", "postgresql"),
    ("pg-set-config", "SELECT set_config('x', 'y', false)", "postgresql"),
    ("mysql-release-lock", "SELECT RELEASE_LOCK('x')", "mysql"),
    ("tsql-fn-xe-file-target-read-file",
     "SELECT * FROM fn_xe_file_target_read_file('x', NULL, NULL, NULL)", "mssql"),
    ("tsql-fn-get-audit-file",
     "SELECT * FROM fn_get_audit_file('x', NULL, NULL)", "mssql"),
    ("oracle-dbms-network-acl-admin",
     "SELECT DBMS_NETWORK_ACL_ADMIN.check_privilege('a', 'b', 'c') FROM dual",
     "oracle"),
    ("oracle-utl-dbws", "SELECT UTL_DBWS.create_service('x') FROM dual", "oracle"),
    ("pg-lo-get", "SELECT lo_get(16384)", "postgresql"),
    ("pg-lo-get-slice", "SELECT lo_get(16384, 0, 100)", "postgresql"),
    ("pg-loread", "SELECT loread(0, 100)", "postgresql"),
    ("pg-lo-put", "SELECT lo_put(16384, 0, 'x')", "postgresql"),
]

_ADDED_IDS = {"pg-notify", "pg-reload-conf", "pg-switch-wal",
              "pg-create-restore-point", "pg-lo-unlink", "pg-set-config",
              "mysql-release-lock", "tsql-fn-xe-file-target-read-file",
              "tsql-fn-get-audit-file", "oracle-dbms-network-acl-admin",
              "oracle-utl-dbws", "pg-lo-get", "pg-lo-get-slice", "pg-loread",
              "pg-lo-put"}


def test_denylist_additions_are_present():
    for name in ("pg_notify", "pg_reload_conf", "pg_switch_wal",
                 "pg_create_restore_point", "lo_unlink", "set_config",
                 "release_lock", "fn_xe_file_target_read_file",
                 "fn_get_audit_file", "dbms_network_acl_admin", "utl_dbws",
                 "lo_get", "loread", "lo_put"):
        assert db_connector._is_denied_function(name), name
        assert db_connector._is_denied_function(name.upper()), name


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "lenient"])
@pytest.mark.parametrize("sql, dialect",
                         [c[1:] for c in NEGATIVE_MORE if c[0] in _ADDED_IDS],
                         ids=[c[0] for c in NEGATIVE_MORE if c[0] in _ADDED_IDS])
def test_denylist_additions_refuse(sql, dialect, strict):
    with pytest.raises(ValueError):
        _guard(sql, allow_cte=True, strict_parse=strict, dialect=dialect)


# ===========================================================================
# a driver's own execution-time-exceeded error is a timeout on every dialect
# ===========================================================================
_DRIVER_TIMEOUTS = [
    ("mysql-3024", _Wrapped(_Orig(
        3024, "Query execution was interrupted, maximum statement execution "
              "time exceeded"))),
    ("pg-57014", _pg("57014")),
    ("odbc-HYT00", _odbc("HYT00")),
]


@pytest.mark.parametrize("exc", [c[1] for c in _DRIVER_TIMEOUTS],
                         ids=[c[0] for c in _DRIVER_TIMEOUTS])
def test_is_timeout_recognises_the_driver_code(exc):
    """The count verdict and the live query share this classifier: a
    statement-time-exceeded error is a timeout by its driver CODE, not only
    by the wording of its message."""
    assert db_connector._is_timeout(exc) is True


@pytest.mark.parametrize("exc", [c[1] for c in _DRIVER_TIMEOUTS],
                         ids=[c[0] for c in _DRIVER_TIMEOUTS])
def test_run_live_select_driver_timeout_is_flagged_and_classified(live_cfg,
                                                                  monkeypatch,
                                                                  exc):
    def boom(*a, **kw):
        raise exc
    monkeypatch.setattr(db_connector, "get_engine", boom)
    res = _live(live_cfg, "SELECT n FROM r", row_cap=10)
    assert res["ok"] is False
    assert res["timed_out"] is True
    assert res["error_class"] == "timeout"
    assert res["error"] == "The live query timed out."
