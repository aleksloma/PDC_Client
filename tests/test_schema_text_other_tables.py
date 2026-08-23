"""OTHER REGISTERED TABLES schema section (MISSING_DATA support, QA report §4).

The planner can only SUGGEST the missing table if the schema text names the
registered-but-not-loaded tables. Gated on the chat using DB tables; role
filtered; capped at 20; metadata only. File-only chats stay byte-identical.
"""
import pandas as pd
import pytest

import schema_builder
import run_chat_local
from settings import settings


def _db_doc():
    return {"file_description": "clients", "fields": {},
            "source": "database", "db_table": "shop.cl_info",
            "refreshed_at": "2026-07-27T00:00:00+00:00", "relations": []}


# --- schema_builder rendering ------------------------------------------------

def test_section_renders_names_columns_and_relations():
    dfs = {"clients information": pd.DataFrame({"client_id": [1]})}
    docs = {"clients information": _db_doc()}
    other = [{"id": "aa" * 8, "display_name": "branches dictionary",
              "updated_at": "2026-08-01", "columns": ["branch_id", "city"],
              "relations": [{"related_display_name": "clients information",
                             "join_keys": [["branch_id", "branch_id"]]}]}]
    text = schema_builder._schema_text_uncached(docs, dfs, None, other)
    assert "OTHER REGISTERED TABLES" in text
    assert "branches dictionary: columns [branch_id, city]" in text
    assert "relates to loaded table clients information via [branch_id ⟷ branch_id]" in text
    assert "NOT loaded in this chat" in text


def test_absent_other_tables_byte_identical():
    dfs = {"clients information": pd.DataFrame({"client_id": [1]})}
    docs = {"clients information": _db_doc()}
    base = schema_builder._schema_text_uncached(docs, dfs)
    assert schema_builder._schema_text_uncached(docs, dfs, None, None) == base
    assert schema_builder._schema_text_uncached(docs, dfs, None, []) == base
    assert "OTHER REGISTERED TABLES" not in base


def test_cache_key_changes_with_other_tables():
    dfs = {"a": pd.DataFrame({"x": [1]})}
    docs = {"a": {"file_description": "", "fields": {}}}
    k_none = schema_builder._schema_text_cache_key(docs, dfs, None)
    k_absent = schema_builder._schema_text_cache_key(docs, dfs, None, None)
    assert k_none == k_absent          # pre-feature keys stay valid
    rows_v1 = [{"id": "aa" * 8, "display_name": "t", "updated_at": "1",
                "columns": ["c"], "relations": []}]
    rows_v2 = [{**rows_v1[0], "updated_at": "2"}]   # registry re-registered
    k1 = schema_builder._schema_text_cache_key(docs, dfs, None, rows_v1)
    k2 = schema_builder._schema_text_cache_key(docs, dfs, None, rows_v2)
    assert k_none != k1 != k2


# --- _other_registered_tables assembly ---------------------------------------

@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    from cryptography.fernet import Fernet
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import db_sources
    store = db_sources.DataSourceStore()
    conn = store.create_connection(
        {"name": "c", "db_type": "postgresql", "host": "h", "port": 5432,
         "database": "d", "user": "u"}, "pw", actor="ladmin")
    ids = {}
    for i, (tname, disp, is_conn) in enumerate([
            ("cl_info", "clients information", False),
            ("br_dict", "branches dictionary", False),
            ("hidden_conn", "hidden connector", True)] +
            [(f"extra{n}", f"extra table {n:02d}", False) for n in range(25)]):
        doc = {"id": f"{i:016x}", "connection_id": conn["id"], "schema": "",
               "table_name": tname, "display_name": disp,
               "description": "", "columns": [{"name": "branch_id", "dtype": "int"},
                                              {"name": "city", "dtype": "text"}],
               "is_connector": is_conn,
               "relations": ([{"related_table_id": f"{0:016x}",
                               "join_keys": [["branch_id", "branch_id"]]}]
                             if tname == "br_dict" else [])}
        store.upsert_table(doc, actor="ladmin")
        ids[disp] = doc["id"]
    return ids


def test_helper_gated_on_db_chat_and_caps_at_20(registry, monkeypatch):
    import roles_store
    monkeypatch.setattr(roles_store, "allowed_table_ids_for",
                        lambda email: set(registry.values()))
    dfs = {"clients information": pd.DataFrame({"branch_id": [1]})}
    docs = {"clients information": _db_doc()}
    rows = run_chat_local._other_registered_tables(docs, dfs, "u@x.com")
    names = [r["display_name"] for r in rows]
    # loaded table excluded, connector excluded, capped at 20, sorted
    assert "clients information" not in names
    assert "hidden connector" not in names
    assert len(rows) == 20
    assert names == sorted(names)
    # the related-to-loaded relation survives with display names
    br = next(r for r in rows if r["display_name"] == "branches dictionary")
    assert br["relations"] == [{"related_display_name": "clients information",
                                "join_keys": [["branch_id", "branch_id"]]}]
    assert br["columns"] == ["branch_id", "city"]


def test_helper_absent_for_file_only_chat(registry, monkeypatch):
    import roles_store
    monkeypatch.setattr(roles_store, "allowed_table_ids_for",
                        lambda email: set(registry.values()))
    dfs = {"sales.csv": pd.DataFrame({"a": [1]})}
    docs = {"sales.csv": {"file_description": "", "fields": {}}}
    assert run_chat_local._other_registered_tables(docs, dfs, "u@x.com") == []


def test_helper_role_filtered_fail_closed(registry, monkeypatch):
    import roles_store
    monkeypatch.setattr(roles_store, "allowed_table_ids_for", lambda email: set())
    dfs = {"clients information": pd.DataFrame({"branch_id": [1]})}
    docs = {"clients information": _db_doc()}
    assert run_chat_local._other_registered_tables(docs, dfs, "u@x.com") == []


def test_helper_never_raises(monkeypatch, tmp_path):
    # registry read blowing up → [] (Article IV), never a dead plan call
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    import db_sources
    monkeypatch.setattr(db_sources.DataSourceStore, "list_tables",
                        lambda self, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    dfs = {"t": pd.DataFrame({"a": [1]})}
    docs = {"t": _db_doc()}
    assert run_chat_local._other_registered_tables(docs, dfs, "u@x.com") == []
