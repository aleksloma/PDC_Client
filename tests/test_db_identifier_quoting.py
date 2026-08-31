"""Identifier quoting + row limiting are the SQLAlchemy dialect's job.

The bug this file pins: `SELECT * FROM "bsrep"."offering_all" FETCH FIRST 20
ROWS ONLY` -> ORA-00942. SQLAlchemy normalizes Oracle's case-folded identifiers
to LOWERCASE on the way out of the Inspector; hand-quoting that lowercase name
makes it case-sensitive, and the server stores BSREP.OFFERING_ALL.

Oracle / MySQL / MSSQL have no local server, so they are covered by compiling
`_select_stmt` against the REAL SQLAlchemy dialect objects — the same compilers
that run in production. The result-NAME half is proven live through the hidden
sqlite entry against a physically uppercase table (the Oracle analogue).
"""
import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import mssql, mysql, oracle, postgresql
from sqlalchemy.dialects import sqlite as sa_sqlite

import db_connector


def _sql(dialect, *a, **kw):
    """Compile through the production gate and flatten whitespace."""
    return " ".join(
        db_connector._compiled_sql(
            db_connector._select_stmt(*a, **kw), dialect).split())


# ---------------------------------------------------------------------------
# The regression proper
# ---------------------------------------------------------------------------

def test_oracle_does_not_quote_the_normalized_name():
    """THE bug. A lowercase Inspector name must render UNQUOTED so Oracle folds
    it back to BSREP.OFFERING_ALL — quoting it is ORA-00942."""
    sql = _sql(oracle.dialect(), "bsrep", "offering_all", row_cap=20)
    assert '"bsrep"."offering_all"' not in sql
    assert sql == "SELECT * FROM bsrep.offering_all FETCH FIRST 20 ROWS ONLY"


def test_oracle_still_quotes_a_case_sensitive_name():
    """The other half of the convention: a name SQLAlchemy did not fold is
    case-sensitive and must keep its quotes."""
    sql = _sql(oracle.dialect(), "BsRep", "Offering_All")
    assert '"BsRep"."Offering_All"' in sql


def test_oracle_below_12c_uses_the_rownum_wrapper():
    """FETCH FIRST is 12c+; older servers raise ORA-00933. SQLAlchemy picks the
    form from the live server version — and the wrapper must still pass the
    SELECT-only gate."""
    d = oracle.dialect()
    d._supports_offset_fetch = False
    sql = _sql(d, "bsrep", "offering_all", row_cap=20)
    assert "ROWNUM <= 20" in sql and "FETCH FIRST" not in sql


@pytest.mark.parametrize("dialect, expected", [
    (postgresql.dialect(), "SELECT * FROM bsrep.offering_all LIMIT 20"),
    (mysql.dialect(), "SELECT * FROM bsrep.offering_all LIMIT 20"),
    (mssql.dialect(), "SELECT TOP 20 * FROM bsrep.offering_all"),
    (sa_sqlite.dialect(), "SELECT * FROM bsrep.offering_all LIMIT 20"),
])
def test_row_limit_per_dialect(dialect, expected):
    sql = _sql(dialect, "bsrep", "offering_all", row_cap=20)
    assert sql.replace(" OFFSET 0", "") == expected


def test_clickhouse_row_limit():
    pytest.importorskip("clickhouse_sqlalchemy")
    from clickhouse_sqlalchemy.drivers.native.base import ClickHouseDialect_native
    assert (_sql(ClickHouseDialect_native(), "analytics", "events", row_cap=20)
            == "SELECT * FROM analytics.events LIMIT 20")


@pytest.mark.parametrize("dialect, quoted", [
    (postgresql.dialect(), '"MixedCol"'),
    (oracle.dialect(), '"MixedCol"'),
    (mysql.dialect(), "`MixedCol`"),
    (mssql.dialect(), "[MixedCol]"),
])
def test_names_that_need_quoting_still_get_it(dialect, quoted):
    sql = _sql(dialect, None, "orders", columns=["MixedCol", "amount"])
    assert quoted in sql
    # …and one that does not need it is left bare, so folding dialects work.
    assert "amount" in sql


def test_reserved_word_and_embedded_quote_are_escaped():
    pg = postgresql.dialect()
    assert '"order"' in _sql(pg, None, "order")
    assert '"a""b"' in _sql(pg, None, 'a"b')


def test_where_and_row_cap_compose_in_the_right_order():
    sql = _sql(oracle.dialect(), "bsrep", "offering_all",
               where="status = 'X'", row_cap=5)
    assert sql == ("SELECT * FROM bsrep.offering_all WHERE status = 'X' "
                   "FETCH FIRST 5 ROWS ONLY")


def test_the_select_only_gate_still_fires_on_a_bad_where():
    with pytest.raises(ValueError):
        db_connector._compiled_sql(
            db_connector._select_stmt(None, "orders",
                                      where="1=1; DROP TABLE x"),
            postgresql.dialect())


# ---------------------------------------------------------------------------
# Catalog estimates — bound as VALUES, so they need the reverse mapping
# ---------------------------------------------------------------------------

def test_catalog_name_denormalizes_only_for_oracle():
    assert db_connector._catalog_name(oracle.dialect(), "bsrep") == "BSREP"
    for d in (postgresql.dialect(), mysql.dialect(), mssql.dialect(),
              sa_sqlite.dialect()):
        assert db_connector._catalog_name(d, "bsrep") == "bsrep"
    assert db_connector._catalog_name(oracle.dialect(), None) is None
    # A case-sensitive Oracle name is stored as typed — must not be uppercased.
    assert db_connector._catalog_name(oracle.dialect(), "BsRep") == "BsRep"


# ---------------------------------------------------------------------------
# Result NAMES — proven live, through a physically uppercase table
# ---------------------------------------------------------------------------

@pytest.fixture
def upper_cfg(tmp_path):
    """A sqlite DB whose physical columns are UPPERCASE: selecting them with
    the lowercase names an Oracle Inspector would hand us reproduces exactly
    the casing mismatch, without an Oracle server."""
    db = tmp_path / "u.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE ORDERS (ORDER_ID INTEGER, "
                          "AMOUNT REAL, CREATED_AT TEXT)"))
        for i in range(4):
            conn.execute(text(
                "INSERT INTO ORDERS VALUES "
                f"({i}, {i * 2.0}, '2026-0{i + 1}-01')"))
    eng.dispose()
    return {"db_type": "sqlite", "url_override": f"sqlite+pysqlite:///{db}"}


def test_preview_without_columns_returns_the_drivers_casing(upper_cfg):
    """The pre-fix behavior, kept visible: no `columns` -> cursor names."""
    res = db_connector.preview_rows(upper_cfg, "", None, "orders", sid="t")
    assert res["ok"] is True
    assert res["columns"] == ["ORDER_ID", "AMOUNT", "CREATED_AT"]


def test_preview_with_columns_returns_the_introspected_names(upper_cfg):
    """What the admin routes now pass: the frame is keyed by the names the
    registry and the AI-draft column map actually use."""
    cols = ["order_id", "amount", "created_at"]
    res = db_connector.preview_rows(upper_cfg, "", None, "orders",
                                    columns=cols, limit=3, sid="t")
    assert res["ok"] is True
    assert res["columns"] == cols
    assert len(res["rows"]) == 3


def test_snapshot_with_columns_writes_the_introspected_names(upper_cfg, tmp_path):
    dest = tmp_path / "snap.parquet"
    cols = ["order_id", "amount", "created_at"]
    res = db_connector.snapshot_table(upper_cfg, "", schema=None,
                                      table="orders", columns=cols,
                                      dest=dest, sid="t", chunk_rows=2)
    assert res["ok"] is True and res["rows"] == 4
    assert res["columns"] == cols
    assert list(pd.read_parquet(dest).columns) == cols


def test_fingerprint_aliases_survive_a_folding_cursor(upper_cfg):
    """`fp_count` unquoted comes back FP_COUNT from a folding cursor; only
    executing the CONSTRUCT puts our own label back. Reading `count` at all is
    what proves it — pre-fix this raised KeyError on Oracle, which silently
    disabled the unchanged-table skip on every refresh."""
    fp = db_connector.fingerprint_table(upper_cfg, "", None, "orders", sid="t")
    assert fp["ok"] is True
    assert fp["agg"]["count"] == 4
    # Aggregates stay keyed by the INTROSPECTED names (sqlite preserves the
    # DDL casing here), never by the alias.
    assert set(fp["agg"]["sums"]) == {"ORDER_ID", "AMOUNT"}
    assert float(fp["agg"]["sums"]["AMOUNT"]) == pytest.approx(12.0)
