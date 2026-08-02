"""db_connector driven through the hidden sqlite dialect — the OFFLINE proof
that the registry / URL / engine / Inspector / chunked-snapshot code path
works end-to-end, plus the SELECT-only guard and dtype-plan stability."""
import re
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

import db_connector


@pytest.fixture
def sqlite_cfg(tmp_path):
    """A tmp-FILE SQLite DB seeded with FK+index DDL, reached through the same
    build_url/get_engine path production uses (url_override on the hidden
    sqlite entry). :memory: would give each NullPool connection its own empty
    DB — a file is the honest analogue."""
    db = tmp_path / "t.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE clients (
                client_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                city_code INTEGER
            )"""))
        conn.execute(text("""
            CREATE TABLE orders (
                order_id INTEGER PRIMARY KEY,
                client_id INTEGER REFERENCES clients(client_id),
                amount REAL,
                segment TEXT
            )"""))
        conn.execute(text("CREATE INDEX ix_orders_client ON orders(client_id)"))
        for i in range(7):
            conn.execute(text(
                "INSERT INTO orders (client_id, amount, segment) "
                f"VALUES ({i % 3}, {i * 1.5}, 'seg{i % 2}')"))
        conn.execute(text("INSERT INTO clients VALUES (1, 'Acme', 10)"))
    eng.dispose()
    return {"db_type": "sqlite", "url_override": f"sqlite+pysqlite:///{db}"}


def test_registry_shape():
    for key in ("postgresql", "mysql", "mariadb", "mssql", "oracle"):
        d = db_connector.DIALECTS[key]
        assert d.drivername and d.select1_sql and d.quote
        assert not d.hidden
    # UI list is derived purely from the registry, hidden entries filtered.
    keys = {r["key"] for r in db_connector.list_dialects()}
    assert "sqlite" not in keys
    assert {"postgresql", "mysql", "mariadb", "mssql", "oracle"} <= keys
    for r in db_connector.list_dialects():
        assert set(r) == {"key", "label", "default_port", "needs",
                          "supports_schemas", "available", "unavailable_reason"}


def test_build_url_masks_password():
    url = db_connector.build_url(
        {"db_type": "postgresql", "host": "h", "port": 5432,
         "database": "d", "user": "u"}, "p@ss:word/x")
    # URL.create handles escaping; str()/repr() masks the password.
    assert "p@ss:word/x" not in str(url)
    assert "***" in str(url)
    assert url.password == "p@ss:word/x"


def test_test_connection_ok(sqlite_cfg):
    res = db_connector.test_connection(sqlite_cfg, "", sid="t")
    assert res["ok"] is True and res["error"] is None
    assert isinstance(res["elapsed_ms"], int)


def test_test_connection_failure_returns_ok_false_not_raise(tmp_path):
    cfg = {"db_type": "sqlite",
           "url_override": f"sqlite+pysqlite:///{tmp_path}/no/such/dir/x.db"}
    res = db_connector.test_connection(cfg, "hunter2", sid="t")
    assert res["ok"] is False
    assert res["error"]
    assert "hunter2" not in res["error"]  # scrubbed


def test_introspect_columns_pk_fk_indexes(sqlite_cfg):
    res = db_connector.introspect(sqlite_cfg, "", None, "orders", sid="t")
    assert res["ok"] is True
    cols = {c["name"]: c for c in res["columns"]}
    assert set(cols) == {"order_id", "client_id", "amount", "segment"}
    assert cols["order_id"]["pk"] is True and cols["order_id"]["indexed"] is True
    assert cols["client_id"]["indexed"] is True   # via ix_orders_client
    assert cols["segment"]["indexed"] is False
    assert res["foreign_keys"][0]["referred_table"] == "clients"
    assert any(ix["columns"] == ["client_id"] for ix in res["indexes"])
    # sqlite degradations, asserted: exact count works, size does not.
    assert res["row_count_estimate"] == 7
    assert res["size_bytes_estimate"] is None
    assert "size" in res["degraded"]
    assert res["table_comment"] is None


def test_list_schemas_and_tables_degrade_on_sqlite(sqlite_cfg):
    sch = db_connector.list_schemas(sqlite_cfg, "", sid="t")
    assert sch["ok"] is True and "main" in sch["schemas"]
    tabs = db_connector.list_tables(sqlite_cfg, "", None, sid="t")
    assert {t["name"] for t in tabs["tables"]} == {"clients", "orders"}


def test_preview_rows(sqlite_cfg):
    res = db_connector.preview_rows(sqlite_cfg, "", None, "orders", limit=3, sid="t")
    assert res["ok"] is True
    assert res["columns"] == ["order_id", "client_id", "amount", "segment"]
    assert len(res["rows"]) == 3


def test_snapshot_table_chunked_writes_parquet(sqlite_cfg, tmp_path):
    dest = tmp_path / "snap.parquet"
    res = db_connector.snapshot_table(sqlite_cfg, "", schema=None, table="orders",
                                      dest=dest, sid="t", chunk_rows=2)
    assert res["ok"] is True and res["rows"] == 7
    assert dest.exists() and res["bytes"] == dest.stat().st_size
    # No tmp leftovers (atomic os.replace).
    assert [p for p in tmp_path.iterdir() if ".tmp-" in p.name] == []
    df = pd.read_parquet(dest)
    assert len(df) == 7
    assert list(df.columns) == ["order_id", "client_id", "amount", "segment"]


def test_snapshot_dtype_plan_stable_across_chunks(sqlite_cfg, tmp_path):
    """Chunk 1 pins the plan; later chunks reuse it → one consistent Arrow
    schema (the naive per-chunk astype would raise on write_table)."""
    dest = tmp_path / "snap.parquet"
    res = db_connector.snapshot_table(sqlite_cfg, "", schema=None, table="orders",
                                      dest=dest, sid="t", chunk_rows=2)
    assert res["ok"] is True
    plan = res["dtype_plan"]
    # Low-cardinality string column recorded as category (metadata only —
    # never baked into the file, never applied by the loader);
    # numerics downcast (writer-side).
    assert plan.get("segment") == "category"
    assert plan.get("order_id", "").startswith("int")
    df = pd.read_parquet(dest)
    assert str(df["segment"].dtype) != "category"  # not baked into the file
    # A refresh reusing the stored plan stays stable.
    res2 = db_connector.snapshot_table(sqlite_cfg, "", schema=None, table="orders",
                                       dest=dest, sid="t", chunk_rows=2,
                                       dtype_plan=plan)
    assert res2["ok"] is True and res2["dtype_plan"] == plan


def test_snapshot_row_cap_and_where(sqlite_cfg, tmp_path):
    dest = tmp_path / "snap.parquet"
    res = db_connector.snapshot_table(sqlite_cfg, "", schema=None, table="orders",
                                      where="client_id = 0", row_cap=2,
                                      dest=dest, sid="t")
    assert res["ok"] is True and res["rows"] <= 2
    df = pd.read_parquet(dest)
    assert (df["client_id"] == 0).all()


@pytest.mark.parametrize("bad", [
    "1=1; DROP TABLE orders",
    "1=1 UNION SELECT * FROM x INTO OUTFILE '/tmp/x'",
    "EXISTS (SELECT 1 FROM t WHERE DELETE)",
])
def test_assert_single_select_rejects_dml_in_where(sqlite_cfg, tmp_path, bad):
    dest = tmp_path / "snap.parquet"
    res = db_connector.snapshot_table(sqlite_cfg, "", schema=None, table="orders",
                                      where=bad, dest=dest, sid="t")
    assert res["ok"] is False
    assert not dest.exists()


@pytest.mark.parametrize("bad_sql", [
    "DROP TABLE x",
    "SELECT 1; DROP TABLE x",
    "WITH c AS (SELECT 1) SELECT * FROM c",
    "INSERT INTO x VALUES (1)",
    "SELECT * FROM x INTO y",
])
def test_assert_single_select_rejects(bad_sql):
    with pytest.raises(ValueError):
        db_connector._assert_single_select(bad_sql)


def test_assert_single_select_allows_plain_select():
    db_connector._assert_single_select('SELECT "a", "b" FROM "s"."t" WHERE "a" > 1')


def test_snapshot_failure_keeps_previous_snapshot(sqlite_cfg, tmp_path):
    """A failed refresh must leave the last good parquet in place (Article IV
    — chats keep serving the previous data)."""
    dest = tmp_path / "snap.parquet"
    ok = db_connector.snapshot_table(sqlite_cfg, "", schema=None, table="orders",
                                     dest=dest, sid="t")
    assert ok["ok"] is True
    before = dest.stat().st_mtime_ns
    bad = db_connector.snapshot_table(sqlite_cfg, "", schema=None,
                                      table="no_such_table", dest=dest, sid="t")
    assert bad["ok"] is False
    assert dest.exists() and dest.stat().st_mtime_ns == before


def test_every_text_literal_is_select_or_set():
    """Structural guard: every SQL string in db_connector is SELECT/SET —
    the connector can never grow a write path unnoticed."""
    src = Path(db_connector.__file__).read_text(encoding="utf-8")
    for m in re.finditer(r'(?:row_count_sql|table_size_sql)=\("([A-Z]+) ', src):
        assert m.group(1) in ("SELECT",)
    for m in re.finditer(r'text\(f?"([A-Za-z]+)[ _]', src):
        assert m.group(1).upper() in ("SELECT", "SET"), m.group(0)


# ---------------------------------------------------------------------------
# v4.2 — timeout bounds and timeout-shaped error wording
# ---------------------------------------------------------------------------

def test_every_networked_dialect_bounds_the_connect():
    """A dialect with no connect bound falls back to the OS TCP timeout
    (~127s) — long enough to outlast any admin click. Oracle was that gap."""
    for key, d in db_connector.DIALECTS.items():
        if d.hidden:
            continue                      # sqlite is a local file
        args = d.connect_args({}, 8)
        query = d.query_args({})
        bounded = any("timeout" in str(k).lower() for k in args) or \
            any("timeout" in str(k).lower() for k in query)
        assert bounded, f"{key} has no connect timeout"


def test_oracle_connect_args_carry_the_driver_kwarg():
    """Pinned oracledb 2.5.1 thin mode spells it `tcp_connect_timeout`."""
    args = db_connector.DIALECTS["oracle"].connect_args({}, 8)
    assert args == {"tcp_connect_timeout": 8.0}


def test_friendly_error_maps_timeout_error_in_the_cause_chain():
    phrase = "Database connection timed out."
    try:
        try:
            raise TimeoutError("socket timed out")
        except TimeoutError as inner:
            raise RuntimeError("could not connect") from inner
    except RuntimeError as e:
        assert db_connector._friendly_db_error(e, phrase) == phrase


def test_friendly_error_maps_driver_specific_timeout_strings():
    phrase = "Database connection timed out."
    for msg in ("connection timeout expired",
                "('HYT00', '[HYT00] Login timeout expired')",
                "Lost connection: read timed out",
                "QueryCanceled: canceling statement due to statement timeout",
                "TimeoutError raised by the pool"):
        e = RuntimeError(msg)
        assert db_connector._friendly_db_error(e, phrase) == phrase, msg


def test_friendly_error_does_not_relabel_generic_connect_failures():
    """DPY-6005 is oracledb's generic "cannot connect" — listener down,
    refused, bad DNS. Calling those a timeout destroys the diagnosability
    this helper exists for."""
    e = RuntimeError("DPY-6005: cannot connect to database")
    out = db_connector._friendly_db_error(e, "Database connection timed out.")
    assert "DPY-6005" in out and "timed out" not in out


def test_friendly_error_ignores_the_word_timeout_inside_a_dsn_echo():
    """Postgres puts `options=-c statement_timeout=…` in EVERY connection, so
    a connect error echoing the DSN must not read as a timeout."""
    e = RuntimeError("could not translate host name; "
                     "options='-c statement_timeout=300000'")
    out = db_connector._friendly_db_error(e, "Database connection timed out.")
    assert "could not translate host name" in out


def test_friendly_error_keeps_non_timeout_driver_text():
    """A wrong password must never be reported as a timeout."""
    e = RuntimeError('FATAL: password authentication failed for user "pdc"')
    out = db_connector._friendly_db_error(e, "Database connection timed out.")
    assert "password authentication failed" in out
    assert "timed out" not in out


def test_friendly_error_scrubs_the_password_from_non_timeout_text():
    e = RuntimeError("connection string used s3cr3t-pw")
    out = db_connector._friendly_db_error(
        e, "Database connection timed out.", "s3cr3t-pw")
    assert "s3cr3t-pw" not in out and "***" in out


def test_friendly_error_survives_a_self_referential_cause_chain():
    """Never hang on a cycle — the walk is depth-capped."""
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert "RuntimeError" in db_connector._friendly_db_error(a, "phrase")


def test_snapshot_failure_uses_the_snapshot_phrase(sqlite_cfg, tmp_path, monkeypatch):
    """A statement timeout during snapshot must not read as a CONNECT failure."""
    def boom(*a, **kw):
        raise RuntimeError("canceling statement due to statement timeout")
    monkeypatch.setattr(db_connector.pd, "read_sql", boom)
    res = db_connector.snapshot_table(
        sqlite_cfg, "", schema=None, table="clients",
        dest=tmp_path / "s.parquet", sid="t")
    assert res["ok"] is False
    assert res["error"] == "Database snapshot timed out."
