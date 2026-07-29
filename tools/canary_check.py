"""Post-release canary for the dataframe execution pipeline.

Loads a known dataset through the REAL production load pipeline
(local_store.ChatDataStore.load_dataframes: parse -> parquet cache -> memory
cache) and runs a fixed battery of analytical operations through the REAL
execution path (code_exec.safe_execute — sandbox builtins + the Article XIII
sanitize gate on-path), comparing results against pinned expected values in
tools/canary_expected.json. Catches "pipeline serves data that computes
wrong answers" regressions (e.g. exotic dtypes reaching generated code —
the demo 3D-chart cartesian-groupby incident) that unit tests on individual
layers can miss.

Usage:
    python tools/canary_check.py                     # CSV mode, default fixture
    python tools/canary_check.py --source my.csv
    python tools/canary_check.py --snapshots-dir DIR --registry data_sources.json
    python tools/canary_check.py --write-expected    # regenerate expected file

Run after a release: on the upgraded stack's image (or any checkout of the
shipped code), run the no-argument form — all checks must PASS (exit 0).
To validate a customer/demo DB-snapshot set, additionally run the
--snapshots-dir form pointing at the instance's db_snapshots/ directory and
data_sources.json (invariant checks only — values there change with data).

Checks (CSV mode — value expectations from tools/canary_expected.json):
    C1  load: dataset key present, shape, column list
    C2  dtype gate: sanitize_for_execution identity fast path + every column
        dtype standard (numpy or tz-aware datetime)
    C3  revenue by product (groupby sum)
    C4  product value_counts
    C5  to_datetime month split
    C6  two-key groupby (region x product) — OBSERVED combinations only
        (a categorical regression yields the full cartesian product)
    C7  merge with a zone dictionary then groupby zone
    C8  filter (revenue > 100) then groupby region

Snapshot mode (--snapshots-dir + --registry) runs per registered table:
    S1  parquet loads, post-gate dtypes all standard
    S2  observed-only groupby invariant on the lowest-cardinality string col
    S3  exec smoke: len(dfs[key]) through safe_execute

Exit code 0 on PASS-ALL, 1 otherwise.
"""
import argparse
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "tools" / "fixtures" / "sample_sales.csv"
DEFAULT_EXPECTED = REPO_ROOT / "tools" / "canary_expected.json"


def _match(expected, got, rtol):
    """Recursive comparison; floats via isclose. JSON-normalized inputs."""
    if isinstance(expected, dict):
        return (isinstance(got, dict) and set(expected) == set(got)
                and all(_match(expected[k], got[k], rtol) for k in expected))
    if isinstance(expected, list):
        return (isinstance(got, list) and len(expected) == len(got)
                and all(_match(e, g, rtol) for e, g in zip(expected, got)))
    if isinstance(expected, bool) or isinstance(got, bool):
        return expected == got
    if isinstance(expected, (int, float)) and isinstance(got, (int, float)):
        return math.isclose(expected, got, rel_tol=rtol, abs_tol=1e-9)
    return expected == got


def _jsonify(value):
    """Round-trip through JSON so int dict keys become strings etc. — the
    same normalization the expected file went through when written."""
    return json.loads(json.dumps(value))


class Checker:
    def __init__(self):
        self.results = []

    def record(self, name, passed, detail=""):
        self.results.append((name, passed, detail))
        print(f"[{'PASS' if passed else 'FAIL'}] {name}" +
              (f" — {detail}" if detail and not passed else ""))

    @property
    def all_passed(self):
        return all(p for _, p, _ in self.results)


def _exec_result(code_exec, code, dfs, name):
    res = code_exec.safe_execute(code, dfs, sid="canary")
    if res.get("error"):
        raise RuntimeError(f"{name}: exec error: {res['error']}")
    return res.get("result")


def _value_checks(code_exec, dfs, key):
    """C3..C8 — each returns a JSON-safe value via the real exec path."""
    k = repr(key)
    zones = ("pd.DataFrame({'region': ['North','South','East','West'], "
             "'zone': ['NS','NS','EW','EW']})")
    return {
        "C3_revenue_by_product": _exec_result(
            code_exec,
            f"RESULT = dfs[{k}].groupby('product')['revenue'].sum().to_dict()",
            dfs, "C3"),
        "C4_product_counts": _exec_result(
            code_exec,
            f"RESULT = dfs[{k}]['product'].value_counts().to_dict()",
            dfs, "C4"),
        "C5_month_split": _exec_result(
            code_exec,
            f"RESULT = {{int(m): int(c) for m, c in "
            f"pd.to_datetime(dfs[{k}]['order_date']).dt.month"
            f".value_counts().to_dict().items()}}",
            dfs, "C5"),
        "C6_two_key_observed": _exec_result(
            code_exec,
            f"g = dfs[{k}].groupby(['region','product'])['revenue'].sum()\n"
            f"RESULT = sorted([[r, p, float(v)] for (r, p), v in "
            f"g.to_dict().items()])",
            dfs, "C6"),
        "C7_merge_then_group": _exec_result(
            code_exec,
            f"zones = {zones}\n"
            f"RESULT = dfs[{k}].merge(zones, on='region')"
            f".groupby('zone')['revenue'].sum().to_dict()",
            dfs, "C7"),
        "C8_filter_then_group": _exec_result(
            code_exec,
            f"d = dfs[{k}]\n"
            f"RESULT = d[d['revenue'] > 100]"
            f".groupby('region')['revenue'].sum().to_dict()",
            dfs, "C8"),
    }


def _run_csv_mode(args, checker, modules):
    local_store, code_exec, exec_sanitizer = modules
    source = Path(args.source)
    store = local_store.ChatDataStore("c_canary")
    shutil.copy(source, store.files_dir / source.name)
    dfs = store.load_dataframes()
    key = source.name

    checker.record("C1_load", key in dfs and list(dfs) == [key]
                   and len(dfs[key]) > 0,
                   f"keys={list(dfs)}")
    if key not in dfs:
        return None
    df = dfs[key]
    gated = exec_sanitizer.sanitize_for_execution(dfs, "canary")
    checker.record(
        "C2_dtype_gate",
        gated is dfs and all(exec_sanitizer._is_standard_dtype(dt)
                             for dt in df.dtypes),
        f"dtypes={[str(d) for d in df.dtypes]}, identity={gated is dfs}")

    got = {name: _jsonify(val)
           for name, val in _value_checks(code_exec, dfs, key).items()}

    if args.write_expected:
        Path(args.expected).write_text(
            json.dumps(got, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Wrote expected values to {args.expected}")
        return got

    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    for name in sorted(expected):
        ok = name in got and _match(expected[name], got[name], args.rtol)
        checker.record(name, ok,
                       f"expected={expected[name]} got={got.get(name)}")
    return got


def _run_snapshot_mode(args, checker, modules):
    local_store, code_exec, exec_sanitizer = modules
    import pandas as pd
    registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    tables = registry.get("tables", [])
    if not tables:
        checker.record("S0_registry", False, "no tables in registry")
        return
    for t in tables:
        tid, name = t.get("id"), t.get("display_name") or t.get("id")
        path = Path(args.snapshots_dir) / f"{tid}.parquet"
        if not path.exists():
            checker.record(f"S1_load[{name}]", False, f"missing {path}")
            continue
        df = pd.read_parquet(path)
        dfs = {name: df}
        gated = exec_sanitizer.sanitize_for_execution(dfs, "canary")
        checker.record(
            f"S1_dtypes[{name}]",
            all(exec_sanitizer._is_standard_dtype(dt)
                for dt in gated[name].dtypes),
            f"dtypes={[str(d) for d in gated[name].dtypes]}")
        str_cols = [c for c in df.columns if df[c].dtype == object]
        if str_cols:
            col = min(str_cols, key=lambda c: df[c].nunique(dropna=True))
            got = _exec_result(
                code_exec,
                f"g = dfs[{name!r}].groupby({col!r}).size()\n"
                f"RESULT = [int(len(g)), int(dfs[{name!r}][{col!r}]"
                f".nunique(dropna=True))]",
                dfs, "S2")
            checker.record(f"S2_observed_groupby[{name}]",
                           got[0] == got[1], f"groups={got[0]} uniques={got[1]}")
        got_len = _exec_result(code_exec,
                               f"RESULT = int(len(dfs[{name!r}]))", dfs, "S3")
        checker.record(f"S3_exec_smoke[{name}]", got_len == len(df),
                       f"exec len={got_len} direct len={len(df)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help="CSV to load through the file pipeline")
    parser.add_argument("--snapshots-dir", type=Path, default=None,
                        help="db_snapshots/ dir for snapshot mode")
    parser.add_argument("--registry", type=Path, default=None,
                        help="data_sources.json for snapshot mode")
    parser.add_argument("--expected", type=Path, default=DEFAULT_EXPECTED,
                        help="expected-values JSON (CSV mode)")
    parser.add_argument("--write-expected", action="store_true",
                        help="regenerate the expected-values JSON and exit")
    parser.add_argument("--rtol", type=float, default=1e-6,
                        help="relative tolerance for float comparisons")
    args = parser.parse_args()

    # Isolate BEFORE importing settings/local_store — DATA_ROOT is frozen
    # from the environment at import time. Guarantees the canary can never
    # touch a real data root.
    tmp_root = tempfile.mkdtemp(prefix="pdc_canary_")
    os.environ["DATA_ROOT"] = tmp_root
    sys.path.insert(0, str(REPO_ROOT))
    try:
        import local_store
        import code_exec
        import exec_sanitizer
        modules = (local_store, code_exec, exec_sanitizer)

        checker = Checker()
        if args.snapshots_dir or args.registry:
            if not (args.snapshots_dir and args.registry):
                print("snapshot mode needs BOTH --snapshots-dir and --registry")
                return 1
            _run_snapshot_mode(args, checker, modules)
        else:
            _run_csv_mode(args, checker, modules)
            if args.write_expected:
                return 0
        print(f"\n{'PASS-ALL' if checker.all_passed else 'FAILURES PRESENT'} "
              f"({sum(1 for _, p, _ in checker.results if p)}/"
              f"{len(checker.results)} passed)")
        return 0 if checker.all_passed else 1
    except Exception as e:
        print(f"CANARY ERROR: {type(e).__name__}: {e}")
        return 1
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
