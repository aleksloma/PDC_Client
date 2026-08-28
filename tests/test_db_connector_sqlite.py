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
    for key in ("postgresql", "mysql", "mariadb", "mssql", "oracle",
                "clickhouse"):
        d = db_connector.DIALECTS[key]
        assert d.drivername and d.select1_sql and d.quote
        assert not d.hidden
    # UI list is derived purely from the registry, hidden entries filtered.
    keys = {r["key"] for r in db_connector.list_dialects()}
    assert "sqlite" not in keys
    assert {"postgresql", "mysql", "mariadb", "mssql", "oracle",
            "clickhouse"} <= keys
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
                "TimeoutError raised by the pool",
                # clickhouse-driver, verbatim off ClickHouse 24.8 (the
                # clickhouse-sqlalchemy wrapper prefixes the server text).
                "Orig exception: Code: 159. DB::Exception: Timeout exceeded: "
                "elapsed 2.000478552 seconds, maximum: 2."):
        e = RuntimeError(msg)
        assert db_connector._friendly_db_error(e, phrase) == phrase, msg


def test_friendly_error_maps_the_clickhouse_socket_timeout_class():
    """clickhouse-driver reports a socket timeout as SocketTimeoutError whose
    text is just `Code: 209.` — the CLASS NAME is what carries the meaning,
    and _friendly_db_error matches against `type(e).__name__ + str(e)`."""
    SocketTimeoutError = type("SocketTimeoutError", (Exception,), {})
    e = SocketTimeoutError("Code: 209. (ch-host:9000)")
    phrase = "Database connection timed out."
    assert db_connector._friendly_db_error(e, phrase) == phrase


def test_friendly_error_does_not_relabel_generic_connect_failures():
    """DPY-6005 is oracledb's generic "cannot connect" — listener down,
    refused, bad DNS. Calling those a timeout destroys the diagnosability
    this helper exists for."""
    e = RuntimeError("DPY-6005: cannot connect to database")
    out = db_connector._friendly_db_error(e, "Database connection timed out.")
    assert "DPY-6005" in out and "timed out" not in out
    # Same policy for clickhouse-driver's NetworkError (code 210).
    NetworkError = type("NetworkError", (Exception,), {})
    out = db_connector._friendly_db_error(
        NetworkError("Code: 210. Connection refused (ch-host:9000)"),
        "Database connection timed out.")
    assert "Connection refused" in out and "timed out" not in out


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


# ---------------------------------------------------------------------------
# fingerprint_table (Prompt 13 Part C)
# ---------------------------------------------------------------------------

def test_fingerprint_values_and_determinism(sqlite_cfg):
    fp = db_connector.fingerprint_table(
        sqlite_cfg, "", None, "orders",
        preferred_order=["order_id", "client_id", "amount", "segment"],
        sid="t")
    assert fp["ok"] is True
    assert fp["agg"]["count"] == 7
    # amounts: 0, 1.5, ..., 9.0 → sum 31.5
    assert float(fp["agg"]["sums"]["amount"]) == pytest.approx(31.5)
    assert float(fp["agg"]["avgs"]["amount"]) == pytest.approx(4.5)
    names = [c["name"] for c in fp["columns"]]
    assert names == ["order_id", "client_id", "amount", "segment"]
    fp2 = db_connector.fingerprint_table(
        sqlite_cfg, "", None, "orders",
        preferred_order=["order_id", "client_id", "amount", "segment"],
        sid="t")
    assert fp == fp2                                     # deterministic


def test_fingerprint_respects_where_and_row_cap(sqlite_cfg):
    fp_all = db_connector.fingerprint_table(sqlite_cfg, "", None, "orders", sid="t")
    fp_where = db_connector.fingerprint_table(
        sqlite_cfg, "", None, "orders", where="client_id = 0", sid="t")
    assert fp_where["ok"] and fp_where["agg"]["count"] == 3
    assert fp_where["agg"]["count"] != fp_all["agg"]["count"]
    fp_cap = db_connector.fingerprint_table(
        sqlite_cfg, "", None, "orders", row_cap=2, sid="t")
    assert fp_cap["ok"] and fp_cap["agg"]["count"] == 2  # cap INSIDE the subquery


def test_fingerprint_column_pick_caps_and_order(sqlite_cfg, tmp_path):
    # a table with 6 numerics + 3 timestamps → ≤4 numeric, ≤2 temporal,
    # registry (preferred) order wins
    db = tmp_path / "wide.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE w (n1 REAL, n2 REAL, n3 REAL, n4 REAL, n5 REAL, "
            "n6 REAL, t1 TIMESTAMP, t2 TIMESTAMP, t3 TIMESTAMP, s TEXT)"))
        conn.execute(text(
            "INSERT INTO w VALUES (1,2,3,4,5,6,'2026-01-01','2026-02-01',"
            "'2026-03-01','x')"))
    eng.dispose()
    cfg = {"db_type": "sqlite", "url_override": f"sqlite+pysqlite:///{db}"}
    pref = ["n6", "n5", "n4", "n3", "t3", "t2", "n1", "n2", "t1", "s"]
    fp = db_connector.fingerprint_table(cfg, "", None, "w",
                                        preferred_order=pref, sid="t")
    assert fp["ok"] is True
    assert sorted(fp["agg"]["sums"]) == ["n3", "n4", "n5", "n6"]  # top 4 by pref
    assert sorted(fp["agg"]["maxes"]) == ["t2", "t3"]             # top 2 by pref
    assert "s" not in fp["agg"]["sums"]


def test_fingerprint_failure_shape(sqlite_cfg):
    fp = db_connector.fingerprint_table(sqlite_cfg, "", None, "no_such_table",
                                        sid="t")
    assert fp["ok"] is False and fp["error"]


# ---------------------------------------------------------------------------
# ClickHouse — registry-only proofs (no live server; the dialect is exercised
# through the same generic code paths as every other entry)
# ---------------------------------------------------------------------------

def test_clickhouse_registry_entry():
    import importlib.util
    d = db_connector.DIALECTS["clickhouse"]
    assert d.label == "ClickHouse"
    assert d.drivername == "clickhouse+native"
    assert d.driver_module == "clickhouse_driver"
    assert d.default_port == 9000
    assert d.quote == ("`", "`")
    assert d.needs == ("database",)
    # ClickHouse databases are exposed as schemas, so the browser lists them.
    assert d.supports_schemas is True
    assert d.select1_sql == "SELECT 1"
    # Columnar or not, never COUNT(*) a customer table: system.tables answers.
    assert d.exact_count_fallback is False
    assert d.hidden is False
    assert d.allow_url_override is False
    ok, reason = d.available()
    assert ok is (importlib.util.find_spec("clickhouse_driver") is not None)
    assert ok or "clickhouse_driver" in reason


def test_clickhouse_build_url():
    url = db_connector.build_url(
        {"db_type": "clickhouse", "host": "ch", "database": "analytics",
         "user": "reader"}, "p@ss/word")
    assert url.drivername == "clickhouse+native"
    assert url.port == 9000            # registry default when cfg omits it
    assert url.database == "analytics"
    assert "p@ss/word" not in str(url) and "***" in str(url)
    assert url.password == "p@ss/word"


def test_clickhouse_bounds_the_connect_via_the_url_query():
    """clickhouse+native DISCARDS connect_args: create_connect_args returns
    ((url_string,), {}) and Connection.__init__ does Client.from_url(args[0]),
    ignoring **kwargs. A timeout put in connect_args would read as correct and
    bound NOTHING — the Oracle ~127s gap all over again. Everything therefore
    rides in the URL query, which clickhouse-driver's parse_url types."""
    d = db_connector.DIALECTS["clickhouse"]
    assert d.connect_args({}, 8) == {}
    q = d.query_args({})
    assert q["connect_timeout"] == "8"           # settings.DB_CONNECT_TIMEOUT
    assert q["max_execution_time"] == "300"      # unknown key -> server setting
    # The socket read bound outlasts the server kill so the two can't race.
    assert q["send_receive_timeout"] == "330"
    url = db_connector.build_url({"db_type": "clickhouse", "host": "ch"}, "")
    assert url.query["connect_timeout"] == "8"


def test_clickhouse_bounds_survive_into_the_dsn_the_driver_parses():
    """The one that would actually catch a regression: putting a key in the
    SQLAlchemy URL proves nothing unless the dialect RENDERS it into the DSN
    it hands clickhouse-driver, and the driver types it back out. If a future
    clickhouse-sqlalchemy changed that rendering, every other test here would
    still pass while the bound silently disappeared — the Oracle failure mode.
    Skipped where the driver is absent; it is installed in the image."""
    pytest.importorskip("clickhouse_sqlalchemy")
    from clickhouse_driver.util.helpers import parse_url
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    url = db_connector.build_url(
        {"db_type": "clickhouse", "host": "ch", "database": "analytics",
         "user": "u", "ssl": True, "trust_server_certificate": True}, "pw")
    engine = create_engine(url, poolclass=NullPool)   # no connection is opened
    try:
        args, kwargs = engine.dialect.create_connect_args(engine.url)
        # The channel create_engine(connect_args=...) would have used.
        assert kwargs == {}, "connect_args would NOT be discarded any more"
        _host, client_kwargs = parse_url(args[0])
    finally:
        engine.dispose()

    assert client_kwargs["connect_timeout"] == 8.0
    assert client_kwargs["send_receive_timeout"] == 330.0
    assert client_kwargs["secure"] is True
    assert client_kwargs["verify"] is False
    assert client_kwargs["settings"] == {"max_execution_time": "300"}
    assert client_kwargs["database"] == "analytics"


def test_clickhouse_query_args_ssl_flags():
    d = db_connector.DIALECTS["clickhouse"]
    plain = d.query_args({})
    assert "secure" not in plain and "verify" not in plain
    tls = d.query_args({"ssl": True})
    assert tls["secure"] == "true" and "verify" not in tls
    self_signed = d.query_args({"ssl": True, "trust_server_certificate": True})
    assert self_signed["secure"] == "true" and self_signed["verify"] == "false"


def test_clickhouse_query_args_honour_admin_overrides():
    q = db_connector.DIALECTS["clickhouse"].query_args(
        {"connect_timeout": 3, "statement_timeout": 30})
    assert q["connect_timeout"] == "3"
    assert q["max_execution_time"] == "30"
    assert q["send_receive_timeout"] == "60"   # the kill + the 30s margin


def test_clickhouse_issues_no_session_statement_timeout():
    """The statement bound is max_execution_time in the URL query, NOT a
    session SET: clickhouse-driver re-sends its own settings with every query
    and they overwrite anything SET on the session (measured against
    ClickHouse 24.8 — after `SET max_execution_time = 7`, system.settings
    still read the URL value). Emitting the SET anyway would be dead weight
    that reads like a working bound, so this dialect has no
    apply_stmt_timeout — the same shape as mssql."""
    class _Conn:
        def execute(self, stmt):            # pragma: no cover - must not run
            raise AssertionError("clickhouse must not issue a session SET")

    db_connector.DIALECTS["clickhouse"].apply_stmt_timeout(_Conn(), 42)
    assert db_connector.DIALECTS["clickhouse"].apply_stmt_timeout is         db_connector._no_op_timeout


def test_clickhouse_build_select_quotes_and_limits():
    d = db_connector.DIALECTS["clickhouse"]
    assert (db_connector._build_select(d, "analytics", "events", row_cap=5)
            == "SELECT * FROM `analytics`.`events` LIMIT 5")
    assert (db_connector._build_select(d, None, "events", columns=["a`b"])
            == "SELECT `a``b` FROM `events`")


def test_clickhouse_catalog_estimates_are_bound_by_schema_and_table():
    d = db_connector.DIALECTS["clickhouse"]
    for sql in (d.row_count_sql, d.table_size_sql):
        assert sql.startswith("SELECT ") and "system.tables" in sql
        assert ":schema" in sql and ":table" in sql
