"""P31 Part D §5 / §6 — identifiers in Excel, PDF and PPTX exports.

* Excel (`routes/chat.py` `_df_to_xlsx_bytes` / `_table_to_xlsx_bytes`),
  AFTER THE EVALUATOR REVIEW (deliberate update of the first version, which
  wrote every identifier as a text cell): an integral identifier column
  (`ident_rule.identifier_columns(df.columns, source idents when
  available)`) is written as a NUMERIC cell with number_format "0" — Excel
  shows every digit (no grouping, no 5.29E+11) and the cell still sorts and
  filters as a number. An identifier of 16+ digits (beyond Excel's 15-digit
  precision) is a TEXT cell so no digit is lost. A "Year" column stays
  numeric. Measures are unchanged (General format). A failure logs the
  exception TYPE only.
* PDF / PPTX table cells (`routes/report.py` `_render_pdf` ~2403,
  `_render_pptx_builtin` ~709): an identifier column whose stored rows turned
  float (a NaN in the frame) renders `str(int(v))` — no ".0".

Tested through the observable output (the workbook read back with openpyxl,
the PPTX read back with python-pptx, the PDF table data captured at
ReportLab's `Table`), so the tests do not depend on the name of any helper the
implementation introduces. Offline.
"""
import io

import numpy as np
import openpyxl
import pandas as pd
import pytest

import routes.chat as chat_mod
import routes.report as report_mod

PN = 52906123456
LONG_ID = 1234567890123          # 13 digits: Excel would show 1.23457E+12
HUGE_ID = 1234567890123456       # 16 digits: beyond Excel's 15-digit precision


def _sheet(xlsx: bytes):
    wb = openpyxl.load_workbook(io.BytesIO(xlsx))
    return wb.active


def _col(ws, header):
    heads = [c.value for c in ws[1]]
    j = heads.index(header) + 1
    return [ws.cell(row=r, column=j) for r in range(2, ws.max_row + 1)]


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def test_df_to_xlsx_writes_integral_identifiers_as_numbers_with_format_0():
    df = pd.DataFrame({"personal_number": pd.Series([PN, PN + 1], dtype="int64"),
                       "client_id": [LONG_ID, LONG_ID + 7],
                       "loan_amount": [1234567.5, 2000.25]})
    ws = _sheet(chat_mod._df_to_xlsx_bytes(df))
    pn = _col(ws, "personal_number")
    assert [c.data_type for c in pn] == ["n", "n"]
    assert [c.value for c in pn] == [52906123456, 52906123457]
    assert all(isinstance(c.value, int) for c in pn)
    assert [c.number_format for c in pn] == ["0", "0"]
    ids = _col(ws, "client_id")
    assert [c.value for c in ids] == [1234567890123, 1234567890130]
    assert [c.number_format for c in ids] == ["0", "0"]
    amt = _col(ws, "loan_amount")
    assert [c.data_type for c in amt] == ["n", "n"]
    assert [c.value for c in amt] == [1234567.5, 2000.25]
    assert all(c.number_format == "General" for c in amt)


def test_df_to_xlsx_identifier_of_16_or_more_digits_is_a_text_cell():
    df = pd.DataFrame({"card_number": pd.Series([HUGE_ID, HUGE_ID + 1], dtype="int64"),
                       "amount": [1.5, 2.5]})
    ws = _sheet(chat_mod._df_to_xlsx_bytes(df))
    card = _col(ws, "card_number")
    assert [c.data_type for c in card] == ["s", "s"]
    assert [c.value for c in card] == [str(HUGE_ID), str(HUGE_ID + 1)]


def test_df_to_xlsx_identifier_float_column_with_nan_has_no_decimals():
    df = pd.DataFrame({"personal_number": [float(PN), np.nan],
                       "amount": [1.5, 2.5]})
    ws = _sheet(chat_mod._df_to_xlsx_bytes(df))
    pn = _col(ws, "personal_number")
    assert pn[0].data_type == "n" and pn[0].value == 52906123456
    assert pn[0].number_format == "0"
    assert pn[1].value in (None, "")


def test_df_to_xlsx_year_column_stays_numeric():
    df = pd.DataFrame({"Year": [2019, 2020, 2021], "revenue": [1234567, 2345678, 3456789]})
    ws = _sheet(chat_mod._df_to_xlsx_bytes(df))
    year = _col(ws, "Year")
    assert [c.data_type for c in year] == ["n", "n", "n"]
    assert [c.value for c in year] == [2019, 2020, 2021]
    rev = _col(ws, "revenue")
    assert [c.data_type for c in rev] == ["n", "n", "n"]
    assert all(c.number_format == "General" for c in rev)


def test_df_to_xlsx_without_identifiers_keeps_numbers_numeric():
    df = pd.DataFrame({"region": ["N", "S"], "total": [1234567, 89]})
    ws = _sheet(chat_mod._df_to_xlsx_bytes(df))
    tot = _col(ws, "total")
    assert [c.data_type for c in tot] == ["n", "n"]
    assert [c.value for c in tot] == [1234567, 89]
    assert all(c.number_format == "General" for c in tot)


def test_df_to_xlsx_does_not_mutate_the_frame():
    df = pd.DataFrame({"personal_number": pd.Series([PN, PN + 1], dtype="int64")})
    before = df.copy()
    chat_mod._df_to_xlsx_bytes(df)
    pd.testing.assert_frame_equal(df, before)


def test_table_to_xlsx_writes_integral_identifiers_as_numbers_with_format_0():
    cols = ["personalNumber", "card_no", "amount"]
    rows = [{"personalNumber": PN, "card_no": float(LONG_ID), "amount": 1500},
            {"personalNumber": PN + 1, "card_no": None, "amount": 2500}]
    ws = _sheet(chat_mod._table_to_xlsx_bytes(cols, rows))
    pn = _col(ws, "personalNumber")
    assert [c.data_type for c in pn] == ["n", "n"]
    assert [c.value for c in pn] == [52906123456, 52906123457]
    assert [c.number_format for c in pn] == ["0", "0"]
    card = _col(ws, "card_no")
    assert card[0].data_type == "n" and card[0].value == 1234567890123
    assert card[0].number_format == "0"
    amt = _col(ws, "amount")
    assert [c.data_type for c in amt] == ["n", "n"]
    assert all(c.number_format == "General" for c in amt)


# ---------------------------------------------------------------------------
# PPTX (built-in renderer)
# ---------------------------------------------------------------------------
TABLE = {"columns": ["personal_number", "name", "loan_amount"],
         "rows": [{"personal_number": float(PN), "name": "A", "loan_amount": 1500.5},
                  {"personal_number": float(PN + 1), "name": "B", "loan_amount": 2500.0},
                  {"personal_number": None, "name": "C", "loan_amount": 10.0}],
         "total_rows": 3}


def _qa_pairs(table=TABLE):
    return [{"human": {"content": "Top clients?"},
             "ai": {"content": "Here are the top clients.", "table": table},
             "ai_index": 1, "has_chart": False, "has_table": True}]


STRUCTURE = {"report_title": "Clients", "executive_summary": "Summary",
             "findings": [], "key_takeaways": []}


def _pptx_table_cells(data: bytes) -> list:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    cells = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_table", False) and shape.has_table:
                tbl = shape.table
                for r in tbl.rows:
                    cells.append([c.text for c in r.cells])
    return cells


def test_pptx_builtin_identifier_cells_have_no_decimal_suffix():
    data = report_mod._render_pptx_builtin(_qa_pairs(), STRUCTURE, "sid")
    cells = _pptx_table_cells(data)
    assert cells, "no table in the deck"
    header, *body = cells
    j = header.index("personal_number")
    assert body[0][j] == "52906123456"
    assert body[1][j] == "52906123457"
    assert all(not row[j].endswith(".0") for row in body)
    k = header.index("loan_amount")
    assert body[0][k] == "1500.5"          # a measure: unchanged


def test_pptx_builtin_non_identifier_float_column_is_unchanged():
    table = {"columns": ["total"], "rows": [{"total": 1500.0}], "total_rows": 1}
    cells = _pptx_table_cells(report_mod._render_pptx_builtin(_qa_pairs(table), STRUCTURE, "sid"))
    assert cells[1] == ["1500.0"]


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
@pytest.fixture
def pdf_tables(monkeypatch):
    import reportlab.platypus as platypus
    captured = []

    class FakeTable:
        def __init__(self, data, *a, **k):
            captured.append(data)

        def setStyle(self, *a, **k):
            return None

    class FakeDoc:
        def __init__(self, *a, **k):
            pass

        def build(self, flow):
            return None

    monkeypatch.setattr(platypus, "Table", FakeTable)
    monkeypatch.setattr(platypus, "SimpleDocTemplate", FakeDoc)
    return captured


def test_pdf_identifier_cells_have_no_decimal_suffix(pdf_tables):
    report_mod._render_pdf(_qa_pairs(), STRUCTURE, "sid")
    assert pdf_tables, "no table built"
    header, *body = pdf_tables[0]
    j = list(header).index("personal_number")
    assert body[0][j] == "52906123456"
    assert body[1][j] == "52906123457"
    assert all(not str(row[j]).endswith(".0") for row in body)
    k = list(header).index("loan_amount")
    assert body[0][k] == "1500.5"


def test_pdf_non_identifier_float_column_is_unchanged(pdf_tables):
    table = {"columns": ["total"], "rows": [{"total": 1500.0}], "total_rows": 1}
    report_mod._render_pdf(_qa_pairs(table), STRUCTURE, "sid")
    assert pdf_tables[0][1] == ["1500.0"]
