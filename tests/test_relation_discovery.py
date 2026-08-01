"""relation_discovery pure logic: name normalization + ubiquity down-weighting,
FK/name/description candidate generation, sqlglot join extraction (aliases,
CTEs, subqueries, composite keys, unregistered-table filtering, literal
stripping, per-statement frequency), verification math (cardinality,
direction flip, overlap/orphans, dtype casts, the sibling-FK N:M case),
banding precedence, merge/dedupe, and already-declared exclusion.
Offline and store-free — every function takes data in / returns data out."""
import pandas as pd
import pytest

import relation_discovery as rd

TID_A = "aa11bb22cc33dd44"
TID_B = "bb22cc33dd44ee55"
TID_C = "cc33dd44ee55ff66"
TID_D = "dd44ee55ff66aa11"


def _table(tid, name, cols, schema="", relations=None, confirmed=True, display=None):
    return {
        "id": tid, "schema": schema, "table_name": name,
        "display_name": display or name.replace("_", " "),
        "columns": [c if isinstance(c, dict) else {"name": c} for c in cols],
        "relations": relations or [],
        "descriptions_confirmed_by": "ladmin" if confirmed else None,
    }


ORDERS = _table(TID_A, "orders", [
    {"name": "order_id", "pk": True}, {"name": "customer_id"}, {"name": "amount"}])
CUSTOMERS = _table(TID_B, "customers", [
    {"name": "id", "pk": True}, {"name": "city"}])


# ── normalize_column_name ──────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Customer_ID", "customer"),
    ("customerid", "customerid"),      # no separator: only affix strip applies
    ("id_customer", "customer"),
    ("tbl_orders", "orders"),
    ("fk_client_code", "client"),      # one prefix + one suffix
    ("CITY CODE", "city"),
    ("amount", "amount"),
    ("_id", "id"),                     # degenerate: separators canonicalized first
])
def test_normalize_column_name(raw, expected):
    assert rd.normalize_column_name(raw) == expected


def test_normalize_never_empty():
    assert rd.normalize_column_name("__") != ""
    assert rd.normalize_column_name("") == ""


# ── ubiquity + name candidates ─────────────────────────────────────────────

def test_name_exact_match_proposed_and_containment():
    cands = rd.name_candidates([ORDERS, CUSTOMERS])
    # containment: orders.customer_id -> customers.id (pk side is parent)
    assert len(cands) == 1
    c = cands[0]
    assert c["table_id"] == TID_A and c["related_table_id"] == TID_B
    assert c["join_keys"] == [["customer_id", "id"]]
    assert c["sources"] == ["name"]
    assert c["evidence"]["name"]["score"] > 0


def test_name_exact_match_across_two_fact_tables():
    payments = _table(TID_C, "payments", [{"name": "payment_id", "pk": True},
                                          {"name": "customer_id"}])
    cands = rd.name_candidates([ORDERS, payments])
    pairs = [tuple(c["join_keys"][0]) for c in cands]
    assert ("customer_id", "customer_id") in pairs


def test_ubiquitous_name_hard_capped():
    # `status` in 3 of 4 tables (75% > 50% share, count >= MIN_UBIQUITY_TABLES)
    tabs = [
        _table(TID_A, "t_one", ["status", "alpha"]),
        _table(TID_B, "t_two", ["status", "beta"]),
        _table(TID_C, "t_three", ["status", "gamma"]),
        _table(TID_D, "t_four", ["delta"]),
    ]
    cands = rd.name_candidates(tabs)
    assert all("status" not in {c["join_keys"][0][0], c["join_keys"][0][1]}
               for c in cands)


def test_small_registry_not_killed_by_share_cap():
    """With only two tables every shared name is in 100% of tables — the
    MIN_UBIQUITY_TABLES floor keeps the share cap from killing the feature."""
    a = _table(TID_A, "invoices", [{"name": "contract_ref"}])
    b = _table(TID_B, "contracts", [{"name": "contract_ref", "pk": True}])
    assert rd.name_candidates([a, b])


def test_generic_name_downweighted_below_threshold():
    a = _table(TID_A, "alpha", [{"name": "type"}])
    b = _table(TID_B, "beta", [{"name": "type"}])
    assert rd.name_candidates([a, b]) == []


# ── description candidates ─────────────────────────────────────────────────

def test_description_similarity_threshold_and_confirmed_gate():
    desc = "internal contract reference number issued by billing"
    a = _table(TID_A, "invoices", [{"name": "c_ref", "description": desc}])
    b = _table(TID_B, "contracts", [{"name": "ref_no", "description": desc, "pk": True}])
    cands = rd.description_candidates([a, b])
    assert len(cands) == 1
    assert cands[0]["sources"] == ["description"]
    assert cands[0]["related_table_id"] == TID_B     # pk side is parent

    # unconfirmed table -> excluded
    b_unconfirmed = dict(b, descriptions_confirmed_by=None)
    assert rd.description_candidates([a, b_unconfirmed]) == []

    # dissimilar descriptions -> below threshold
    b_other = _table(TID_B, "contracts",
                     [{"name": "ref_no", "description": "postal address of the office"}])
    assert rd.description_candidates([a, b_other]) == []


# ── FK candidates ──────────────────────────────────────────────────────────

def _fk_intro(fks):
    return {"ok": True, "foreign_keys": fks}


def test_fk_candidates_resolution_and_composite():
    fk_map = {TID_A: _fk_intro([{
        "constrained_columns": ["customer_id"], "referred_schema": None,
        "referred_table": "CUSTOMERS", "referred_columns": ["id"]}])}
    cands = rd.fk_candidates([ORDERS, CUSTOMERS], fk_map)
    assert len(cands) == 1                       # case-insensitive resolution
    assert cands[0]["sources"] == ["fk"]
    assert cands[0]["join_keys"] == [["customer_id", "id"]]

    comp = {TID_A: _fk_intro([{
        "constrained_columns": ["customer_id", "amount"], "referred_schema": "",
        "referred_table": "customers", "referred_columns": ["id", "city"]}])}
    cands = rd.fk_candidates([ORDERS, CUSTOMERS], comp)
    assert len(cands) == 1
    assert cands[0]["join_keys"] == [["customer_id", "id"], ["amount", "city"]]


def test_fk_unregistered_target_and_failed_introspection_skipped():
    fk_map = {
        TID_A: _fk_intro([{"constrained_columns": ["customer_id"],
                           "referred_schema": None,
                           "referred_table": "warehouses",   # not registered
                           "referred_columns": ["id"]}]),
        TID_B: {"ok": False, "error": "boom"},
    }
    assert rd.fk_candidates([ORDERS, CUSTOMERS], fk_map) == []


# ── SQL extraction ─────────────────────────────────────────────────────────

def test_sql_alias_join_and_where_join():
    sql = ("SELECT o.amount FROM orders o JOIN customers c ON o.customer_id = c.id;"
           "SELECT 1 FROM orders o, customers c WHERE o.customer_id = c.id")
    cands, stats = rd.extract_sql_joins(sql, [ORDERS, CUSTOMERS], None)
    assert stats["statements"] == 2 and stats["failed"] == 0
    assert len(cands) == 1
    c = cands[0]
    assert c["sql_frequency"] == 2               # two DISTINCT statements
    assert sorted(map(tuple, c["join_keys"])) == [("customer_id", "id")]


def test_sql_pair_twice_in_one_statement_counts_once():
    sql = ("SELECT 1 FROM orders o JOIN customers c ON o.customer_id = c.id "
           "WHERE o.customer_id = c.id")
    cands, _ = rd.extract_sql_joins(sql, [ORDERS, CUSTOMERS], None)
    assert len(cands) == 1 and cands[0]["sql_frequency"] == 1


def test_sql_cte_resolves_to_base_table():
    sql = ("WITH big AS (SELECT customer_id AS cid, amount FROM orders) "
           "SELECT 1 FROM big b JOIN customers c ON b.cid = c.id")
    cands, stats = rd.extract_sql_joins(sql, [ORDERS, CUSTOMERS], None)
    assert stats["parsed"] == 1
    assert len(cands) == 1
    assert sorted(map(tuple, cands[0]["join_keys"])) == [("customer_id", "id")]


def test_sql_subquery_in_from_resolves():
    sql = ("SELECT 1 FROM (SELECT customer_id FROM orders) sub "
           "JOIN customers c ON sub.customer_id = c.id")
    cands, _ = rd.extract_sql_joins(sql, [ORDERS, CUSTOMERS], None)
    assert len(cands) == 1
    assert sorted(map(tuple, cands[0]["join_keys"])) == [("customer_id", "id")]


def test_sql_composite_on_is_one_candidate():
    shipments = _table(TID_C, "shipments", ["order_id", "line_no"])
    sql = ("SELECT 1 FROM orders o JOIN shipments s "
           "ON s.order_id = o.order_id AND s.line_no = o.amount")
    cands, _ = rd.extract_sql_joins(sql, [ORDERS, CUSTOMERS, shipments], None)
    assert len(cands) == 1
    assert len(cands[0]["join_keys"]) == 2


def test_sql_unregistered_table_filtered_and_literals_stripped():
    sql = ("SELECT 1 FROM orders o JOIN warehouses w ON o.customer_id = w.id "
           "WHERE o.amount = 5 AND o.customer_id = 'X'")
    cands, stats = rd.extract_sql_joins(sql, [ORDERS, CUSTOMERS], None)
    assert cands == [] and stats["parsed"] == 1


def test_sql_garbage_never_raises_and_never_echoes_sql():
    marker = "zqxwv_secret_value_991"
    sql = f"SELECT FROM WHERE {marker} ((;; not sql at all"
    cands, stats = rd.extract_sql_joins(sql, [ORDERS, CUSTOMERS], None)
    assert stats["failed"] >= 1
    flat = repr((cands, stats))
    assert marker not in flat


def test_sql_computed_projection_dropped():
    sql = ("WITH agg AS (SELECT customer_id + 1 AS cid FROM orders) "
           "SELECT 1 FROM agg a JOIN customers c ON a.cid = c.id")
    cands, _ = rd.extract_sql_joins(sql, [ORDERS, CUSTOMERS], None)
    assert cands == []


# ── verification math ──────────────────────────────────────────────────────

def _loader(snaps):
    def load(tid, cols):
        df = snaps.get(tid)
        if df is None:
            return None
        return df[[c for c in cols if c in df.columns]]
    return load


def _cand(child=ORDERS, parent=CUSTOMERS, keys=(("customer_id", "id"),), source="name"):
    return rd._make_candidate(child, parent, list(keys), source, {"score": 1.0})


def test_verify_n_to_1_overlap_and_orphans():
    snaps = {TID_A: pd.DataFrame({"customer_id": [1, 1, 2, 3, None]}),
             TID_B: pd.DataFrame({"id": [1, 2, 4]})}
    v = rd.verify_candidates([_cand()], _loader(snaps))[0]
    assert v["verified"] is True
    assert v["cardinality"] == "N:1"
    assert v["overlap_pct"] == 75.0              # 3 of 4 non-null child rows
    assert v["orphans"] == 1
    assert v["child_nonnull"] == 4
    assert v["child_unique"] is False and v["parent_unique"] is True


def test_verify_flip_stores_many_to_one_with_correct_parent():
    snaps = {TID_A: pd.DataFrame({"customer_id": [1, 2, 3]}),        # unique
             TID_B: pd.DataFrame({"id": [1, 1, 2, 9]})}              # not unique
    v = rd.verify_candidates([_cand()], _loader(snaps))[0]
    assert v["cardinality"] == "N:1"
    assert v["table_id"] == TID_B and v["related_table_id"] == TID_A
    assert v["join_keys"] == [["id", "customer_id"]]
    assert v["overlap_pct"] == 75.0              # 3 of 4 ids appear in orders


def test_verify_fk_orientation_never_flipped_by_data():
    """Reviewer finding: a declared FK is ground truth on direction — snapshot
    uniqueness must not swap child/parent even when the data looks inverted."""
    snaps = {TID_A: pd.DataFrame({"customer_id": [1, 2, 3]}),        # unique
             TID_B: pd.DataFrame({"id": [1, 1, 2, 9]})}              # not unique
    cand = rd._make_candidate(ORDERS, CUSTOMERS, [("customer_id", "id")], "fk", True)
    v = rd.verify_candidates([cand], _loader(snaps))[0]
    assert v["table_id"] == TID_A and v["related_table_id"] == TID_B
    assert v["join_keys"] == [["customer_id", "id"]]
    assert v["cardinality"] == "1:N"
    assert rd.band(v) == "confirmed"             # FK stays confirmed regardless


def test_verify_one_to_one():
    snaps = {TID_A: pd.DataFrame({"customer_id": [1, 2, 3]}),
             TID_B: pd.DataFrame({"id": [1, 2, 3]})}
    v = rd.verify_candidates([_cand()], _loader(snaps))[0]
    assert v["cardinality"] == "1:1" and v["overlap_pct"] == 100.0


def test_verify_sibling_fk_is_n_to_m_and_needs_attention():
    """Two fact tables sharing a dimension key: both sides non-unique ->
    N:M -> the needs-attention band, never confirmed."""
    payments = _table(TID_C, "payments", ["customer_id"])
    snaps = {TID_A: pd.DataFrame({"customer_id": [1, 1, 2]}),
             TID_C: pd.DataFrame({"customer_id": [1, 2, 2]})}
    v = rd.verify_candidates(
        [_cand(child=ORDERS, parent=payments,
               keys=(("customer_id", "customer_id"),))], _loader(snaps))[0]
    assert v["cardinality"] == "N:M"
    assert rd.band(v) == "attention"


def test_verify_composite_key():
    shipments = _table(TID_C, "shipments", ["order_id", "line_no"])
    snaps = {
        TID_C: pd.DataFrame({"order_id": [1, 1, 2, 2], "line_no": [1, 2, 1, 9]}),
        TID_A: pd.DataFrame({"order_id": [1, 1, 2], "amount": [1, 2, 1]}),
    }
    cand = rd._make_candidate(shipments, ORDERS,
                              [("order_id", "order_id"), ("line_no", "amount")],
                              "sql", {"frequency": 1})
    v = rd.verify_candidates([cand], _loader(snaps))[0]
    assert v["verified"] is True
    assert v["overlap_pct"] == 75.0              # (2,9) has no parent tuple
    assert v["orphans"] == 1


def test_verify_dtype_mismatch_still_matches():
    snaps = {TID_A: pd.DataFrame({"customer_id": [1.0, 2.0, None]}),   # float64 w/ NaN
             TID_B: pd.DataFrame({"id": ["1", "2", "3"]})}             # object str
    v = rd.verify_candidates([_cand()], _loader(snaps))[0]
    assert v["overlap_pct"] == 100.0
    assert v["evidence"].get("dtype_cast") is True


def test_verify_missing_snapshot_or_column_unverified():
    v = rd.verify_candidates([_cand()], _loader({}))[0]
    assert v["verified"] is False and v["unverified_reason"]
    assert rd.band(v) == "attention"

    snaps = {TID_A: pd.DataFrame({"other": [1]}),
             TID_B: pd.DataFrame({"id": [1]})}
    v = rd.verify_candidates([_cand()], _loader(snaps))[0]
    assert v["verified"] is False


def test_verify_loader_exception_is_contained():
    def bad_loader(tid, cols):
        raise RuntimeError("disk on fire")
    v = rd.verify_candidates([_cand()], bad_loader)[0]
    assert v["verified"] is False


# ── banding precedence ─────────────────────────────────────────────────────

def _banded(**over):
    c = {"sources": ["name"], "verified": True, "cardinality": "N:1",
         "overlap_pct": 80.0, "sql_frequency": 0}
    c.update(over)
    return rd.band(c)


def test_band_matrix():
    assert _banded(sources=["fk"], verified=False) == "confirmed"    # FK is ground truth
    assert _banded(sources=["fk"], cardinality="N:M") == "confirmed"
    assert _banded(verified=False) == "attention"
    assert _banded(cardinality="N:M", overlap_pct=99.0) == "attention"
    assert _banded(overlap_pct=60.0, sql_frequency=5) == "attention"  # attention overrides sql freq
    assert _banded(sources=["sql"], sql_frequency=2) == "confirmed"
    assert _banded(overlap_pct=96.0) == "confirmed"
    assert _banded(overlap_pct=96.0, cardinality="1:1") == "confirmed"
    assert _banded(overlap_pct=80.0) == "suggested"
    assert _banded(sources=["sql"], sql_frequency=1, overlap_pct=80.0) == "suggested"


# ── merge + filter_existing ────────────────────────────────────────────────

def test_merge_unions_evidence_and_fk_orientation_wins():
    name = rd._make_candidate(CUSTOMERS, ORDERS, [("id", "customer_id")],
                              "name", {"score": 0.8})       # wrong orientation
    fk = rd._make_candidate(ORDERS, CUSTOMERS, [("customer_id", "id")],
                            "fk", True)
    assert name["candidate_id"] == fk["candidate_id"]        # direction-independent
    merged = rd.merge_candidates([name], [fk])
    assert len(merged) == 1
    m = merged[0]
    assert m["sources"] == ["fk", "name"]
    assert m["table_id"] == TID_A                            # FK orientation adopted
    assert "name" in m["evidence"] and "fk" in m["evidence"]


def test_filter_existing_drops_declared_including_flipped_and_legacy_name():
    cand = _cand()
    # declared on the child by id, same keys
    t1 = dict(ORDERS, relations=[{"related_table_id": TID_B,
                                  "join_keys": [["customer_id", "id"]]}])
    assert rd.filter_existing([cand], [t1, CUSTOMERS]) == []
    # declared FLIPPED on the parent side
    t2 = dict(CUSTOMERS, relations=[{"related_table_id": TID_A,
                                     "join_keys": [["id", "customer_id"]]}])
    assert rd.filter_existing([cand], [ORDERS, t2]) == []
    # legacy name-based ref
    t3 = dict(ORDERS, relations=[{"related_table": "customers",
                                  "join_keys": [["customer_id", "id"]]}])
    assert rd.filter_existing([cand], [t3, CUSTOMERS]) == []
    # different keys -> kept
    t4 = dict(ORDERS, relations=[{"related_table_id": TID_B,
                                  "join_keys": [["amount", "id"]]}])
    assert rd.filter_existing([cand], [t4, CUSTOMERS]) == [cand]


# ── physical identity (duplicate registrations) ────────────────────────────

def _phys_table(tid, name, cols, *, conn="conn-1", schema="shop",
                connector=False, created="2026-01-01", **kw):
    t = _table(tid, name, cols, schema=schema, **kw)
    t["connection_id"] = conn
    t["is_connector"] = connector
    t["created_at"] = created
    return t


def test_physical_key_and_same_physical_filter():
    a = _phys_table(TID_A, "city_dict", ["city_code"], connector=True)
    b = _phys_table(TID_B, "CITY_DICT", ["city_code"])       # case-insensitive
    other_conn = _phys_table(TID_C, "city_dict", ["city_code"], conn="conn-2")
    assert rd.physical_key(a) == rd.physical_key(b)
    assert rd.physical_key(a) != rd.physical_key(other_conn)

    same = rd._make_candidate(a, b, [("city_code", "city_code")], "name", {})
    cross = rd._make_candidate(a, other_conn, [("city_code", "city_code")], "name", {})
    out = rd.filter_same_physical([same, cross], [a, b, other_conn])
    assert out == [cross]        # same physical dropped; cross-connection kept


def test_same_physical_never_matches_incomplete_keys():
    """A wizard draft without connection_id must never false-positive."""
    a = _table(TID_A, "orders", ["x"])           # no connection_id
    b = _table(TID_B, "orders", ["x"])
    cand = rd._make_candidate(a, b, [("x", "x")], "name", {})
    assert rd.filter_same_physical([cand], [a, b]) == [cand]


def test_dedupe_physical_targets_prefers_connector_and_notes_alternates():
    child = _phys_table(TID_A, "orders", ["city_code"])
    dict_a = _phys_table(TID_B, "city_dict", ["city_code"], connector=True,
                         created="2026-02-01", display="cities dictionary")
    dict_b = _phys_table(TID_C, "city_dict", ["city_code"],
                         created="2026-01-01", display="city dictionary")
    c1 = rd._make_candidate(child, dict_a, [("city_code", "city_code")], "fk", True)
    c2 = rd._make_candidate(child, dict_b, [("city_code", "city_code")], "fk", True)
    out = rd.dedupe_physical_targets([c2, c1], [child, dict_a, dict_b])
    assert len(out) == 1
    kept = out[0]
    assert kept["related_table_id"] == TID_B             # connector wins
    assert kept["alternate_targets"] == [{"id": TID_C, "label": "city dictionary"}]

    # tie (neither connector) -> earliest created_at wins
    dict_a2 = dict(dict_a, is_connector=False)
    out = rd.dedupe_physical_targets([c1, c2], [child, dict_a2, dict_b])
    assert out[0]["related_table_id"] == TID_C           # created 2026-01-01


def test_dedupe_physical_collapses_child_side_too():
    dup1 = _phys_table(TID_A, "city_dict", ["region"], connector=True)
    dup2 = _phys_table(TID_B, "city_dict", ["region"])
    other = _phys_table(TID_C, "regions", ["region"])
    c1 = rd._make_candidate(dup1, other, [("region", "region")], "name", {})
    c2 = rd._make_candidate(dup2, other, [("region", "region")], "name", {})
    out = rd.dedupe_physical_targets([c2, c1], [dup1, dup2, other])
    assert len(out) == 1 and out[0]["table_id"] == TID_A  # connector side kept


def test_filter_existing_physical_blocks_duplicate_registration_reproposal():
    child = _phys_table(TID_A, "orders", ["city_code"])
    dict_a = _phys_table(TID_B, "city_dict", ["city_code"], connector=True)
    dict_b = _phys_table(TID_C, "city_dict", ["city_code"])
    # declared to registration A only
    child_declared = dict(child, relations=[{
        "related_table_id": TID_B, "join_keys": [["city_code", "city_code"]]}])
    to_b = rd._make_candidate(child, dict_b, [("city_code", "city_code")], "name", {})
    assert rd.filter_existing_physical([to_b], [child_declared, dict_a, dict_b]) == []
    # flipped orientation also blocked
    flipped = rd._make_candidate(dict_b, child, [("city_code", "city_code")], "name", {})
    assert rd.filter_existing_physical([flipped], [child_declared, dict_a, dict_b]) == []
    # different columns still proposed
    other_cols = rd._make_candidate(child, dict_b, [("city_code", "city_name")], "name", {})
    assert rd.filter_existing_physical([other_cols],
                                       [child_declared, dict_a, dict_b]) == [other_cols]


def test_sql_duplicate_registration_resolves_to_preferred_and_unknown_reported():
    orders = _phys_table(TID_A, "orders", ["city_code", "amount"])
    dict_a = _phys_table(TID_B, "city_dict", ["city_code"], connector=True,
                         display="cities dictionary")
    dict_b = _phys_table(TID_C, "city_dict", ["city_code"])
    sql = ("SELECT 1 FROM shop.orders o JOIN shop.city_dict d ON o.city_code = d.city_code;"
           "SELECT 1 FROM shop.orders o JOIN shop.warehouses w ON o.city_code = w.id")
    cands, stats = rd.extract_sql_joins(sql, [orders, dict_a, dict_b], None)
    assert len(cands) == 1
    ids = {cands[0]["table_id"], cands[0]["related_table_id"]}
    assert ids == {TID_A, TID_B}                 # preferred (connector) registration
    assert stats["unknown_tables"] == ["shop.warehouses"]


def test_prefer_registration_cross_connection_stays_ambiguous():
    """Reviewer regression pin: preference applies ONLY to duplicate
    registrations of one physical table — same-named tables on different
    connections stay genuinely ambiguous."""
    a = _phys_table(TID_A, "orders", ["x"], conn="conn-1")
    b = _phys_table(TID_B, "orders", ["x"], conn="conn-2")
    assert rd.prefer_registration([a, b]) is None
    dup = _phys_table(TID_C, "orders", ["x"], conn="conn-1", connector=True)
    assert rd.prefer_registration([a, dup])["id"] == TID_C
    assert rd.prefer_registration([]) is None


def test_sql_unqualified_duplicate_name_resolves_to_preferred():
    """Reviewer regression pin: an unqualified SQL name matching TWO
    registrations of one physical table resolves (it is a duplicate, not an
    unknown) — the admin must not be told to 'register' a blocked table."""
    orders = _phys_table(TID_A, "orders", ["city_code", "amount"])
    dict_a = _phys_table(TID_B, "city_dict", ["city_code"], connector=True)
    dict_b = _phys_table(TID_C, "city_dict", ["city_code"])
    sql = "SELECT 1 FROM orders o JOIN city_dict d ON o.city_code = d.city_code"
    cands, stats = rd.extract_sql_joins(sql, [orders, dict_a, dict_b], None)
    assert len(cands) == 1
    assert {cands[0]["table_id"], cands[0]["related_table_id"]} == {TID_A, TID_B}
    assert stats["unknown_tables"] == []


def test_sql_same_name_two_connections_dropped_not_unknown():
    orders = _phys_table(TID_A, "orders", ["x"])
    t1 = _phys_table(TID_B, "dims", ["x"], conn="conn-1")
    t2 = _phys_table(TID_C, "dims", ["x"], conn="conn-2")
    sql = "SELECT 1 FROM shop.orders o JOIN shop.dims d ON o.x = d.x"
    cands, stats = rd.extract_sql_joins(sql, [orders, t1, t2], None)
    assert cands == []                           # ambiguous -> dropped...
    assert stats["unknown_tables"] == []         # ...but NOT "not registered"


def test_discover_end_to_end_excludes_declared():
    fk_map = {TID_A: _fk_intro([{
        "constrained_columns": ["customer_id"], "referred_schema": None,
        "referred_table": "customers", "referred_columns": ["id"]}])}
    cands = rd.discover([ORDERS, CUSTOMERS], fk_map)
    assert len(cands) == 1                        # fk + name merged into one
    assert set(cands[0]["sources"]) == {"fk", "name"}
    declared = dict(ORDERS, relations=[{"related_table_id": TID_B,
                                        "join_keys": [["customer_id", "id"]]}])
    assert rd.discover([declared, CUSTOMERS], fk_map) == []
