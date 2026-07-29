"""File-pipeline vs DB-pipeline parity (Article XIII): the SAME dataset loaded
once through the file-upload pipeline (CSV in a ChatDataStore) and once
through the DB-snapshot pipeline (sqlite via the real SQLAlchemy snapshot
path) must be analytically equivalent — standard dtypes on both sides and
identical results for a battery of representative generated-code operations.
Guards the class of bug behind the demo 3D-chart incident (categorical dtype
made a two-key groupby emit the cartesian product of ALL categories).
All offline; DATA_ROOT isolated to tmp_path."""
import json
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

import db_connector
import local_store
from exec_sanitizer import sanitize_for_execution

TID = "cc77dd88ee99ff00"

# Same rows as tools/fixtures/sample_sales.csv — binary-exact floats
# (.25/.50/.00) so float32-downcast snapshot values compare exactly.
ROWS = [
    (1, "c1", "North", "Widget A", 150.25, "2025-01-15"),
    (2, "c2", "South", "Widget B", 75.50, "2025-01-20"),
    (3, "c1", "East", "Widget C", 220.00, "2025-01-28"),
    (4, "c3", "North", "Widget A", 150.25, "2025-02-03"),
    (5, "c2", "South", "Widget B", 75.50, "2025-02-07"),
    (6, "c4", "West", "Widget A", 150.25, "2025-02-10"),
]
COLS = ["order_id", "customer_id", "region", "product", "revenue", "order_date"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store.settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    yield
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def df_file():
    store = local_store.ChatDataStore("c_parityfile")
    csv = ",".join(COLS) + "\n" + "\n".join(
        ",".join(str(v) for v in row) for row in ROWS)
    (store.files_dir / "sales.csv").write_text(csv, encoding="utf-8")
    return store.load_dataframes()["sales.csv"]


@pytest.fixture
def df_db(tmp_path):
    db = tmp_path / "parity.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE sales (order_id INTEGER, customer_id TEXT,
                                region TEXT, product TEXT, revenue REAL,
                                order_date TEXT)"""))
        for row in ROWS:
            conn.execute(text(
                "INSERT INTO sales VALUES (:a, :b, :c, :d, :e, :f)"),
                dict(zip("abcdef", row)))
    eng.dispose()
    cfg = {"db_type": "sqlite", "url_override": f"sqlite+pysqlite:///{db}"}
    res = db_connector.snapshot_table(
        cfg, "", schema=None, table="sales",
        dest=local_store.db_snapshot_path(TID), sid="t", chunk_rows=3)
    assert res["ok"] is True and res["rows"] == 6
    # Old-shape registry with the recorded plan (category entries included) —
    # the loader must ignore the category entries (regression-guarded
    # elsewhere; here it feeds the parity assertions).
    (Path(local_store.settings.DATA_ROOT) / "data_sources.json").write_text(
        json.dumps({"connections": [],
                    "tables": [{"id": TID, "dtype_plan": res["dtype_plan"]}]}),
        encoding="utf-8")
    store = local_store.ChatDataStore("c_paritydb")
    meta = store.read_meta()
    meta["files"] = [{
        "file_name": "sales", "file_description": "d", "source": "database",
        "db": {"table_id": TID, "connection_id": "cc11cc11cc11cc11",
               "schema": None, "table_name": "sales", "display_name": "sales",
               "is_connector": False, "auto_included": False,
               "refreshed_at": "2026-07-29T00:00:00+00:00"},
        "schema": {"file_name": "sales", "fields": {}},
    }]
    store.write_meta(meta)
    return store.load_dataframes()["sales"]


def test_both_pipelines_standard_and_gate_identity(df_file, df_db):
    for df in (df_file, df_db):
        assert list(df.columns) == COLS
        assert len(df) == 6
        dfs = {"sales": df}
        assert sanitize_for_execution(dfs, "t") is dfs  # already clean
    # Both pipelines serve dates as object strings (read_csv without
    # parse_dates / sqlite TEXT) — the battery uses explicit to_datetime.
    assert str(df_file["order_date"].dtype) == "object"
    assert str(df_db["order_date"].dtype) == "object"


def test_two_key_groupby_observed_only(df_file, df_db):
    def battery(df):
        g = (df.groupby(["region", "product"], as_index=False)["revenue"]
               .agg(["sum", "count"])
               .sort_values(["region", "product"])
               .reset_index(drop=True))
        return g
    g_file, g_db = battery(df_file), battery(df_db)
    # 4 observed combos — a categorical regression would yield 12 (4x3).
    assert len(g_file) == 4 and len(g_db) == 4
    pd.testing.assert_frame_equal(g_file, g_db, check_dtype=False)


def test_merge_then_group(df_file, df_db):
    zones = pd.DataFrame({"region": ["North", "South", "East", "West"],
                          "zone": ["NS", "NS", "EW", "EW"]})
    def battery(df):
        return (df.merge(zones, on="region")
                  .groupby("zone")["revenue"].sum().to_dict())
    expect = {"NS": 451.50, "EW": 370.25}
    assert battery(df_file) == pytest.approx(expect)
    assert battery(df_db) == pytest.approx(expect)


def test_dt_accessor_month_split(df_file, df_db):
    def battery(df):
        return {int(k): int(v) for k, v in
                pd.to_datetime(df["order_date"]).dt.month
                  .value_counts().to_dict().items()}
    assert battery(df_file) == {1: 3, 2: 3}
    assert battery(df_db) == {1: 3, 2: 3}


def test_value_counts(df_file, df_db):
    expect = {"Widget A": 3, "Widget B": 2, "Widget C": 1}
    assert df_file["product"].value_counts().to_dict() == expect
    assert df_db["product"].value_counts().to_dict() == expect


def test_filter_then_group(df_file, df_db):
    def battery(df):
        return (df[df["revenue"] > 100]
                .groupby("region")["revenue"].sum().to_dict())
    expect = {"North": 300.50, "East": 220.00, "West": 150.25}
    assert battery(df_file) == pytest.approx(expect)
    assert battery(df_db) == pytest.approx(expect)


def test_numeric_width_divergence_documented(df_file, df_db):
    """Snapshot writer downcasts numerics (int8/float32) while the CSV parse
    yields int64/float64 — the reason the frame comparison above needs
    check_dtype=False. If this stops holding, revisit the parity battery."""
    assert df_db["order_id"].dtype.kind == df_file["order_id"].dtype.kind == "i"
    assert df_db["order_id"].dtype.itemsize <= df_file["order_id"].dtype.itemsize
    assert df_db["revenue"].dtype.kind == df_file["revenue"].dtype.kind == "f"
    assert df_db["revenue"].dtype.itemsize <= df_file["revenue"].dtype.itemsize
