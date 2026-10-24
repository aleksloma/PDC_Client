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
    "ibm_db", "hdbcli", "snowflake", "clickhouse_driver",
    "clickhouse_connect", "clickhouse_sqlalchemy",
    # this client's credential surface
    "db_connector", "db_sources", "db_scheduler", "local_store", "settings",
    "brain_client", "password_utils",
    # SSO config: sso_store.load() would hand back the DECRYPTED Entra
    # client secret (and its module-level `import db_sources` runs under
    # real builtins, so denying db_sources alone does not cover it)
    "sso_store",
    # Optional direct-upload transport + the google namespace it imports:
    # with a bucket configured, google.auth.default() would hand generated
    # code the runtime credentials.
    "gcs_upload",
    "google",
    # role grants: sandboxed code must not rewrite roles.json (privilege
    # escalation for the DB-table role gate)
    "roles_store",
    # approved sign-in domains: rewriting signin_domains.json would let
    # generated code open self-service sign-up for any domain
    "signin_domains",
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


# ---------------------------------------------------------------------------
# `pdc_diag` cannot be redefined (BRB retest follow-up)
# ---------------------------------------------------------------------------
# A retry block once "replicated" the diagnostics library
# (`def detect_anomalies(...)`, `FINDINGS = ""`) and silently replaced the
# real findings with its own. Both exec sites run this AST check BEFORE
# anything executes and refuse such a block with the fixed text below, which
# carries no code (it travels to the brain's retry prompt).
#
# HONEST LIMITS (defense in depth, not a boundary): `setattr(pdc_diag, ...)`,
# `globals()["pdc_diag"] = ...` and `pdc_diag.__dict__[...] = ...` are not
# caught; a name that merely CONTAINS `pdc_diag` (`pdc_diag_out`,
# `my_pdc_diag`) is accepted; a plain `import pdc_diag` (or
# `import pdc_diag as pdc_diag`) is accepted because it rebinds the real
# module. Code that does not parse is left to exec, which reports Python's
# own SyntaxError as today.

import ast as _ast

PDC_DIAG_REDEFINED_TEXT = "pdc_diag cannot be redefined"

_PDC_DIAG = "pdc_diag"

PDC_DIAG_PUBLIC = frozenset({
    "fmt", "coverage_check", "detect_anomalies", "decompose_change",
    "attribute_level", "check_metric_overlap",
    "plot_highlighted_series", "plot_waterfall", "plot_importance",
})

_STORE_DEL = (_ast.Store, _ast.Del)


def _node_redefines(node) -> bool:
    if isinstance(node, _ast.Name):
        return node.id == _PDC_DIAG and isinstance(node.ctx, _STORE_DEL)
    if isinstance(node, _ast.Attribute):
        return (isinstance(node.ctx, _STORE_DEL)
                and isinstance(node.value, _ast.Name)
                and node.value.id == _PDC_DIAG)
    if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
        return node.name == _PDC_DIAG or node.name in PDC_DIAG_PUBLIC
    if isinstance(node, _ast.ClassDef):
        return node.name == _PDC_DIAG
    if isinstance(node, _ast.ExceptHandler):
        return node.name == _PDC_DIAG
    if isinstance(node, (_ast.Global, _ast.Nonlocal)):
        return _PDC_DIAG in node.names
    if isinstance(node, _ast.Import):
        # `import X as pdc_diag` binds another module under the name;
        # `import pdc_diag` / `import pdc_diag as pdc_diag` rebinds the real one.
        return any(a.asname == _PDC_DIAG and a.name != _PDC_DIAG for a in node.names)
    if isinstance(node, _ast.ImportFrom):
        # `from M import pdc_diag` / `from M import N as pdc_diag` bind
        # whatever M exposes — never assumed to be the real module.
        return any((a.asname or a.name) == _PDC_DIAG for a in node.names)
    return False


def _log_guard_event(level: str, message: str) -> None:
    """Value-free log line (constant text + exception type only). The logger
    is imported lazily so this module stays a stdlib-only leaf at import."""
    try:
        from logger_utils import log_with_sid
        log_with_sid("exec", level, message)
    except Exception:
        pass


def pdc_diag_redefinition(code) -> str | None:
    """`PDC_DIAG_REDEFINED_TEXT` when `code` rebinds, deletes or mutates
    `pdc_diag`, or defines a function under one of its public names; else
    None (also for non-str, empty or unparseable code). Never raises."""
    try:
        if not isinstance(code, str) or not code.strip():
            return None
        try:
            tree = _ast.parse(code)
        except Exception as e:
            # Unparseable: exec reports Python's own SyntaxError as today.
            _log_guard_event("debug", f"PDC_DIAG_GUARD_PARSE_SKIPPED error={type(e).__name__}")
            return None
        for node in _ast.walk(tree):
            if _node_redefines(node):
                return PDC_DIAG_REDEFINED_TEXT
        return None
    except Exception as e:
        # A guard failure must never turn into an exec failure; the exec
        # itself still runs under SANDBOX_BUILTINS.
        _log_guard_event("warning", f"PDC_DIAG_GUARD_FAILED error={type(e).__name__}")
        return None
