"""Registry of admin-registered database sources (connections + tables).

Storage: DATA_ROOT/data_sources.json — one JSON document holding
  - connections: how to reach a customer database (credentials Fernet-encrypted
    at rest with CLIENT_ENCRYPTION_KEY; masked in every API response; never
    logged; never included in any brain_client payload).
  - tables: registered tables (display name, ladmin-confirmed English
    descriptions, columns, is_connector flag, relations, row count / size
    estimates, refreshed_at, snapshot path).

Snapshots live at DATA_ROOT/db_snapshots/{table_id}.parquet — ONE central copy
per table, shared by every chat that uses it (chat meta references the table_id
only; a nightly refresh updates all chats at once).

Security invariants (see docs/AI_CONSTITUTION.md Article VII):
  - Passwords are encrypted at rest and decrypted only into function-locals at
    the moment of use — never stored at importable module scope in cleartext.
  - `_mask_connection` is the only way a connection leaves this store through
    an API; `get_connection(with_secret=True)` is called solely by the admin
    routes and the refresh scheduler.
  - The admin audit trail is an append-only JSONL (Article V pattern) with a
    recursive secret scrubber.

This module deliberately lives OUTSIDE local_store.py: local_store is imported
by nearly everything and must not grow a `cryptography` dependency. Atomic
writes and JSON sanitizing are REUSED from local_store, not copied.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from collections import deque
from pathlib import Path
from typing import Optional

from settings import settings
from logger_utils import log_with_sid
from local_store import (_data_root, _json_safe, _now, _write_json_atomic,
                         _DB_SNAPSHOT_DIRNAME as _SNAPSHOT_DIRNAME,
                         db_snapshot_path)

_LOCK = threading.RLock()

_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")

_DOC_NAME = "data_sources.json"
_AUDIT_NAME = "admin_audit.jsonl"

# Keys whose values must never appear in an audit row or a log line.
_SECRET_KEYS = {"password", "password_enc", "url", "url_override", "dsn", "connect_args"}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class EncryptionUnavailable(RuntimeError):
    """CLIENT_ENCRYPTION_KEY is missing or invalid — credentials cannot be
    encrypted. Callers surface a clear 503; there is NO plaintext fallback."""


# ---------------------------------------------------------------------------
# Fernet crypto (lazy import so pytest collection without `cryptography`
# fails only when the feature is actually used)
# ---------------------------------------------------------------------------

def _fernet():
    """MultiFernet([new] or [new, old]) from the env keys, or None when no
    usable key is configured. The key value itself is never logged."""
    key = (settings.CLIENT_ENCRYPTION_KEY or "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet, MultiFernet
        fernets = [Fernet(key.encode())]
        old = (settings.CLIENT_ENCRYPTION_KEY_OLD or "").strip()
        if old:
            try:
                fernets.append(Fernet(old.encode()))
            except Exception:
                log_with_sid("db_sources", "warning", "DB_ENCRYPTION_KEY_OLD_INVALID")
        return MultiFernet(fernets)
    except Exception:
        log_with_sid("db_sources", "warning", "DB_ENCRYPTION_KEY_INVALID")
        return None


def encrypt_password(plain: str) -> str:
    f = _fernet()
    if f is None:
        raise EncryptionUnavailable(
            "CLIENT_ENCRYPTION_KEY is not configured. Set it in the environment "
            "and restart the container.")
    return f.encrypt((plain or "").encode("utf-8")).decode("ascii")


def decrypt_password(enc: str) -> Optional[str]:
    """None on any failure (no key / rotated key / corrupt token) — never
    raises (Article IV). A None result surfaces as password_readable=false."""
    if not enc:
        return None
    f = _fernet()
    if f is None:
        return None
    try:
        return f.decrypt(enc.encode("ascii")).decode("utf-8")
    except Exception:
        return None


def encryption_ready() -> bool:
    return _fernet() is not None


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _doc_path() -> Path:
    return _data_root() / _DOC_NAME


def _audit_path() -> Path:
    return _data_root() / _AUDIT_NAME


# ---------------------------------------------------------------------------
# Masking + audit
# ---------------------------------------------------------------------------

def _mask_connection(c: dict) -> dict:
    """The ONLY shape in which a connection may leave this store via an API.
    Strips password_enc AND url_override (a DSN can embed credentials)."""
    out = {k: v for k, v in c.items() if k not in ("password_enc", "url_override")}
    has_pw = bool(c.get("password_enc"))
    out["password_set"] = has_pw
    out["password_readable"] = has_pw and decrypt_password(c.get("password_enc")) is not None
    out["password_masked"] = "••••••••" if has_pw else ""
    return out


def _scrub_detail(value, depth: int = 0):
    """Recursively replace secret-keyed values with '***' at any depth."""
    if depth > 6:
        return "***"
    if isinstance(value, dict):
        return {k: ("***" if str(k).lower() in _SECRET_KEYS else _scrub_detail(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_detail(v, depth + 1) for v in value]
    return value


def audit(actor: str, action: str, *, target: str = "", ok: bool = True,
          detail: Optional[dict] = None, ip: Optional[str] = None) -> None:
    """Append one admin-action row to DATA_ROOT/admin_audit.jsonl. Never
    raises; a failed write is logged and swallowed (Article IV)."""
    try:
        row = {"ts": _now(), "actor": actor, "action": action, "target": target,
               "ok": bool(ok)}
        if detail:
            row["detail"] = _scrub_detail(_json_safe(detail))
        if ip:
            row["ip"] = ip
        line = json.dumps(row, ensure_ascii=False)
        if len(line) > 4096:
            row["detail"] = {"_truncated": True}
            line = json.dumps(row, ensure_ascii=False)
        with _LOCK:
            with _audit_path().open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as e:
        log_with_sid(actor or "admin", "error", f"ADMIN_AUDIT_WRITE_FAILED: {e}")


def read_audit_tail(limit: int = 200) -> list[dict]:
    """Newest-first tail of the audit log. Best-effort; unreadable lines are
    skipped."""
    p = _audit_path()
    if not p.exists():
        return []
    rows: list[dict] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        for line in lines[-max(1, int(limit)):]:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception:
                continue
    except Exception as e:
        log_with_sid("admin", "warning", f"ADMIN_AUDIT_READ_FAILED: {e}")
    rows.reverse()
    return rows


# ---------------------------------------------------------------------------
# DataSourceStore
# ---------------------------------------------------------------------------

def _default_doc() -> dict:
    return {
        "version": 1,
        "updated_at": None,
        "settings": {
            "refresh_enabled": bool(settings.DB_REFRESH_ENABLED),
            "refresh_time": settings.DB_REFRESH_TIME if _TIME_RE.match(settings.DB_REFRESH_TIME or "") else "00:00",
            "last_run_at": None,
            "last_run_summary": None,
        },
        "connections": [],
        "tables": [],
    }


class DataSourceStore:
    """data_sources.json registry — same discipline as DashboardStore: every
    mutation under _LOCK with an atomic whole-file replace, every read .get()s
    with defaults so an old-shape doc keeps loading after an upgrade."""

    @staticmethod
    def valid_id(value) -> bool:
        return bool(isinstance(value, str) and _ID_RE.match(value))

    # ---- doc ---------------------------------------------------------------
    def read_doc(self) -> dict:
        p = _doc_path()
        if not p.exists():
            return _default_doc()
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                return _default_doc()
        except Exception as e:
            log_with_sid("db_sources", "error", f"DB_SOURCES_READ_FAILED: {e}")
            return _default_doc()
        base = _default_doc()
        base["version"] = doc.get("version", 1)
        base["updated_at"] = doc.get("updated_at")
        stg = doc.get("settings") or {}
        if isinstance(stg, dict):
            base["settings"].update({k: stg[k] for k in base["settings"] if k in stg})
        base["connections"] = [c for c in (doc.get("connections") or []) if isinstance(c, dict)]
        base["tables"] = [t for t in (doc.get("tables") or []) if isinstance(t, dict)]
        return base

    def _write_doc(self, doc: dict) -> None:
        doc["updated_at"] = _now()
        _write_json_atomic(_doc_path(), doc)

    # ---- connections -------------------------------------------------------
    def list_connections(self) -> list[dict]:
        return [_mask_connection(c) for c in self.read_doc()["connections"]]

    def get_connection(self, cid: str, *, with_secret: bool = False) -> Optional[dict]:
        """with_secret=True returns password_enc and url_override — used ONLY
        by the admin routes and the refresh scheduler, which decrypt into
        function-locals at the moment of use."""
        if not self.valid_id(cid):
            return None
        for c in self.read_doc()["connections"]:
            if c.get("id") == cid:
                return dict(c) if with_secret else _mask_connection(c)
        return None

    def create_connection(self, fields: dict, password: str, actor: str) -> dict:
        enc = encrypt_password(password)  # raises EncryptionUnavailable when no key
        now = _now()
        conn = {
            "id": secrets.token_hex(8),
            "name": (fields.get("name") or "").strip(),
            "db_type": (fields.get("db_type") or "").strip(),
            "host": (fields.get("host") or "").strip(),
            "port": fields.get("port"),
            "database": (fields.get("database") or "").strip() or None,
            "service_name": (fields.get("service_name") or "").strip() or None,
            "user": (fields.get("user") or "").strip(),
            "password_enc": enc,
            "password_set_at": now,
            "ssl": bool(fields.get("ssl")),
            "trust_server_certificate": bool(fields.get("trust_server_certificate")),
            "connect_timeout": int(fields.get("connect_timeout") or settings.DB_CONNECT_TIMEOUT),
            "statement_timeout": int(fields.get("statement_timeout") or settings.DB_STATEMENT_TIMEOUT),
            "url_override": (fields.get("url_override") or "").strip() or None,
            "created_at": now, "created_by": actor,
            "updated_at": now, "updated_by": actor,
            "last_test_at": None, "last_test_ok": None,
        }
        with _LOCK:
            doc = self.read_doc()
            doc["connections"].append(conn)
            self._write_doc(doc)
        audit(actor, "connection.create", target=conn["id"],
              detail={"name": conn["name"], "db_type": conn["db_type"],
                      "host": conn["host"], "port": conn["port"]})
        log_with_sid(actor, "info", f"DB_CONNECTION_CREATED conn={conn['id']} type={conn['db_type']}")
        return _mask_connection(conn)

    def update_connection(self, cid: str, fields: dict, password: Optional[str],
                          actor: str) -> Optional[dict]:
        """password=None keeps the stored credential; a string re-encrypts."""
        with _LOCK:
            doc = self.read_doc()
            for c in doc["connections"]:
                if c.get("id") == cid:
                    for k in ("name", "host", "database", "service_name", "user"):
                        if k in fields:
                            c[k] = (fields.get(k) or "").strip() or (None if k in ("database", "service_name") else "")
                    if "db_type" in fields and fields.get("db_type"):
                        c["db_type"] = str(fields["db_type"]).strip()
                    if "port" in fields:
                        c["port"] = fields.get("port")
                    for k in ("ssl", "trust_server_certificate"):
                        if k in fields:
                            c[k] = bool(fields.get(k))
                    for k in ("connect_timeout", "statement_timeout"):
                        if k in fields and fields.get(k) is not None:
                            c[k] = int(fields[k])
                    if "url_override" in fields:
                        c["url_override"] = (fields.get("url_override") or "").strip() or None
                    if password is not None and password != "":
                        c["password_enc"] = encrypt_password(password)
                        c["password_set_at"] = _now()
                    c["updated_at"] = _now()
                    c["updated_by"] = actor
                    self._write_doc(doc)
                    audit(actor, "connection.update", target=cid,
                          detail={"password_changed": bool(password)})
                    return _mask_connection(c)
        return None

    def mark_tested(self, cid: str, ok: bool) -> None:
        try:
            with _LOCK:
                doc = self.read_doc()
                for c in doc["connections"]:
                    if c.get("id") == cid:
                        c["last_test_at"] = _now()
                        c["last_test_ok"] = bool(ok)
                        self._write_doc(doc)
                        return
        except Exception as e:
            log_with_sid("db_sources", "warning", f"DB_CONN_MARK_TESTED_FAILED conn={cid}: {e}")

    def delete_connection(self, cid: str, actor: str, *, cascade: bool = False) -> dict:
        """Without cascade, refuses when tables still reference the connection
        (the route maps that to 409). With cascade, deletes those tables and
        their snapshots too."""
        with _LOCK:
            doc = self.read_doc()
            dependent = [t for t in doc["tables"] if t.get("connection_id") == cid]
            if dependent and not cascade:
                return {"ok": False,
                        "tables": [{"id": t.get("id"), "display_name": t.get("display_name")}
                                   for t in dependent]}
            before = len(doc["connections"])
            doc["connections"] = [c for c in doc["connections"] if c.get("id") != cid]
            deleted = len(doc["connections"]) < before
            deleted_tables = []
            if cascade and dependent:
                keep_ids = {t.get("id") for t in dependent}
                doc["tables"] = [t for t in doc["tables"] if t.get("id") not in keep_ids]
                deleted_tables = sorted(keep_ids)
            self._write_doc(doc)
        for tid in deleted_tables:
            self._drop_snapshot(tid)
        audit(actor, "connection.delete", target=cid, ok=deleted,
              detail={"cascade": cascade, "deleted_tables": deleted_tables})
        return {"ok": True, "deleted_tables": deleted_tables}

    # ---- tables ------------------------------------------------------------
    def list_tables(self, *, include_connector: bool = True) -> list[dict]:
        tables = self.read_doc()["tables"]
        if include_connector:
            return [dict(t) for t in tables]
        return [dict(t) for t in tables if not t.get("is_connector")]

    def get_table(self, tid: str) -> Optional[dict]:
        if not self.valid_id(tid):
            return None
        for t in self.read_doc()["tables"]:
            if t.get("id") == tid:
                return dict(t)
        return None

    def upsert_table(self, table_doc: dict, actor: str) -> dict:
        """Insert (no/unknown id) or replace (existing id) a table doc. The
        caller (routes/admin_data.py) is responsible for the mandatory-confirm
        gate; this store never invents confirmation fields."""
        with _LOCK:
            doc = self.read_doc()
            tid = table_doc.get("id")
            if not self.valid_id(tid):
                tid = secrets.token_hex(8)
                table_doc = dict(table_doc)
                table_doc["id"] = tid
                table_doc.setdefault("created_at", _now())
            table_doc["updated_at"] = _now()
            table_doc["snapshot_path"] = f"{_SNAPSHOT_DIRNAME}/{tid}.parquet"
            replaced = False
            for i, t in enumerate(doc["tables"]):
                if t.get("id") == tid:
                    doc["tables"][i] = table_doc
                    replaced = True
                    break
            if not replaced:
                doc["tables"].append(table_doc)
            self._write_doc(doc)
        audit(actor, "table.save", target=tid,
              detail={"display_name": table_doc.get("display_name"),
                      "schema": table_doc.get("schema"),
                      "table_name": table_doc.get("table_name"),
                      "is_connector": bool(table_doc.get("is_connector")),
                      "confirmed": bool(table_doc.get("descriptions_confirmed_by")),
                      "columns": len(table_doc.get("columns") or [])})
        return dict(table_doc)

    def delete_table(self, tid: str, actor: str, *, drop_snapshot: bool = True) -> bool:
        with _LOCK:
            doc = self.read_doc()
            before = len(doc["tables"])
            doc["tables"] = [t for t in doc["tables"] if t.get("id") != tid]
            deleted = len(doc["tables"]) < before
            if deleted:
                self._write_doc(doc)
        if deleted and drop_snapshot:
            self._drop_snapshot(tid)
        audit(actor, "table.delete", target=tid, ok=deleted)
        return deleted

    def _drop_snapshot(self, tid: str) -> None:
        try:
            p = db_snapshot_path(tid)
            if p.exists():
                p.unlink()
        except Exception as e:
            log_with_sid("db_sources", "warning", f"DB_SNAPSHOT_DELETE_FAILED table={tid}: {e}")

    def mark_refreshed(self, tid: str, *, rows: Optional[int] = None,
                       snapshot_bytes: Optional[int] = None,
                       columns: Optional[list] = None,
                       dtype_plan: Optional[dict] = None,
                       error: Optional[str] = None) -> None:
        """Update refresh bookkeeping on a table row. On error, refreshed_at is
        left at its previous value (chats keep the last good snapshot)."""
        try:
            with _LOCK:
                doc = self.read_doc()
                for t in doc["tables"]:
                    if t.get("id") == tid:
                        if error is None:
                            t["refreshed_at"] = _now()
                            t["last_refresh_error"] = None
                            if rows is not None:
                                t["snapshot_rows"] = int(rows)
                                t["row_count"] = int(rows)
                            if snapshot_bytes is not None:
                                t["snapshot_bytes"] = int(snapshot_bytes)
                            if columns is not None:
                                t["columns"] = columns
                            if dtype_plan is not None:
                                t["dtype_plan"] = dtype_plan
                        else:
                            t["last_refresh_error"] = str(error)[:500]
                        self._write_doc(doc)
                        return
        except Exception as e:
            log_with_sid("db_sources", "error", f"DB_MARK_REFRESHED_FAILED table={tid}: {e}")

    # ---- refresh settings --------------------------------------------------
    def get_refresh_settings(self) -> dict:
        return dict(self.read_doc()["settings"])

    def set_refresh_settings(self, *, refresh_time: str, refresh_enabled: bool,
                             actor: str) -> dict:
        if not _TIME_RE.match(refresh_time or ""):
            raise ValueError("refresh_time must be HH:MM (24h)")
        with _LOCK:
            doc = self.read_doc()
            doc["settings"]["refresh_time"] = refresh_time
            doc["settings"]["refresh_enabled"] = bool(refresh_enabled)
            self._write_doc(doc)
            out = dict(doc["settings"])
        audit(actor, "refresh.settings", detail={"refresh_time": refresh_time,
                                                 "refresh_enabled": bool(refresh_enabled)})
        return out

    def record_run(self, summary: dict) -> None:
        try:
            with _LOCK:
                doc = self.read_doc()
                doc["settings"]["last_run_at"] = _now()
                doc["settings"]["last_run_summary"] = _scrub_detail(_json_safe(summary))
                self._write_doc(doc)
        except Exception as e:
            log_with_sid("db_sources", "warning", f"DB_RECORD_RUN_FAILED: {e}")


# ---------------------------------------------------------------------------
# Connector transitive closure
# ---------------------------------------------------------------------------

def expand_with_connectors(table_ids: list[str],
                           store: Optional[DataSourceStore] = None) -> list[str]:
    """Given user-selected (non-connector) table ids, return the sorted closure
    including every CONNECTOR table reachable through the relations graph.

    - The graph is undirected (admins usually declare a relation on one side).
    - Only `is_connector` tables are pulled in — a relation to a normal table
      is never followed (it would drag the whole warehouse into one chat).
    - Connector→connector chains are followed transitively; cycles terminate
      via the membership check.
    - Deterministic (sorted) so the df-key order feeds a stable schema_text
      cache key; capped at settings.DB_MAX_AUTO_CONNECTORS auto-includes.
    """
    store = store or DataSourceStore()
    tables = {t.get("id"): t for t in store.list_tables() if t.get("id")}
    by_name = {}
    for t in tables.values():
        by_name[f"{t.get('schema') or ''}.{t.get('table_name') or ''}".lower()] = t.get("id")
        by_name[(t.get("table_name") or "").lower()] = t.get("id")

    def _resolve(rel: dict) -> Optional[str]:
        rid = rel.get("related_table_id") or rel.get("related_table")
        if rid in tables:
            return rid
        if isinstance(rid, str) and rid.lower() in by_name:
            return by_name[rid.lower()]
        return None

    neighbors: dict[str, set] = {}
    for t in tables.values():
        for rel in (t.get("relations") or []):
            if not isinstance(rel, dict):
                continue
            other = _resolve(rel)
            if other is None:
                log_with_sid("db_sources", "warning",
                             f"DB_RELATION_DANGLING table={t.get('id')} rel={rel.get('related_table_id') or rel.get('related_table')}")
                continue
            neighbors.setdefault(t["id"], set()).add(other)
            neighbors.setdefault(other, set()).add(t["id"])

    seeds = [tid for tid in table_ids if tid in tables]
    result = set(seeds)
    queue = deque(sorted(seeds))
    cap = max(0, int(settings.DB_MAX_AUTO_CONNECTORS))
    auto_added = 0
    while queue:
        tid = queue.popleft()
        for nid in sorted(neighbors.get(tid, ())):
            if nid in result:
                continue
            if not tables[nid].get("is_connector"):
                continue
            if auto_added >= cap:
                log_with_sid("db_sources", "warning",
                             f"DB_CONNECTOR_CLOSURE_CAPPED cap={cap}")
                queue.clear()
                break
            result.add(nid)
            auto_added += 1
            queue.append(nid)
    return sorted(result)
