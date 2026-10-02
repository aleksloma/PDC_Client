"""The "Select from DB" picker is grouped by connection, then schema.

`GET /api/db_tables` rows keep their keys and additionally carry
`connection_id`, `connection_name` and `schema`, so two tables with the same
display name on two connections / schemas can be told apart. Nothing else of
the connection crosses to the browser (no host, port, login or secret), and
the role gate decides which rows are listed exactly as before. Offline.
"""
import json
import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import db_sources
import local_store
import roles_store
from settings import settings

from test_rendering_isolation import _function_body

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "dashboard.js"
CSS = ROOT / "static" / "dashboard.css"
I18N = ROOT / "static" / "i18n.js"

ADMIN = "ladmin"
OWNER = "user@x.com"

HOST_A = "db-host-secret.example"
USER_A = "svc_secret_user"
DB_A = "secret_database_alpha"
HOST_B = "db-host-other.example"
USER_B = "svc_other_login"
DB_B = "secret_database_beta"
PASSWORD = "pw-Secret-Value-91"

BASE_KEYS = {"table_id", "display_name", "description", "row_count",
             "refreshed_at", "mode"}
NEW_KEYS = {"connection_id", "connection_name", "schema"}
FORBIDDEN_KEYS = ("host", "port", "user", "password", "password_enc",
                  "database", "service_name", "url_override")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(ADMIN)
    local_store.AuthStore().set_role(ADMIN, "admin")
    local_store.AuthStore().ensure_user(OWNER)
    roles_store.RolesStore().ensure_base_role()

    import routes.upload as upload_mod
    monkeypatch.setattr(upload_mod.brain_client, "post_activity",
                        lambda *a, **k: None)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(upload_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_0000000000000c01"
        request.session.pop("must_change_password", None)
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


def _add_table(store, conn_id, schema, table_name, display, *, connector=False):
    import pandas as pd
    t = store.upsert_table({
        "connection_id": conn_id, "schema": schema, "table_name": table_name,
        "display_name": display, "description": f"{display} description",
        "is_connector": connector, "relations": [],
        "columns": [{"name": "city_code", "dtype": "int",
                     "description": "", "indexed": True}],
    }, actor=ADMIN)
    pd.DataFrame({"city_code": [1, 2]}).to_parquet(
        local_store.db_snapshot_path(t["id"]))
    return t["id"]


@pytest.fixture
def registry(client):
    """Two connections. `orders` exists on BOTH (different schemas), a
    schema-less table and a connector sit on the first."""
    store = db_sources.DataSourceStore()
    a = store.create_connection(
        {"name": "Warehouse A", "db_type": "postgresql", "host": HOST_A,
         "port": 5432, "database": DB_A, "user": USER_A}, PASSWORD, actor=ADMIN)
    b = store.create_connection(
        {"name": "Warehouse B", "db_type": "postgresql", "host": HOST_B,
         "port": 6543, "database": DB_B, "user": USER_B}, PASSWORD, actor=ADMIN)
    ids = {"conn_a": a["id"], "conn_b": b["id"]}
    ids["orders_a"] = _add_table(store, a["id"], "shop", "orders", "orders")
    ids["orders_b"] = _add_table(store, b["id"], "sales", "orders", "orders")
    ids["noschema"] = _add_table(store, a["id"], "", "flat", "flat table")
    ids["connector"] = _add_table(store, a["id"], "shop", "city_dict",
                                  "cities dictionary", connector=True)
    return ids


def _grant(role_fields):
    role = roles_store.RolesStore().create_role(role_fields, actor=ADMIN)
    local_store.AuthStore().set_data_role(OWNER, role["id"])
    return role


def _grant_everything(registry):
    _grant({"name": "All", "scope_grants": [
        {"connection_id": registry["conn_a"], "schema": None},
        {"connection_id": registry["conn_b"], "schema": None}]})


def _rows(client) -> dict:
    r = client.get("/api/db_tables")
    assert r.status_code == 200, r.text
    return {t["table_id"]: t for t in r.json()["tables"]}


# ── the route ──────────────────────────────────────────────────────────────

def test_rows_keep_their_keys_and_gain_connection_and_schema(client, registry):
    _grant_everything(registry)
    rows = _rows(client)
    row = rows[registry["orders_a"]]
    assert BASE_KEYS <= set(row), sorted(row)
    assert NEW_KEYS <= set(row), sorted(row)
    assert row["display_name"] == "orders"
    assert row["connection_id"] == registry["conn_a"]
    assert row["connection_name"] == "Warehouse A"
    assert row["schema"] == "shop"


def test_same_display_name_on_two_connections_is_distinguishable(client, registry):
    _grant_everything(registry)
    rows = _rows(client)
    a, b = rows[registry["orders_a"]], rows[registry["orders_b"]]
    assert a["display_name"] == b["display_name"] == "orders"
    assert (a["connection_id"], a["connection_name"], a["schema"]) == \
        (registry["conn_a"], "Warehouse A", "shop")
    assert (b["connection_id"], b["connection_name"], b["schema"]) == \
        (registry["conn_b"], "Warehouse B", "sales")


def test_a_table_without_a_schema_reports_an_empty_string(client, registry):
    _grant_everything(registry)
    row = _rows(client)[registry["noschema"]]
    assert row["schema"] == ""
    assert row["connection_name"] == "Warehouse A"


def test_a_table_whose_connection_is_gone_still_lists(client, registry):
    """An explicit table grant survives the connection vanishing from the
    registry; the row is listed with an empty connection name."""
    _grant({"name": "R", "table_ids": [registry["orders_b"],
                                       registry["orders_a"]]})
    store = db_sources.DataSourceStore()
    with db_sources._LOCK:
        doc = store.read_doc()
        doc["connections"] = [c for c in doc["connections"]
                              if c.get("id") != registry["conn_b"]]
        store._write_doc(doc)

    rows = _rows(client)
    assert registry["orders_b"] in rows, sorted(rows)
    orphan = rows[registry["orders_b"]]
    assert orphan["connection_name"] == ""
    assert orphan["schema"] == "sales"
    # The table on the surviving connection is unaffected.
    assert rows[registry["orders_a"]]["connection_name"] == "Warehouse A"


def _all_keys(value) -> set:
    out = set()
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(k)
            out |= _all_keys(v)
    elif isinstance(value, list):
        for v in value:
            out |= _all_keys(v)
    return out


def test_no_connection_detail_beyond_id_and_name(client, registry):
    _grant_everything(registry)
    r = client.get("/api/db_tables")
    assert r.status_code == 200
    body = r.json()
    assert body["tables"], "the fixture grants every table"
    leaked = _all_keys(body) & set(FORBIDDEN_KEYS)
    assert not leaked, sorted(leaked)
    text = r.text
    for value in (HOST_A, USER_A, DB_A, HOST_B, USER_B, DB_B, PASSWORD):
        assert value not in text, value
    assert "postgresql" not in text


def test_base_role_still_sees_nothing(client, registry):
    r = client.get("/api/db_tables")
    assert r.status_code == 200
    assert r.json()["tables"] == []


def test_one_granted_table_is_the_only_row_and_carries_the_new_keys(client, registry):
    _grant({"name": "R", "table_ids": [registry["orders_b"]]})
    r = client.get("/api/db_tables")
    assert r.status_code == 200
    tables = r.json()["tables"]
    assert [t["table_id"] for t in tables] == [registry["orders_b"]]
    row = tables[0]
    assert row["connection_id"] == registry["conn_b"]
    assert row["connection_name"] == "Warehouse B"
    assert row["schema"] == "sales"


def test_connectors_are_never_listed(client, registry):
    _grant_everything(registry)
    rows = _rows(client)
    assert registry["connector"] not in rows
    assert "cities dictionary" not in {t["display_name"] for t in rows.values()}
    assert set(rows) == {registry["orders_a"], registry["orders_b"],
                         registry["noschema"]}


# ── structural: the picker renders the groups ──────────────────────────────

def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _lang_blocks() -> dict:
    src = _read(I18N).replace("\r\n", "\n")
    starts = {lang: src.index(f"\n  {lang}: {{") for lang in ("en", "geo", "ru")}
    order = sorted(starts, key=starts.get)
    out = {}
    for i, lang in enumerate(order):
        end = starts[order[i + 1]] if i + 1 < len(order) else src.index("\n};")
        out[lang] = src[starts[lang]:end]
    return out


def test_the_picker_renderer_emits_connection_and_schema_headers():
    src = _read(JS)
    assert "db-group-conn" in src
    assert "db-group-schema" in src
    render = _function_body(src, "_renderDbTableList")
    assert render, "_renderDbTableList missing"
    assert "connection_name" in render
    assert "schema" in render


def test_the_stylesheet_defines_both_group_headers():
    css = _read(CSS)
    assert re.search(r"\.db-group-conn\b[^{}]*\{", css)
    assert re.search(r"\.db-group-schema\b[^{}]*\{", css)


@pytest.mark.parametrize("key", ["wizard.db_unknown_connection",
                                 "wizard.db_default_schema"])
def test_group_fallback_labels_exist_in_all_three_languages(key):
    src = _read(I18N)
    pattern = rf"""['"]{re.escape(key)}['"]\s*:"""
    assert len(re.findall(pattern, src)) == 3, key
    for lang, block in _lang_blocks().items():
        assert re.search(pattern, block), (key, lang)
