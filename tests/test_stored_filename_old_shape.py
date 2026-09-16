"""T-old-shape — chats written BEFORE the upload-filename sanitizer must keep
loading after an image upgrade (CLAUDE.md "Data safety": a stored-shape change
ships an old-shape regression test).

The sanitizer only runs on NEW uploads. Names already on disk and already in
`chatdata/<id>/meta.json` are never rewritten, so a chat created by the
previous release can carry a stored name today's sanitizer would ALTER:

  * a >200-byte name (today capped at 200 UTF-8 bytes),
  * a name whose stem ends in a space ("sales .csv" -> "sales.csv"),
  * a leading-dot name (".hidden.csv" -> "hidden.csv").

What must not change is what those chats do after the upgrade: the files are
still parsed under their stored df keys and `schema_docs()` still carries
their meta entries verbatim (that text is what schema_text is built from).

The leading-dot case is pinned as it REALLY behaves, not as one might hope:
every loader in local_store skips names starting with "." (that is how
`.parquet_cache` and `.profiles` stay invisible), so such a file never
produced a dataframe — before OR after the sanitizer. Its meta entry still
loads, and that is the whole compatibility claim for it.
"""
import pytest

import local_store
from settings import settings

CHAT = "chatoldshape1"
CSV = b"a,b\n1,2\n3,4\n"
# 70 Georgian letters (3 bytes each) + ".csv" = 214 UTF-8 bytes: over the
# sanitizer's 200-byte cap, yet only 74 characters — short enough for
# Windows' 260-character MAX_PATH under a pytest tmp_path.
LONG_NAME = "ა" * 70 + ".csv"
SPACED_NAME = "sales .csv"
HIDDEN_NAME = ".hidden.csv"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    st = local_store.ChatDataStore(CHAT)
    meta = {"owner": "user@x.com", "files": []}
    for name in (LONG_NAME, SPACED_NAME, HIDDEN_NAME):
        (st.files_dir / name).write_bytes(CSV)
        meta["files"].append({
            "file_name": name,
            "file_description": f"pre-sanitizer file {name[:12]}",
            "schema": {"file_name": name,
                       "fields": {"a": {"description": "col a", "values": None},
                                  "b": {"description": "col b", "values": None}}},
        })
    st.write_meta(meta)
    yield st
    local_store._DATAFRAME_CACHE.invalidate()


def test_sanitizer_would_alter_these_stored_names():
    """Guard for the guard: if the sanitizer ever stops changing these names
    the fixture is no longer testing an old shape."""
    assert local_store.sanitize_upload_filename(LONG_NAME) != LONG_NAME
    assert local_store.sanitize_upload_filename(SPACED_NAME) != SPACED_NAME
    assert local_store.sanitize_upload_filename(HIDDEN_NAME) != HIDDEN_NAME


def test_old_shape_files_still_load_under_their_stored_keys(store):
    dfs = store.load_dataframes()
    assert LONG_NAME in dfs, sorted(dfs)
    assert SPACED_NAME in dfs, sorted(dfs)
    assert list(dfs[LONG_NAME].columns) == ["a", "b"]
    assert len(dfs[SPACED_NAME]) == 2
    # Pre-existing behavior, unrelated to the sanitizer: dot-prefixed files are
    # skipped by every loader, so this stored name yields no dataframe.
    assert HIDDEN_NAME not in dfs, sorted(dfs)


def test_old_shape_meta_entries_survive_in_schema_docs(store):
    docs = store.schema_docs()
    assert set(docs) == {LONG_NAME, SPACED_NAME, HIDDEN_NAME}
    for name in docs:
        assert docs[name]["file_description"].startswith("pre-sanitizer file")
        assert sorted(docs[name]["fields"]) == ["a", "b"]
    # File entries emit EXACTLY the two keys they always did (no DB extras).
    assert set(docs[LONG_NAME]) == {"file_description", "fields"}


def test_old_shape_meta_is_not_rewritten_by_a_load(store):
    before = store.meta_path.read_text(encoding="utf-8")
    store.load_dataframes()
    store.schema_docs()
    assert store.meta_path.read_text(encoding="utf-8") == before
    names = [e["file_name"] for e in store.read_meta()["files"]]
    assert names == [LONG_NAME, SPACED_NAME, HIDDEN_NAME]
