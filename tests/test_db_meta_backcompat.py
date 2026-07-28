"""Data-safety regression: a PRE-FEATURE meta.json (no `source` key anywhere)
must load, render, and produce byte-identical schema_text after the database-
tables feature ships — customers upgrade the image against existing volumes."""
import json

import pandas as pd
import pytest

import local_store
import schema_builder


OLD_SHAPE_META = {
    "files": [
        {
            "file_name": "sales.csv",
            "file_description": "Employee salaries",
            "schema": {
                "file_name": "sales.csv",
                "fields": {
                    "name": {"description": "Employee name",
                             "technical_description": "object, 3/3 filled",
                             "values": None},
                    "salary": {"description": "Annual gross",
                               "technical_description": "int64, 3/3 filled",
                               "values": None},
                },
            },
        }
    ],
    "common_fields": [],
    "owner": "user@x.com",
    "title": "Old chat",
    "chat_id": "c_oldshape",
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store.settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    yield
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def old_chat(tmp_path):
    store = local_store.ChatDataStore("c_oldshape")
    store.meta_path.write_text(json.dumps(OLD_SHAPE_META), encoding="utf-8")
    (store.files_dir / "sales.csv").write_text(
        "name,salary\nA,100\nB,200\nC,300\n", encoding="utf-8")
    return store


def test_old_shape_meta_loads_as_files(old_chat):
    assert local_store.db_entries_from_meta(old_chat.read_meta()) == []
    dfs = old_chat.load_dataframes()
    assert list(dfs) == ["sales.csv"]
    assert len(dfs["sales.csv"]) == 3


def test_old_shape_schema_docs_exact_keys(old_chat):
    docs = old_chat.schema_docs()
    assert set(docs) == {"sales.csv"}
    # EXACTLY the historical two keys — no new fields leak onto file entries.
    assert set(docs["sales.csv"]) == {"file_description", "fields"}


def test_old_shape_schema_text_has_no_db_sections(old_chat):
    dfs = old_chat.load_dataframes()
    text = schema_builder.schema_text(old_chat.schema_docs(), dfs, [])
    assert "Database Relations" not in text
    assert "Source: database table" not in text
    assert "File: sales.csv" in text
    assert "Employee name" in text
