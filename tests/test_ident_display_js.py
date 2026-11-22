"""P31 Part D §2 / §3 — the browser copies of the identifier rule, and the
sandbox image's COPY lists.

Browser (spec §2): `dashboard.js` (`_fmtCellValue`), `vendor/viewers.js`
(`fmtCell`, Show data / full table) and `dashboard_view.js` read the table
payload's `identifier_columns`; without it (an old stored row) they fall back
to a NAME rule mirroring `ident_rule.is_identifier_name` — camelCase split,
the new words (`number`, `номер`, `ნომერი`, …), a trailing `#` — and show an
identifier as plain digits (`String(v)` for an integer), never grouped, never
decimals. `static/chat.js` (loaded by no template) has its copy removed when
nothing references it, otherwise aligned: since chat.js's own table renderer
calls it, the copy must either be gone together with every call, or follow
the same rule.

Pinned two ways, without a browser:
* structurally — each file reads `identifier_columns` and lists every word;
* behaviourally in node (when node is on PATH) — the contiguous block from
  the word set to the cell formatter is evaluated on its own and the fallback
  rule is run against the same label table as `ident_rule`.

Sandbox image (spec §3): `ident_rule.py` is in BOTH Dockerfiles' COPY lists
(the executor list is pinned exactly by tests/test_executor_structure.py and
tests/test_executor_dispatch.py, extended for this change).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

PAGE_FILES = {
    "dashboard.js": STATIC / "dashboard.js",
    "viewers.js": STATIC / "vendor" / "viewers.js",
    "dashboard_view.js": STATIC / "dashboard_view.js",
}
CHAT_JS = STATIC / "chat.js"

NEW_WORDS = ["number", "num", "no", "nr", "nbr", "key", "uid", "guid", "uuid", "pin",
             "pinfl", "ssn", "tin", "inn", "iin", "passport", "card", "msisdn", "imei",
             "ნომერი", "номер", "код", "счет", "счёт", "ид"]
OLD_WORDS = ["year", "yr", "წელი", "id", "ids", "code", "codes", "კოდი", "იდ", "zip",
             "postal", "phone", "account", "iban", "invoice", "order", "ref"]

# label -> expected verdict of the fallback NAME rule (mirrors ident_rule)
NAME_CASES = {
    "personal_number": True, "personalNumber": True, "PersonalNumber": True,
    "clientID": True, "ClientId": True, "client_id": True, "IBAN": True,
    "card_no": True, "phone": True, "Account #": True, "Client #": True,
    "პირადი ნომერი": True, "Лицевой счет": True, "Year": True, "Order ID": True,
    "number_of_orders": False, "Number of Orders": False, "order_count": False,
    "amount": False, "income": False, "client_since": False, "Order Value": False,
    "ID Count": False, "# of clients": False,
    # last-token rule: "счета" is not a listed word (see test_ident_rule.py)
    "Номер счета": False, "Код клиента": False,
    # evaluator review: the measure-word veto, and "no" needs a noun before it
    "Total Number": False, "сумма_счет": False, "თანხა_ნომერი": False,
    "salary_no": False, "price_key": False,
    # re-review: an ORIGINAL last word overrides the measure veto
    "Revenue per Order": True, "Sales per account": True, "total_id": True,
    "amount_code": True, "count_id": True, "sales_year": True, "Sales Order": True,
    "income_account": True, "revenue_account": True, "price_code": True,
    "rate_code": True,
    "no": False, "No": False, "Yes/No": False, "account_no": True,
    # coordinator decision: entity nouns are not measure words
    "loan_id": True, "payment_id": True, "deposit_no": True, "card_limit_id": True,
    "loan_amount": False, "payment_total": False, "deposit_sum": False,
}

# (column, value, expected text) for the cell formatter WITHOUT a payload list
CELL_CASES = [
    ("personal_number", 52906123456, "52906123456"),
    ("personalNumber", 52906123456, "52906123456"),
    ("clientID", 1234567890123, "1234567890123"),
    ("amount", 1234567, "1,234,567"),
    ("loan_amount", 1234.5, "1,234.5"),
    ("personal_number", None, ""),
]

_WORDSET_RE = re.compile(r"(?:const|var|let)\s+(_?IDENT\w*WORDS)\s*=")
_IDENT_FN_RE = re.compile(r"function\s+(_?is\w*Ident\w*)\s*\(")
_FMT_FN_RE = re.compile(r"function\s+(_?fmtCell\w*)\s*\(")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_end(src: str, start: int) -> int:
    """Index just past the closing brace of the function starting at `start`."""
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        ch = src[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    raise AssertionError("unbalanced function body")


def _rule_block(src: str):
    """(block source, ident fn name, fmt fn name): the contiguous block from
    the word set to the end of the later of the two functions."""
    ws = _WORDSET_RE.search(src)
    idf = _IDENT_FN_RE.search(src)
    fmf = _FMT_FN_RE.search(src)
    assert ws and idf and fmf, (bool(ws), bool(idf), bool(fmf))
    start = min(ws.start(), idf.start(), fmf.start())
    end = max(_function_end(src, idf.start()), _function_end(src, fmf.start()))
    return src[start:end], idf.group(1), fmf.group(1)


def _word_set_literal(src: str) -> str:
    m = _WORDSET_RE.search(src)
    assert m, "no identifier word set"
    end = src.index(")", m.end())
    return src[m.start():end]


# ---------------------------------------------------------------------------
# structural
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(PAGE_FILES))
def test_page_script_reads_identifier_columns_from_the_table_payload(name):
    src = _read(PAGE_FILES[name])
    assert "identifier_columns" in src, f"{name} never reads identifier_columns"


@pytest.mark.parametrize("name", sorted(PAGE_FILES))
@pytest.mark.parametrize("word", OLD_WORDS + NEW_WORDS)
def test_page_script_fallback_word_set_holds_every_word(name, word):
    literal = _word_set_literal(_read(PAGE_FILES[name]))
    assert re.search(r"['\"]" + re.escape(word) + r"['\"]", literal), (name, word)


@pytest.mark.parametrize("name", sorted(PAGE_FILES))
def test_page_script_fallback_splits_camelcase(name):
    block, _i, _f = _rule_block(_read(PAGE_FILES[name]))
    # a lower-case -> upper-case boundary test, by Unicode class or by range
    assert re.search(r"\\p\{Ll\}|\\p\{Lu\}|\[a-z[^\]]*\][^\n]*\[A-Z", block), name


@pytest.mark.parametrize("name", sorted(PAGE_FILES))
def test_page_script_fallback_knows_the_trailing_hash(name):
    block, _i, _f = _rule_block(_read(PAGE_FILES[name]))
    assert "'#'" in block or '"#"' in block, name


@pytest.mark.parametrize("name", sorted(PAGE_FILES))
def test_page_script_cells_stay_text_content(name):
    """Art. XV: the identifier change must not introduce innerHTML for cells —
    the formatter's result still lands in `textContent` (or is escaped)."""
    src = _read(PAGE_FILES[name])
    fmt = _FMT_FN_RE.search(src).group(1)
    uses = [m.start() for m in re.finditer(re.escape(fmt) + r"\(", src)]
    assert len(uses) >= 2, f"{fmt} is defined but never called in {name}"
    for pos in uses:
        line = src[src.rfind("\n", 0, pos) + 1: src.find("\n", pos)]
        if re.search(r"function\s+" + re.escape(fmt), line):
            continue
        assert "innerHTML" not in line or "esc(" in line, line


def test_chat_js_copy_is_removed_or_aligned():
    src = _read(CHAT_JS)
    defines_rule = bool(_IDENT_FN_RE.search(src))
    defines_fmt = bool(_FMT_FN_RE.search(src))
    if not defines_rule and not defines_fmt:
        # removed: then nothing may call it any more
        assert "_fmtCellValue(" not in src and "_isIdentifierLabel(" not in src
        assert "_IDENT_HEAD_WORDS" not in src
        return
    literal = _word_set_literal(src)
    for word in NEW_WORDS:
        assert re.search(r"['\"]" + re.escape(word) + r"['\"]", literal), word
    assert "identifier_columns" in src


# ---------------------------------------------------------------------------
# behaviour in node
# ---------------------------------------------------------------------------
_HARNESS = r"""
const fs = require('fs');
const [blockPath, identName, fmtName, casesPath] = process.argv.slice(2);
const block = fs.readFileSync(blockPath, 'utf8');
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const fns = new Function(block + `\nreturn { ident: ${identName}, fmt: ${fmtName} };`)();
const names = {};
for (const label of cases.names) names[label] = !!fns.ident(label);
const cells = cases.cells.map(([c, v]) => fns.fmt(c, v));
process.stdout.write(JSON.stringify({ names, cells }));
"""


def _run_node(path: Path, tmp_path: Path) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    block, ident_name, fmt_name = _rule_block(_read(path))
    block_file = tmp_path / "block.js"
    block_file.write_text(block, encoding="utf-8")
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps({"names": list(NAME_CASES),
                                      "cells": [[c, v] for c, v, _e in CELL_CASES]},
                                     ensure_ascii=False), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(block_file), ident_name, fmt_name,
                           str(cases_file)], capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-2000:]
    return json.loads(proc.stdout.decode("utf-8"))


def _js_targets():
    targets = sorted(PAGE_FILES.items())
    if _IDENT_FN_RE.search(_read(CHAT_JS)):
        targets.append(("chat.js", CHAT_JS))
    return targets


@pytest.mark.parametrize("name,path", _js_targets(), ids=[n for n, _p in _js_targets()])
def test_js_fallback_name_rule_mirrors_ident_rule(name, path, tmp_path):
    out = _run_node(path, tmp_path)
    wrong = {label: got for label, got in out["names"].items() if got != NAME_CASES[label]}
    assert wrong == {}, (name, wrong)


@pytest.mark.parametrize("name,path", _js_targets(), ids=[n for n, _p in _js_targets()])
def test_js_cell_formatter_shows_identifiers_as_plain_digits(name, path, tmp_path):
    out = _run_node(path, tmp_path)
    expected = [e for _c, _v, e in CELL_CASES]
    assert out["cells"] == expected, (name, list(zip(CELL_CASES, out["cells"])))


def test_js_rule_agrees_with_the_python_rule_on_the_label_table():
    import ident_rule
    wrong = {label: ident_rule.is_identifier_name(label) for label, exp in NAME_CASES.items()
             if ident_rule.is_identifier_name(label) is not exp}
    assert wrong == {}, wrong


# ---------------------------------------------------------------------------
# both Dockerfiles copy ident_rule.py
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dockerfile", ["Dockerfile", "executor/Dockerfile"])
def test_both_dockerfiles_copy_ident_rule(dockerfile):
    text = _read(ROOT / dockerfile)
    copy_text = " ".join(
        line for line in re.split(r"(?<!\\)\n", text.replace("\\\n", " "))
        if line.strip().upper().startswith("COPY"))
    assert re.search(r"(^|\s)ident_rule\.py(\s|$)", copy_text), dockerfile
