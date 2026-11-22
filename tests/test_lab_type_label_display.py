"""C4 (p29, finding F9 §3) — the Text / Number / Date / Yes-No type label is
DISPLAY ONLY.

The Descriptions view used to show the raw pandas dtype (built in
`dataset_profile._generate_technical_description`, `str(series.dtype)`), and
a CSV date column read `object` because nothing parses it. C4 shows a
friendly label instead, computed in static/dashboard.js (`_columnTypeLabel`)
with a display-only date check. What must NOT change:

* the stored technical description still starts with the raw dtype (it is
  what the schema text and the brain see);
* `compute_profile` still reports the raw dtype per column;
* the schema text's `Column Types` still carries the raw dtype for a
  date-like text column (no "Date" re-typing, no new data to the brain).

What the label does (N3, executed in node when it is on PATH): the values a
technical description already lists decide when present (a robust split, so
"Jan 5, 2024" survives the ", " separator); otherwise the column NAME decides
— tokens date / dates / datetime / timestamp / dt / datum / дата / თარიღი, an
`_at` / `_on` suffix or camelCase `At` / `On` — unless the name also carries a
text-like token (comment, note, text, description, desc, remark).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

import dataset_profile
import schema_builder

ROOT = Path(__file__).resolve().parent.parent
JS_PATH = ROOT / "static" / "dashboard.js"

DATES = ["2024-01-05", "2024-02-11", "2024-03-17", None, "2024-04-02"]


def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "order_date": pd.Series(DATES, dtype=object),
        "amount": [10.5, 20.0, 7.25, 3.0, 11.0],
        "qty": [1, 2, 3, 4, 5],
        "region": ["North", "South", "North", "East", "West"],
        "is_member": [True, False, True, True, False],
        "parsed": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03",
                                  "2024-01-04", "2024-01-05"]),
    })


# ---------------------------------------------------------------------------
# the analysis-facing types do not change
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("col", ["order_date", "amount", "qty", "region", "is_member", "parsed"])
def test_the_technical_description_still_starts_with_the_raw_dtype(col):
    df = _frame()
    tech = dataset_profile._generate_technical_description(df[col], len(df), name=col)
    assert tech.split(",")[0].strip() == str(df[col].dtype), tech


def test_a_date_like_text_column_stays_text_in_the_technical_description():
    df = _frame()
    tech = dataset_profile._generate_technical_description(df["order_date"], len(df))
    first = tech.split(",")[0].strip().lower()
    assert "date" not in first, tech


def test_the_profile_reports_the_raw_dtype():
    df = _frame()
    prof = dataset_profile.compute_profile(df)
    for col in df.columns:
        assert prof["columns"][col]["dtype"] == str(df[col].dtype), (col, prof["columns"][col])


def test_the_schema_text_keeps_the_raw_dtype_for_a_date_like_text_column():
    df = _frame()
    text = schema_builder.schema_text({}, {"orders.csv": df})
    m = re.search(r"Column Types: (\{.*?\})\s*(?:\n|$)", text)
    assert m, text[:600]
    types = json.loads(m.group(1))
    # A text column reads as text there (`object`, `object (N unique)` or a
    # CATEGORICAL list) — never as a date type.
    shown = str(types.get("order_date"))
    assert not re.match(r"\s*(date|datetime)", shown, re.I), types
    assert shown.startswith(str(df["order_date"].dtype)) or shown.startswith("CATEGORICAL"), types
    assert types.get("amount") == "float64" and types.get("qty") == "int64", types
    for label in ("Date", "Text", "Number", "Yes-No", "Yes/No"):
        assert label not in types.values(), types


def test_the_frame_is_not_mutated_by_profiling():
    df = _frame()
    before = df.dtypes.astype(str).to_dict()
    dataset_profile.compute_profile(df)
    for col in df.columns:
        dataset_profile._generate_technical_description(df[col], len(df), name=col)
    assert df.dtypes.astype(str).to_dict() == before


# ---------------------------------------------------------------------------
# the label heuristic, executed (N3)
# ---------------------------------------------------------------------------
# The heuristic is the block of top-level declarations from
# `const _COLUMN_TYPE_LABELS` up to `function _buildEditorRow` in
# dashboard.js; node evaluates it with `_t` stubbed to its English fallback
# and calls `_columnTypeLabel(dtype, technical, name)` for every case.
_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const start = src.indexOf('const _COLUMN_TYPE_LABELS');
const end = src.indexOf('function _buildEditorRow', start);
if (start < 0 || end < 0) { console.log(JSON.stringify({error: 'block not found'})); process.exit(0); }
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const body = src.slice(start, end) +
  '\nreturn cases.map(c => _columnTypeLabel(c[0], c[1], c[2]));';
const run = new Function('_t', 'cases', body);
const out = run((key, fallback) => fallback, cases);
console.log(JSON.stringify({labels: out}));
"""

NO_LIST = "object, 10/10 filled, 250 unique"


def _listed(values: str, n: int) -> str:
    return f"object, {n}/{n} filled, CATEGORICAL ({n} unique: {values})"


# (case id, dtype, technical description, column name, expected label)
CASES = [
    ("created_at", "object", NO_LIST, "created_at", "Date"),
    ("createdAt", "object", NO_LIST, "createdAt", "Date"),
    ("paid_on", "object", NO_LIST, "paid_on", "Date"),
    ("order_date", "object", NO_LIST, "order_date", "Date"),
    ("timestamp", "object", NO_LIST, "Timestamp", "Date"),
    ("ru_data", "object", NO_LIST, "Дата", "Date"),
    ("geo_tarighi", "object", NO_LIST, "თარიღი", "Date"),
    ("date_comment", "object", NO_LIST, "date_comment", "Text"),
    ("note_date_text", "object", NO_LIST, "note_date_text", "Text"),
    ("date_description", "object", NO_LIST, "date_description", "Text"),
    ("plain_text", "object", NO_LIST, "region", "Text"),
    ("data_not_date", "object", NO_LIST, "update_status", "Text"),
    ("listed_month_names", "object", _listed("Jan 5, 2024, Feb 1, 2024", 2), "when", "Date"),
    ("listed_iso", "object", _listed("2024-01-05, 2024-02-11", 2), "x", "Date"),
    ("listed_not_dates_beat_name", "object", _listed("pending, n/a", 2), "due_date", "Text"),
    ("listed_not_dates", "object", _listed("pending, n/a", 2), "status", "Text"),
    ("version", "object", _listed("1.2.10", 1), "version", "Text"),
    ("versions", "object", _listed("1.2.10, 1.3.0", 2), "version", "Text"),
    ("int", "int64", "int64, 10/10 filled", "qty", "Number"),
    ("float", "float64", "float64, 10/10 filled", "amount", "Number"),
    ("bool", "bool", "bool, 10/10 filled, boolean", "is_member", None),   # Yes-No text
    ("datetime", "datetime64[ns]", "datetime64[ns], 10/10 filled", "parsed", "Date"),
]


@pytest.fixture(scope="module")
def node_labels(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    d = tmp_path_factory.mktemp("type_label")
    harness = d / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    cases = d / "cases.json"
    cases.write_text(json.dumps([[c[1], c[2], c[3]] for c in CASES], ensure_ascii=False),
                     encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(JS_PATH), str(cases)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr[-1500:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "error" not in out, out
    labels = out["labels"]
    assert len(labels) == len(CASES), labels
    return {c[0]: label for c, label in zip(CASES, labels)}


@pytest.mark.parametrize("case", [c for c in CASES if c[4] is not None], ids=lambda c: c[0])
def test_the_type_label_heuristic(node_labels, case):
    cid, dtype, tech, name, want = case
    assert node_labels[cid] == want, (cid, name, tech, node_labels[cid])


def test_a_boolean_reads_yes_no(node_labels):
    assert re.fullmatch(r"yes\s*[/\-–—]\s*no", node_labels["bool"].strip(), re.I), node_labels["bool"]
