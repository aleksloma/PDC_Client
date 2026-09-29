"""An .xlsx decompression bomb is refused before any parser opens it.

`excel_table_detector.inspect_xlsx_archive` reads only the zip's central
directory and each sheet's `<dimension>` element: the total uncompressed size
(XLSX_MAX_UNCOMPRESSED_MB), every entry's compression ratio
(XLSX_MAX_COMPRESSION_RATIO) and the declared cell count (XLSX_MAX_CELLS).
`load_excel_sheets` calls it first, and the upload report shows a fixed
message. A normal workbook still loads; .xls (not a zip) is not inspected.
"""
import zipfile

import openpyxl
import pytest

import excel_table_detector as etd
import local_store
from settings import settings

_CT = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
       'content-types"/>')


def _bomb(path, *, sheet_bytes=0, dimension="A1:B2"):
    head = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
            f'spreadsheetml/2006/main"><dimension ref="{dimension}"/><sheetData>').encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("xl/workbook.xml", "<workbook/>")
        with z.open("xl/worksheets/sheet1.xml", "w") as fh:
            fh.write(head)
            chunk = b" " * (1024 * 1024)
            for _ in range(sheet_bytes // len(chunk)):
                fh.write(chunk)
            fh.write(b"</sheetData></worksheet>")
    return path


@pytest.fixture
def no_parser(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a parser was reached")

    monkeypatch.setattr(openpyxl, "load_workbook", refuse)


def test_a_high_ratio_sheet_is_refused_before_parsing(tmp_path, no_parser, monkeypatch):
    monkeypatch.setattr(settings, "XLSX_MAX_UNCOMPRESSED_MB", 500)
    path = _bomb(tmp_path / "bomb.xlsx", sheet_bytes=64 * 1024 * 1024)
    assert path.stat().st_size < 1024 * 1024
    with pytest.raises(etd.ExcelArchiveRejected):
        etd.load_excel_sheets(path, "bomb.xlsx")


def test_the_total_uncompressed_size_is_capped(tmp_path, no_parser, monkeypatch):
    monkeypatch.setattr(settings, "XLSX_MAX_UNCOMPRESSED_MB", 8)
    monkeypatch.setattr(settings, "XLSX_MAX_COMPRESSION_RATIO", 10_000)
    path = _bomb(tmp_path / "big.xlsx", sheet_bytes=16 * 1024 * 1024)
    with pytest.raises(etd.ExcelArchiveRejected):
        etd.load_excel_sheets(path, "big.xlsx")


def test_a_declared_cell_count_over_the_cap_is_refused(tmp_path, no_parser, monkeypatch):
    monkeypatch.setattr(settings, "XLSX_MAX_CELLS", 20_000_000)
    path = _bomb(tmp_path / "wide.xlsx", dimension="A1:ZZ9999999")
    with pytest.raises(etd.ExcelArchiveRejected):
        etd.load_excel_sheets(path, "wide.xlsx")


def test_the_upload_report_shows_the_fixed_message(tmp_path, no_parser, caplog):
    path = _bomb(tmp_path / "bomb.xlsx", sheet_bytes=64 * 1024 * 1024)
    report: list = []
    out = local_store._load_one_file(path, report)
    assert out == {}
    assert report and report[0]["message"] == etd.ARCHIVE_REJECTED_TEXT, report
    assert "XLSX_ARCHIVE_REJECTED" in caplog.text
    assert "reason=ratio" in caplog.text


def _real_workbook(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["id", "city", "amount"])
    for i in range(rows):
        ws.append([i, f"city-{i % 97}", i * 1.5])
    wb.save(path)
    return path


def _declare_sheet_size(path, declared: int):
    """Rewrite the sheet entry's uncompressed size in BOTH the local header
    and the central directory, leaving the data and the CRC honest — the
    header lie the audit reproduced."""
    import struct
    data = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("xl/worksheets/sheet1.xml")
        start_dir = z.start_dir
    struct.pack_into("<I", data, info.header_offset + 22, declared)
    name = b"xl/worksheets/sheet1.xml"
    pos = start_dir
    while True:
        pos = data.index(b"PK\x01\x02", pos)
        name_len = struct.unpack_from("<H", data, pos + 28)[0]
        if bytes(data[pos + 46:pos + 46 + name_len]) == name:
            struct.pack_into("<I", data, pos + 24, declared)
            break
        pos += 4
    path.write_bytes(bytes(data))
    return path


def test_a_lied_uncompressed_size_is_measured_not_trusted(tmp_path, no_parser, monkeypatch):
    """The central directory says 1 KB; the sheet really expands to several
    MB. The inspector decompresses and counts, so the cap applies to the real
    size."""
    path = _real_workbook(tmp_path / "lie.xlsx", 60_000)
    with zipfile.ZipFile(path) as z:
        real = z.getinfo("xl/worksheets/sheet1.xml").file_size
    assert real > 2 * 1024 * 1024
    _declare_sheet_size(path, 1024)
    monkeypatch.setattr(settings, "XLSX_MAX_UNCOMPRESSED_MB", 1)
    with pytest.raises(etd.ExcelArchiveRejected):
        etd.load_excel_sheets(path, "lie.xlsx")


def test_a_size_mismatch_under_the_cap_is_refused(tmp_path, no_parser, caplog):
    path = _declare_sheet_size(_real_workbook(tmp_path / "lie2.xlsx", 5_000), 4096)
    with pytest.raises(etd.ExcelArchiveRejected):
        etd.inspect_workbook_if_zip(str(path), "lie2.xlsx")
    assert "reason=size_mismatch" in caplog.text or "reason=unreadable" in caplog.text


def test_too_many_entries_are_refused(tmp_path, no_parser, monkeypatch):
    monkeypatch.setattr(etd, "_MAX_ARCHIVE_ENTRIES", 5)
    path = tmp_path / "many.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(6):
            z.writestr(f"xl/part{i}.xml", "<x/>")
    with pytest.raises(etd.ExcelArchiveRejected):
        etd.inspect_workbook_if_zip(str(path), "many.xlsx")


def test_an_unexpected_compression_method_is_refused(tmp_path, no_parser):
    path = tmp_path / "bz.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_BZIP2) as z:
        z.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
    with pytest.raises(etd.ExcelArchiveRejected):
        etd.inspect_workbook_if_zip(str(path), "bz.xlsx")


def test_a_normal_workbook_still_loads(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["city", "sales"])
    for i in range(20):
        ws.append([f"c{i}", i])
    path = tmp_path / "ok.xlsx"
    wb.save(path)
    etd.inspect_workbook_if_zip(str(path), "ok.xlsx")      # no raise
    frames = etd.load_excel_sheets(path, "ok.xlsx")
    assert frames and list(next(iter(frames.values())).columns) == ["city", "sales"]


def test_an_xls_name_is_not_inspected(tmp_path):
    path = tmp_path / "old.xls"
    path.write_bytes(b"not a zip at all")
    etd.inspect_workbook_if_zip(str(path), "old.xls")        # no raise


def test_the_defaults(monkeypatch):
    for name in ("XLSX_MAX_UNCOMPRESSED_MB", "XLSX_MAX_COMPRESSION_RATIO", "XLSX_MAX_CELLS"):
        monkeypatch.delenv(name, raising=False)
    from settings import Settings
    s = Settings()
    assert (s.XLSX_MAX_UNCOMPRESSED_MB, s.XLSX_MAX_COMPRESSION_RATIO, s.XLSX_MAX_CELLS) == \
        (500, 100, 20_000_000)


def test_the_add_data_column_probe_refuses_a_bomb_before_openpyxl(tmp_path, no_parser):
    """/api/chat/{id}/probe_columns opens the uploaded bytes with openpyxl;
    the archive check runs first and the probe answers None (no comparison)."""
    import routes.chat as chat_mod
    data = _bomb(tmp_path / "bomb.xlsx", sheet_bytes=64 * 1024 * 1024).read_bytes()
    assert chat_mod._probe_structure(data, "bomb.xlsx") is None
