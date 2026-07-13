"""schema_text memoization tests: repeat calls with identical inputs must not
re-scan dataframe columns; different inputs must produce distinct entries."""
import pandas as pd
import pytest

import schema_builder


@pytest.fixture(autouse=True)
def _fresh_cache():
    schema_builder._SCHEMA_TEXT_CACHE.invalidate()
    yield
    schema_builder._SCHEMA_TEXT_CACHE.invalidate()


def _dfs():
    return {"f.csv": pd.DataFrame({"cat": ["a", "b", "a", "c"], "num": [1, 2, 3, 4]})}


def _docs():
    return {"f.csv": {"file_description": "test file",
                      "fields": {"cat": {"description": "category"},
                                 "num": {"description": "number"}}}}


def test_second_call_is_cached(monkeypatch):
    first = schema_builder.schema_text(_docs(), _dfs())
    calls = {"n": 0}
    real = schema_builder._schema_text_uncached

    def _counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(schema_builder, "_schema_text_uncached", _counting)
    second = schema_builder.schema_text(_docs(), _dfs())
    assert calls["n"] == 0  # served from cache, no recompute
    assert second == first
    assert "CATEGORICAL" in second  # sanity: real content, not empty


def test_different_inputs_get_distinct_entries():
    out1 = schema_builder.schema_text(_docs(), _dfs())
    docs2 = _docs()
    docs2["f.csv"]["file_description"] = "changed description"
    out2 = schema_builder.schema_text(docs2, _dfs())
    assert out1 != out2
    assert "changed description" in out2


def test_different_df_name_set_distinct():
    out1 = schema_builder.schema_text(_docs(), _dfs())
    dfs2 = _dfs()
    dfs2["g.csv"] = pd.DataFrame({"x": [1]})
    out2 = schema_builder.schema_text(_docs(), dfs2)
    assert out1 != out2
    assert "File: g.csv" in out2


def test_same_names_different_data_distinct_entries():
    # Two chats whose files share names/columns but hold DIFFERENT data must
    # never be served each other's schema string (its CATEGORICAL listings
    # embed actual values).
    out1 = schema_builder.schema_text(_docs(), _dfs())
    dfs_other = {"f.csv": pd.DataFrame({"cat": ["x", "y", "z", "x"], "num": [9, 8, 7, 6]})}
    out2 = schema_builder.schema_text(_docs(), dfs_other)
    assert out1 != out2
    assert "x" in out2 and "'a'" not in out2.replace('"', "'").split("CATEGORICAL")[0]


def test_changed_data_same_schema_docs_refreshes():
    # Add Data overwrite that renames a column but leaves meta unchanged must
    # not serve the stale column list.
    out1 = schema_builder.schema_text(_docs(), _dfs())
    dfs2 = {"f.csv": pd.DataFrame({"category_renamed": ["a", "b"], "num": [1, 2]})}
    out2 = schema_builder.schema_text(_docs(), dfs2)
    assert "category_renamed" in out2
    assert out1 != out2


def test_cache_key_failure_still_computes(monkeypatch):
    monkeypatch.setattr(schema_builder, "_schema_text_cache_key", lambda *a: None)
    out = schema_builder.schema_text(_docs(), _dfs())
    assert "File: f.csv" in out
    assert len(schema_builder._SCHEMA_TEXT_CACHE) == 0  # never cached
