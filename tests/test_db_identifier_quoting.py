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


# ---------------------------------------------------------------------------
# PHYSICALLY case-sensitive names — quoted_name(quote=True) end to end.
# The mirror-image bug of the file's original regression: a created-quoted
# lowercase column loses its quote flag at any str()/JSON hop, compiles
# UNQUOTED, Oracle folds it to a name that does not exist → ORA-00904.
# ---------------------------------------------------------------------------

from sqlalchemy.sql import quoted_name  # noqa: E402


def _q(name):
    return quoted_name(name, True)


@pytest.mark.parametrize("dialect, expected", [
    (oracle.dialect(),
     'SELECT "bsrep"."agro_model_final_predictions"."prediction" '
     'FROM "bsrep"."agro_model_final_predictions"'),
    (postgresql.dialect(),
     'SELECT "bsrep"."agro_model_final_predictions"."prediction" '
     'FROM "bsrep"."agro_model_final_predictions"'),
    (mysql.dialect(),
     "SELECT `bsrep`.`agro_model_final_predictions`.`prediction` "
     "FROM `bsrep`.`agro_model_final_predictions`"),
    (mssql.dialect(),
     "SELECT [bsrep].[agro_model_final_predictions].[prediction] "
     "FROM [bsrep].[agro_model_final_predictions]"),
    (sa_sqlite.dialect(),
     'SELECT "bsrep"."agro_model_final_predictions"."prediction" '
     'FROM "bsrep"."agro_model_final_predictions"'),
])
def test_quote_flag_compiles_quoted_exact_case(dialect, expected):
    """THE production ORA-00904: the same lowercase names that must render
    UNQUOTED as plain strings must render QUOTED exact-case with quote=True —
    the flag, not the spelling, is the distinguishing bit."""
    sql = _sql(dialect, _q("bsrep"), _q("agro_model_final_predictions"),
               columns=[_q("prediction")])
    assert sql == expected


def test_quote_flag_clickhouse():
    pytest.importorskip("clickhouse_sqlalchemy")
    from clickhouse_sqlalchemy.drivers.native.base import ClickHouseDialect_native
    sql = _sql(ClickHouseDialect_native(), _q("bsrep"), _q("preds"),
               columns=[_q("prediction")])
    assert '"bsrep"."preds"."prediction"' in sql


def test_qname_tristate_and_col_ident():
    """qname(None-ish quote) is a byte-identical passthrough — the entire
    legacy registry keeps compiling exactly as today."""
    assert db_connector.qname(None, True) is None
    assert db_connector.qname("prediction", None) == "prediction"
    assert db_connector.qname("prediction", False) == "prediction"
    q = db_connector.qname("prediction", True)
    assert isinstance(q, quoted_name) and q.quote is True
    # A plain-str passthrough compiles identically to the never-touched path.
    d = oracle.dialect()
    assert (_sql(d, db_connector.qname("bsrep", None), "offering_all", row_cap=20)
            == _sql(d, "bsrep", "offering_all", row_cap=20))
    # col_ident: the persisted flag wins; an in-process quoted_name value
    # keeps its own flag; a legacy dict stays plain.
    assert db_connector.col_ident({"name": "a"}) == "a"
    assert not isinstance(db_connector.col_ident({"name": "a"}), quoted_name)
    assert db_connector.col_ident({"name": "a", "quote": True}).quote is True
    assert db_connector.col_ident({"name": _q("a")}).quote is True


def test_json_roundtrip_rebuild_compiles_identically():
    """Introspected names → JSON (the flag drops off `name`, survives in
    `quote`) → col_ident rebuild → compiled SQL identical to the
    never-serialized path."""
    import json
    live = [{"name": _q("prediction"), "quote": True},
            {"name": "max_loan_amount"}]
    thawed = json.loads(json.dumps(live))
    # The JSON hop really does strip the flag from the name itself…
    assert not isinstance(thawed[0]["name"], quoted_name)
    d = oracle.dialect()
    direct = _sql(d, _q("bsrep"), _q("preds"),
                  columns=[c["name"] for c in live])
    rebuilt = _sql(d, db_connector.qname("bsrep", True),
                   db_connector.qname("preds", True),
                   columns=[db_connector.col_ident(c) for c in thawed])
    assert direct == rebuilt
    assert '"prediction"' in rebuilt and "max_loan_amount" in rebuilt


def test_catalog_name_preserves_a_quoted_lowercase_name():
    """Pins the SQLAlchemy 2.0.36 behavior this fix leans on: denormalize_name
    is a no-op on quote=True names (quoted_name.upper() refuses), so the
    catalog binds stay exact-case for a physically lowercase Oracle object."""
    r = db_connector._catalog_name(oracle.dialect(), _q("bsrep"))
    assert r == "bsrep" and getattr(r, "quote", None) is True


def test_compose_fingerprint_hash_ignores_the_additive_keys():
    """`quote`/`type_info` on fingerprint column dicts must never invalidate a
    stored fingerprint — the projection is exactly [[name, dtype]]."""
    import db_scheduler
    base = {"ok": True, "agg": {"count": 4, "sums": {}, "avgs": {}, "maxes": {}},
            "columns": [{"name": "prediction", "dtype": "FLOAT"}]}
    extra = {"ok": True, "agg": {"count": 4, "sums": {}, "avgs": {}, "maxes": {}},
             "columns": [{"name": "prediction", "dtype": "FLOAT", "quote": True,
                          "type_info": {"py": "float"}}]}
    assert db_scheduler.compose_fingerprint(base) == \
        db_scheduler.compose_fingerprint(extra)


def test_resolve_live_idents_matches_and_degrades():
    class _Insp:
        def get_schema_names(self):
            return ["main", quoted_name("bsrep", True)]

        def get_table_names(self, schema=None):
            return [quoted_name("preds", True), "orders"]

        def get_view_names(self, schema=None):
            raise RuntimeError("no views")

    sch, tbl = db_connector._resolve_live_idents(_Insp(), "bsrep", "preds")
    assert getattr(sch, "quote", None) is True
    assert getattr(tbl, "quote", None) is True
    # Ordinary names come back as the plain listing entries.
    sch, tbl = db_connector._resolve_live_idents(_Insp(), "main", "orders")
    assert not isinstance(sch, quoted_name) and not isinstance(tbl, quoted_name)
    # A broken inspector degrades to passthrough (Article IV).
    class _Boom:
        def get_schema_names(self):
            raise RuntimeError("down")

    assert db_connector._resolve_live_idents(_Boom(), "s", "t") == ("s", "t")


def test_build_table_doc_persists_the_flags_from_intro():
    """Flags come from the FRESH introspection, never the posted body; absent
    for ordinary names, so legacy docs stay byte-identical."""
    from routes.admin_data import _build_table_doc
    intro = {"ok": True, "schema": _q("bsrep"), "table": _q("preds"),
             "schema_quote": True, "table_quote": True,
             "columns": [{"name": _q("prediction"), "quote": True},
                         {"name": "max_loan_amount"}]}
    doc = _build_table_doc(
        tid="", connection_id="c", schema="bsrep", table="preds",
        display_name="p", description="", is_connector=False, relations=[],
        columns=[{"name": "prediction", "dtype": "FLOAT", "description": "x"},
                 {"name": "max_loan_amount", "dtype": "NUMBER"}],
        intro=intro, where_filter=None, row_cap=None, email="a@b")
    cols = {c["name"]: c for c in doc["columns"]}
    assert cols["prediction"]["quote"] is True
    assert "quote" not in cols["max_loan_amount"]
    assert doc["schema_quote"] is True and doc["table_quote"] is True
    # Legacy shape: no flags anywhere → no new keys.
    plain = _build_table_doc(
        tid="", connection_id="c", schema="s", table="t",
        display_name="p", description="", is_connector=False, relations=[],
        columns=[{"name": "a", "dtype": "INTEGER"}],
        intro={"ok": True, "schema": "s", "table": "t",
               "columns": [{"name": "a"}]},
        where_filter=None, row_cap=None, email="a@b")
    assert "schema_quote" not in plain and "table_quote" not in plain
    assert "quote" not in plain["columns"][0]


# ---------------------------------------------------------------------------
# Live end-to-end: an Inspector that flags case-sensitive names (the Oracle
# behavior, simulated on sqlite) through register → preview → snapshot →
# refresh twice. The flags must survive mark_refreshed's wholesale columns
# replace — dropping them on the nightly run was the #1 regression trap.
# ---------------------------------------------------------------------------

@pytest.fixture
def cased_setup(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from sqlalchemy.engine.reflection import Inspector

    import db_sources
    import local_store
    from settings import settings

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    local_store._DATAFRAME_CACHE.invalidate()

    db = tmp_path / "cs.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text('CREATE TABLE casetab ("order" INTEGER, '
                          '"MixedCase" TEXT, plain_col REAL)'))
        for i in range(5):
            conn.execute(text(
                f"INSERT INTO casetab VALUES ({i}, 'v{i}', {i * 1.5})"))
    eng.dispose()

    # sqlite's Inspector hands back plain strings; make it behave like
    # Oracle's on a created-quoted table so the FULL production path
    # (fingerprint → snap_cols → _select_stmt → mark_refreshed re-emit)
    # runs against flagged names.
    orig = Inspector.get_columns

    def flagged(self, table_name, schema=None, **kw):
        cols = orig(self, table_name, schema=schema, **kw)
        for c in cols:
            if c.get("name") in ("order", "MixedCase"):
                c["name"] = quoted_name(c["name"], True)
        return cols

    monkeypatch.setattr(Inspector, "get_columns", flagged)

    store = db_sources.DataSourceStore()
    c = store.create_connection(
        {"name": "s", "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{db}"}, "pw", actor="ladmin")
    t = store.upsert_table({
        "connection_id": c["id"], "schema": "", "table_name": "casetab",
        "table_quote": True,   # sqlite matches quoted names case-insensitively
        "display_name": "case table", "description": "d",
        "columns": [
            {"name": "order", "dtype": "INTEGER", "description": "", "quote": True},
            {"name": "MixedCase", "dtype": "TEXT", "description": "", "quote": True},
            {"name": "plain_col", "dtype": "REAL", "description": ""},
        ],
    }, actor="ladmin")
    yield {"cfg": {"db_type": "sqlite",
                   "url_override": f"sqlite+pysqlite:///{db}"},
           "store": store, "tid": t["id"]}
    local_store._DATAFRAME_CACHE.invalidate()


def test_cased_preview_and_introspect_carry_the_flags(cased_setup):
    intro = db_connector.introspect(cased_setup["cfg"], "", None, "casetab",
                                    sid="t")
    assert intro["ok"] is True
    by_name = {str(c["name"]): c for c in intro["columns"]}
    assert by_name["order"].get("quote") is True
    assert by_name["MixedCase"].get("quote") is True
    assert "quote" not in by_name["plain_col"]
    res = db_connector.preview_rows(
        cased_setup["cfg"], "", None, "casetab",
        columns=[c["name"] for c in intro["columns"]], limit=3, sid="t")
    assert res["ok"] is True
    assert res["columns"] == ["order", "MixedCase", "plain_col"]


def test_refresh_heals_a_legacy_doc_missing_the_table_flag(cased_setup,
                                                           monkeypatch):
    """The repair path: a legacy doc without `table_quote` whose live catalog
    flags the table name. The fingerprint path never resolves table names, so
    force it down the introspect fallback — the resolved flags must be
    PERSISTED by mark_refreshed (not healed once per run), and the next
    refresh then works from the stored doc."""
    import db_connector
    import db_scheduler
    from sqlalchemy.engine.reflection import Inspector

    store, tid = cased_setup["store"], cased_setup["tid"]
    doc = store.get_table(tid)
    doc.pop("table_quote", None)          # the legacy shape
    store.upsert_table(doc, actor="ladmin")

    orig_tables = Inspector.get_table_names

    def flagged_tables(self, schema=None, **kw):
        return [quoted_name(n, True) if n == "casetab" else n
                for n in orig_tables(self, schema=schema, **kw)]

    monkeypatch.setattr(Inspector, "get_table_names", flagged_tables)
    monkeypatch.setattr(db_connector, "fingerprint_table",
                        lambda *a, **k: {"ok": False, "error": "forced"})

    res = db_scheduler.refresh_one_table(tid, actor="test")
    assert res["ok"] is True
    assert store.get_table(tid).get("table_quote") is True


def test_cased_refresh_twice_keeps_flags_and_reports_no_drift(cased_setup):
    import db_scheduler
    import local_store

    tid, store = cased_setup["tid"], cased_setup["store"]
    res = db_scheduler.refresh_one_table(tid, actor="test")
    assert res["ok"] is True and res["rows"] == 5
    assert res["drift"] == {"added": [], "removed": [], "retyped": []}
    df = pd.read_parquet(local_store.db_snapshot_path(tid))
    assert list(df.columns) == ["order", "MixedCase", "plain_col"]

    res2 = db_scheduler.refresh_one_table(tid, actor="test")
    assert res2["ok"] is True
    assert res2["drift"] == {"added": [], "removed": [], "retyped": []}
    # The flags survived BOTH wholesale column rewrites.
    row = store.get_table(tid)
    by_name = {c["name"]: c for c in row["columns"]}
    assert by_name["order"].get("quote") is True
    assert by_name["MixedCase"].get("quote") is True
    assert "quote" not in by_name["plain_col"]
