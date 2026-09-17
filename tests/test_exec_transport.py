"""`exec_transport`: the shared serialization module between the main app and
the `pdc-executor` container (devbox unit tests, pure pandas/pyarrow, Python
3.11-compatible).

Coverage:
  * job-id regex + `create_job_dir` modes (mode bits asserted on POSIX only);
  * the input dtype/shape matrix → parquet entries that read back `equals`
    with identical labels, the two pickle fallbacks (mixed int/str object
    column, list cells), dict ORDER preserved end to end, categorical input
    sanitized, hostile manifest paths refused;
  * every `result` kind round-tripped through the wire dialect, with the
    three normalizations parity-tested against
    `run_chat_local._normalize_df_for_table`;
  * the `_safe_preview` baseline pins — they describe today's in-process
    behaviour and are the base of the wire-parity test;
  * preview WIRE PARITY: `json.dumps(_safe_preview(decode(encode(p))))` ==
    `json.dumps(_safe_preview(p))` over the whole matrix, NaN kept as NaN;
  * absence preservation, big-string refs, hostile refs, the parquet
    bomb guard, the status → error-text mapping for both kinds, unknown keys
    dropped, `error` truncation, caps parity, `dumps`/`loads` NaN/Inf.

Every value under test is bound to a local first so a failure prints it.
"""
import base64
import json
import math
import os
import stat
import threading
import warnings
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

import code_exec
import exec_transport
import run_chat_local
from run_chat_local import (
    _build_table_from_result,
    _build_tables_from_result,
    _normalize_df_for_table,
    _safe_preview,
    _styler_to_html,
)

ROOT = Path(__file__).resolve().parent.parent
POSIX = os.name == "posix"

TIMEOUT_TEXT = "TimeoutError: Code execution exceeded {timeout} seconds limit"
MEMORY_TEXT = "MemoryError: execution exceeded the memory limit"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _sales() -> pd.DataFrame:
    return pd.DataFrame({
        "a": [1, 2, 3],
        "b": [1.5, np.nan, 3.5],
        "c": ["x", "y", "z"],
    })


def _response(payload, kind="PYTHON", status="ok", **extra) -> dict:
    """The executor's HTTP body shape (`executor/app.py`'s `_run_job`)."""
    body = {
        "status": status,
        "kind": kind,
        "payload": payload,
        "elapsed_ms": 12,
        "peak_rss_mb": 100.0,
        "stdout": "",
        "stderr": "",
        "traceback": "",
        "exit_code": 0,
        "signal": None,
        "reason": None,
    }
    body.update(extra)
    return body


def _wire(obj):
    """One JSON hop in the module's own dialect (NaN/Infinity tokens survive)."""
    return exec_transport.loads(exec_transport.dumps(obj))


def _roundtrip(exec_return: dict, job_dir: Path, kind: str = "PYTHON", timeout_s=60) -> dict:
    """runner side (`serialize_result`) → wire → main-app side (`deserialize_result`)."""
    payload = exec_transport.serialize_result(exec_return, job_dir)
    response = _response(_wire(payload), kind=kind)
    return exec_transport.deserialize_result(response, job_dir, kind, timeout_s)


def _python_return(result, preview=None, image_base64=None) -> dict:
    """`code_exec.safe_execute`'s success shape (error: None, no `ok`)."""
    return {"error": None, "result": result, "preview": preview, "image_base64": image_base64}


def _same_table(built, expected) -> bool:
    """Deep equality for a `_build_table_from_result` / `_build_tables_from_result`
    output, with NaN matching NaN.

    Plain `==` cannot express "the table builder sees the same frame": the row
    dicts come from `to_dict(orient="records")`, which boxes every float into a
    FRESH object, and `float("nan") != float("nan")` — so
    `_build_table_from_result(df) == _build_table_from_result(df)` is already
    False for the same frame whenever it holds a NaN, no matter what
    `exec_transport` does. Everything else (column list, row order, row keys,
    total_rows, styled_html) is still compared exactly.
    """
    if isinstance(built, float) and isinstance(expected, float):
        return built == expected or (math.isnan(built) and math.isnan(expected))
    if isinstance(built, dict) and isinstance(expected, dict):
        return (list(built) == list(expected)
                and all(_same_table(built[k], expected[k]) for k in built))
    if isinstance(built, list) and isinstance(expected, list):
        return (len(built) == len(expected)
                and all(_same_table(b, e) for b, e in zip(built, expected)))
    return type(built) is type(expected) and built == expected


def _error_text(decoded: dict, kind: str) -> str:
    """The exec-error text for either caller shape."""
    assert isinstance(decoded, dict), decoded
    if kind == "PLOT":
        ok = decoded.get("ok")
        assert ok is False, decoded
        trace = decoded.get("trace")
        assert trace == "", decoded
    err = decoded.get("error")
    assert isinstance(err, str), decoded
    return err


@pytest.fixture
def job_dir(tmp_path) -> Path:
    return exec_transport.create_job_dir(tmp_path, exec_transport.new_job_id())


# ---------------------------------------------------------------------------
# job ids / dirs
# ---------------------------------------------------------------------------
def test_new_job_id_matches_regex_and_valid_job_id_rejects_hostile_ids():
    jid = exec_transport.new_job_id()
    assert exec_transport.JOB_ID_RE.match(jid), jid
    assert exec_transport.valid_job_id(jid) is True
    assert len(jid) == 32, jid
    assert exec_transport.JOB_ID_RE.pattern == r"^[0-9a-f]{32}$", exec_transport.JOB_ID_RE.pattern
    for bad in ("", "..", "../etc", "/" + "0" * 31, "0" * 31, "0" * 33,
                "A" * 32, "0" * 32 + "\n", "0" * 31 + "g", "x/" + "0" * 30):
        verdict = exec_transport.valid_job_id(bad)
        assert verdict is False, (bad, verdict)


def test_create_job_dir_creates_job_and_in_dirs_with_setgid_group_mode(tmp_path):
    jid = exec_transport.new_job_id()
    job_dir = exec_transport.create_job_dir(tmp_path, jid)
    assert job_dir == tmp_path / jid, job_dir
    assert job_dir.is_dir(), job_dir
    in_dir = job_dir / "in"
    assert in_dir.is_dir(), in_dir
    if POSIX:
        # 0o2770: setgid + rwx for owner and group, nothing for others — an
        # explicit chmod so the creator's umask (0022 in the main app) is
        # irrelevant, and group-write on in/ so the executor can rmtree an orphan.
        job_mode = os.stat(job_dir).st_mode & 0o7777
        in_mode = os.stat(in_dir).st_mode & 0o7777
        assert job_mode == 0o2770, oct(job_mode)
        assert in_mode == 0o2770, oct(in_mode)


@pytest.mark.parametrize("bad", ["", "..", "0" * 31, "A" * 32, "../" + "0" * 32, "0" * 32 + "/x"])
def test_create_job_dir_rejects_invalid_job_id(tmp_path, bad):
    with pytest.raises(ValueError):
        exec_transport.create_job_dir(tmp_path, bad)
    leaked = sorted(p.name for p in tmp_path.iterdir())
    assert leaked == [], leaked


# ---------------------------------------------------------------------------
# inputs: the dtype/shape matrix
# ---------------------------------------------------------------------------
def _parquet_matrix() -> dict:
    tz = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    return {
        "scalars": pd.DataFrame({"i": [1, 2, 3], "f": [1.5, np.nan, 2.5],
                                 "b": [True, False, True], "s": ["x", "y", "z"]}),
        "datetime_nat": pd.DataFrame({"d": pd.to_datetime(["2024-01-01", None, "2024-03-01"])}),
        "tz_aware": pd.DataFrame({"d": tz}),
        "timedelta": pd.DataFrame({"t": pd.to_timedelta([1, 2, 3], unit="s")}),
        "int_labels": pd.DataFrame({1: [1, 2], 2: [3, 4]}),
        "multiindex_columns": pd.DataFrame(
            [[1, 2], [3, 4]], columns=pd.MultiIndex.from_tuples([("x", "a"), ("x", "b")])),
        "multiindex_rows": pd.DataFrame(
            {"v": [1, 2, 3]},
            index=pd.MultiIndex.from_tuples([("r", 1), ("r", 2), ("s", 1)], names=["g", "n"])),
        "datetime_index": pd.DataFrame({"v": [1, 2, 3]},
                                       index=pd.date_range("2024-01-01", periods=3)),
        "named_object_index": pd.DataFrame({"v": [1, 2]}, index=pd.Index(["Tbilisi", "Batumi"], name="city")),
        "filtered_int_index": pd.DataFrame({"v": [10, 30]}, index=pd.Index([0, 2])),
        "bool_nan_object": pd.DataFrame({"b": pd.Series([True, np.nan, False], dtype="object")}),
        "decimal_object": pd.DataFrame({"d": [Decimal("1.10"), Decimal("2.20")]}),
        "empty_int": pd.DataFrame({"i": pd.Series([], dtype="int64")}),
        "unnamed_series_frame": pd.Series([1, 2, 3]).to_frame(),
        "named_series_named_index": pd.Series([1.0, 2.0], index=pd.Index(["a", "b"], name="k"), name="val").to_frame(),
    }


@pytest.mark.parametrize("case", sorted(_parquet_matrix()))
def test_write_inputs_parquet_entries_read_back_equal_with_identical_labels(job_dir, case):
    df = _parquet_matrix()[case]
    manifest = exec_transport.write_inputs({"t": df}, job_dir, sid="test")
    assert isinstance(manifest, list) and len(manifest) == 1, manifest
    entry = manifest[0]
    fmt = entry.get("format")
    assert fmt == "parquet", entry
    assert entry.get("name") == "t", entry
    path = entry.get("path")
    assert path == "in/0.parquet", entry
    assert (job_dir / path).is_file(), path
    back = exec_transport.read_inputs(manifest, job_dir)
    assert list(back) == ["t"], list(back)
    equal = back["t"].equals(df)
    assert equal, (case, back["t"].dtypes.to_dict(), df.dtypes.to_dict())
    labels = list(back["t"].columns)
    assert labels == list(df.columns), (labels, list(df.columns))
    index_equal = back["t"].index.equals(df.index)
    assert index_equal, (back["t"].index, df.index)


def test_write_inputs_falls_back_to_pickle_for_mixed_object_column_and_list_cells(job_dir):
    mixed = pd.DataFrame({"m": [1, "a", 3], "x": [1, 2, 3]})
    lists = pd.DataFrame({"l": [[1, 2], [3, 4]]})
    manifest = exec_transport.write_inputs({"mixed": mixed, "lists": lists}, job_dir, sid="test")
    formats = [e.get("format") for e in manifest]
    assert formats == ["pickle", "pickle"], manifest
    paths = [e.get("path") for e in manifest]
    assert paths == ["in/0.pkl", "in/1.pkl"], manifest
    back = exec_transport.read_inputs(manifest, job_dir)
    assert back["mixed"].equals(mixed), back["mixed"]
    assert list(back["mixed"]["m"]) == [1, "a", 3], list(back["mixed"]["m"])
    cells = list(back["lists"]["l"])
    assert cells == [[1, 2], [3, 4]], cells
    assert all(isinstance(c, list) for c in cells), [type(c) for c in cells]


def test_write_inputs_preserves_dict_order_end_to_end(job_dir):
    """`code_exec` aliases the FIRST frame as `df` — the manifest and the
    rebuilt dict must both keep insertion order, not sort by name."""
    dfs = {"zeta": _sales(), "alpha": _sales().head(1), "mid": _sales().head(2)}
    manifest = exec_transport.write_inputs(dfs, job_dir, sid="test")
    names = [e.get("name") for e in manifest]
    assert names == ["zeta", "alpha", "mid"], names
    wired = _wire(manifest)
    back = exec_transport.read_inputs(wired, job_dir)
    assert list(back) == ["zeta", "alpha", "mid"], list(back)
    first = next(iter(back))
    assert first == next(iter(dfs)), first
    assert back["zeta"].equals(dfs["zeta"])


def test_write_inputs_applies_the_article_xiii_sanitize_gate(job_dir):
    cat = pd.DataFrame({"c": pd.Categorical(["a", "b", "a"]), "v": [1, 2, 3]})
    manifest = exec_transport.write_inputs({"cat": cat}, job_dir, sid="test")
    back = exec_transport.read_inputs(manifest, job_dir)
    dtype = back["cat"]["c"].dtype
    assert not isinstance(dtype, pd.CategoricalDtype), dtype
    assert dtype == object, dtype
    assert list(back["cat"]["c"]) == ["a", "b", "a"]
    # the caller's frame is never mutated by the gate
    assert isinstance(cat["c"].dtype, pd.CategoricalDtype)


@pytest.mark.parametrize("path", [
    "in/../job.json",
    "../in/0.parquet",
    "/etc/passwd",
    "out/0.parquet",
    "in/sub/0.parquet",
    "in/0.parquet\n",
    "in/",
    "0.parquet",
])
def test_read_inputs_refuses_hostile_manifest_paths(job_dir, path):
    exec_transport.write_inputs({"t": _sales()}, job_dir, sid="test")
    manifest = [{"name": "t", "path": path, "format": "parquet"}]
    with pytest.raises(ValueError):
        exec_transport.read_inputs(manifest, job_dir)


def test_read_inputs_refuses_unknown_format_directory_and_missing_file(job_dir):
    exec_transport.write_inputs({"t": _sales()}, job_dir, sid="test")
    with pytest.raises(ValueError):
        exec_transport.read_inputs([{"name": "t", "path": "in/0.parquet", "format": "json"}], job_dir)
    (job_dir / "in" / "dir.parquet").mkdir()
    with pytest.raises(ValueError):
        exec_transport.read_inputs([{"name": "t", "path": "in/dir.parquet", "format": "parquet"}], job_dir)
    with pytest.raises(ValueError):
        exec_transport.read_inputs([{"name": "t", "path": "in/missing.parquet", "format": "parquet"}], job_dir)


@pytest.mark.skipif(not POSIX, reason="symlinks need POSIX")
def test_read_inputs_refuses_symlinked_input(job_dir, tmp_path):
    outside = tmp_path / "outside.parquet"
    _sales().to_parquet(outside, engine="pyarrow", compression=None)
    os.symlink(outside, job_dir / "in" / "0.parquet")
    with pytest.raises(ValueError):
        exec_transport.read_inputs([{"name": "t", "path": "in/0.parquet", "format": "parquet"}], job_dir)


# ---------------------------------------------------------------------------
# result kinds
# ---------------------------------------------------------------------------
def test_result_none_round_trips_with_error_none_and_no_ok_key(job_dir):
    decoded = _roundtrip(_python_return(None), job_dir)
    assert decoded["result"] is None, decoded
    assert "error" in decoded and decoded["error"] is None, decoded
    assert "ok" not in decoded, decoded
    assert decoded["preview"] is None, decoded
    assert decoded["image_base64"] is None, decoded


@pytest.mark.parametrize("value,expected_type,expected", [
    (np.int64(3), int, 3),
    (np.float64(1.5), float, 1.5),
    (np.bool_(True), bool, True),
    ("text", str, "text"),
    (7, int, 7),
    (2.25, float, 2.25),
    (False, bool, False),
])
def test_result_scalars_become_native(job_dir, value, expected_type, expected):
    decoded = _roundtrip(_python_return(value, preview=value), job_dir)
    result = decoded["result"]
    assert type(result) is expected_type, (type(result), result)
    assert result == expected, result


def test_result_list_of_json_values_round_trips(job_dir):
    value = [1, "a", 2.5, None, True, [1, 2], {"k": "v"}]
    decoded = _roundtrip(_python_return(value), job_dir)
    assert decoded["result"] == value, decoded["result"]


def test_result_list_with_timestamp_becomes_opaque(job_dir):
    value = [1, pd.Timestamp("2024-01-01")]
    decoded = _roundtrip(_python_return(value), job_dir)
    result = decoded["result"]
    assert isinstance(result, exec_transport.Opaque), result
    assert result.type_name == "list", result.type_name
    assert _build_table_from_result(result) is None
    assert _safe_preview(result) is None


def test_result_dict_of_numpy_scalars_stays_a_dict_with_native_values(job_dir):
    """tools/canary_check.py needs `{"n": np.int64(3)}` to stay a dict."""
    value = {"n": np.int64(3), "m": np.float64(1.5)}
    decoded = _roundtrip(_python_return(value), job_dir)
    result = decoded["result"]
    assert isinstance(result, dict), result
    assert result == {"n": 3, "m": 1.5}, result
    assert type(result["n"]) is int and type(result["m"]) is float, result


def test_result_dict_of_scalars_restores_typed_keys_and_stringifies_tuple_keys(job_dir):
    value = {1: 5, 2.5: "x", False: None, ("a", "b"): 3, "s": 7}
    decoded = _roundtrip(_python_return(value), job_dir)
    result = decoded["result"]
    assert isinstance(result, dict), result
    keys = list(result)
    assert keys == [1, 2.5, False, "('a', 'b')", "s"], keys
    assert [type(k) for k in keys] == [int, float, bool, str, str], [type(k) for k in keys]
    assert result == {1: 5, 2.5: "x", False: None, "('a', 'b')": 3, "s": 7}, result


def _frame_matrix() -> dict:
    return {
        "plain": _sales(),
        "multiindex_rows": pd.DataFrame(
            {"v": [1, 2, 3]},
            index=pd.MultiIndex.from_tuples([("r", 1), ("r", 2), ("s", 1)], names=["g", "n"])),
        "multiindex_columns": pd.DataFrame(
            [[1, 2], [3, 4]], columns=pd.MultiIndex.from_tuples([("x", "a"), ("x", "b")])),
        "named_index": pd.DataFrame({"v": [1, 2]}, index=pd.Index(["Tbilisi", "Batumi"], name="city")),
        "filtered_index": _sales().iloc[[0, 2]],
        "datetime_index": pd.DataFrame({"v": [1, 2, 3]}, index=pd.date_range("2024-01-01", periods=3)),
        "tz_aware": pd.DataFrame({"d": pd.date_range("2024-01-01", periods=2, tz="UTC")}),
        "empty": pd.DataFrame({"i": pd.Series([], dtype="int64")}),
    }


@pytest.mark.parametrize("case", sorted(_frame_matrix()))
def test_result_dataframe_round_trips(job_dir, case):
    df = _frame_matrix()[case]
    decoded = _roundtrip(_python_return(df), job_dir)
    result = decoded["result"]
    assert isinstance(result, pd.DataFrame), type(result)
    assert result.equals(df), (case, result, df)
    assert list(result.columns) == list(df.columns), list(result.columns)
    assert result.index.equals(df.index), (result.index, df.index)
    assert (job_dir / "out" / "result.parquet").is_file()
    # the table builder sees the same frame the in-process path sees
    assert _same_table(_build_table_from_result(result), _build_table_from_result(df))


def test_dedupe_columns_matches_normalize_df_for_table_and_is_identity_when_unique():
    dup = pd.DataFrame([[1, 2, 3]], columns=["a", "a", "b"])
    expected = list(_normalize_df_for_table(dup).columns)
    assert expected == ["a", "a.1", "b"], expected
    got = list(exec_transport.dedupe_columns(dup).columns)
    assert got == expected, got
    mi = pd.DataFrame([[1, 2]], columns=pd.MultiIndex.from_tuples([("x", "y"), ("x", "y")]))
    expected_mi = list(_normalize_df_for_table(mi).columns)
    assert expected_mi == ["x / y", "x / y.1"], expected_mi
    got_mi = list(exec_transport.dedupe_columns(mi).columns)
    assert got_mi == expected_mi, got_mi
    unique = _sales()
    assert exec_transport.dedupe_columns(unique) is unique
    # the caller's frame is never mutated
    assert list(dup.columns) == ["a", "a", "b"]


def test_result_duplicate_columns_are_deduped_identically_to_the_table_path(job_dir):
    dup = pd.DataFrame([[1, 2, 3], [4, 5, 6]], columns=["a", "a", "b"])
    decoded = _roundtrip(_python_return(dup), job_dir)
    result = decoded["result"]
    assert isinstance(result, pd.DataFrame), type(result)
    expected = _normalize_df_for_table(dup)
    assert list(result.columns) == list(expected.columns), list(result.columns)
    assert result.equals(expected), result
    assert _build_table_from_result(result) == _build_table_from_result(dup)


def test_result_mixed_object_column_becomes_str_with_nan_kept(job_dir):
    df = pd.DataFrame({"m": [1, "a", np.nan, None], "v": [1, 2, 3, 4]})
    decoded = _roundtrip(_python_return(df), job_dir)
    result = decoded["result"]
    assert isinstance(result, pd.DataFrame), type(result)
    col = result["m"]
    assert col.iloc[0] == "1", col.iloc[0]
    assert col.iloc[1] == "a", col.iloc[1]
    assert pd.isna(col.iloc[2]), col.iloc[2]
    assert pd.isna(col.iloc[3]), col.iloc[3]
    assert list(result["v"]) == [1, 2, 3, 4]


def test_result_mixed_type_column_labels_cast_to_str_without_pyarrow_warning(job_dir):
    ct = pd.crosstab(pd.Series(["x", "y", "x"], name="k"), pd.Series([1, 2, 1], name="n"), margins=True)
    assert list(ct.columns) == [1, 2, "All"], list(ct.columns)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        payload = exec_transport.serialize_result(_python_return(ct), job_dir)
    leaked = [str(w.message) for w in rec
              if issubclass(w.category, UserWarning) and "mixed type" in str(w.message)]
    assert leaked == [], leaked
    decoded = exec_transport.deserialize_result(_response(_wire(payload)), job_dir, "PYTHON", 60)
    result = decoded["result"]
    assert isinstance(result, pd.DataFrame), type(result)
    assert list(result.columns) == ["1", "2", "All"], list(result.columns)
    assert result.index.equals(ct.index), result.index
    assert (result.values == ct.values).all()


def test_result_int_column_labels_are_untouched(job_dir):
    ints = pd.DataFrame({1: [1, 2], 2: [3, 4]})
    decoded = _roundtrip(_python_return(ints), job_dir)
    assert list(decoded["result"].columns) == [1, 2], list(decoded["result"].columns)
    assert decoded["result"].equals(ints)


def test_result_series_named_and_unnamed(job_dir):
    named = pd.Series([1.0, 2.0], index=pd.Index(["a", "b"], name="k"), name="val")
    decoded = _roundtrip(_python_return(named), job_dir)
    result = decoded["result"]
    assert isinstance(result, pd.Series), type(result)
    assert result.equals(named), result
    assert result.name == "val", result.name
    assert result.index.name == "k", result.index.name
    assert _build_table_from_result(result) == _build_table_from_result(named)

    unnamed = pd.Series([1, 2, 3])
    decoded = _roundtrip(_python_return(unnamed), job_dir)
    result = decoded["result"]
    assert isinstance(result, pd.Series), type(result)
    assert result.name is None, result.name
    assert result.equals(unnamed), result
    table = _build_table_from_result(result)
    assert table == _build_table_from_result(unnamed), table
    assert table["columns"] == ["index", 0], table["columns"]


def test_result_styler_html_is_byte_identical_to_that_object(job_dir):
    """ONE Styler object: every `.style` access carries a fresh uuid, so the
    comparison must be against the same object's own `to_html()`."""
    df = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [3.0, 2.0, 1.0]})
    sty = df.style.background_gradient(cmap="Blues").format("{:.2f}")
    expected_html = sty.to_html()
    decoded = _roundtrip(_python_return(sty), job_dir)
    result = decoded["result"]
    assert isinstance(result, exec_transport.StyledFrame), type(result)
    assert isinstance(result.data, pd.DataFrame), type(result.data)
    assert result.data.equals(df), result.data
    html = result.to_html()
    assert html == expected_html
    assert _styler_to_html(result) == _styler_to_html(sty)
    table = _build_table_from_result(result)
    expected_table = _build_table_from_result(sty)
    assert table == expected_table, table
    assert table.get("styled_html") == expected_table.get("styled_html")


def test_result_styler_over_cap_ships_no_html_but_keeps_the_rows(job_dir):
    df = pd.DataFrame({"a": np.arange(201, dtype=float)})
    assert len(df) == exec_transport.STYLED_MAX_ROWS + 1
    sty = df.style.background_gradient(cmap="Blues")
    decoded = _roundtrip(_python_return(sty), job_dir)
    result = decoded["result"]
    assert isinstance(result, exec_transport.StyledFrame), type(result)
    html = result.to_html()
    assert html == "", html[:80]
    assert _styler_to_html(result) is None
    table = _build_table_from_result(result)
    assert table is not None
    assert table["total_rows"] == 201, table["total_rows"]
    assert "styled_html" not in table, sorted(table)
    assert result.data.equals(df)


def test_styled_frame_to_html_returns_empty_string_when_html_is_none():
    sf = exec_transport.StyledFrame(_sales(), None)
    assert sf.to_html() == ""
    assert sf.data.equals(_sales())
    sf2 = exec_transport.StyledFrame(_sales(), "<table></table>")
    assert sf2.to_html() == "<table></table>"


def test_result_dict_of_frames_with_per_entry_styler_html(job_dir):
    df1 = _sales()
    df2 = pd.DataFrame({"q": [1.0, 2.0], "r": [2.0, 1.0]})
    sty2 = df2.style.background_gradient(cmap="Blues").format("{:.1f}")
    value = {"Sales first 10": df1, "Stock first 10": sty2}
    expected_tables = _build_tables_from_result(value)
    decoded = _roundtrip(_python_return(value), job_dir)
    result = decoded["result"]
    assert isinstance(result, dict), type(result)
    assert list(result) == ["Sales first 10", "Stock first 10"], list(result)
    assert isinstance(result["Sales first 10"], pd.DataFrame)
    assert result["Sales first 10"].equals(df1)
    styled = result["Stock first 10"]
    assert isinstance(styled, exec_transport.StyledFrame), type(styled)
    assert styled.data.equals(df2)
    assert styled.to_html() == sty2.to_html()
    tables = _build_tables_from_result(result)
    assert _same_table(tables, expected_tables), tables
    assert [t["title"] for t in tables] == ["Sales first 10", "Stock first 10"]
    assert "styled_html" in tables[1] and "styled_html" not in tables[0], tables


def test_result_mixed_dict_of_scalars_frames_lists_and_opaque(job_dir):
    df = _sales()
    value = {"count": np.int64(3), "table": df, "labels": ["a", "b"],
             "when": pd.Timestamp("2024-01-01"), "series": pd.Series([1, 2], name="s")}
    decoded = _roundtrip(_python_return(value), job_dir)
    result = decoded["result"]
    assert isinstance(result, dict), type(result)
    assert list(result) == ["count", "table", "labels", "when", "series"], list(result)
    assert result["count"] == 3 and type(result["count"]) is int, result["count"]
    assert isinstance(result["table"], pd.DataFrame) and result["table"].equals(df)
    assert result["labels"] == ["a", "b"], result["labels"]
    assert isinstance(result["when"], exec_transport.Opaque), result["when"]
    assert isinstance(result["series"], pd.Series) and result["series"].equals(pd.Series([1, 2], name="s"))
    # the multi-table builder sees exactly the same tables as today
    assert _same_table(_build_tables_from_result(result), _build_tables_from_result(value))


def test_result_figure_and_ndarray_become_opaque_with_type_name_only(job_dir):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    fig = Figure()
    decoded = _roundtrip(_python_return(fig), job_dir)
    result = decoded["result"]
    assert isinstance(result, exec_transport.Opaque), type(result)
    assert result.type_name == "Figure", result.type_name
    assert _build_table_from_result(result) is None
    assert _build_tables_from_result(result) is None
    assert _safe_preview(result) is None
    payload = exec_transport.serialize_result(_python_return(np.arange(4)), job_dir)
    assert payload["result"] == {"kind": "opaque", "type_name": "ndarray"}, payload["result"]
    decoded = exec_transport.deserialize_result(_response(_wire(payload)), job_dir, "PYTHON", 60)
    assert isinstance(decoded["result"], exec_transport.Opaque)
    assert decoded["result"].type_name == "ndarray"
    assert _build_table_from_result(decoded["result"]) is None


# ---------------------------------------------------------------------------
# `_safe_preview` baseline pins — today's in-process behaviour
# ---------------------------------------------------------------------------
def test_safe_preview_rejects_numpy_int_key():
    assert _safe_preview({np.int64(2023): 5}) is None


def test_safe_preview_rejects_timestamp_key():
    assert _safe_preview({pd.Timestamp("2023-01-01"): 5}) is None


def test_safe_preview_accepts_plain_int_key():
    out = _safe_preview({2023: 5})
    assert out == {2023: 5}, out


def test_safe_preview_passes_nan_and_inf_through():
    """NaN/Inf pass the scalar allow-list untouched; `json.dumps` (allow_nan
    True by default, which is what httpx's `json=` uses today) renders them
    as the `NaN` / `Infinity` tokens — the wire bytes the brain receives."""
    out = _safe_preview({"a": float("nan"), "b": float("inf"), "c": np.float64("nan")})
    assert math.isnan(out["a"]), out
    assert math.isinf(out["b"]), out
    assert math.isnan(out["c"]), out
    assert out["a"] is not None
    assert json.dumps(out) == '{"a": NaN, "b": Infinity, "c": NaN}', json.dumps(out)


# ---------------------------------------------------------------------------
# preview wire parity
# ---------------------------------------------------------------------------
def _preview_matrix() -> dict:
    # EXCLUDED on purpose: the duplicate-key shape `{1: "a", "1": "b"}`. Today
    # httpx's `json=` puts TWO keys `"1"` on the wire (json.dumps does not
    # dedupe), and after transport the dict holds ONE `"1"` (last wins) — a
    # different byte sequence for a shape no consumer relies on. Recorded in
    # EXECUTOR_PROTOCOL.md; not a parity target.
    return {
        "none": None,
        "str": "hello",
        "int": 7,
        "float": 2.5,
        "bool": True,
        "np_float64_top": np.float64(1.25),
        "nan_value": {"a": float("nan")},
        "inf_value": {"a": float("inf"), "b": float("-inf")},
        "neg_zero": {"a": -0.0},
        "big_int": {"a": 10 ** 30},
        "np_int64_key": {np.int64(2023): 5},
        "timestamp_key": {pd.Timestamp("2023-01-01"): 5},
        "none_key_value": {"k": None},
        "int_key": {2023: 5},
        "float_key": {1.5: "x"},
        "bool_key": {True: "y"},
        "np_float64_nan": {"a": np.float64("nan")},
        "np_bool": {"a": np.bool_(True)},
        "np_datetime64_ns": {"a": np.datetime64("2024-01-01", "ns")},
        "np_datetime64_nat": {"a": np.datetime64("NaT")},
        "np_datetime64_day": {"a": np.datetime64("2024-01-01", "D")},
        "np_array_one": {"a": np.array([5])},
        "np_timedelta64_ns": {"a": np.timedelta64(5, "ns")},
        "np_timedelta64_s": {"a": np.timedelta64(5, "s")},
        "np_bytes": {"a": np.bytes_(b"x")},
        "np_complex": {"a": np.complex128(1 + 2j)},
        "pd_nat": {"a": pd.NaT},
        "pd_timestamp": {"a": pd.Timestamp("2024-01-01")},
        "pd_timedelta": {"a": pd.Timedelta("1s")},
        "pd_na": {"a": pd.NA},
        "decimal": {"a": Decimal("1.5")},
        "bytes": {"a": b"x"},
        "list_top": [1, 2, 3],
        "list_value": {"a": [1, 2]},
        "nested_dict": {"a": {"b": 1}},
        "records_list": [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}],
        "long_str": "x" * 4000,
        "long_str_value": {"a": "y" * 4000},
        "mixed_ok": {"n": np.int64(3), "m": np.float64(1.5), "s": "t", "f": False, "z": None},
        "unicode": {"ქალაქი": "თბილისი"},
    }


@pytest.mark.parametrize("case", sorted(_preview_matrix()))
def test_preview_wire_parity_with_safe_preview(case):
    p = _preview_matrix()[case]
    expected = json.dumps(_safe_preview(p))
    decoded = exec_transport.decode_preview(
        exec_transport.loads(exec_transport.dumps(exec_transport.encode_preview(p))))
    got = json.dumps(_safe_preview(decoded))
    assert got == expected, (case, got, expected)


def test_preview_nan_survives_as_nan_never_none():
    p = {"a": float("nan"), "b": np.float64("nan")}
    decoded = exec_transport.decode_preview(
        exec_transport.loads(exec_transport.dumps(exec_transport.encode_preview(p))))
    assert isinstance(decoded, dict), decoded
    assert decoded["a"] is not None
    assert math.isnan(decoded["a"]), decoded
    assert math.isnan(decoded["b"]), decoded
    encoded_text = exec_transport.dumps(exec_transport.encode_preview(p))
    assert "NaN" in encoded_text and "null" not in encoded_text, encoded_text


def test_preview_records_list_decodes_to_opaque_and_safe_preview_none():
    p = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    encoded = exec_transport.encode_preview(p)
    assert encoded == {"__opaque__": "list"}, encoded
    decoded = exec_transport.decode_preview(_wire(encoded))
    assert isinstance(decoded, exec_transport.Opaque), decoded
    assert decoded.type_name == "list", decoded.type_name
    assert _safe_preview(decoded) is None
    # no row value ever reaches the wire
    text = exec_transport.dumps(encoded)
    assert '"x"' not in text and '"a"' not in text, text


def test_preview_rejected_dict_ships_no_sibling_scalars():
    p = {"ok": 1, "rows": [1, 2, 3], "secret_value": "Tbilisi"}
    encoded = exec_transport.encode_preview(p)
    assert encoded == {"__opaque__": "dict"}, encoded
    text = exec_transport.dumps(encoded)
    assert "Tbilisi" not in text and "ok" not in text, text


def test_preview_top_level_numpy_float_is_inline_without_item_call():
    encoded = exec_transport.encode_preview(np.float64(1.25))
    assert encoded == 1.25 and isinstance(encoded, float), encoded


# ---------------------------------------------------------------------------
# absence preservation
# ---------------------------------------------------------------------------
def test_absence_preserved_for_matplotlib_multi_axes_error_shape(job_dir):
    shape = {
        "ok": False,
        "multi_axes": True,
        "error": "MultiAxesChartError: your code combined multiple plots in ONE figure",
        "trace": "",
    }
    decoded = _roundtrip(dict(shape), job_dir, kind="PLOT")
    assert decoded == shape, decoded
    assert "is_plotly" not in decoded, sorted(decoded)


def test_absence_preserved_for_plotly_multi_axes_error_shape(job_dir):
    shape = {"ok": False, "multi_axes": True, "is_plotly": True,
             "error": "MultiAxesChartError: plotly make_subplots", "trace": ""}
    decoded = _roundtrip(dict(shape), job_dir, kind="PLOT")
    assert decoded == shape, decoded


def test_python_success_has_error_none_and_no_ok(job_dir):
    decoded = _roundtrip(_python_return(42, preview=42), job_dir)
    assert sorted(decoded) == ["error", "image_base64", "preview", "result"], sorted(decoded)
    assert decoded["error"] is None


def test_plot_success_has_no_error_key(job_dir):
    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode("ascii")
    shape = {"ok": True, "image": png_b64, "is_plotly": False, "chart_data": {"x": [1, 2], "y": [3, 4]}}
    decoded = _roundtrip(dict(shape), job_dir, kind="PLOT")
    assert decoded == shape, decoded
    assert "error" not in decoded, sorted(decoded)


def test_plotly_success_shape_round_trips_inline(job_dir):
    html = "<div><script src=\"/static/vendor/plotly/plotly.min.js\"></script></div>"
    shape = {"ok": True, "plotly_html": html, "is_plotly": True, "chart_data": None}
    decoded = _roundtrip(dict(shape), job_dir, kind="PLOT")
    assert decoded == shape, decoded


def test_plot_exec_error_shape_round_trips(job_dir):
    shape = {"ok": False, "error": "NameError: name 'x' is not defined", "trace": "Traceback ..."}
    decoded = _roundtrip(dict(shape), job_dir, kind="PLOT")
    assert decoded == shape, decoded


def test_python_exec_error_shape_round_trips(job_dir):
    shape = {"error": "KeyError: 'missing'"}
    decoded = _roundtrip(dict(shape), job_dir)
    assert decoded == shape, decoded


# ---------------------------------------------------------------------------
# big strings → file refs
# ---------------------------------------------------------------------------
def test_png_over_inline_cap_goes_to_file_and_returns_identical_string(job_dir):
    raw = os.urandom(1_600_000)
    b64 = base64.b64encode(raw).decode("ascii")
    assert len(b64) > exec_transport.INLINE_MAX_BYTES
    payload = exec_transport.serialize_result(_python_return(None, image_base64=b64), job_dir)
    ref = payload["image_base64"]
    assert ref == {"ref": "out/figure.png", "encoding": "png-b64"}, ref
    stored = (job_dir / "out" / "figure.png").read_bytes()
    assert stored == raw
    decoded = exec_transport.deserialize_result(_response(_wire(payload)), job_dir, "PYTHON", 60)
    assert decoded["image_base64"] == b64


def test_small_png_stays_inline(job_dir):
    b64 = base64.b64encode(b"\x89PNG" + b"\x00" * 100).decode("ascii")
    payload = exec_transport.serialize_result(_python_return(None, image_base64=b64), job_dir)
    assert payload["image_base64"] == b64, payload["image_base64"]


def test_plotly_html_over_inline_cap_goes_to_file_utf8(job_dir, monkeypatch):
    monkeypatch.setattr(exec_transport, "INLINE_MAX_BYTES", 1000)
    html = "<div>" + "ქართული" * 400 + "</div>"
    assert len(html) > 1000
    shape = {"ok": True, "plotly_html": html, "is_plotly": True, "chart_data": None}
    payload = exec_transport.serialize_result(dict(shape), job_dir)
    ref = payload["plotly_html"]
    assert ref == {"ref": "out/chart.html", "encoding": "utf8"}, ref
    assert (job_dir / "out" / "chart.html").read_text(encoding="utf-8") == html
    decoded = exec_transport.deserialize_result(_response(_wire(payload), kind="PLOT"), job_dir, "PLOT", 60)
    assert decoded == shape, decoded


def test_plot_image_over_inline_cap_goes_to_file(job_dir, monkeypatch):
    monkeypatch.setattr(exec_transport, "INLINE_MAX_BYTES", 1000)
    raw = os.urandom(3000)
    b64 = base64.b64encode(raw).decode("ascii")
    shape = {"ok": True, "image": b64, "is_plotly": False, "chart_data": {"x": [1]}}
    payload = exec_transport.serialize_result(dict(shape), job_dir)
    assert payload["image"] == {"ref": "out/figure.png", "encoding": "png-b64"}, payload["image"]
    decoded = exec_transport.deserialize_result(_response(_wire(payload), kind="PLOT"), job_dir, "PLOT", 60)
    assert decoded == shape


def test_multi_charts_images_over_cap_go_to_numbered_files(job_dir, monkeypatch):
    monkeypatch.setattr(exec_transport, "INLINE_MAX_BYTES", 1000)
    small = base64.b64encode(b"\x89PNG" + b"\x01" * 10).decode("ascii")
    big0 = base64.b64encode(os.urandom(2000)).decode("ascii")
    big2 = base64.b64encode(os.urandom(2500)).decode("ascii")
    shape = {"ok": True, "is_plotly": False, "multi_charts": [
        {"image": big0, "is_plotly": False, "title": "first", "chart_data": {"x": [1]}},
        {"image": small, "is_plotly": False, "title": "second", "chart_data": None},
        {"image": big2, "is_plotly": False, "title": "third", "chart_data": {"y": [2.5]}},
    ]}
    payload = exec_transport.serialize_result(json.loads(json.dumps(shape)), job_dir)
    refs = [c["image"] for c in payload["multi_charts"]]
    assert isinstance(refs[0], dict) and refs[0]["encoding"] == "png-b64", refs
    assert refs[1] == small, refs
    assert isinstance(refs[2], dict) and refs[2]["encoding"] == "png-b64", refs
    assert refs[0]["ref"] != refs[2]["ref"], refs
    for r in (refs[0], refs[2]):
        assert r["ref"].startswith("out/chart_") and r["ref"].endswith(".png"), r
        assert (job_dir / r["ref"]).is_file(), r
    decoded = exec_transport.deserialize_result(_response(_wire(payload), kind="PLOT"), job_dir, "PLOT", 60)
    assert decoded == shape, decoded


def test_multi_charts_plotly_html_over_cap_goes_to_numbered_html_files(job_dir, monkeypatch):
    monkeypatch.setattr(exec_transport, "INLINE_MAX_BYTES", 1000)
    html = "<div>" + "p" * 2000 + "</div>"
    shape = {"ok": True, "is_plotly": True, "multi_charts": [
        {"image": html, "is_plotly": True, "title": "t", "chart_data": None},
    ]}
    payload = exec_transport.serialize_result(json.loads(json.dumps(shape)), job_dir)
    ref = payload["multi_charts"][0]["image"]
    assert isinstance(ref, dict) and ref["encoding"] == "utf8", ref
    assert ref["ref"].startswith("out/chart_") and ref["ref"].endswith(".html"), ref
    decoded = exec_transport.deserialize_result(_response(_wire(payload), kind="PLOT"), job_dir, "PLOT", 60)
    assert decoded == shape, decoded


# ---------------------------------------------------------------------------
# hostile refs
# ---------------------------------------------------------------------------
def _frame_payload(ref: str) -> dict:
    return {"error": None, "result": {"kind": "frame", "ref": ref}, "preview": None, "image_base64": None}


def _plot_payload(ref: str) -> dict:
    return {"ok": True, "image": {"ref": ref, "encoding": "png-b64"}, "is_plotly": False, "chart_data": None}


@pytest.mark.parametrize("ref", [
    "out/../job.json",
    "../out/result.parquet",
    "/etc/passwd",
    "out/sub/result.parquet",
    "out/result.pkl",
    "in/0.parquet",
    "out/result.parquet\n",
    "out/",
    "result.parquet",
])
@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
def test_hostile_result_and_image_refs_are_executor_response_errors(job_dir, ref, kind):
    (job_dir / "out").mkdir(exist_ok=True)
    (job_dir / "job.json").write_text("{}", encoding="utf-8")
    payload = _frame_payload(ref) if kind == "PYTHON" else _plot_payload(ref)
    decoded = exec_transport.deserialize_result(_response(payload, kind=kind), job_dir, kind, 60)
    err = _error_text(decoded, kind)
    assert err.startswith("ExecutorResponseError:"), err
    if kind == "PYTHON":
        assert decoded.get("result") is None, decoded


def test_pkl_ref_is_never_read_even_when_the_file_exists(job_dir):
    out = job_dir / "out"
    out.mkdir(exist_ok=True)
    _sales().to_pickle(out / "result.pkl")
    decoded = exec_transport.deserialize_result(_response(_frame_payload("out/result.pkl")), job_dir, "PYTHON", 60)
    err = _error_text(decoded, "PYTHON")
    assert err.startswith("ExecutorResponseError:"), err


def test_missing_ref_file_is_an_executor_response_error(job_dir):
    (job_dir / "out").mkdir(exist_ok=True)
    decoded = exec_transport.deserialize_result(_response(_frame_payload("out/result.parquet")), job_dir, "PYTHON", 60)
    err = _error_text(decoded, "PYTHON")
    assert err.startswith("ExecutorResponseError:"), err


@pytest.mark.skipif(not POSIX, reason="symlinks / FIFOs need POSIX")
def test_symlinked_ref_is_refused_on_posix(job_dir, tmp_path):
    outside = tmp_path / "outside.parquet"
    _sales().to_parquet(outside, engine="pyarrow", compression=None)
    out = job_dir / "out"
    out.mkdir(exist_ok=True)
    os.symlink(outside, out / "result.parquet")
    decoded = exec_transport.deserialize_result(_response(_frame_payload("out/result.parquet")), job_dir, "PYTHON", 60)
    err = _error_text(decoded, "PYTHON")
    assert err.startswith("ExecutorResponseError:"), err


@pytest.mark.skipif(not POSIX, reason="symlinks / FIFOs need POSIX")
def test_symlinked_out_directory_is_refused_on_posix(job_dir, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _sales().to_parquet(elsewhere / "result.parquet", engine="pyarrow", compression=None)
    os.symlink(elsewhere, job_dir / "out", target_is_directory=True)
    decoded = exec_transport.deserialize_result(_response(_frame_payload("out/result.parquet")), job_dir, "PYTHON", 60)
    err = _error_text(decoded, "PYTHON")
    assert err.startswith("ExecutorResponseError:"), err


@pytest.mark.skipif(not POSIX, reason="symlinks / FIFOs need POSIX")
def test_fifo_ref_is_refused_without_blocking_on_posix(job_dir):
    out = job_dir / "out"
    out.mkdir(exist_ok=True)
    os.mkfifo(out / "figure.png")
    holder = {}

    def _run():
        holder["decoded"] = exec_transport.deserialize_result(
            _response(_plot_payload("out/figure.png"), kind="PLOT"), job_dir, "PLOT", 60)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "deserialize_result blocked on a FIFO"
    err = _error_text(holder["decoded"], "PLOT")
    assert err.startswith("ExecutorResponseError:"), err


def test_oversized_png_ref_is_refused(job_dir, monkeypatch):
    monkeypatch.setattr(exec_transport, "REF_FILE_MAX_BYTES", 1024)
    out = job_dir / "out"
    out.mkdir(exist_ok=True)
    (out / "figure.png").write_bytes(b"\x89PNG" + b"\x00" * 4096)
    decoded = exec_transport.deserialize_result(_response(_plot_payload("out/figure.png"), kind="PLOT"), job_dir, "PLOT", 60)
    err = _error_text(decoded, "PLOT")
    assert err.startswith("ExecutorResponseError:"), err


# ---------------------------------------------------------------------------
# parquet bomb guard
# ---------------------------------------------------------------------------
def _write_result_parquet(job_dir: Path, df: pd.DataFrame, **kw) -> Path:
    out = job_dir / "out"
    out.mkdir(exist_ok=True)
    path = out / "result.parquet"
    kw.setdefault("compression", None)
    df.to_parquet(path, engine="pyarrow", **kw)
    return path


def test_compressed_result_parquet_is_result_too_large(job_dir):
    path = _write_result_parquet(job_dir, _sales(), compression="zstd")
    codec = pq.ParquetFile(path).metadata.row_group(0).column(0).compression
    assert codec == "ZSTD", codec
    decoded = exec_transport.deserialize_result(_response(_frame_payload("out/result.parquet")), job_dir, "PYTHON", 60)
    err = _error_text(decoded, "PYTHON")
    assert err.startswith("ResultTooLarge:"), err


def test_result_parquet_over_row_cap_is_result_too_large(job_dir, monkeypatch):
    monkeypatch.setattr(exec_transport, "RESULT_MAX_ROWS", 10)
    _write_result_parquet(job_dir, pd.DataFrame({"a": np.arange(50)}))
    decoded = exec_transport.deserialize_result(_response(_frame_payload("out/result.parquet")), job_dir, "PYTHON", 60)
    err = _error_text(decoded, "PYTHON")
    assert err.startswith("ResultTooLarge:"), err


def test_result_parquet_over_column_cap_is_result_too_large(job_dir, monkeypatch):
    monkeypatch.setattr(exec_transport, "RESULT_MAX_COLUMNS", 3)
    _write_result_parquet(job_dir, pd.DataFrame({f"c{i}": [1] for i in range(8)}))
    decoded = exec_transport.deserialize_result(_response(_frame_payload("out/result.parquet")), job_dir, "PYTHON", 60)
    err = _error_text(decoded, "PYTHON")
    assert err.startswith("ResultTooLarge:"), err


def test_result_parquet_over_file_cap_is_result_too_large(job_dir, monkeypatch):
    monkeypatch.setattr(exec_transport, "RESULT_FILE_MAX_BYTES", 2048)
    _write_result_parquet(job_dir, pd.DataFrame({"a": np.random.rand(5000)}))
    decoded = exec_transport.deserialize_result(_response(_frame_payload("out/result.parquet")), job_dir, "PYTHON", 60)
    err = _error_text(decoded, "PYTHON")
    assert err.startswith("ResultTooLarge:"), err


def test_result_parquet_over_decoded_cap_is_result_too_large(job_dir, monkeypatch):
    monkeypatch.setattr(exec_transport, "RESULT_MAX_DECODED_BYTES", 4096)
    _write_result_parquet(job_dir, pd.DataFrame({"a": np.random.rand(20000)}))
    decoded = exec_transport.deserialize_result(_response(_frame_payload("out/result.parquet")), job_dir, "PYTHON", 60)
    err = _error_text(decoded, "PYTHON")
    assert err.startswith("ResultTooLarge:"), err


def test_result_too_large_wraps_in_plot_shape_for_plot_kind(job_dir, monkeypatch):
    monkeypatch.setattr(exec_transport, "RESULT_MAX_ROWS", 1)
    _write_result_parquet(job_dir, _sales())
    payload = {"ok": True, "is_plotly": False, "image": None, "chart_data": None,
               "result": {"kind": "frame", "ref": "out/result.parquet"}}
    decoded = exec_transport.deserialize_result(_response(payload, kind="PLOT"), job_dir, "PLOT", 60)
    err = _error_text(decoded, "PLOT")
    assert err.startswith("ResultTooLarge:"), err


def test_uncompressed_result_parquet_within_caps_is_read(job_dir):
    _write_result_parquet(job_dir, _sales())
    decoded = exec_transport.deserialize_result(_response(_frame_payload("out/result.parquet")), job_dir, "PYTHON", 60)
    assert isinstance(decoded.get("result"), pd.DataFrame), decoded
    assert decoded["result"].equals(_sales())


def test_serialize_result_writes_uncompressed_parquet(job_dir):
    exec_transport.serialize_result(_python_return(_sales()), job_dir)
    pf = pq.ParquetFile(job_dir / "out" / "result.parquet")
    codecs = {pf.metadata.row_group(rg).column(c).compression
              for rg in range(pf.metadata.num_row_groups) for c in range(pf.metadata.num_columns)}
    assert codecs == {"UNCOMPRESSED"}, codecs


# ---------------------------------------------------------------------------
# status → error text
# ---------------------------------------------------------------------------
def test_timeout_error_text_matches_code_exec_literal():
    source = (ROOT / "code_exec.py").read_text(encoding="utf-8")
    assert 'f"TimeoutError: Code execution exceeded {timeout} seconds limit"' in source
    assert exec_transport.timeout_error_text(60) == TIMEOUT_TEXT.format(timeout=60)
    assert exec_transport.timeout_error_text(60) == "TimeoutError: Code execution exceeded 60 seconds limit"
    assert exec_transport.timeout_error_text(2.5) == "TimeoutError: Code execution exceeded 2.5 seconds limit"


def test_timeout_error_text_matches_the_in_process_text_for_the_real_default():
    """The REAL default, built on both sides from the module constant.

    `code_exec.CODE_EXEC_TIMEOUT_SECONDS` is the int `60`, so the in-process
    text says "60 seconds limit"; the same value crosses to the executor as
    JSON and comes back through a `float`-typed field, which would render
    "60.0 seconds limit" — two characters that `brain_client.retry` forwards
    verbatim to the brain. Never assert a literal `60` here: the expected
    string is built the way `code_exec.py:251` builds it, so a change on
    EITHER side fails this test instead of silently drifting.
    """
    default = code_exec.CODE_EXEC_TIMEOUT_SECONDS
    timeout = default
    expected = f"TimeoutError: Code execution exceeded {timeout} seconds limit"
    assert exec_transport.timeout_error_text(default) == expected, expected
    assert exec_transport.timeout_error_text(float(default)) == expected, expected


@pytest.mark.parametrize("timeout_s,rendered", [
    (60, 60),
    (60.0, 60),
    (2.5, 2.5),
    (120.0, 120),
    (3, 3),
    (3.0, 3),
])
def test_status_timeout_maps_to_the_byte_identical_text(job_dir, timeout_s, rendered):
    text = TIMEOUT_TEXT.format(timeout=rendered)
    py = exec_transport.deserialize_result(_response(None, status="timeout"), job_dir, "PYTHON", timeout_s)
    assert py == {"error": text}, py
    pl = exec_transport.deserialize_result(_response(None, status="timeout", kind="PLOT"), job_dir, "PLOT", timeout_s)
    assert pl == {"ok": False, "error": text, "trace": ""}, pl


@pytest.mark.parametrize("value,expected", [
    (60, 60),
    (60.0, 60),
    (60.000, 60),
    (3, 3),
    (3.0, 3),
    (2.5, 2.5),
    (0.5, 0.5),
])
def test_normalize_timeout_keeps_integral_values_integral(value, expected):
    """An integral timeout normalizes to an `int` — the TYPE is what renders."""
    got = exec_transport.normalize_timeout(value)
    assert got == expected, (got, expected)
    assert type(got) is type(expected), (type(got), type(expected))
    rendered = exec_transport.timeout_error_text(value)
    assert rendered == TIMEOUT_TEXT.format(timeout=expected), rendered


@pytest.mark.parametrize("value", [None, "later", float("nan"), float("inf")])
def test_normalize_timeout_never_raises_on_odd_input(value):
    """Article IV: the helper never raises — odd input comes back unchanged
    (NaN compares unequal to itself, so identity counts as unchanged)."""
    got = exec_transport.normalize_timeout(value)
    unchanged = got is value or got == value
    assert unchanged, (got, value)


def test_status_killed_maps_to_memory_error(job_dir):
    py = exec_transport.deserialize_result(_response(None, status="killed", exit_code=-9, signal=9), job_dir, "PYTHON", 60)
    assert py == {"error": MEMORY_TEXT}, py
    pl = exec_transport.deserialize_result(_response(None, status="killed", kind="PLOT", exit_code=-9, signal=9), job_dir, "PLOT", 60)
    assert pl == {"ok": False, "error": MEMORY_TEXT, "trace": ""}, pl


def test_status_crashed_maps_to_executor_crash_error_with_details(job_dir):
    resp = _response(None, status="crashed", exit_code=139, signal=11, reason="signal",
                     traceback="Segmentation fault")
    py = exec_transport.deserialize_result(resp, job_dir, "PYTHON", 60)
    err = _error_text(py, "PYTHON")
    assert err.startswith("ExecutorCrashError: the analysis process exited unexpectedly ("), err
    assert "exit=139" in err and "signal=11" in err and "reason=signal" in err, err
    assert sorted(py) == ["error"], sorted(py)
    resp_pl = _response(None, status="crashed", kind="PLOT", exit_code=0, signal=None, reason="response_invalid")
    pl = exec_transport.deserialize_result(resp_pl, job_dir, "PLOT", 60)
    err_pl = _error_text(pl, "PLOT")
    assert err_pl.startswith("ExecutorCrashError:"), err_pl
    assert "reason=response_invalid" in err_pl, err_pl
    assert sorted(pl) == ["error", "ok", "trace"], sorted(pl)


def test_status_error_returns_the_reconstructed_payload(job_dir):
    payload = {"error": "ZeroDivisionError: division by zero"}
    py = exec_transport.deserialize_result(_response(payload, status="error"), job_dir, "PYTHON", 60)
    assert py == payload, py
    pl_payload = {"ok": False, "error": "ValueError: bad", "trace": "tb"}
    pl = exec_transport.deserialize_result(_response(pl_payload, status="error", kind="PLOT"), job_dir, "PLOT", 60)
    assert pl == pl_payload, pl


_GARBAGE_RESPONSES = [
    None,
    "junk",
    {},
    {"status": "ok"},
    {"status": "ok", "payload": "junk"},
    {"status": "ok", "payload": ["list"]},
    {"status": "weird", "payload": {}},
    {"status": "ok", "payload": {"result": {"kind": "frame"}}},
    {"status": "ok", "payload": {"result": {"kind": "nonsense"}}},
    {"status": "ok", "payload": {"result": {"kind": "dict", "entries": "junk"}}},
    {"status": "ok", "payload": {"image_base64": {"ref": "out/figure.png", "encoding": "zip"}}},
    {"status": "ok", "payload": {"preview": {"__opaque__": 5}}},
]


@pytest.mark.parametrize("response", _GARBAGE_RESPONSES, ids=range(len(_GARBAGE_RESPONSES)))
@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
def test_deserialize_result_never_raises_on_garbage(job_dir, response, kind):
    decoded = exec_transport.deserialize_result(response, job_dir, kind, 60)
    assert isinstance(decoded, dict), decoded
    structurally_broken = (
        not isinstance(response, dict)
        or response.get("status") not in ("ok", "error")
        or not isinstance(response.get("payload"), dict)
    )
    if structurally_broken:
        err = _error_text(decoded, kind)
        assert err.startswith("ExecutorResponseError:"), err


def test_unknown_payload_keys_are_dropped(job_dir):
    payload = {"error": None, "result": {"kind": "scalar", "value": 1}, "preview": 1,
               "image_base64": None, "evil": "x", "__class__": "y"}
    decoded = exec_transport.deserialize_result(_response(payload), job_dir, "PYTHON", 60)
    assert "evil" not in decoded and "__class__" not in decoded, sorted(decoded)
    pl = {"ok": True, "image": None, "is_plotly": False, "chart_data": None, "extra": 1}
    decoded_pl = exec_transport.deserialize_result(_response(pl, kind="PLOT"), job_dir, "PLOT", 60)
    assert "extra" not in decoded_pl, sorted(decoded_pl)


def test_error_strings_are_truncated_to_the_cap(job_dir):
    long = "E" * (exec_transport.ERROR_MAX_CHARS + 5000)
    decoded = exec_transport.deserialize_result(_response({"error": long}, status="error"), job_dir, "PYTHON", 60)
    err = decoded["error"]
    assert len(err) <= exec_transport.ERROR_MAX_CHARS, len(err)
    assert err.startswith("EEEE"), err[:10]
    pl = exec_transport.deserialize_result(
        _response({"ok": False, "error": long, "trace": "T" * 50000}, status="error", kind="PLOT"), job_dir, "PLOT", 60)
    assert len(pl["error"]) <= exec_transport.ERROR_MAX_CHARS, len(pl["error"])
    assert len(pl["trace"]) <= exec_transport.ERROR_MAX_CHARS, len(pl["trace"])


def test_chart_data_over_cap_becomes_none(job_dir, monkeypatch):
    monkeypatch.setattr(exec_transport, "CHART_DATA_MAX_BYTES", 100)
    big = {"x": list(range(200)), "y": list(range(200))}
    payload = {"ok": True, "image": None, "is_plotly": False, "chart_data": big}
    decoded = exec_transport.deserialize_result(_response(payload, kind="PLOT"), job_dir, "PLOT", 60)
    assert decoded.get("ok") is True, decoded
    assert decoded.get("chart_data") is None, decoded


# ---------------------------------------------------------------------------
# constants, caps parity, JSON dialect
# ---------------------------------------------------------------------------
def test_constants_have_the_planned_values():
    values = {
        "INLINE_MAX_BYTES": 2 * 1024 * 1024,
        "REF_FILE_MAX_BYTES": 64 * 1024 * 1024,
        "RESULT_MAX_ROWS": 5_000_000,
        "RESULT_MAX_COLUMNS": 2_000,
        "RESULT_FILE_MAX_BYTES": 256 * 1024 * 1024,
        "RESULT_MAX_DECODED_BYTES": 512 * 1024 * 1024,
        "STDIO_MAX_CHARS": 65536,
        "ERROR_MAX_CHARS": 20000,
        "CHART_DATA_MAX_BYTES": 4 * 1024 * 1024,
        "STYLED_MAX_ROWS": 200,
        "STYLED_MAX_COLS": 40,
        "STYLED_MAX_CHARS": 500_000,
    }
    for name, expected in values.items():
        got = getattr(exec_transport, name)
        assert got == expected, (name, got, expected)


def test_styled_caps_match_run_chat_local():
    assert exec_transport.STYLED_MAX_ROWS == run_chat_local._STYLED_MAX_ROWS
    assert exec_transport.STYLED_MAX_COLS == run_chat_local._STYLED_MAX_COLS
    assert exec_transport.STYLED_MAX_CHARS == run_chat_local._STYLED_MAX_CHARS


def test_dumps_loads_keep_nan_and_inf_and_unicode_compact():
    text = exec_transport.dumps({"a": float("nan"), "b": float("inf"), "c": float("-inf"), "u": "ქ"})
    assert text == '{"a":NaN,"b":Infinity,"c":-Infinity,"u":"ქ"}', text
    back = exec_transport.loads(text)
    assert math.isnan(back["a"]) and math.isinf(back["b"]) and back["c"] < 0 and back["u"] == "ქ", back
    from_bytes = exec_transport.loads(text.encode("utf-8"))
    assert math.isnan(from_bytes["a"]) and from_bytes["u"] == "ქ", from_bytes
    assert exec_transport.loads(b'{"x":[1,2.5,null,true]}') == {"x": [1, 2.5, None, True]}


# ---------------------------------------------------------------------------
# descriptor hygiene and the both-formats-fail input branch
# ---------------------------------------------------------------------------
def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.skipif(not POSIX or not Path("/proc/self/fd").is_dir(),
                    reason="counting descriptors needs /proc")
def test_a_failing_fstat_while_opening_a_reference_leaks_no_descriptor(job_dir, monkeypatch):
    """The checks that run while the reference is open sit under ONE guard: a
    per-branch close left the descriptor open when `os.fstat` was the raiser,
    so a hostile response could exhaust the web worker's descriptors."""
    out = job_dir / "out"
    out.mkdir(exist_ok=True)
    _sales().to_parquet(out / "result.parquet", engine="pyarrow", compression=None)

    def boom(fd):
        raise OSError(9, "bad file descriptor")

    monkeypatch.setattr(os, "fstat", boom)
    before = _fd_count()
    errors = []
    for _ in range(25):
        decoded = exec_transport.deserialize_result(
            _response(_frame_payload("out/result.parquet")), job_dir, "PYTHON", 60)
        errors.append(_error_text(decoded, "PYTHON"))
    after = _fd_count()
    monkeypatch.undo()

    assert all(e.startswith("ExecutorResponseError:") for e in errors), errors[:2]
    leaked = after - before
    assert leaked <= 0, (before, after)


def test_write_inputs_raises_when_neither_format_can_carry_the_frame(job_dir, monkeypatch):
    """The dispatcher catches this `ValueError` and reports a preparation
    error; it must name the key and leave the reason in the log."""
    logged = []
    monkeypatch.setattr(exec_transport, "log_with_sid",
                        lambda sid, level, message, *a, **k: logged.append(message))

    def no_parquet(self, *a, **k):
        raise OSError("no parquet here")

    def no_pickle(self, *a, **k):
        raise OSError("no pickle here")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", no_parquet)
    monkeypatch.setattr(pd.DataFrame, "to_pickle", no_pickle)

    with pytest.raises(ValueError) as excinfo:
        exec_transport.write_inputs({"sales": _sales()}, job_dir, sid="t")

    message = str(excinfo.value)
    assert "cannot transport dataframe 'sales'" in message, message
    failures = [m for m in logged if m.startswith("EXEC_INPUT_WRITE_FAILED")]
    assert len(failures) == 1, logged
    line = failures[0]
    assert "key=sales" in line, line
    assert "no parquet here" in line and "no pickle here" in line, line
