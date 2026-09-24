"""Transport encryption per database dialect.

What the "SSL / Encrypt" box on a registered connection actually buys, pinned
dialect by dialect:

- PostgreSQL: `sslmode=require` when ticked (encryption enforced by libpq),
  `prefer` when not.
- MySQL / MariaDB: PyMySQL negotiates TLS only if the server advertises it and
  otherwise carries on in PLAINTEXT without an error. A `connect` listener
  therefore asks the session for its cipher and refuses a plaintext session —
  closing the DBAPI connection first, because SQLAlchemy does not close a
  connection whose `connect` event raised.
- Oracle: the ticked box switches the thin driver to a `tcps://` DSN with
  server-DN matching; the trust box relaxes certificate checks exactly like
  the SQL Server and ClickHouse trust flags do.
- SQL Server and ClickHouse: unchanged connector arguments.
- ClickHouse's registry default is its TLS port (9440) with SSL pre-ticked;
  the plaintext port (9000) stays known so an existing blank-port row with SSL
  off keeps reaching the port it always used.

No server is needed: `create_engine` never connects, and the MySQL probe is
exercised against a fake DBAPI connection.
"""
import ssl

import pytest

import db_connector


# ---------------------------------------------------------------------------
# The exception
# ---------------------------------------------------------------------------

def test_tls_not_negotiated_exception_exists():
    assert isinstance(db_connector.TLSNotNegotiated, type)
    assert issubclass(db_connector.TLSNotNegotiated, Exception)


# ---------------------------------------------------------------------------
# PostgreSQL — pin current behaviour
# ---------------------------------------------------------------------------

def test_postgres_ssl_requires_tls():
    args = db_connector.DIALECTS["postgresql"].connect_args(
        {"ssl": True, "statement_timeout": 300}, 8)
    assert args["sslmode"] == "require"
    assert args["connect_timeout"] == 8
    assert args["options"] == "-c statement_timeout=300000"


def test_postgres_without_ssl_prefers():
    args = db_connector.DIALECTS["postgresql"].connect_args(
        {"statement_timeout": 300}, 8)
    assert args["sslmode"] == "prefer"


# ---------------------------------------------------------------------------
# MySQL / MariaDB — the post-connect cipher probe
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed = []
        self.closed = False

    def execute(self, sql, *a, **k):
        self.executed.append(sql)

    def fetchone(self):
        return self._row

    def close(self):
        self.closed = True


class _FakeDbapiConn:
    def __init__(self, row):
        self.cursor_obj = _FakeCursor(row)
        self.close_calls = 0

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.close_calls += 1


@pytest.mark.parametrize("row", [("Ssl_cipher", ""), None])
def test_mysql_probe_refuses_a_plaintext_session_and_closes_it(row):
    conn = _FakeDbapiConn(row)
    with pytest.raises(db_connector.TLSNotNegotiated):
        db_connector._mysql_require_tls(conn, object())
    assert conn.close_calls == 1, "the DBAPI connection must be closed before the raise"
    executed = [" ".join(s.split()).upper() for s in conn.cursor_obj.executed]
    assert executed == ["SHOW SESSION STATUS LIKE 'SSL_CIPHER'"]


def test_mysql_probe_accepts_a_negotiated_cipher():
    conn = _FakeDbapiConn(("Ssl_cipher", "TLS_AES_256_GCM_SHA384"))
    db_connector._mysql_require_tls(conn, object())
    assert conn.close_calls == 0
    assert len(conn.cursor_obj.executed) == 1


def test_tls_refusal_message_names_the_cause():
    conn = _FakeDbapiConn(("Ssl_cipher", ""))
    with pytest.raises(db_connector.TLSNotNegotiated) as ei:
        db_connector._mysql_require_tls(conn, object())
    assert "TLS" in str(ei.value)


def _engine(db_type, ssl_on):
    cfg = {"db_type": db_type, "host": "h", "database": "d", "user": "u"}
    if ssl_on:
        cfg["ssl"] = True
    return db_connector.get_engine(cfg, "pw")


@pytest.mark.parametrize("db_type", ["mysql", "mariadb"])
def test_mysql_listener_registered_only_with_ssl(db_type):
    pytest.importorskip("pymysql")
    from sqlalchemy import event
    on = _engine(db_type, True)
    off = _engine(db_type, False)
    try:
        assert event.contains(on, "connect", db_connector._mysql_require_tls) is True
        assert event.contains(off, "connect", db_connector._mysql_require_tls) is False
    finally:
        on.dispose()
        off.dispose()


def test_postgres_never_gets_the_mysql_listener():
    pytest.importorskip("psycopg2")
    from sqlalchemy import event
    for ssl_on in (True, False):
        eng = _engine("postgresql", ssl_on)
        try:
            assert event.contains(eng, "connect", db_connector._mysql_require_tls) is False
        finally:
            eng.dispose()


def test_mysql_connect_args_unchanged():
    d = db_connector.DIALECTS["mysql"]
    assert d.connect_args({}, 8) == {"connect_timeout": 8}
    assert d.connect_args({"ssl": True}, 8) == {"connect_timeout": 8,
                                                "ssl": {"ssl": True}}


# ---------------------------------------------------------------------------
# Oracle — TCPS
# ---------------------------------------------------------------------------

def test_oracle_ssl_uses_tcps_with_dn_match():
    args = db_connector.DIALECTS["oracle"].connect_args(
        {"ssl": True, "host": "h", "port": 2484, "service_name": "svc"}, 8)
    assert args == {"tcp_connect_timeout": 8.0,
                    "dsn": "tcps://h:2484/svc",
                    "ssl_server_dn_match": True}


def test_oracle_ssl_blank_port_falls_back_to_1521():
    args = db_connector.DIALECTS["oracle"].connect_args(
        {"ssl": True, "host": "h", "port": "", "service_name": "svc"}, 8)
    assert args["dsn"] == "tcps://h:1521/svc"


def test_oracle_ssl_uses_database_when_no_service_name():
    args = db_connector.DIALECTS["oracle"].connect_args(
        {"ssl": True, "host": "h", "port": 2484, "database": "orcl"}, 8)
    assert args["dsn"] == "tcps://h:2484/orcl"


def test_oracle_trust_box_relaxes_certificate_checks():
    args = db_connector.DIALECTS["oracle"].connect_args(
        {"ssl": True, "trust_server_certificate": True, "host": "h",
         "port": 2484, "service_name": "svc"}, 8)
    assert args["dsn"] == "tcps://h:2484/svc"
    assert args["ssl_server_dn_match"] is False
    ctx = args["ssl_context"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_oracle_without_ssl_is_plain_tcp():
    args = db_connector.DIALECTS["oracle"].connect_args(
        {"host": "h", "port": 2484, "service_name": "svc"}, 8)
    assert args == {"tcp_connect_timeout": 8.0}


# ---------------------------------------------------------------------------
# SQL Server / ClickHouse — pin current values
# ---------------------------------------------------------------------------

def test_mssql_args_unchanged():
    d = db_connector.DIALECTS["mssql"]
    assert d.connect_args({"statement_timeout": 300}, 8) == {"timeout": 300}
    assert d.query_args({"ssl": True, "connect_timeout": 8}) == {
        "driver": "ODBC Driver 18 for SQL Server", "Encrypt": "yes",
        "TrustServerCertificate": "no", "LoginTimeout": "8"}
    assert d.query_args({"ssl": True, "trust_server_certificate": True,
                         "connect_timeout": 8})["TrustServerCertificate"] == "yes"
    assert d.query_args({"connect_timeout": 8})["Encrypt"] == "no"


def test_clickhouse_args_unchanged():
    d = db_connector.DIALECTS["clickhouse"]
    assert d.connect_args({"ssl": True}, 8) == {}
    base = {"connect_timeout": 8, "statement_timeout": 300}
    assert d.query_args(base) == {"connect_timeout": "8",
                                  "send_receive_timeout": "330",
                                  "max_execution_time": "300"}
    assert d.query_args({**base, "ssl": True}) == {
        "connect_timeout": "8", "send_receive_timeout": "330",
        "max_execution_time": "300", "secure": "true"}
    assert d.query_args({**base, "ssl": True, "trust_server_certificate": True})[
        "verify"] == "false"


# ---------------------------------------------------------------------------
# Registry fields
# ---------------------------------------------------------------------------

def test_clickhouse_registry_defaults_to_tls():
    d = db_connector.DIALECTS["clickhouse"]
    assert d.default_port == 9440
    assert d.plaintext_port == 9000
    assert d.ssl_default is True


@pytest.mark.parametrize("key", ["postgresql", "mysql", "mariadb", "mssql",
                                 "oracle", "sqlite"])
def test_other_dialects_have_no_plaintext_port_and_no_ssl_default(key):
    d = db_connector.DIALECTS[key]
    assert d.plaintext_port is None
    assert d.ssl_default is False


def test_list_dialects_exposes_the_tls_fields():
    rows = {r["key"]: r for r in db_connector.list_dialects()}
    for r in rows.values():
        assert "plaintext_port" in r and "ssl_default" in r
    assert rows["clickhouse"]["plaintext_port"] == 9000
    assert rows["clickhouse"]["ssl_default"] is True
    assert rows["clickhouse"]["default_port"] == 9440
    assert rows["postgresql"]["plaintext_port"] is None
    assert rows["postgresql"]["ssl_default"] is False


# ---------------------------------------------------------------------------
# Port fallback for a blank port
# ---------------------------------------------------------------------------

_CH = {"db_type": "clickhouse", "host": "h", "database": "d", "user": "u"}


def test_clickhouse_blank_port_without_ssl_keeps_the_plaintext_port():
    assert db_connector.build_url(dict(_CH), "pw").port == 9000


def test_clickhouse_blank_port_with_ssl_gets_the_tls_port():
    assert db_connector.build_url({**_CH, "ssl": True}, "pw").port == 9440


@pytest.mark.parametrize("ssl_on", [True, False])
def test_explicit_port_always_wins(ssl_on):
    cfg = {**_CH, "port": 19000}
    if ssl_on:
        cfg["ssl"] = True
    assert db_connector.build_url(cfg, "pw").port == 19000


def test_postgres_blank_port_uses_its_default():
    url = db_connector.build_url(
        {"db_type": "postgresql", "host": "h", "database": "d", "user": "u"}, "pw")
    assert url.port == 5432


class _RaisingCursor(_FakeCursor):
    def execute(self, sql, *a, **k):
        self.executed.append(sql)
        raise RuntimeError("status variables unavailable")


def test_mysql_probe_that_raises_refuses_and_closes_the_connection():
    """A probe that cannot run leaves the channel's state unknown: the
    connection is refused like a plaintext one and never leaked open."""
    conn = _FakeDbapiConn(None)
    conn.cursor_obj = _RaisingCursor(None)
    with pytest.raises(db_connector.TLSNotNegotiated):
        db_connector._mysql_require_tls(conn, object())
    assert conn.close_calls == 1
    assert conn.cursor_obj.closed
