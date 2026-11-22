"""P31 Part D §1 / §7 — `identifier_columns` on the web app's table payloads.

The web app computes `ident_rule.source_identifier_columns(dfs)` once per turn
where the frames are loaded, and the table payload builders
(`run_chat_local._build_table_from_result` / `_build_tables_from_result`) add
an ADDITIVE `identifier_columns: [...]` — ONLY when non-empty — decided by
`ident_rule.identifier_columns(result columns, source idents)`: the name rule
or a case-insensitive match with a source identifier, never a value rule on a
result. A table without identifiers keeps today's shape byte-identical, and
old stored rows without the key keep working (the browser falls back to its
name rule).

Every path that builds a table carries it: both chat flows, the full-table
route (`full_table_get`), the per-item refresh (`run_item_refresh`), and the
dashboard tile refresh / pin / serve (`routes/dashboards.py`). The Excel
download of the same record writes those columns as text.

§7 logs: at most counts, never an identifier VALUE.

Offline: DATA_ROOT is tmp_path, brain calls stubbed, python in process.
"""
import asyncio
import io
import json
import logging

import openpyxl
import pandas as pd
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
import logger_utils
import run_chat_local
from conftest import JSON_HEADERS, seed_history
from settings import settings

OWNER = "alice@acme.com"
CHAT = "c_p31identpay01"
PN0 = 52906123456
CSV = "clients.csv"
TABLE_CODE = f"RESULT = dfs['{CSV}'][['personal_number', 'client_since', 'loan_amount']].head(5)"
PLAIN_CODE = f"RESULT = dfs['{CSV}'][['name', 'loan_amount']].head(5)"


def clients(n=40) -> pd.DataFrame:
    limits = [500, 1500, 3000, 25000, 100000, 750]
    return pd.DataFrame({
        "personal_number": pd.Series([PN0 + i * 1013 for i in range(n)], dtype="int64"),
        "name": [f"Client {i}" for i in range(n)],
        "client_since": [1995 + (i % 30) for i in range(n)],
        "age": [18 + (i * 7) % 60 for i in range(n)],
        "credit_limit_gel": [limits[i % len(limits)] for i in range(n)],
        "loan_amount": [12345.67 + i * 101.3 for i in range(n)],
    })


def _tables_in(obj):
    """Every table-shaped dict (`columns` + `rows`) anywhere in `obj`."""
    out = []
    if isinstance(obj, dict):
        if "columns" in obj and "rows" in obj:
            out.append(obj)
        for v in obj.values():
            out.extend(_tables_in(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_tables_in(v))
    return out


# ---------------------------------------------------------------------------
# the payload builders
# ---------------------------------------------------------------------------
def test_table_builder_adds_identifier_columns_for_a_personal_number():
    df = clients()[["personal_number", "loan_amount"]].head(5)
    table = run_chat_local._build_table_from_result(df)
    assert table["identifier_columns"] == ["personal_number"]


def test_table_builder_without_identifiers_keeps_the_old_shape():
    df = pd.DataFrame({"region": ["N", "S"], "total": [1234567, 89]})
    table = run_chat_local._build_table_from_result(df)
    assert "identifier_columns" not in table
    assert table == {"columns": ["region", "total"],
                     "rows": [{"region": "N", "total": 1234567},
                              {"region": "S", "total": 89}],
                     "total_rows": 2}


def test_table_builder_styler_carries_both_styled_html_and_identifier_columns():
    df = clients()[["personal_number", "loan_amount"]].head(5)
    table = run_chat_local._build_table_from_result(df.style.format(thousands=","))
    assert table.get("styled_html")
    assert table["identifier_columns"] == ["personal_number"]


def test_table_builder_series_result():
    s = clients().set_index("personal_number")["loan_amount"].head(3)
    table = run_chat_local._build_table_from_result(s)
    assert table["identifier_columns"] == ["personal_number"]


def test_table_builder_applies_no_value_rule_to_a_result():
    """A RESULT column of 11-digit unique integers is not an identifier unless
    its NAME says so (or it is a source identifier)."""
    df = pd.DataFrame({"total_balance": [PN0 + i * 7 for i in range(30)],
                       "region": [f"R{i}" for i in range(30)]})
    table = run_chat_local._build_table_from_result(df)
    assert "identifier_columns" not in table


def test_multi_table_builder_marks_each_table_by_its_own_columns():
    tables = run_chat_local._build_tables_from_result({
        "top": clients()[["personal_number", "loan_amount"]].head(3),
        "regions": pd.DataFrame({"region": ["N", "S"], "total": [1, 2]}),
    })
    assert [t["title"] for t in tables] == ["top", "regions"]
    assert tables[0]["identifier_columns"] == ["personal_number"]
    assert "identifier_columns" not in tables[1]


# ---------------------------------------------------------------------------
# both chat flows (source identifiers computed from the turn's frames)
# ---------------------------------------------------------------------------
@pytest.fixture
def brain(monkeypatch):
    state = {"plan": None}
    bc = run_chat_local.brain_client
    monkeypatch.setattr(bc, "plan", lambda **kw: dict(state["plan"]))
    monkeypatch.setattr(bc, "retry", lambda **kw: {"kind": "NO_CODE", "code": "", "usage": {}})
    monkeypatch.setattr(bc, "describe", lambda **kw: {"text": "described", "usage": {}})
    monkeypatch.setattr(bc, "summarize", lambda **kw: {"text": "summarized", "usage": {}})
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")
    return state


def _python_plan(code):
    return {"raw_text": "", "kind": "PYTHON", "code": code, "usage": {},
            "context_decision": {}}


def test_single_flow_table_carries_name_and_source_identifiers(brain):
    brain["plan"] = _python_plan(TABLE_CODE)
    out = run_chat_local.run_chat(sid="t", dfs={CSV: clients()}, schema_docs={},
                                  question="top clients?", history_rows=[],
                                  user_email=OWNER)
    assert out["table"]["identifier_columns"] == ["personal_number", "client_since"]


def test_multi_flow_table_carries_name_and_source_identifiers(brain):
    brain["plan"] = _python_plan(TABLE_CODE)
    events = list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs={CSV: clients()}, schema_docs={}, question="top clients?",
        history_rows=[], user_email=OWNER))
    tables = _tables_in(events)
    assert tables, events
    for t in tables:
        assert t.get("identifier_columns") == ["personal_number", "client_since"], t


def test_multi_flow_table_without_identifiers_has_no_key(brain):
    brain["plan"] = _python_plan(PLAIN_CODE)
    events = list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs={CSV: clients()}, schema_docs={}, question="loans?",
        history_rows=[], user_email=OWNER))
    tables = _tables_in(events)
    assert tables, events
    assert all("identifier_columns" not in t for t in tables), tables


# ---------------------------------------------------------------------------
# routes: full_table, download_excel, refresh_item, dashboards
# ---------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(OWNER)
    store = local_store.ChatDataStore(CHAT)
    clients().to_csv(store.files_dir / CSV, index=False)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["title"] = "Clients"
    meta["files"] = [{"file_name": CSV, "schema": {}}]
    store.write_meta(meta)

    import routes.chat as chat_mod
    import routes.dashboards as dash_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)
    app.include_router(dash_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}", headers=JSON_HEADERS)
    yield {"client": tc, "store": store, "chat_mod": chat_mod, "dash_mod": dash_mod}
    local_store._DATAFRAME_CACHE.invalidate()


def _full_record(store, code, key="0123456789abcdef", result_key=None):
    full = store.conversations_dir / "full"
    full.mkdir(parents=True, exist_ok=True)
    rec = {"columns": ["x"], "rows": [{"x": 1}], "total_rows": 1, "code": code}
    if result_key is not None:
        rec["result_key"] = result_key
    (full / f"{key}.json").write_text(json.dumps(rec), encoding="utf-8")
    return key


def test_full_table_route_carries_identifier_columns(world):
    key = _full_record(world["store"], TABLE_CODE)
    r = world["client"].get(f"/api/chat/{CHAT}/full_table/{key}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"] == ["personal_number", "client_since", "loan_amount"]
    assert body["identifier_columns"] == ["personal_number", "client_since"]
    # the JSON numbers themselves are untouched (display is the browser's job)
    assert body["rows"][0]["personal_number"] == PN0


def test_full_table_route_without_identifiers_keeps_the_old_keys(world):
    key = _full_record(world["store"], PLAIN_CODE)
    r = world["client"].get(f"/api/chat/{CHAT}/full_table/{key}")
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"columns", "rows", "total_rows"}


def test_download_excel_writes_identifier_columns_as_plain_numbers(world):
    """Evaluator review (deliberate update): an integral identifier is a
    NUMERIC cell with number_format "0" (every digit shown, still a number),
    not a text cell; a measure keeps the General format."""
    key = _full_record(world["store"], TABLE_CODE)
    r = world["client"].post(f"/api/chat/{CHAT}/download_excel/{key}", json={})
    assert r.status_code == 200, r.text
    ws = openpyxl.load_workbook(io.BytesIO(r.content)).active
    heads = [c.value for c in ws[1]]
    pn = ws.cell(row=2, column=heads.index("personal_number") + 1)
    assert pn.data_type == "n" and pn.value == PN0 and pn.number_format == "0"
    since = ws.cell(row=2, column=heads.index("client_since") + 1)
    assert since.data_type == "n" and since.value == 1995
    amt = ws.cell(row=2, column=heads.index("loan_amount") + 1)
    assert amt.data_type == "n" and amt.number_format == "General"


def test_run_item_refresh_table_carries_identifier_columns(world):
    chat_mod = world["chat_mod"]
    out = asyncio.run(chat_mod.run_item_refresh(CHAT, TABLE_CODE, "table", "sid"))
    assert out["ok"] is True, out
    assert out["table"]["identifier_columns"] == ["personal_number", "client_since"]


def test_refresh_item_route_carries_identifier_columns(world):
    seed_history(CHAT, TABLE_CODE)
    r = world["client"].post(f"/api/chat/{CHAT}/refresh_item",
                             json={"code": TABLE_CODE, "kind": "table"})
    assert r.status_code == 200, r.text
    assert r.json()["table"]["identifier_columns"] == ["personal_number", "client_since"]


def _table_tile(code, result_key=None, snapshot_table=None):
    ds = local_store.DashboardStore()
    row = ds.create_dashboard(OWNER, "Board")
    tile = {"chat_id": CHAT, "kind": "table", "code": code,
            "snapshot": {"table": snapshot_table or
                         {"columns": ["x"], "rows": [{"x": 1}], "total_rows": 1}}}
    if result_key is not None:
        tile["result_key"] = result_key
    tile = ds.add_tile(OWNER, row["dash_id"], tile)
    return row["dash_id"], tile["tile_id"]


def _stored_tile(dash_id, tile_id):
    doc = local_store.DashboardStore().get_dashboard(OWNER, dash_id) or {}
    return next(t for t in doc.get("tiles") or [] if t.get("tile_id") == tile_id)


def test_dashboard_tile_refresh_carries_and_stores_identifier_columns(world):
    dash_id, tile_id = _table_tile(TABLE_CODE)
    r = world["client"].post(f"/api/dashboards/{dash_id}/tiles/{tile_id}/refresh", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert body["table"]["identifier_columns"] == ["personal_number", "client_since"]
    snap = _stored_tile(dash_id, tile_id)["snapshot"]["table"]
    assert snap["identifier_columns"] == ["personal_number", "client_since"]


def test_dashboard_multi_table_tile_refresh_carries_identifier_columns(world):
    code = (f"RESULT = {{'top': dfs['{CSV}'][['personal_number', 'loan_amount']].head(3), "
            f"'n': dfs['{CSV}'][['loan_amount']].head(3)}}")
    dash_id, tile_id = _table_tile(code, result_key="top")
    r = world["client"].post(f"/api/dashboards/{dash_id}/tiles/{tile_id}/refresh", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert body["table"]["identifier_columns"] == ["personal_number"]
    snap = _stored_tile(dash_id, tile_id)["snapshot"]["table"]
    assert snap["identifier_columns"] == ["personal_number"]


def test_dashboard_pin_keeps_identifier_columns_and_serves_them(world):
    seed_history(CHAT, TABLE_CODE)
    client = world["client"]
    r = client.post("/api/dashboards", json={"name": "Board"})
    dash_id = r.json()["dash_id"]
    table = {"columns": ["personal_number", "loan_amount"],
             "rows": [{"personal_number": PN0, "loan_amount": 1.5}],
             "total_rows": 1, "identifier_columns": ["personal_number"]}
    r = client.post(f"/api/dashboards/{dash_id}/tiles",
                    json={"chat_id": CHAT, "kind": "table", "description": "d",
                          "code": TABLE_CODE, "table": table})
    assert r.status_code == 200, r.text
    r = client.get(f"/api/dashboards/{dash_id}")
    assert r.status_code == 200, r.text
    tiles = r.json()["tiles"]
    assert tiles[0]["snapshot"]["table"]["identifier_columns"] == ["personal_number"]


def test_dashboard_old_tile_without_the_key_is_served_unchanged(world):
    old = {"columns": ["personal_number"], "rows": [{"personal_number": PN0}], "total_rows": 1}
    dash_id, _tile_id = _table_tile(TABLE_CODE, snapshot_table=old)
    r = world["client"].get(f"/api/dashboards/{dash_id}")
    assert r.status_code == 200, r.text
    served = r.json()["tiles"][0]["snapshot"]["table"]
    assert served == old


# ---------------------------------------------------------------------------
# §7 logs: never an identifier VALUE
# ---------------------------------------------------------------------------
@pytest.fixture
def all_logs():
    logger_utils.get_logger()
    lines = []

    class _Capture(logging.Handler):
        def emit(self, record):
            try:
                lines.append(record.getMessage())
            except Exception:
                lines.append(str(record.msg))

    handler = _Capture(level=logging.DEBUG)
    lg = logging.getLogger("datachat")
    lg.addHandler(handler)
    try:
        yield lines
    finally:
        lg.removeHandler(handler)


def _values_in(lines):
    needles = [str(PN0 + i * 1013) for i in range(40)]
    needles += [f"{PN0 + i * 1013:,}" for i in range(40)]
    return [l for l in lines if any(n in l for n in needles)]


def test_no_log_line_carries_an_identifier_value_through_a_turn(brain, all_logs):
    brain["plan"] = _python_plan(TABLE_CODE)
    list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs={CSV: clients()}, schema_docs={}, question="top clients?",
        history_rows=[], user_email=OWNER))
    run_chat_local.run_chat(sid="t", dfs={CSV: clients()}, schema_docs={},
                            question="top clients?", history_rows=[], user_email=OWNER)
    assert all_logs, "nothing was logged — the capture is not wired"
    assert _values_in(all_logs) == []


def test_no_log_line_carries_an_identifier_value_through_the_routes(world, all_logs):
    key = _full_record(world["store"], TABLE_CODE)
    client = world["client"]
    assert client.get(f"/api/chat/{CHAT}/full_table/{key}").status_code == 200
    assert client.post(f"/api/chat/{CHAT}/download_excel/{key}", json={}).status_code == 200
    dash_id, tile_id = _table_tile(TABLE_CODE)
    client.post(f"/api/dashboards/{dash_id}/tiles/{tile_id}/refresh", json={})
    assert _values_in(all_logs) == []


def test_ident_log_lines_carry_counts_only(brain, all_logs):
    brain["plan"] = _python_plan(TABLE_CODE)
    list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs={CSV: clients()}, schema_docs={}, question="top clients?",
        history_rows=[], user_email=OWNER))
    for line in all_logs:
        if "IDENT_COLUMNS" in line:
            assert "n=" in line, line
            assert str(PN0) not in line
