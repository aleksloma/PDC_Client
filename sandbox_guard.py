"""Import guard for the code-execution sandbox (Article VII).

Both exec sites (`code_exec.safe_execute`, `plot_utils.render_plot_safe`)
install `SANDBOX_BUILTINS` as the exec environment's __builtins__. Without
this, CPython injects the FULL builtins module (including __import__), so
generated code could `import psycopg2` and open a database connection with
the drivers this image now ships for the Data-sources feature.

DENYLIST, not allowlist: pandas/matplotlib/plotly/kaleido perform lazy
transitive imports at call time INSIDE the sandbox, so an allowlist would
have to enumerate the transitive closure of five plotting/ML stacks — a miss
would fail-freeze historical stored code that refresh_item/_reexecute_full_df
re-execute. A denylist miss is a gap, not an outage.

HONEST LIMIT (also stated in docs/AI_CONSTITUTION.md): this is defense in
depth, NOT a security boundary — open/eval remain available, and already-
imported modules stay reachable through object graphs. The load-bearing
controls are the SELECT-only database grant, credentials never existing in
cleartext at importable module scope, and db_connector being the only code
that can build a connection URL.
"""
from __future__ import annotations

import builtins as _builtins

# SQLAlchemy + every present and planned DB driver, sqlite3, and this
# client's own credential/connector surface.
DENIED_MODULES = frozenset({
    # DB stack
    "sqlalchemy", "psycopg2", "psycopg", "pymysql", "MySQLdb", "mysql",
    "mariadb", "pyodbc", "pymssql", "oracledb", "cx_Oracle", "sqlite3",
    "ibm_db", "hdbcli", "snowflake", "clickhouse_driver", "clickhouse_connect",
    # this client's credential surface
    "db_connector", "db_sources", "db_scheduler", "local_store", "settings",
    "brain_client", "password_utils",
    # role grants: sandboxed code must not rewrite roles.json (privilege
    # escalation for the DB-table role gate)
    "roles_store",
    # relation discovery: one hop from the registry, and the sandbox has no
    # business growing a SQL parser for free
    "relation_discovery", "sqlglot",
})

_REAL_IMPORT = _builtins.__import__


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".")[0] in DENIED_MODULES:
        raise ImportError(
            f"Import of '{name.split('.')[0]}' is not permitted in the "
            "analysis sandbox.")
    return _REAL_IMPORT(name, globals, locals, fromlist, level)


# Full builtins minus the real __import__ — everything generated code
# legitimately uses (print, len, range, sorted, Exception, ...) keeps working.
SANDBOX_BUILTINS = {**vars(_builtins), "__import__": _guarded_import}
