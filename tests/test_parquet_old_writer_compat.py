"""Parquet files written by the pyarrow version this image shipped BEFORE the
current pin must still load unchanged under the pinned pyarrow.

A customer upgrades the image against their existing data volume, which is
full of parquet produced by the older writer: one central snapshot per
registered database table (`DATA_ROOT/db_snapshots/{table_id}.parquet`) and
one entry per cached parsed table (`<files_dir>/.parquet_cache/`). Both read
paths are pinned here against committed old-writer fixtures, dtype by dtype,
including the null markers (NaN / NaT / None) that are the parts most likely
to shift between arrow versions.

The stale-manifest guard is pinned too: an OLD-writer cache whose
`parser_version` no longer matches must be re-parsed from the source file, not
served.

The fixtures live in `tools/fixtures/legacy_pyarrow17/` and were written by
`pyarrow==17.0.0` / `pandas==2.2.3`:

| File | Shape it stands for | Columns |
|---|---|---|
| `legacy_snapshot.parquet` | one `db_snapshots/{table_id}.parquet` | int64, str with None, float64 with NaN, datetime64 with NaT, bool |
| `legacy_cache.parquet` | one entry of a `.parquet_cache/` directory | str, int64, float64 with NaN, datetime64 (one name carries a space) |

Neither carries an index, which is how both writers in `local_store.py`
produce them. NEVER re-save them with the current pyarrow: being old-writer
output is the whole point, and the first test fails loudly if someone does.
Regenerate only if the stored shape itself changes, with the old versions in a
throwaway environment:

    # pip install pyarrow==17.0.0 pandas==2.2.3 numpy==1.26.4
    df.to_parquet("legacy_snapshot.parquet", engine="pyarrow", index=False)

The reverse direction — files written by the CURRENT pyarrow being read by the
older one, which is what a rollback does — is not pinned by a fixture, because
it would need the old library at test time. Check it the same way when a
release changes the pyarrow pin: write a frame with the current version, read
it with the previous one in a throwaway environment, and cover tz-aware,
timedelta, NaT, a named index and MultiIndex columns.
"""
import json
import shutil
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

import local_store


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tools" / "fixtures" / "legacy_pyarrow17"
SNAPSHOT_FIXTURE = FIXTURE_DIR / "legacy_snapshot.parquet"
CACHE_FIXTURE = FIXTURE_DIR / "legacy_cache.parquet"

# The writer version the fixtures stand for. It must appear in the files'
# `created_by` metadata, otherwise they are not old-writer output any more.
OLD_WRITER_VERSION = "17.0.0"

# 16-hex, per local_store._DB_TABLE_ID_RE - db_snapshot_path derives the
# snapshot filename from it.
TID = "a1b2c3d4e5f60718"

REGENERATED_MSG = (
    "fixture {name} no longer reports the old writer in its parquet "
    "`created_by` metadata (got {got!r}, expected it to contain {want!r}). "
    "Someone re-saved it with the CURRENT pyarrow, which makes this whole "
    "module meaningless: it would only prove that the current writer can "
    "read its own output. Restore the committed fixture, or regenerate it "
    "with the old writer (see this module's docstring)."
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """DATA_ROOT on tmp_path only - never ./client_data, never the volume."""
    monkeypatch.setattr(local_store.settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    yield
    local_store._DATAFRAME_CACHE.invalidate()


def _created_by(path: Path) -> str:
    return str(pq.read_metadata(path).created_by)


def _assert_old_writer(path: Path) -> None:
    created_by = _created_by(path)
    assert OLD_WRITER_VERSION in created_by, REGENERATED_MSG.format(
        name=path.name, got=created_by, want=OLD_WRITER_VERSION)


def _db_entry(display_name: str, tid: str) -> dict:
    """A meta entry of the shape `db_entries_from_meta` selects and
    `_load_db_snapshots` reads: source "database", the table id under `db`,
    the DISPLAY NAME as the df key (`file_name`)."""
    return {
        "file_name": display_name,
        "file_description": "clients registered from the data-sources panel",
        "source": "database",
        "db": {
            "table_id": tid,
            "connection_id": "cc11cc11cc11cc11",
            "schema": "shop",
            "table_name": "clients",
            "display_name": display_name,
            "is_connector": False,
            "auto_included": False,
            "refreshed_at": "2026-01-01T00:00:00+00:00",
        },
        "schema": {"file_name": display_name, "fields": {}},
    }


def _chat_with(entries: list):
    store = local_store.ChatDataStore("c_pqcompat")
    meta = store.read_meta()
    meta["files"] = entries
    store.write_meta(meta)
    return store


def _install_cache_entry(source: Path, df_key: str, parser_version: int):
    """Put a copy of the old-writer cache fixture under `.parquet_cache/` with
    the manifest `_parquet_cache_read` expects for `source`."""
    cache_dir, manifest_path = local_store._parquet_cache_paths(source)
    cache_dir.mkdir(parents=True, exist_ok=True)
    pq_name = local_store._parquet_cache_safe_name(df_key) + ".parquet"
    shutil.copyfile(CACHE_FIXTURE, cache_dir / pq_name)
    st = source.stat()
    manifest = {
        "src_size": st.st_size,
        "src_mtime_ns": st.st_mtime_ns,
        "parser_version": parser_version,
        "entries": [{"key": df_key, "parquet": pq_name}],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return cache_dir, manifest_path


# ---------------------------------------------------------------------------
# The fixtures themselves
# ---------------------------------------------------------------------------

def test_fixtures_were_written_by_the_previous_writer_version():
    """Guard on the guard: both fixtures must still BE old-writer output."""
    for path in (SNAPSHOT_FIXTURE, CACHE_FIXTURE):
        assert path.exists(), f"missing committed fixture {path}"
        _assert_old_writer(path)


# ---------------------------------------------------------------------------
# Central database snapshot
# ---------------------------------------------------------------------------

def test_old_writer_db_snapshot_loads_with_dtypes_and_nulls_intact(tmp_path):
    """A db_snapshots/ parquet from the older writer still loads through the
    real chat load path with its columns, order, dtypes and null markers."""
    _assert_old_writer(SNAPSHOT_FIXTURE)

    dest = local_store.db_snapshot_path(TID)
    shutil.copyfile(SNAPSHOT_FIXTURE, dest)
    assert dest.parent == tmp_path / "db_snapshots", dest

    display_name = "clients information"
    store = _chat_with([_db_entry(display_name, TID)])
    dfs = store.load_dataframes()

    keys = list(dfs)
    assert keys == [display_name], keys

    df = dfs[display_name]
    columns = list(df.columns)
    assert columns == ["client_id", "city", "balance", "opened_at", "is_active"], columns

    n_rows = len(df)
    assert n_rows == 4, n_rows

    dtypes = {c: str(df[c].dtype) for c in columns}
    expected_dtypes = {
        "client_id": "int64",
        "city": "object",
        "balance": "float64",
        "opened_at": "datetime64[ns]",
        "is_active": "bool",
    }
    assert dtypes == expected_dtypes, dtypes

    null_counts = {c: int(df[c].isna().sum()) for c in columns}
    assert null_counts == {"client_id": 0, "city": 1, "balance": 1,
                           "opened_at": 1, "is_active": 0}, null_counts

    balance_nan_rows = df.index[df["balance"].isna()].tolist()
    assert balance_nan_rows == [1], balance_nan_rows
    opened_at_nat_rows = df.index[df["opened_at"].isna()].tolist()
    assert opened_at_nat_rows == [2], opened_at_nat_rows
    city_null_rows = df.index[df["city"].isna()].tolist()
    assert city_null_rows == [3], city_null_rows

    first_client_id = df["client_id"].iloc[0]
    assert first_client_id == 101, first_client_id
    first_city = df["city"].iloc[0]
    assert first_city == "Tbilisi", first_city
    first_balance = df["balance"].iloc[0]
    assert first_balance == pytest.approx(1500.50), first_balance
    first_opened_at = df["opened_at"].iloc[0]
    assert first_opened_at == pd.Timestamp("2024-01-15"), first_opened_at
    first_is_active = bool(df["is_active"].iloc[0])
    assert first_is_active is True, first_is_active


# ---------------------------------------------------------------------------
# Parsed-table parquet cache
# ---------------------------------------------------------------------------

def test_old_writer_parquet_cache_entry_is_served_without_reparsing():
    """A `.parquet_cache/` entry from the older writer is served as-is: the
    frame comes from the cached parquet, not from re-parsing the source."""
    _assert_old_writer(CACHE_FIXTURE)

    store = local_store.ChatDataStore("c_pqcompat")
    source = store.files_dir / "sales.csv"
    # Deliberately DIFFERENT columns from the cached parquet: were the loader
    # to re-parse the source, the assertions below would see these instead.
    source.write_text("reparsed_a,reparsed_b\n1,2\n", encoding="utf-8")
    _install_cache_entry(source, source.name,
                         local_store._PARQUET_CACHE_PARSER_VERSION)

    out = local_store._load_one_file_cached(source)

    keys = list(out)
    assert keys == [source.name], keys

    df = out[source.name]
    columns = list(df.columns)
    assert columns == ["Region", "Units", "Revenue", "Sold On"], columns
    assert "reparsed_a" not in columns, columns

    n_rows = len(df)
    assert n_rows == 3, n_rows

    dtypes = {c: str(df[c].dtype) for c in columns}
    expected_dtypes = {
        "Region": "object",
        "Units": "int64",
        "Revenue": "float64",
        "Sold On": "datetime64[ns]",
    }
    assert dtypes == expected_dtypes, dtypes

    revenue_nan_rows = df.index[df["Revenue"].isna()].tolist()
    assert revenue_nan_rows == [1], revenue_nan_rows

    regions = df["Region"].tolist()
    assert regions == ["North", "South", "North"], regions
    units = df["Units"].tolist()
    assert units == [10, 20, 30], units
    sold_on_first = df["Sold On"].iloc[0]
    assert sold_on_first == pd.Timestamp("2024-05-01"), sold_on_first


def test_old_writer_parquet_cache_is_invalidated_by_a_parser_version_bump():
    """Old-writer caches still SELF-HEAL: a manifest whose parser_version no
    longer matches is a miss, so the source file is re-parsed."""
    store = local_store.ChatDataStore("c_pqcompat")
    source = store.files_dir / "sales.csv"
    source.write_text("reparsed_a,reparsed_b\n1,2\n", encoding="utf-8")
    stale_version = local_store._PARQUET_CACHE_PARSER_VERSION + 1
    _install_cache_entry(source, source.name, stale_version)

    out = local_store._load_one_file_cached(source)

    keys = list(out)
    assert keys == [source.name], keys

    df = out[source.name]
    columns = list(df.columns)
    assert columns == ["reparsed_a", "reparsed_b"], columns
    assert "Region" not in columns, columns

    n_rows = len(df)
    assert n_rows == 1, n_rows

    row = df.iloc[0].tolist()
    assert row == [1, 2], row
