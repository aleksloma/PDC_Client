"""Relation discovery for admin-registered database tables (ladmin review).

Pure logic — no store, network, or filesystem access. Candidate generation
(declared FKs, name/description similarity, pasted-SQL join extraction),
snapshot verification (cardinality / overlap / orphans), and banding for the
review UI. The route layer (routes/admin_data.py) owns introspection, the
registry, and snapshot loading.

Security invariants (Article II / VII):
  - Pasted SQL is parsed in memory only. The SQL TEXT is never persisted,
    logged, audited, or sent to the brain — and neither are sqlglot's error
    messages, which embed the offending SQL text. Only table/column
    IDENTIFIERS and statement counts extracted from it may persist locally
    (the "Recommended tables" evidence); literals never survive extraction
    because only Column = Column predicates are read at all.
  - Verification emits aggregates only (counts, percentages, dtype names).
    No cell value ever enters a candidate dict, a log line, or a response.

Nothing here applies anything: candidates are proposals until ladmin accepts
them through the /api/admin/relations/accept endpoint.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Callable, Optional

import pandas as pd

from logger_utils import log_with_sid

# ── Tunables (one block by design — see docs/ENTERPRISE_ARCHITECTURE.md) ────
CONFIRMED_OVERLAP = 95.0      # band: confirmed when >= and cardinality 1:1/N:1
SUGGESTED_OVERLAP = 70.0      # band: below this -> needs attention
SQL_CONFIRMED_FREQ = 2        # SQL pair seen in >= N distinct statements
NAME_MAX_TABLE_SHARE = 0.5    # normalized name in > this share of tables -> never proposed
MIN_UBIQUITY_TABLES = 3       # ...but only once it appears in at least N tables
DESC_SIM_THRESHOLD = 0.6      # token-Jaccard on confirmed descriptions
NAME_SCORE_THRESHOLD = 0.55   # idf-weighted name score floor
GENERIC_NAMES = frozenset({"id", "code", "name", "status", "date", "type",
                           "no", "num", "key", "value", "desc", "description"})
STRIP_PREFIXES = ("id_", "tbl_", "fk_")
STRIP_SUFFIXES = ("_id", "_code", "_no", "_key", "_num", "_nbr")
DESC_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "is",
    "this", "that", "with", "by", "as", "at", "from", "it", "its",
    "table", "column", "field", "value", "values", "unique", "identifier",
})
SQLGLOT_DIALECT = {"postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql",
                   "mssql": "tsql", "oracle": "oracle", "sqlite": "sqlite"}
CARDINALITY_LABEL = {"N:1": "many-to-one", "1:1": "one-to-one",
                     "1:N": "one-to-many", "N:M": "many-to-many"}
SOURCE_PRECEDENCE = ("fk", "sql", "name", "description")
_MAX_RESOLVE_DEPTH = 5        # CTE/subquery column-resolution recursion cap
MAX_CANDIDATES_PER_SOURCE = 200  # bound for the O(tables²·cols²) pair scans
UNREG_TABLE_CAP = 20          # distinct unregistered tables tracked per analyze


# ---------------------------------------------------------------------------
# Normalization + identity
# ---------------------------------------------------------------------------

def normalize_column_name(name: str) -> str:
    """Lowercase; canonicalize every separator run to `_` (so `CITY CODE`
    behaves like `city_code`); strip ONE known prefix and ONE known suffix;
    drop remaining separators. `Customer_ID` == `customerid` == `id_customer`
    -> `customer`. Degenerate empty result -> plain lowercased original."""
    raw = str(name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    for p in STRIP_PREFIXES:
        if s.startswith(p) and len(s) > len(p):
            s = s[len(p):]
            break
    for suf in STRIP_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    s = s.replace("_", "")
    return s or re.sub(r"[^a-z0-9]", "", raw) or raw


def _stem(s: str) -> str:
    """Drop one trailing plural 's' so `customers` matches `customer`."""
    return s[:-1] if len(s) > 3 and s.endswith("s") else s


def candidate_id(tid_a: str, cols_a: list, tid_b: str, cols_b: list) -> str:
    """Direction- and pair-order-independent identity for one candidate, so
    FK (directed) and name/SQL (undirected) evidence for the same join merge."""
    pairs = sorted(
        tuple(sorted([(str(tid_a), str(ca)), (str(tid_b), str(cb))]))
        for ca, cb in zip(cols_a, cols_b)
    )
    return hashlib.sha1(repr(pairs).encode("utf-8")).hexdigest()[:16]


def _label(t: dict) -> str:
    return t.get("display_name") or t.get("table_name") or t.get("id") or "?"


def _has_column(t: dict, col: str) -> bool:
    """Case-insensitive column-existence check against registry metadata.
    Case-insensitive on purpose: sqlglot's qualify normalizes identifier case
    on the happy path (lowercase on postgres, uppercase on oracle), but the
    qualify-FALLBACK path preserves the raw SQL casing — an exact match would
    false-flag valid pairs there, while a column absent under ANY casing is
    absent case-insensitively. A doc with NO column metadata (legacy shapes)
    is unvalidatable — treated like an unresolved side, never flagged."""
    names = [str(c.get("name") or "").lower()
             for c in (t.get("columns") or []) if c.get("name")]
    if not names:
        return True
    return str(col or "").lower() in names


def _make_candidate(child: dict, parent: dict, join_keys: list,
                    source: str, evidence) -> dict:
    jk = [[str(a), str(b)] for a, b in join_keys]
    return {
        "candidate_id": candidate_id(child["id"], [p[0] for p in jk],
                                     parent["id"], [p[1] for p in jk]),
        "table_id": child["id"], "table_label": _label(child),
        "related_table_id": parent["id"], "related_label": _label(parent),
        "join_keys": jk,
        "sources": [source],
        "sql_frequency": 0,
        "evidence": {source: evidence},
        "verified": False,
        "cardinality": None,
        "overlap_pct": None,
        "orphans": None,
        "child_nonnull": None,
        "child_unique": None,
        "parent_unique": None,
        "unverified_reason": None,
        "band": None,
    }


# ---------------------------------------------------------------------------
# Registry lookup maps (shared by the FK and SQL sources)
# ---------------------------------------------------------------------------

def build_registered_map(tables: list) -> dict:
    """Physical-name lookup: {(schema_lower, table_lower): [table, ...]} plus
    name-only keys {table_lower: [table, ...]} for the unambiguous fallback.
    Case-insensitive on purpose (Oracle/mssql catalogs case-fold)."""
    by_schema_table: dict = {}
    by_name: dict = {}
    for t in tables or []:
        tn = str(t.get("table_name") or "").lower()
        if not tn or not t.get("id"):
            continue
        sc = str(t.get("schema") or "").lower()
        by_schema_table.setdefault((sc, tn), []).append(t)
        by_name.setdefault(tn, []).append(t)
    return {"by_schema_table": by_schema_table, "by_name": by_name}


def _resolve_physical(registered: dict, schema: str, table: str) -> list:
    """Registered tables matching a physical (schema, table) name; schema-
    qualified match first, then name-only iff unambiguous."""
    sc, tn = str(schema or "").lower(), str(table or "").lower()
    hit = registered["by_schema_table"].get((sc, tn))
    if hit:
        return hit
    rows = registered["by_name"].get(tn) or []
    if len(rows) == 1:
        return rows
    # Several rows that are all registrations of ONE physical table are not
    # ambiguous — they are duplicates (legacy state) and resolve fine.
    if rows and len({physical_key(t) for t in rows}) == 1:
        return rows
    return []


# ---------------------------------------------------------------------------
# Physical identity (duplicate registrations of one source table)
# ---------------------------------------------------------------------------

def physical_key(t: dict) -> tuple:
    """Identity of the underlying SOURCE table: (connection_id, schema,
    table_name), case-insensitive on the name parts. Two registrations with
    the same key are copies of one physical table."""
    return (str(t.get("connection_id") or ""),
            str(t.get("schema") or "").lower(),
            str(t.get("table_name") or "").lower())


def _keys_match(a: tuple, b: tuple) -> bool:
    """Physical keys match only when complete on both sides — a doc without a
    connection_id or table_name (e.g. a wizard draft that didn't send one)
    must never false-positive against a real registration."""
    return a == b and bool(a[0]) and bool(a[2])


def registration_rank(t: dict) -> tuple:
    """Preference order among registrations of ONE physical table: connector
    first (connectors exist to be auto-included via relations), then the
    earliest-registered, then id — fully deterministic."""
    return (0 if t.get("is_connector") else 1,
            str(t.get("created_at") or "~"), str(t.get("id") or ""))


def prefer_registration(hits: list) -> Optional[dict]:
    """The preferred registration among resolution hits — but ONLY when they
    all share one physical key (duplicate registrations). Hits spanning
    DIFFERENT physical tables (a same-named table on two connections) stay
    genuinely ambiguous -> None."""
    if not hits:
        return None
    if len({physical_key(t) for t in hits}) > 1:
        return None
    return min(hits, key=registration_rank)


def filter_same_physical(candidates: list, tables: list) -> list:
    """Drop candidates whose two endpoints are registrations of the SAME
    physical table — a join of a table to its own copy is never a relation.
    Generation-side only; confirmed relations are never touched here."""
    by_id = {t.get("id"): t for t in tables or []}
    out = []
    for c in candidates or []:
        child = by_id.get(c.get("table_id"))
        parent = by_id.get(c.get("related_table_id"))
        if child is not None and parent is not None and \
                _keys_match(physical_key(child), physical_key(parent)):
            continue
        out.append(c)
    return out


def filter_existing_physical(candidates: list, tables: list) -> list:
    """Like filter_existing, but declared relations suppress candidates at the
    PHYSICAL level: a relation confirmed to ANY registration of a physical
    table also blocks re-proposals to its duplicate registrations (same
    columns, either orientation). For single registrations this is identical
    to the id-level filter."""
    registered = build_registered_map(tables)
    by_id = {t.get("id"): t for t in tables or []}

    def _phys_id(t1, cols1, t2, cols2):
        pairs = sorted(
            tuple(sorted([(physical_key(t1), str(c1)), (physical_key(t2), str(c2))]))
            for c1, c2 in zip(cols1, cols2))
        return repr(pairs)

    existing: set = set()
    for t in tables or []:
        for rel in t.get("relations") or []:
            if not isinstance(rel, dict):
                continue
            other = by_id.get(rel.get("related_table_id"))
            if other is None and rel.get("related_table"):
                name = str(rel.get("related_table"))
                sc, _, tn = name.rpartition(".")
                hits = _resolve_physical(registered, sc, tn)
                other = hits[0] if hits else None
            if other is None:
                continue
            try:
                jk = [(str(p[0]), str(p[1])) for p in (rel.get("join_keys") or [])]
            except Exception as e:
                log_with_sid("relation_discovery", "warning",
                             f"REL_EXISTING_MALFORMED table={t.get('id')}: {type(e).__name__}")
                continue
            if jk:
                existing.add(_phys_id(t, [p[0] for p in jk],
                                      other, [p[1] for p in jk]))
    out = []
    for c in candidates or []:
        child = by_id.get(c.get("table_id"))
        parent = by_id.get(c.get("related_table_id"))
        if child is not None and parent is not None and _phys_id(
                child, [p[0] for p in c["join_keys"]],
                parent, [p[1] for p in c["join_keys"]]) in existing:
            continue
        out.append(c)
    return out


def dedupe_physical_targets(candidates: list, tables: list) -> list:
    """While duplicate registrations exist (legacy state), collapse candidates
    that differ only in WHICH registration of a physical table they point at
    into ONE — the member whose endpoints are the preferred registrations
    (registration_rank, applied to both sides). The kept candidate carries
    `alternate_targets` [{id, label}] for the other parent registrations so
    the UI can offer a retarget."""
    by_id = {t.get("id"): t for t in tables or []}

    def _group_key(c):
        child, parent = by_id.get(c.get("table_id")), by_id.get(c.get("related_table_id"))
        if child is None or parent is None:
            return ("solo", c.get("candidate_id"))
        pairs = sorted(
            tuple(sorted([(physical_key(child), str(a)), (physical_key(parent), str(b))]))
            for a, b in c["join_keys"])
        return ("phys", repr(pairs))

    def _rank(c):
        child, parent = by_id.get(c.get("table_id")), by_id.get(c.get("related_table_id"))
        return (registration_rank(child) if child else (9,),
                registration_rank(parent) if parent else (9,))

    groups: dict = {}
    order: list = []
    for c in candidates or []:
        k = _group_key(c)
        if k not in groups:
            order.append(k)
        groups.setdefault(k, []).append(c)

    out = []
    for k in order:
        members = sorted(groups[k], key=_rank)
        chosen = members[0]
        if len(members) > 1:
            seen_alt = {chosen.get("related_table_id")}
            alts = []
            for m in members[1:]:
                rid = m.get("related_table_id")
                if rid in seen_alt:
                    continue
                seen_alt.add(rid)
                alts.append({"id": rid, "label": m.get("related_label")})
            if alts:
                chosen = dict(chosen)
                chosen["alternate_targets"] = alts
        out.append(chosen)
    return out


# ---------------------------------------------------------------------------
# Missing-table hints + graph assembly (pure; rendered by the admin UI)
# ---------------------------------------------------------------------------

def unregistered_fk_refs(tables: list, fk_map: dict) -> list:
    """FKs on registered tables that point at UNREGISTERED physical tables —
    the dictionary/bridge tables the admin forgot to register. Connection-
    scoped: a same-named registration on ANOTHER connection does not mask a
    missing table (FKs never cross connections, so the ghost's connection is
    the child's). Referred schema falls back to the child's (inspectors
    return None for search-path refs)."""
    registered = build_registered_map(tables)
    by_id = {t.get("id"): t for t in tables or []}
    refs: dict = {}
    for tid, intro in (fk_map or {}).items():
        child = by_id.get(tid)
        if not child or not isinstance(intro, dict) or not intro.get("ok"):
            continue
        conn = str(child.get("connection_id") or "")
        for fk in intro.get("foreign_keys") or []:
            if not isinstance(fk, dict):
                continue
            rtable = str(fk.get("referred_table") or "").strip()
            if not rtable:
                continue
            rschema = str(fk.get("referred_schema")
                          or child.get("schema") or "").strip()
            hits = _resolve_physical(registered, rschema, rtable)
            if any(str(h.get("connection_id") or "") == conn for h in hits):
                continue
            key = (conn, rschema.lower(), rtable.lower())
            entry = refs.setdefault(key, {
                "connection_id": conn, "schema": rschema, "table": rtable,
                "referenced_by": [], "referenced_by_ids": [],
                "referenced_pairs": []})
            label = child.get("display_name") or child.get("table_name")
            # Column pairs, MISSING-table-column first ([referred, constrained])
            # — the same anchor-first orientation the SQL evidence uses.
            # Length mismatch -> no pairs for this FK (lenient, additive).
            ccols = [str(x) for x in (fk.get("constrained_columns") or [])]
            rcols = [str(x) for x in (fk.get("referred_columns") or [])]
            pairs = [[r, cc] for cc, r in zip(ccols, rcols)] \
                if ccols and len(ccols) == len(rcols) else []
            if child.get("id") not in entry["referenced_by_ids"]:
                entry["referenced_by_ids"].append(child.get("id"))
                entry["referenced_by"].append(str(label))
                entry["referenced_pairs"].append(pairs)
            else:
                idx = entry["referenced_by_ids"].index(child.get("id"))
                for p in pairs:
                    if p not in entry["referenced_pairs"][idx]:
                        entry["referenced_pairs"][idx].append(p)
    out = [refs[k] for k in sorted(refs)]
    for e in out:
        order = sorted(range(len(e["referenced_by"])),
                       key=lambda i: e["referenced_by"][i])
        e["referenced_by"] = [e["referenced_by"][i] for i in order]
        e["referenced_by_ids"] = [e["referenced_by_ids"][i] for i in order]
        e["referenced_pairs"] = [e["referenced_pairs"][i] for i in order]
    return out


def resolve_unknown_tables(unknown: list, tables: list, connections: list) -> list:
    """Register-shortcut hints for analyze_sql's unknown table names. A hint
    is offered only when the connection is unambiguous: exactly one connection
    exists, or exactly one connection's registered tables share the entry's
    schema. Names that already match a registered table get NO hint —
    re-registering them is blocked (bare-ambiguous names resolve to [] in
    extraction but ARE registered)."""
    registered = build_registered_map(tables)
    hints = []
    for name in unknown or []:
        raw = str(name)
        sc, _, tn = raw.rpartition(".")
        sc_l, tn_l = sc.lower(), tn.lower()
        if registered["by_name"].get(tn_l) or \
                registered["by_schema_table"].get((sc_l, tn_l)):
            continue
        conn_id = None
        conns_list = [c for c in (connections or []) if isinstance(c, dict)]
        if len(conns_list) == 1:
            conn_id = conns_list[0].get("id")
        elif sc_l:
            conns = {str(t.get("connection_id") or "") for t in tables or []
                     if str(t.get("schema") or "").lower() == sc_l}
            conns.discard("")
            if len(conns) == 1:
                conn_id = next(iter(conns))
        if conn_id:
            hints.append({"name": raw, "connection_id": conn_id,
                          "schema": sc, "table": tn})
    return hints


def validate_rec_evidence(rec: dict, tables: list) -> tuple:
    """(clean_evidence, invalid) — replay-time column validation for stored
    evidence, applied to every side that RESOLVES to a registration in the
    CURRENT registry. The columns were unknowable at analyze time when the
    table was unregistered (analysis is metadata-only by design); once they
    are known, a pair naming a column the registration does not have is
    dropped and reported — never a bogus candidate, never a silent drop. An
    entry is dropped only when NO valid pairs remain; unresolvable sides stay
    unvalidated (nothing knowable). This is also the corrupted-store guard:
    stale/wrong evidence from any earlier release dies here, no migration.
    Case-insensitive (same rule as analyze time); identifiers only.
    invalid = [{"table": label, "column": col}] deduped."""
    registered = build_registered_map(tables)
    rconn = str(rec.get("connection_id") or "")
    rhits = _resolve_physical(registered, rec.get("schema"), rec.get("table"))
    if rconn:
        rhits = [h for h in rhits if str(h.get("connection_id") or "") == rconn]
    child = prefer_registration(rhits)
    clean: list = []
    invalid: list = []
    seen: set = set()

    def _bad(t: dict, col) -> None:
        key = (_label(t), str(col))
        if key not in seen:
            seen.add(key)
            invalid.append({"table": _label(t), "column": str(col)})

    for ev in rec.get("evidence") or []:
        if not isinstance(ev, dict):
            continue
        other = ev.get("other") or {}
        oconn = str(other.get("connection_id") or "") or rconn
        ohits = _resolve_physical(registered, other.get("schema"),
                                  other.get("table"))
        if oconn:
            ohits = [h for h in ohits
                     if str(h.get("connection_id") or "") == oconn]
        parent = prefer_registration(ohits)
        pairs_ok: list = []
        dropped = False
        for p in ev.get("pairs") or []:
            if not (isinstance(p, (list, tuple)) and len(p) == 2):
                dropped = True
                continue
            ok = True
            if child is not None and not _has_column(child, p[0]):
                _bad(child, p[0])
                ok = False
            if parent is not None and not _has_column(parent, p[1]):
                _bad(parent, p[1])
                ok = False
            if ok:
                pairs_ok.append([str(p[0]), str(p[1])])
            else:
                dropped = True
        if not pairs_ok and (ev.get("pairs") or []):
            continue                    # nothing valid left in this entry
        clean.append({**ev, "pairs": pairs_ok} if dropped else ev)
    return clean, invalid


def recommendation_candidates(rec: dict, tables: list) -> list:
    """Relation candidates replayed from a recommendation's stored SQL
    evidence once its table is registered. SQL evidence ONLY — FK evidence is
    deliberately never replayed: the next scan's live introspection re-derives
    those through fk_candidates, and a replayed candidate must never carry
    "fk" in sources (band()'s fk-auto-confirm is for ground truth only).
    Stored pairs are anchor-column-first, so child = the recommended table;
    verification flips direction as usual if the data says otherwise. Others
    still unregistered or ambiguous are skipped — their evidence stays stored
    until that side is registered too. v4.1: evidence passes
    validate_rec_evidence first — pairs naming columns a now-known
    registration lacks can never become candidates (the route surfaces them
    as warnings)."""
    registered = build_registered_map(tables)
    rconn = str(rec.get("connection_id") or "")
    hits = _resolve_physical(registered, rec.get("schema"), rec.get("table"))
    if rconn:
        hits = [h for h in hits if str(h.get("connection_id") or "") == rconn]
    child = prefer_registration(hits)
    if child is None:
        return []
    clean, _invalid = validate_rec_evidence(rec, tables)
    out = []
    for ev in clean:
        if not isinstance(ev, dict) or ev.get("origin") != "sql":
            continue
        other = ev.get("other") or {}
        pairs = [(str(p[0]), str(p[1])) for p in (ev.get("pairs") or [])
                 if isinstance(p, (list, tuple)) and len(p) == 2]
        if not pairs:
            continue
        oconn = str(other.get("connection_id") or "") or rconn
        ohits = _resolve_physical(registered, other.get("schema"),
                                  other.get("table"))
        if oconn:
            ohits = [h for h in ohits
                     if str(h.get("connection_id") or "") == oconn]
        parent = prefer_registration(ohits)
        if parent is None or parent.get("id") == child.get("id"):
            continue
        count = int(ev.get("count") or 0)
        cand = _make_candidate(child, parent, pairs, "sql",
                               {"frequency": count})
        cand["sql_frequency"] = count
        out.append(cand)
    return out


def recommendation_summary(rec: dict, tables: list) -> dict:
    """Read-time enrichment of one stored recommendation for the admin UI:
    dynamic role (bridge = its evidence joins >= 2 DIFFERENT REGISTERED
    physical tables in the CURRENT registry, else referenced — never stored,
    and unregistered partners deliberately don't count: the semantic is
    "registering it CONNECTS >= 2 registered tables"), the FULL join-partner
    list — v4.1: unresolved partners are listed with registered:false instead
    of being dropped, so a row is never frequency-only — the locked "pending
    relations" preview (SQL-origin evidence only: replay never proposes FK
    evidence, scan re-derives that live after registration), and the per-rec
    replay-validation warnings. Evidence passes validate_rec_evidence first,
    so invalid pairs appear in evidence_warnings, never in joins/pending.
    Pure; identifiers only; nothing persisted."""
    registered = build_registered_map(tables)
    rconn = str(rec.get("connection_id") or "")
    rec_phys = (f"{rec.get('schema')}.{rec.get('table')}"
                if rec.get("schema") else str(rec.get("table") or ""))
    rhits = _resolve_physical(registered, rec.get("schema"), rec.get("table"))
    if rconn:
        rhits = [h for h in rhits if str(h.get("connection_id") or "") == rconn]
    rec_registered = prefer_registration(rhits) is not None
    clean, invalid = validate_rec_evidence(rec, tables)
    partners: dict = {}
    pending: list = []
    for ev in clean:
        other = ev.get("other") or {}
        oconn = str(other.get("connection_id") or "") or rconn
        hits = _resolve_physical(registered, other.get("schema"),
                                 other.get("table"))
        if oconn:
            hits = [h for h in hits
                    if str(h.get("connection_id") or "") == oconn]
        pref = prefer_registration(hits)
        oname = (f"{other.get('schema')}.{other.get('table')}"
                 if other.get("schema") else str(other.get("table") or ""))
        if pref is not None:
            pkey = physical_key(pref)
            label, is_reg = _label(pref), True
        else:
            pkey = ("", str(other.get("schema") or "").lower(),
                    str(other.get("table") or "").lower())
            label, is_reg = oname, False
        p = partners.setdefault(pkey, {
            "label": label, "registered": is_reg,
            "cols": set(), "origins": set()})
        for pr in ev.get("pairs") or []:
            if isinstance(pr, (list, tuple)) and len(pr) == 2:
                p["cols"].add(str(pr[0]))
        if ev.get("origin"):
            p["origins"].add(str(ev.get("origin")))
        if str(ev.get("origin") or "") != "sql":
            continue
        blocked = ([] if rec_registered else [rec_phys]) \
            + ([] if is_reg else [oname])
        if blocked:
            for pr in ev.get("pairs") or []:
                if isinstance(pr, (list, tuple)) and len(pr) == 2:
                    pending.append({
                        "left": f"{rec.get('table')}.{pr[0]}",
                        "right": f"{label}.{pr[1]}",
                        "blocked_by": blocked,
                    })
    joins = [{"label": p["label"], "registered": p["registered"],
              "cols": sorted(p["cols"]), "origins": sorted(p["origins"])}
             for _k, p in sorted(partners.items())]
    n_reg = sum(1 for p in partners.values() if p["registered"])
    return {"role": "bridge" if n_reg >= 2 else "referenced",
            "joins": joins, "pending": pending,
            "evidence_warnings": invalid}


def build_graph(tables: list, unregistered_refs: Optional[list] = None) -> dict:
    """Graph-view data: registered tables as nodes, confirmed relations as
    child→parent edges, connected components (undirected BFS, deterministic),
    isolated flags, and dashed ghost nodes/edges for the unregistered FK
    refs. Pure — the route feeds it the registry and the client's last scan
    ghosts; the browser only renders."""
    tables = [t for t in (tables or []) if t.get("id")]
    by_id = {t["id"]: t for t in tables}
    registered = build_registered_map(tables)
    nodes = {t["id"]: {
        "id": t["id"],
        "label": str(t.get("display_name") or t.get("table_name") or t["id"]),
        "sub": (f"{t.get('schema')}.{t.get('table_name')}" if t.get("schema")
                else str(t.get("table_name") or "")),
        "connector": bool(t.get("is_connector")),
        "relation_count": 0, "component": None,
        "isolated": True, "ghost": False,
    } for t in tables}

    edges: list = []
    adj: dict = {tid: set() for tid in nodes}
    for t in tables:
        for i, rel in enumerate(t.get("relations") or []):
            if not isinstance(rel, dict):
                continue
            parent = by_id.get(rel.get("related_table_id"))
            related_ref = rel.get("related_table_id") or rel.get("related_table") or ""
            if parent is None and rel.get("related_table"):
                name = str(rel.get("related_table"))
                sc, _, tn = name.rpartition(".")
                parent = prefer_registration(_resolve_physical(registered, sc, tn))
            if parent is None:
                log_with_sid("relation_discovery", "info",
                             f"REL_GRAPH_DANGLING table={t['id']}")
                continue
            # Per-pair filtering (like the list view) — one malformed pair
            # must not strip a whole edge's keys.
            raw_jk = rel.get("join_keys") or []
            jk = [[str(p[0]), str(p[1])] for p in raw_jk
                  if isinstance(p, (list, tuple)) and len(p) == 2]
            if len(jk) != len(raw_jk):
                log_with_sid("relation_discovery", "warning",
                             f"REL_GRAPH_MALFORMED table={t['id']}")
            edges.append({
                "id": f"rel:{t['id']}:{i}",
                "source": t["id"], "target": parent["id"],
                "keys_label": ", ".join(f"{a} = {b}" for a, b in jk),
                "cardinality": rel.get("cardinality"),
                "origin": rel.get("origin") or "manual",
                "suspicious": _keys_match(physical_key(t), physical_key(parent)),
                "ghost": False, "join_keys": jk,
                "related_ref": str(related_ref),
                "related_is_id": bool(rel.get("related_table_id")),
            })
            nodes[t["id"]]["relation_count"] += 1
            nodes[parent["id"]]["relation_count"] += 1
            adj[t["id"]].add(parent["id"])
            adj[parent["id"]].add(t["id"])

    comp = 0
    seen: set = set()
    for tid in nodes:                       # insertion order → deterministic
        if tid in seen:
            continue
        queue = [tid]
        seen.add(tid)
        while queue:
            cur = queue.pop(0)
            nodes[cur]["component"] = comp
            for nb in sorted(adj.get(cur, ())):
                if nb not in seen:
                    seen.add(nb)
                    queue.append(nb)
        comp += 1
    for tid, n in nodes.items():
        n["isolated"] = not adj.get(tid)

    out_nodes = list(nodes.values())
    # Two passes over the ghosts: nodes first, then edges — an evidence edge
    # may target a ghost that appears LATER in the list (ghost↔ghost joins).
    ghost_refs: list = []
    ghost_ids: set = set()
    for ref in unregistered_refs or []:
        if not isinstance(ref, dict) or not ref.get("table"):
            continue
        gid = (f"ghost:{ref.get('connection_id', '')}/"
               f"{str(ref.get('schema') or '').lower()}/"
               f"{str(ref.get('table')).lower()}")
        if gid in ghost_ids:
            continue
        ghost_ids.add(gid)
        ghost_refs.append((ref, gid))
        out_nodes.append({
            "id": gid, "label": str(ref.get("table")),
            "sub": (f"{ref.get('schema')}.{ref.get('table')}"
                    if ref.get("schema") else str(ref.get("table"))),
            "connector": False, "relation_count": 0, "component": None,
            "isolated": False, "ghost": True,
            "connection_id": ref.get("connection_id"),
            "schema": ref.get("schema"), "table": ref.get("table"),
        })
    seen_ev_edges: set = set()
    for ref, gid in ghost_refs:
        drawn_fk: set = set()
        for src_id in ref.get("referenced_by_ids") or []:
            if src_id in by_id:
                drawn_fk.add(src_id)
                edges.append({
                    "id": f"ghostedge:{src_id}:{gid}",
                    "source": src_id, "target": gid,
                    "keys_label": "FK", "cardinality": None, "origin": "fk",
                    "suspicious": False, "ghost": True, "join_keys": [],
                    "related_ref": "", "related_is_id": False,
                })
        # Recommendation-evidence edges. Pairs are anchored ghost-column-
        # first. FK-origin evidence draws the classic child→ghost edge (with
        # real key labels), deduped against the referenced_by_ids edges above;
        # SQL-origin evidence draws ghost→partner (registered or ghost).
        # FK entries are processed FIRST and register their endpoint+pairs
        # key: an SQL edge duplicating a drawn FK edge is suppressed (FK is
        # ground truth — two overlapping dashed edges read as a bug).
        n_ev = 0
        evidence = sorted(
            (ev for ev in ref.get("evidence") or [] if isinstance(ev, dict)),
            key=lambda ev: 0 if str(ev.get("origin") or "") == "fk" else 1)
        for ev in evidence:
            other = ev.get("other") or {}
            pairs = [[str(p[0]), str(p[1])] for p in (ev.get("pairs") or [])
                     if isinstance(p, (list, tuple)) and len(p) == 2]
            oconn = (str(other.get("connection_id") or "")
                     or str(ref.get("connection_id") or ""))
            ohits = _resolve_physical(registered, other.get("schema"),
                                      other.get("table"))
            if oconn:
                ohits = [h for h in ohits
                         if str(h.get("connection_id") or "") == oconn]
            pref = prefer_registration(ohits)
            if str(ev.get("origin") or "") == "fk":
                if pref is None or pref["id"] == gid:
                    continue
                # Register the endpoint+pairs key even when the classic edge
                # already covers this partner — the relationship IS drawn, a
                # same-pairs SQL edge on top would just overlap it.
                seen_ev_edges.add((tuple(sorted((gid, pref["id"]))),
                                   tuple(sorted(tuple(sorted(p)) for p in pairs))))
                if pref["id"] in drawn_fk:
                    continue
                drawn_fk.add(pref["id"])
                edges.append({
                    "id": f"ghostedge:{pref['id']}:{gid}",
                    "source": pref["id"], "target": gid,
                    "keys_label": ", ".join(f"{b} = {a}" for a, b in pairs) or "FK",
                    "cardinality": None, "origin": "fk",
                    "suspicious": False, "ghost": True,
                    "join_keys": [[b, a] for a, b in pairs],
                    "related_ref": "", "related_is_id": False,
                })
                continue
            if pref is not None:
                target = pref["id"]
            else:
                ogid = (f"ghost:{oconn}/"
                        f"{str(other.get('schema') or '').lower()}/"
                        f"{str(other.get('table') or '').lower()}")
                target = ogid if ogid in ghost_ids else None
            if not target or target == gid:
                continue
            # One edge per unordered endpoint pair + pair set — the mirrored
            # record on the other anchor must not draw a second edge.
            ekey = (tuple(sorted((gid, target))),
                    tuple(sorted(tuple(sorted(p)) for p in pairs)))
            if ekey in seen_ev_edges:
                continue
            seen_ev_edges.add(ekey)
            edges.append({
                "id": f"ghostsql:{gid}:{target}:{n_ev}",
                "source": gid, "target": target,
                "keys_label": ", ".join(f"{a} = {b}" for a, b in pairs) or "SQL",
                "cardinality": None, "origin": "sql",
                "suspicious": False, "ghost": True, "join_keys": pairs,
                "related_ref": "", "related_is_id": False,
            })
            n_ev += 1
    return {"nodes": out_nodes, "edges": edges}


# ---------------------------------------------------------------------------
# Source a) declared foreign keys
# ---------------------------------------------------------------------------

def fk_candidates(tables: list, fk_map: dict) -> list:
    """`fk_map` maps table_id -> db_connector.introspect() result (live, since
    FKs are not persisted in the registry). Composite FK -> ONE candidate."""
    registered = build_registered_map(tables)
    by_id = {t.get("id"): t for t in tables or []}
    out: list = []
    for tid, intro in (fk_map or {}).items():
        child = by_id.get(tid)
        if not child or not isinstance(intro, dict) or not intro.get("ok"):
            continue
        for fk in intro.get("foreign_keys") or []:
            if not isinstance(fk, dict):
                continue
            ccols = [str(c) for c in (fk.get("constrained_columns") or [])]
            pcols = [str(c) for c in (fk.get("referred_columns") or [])]
            if not ccols or len(ccols) != len(pcols):
                continue
            for parent in _resolve_physical(registered, fk.get("referred_schema"),
                                            fk.get("referred_table")):
                if parent.get("id") == tid:
                    continue          # self-reference: nothing to declare
                out.append(_make_candidate(child, parent, list(zip(ccols, pcols)),
                                           "fk", True))
    return out


# ---------------------------------------------------------------------------
# Source b) name / description similarity (deterministic, no LLM)
# ---------------------------------------------------------------------------

def name_ubiquity(tables: list) -> dict:
    """normalized column name -> number of registered tables containing it."""
    counts: Counter = Counter()
    for t in tables or []:
        seen = {normalize_column_name(c.get("name"))
                for c in (t.get("columns") or []) if c.get("name")}
        counts.update(seen)
    return dict(counts)


def _too_ubiquitous(norm: str, ubiquity: dict, n_tables: int) -> bool:
    count = ubiquity.get(norm, 0)
    return (count >= MIN_UBIQUITY_TABLES
            and n_tables > 0
            and count / n_tables > NAME_MAX_TABLE_SHARE)


def _idf_weight(norm: str, ubiquity: dict) -> float:
    count = max(1, ubiquity.get(norm, 1))
    w = 1.0 if count <= 1 else 1.0 / (1.0 + math.log(count) - math.log(2))
    if norm in GENERIC_NAMES:
        w *= 0.5
    return w


def name_candidates(tables: list) -> list:
    """Cross-table pairs from normalized-name equality, plus the containment
    pattern `orders.customer_id` <-> `customers.id` (column names the other
    table, the other side is pk-ish). Ubiquitous names are hard-capped, then
    idf-down-weighted; generic names carry an extra penalty."""
    tables = [t for t in (tables or []) if t.get("id")]
    n = len(tables)
    ubiquity = name_ubiquity(tables)
    cols_of = []
    for t in tables:
        cols_of.append([(c, normalize_column_name(c.get("name")))
                        for c in (t.get("columns") or []) if c.get("name")])
    out: list = []
    for i in range(n):
        for j in range(i + 1, n):
            if len(out) >= MAX_CANDIDATES_PER_SOURCE:
                log_with_sid("relation_discovery", "warning",
                             f"REL_CANDIDATE_CAP source=name cap={MAX_CANDIDATES_PER_SOURCE}")
                return out
            a_tab, b_tab = tables[i], tables[j]
            a_stem = _stem(normalize_column_name(a_tab.get("table_name") or ""))
            b_stem = _stem(normalize_column_name(b_tab.get("table_name") or ""))
            for a_col, a_norm in cols_of[i]:
                for b_col, b_norm in cols_of[j]:
                    if _too_ubiquitous(a_norm, ubiquity, n) or \
                       _too_ubiquitous(b_norm, ubiquity, n):
                        continue
                    score = 0.0
                    if a_norm == b_norm:
                        score = 1.0 * _idf_weight(a_norm, ubiquity)
                    elif _stem(a_norm) == b_stem and bool(b_col.get("pk")):
                        # orders.customer_id -> customers.<pk>
                        score = 0.7 * _idf_weight(a_norm, ubiquity)
                    elif _stem(b_norm) == a_stem and bool(a_col.get("pk")):
                        score = 0.7 * _idf_weight(b_norm, ubiquity)
                    if score < NAME_SCORE_THRESHOLD:
                        continue
                    # Orientation heuristic only — verification corrects it.
                    if a_col.get("pk") and not b_col.get("pk"):
                        child, ccol, parent, pcol = b_tab, b_col, a_tab, a_col
                    else:
                        child, ccol, parent, pcol = a_tab, a_col, b_tab, b_col
                    out.append(_make_candidate(
                        child, parent, [(ccol.get("name"), pcol.get("name"))],
                        "name", {"score": round(score, 2)}))
    return out


def _desc_tokens(text: str) -> frozenset:
    toks = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return frozenset(t for t in toks if t not in DESC_STOPWORDS)


def description_candidates(tables: list) -> list:
    """Token-set Jaccard between admin-CONFIRMED column descriptions. Only
    tables whose descriptions were confirmed participate (draft text is not
    evidence). The name-ubiquity hard cap applies to the column names too."""
    tables = [t for t in (tables or [])
              if t.get("id") and t.get("descriptions_confirmed_by")]
    n = len(tables)
    ubiquity = name_ubiquity(tables)
    toks_of = []
    for t in tables:
        toks_of.append([(c, _desc_tokens(c.get("description")))
                        for c in (t.get("columns") or []) if c.get("name")])
    out: list = []
    for i in range(n):
        for j in range(i + 1, n):
            if len(out) >= MAX_CANDIDATES_PER_SOURCE:
                log_with_sid("relation_discovery", "warning",
                             f"REL_CANDIDATE_CAP source=description cap={MAX_CANDIDATES_PER_SOURCE}")
                return out
            for a_col, a_tok in toks_of[i]:
                for b_col, b_tok in toks_of[j]:
                    if len(a_tok) < 3 or len(b_tok) < 3:
                        continue
                    if _too_ubiquitous(normalize_column_name(a_col.get("name")), ubiquity, n) or \
                       _too_ubiquitous(normalize_column_name(b_col.get("name")), ubiquity, n):
                        continue
                    union = a_tok | b_tok
                    sim = len(a_tok & b_tok) / len(union) if union else 0.0
                    if sim < DESC_SIM_THRESHOLD:
                        continue
                    if a_col.get("pk") and not b_col.get("pk"):
                        child, ccol, parent, pcol = tables[j], b_col, tables[i], a_col
                    else:
                        child, ccol, parent, pcol = tables[i], a_col, tables[j], b_col
                    out.append(_make_candidate(
                        child, parent, [(ccol.get("name"), pcol.get("name"))],
                        "description", {"score": round(sim, 2)}))
    return out


# ---------------------------------------------------------------------------
# Source c) pasted SQL (sqlglot; parsed in memory only)
# ---------------------------------------------------------------------------

def _parse_statements(sql_text: str, dialect: Optional[str]):
    """(parsed_statements, failed_count, total_count). One bad statement never
    kills the batch. Only exception TYPES are ever logged — sqlglot messages
    embed the SQL text and must never leave the parser (Article II)."""
    import sqlglot
    try:
        all_stmts = sqlglot.parse(sql_text, read=dialect)
        stmts = [s for s in all_stmts if s is not None]
        return stmts, len(all_stmts) - len(stmts), len(all_stmts)
    except Exception as e:
        log_with_sid("relation_discovery", "info",
                     f"REL_SQL_PARSE_RETRY_SPLIT: {type(e).__name__}")
    # Whole-text parse failed: split naively and salvage what parses.
    chunks = [c for c in (p.strip() for p in sql_text.split(";")) if c]
    parsed, failed = [], 0
    for chunk in chunks:
        try:
            stmt = sqlglot.parse_one(chunk, read=dialect)
            if stmt is not None:
                parsed.append(stmt)
            else:
                failed += 1
        except Exception as e:
            log_with_sid("relation_discovery", "info",
                         f"REL_SQL_STATEMENT_UNPARSEABLE: {type(e).__name__}")
            failed += 1
    return parsed, failed, len(chunks)


def _resolve_column(scope, col, depth: int = 0):
    """Physical (schema_lower, table_lower, column) behind a column reference,
    following CTE/derived-table scopes; computed projections -> None."""
    from sqlglot import exp
    from sqlglot.optimizer.scope import Scope
    if depth > _MAX_RESOLVE_DEPTH or col is None:
        return None
    tname = col.table
    if not tname:
        return None
    src = scope.sources.get(tname)
    if src is None:
        return None
    if isinstance(src, exp.Table):
        return ((src.text("db") or "").lower(), src.name.lower(), col.name)
    if isinstance(src, Scope):
        try:
            selects = src.expression.selects
        except Exception:
            return None
        for proj in selects:
            if proj.alias_or_name == col.name:
                inner = proj.unalias() if isinstance(proj, exp.Alias) else proj
                if isinstance(inner, exp.Column):
                    return _resolve_column(src, inner, depth + 1)
                return None
    return None


def build_schema_map(tables: list) -> dict:
    """Best-effort schema mapping for sqlglot.qualify. Nested {schema: {table:
    {col: type}}} when every table carries a schema, else flat {table: {...}}
    (sqlglot rejects mixed nesting depths)."""
    tables = [t for t in (tables or []) if t.get("table_name")]
    if not tables:
        return {}
    all_have_schema = all((t.get("schema") or "").strip() for t in tables)
    out: dict = {}
    for t in tables:
        cols = {str(c.get("name")): "text"
                for c in (t.get("columns") or []) if c.get("name")}
        if all_have_schema:
            out.setdefault(str(t.get("schema")), {})[str(t.get("table_name"))] = cols
        else:
            out[str(t.get("table_name"))] = cols
    return out


def _unreg_evidence_rows(a, b, ta, tb) -> list:
    """Anchored evidence rows for a predicate with >=1 unregistered side:
    (anchor_name, other, pair) with the ANCHOR's column always first in the
    pair — orientation is fixed by construction, so merged evidence can never
    mix directions. Both sides unregistered -> two rows, one per anchor. An
    ambiguous registered other (prefer_registration -> None) yields no row —
    the same rule candidate extraction applies. Identifiers only."""
    def _nm(x):
        return f"{x[0]}.{x[1]}" if x[0] else x[1]
    if not ta and not tb:
        return [(_nm(a), {"name": _nm(b)}, (a[2], b[2])),
                (_nm(b), {"name": _nm(a)}, (b[2], a[2]))]
    if not ta:
        other = prefer_registration(tb)
        return [(_nm(a), {"table_id": other["id"]}, (a[2], b[2]))] if other else []
    other = prefer_registration(ta)
    return [(_nm(b), {"table_id": other["id"]}, (b[2], a[2]))] if other else []


def extract_sql_joins(sql_text: str, tables: list,
                      dialect: Optional[str]) -> tuple:
    """(candidates, stats) from pasted SELECT statements. Only equality
    predicates column = column survive (literals are dropped by construction,
    which is what "strip literals" means here); both endpoints must resolve to
    registered tables. Composite predicates between one table pair inside one
    ON clause collapse to one multi-column candidate; WHERE-style implicit
    joins group per statement. Frequency counts DISTINCT statements.

    Predicates touching UNREGISTERED tables additionally leave identifier-only
    evidence in stats["unregistered_joins"] (name, other endpoint, column
    pairs, distinct-statement count) — the raw material for the persistent
    "Recommended tables". Never any SQL text or literal.

    v4.1: any predicate side that resolves to a REGISTERED table has its
    column validated (case-insensitively) against registry metadata — wrong
    SQL is a first-class case, not a silent drop: the pair is skipped
    (candidate AND evidence, valid pairs of the same statement are kept) and
    reported in stats["invalid_column_refs"] with the statement number.
    Predicates whose column reference cannot be resolved at all (computed
    CTE/subquery projections, unqualified column refs) are counted in
    stats["unresolved_predicates"] — a known limitation made visible,
    semantics unchanged."""
    stats = {"statements": 0, "parsed": 0, "failed": 0, "non_select": 0,
             "unknown_tables": [], "unregistered_joins": [],
             "unregistered_tables": [], "invalid_column_refs": [],
             "unresolved_predicates": 0}
    unknown: set = set()
    registered = build_registered_map(tables)
    tables_by_id = {t.get("id"): t for t in tables or [] if t.get("id")}
    schema_map = build_schema_map(tables)
    invalid_seen: set = set()

    def _record_invalid(stmt_no: int, table_label: str, column: str) -> None:
        # Reported with the SQL-side spelling — never canonicalized.
        key = (stmt_no, table_label, column)
        if key in invalid_seen or len(stats["invalid_column_refs"]) >= 20:
            return
        invalid_seen.add(key)
        stats["invalid_column_refs"].append(
            {"statement": stmt_no, "table": table_label, "column": column})
        log_with_sid("relation_discovery", "warning",
                     f"REL_SQL_INVALID_COLUMN stmt={stmt_no} "
                     f"table={table_label} col={column}")
    try:
        from sqlglot import exp
        from sqlglot.optimizer.qualify import qualify
        from sqlglot.optimizer.scope import traverse_scope
    except Exception as e:
        log_with_sid("relation_discovery", "error", f"SQLGLOT_IMPORT_FAILED: {e}")
        return [], stats

    stmts, failed, total = _parse_statements(sql_text or "", dialect)
    stats["statements"] = total
    stats["failed"] = failed

    freq: Counter = Counter()
    by_id: dict = {}
    unreg_freq: Counter = Counter()
    unreg_by_key: dict = {}
    unreg_anchors: set = set()
    unreg_table_freq: Counter = Counter()   # distinct statements per TABLE
    unreg_capped = False
    # 1-based over the ANALYZED statements — the split-salvage parse path
    # drops unparseable chunks, so this is "Nth analyzed", not Nth pasted.
    for stmt_no, stmt in enumerate(stmts, 1):
        if not list(stmt.find_all(exp.Select)):
            stats["non_select"] += 1
            continue
        try:
            qualified = qualify(stmt.copy(), dialect=dialect, schema=schema_map,
                                validate_qualify_columns=False)
        except Exception as e:
            log_with_sid("relation_discovery", "info",
                         f"REL_SQL_QUALIFY_FALLBACK: {type(e).__name__}")
            qualified = stmt          # best-effort; raw tree still resolvable
        stmt_candidates: dict = {}
        unreg_groups: dict = {}   # (gkey, anchor, other_key) -> {"other", "pairs"}
        try:
            scopes = traverse_scope(qualified)
        except Exception as e:
            log_with_sid("relation_discovery", "info",
                         f"REL_SQL_SCOPE_FAILED: {type(e).__name__}")
            stats["failed"] += 1
            continue
        stats["parsed"] += 1
        for scope in scopes:
            expr = scope.expression
            groups: list = []        # (group_key_extra, condition_expression)
            for k, join in enumerate(expr.args.get("joins") or []):
                on = join.args.get("on")
                if on is not None:
                    groups.append((f"on{k}", on))
            where = expr.args.get("where")
            if where is not None:
                groups.append(("where", where.this))
            for gkey, cond in groups:
                pair_groups: dict = {}
                for eq in cond.find_all(exp.EQ):
                    l, r = eq.left, eq.right
                    if not (isinstance(l, exp.Column) and isinstance(r, exp.Column)):
                        continue
                    a = _resolve_column(scope, l)
                    b = _resolve_column(scope, r)
                    if not a or not b:
                        # Computed CTE/subquery projections and unqualified
                        # column refs resolve to None — the predicate
                        # contributes nothing (known limitation, counted so
                        # the drop is at least visible).
                        stats["unresolved_predicates"] += 1
                        continue
                    if (a[0], a[1]) == (b[0], b[1]):
                        continue
                    ta = _resolve_physical(registered, a[0], a[1])
                    tb = _resolve_physical(registered, b[0], b[1])
                    if not ta:
                        unknown.add(f"{a[0]}.{a[1]}" if a[0] else a[1])
                    if not tb:
                        unknown.add(f"{b[0]}.{b[1]}" if b[0] else b[1])
                    if not ta or not tb:
                        # v4: keep identifier-only evidence for the missing
                        # side(s) — this is what "Recommended tables" run on.
                        for anchor, other, pair in _unreg_evidence_rows(a, b, ta, tb):
                            # v4.1: the REGISTERED side of a mixed pair is
                            # validatable NOW (pair[1] — anchor-first). Wrong
                            # SQL never becomes evidence; it gets reported.
                            reg_doc = tables_by_id.get(other.get("table_id")) \
                                if "table_id" in other else None
                            if reg_doc is not None and \
                                    not _has_column(reg_doc, pair[1]):
                                _record_invalid(stmt_no, _label(reg_doc), pair[1])
                                continue
                            if anchor not in unreg_anchors:
                                if len(unreg_anchors) >= UNREG_TABLE_CAP:
                                    unreg_capped = True
                                    continue
                                unreg_anchors.add(anchor)
                            okey = ("id", other["table_id"]) if "table_id" in other \
                                else ("name", other["name"])
                            grp = unreg_groups.setdefault(
                                (gkey, anchor, okey), {"other": other, "pairs": set()})
                            grp["pairs"].add(pair)
                        continue
                    # Duplicate registrations of one physical table resolve to
                    # the PREFERRED registration (connector first); hits that
                    # span DIFFERENT physical tables (same name on two
                    # connections) stay ambiguous and are dropped as before.
                    ta0 = prefer_registration(ta)
                    tb0 = prefer_registration(tb)
                    if ta0 is None or tb0 is None or ta0["id"] == tb0["id"]:
                        continue
                    # v4.1: both sides registered — both columns validatable.
                    # An invalid pair is skipped + reported; the other pairs
                    # of a composite ON clause are kept.
                    bad = False
                    for t0, col in ((ta0, a[2]), (tb0, b[2])):
                        if not _has_column(t0, col):
                            _record_invalid(stmt_no, _label(t0), col)
                            bad = True
                    if bad:
                        continue
                    # Canonical endpoint order inside the group so composite
                    # pairs line up regardless of predicate orientation.
                    if ta0["id"] <= tb0["id"]:
                        key = (ta0["id"], tb0["id"])
                        pair = (a[2], b[2])
                    else:
                        key = (tb0["id"], ta0["id"])
                        pair = (b[2], a[2])
                    pair_groups.setdefault((gkey,) + key,
                                           {"child": min(ta0, tb0, key=lambda t: t["id"]),
                                            "parent": max(ta0, tb0, key=lambda t: t["id"]),
                                            "pairs": set()})["pairs"].add(pair)
                for grp in pair_groups.values():
                    pairs = sorted(grp["pairs"])
                    cand = _make_candidate(grp["child"], grp["parent"], pairs,
                                           "sql", {"frequency": 0})
                    stmt_candidates[cand["candidate_id"]] = cand
        for cid, cand in stmt_candidates.items():
            freq[cid] += 1
            if cid not in by_id:
                by_id[cid] = cand
        # Per-statement dedupe for the unregistered evidence too: one
        # statement contributes at most +1 count per (anchor, other, pairs).
        stmt_unreg: dict = {}
        for (_g, anchor, okey), grp in unreg_groups.items():
            pairs = tuple(sorted(grp["pairs"]))
            stmt_unreg[(anchor, okey, pairs)] = {
                "name": anchor, "other": grp["other"],
                "pairs": [[p[0], p[1]] for p in pairs]}
        for k, rec in stmt_unreg.items():
            unreg_freq[k] += 1
            unreg_by_key.setdefault(k, rec)
        for anchor in {k[0] for k in stmt_unreg}:
            unreg_table_freq[anchor] += 1

    out = []
    for cid, cand in by_id.items():
        cand["sql_frequency"] = int(freq[cid])
        cand["evidence"]["sql"] = {"frequency": int(freq[cid])}
        out.append(cand)
    # Join endpoints that are not registered tables — names come from the
    # admin's own pasted SQL and go only back to the admin's browser.
    stats["unknown_tables"] = sorted(unknown)[:10]
    if unreg_capped:
        log_with_sid("relation_discovery", "info",
                     f"REL_UNREG_EVIDENCE_CAP cap={UNREG_TABLE_CAP}")
    stats["unregistered_joins"] = [
        {**unreg_by_key[k], "count": int(unreg_freq[k])}
        for k in sorted(unreg_by_key, key=lambda k: (k[0], str(k[1]), k[2]))
    ]
    stats["unregistered_tables"] = [
        {"name": n, "count": int(unreg_table_freq[n])}
        for n in sorted(unreg_table_freq)
    ]
    return out, stats


# ---------------------------------------------------------------------------
# Merge / dedupe / exclusion of already-declared relations
# ---------------------------------------------------------------------------

def merge_candidates(*lists) -> list:
    """Bucket by candidate_id; union sources (precedence order), merge
    evidence, keep max sql_frequency. FK orientation wins when present."""
    merged: dict = {}
    for cands in lists:
        for c in cands or []:
            cur = merged.get(c["candidate_id"])
            if cur is None:
                merged[c["candidate_id"]] = dict(c)
                continue
            srcs = set(cur["sources"]) | set(c["sources"])
            if "fk" in c["sources"] and "fk" not in cur["sources"]:
                # adopt the FK's child->parent orientation
                for k in ("table_id", "table_label", "related_table_id",
                          "related_label", "join_keys"):
                    cur[k] = c[k]
            cur["sources"] = [s for s in SOURCE_PRECEDENCE if s in srcs]
            cur["evidence"] = {**c["evidence"], **cur["evidence"]}
            cur["sql_frequency"] = max(cur["sql_frequency"], c["sql_frequency"])
            if cur["sql_frequency"]:
                cur["evidence"]["sql"] = {"frequency": cur["sql_frequency"]}
    for c in merged.values():
        c["sources"] = [s for s in SOURCE_PRECEDENCE if s in set(c["sources"])]
    return list(merged.values())


def filter_existing(candidates: list, tables: list) -> list:
    """Drop candidates already declared in any table's relations (either
    orientation — candidate_id is direction-independent). Legacy name-based
    `related_table` refs resolve the same way expand_with_connectors does."""
    registered = build_registered_map(tables)
    by_id = {t.get("id"): t for t in tables or []}
    existing: set = set()
    for t in tables or []:
        for rel in t.get("relations") or []:
            if not isinstance(rel, dict):
                continue
            rid = rel.get("related_table_id")
            other = by_id.get(rid)
            if other is None and rel.get("related_table"):
                name = str(rel.get("related_table"))
                sc, _, tn = name.rpartition(".")
                hits = _resolve_physical(registered, sc, tn)
                other = hits[0] if len(hits) == 1 else None
            if other is None:
                continue
            try:
                jk = [(str(p[0]), str(p[1])) for p in (rel.get("join_keys") or [])]
            except Exception:
                continue
            if not jk:
                continue
            existing.add(candidate_id(t.get("id"), [p[0] for p in jk],
                                      other.get("id"), [p[1] for p in jk]))
    return [c for c in candidates or [] if c["candidate_id"] not in existing]


# ---------------------------------------------------------------------------
# Verification on snapshot data (aggregates only)
# ---------------------------------------------------------------------------

def verify_candidates(candidates: list,
                      load_columns: Callable[[str, list], Optional[pd.DataFrame]]) -> list:
    """Profile every candidate against snapshot data. `load_columns(table_id,
    [col, ...])` returns a DataFrame with those columns or None (missing
    snapshot / missing column) — the route injects the parquet reader, tests
    inject fakes. Loads are cached per (table, column-set) within one call.
    Only aggregates are computed; no cell values enter the result."""
    cache: dict = {}

    def _load(tid: str, cols: list) -> Optional[pd.DataFrame]:
        key = (tid, tuple(sorted(set(cols))))
        if key not in cache:
            try:
                cache[key] = load_columns(tid, sorted(set(cols)))
            except Exception as e:
                log_with_sid("relation_discovery", "warning",
                             f"REL_VERIFY_LOAD_FAILED table={tid}: {type(e).__name__}")
                cache[key] = None
        return cache[key]

    out = []
    for cand in candidates or []:
        c = dict(cand)
        c["evidence"] = dict(cand.get("evidence") or {})   # never mutate the caller's dict
        try:
            _verify_one(c, _load)
        except Exception as e:
            log_with_sid("relation_discovery", "warning",
                         f"REL_VERIFY_FAILED cand={c.get('candidate_id')}: {type(e).__name__}")
            c["verified"] = False
            c["unverified_reason"] = "verification failed"
        out.append(c)
    return out


def _string_keyed(df: pd.DataFrame) -> pd.DataFrame:
    """String view of key columns for a cross-dtype comparison. Integral
    floats render without the trailing `.0` (a float64 child produced by NaNs
    must still match an object-typed parent id)."""
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if pd.api.types.is_float_dtype(s) and (s.dropna() % 1 == 0).all():
            s = s.astype("Int64")
        out[col] = s.astype(str)
    return out


def _verify_one(c: dict, load) -> None:
    ccols = [p[0] for p in c["join_keys"]]
    pcols = [p[1] for p in c["join_keys"]]
    child = load(c["table_id"], ccols)
    parent = load(c["related_table_id"], pcols)
    if child is None or parent is None or \
            any(col not in child.columns for col in ccols) or \
            any(col not in parent.columns for col in pcols):
        c["verified"] = False
        c["unverified_reason"] = "snapshot or column unavailable"
        return

    cdf = child[ccols].dropna()
    pdf = parent[pcols].dropna()
    if len(cdf) == 0 or len(pdf) == 0:
        c["verified"] = False
        c["unverified_reason"] = "no non-null key values in snapshot"
        return

    child_unique = len(cdf) == len(cdf.drop_duplicates())
    parent_unique = len(pdf) == len(pdf.drop_duplicates())

    # Flip BEFORE computing overlap so we always store (and measure) the
    # many-to-one direction with the correct parent. A declared FK is ground
    # truth on direction — snapshot uniqueness never overrides it.
    if child_unique and not parent_unique and "fk" not in c.get("sources", ()):
        c["table_id"], c["related_table_id"] = c["related_table_id"], c["table_id"]
        c["table_label"], c["related_label"] = c["related_label"], c["table_label"]
        c["join_keys"] = [[b, a] for a, b in c["join_keys"]]
        ccols, pcols = pcols, ccols
        cdf, pdf = pdf, cdf
        child_unique, parent_unique = parent_unique, child_unique

    if not child_unique and parent_unique:
        cardinality = "N:1"
    elif child_unique and parent_unique:
        cardinality = "1:1"
    elif child_unique and not parent_unique:
        cardinality = "1:N"          # unflipped FK with a non-unique target
    else:
        cardinality = "N:M"

    child_nonnull = len(cdf)
    # Align parent key columns onto the child names for the merge, casting
    # both sides to str when any pairwise dtype differs (an int64/object id
    # mismatch would otherwise report 0% overlap silently). The parent side
    # is de-duplicated first — merging against a non-unique parent would
    # multiply child rows and inflate overlap past 100%.
    pk = pdf.drop_duplicates().copy()
    pk.columns = ccols
    needs_cast = any(
        str(cdf[cc].dtype) != str(pk[cc].dtype)
        and not (pd.api.types.is_numeric_dtype(cdf[cc])
                 and pd.api.types.is_numeric_dtype(pk[cc]))
        for cc in ccols
    )
    if needs_cast:
        cdf = _string_keyed(cdf)
        pk = _string_keyed(pk).drop_duplicates()
        c["evidence"]["dtype_cast"] = True
    m = cdf.merge(pk, on=ccols, how="left", indicator=True)
    matched = int((m["_merge"] == "both").sum())
    orphans = child_nonnull - matched

    c["verified"] = True
    c["cardinality"] = cardinality
    c["overlap_pct"] = round(100.0 * matched / child_nonnull, 1)
    c["orphans"] = int(orphans)
    c["child_nonnull"] = int(child_nonnull)
    c["child_unique"] = bool(child_unique)
    c["parent_unique"] = bool(parent_unique)
    c["unverified_reason"] = None


# ---------------------------------------------------------------------------
# Banding
# ---------------------------------------------------------------------------

def band(cand: dict) -> str:
    """Ordered rules — first hit wins. Declared FKs are ground truth and stay
    confirmed regardless of snapshot state."""
    if "fk" in cand.get("sources", ()):
        return "confirmed"
    if not cand.get("verified"):
        return "attention"
    if cand.get("cardinality") == "N:M":
        return "attention"
    overlap = cand.get("overlap_pct") or 0.0
    if overlap < SUGGESTED_OVERLAP:
        return "attention"
    if cand.get("sql_frequency", 0) >= SQL_CONFIRMED_FREQ:
        return "confirmed"
    if overlap >= CONFIRMED_OVERLAP and cand.get("cardinality") in ("1:1", "N:1"):
        return "confirmed"
    return "suggested"


def band_all(candidates: list) -> list:
    """Stamp `band` and sort: band order, then overlap desc, then label."""
    order = {"confirmed": 0, "suggested": 1, "attention": 2}
    for c in candidates or []:
        c["band"] = band(c)
    return sorted(candidates or [],
                  key=lambda c: (order.get(c["band"], 3),
                                 -(c.get("overlap_pct") or 0.0),
                                 c.get("table_label") or "",
                                 c.get("candidate_id") or ""))


# ---------------------------------------------------------------------------
# Orchestration (scan = fk + name + description; SQL merges in separately)
# ---------------------------------------------------------------------------

def discover(tables: list, fk_map: dict) -> list:
    """Generate + merge, then drop same-physical pairs, drop candidates
    already declared to ANY registration of the physical target, and collapse
    duplicate-registration fan-out to one candidate per physical pair.
    Verification and banding are invoked separately by the caller so
    analyze_sql reuses them. Single-registration registries behave exactly as
    before (the physical filters degenerate to the id-level ones)."""
    merged = merge_candidates(fk_candidates(tables, fk_map),
                              name_candidates(tables),
                              description_candidates(tables))
    merged = filter_same_physical(merged, tables)
    merged = filter_existing_physical(merged, tables)
    return dedupe_physical_targets(merged, tables)
