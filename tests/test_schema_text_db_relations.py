"""schema_text rendering for database entries: Source line, Database Relations
block gated on both endpoints being loaded, and byte-identical output for
file-only schema_docs (no prompt drift for existing chats)."""
import pandas as pd

import schema_builder


def _db_doc(db_table="shop.cl_info", refreshed="2026-07-27T00:00:00+00:00",
            relations=None):
    return {"file_description": "clients", "fields": {},
            "source": "database", "db_table": db_table,
            "refreshed_at": refreshed, "relations": relations or []}


def test_db_entry_renders_source_and_relations():
    dfs = {"clients information": pd.DataFrame({"client_id": [1]}),
           "cities dictionary": pd.DataFrame({"city_code": [10]})}
    docs = {
        "clients information": _db_doc(relations=[
            {"related_table_id": "x", "related_df_key": "cities dictionary",
             "join_keys": [["city_code", "city_code"]]}]),
        "cities dictionary": _db_doc(db_table="shop.city_dict"),
    }
    text = schema_builder._schema_text_uncached(docs, dfs)
    assert "Source: database table shop.cl_info (snapshot as of 2026-07-27T00:00:00+00:00)" in text
    assert "Database Relations (declared join keys):" in text
    assert "clients information[city_code] ⟷ cities dictionary[city_code]" in text


def test_relation_with_unloaded_endpoint_is_dropped():
    dfs = {"clients information": pd.DataFrame({"a": [1]})}
    docs = {"clients information": _db_doc(relations=[
        {"related_table_id": "x", "related_df_key": "not loaded",
         "join_keys": [["a", "b"]]}])}
    text = schema_builder._schema_text_uncached(docs, dfs)
    assert "Database Relations" not in text


def test_relation_deduped_when_declared_on_both_sides():
    dfs = {"a": pd.DataFrame({"k": [1]}), "b": pd.DataFrame({"k": [1]})}
    rel_ab = [{"related_df_key": "b", "join_keys": [["k", "k"]]}]
    rel_ba = [{"related_df_key": "a", "join_keys": [["k", "k"]]}]
    docs = {"a": _db_doc(relations=rel_ab), "b": _db_doc(relations=rel_ba)}
    text = schema_builder._schema_text_uncached(docs, dfs)
    assert text.count("⟷") == 1


def test_file_only_schema_text_byte_identical():
    """The regression pin: a schema_docs with no `source` keys renders EXACTLY
    what the pre-feature renderer produced."""
    dfs = {"sales.csv": pd.DataFrame({"name": ["A", "B"], "salary": [1, 2]})}
    docs = {"sales.csv": {"file_description": "Employee salaries",
                          "fields": {"name": {"description": "Employee name",
                                              "technical_description": "object",
                                              "values": None},
                                     "salary": {"description": "Annual gross",
                                                "technical_description": "int64",
                                                "values": None}}}}
    text = schema_builder._schema_text_uncached(docs, dfs, [])
    expected = (
        "File: sales.csv\n"
        "File Description: Employee salaries\n"
        "Columns: name, salary\n"
        'Column Descriptions (business): {"name": "Employee name", "salary": "Annual gross"}\n'
        'Column Technical Info: {"name": "object", "salary": "int64"}\n'
        'Column Types: {"name": "CATEGORICAL (2 unique values: A, B)", "salary": "int64"}'
    )
    assert text == expected
    assert "Database Relations" not in text
    assert "Source: database" not in text
