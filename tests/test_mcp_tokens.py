"""MCP access tokens (`mcp_tokens.py`): the token store.

The contract pinned here (MCP integration, stage 1):

* Store: `DATA_ROOT/mcp_tokens.json` = `{"version": 1, "tokens": [...]}`. A
  missing file means the feature is off; no read creates it. Only
  sha256(raw token) is stored; the raw token ("pdcmcp_" + 43 url-safe
  characters) is returned once, by `create_token`.
* `create_token` refuses bad input with a ValueError carrying a plain
  sentence and writes nothing: name (1-60 characters after strip, no control
  character, unique among ACTIVE tokens, case-insensitive), mode, and for
  shared_account an existing account that is not the bootstrap admin.
* `list_tokens` newest first, public keys only. `revoke_token` is idempotent.
* `verify` answers the stored record of an active token and None for anything
  else; it never raises and uses `hmac.compare_digest` over every record.
* `touch` stamps `last_used_at` at most once per 60 s per token.
* A write that meets an existing file it cannot read refuses: the stored
  tokens are never replaced by an empty document.
* No log line carries a raw token or its hash.

Offline: DATA_ROOT is tmp_path. The module is imported inside a fixture, so a
missing module fails the tests, not the collection.
"""
import ast
import hashlib
import hmac
import importlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import local_store
import logger_utils
from settings import settings

ROOT = Path(__file__).resolve().parent.parent
ADMIN = "ladmin"
BOT = "bot@corp.example"

RAW_RE = re.compile(r"pdcmcp_[A-Za-z0-9_-]{43}")
PUBLIC_KEYS = {"id", "name", "mode", "account", "created_at", "created_by",
               "last_used_at", "revoked_at", "revoked_by", "active"}
RAW_A = "pdcmcp_" + "A" * 43
RAW_B = "pdcmcp_" + "B" * 43
RAW_C = "pdcmcp_" + "C" * 43


# ===========================================================================
# helpers
# ===========================================================================
def _sha(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file(root):
    return root / "mcp_tokens.json"


def _doc(root):
    return json.loads(_file(root).read_text(encoding="utf-8"))


def _record(raw, *, tid="aaaaaaaaaaaaaaaa", name="Helpdesk bot", **over):
    rec = {"id": tid, "name": name, "mode": "per_user", "token_hash": _sha(raw),
           "created_at": "2026-11-20T10:00:00+00:00", "created_by": ADMIN,
           "last_used_at": None, "revoked_at": None, "revoked_by": None}
    rec.update(over)
    return rec


def _seed(root, records):
    _file(root).write_text(json.dumps({"version": 1, "tokens": records}),
                           encoding="utf-8")


def _plain_sentence(exc):
    text = str(exc.value)
    assert " " in text and text[0].isupper() and text.endswith("."), text
    assert "Traceback" not in text and "Error" not in text, text


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass


@pytest.fixture
def logs():
    """Every line written through `log_with_sid`, whichever module emits it."""
    logger = logger_utils.get_logger()
    handler = _Capture()
    old_level = logger.level
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", ADMIN)
    local_store._SESSION_GEN_CACHE.clear()
    yield tmp_path
    local_store._SESSION_GEN_CACHE.clear()


@pytest.fixture
def mt(root):
    """The store module (a missing module fails the test)."""
    import mcp_tokens
    return mcp_tokens


@pytest.fixture
def clock(mt, monkeypatch):
    """The module clock, under the test's control."""
    now = {"t": 1_800_000_000.0}
    monkeypatch.setattr(mt, "_clock", lambda: now["t"])
    return now


# ===========================================================================
# create
# ===========================================================================
def test_create_returns_the_public_row_and_the_raw_token_once(mt, root):
    row, raw = mt.create_token("  Helpdesk bot  ", "per_user", ADMIN)
    assert RAW_RE.fullmatch(raw), "unexpected raw token shape"
    assert set(row) == PUBLIC_KEYS
    assert row["name"] == "Helpdesk bot"
    assert row["mode"] == "per_user"
    assert row["account"] is None
    assert row["created_by"] == ADMIN
    assert re.fullmatch(r"[0-9a-f]{16}", row["id"])
    assert row["active"] is True
    assert row["last_used_at"] is None
    assert row["revoked_at"] is None and row["revoked_by"] is None
    stamp = datetime.fromisoformat(row["created_at"])
    assert stamp.utcoffset() == timedelta(0), "created_at must be UTC"
    assert raw not in json.dumps(row)
    assert _sha(raw) not in json.dumps(row)


def test_the_file_holds_the_hash_and_never_the_raw_token(mt, root):
    row, raw = mt.create_token("Helpdesk bot", "per_user", ADMIN)
    text = _file(root).read_text(encoding="utf-8")
    assert raw not in text
    assert raw[len("pdcmcp_"):] not in text
    doc = json.loads(text)
    assert doc["version"] == 1
    assert len(doc["tokens"]) == 1
    rec = doc["tokens"][0]
    assert rec["token_hash"] == _sha(raw)
    assert rec["id"] == row["id"] and rec["name"] == "Helpdesk bot"
    assert rec["mode"] == "per_user"
    assert rec["last_used_at"] is None
    assert rec["revoked_at"] is None and rec["revoked_by"] is None


def test_every_token_is_different(mt):
    made = [mt.create_token(f"Bot {i}", "per_user", ADMIN) for i in range(5)]
    assert len({raw for _, raw in made}) == 5
    assert len({row["id"] for row, _ in made}) == 5


def test_a_shared_account_token_binds_the_account_lowercased(mt, root):
    store = local_store.AuthStore()
    store.create_account(BOT)
    row, raw = mt.create_token("Shared", "shared_account", ADMIN,
                               account="  Bot@Corp.EXAMPLE ")
    assert row["account"] == BOT
    assert set(row) == PUBLIC_KEYS, "account_created_at is not public"
    rec = _doc(root)["tokens"][0]
    assert rec["account"] == BOT
    assert rec["account_created_at"] == store.get_profile(BOT)["created_at"]


@pytest.mark.parametrize("account", [None, BOT, "junk", 5])
def test_per_user_ignores_and_stores_no_account(mt, root, account):
    row, _ = mt.create_token("Per user", "per_user", ADMIN, account=account)
    assert row["account"] is None
    rec = _doc(root)["tokens"][0]
    assert not rec.get("account")
    assert not rec.get("account_created_at")


@pytest.mark.parametrize("name", [
    "", "   ", "x" * 61, "a\nb", "a\rb", "a\tb", "a\x00b", "a\x1bb", "a\x7fb",
    "a\x85b", None, 5, ["a"],
], ids=["empty", "blank", "61", "lf", "cr", "tab", "nul", "esc", "del", "c1",
        "none", "int", "list"])
def test_a_bad_name_is_refused_and_nothing_is_written(mt, root, name):
    with pytest.raises(ValueError) as exc:
        mt.create_token(name, "per_user", ADMIN)
    _plain_sentence(exc)
    assert not _file(root).exists()


@pytest.mark.parametrize("name", ["x", "x" * 60, "  " + "y" * 60 + "  ",
                                  "ჩატბოტი", "Чат-бот №1"],
                         ids=["1", "60", "60-after-strip", "ka", "ru"])
def test_a_good_name_is_accepted(mt, name):
    row, _ = mt.create_token(name, "per_user", ADMIN)
    assert row["name"] == name.strip()


@pytest.mark.parametrize("again", ["Helpdesk bot", "HELPDESK BOT", "  helpdesk Bot "])
def test_a_name_of_an_active_token_is_refused(mt, root, again):
    mt.create_token("Helpdesk bot", "per_user", ADMIN)
    before = _file(root).read_bytes()
    with pytest.raises(ValueError) as exc:
        mt.create_token(again, "per_user", ADMIN)
    _plain_sentence(exc)
    assert _file(root).read_bytes() == before


def test_a_revoked_name_can_be_used_again(mt):
    old, old_raw = mt.create_token("Helpdesk bot", "per_user", ADMIN)
    mt.revoke_token(old["id"], ADMIN)
    new, new_raw = mt.create_token("helpdesk bot", "per_user", ADMIN)
    assert new["id"] != old["id"]
    assert mt.verify(new_raw)["id"] == new["id"]
    assert mt.verify(old_raw) is None
    assert len(mt.list_tokens()) == 2, "the revoked token stays in the list"


@pytest.mark.parametrize("mode", ["", "PER_USER", "admin", None, 5, ["per_user"]],
                         ids=["empty", "upper", "other", "none", "int", "list"])
def test_a_bad_mode_is_refused(mt, root, mode):
    with pytest.raises(ValueError) as exc:
        mt.create_token("Bot", mode, ADMIN, account=BOT)
    _plain_sentence(exc)
    assert not _file(root).exists()


BAD_ACCOUNTS = [
    None, "", "   ", "not-an-email", "a@b", "bot@corp.example, x@corp.example",
    "bot@corp.example\nx", "../bot@corp.example", 5, [BOT],
    "unknown@corp.example",
]


@pytest.mark.parametrize("account", BAD_ACCOUNTS, ids=range(len(BAD_ACCOUNTS)))
def test_a_bad_shared_account_is_refused_and_not_created(mt, root, account):
    local_store.AuthStore().create_account(BOT)
    with pytest.raises(ValueError) as exc:
        mt.create_token("Shared", "shared_account", ADMIN, account=account)
    _plain_sentence(exc)
    assert not _file(root).exists()
    assert sorted(p.name for p in (root / "users").iterdir()) == [BOT]


@pytest.mark.parametrize("bootstrap", [ADMIN, "root@corp.example"])
def test_the_bootstrap_admin_cannot_be_a_shared_account(mt, root, monkeypatch, bootstrap):
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", bootstrap)
    store = local_store.AuthStore()
    store.create_account(bootstrap)
    store.set_role(bootstrap, "admin")
    for typed in (bootstrap, bootstrap.upper()):
        with pytest.raises(ValueError) as exc:
            mt.create_token("Shared", "shared_account", ADMIN, account=typed)
        _plain_sentence(exc)
    assert not _file(root).exists()


def test_a_removed_account_cannot_be_a_shared_account(mt, root):
    store = local_store.AuthStore()
    store.create_account(BOT)
    assert store.remove_user(BOT) is True
    with pytest.raises(ValueError):
        mt.create_token("Shared", "shared_account", ADMIN, account=BOT)
    assert not (root / "users" / BOT).exists()


def test_a_refused_create_keeps_the_stored_tokens(mt, root):
    _, raw = mt.create_token("First", "per_user", ADMIN)
    before = _file(root).read_bytes()
    with pytest.raises(ValueError):
        mt.create_token("Second", "nope", ADMIN)
    assert _file(root).read_bytes() == before
    assert mt.verify(raw)["name"] == "First"


# ===========================================================================
# list
# ===========================================================================
def test_an_absent_file_is_an_empty_list_and_reading_never_creates_it(mt, root):
    assert mt.list_tokens() == []
    assert mt.verify(RAW_A) is None
    assert mt.revoke_token("aaaaaaaaaaaaaaaa", ADMIN) is None
    mt.touch("aaaaaaaaaaaaaaaa")
    assert not _file(root).exists()


def test_list_is_newest_first_with_public_keys_only(mt, root):
    _seed(root, [
        _record(RAW_A, tid="a" * 16, name="A", created_at="2026-11-20T10:00:00+00:00"),
        _record(RAW_B, tid="b" * 16, name="B", created_at="2026-11-21T10:00:00+00:00",
                mode="shared_account", account=BOT,
                account_created_at="2026-01-01T00:00:00+00:00"),
        _record(RAW_C, tid="c" * 16, name="C", created_at="2026-11-22T10:00:00+00:00",
                revoked_at="2026-11-23T10:00:00+00:00", revoked_by=ADMIN),
    ])
    rows = mt.list_tokens()
    assert [r["name"] for r in rows] == ["C", "B", "A"]
    for row in rows:
        assert set(row) == PUBLIC_KEYS
    assert [r["active"] for r in rows] == [False, True, True]
    assert [r["account"] for r in rows] == [None, BOT, None]
    text = json.dumps(rows)
    for raw in (RAW_A, RAW_B, RAW_C):
        assert raw not in text and _sha(raw) not in text


def test_a_created_token_is_listed(mt):
    row, _ = mt.create_token("Bot", "per_user", ADMIN)
    assert mt.list_tokens() == [row]


def test_tokens_survive_a_reload_of_the_module(mt, root):
    """Nothing about a token lives only in the process: a file written by the
    running release reads the same after a restart."""
    row, raw = mt.create_token("Bot", "per_user", ADMIN)
    again = importlib.reload(mt)
    assert again.verify(raw)["id"] == row["id"]
    assert again.list_tokens() == [row]


# ===========================================================================
# revoke
# ===========================================================================
def test_revoke_stamps_and_refuses_the_token(mt, root):
    row, raw = mt.create_token("Bot", "per_user", ADMIN)
    out = mt.revoke_token(row["id"], "second.admin@corp.example")
    assert set(out) == PUBLIC_KEYS
    assert out["id"] == row["id"]
    assert out["active"] is False
    assert out["revoked_by"] == "second.admin@corp.example"
    assert datetime.fromisoformat(out["revoked_at"]).utcoffset() == timedelta(0)
    assert mt.verify(raw) is None
    assert mt.list_tokens() == [out]


def test_a_second_revoke_keeps_the_first_stamps(mt, root):
    row, _ = mt.create_token("Bot", "per_user", ADMIN)
    first = mt.revoke_token(row["id"], ADMIN)
    second = mt.revoke_token(row["id"], "other@corp.example")
    assert second == first
    assert _doc(root)["tokens"][0]["revoked_by"] == ADMIN


@pytest.mark.parametrize("token_id", ["f" * 16, "", "../x", None, 5, ["a"]],
                         ids=["unknown", "empty", "path", "none", "int", "list"])
def test_revoke_of_an_unknown_id_is_none_and_writes_nothing(mt, root, token_id):
    mt.create_token("Bot", "per_user", ADMIN)
    before = _file(root).read_bytes()
    assert mt.revoke_token(token_id, ADMIN) is None
    assert _file(root).read_bytes() == before


def test_revoke_touches_only_its_own_token(mt, root):
    a, raw_a = mt.create_token("A", "per_user", ADMIN)
    b, raw_b = mt.create_token("B", "per_user", ADMIN)
    mt.revoke_token(a["id"], ADMIN)
    assert mt.verify(raw_a) is None
    assert mt.verify(raw_b)["id"] == b["id"]


def test_a_write_keeps_fields_it_does_not_know(mt, root):
    """Stored-shape safety: a field written by a later release survives a
    write by this one."""
    _seed(root, [_record(RAW_A, tid="a" * 16, name="A", note="kept"),
                 _record(RAW_B, tid="b" * 16, name="B", note="kept too")])
    mt.revoke_token("a" * 16, ADMIN)
    mt.touch("b" * 16)
    notes = {r["id"]: r.get("note") for r in _doc(root)["tokens"]}
    assert notes == {"a" * 16: "kept", "b" * 16: "kept too"}


# ===========================================================================
# verify
# ===========================================================================
def test_verify_answers_the_full_record(mt):
    row, raw = mt.create_token("Bot", "per_user", ADMIN)
    rec = mt.verify(raw)
    assert rec["id"] == row["id"]
    assert rec["name"] == "Bot" and rec["mode"] == "per_user"
    assert rec["token_hash"] == _sha(raw)


def test_verify_answers_the_bound_account(mt):
    local_store.AuthStore().create_account(BOT)
    _, raw = mt.create_token("Shared", "shared_account", ADMIN, account=BOT)
    rec = mt.verify(raw)
    assert rec["account"] == BOT and rec["account_created_at"]


BAD_TOKENS = [
    None, "", " ", 5, b"pdcmcp_" + b"A" * 43, ["x"], {}, True,
    "pdcmcp_", "A" * 43, RAW_B, RAW_A[:-1], RAW_A + "A", " " + RAW_A, RAW_A + "\n",
    RAW_A.upper(), RAW_A.lower(), "Bearer " + RAW_A, "pdcmcp_" + "é" * 43,
    "pdcmcp_" + "\ud800" * 43, "pdcmcp_" + "\x00" * 43, "x" * 100_000,
    _sha(RAW_A),
]


@pytest.mark.parametrize("bad", BAD_TOKENS, ids=range(len(BAD_TOKENS)))
def test_anything_but_the_exact_token_is_none(mt, root, bad):
    _seed(root, [_record(RAW_A)])
    assert mt.verify(RAW_A)["id"] == "a" * 16
    assert mt.verify(bad) is None


def test_an_old_record_without_the_optional_keys_is_active(mt, root):
    rec = _record(RAW_A)
    for key in ("last_used_at", "revoked_at", "revoked_by"):
        rec.pop(key)
    _seed(root, [rec])
    assert mt.verify(RAW_A)["id"] == rec["id"]
    (row,) = mt.list_tokens()
    assert set(row) == PUBLIC_KEYS
    assert row["active"] is True and row["last_used_at"] is None


UNREADABLE = {
    "not-json": b"not json at all",
    "empty": b"",
    "half": b'{"version": 1, "tokens": [{"id": "a',
    "list": b"[]",
    "null": b"null",
    "number": b"5",
    "tokens-dict": b'{"version": 1, "tokens": {}}',
    "tokens-null": b'{"version": 1, "tokens": null}',
    "tokens-str": b'{"version": 1, "tokens": "x"}',
    "bad-utf8": b'{"version": 1, "tokens": [], "x": "\xff\xfe"}',
}


@pytest.mark.parametrize("content", list(UNREADABLE.values()), ids=list(UNREADABLE))
def test_an_unreadable_file_refuses_every_token(mt, root, content):
    _file(root).write_bytes(content)
    assert mt.verify(RAW_A) is None
    assert mt.list_tokens() == []


def test_a_file_that_cannot_be_opened_refuses_every_token(mt, root):
    _file(root).mkdir()
    assert mt.verify(RAW_A) is None
    assert mt.list_tokens() == []


@pytest.mark.parametrize("content", list(UNREADABLE.values()), ids=list(UNREADABLE))
def test_a_write_never_overwrites_an_unreadable_file(mt, root, content):
    _file(root).write_bytes(content)
    with pytest.raises(Exception):
        mt.create_token("Bot", "per_user", ADMIN)
    assert _file(root).read_bytes() == content
    try:
        mt.revoke_token("a" * 16, ADMIN)
    except Exception:
        pass                    # refusing by raising is as good as None
    mt.touch("a" * 16)          # never raises
    assert _file(root).read_bytes() == content


def test_a_write_refuses_a_file_that_cannot_be_opened(mt, root):
    _file(root).mkdir()
    with pytest.raises(Exception):
        mt.create_token("Bot", "per_user", ADMIN)
    mt.touch("a" * 16)
    assert _file(root).is_dir()


CORRUPT = [
    None, 5, "x", [], {}, {"id": "b" * 16, "name": "No hash"},
    {"id": "b" * 16, "name": "Hash none", "token_hash": None},
    {"id": "b" * 16, "name": "Hash int", "token_hash": 5},
    {"id": "b" * 16, "name": "Hash list", "token_hash": ["x"]},
    {"id": "b" * 16, "name": "Hash non-ascii", "token_hash": "é" * 64},
    {"id": None, "name": None, "mode": None, "token_hash": "", "created_at": 5},
]


@pytest.mark.parametrize("corrupt", CORRUPT, ids=range(len(CORRUPT)))
@pytest.mark.parametrize("first", [True, False], ids=["before", "after"])
def test_a_corrupt_record_does_not_disable_a_good_one(mt, root, corrupt, first):
    good = _record(RAW_A)
    _seed(root, [corrupt, good] if first else [good, corrupt])
    assert mt.verify(RAW_A)["id"] == good["id"]
    assert mt.verify(RAW_B) is None
    assert good["id"] in [r["id"] for r in mt.list_tokens()]
    mt.touch(good["id"])
    _, raw = mt.create_token("Another", "per_user", ADMIN)
    assert mt.verify(raw) is not None
    assert mt.verify(RAW_A)["id"] == good["id"]
    assert mt.revoke_token(good["id"], ADMIN)["active"] is False
    assert mt.verify(RAW_A) is None


def test_every_record_is_compared_with_compare_digest(mt, root, monkeypatch):
    """Constant time: the comparison count does not depend on which record
    matches, or whether one does."""
    _seed(root, [
        _record(RAW_A, tid="a" * 16, name="A"),
        _record(RAW_B, tid="b" * 16, name="B"),
        _record(RAW_C, tid="c" * 16, name="C",
                revoked_at="2026-11-23T10:00:00+00:00", revoked_by=ADMIN),
    ])
    calls = []
    real = hmac.compare_digest

    def counting(a, b):
        calls.append(1)
        return real(a, b)
    monkeypatch.setattr(hmac, "compare_digest", counting)
    counts = []
    for raw, expected in ((RAW_A, "a" * 16), (RAW_B, "b" * 16), (RAW_C, None),
                          ("pdcmcp_" + "Z" * 43, None)):
        calls.clear()
        rec = mt.verify(raw)
        assert (rec and rec["id"]) == expected
        counts.append(len(calls))
    assert counts == [3, 3, 3, 3]


def test_the_hash_is_never_compared_with_an_operator():
    path = ROOT / "mcp_tokens.py"
    assert path.exists(), "mcp_tokens.py is missing"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    attrs = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "compare_digest"
             and isinstance(n.value, ast.Name) and n.value.id == "hmac"]
    assert attrs, "verify must use hmac.compare_digest"
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            src = ast.unparse(node)
            assert "token_hash" not in src, src


# ===========================================================================
# touch
# ===========================================================================
def test_the_default_clock_is_time_time(mt):
    import time
    assert mt._clock is time.time


def test_touch_stamps_the_module_clock(mt, clock):
    row, _ = mt.create_token("Bot", "per_user", ADMIN)
    mt.touch(row["id"])
    (listed,) = mt.list_tokens()
    stamp = datetime.fromisoformat(listed["last_used_at"])
    assert stamp.utcoffset() == timedelta(0)
    assert abs(stamp.timestamp() - clock["t"]) < 1


def test_touch_writes_at_most_once_a_minute(mt, root, clock):
    row, _ = mt.create_token("Bot", "per_user", ADMIN)
    mt.touch(row["id"])
    first = _file(root).read_bytes()
    for step in (0, 1, 58):
        clock["t"] += step
        mt.touch(row["id"])
        assert _file(root).read_bytes() == first
    clock["t"] += 2          # 61 s after the first stamp
    mt.touch(row["id"])
    assert _file(root).read_bytes() != first
    stamp = datetime.fromisoformat(mt.list_tokens()[0]["last_used_at"])
    assert abs(stamp.timestamp() - clock["t"]) < 1


def test_touch_is_throttled_per_token(mt, clock):
    a, _ = mt.create_token("A", "per_user", ADMIN)
    b, _ = mt.create_token("B", "per_user", ADMIN)
    mt.touch(a["id"])
    used = {r["name"]: r["last_used_at"] for r in mt.list_tokens()}
    assert used["A"] and used["B"] is None
    mt.touch(b["id"])
    used = {r["name"]: r["last_used_at"] for r in mt.list_tokens()}
    assert used["A"] and used["B"]


def test_touch_replaces_an_old_stored_stamp(mt, root, clock):
    _seed(root, [_record(RAW_A, last_used_at="2026-01-01T00:00:00+00:00")])
    mt.touch("a" * 16)
    stamp = datetime.fromisoformat(mt.list_tokens()[0]["last_used_at"])
    assert abs(stamp.timestamp() - clock["t"]) < 1


@pytest.mark.parametrize("stored", ["junk", 5, "", [], "2026-01-01"],
                         ids=["junk", "int", "empty", "list", "naive"])
def test_touch_tolerates_a_junk_stamp(mt, root, clock, stored):
    _seed(root, [_record(RAW_A, last_used_at=stored)])
    mt.touch("a" * 16)
    assert mt.verify(RAW_A) is not None


@pytest.mark.parametrize("token_id", [None, "", "f" * 16, 5, ["x"], {}],
                         ids=["none", "empty", "unknown", "int", "list", "dict"])
def test_touch_of_an_unknown_id_never_raises(mt, root, token_id):
    mt.create_token("Bot", "per_user", ADMIN)
    before = _file(root).read_bytes()
    mt.touch(token_id)
    assert _file(root).read_bytes() == before


def test_touch_never_raises_when_the_write_fails(mt, root, clock, monkeypatch):
    row, raw = mt.create_token("Bot", "per_user", ADMIN)
    before = _file(root).read_bytes()

    def boom(*a, **k):
        raise PermissionError("disk")
    monkeypatch.setattr(os, "replace", boom)
    mt.touch(row["id"])
    assert _file(root).read_bytes() == before
    assert mt.verify(raw)["id"] == row["id"]


def test_verify_does_not_stamp_by_itself(mt, root):
    _, raw = mt.create_token("Bot", "per_user", ADMIN)
    before = _file(root).read_bytes()
    mt.verify(raw)
    assert _file(root).read_bytes() == before


# ===========================================================================
# writes
# ===========================================================================
def test_a_failed_write_keeps_the_previous_file(mt, root, monkeypatch):
    _, raw = mt.create_token("First", "per_user", ADMIN)
    before = _file(root).read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")
    with monkeypatch.context() as m:
        m.setattr(os, "replace", boom)
        with pytest.raises(Exception):
            mt.create_token("Second", "per_user", ADMIN)
    assert _file(root).read_bytes() == before
    assert mt.verify(raw)["name"] == "First"
    assert [r["name"] for r in mt.list_tokens()] == ["First"]


def _run_threads(n, fn):
    barrier = threading.Barrier(n)

    def work(i):
        barrier.wait()
        fn(i)
    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads)


def test_concurrent_creates_lose_nothing(mt):
    made, errors = [], []

    def create(i):
        try:
            made.append(mt.create_token(f"Bot {i}", "per_user", ADMIN))
        except Exception as e:                  # noqa: BLE001 - reported below
            errors.append(e)
    _run_threads(8, create)
    assert errors == []
    assert len(mt.list_tokens()) == 8
    for row, raw in made:
        assert mt.verify(raw)["id"] == row["id"]


def test_concurrent_creates_of_one_name_make_one_token(mt):
    made, refused = [], []

    def create(i):
        try:
            made.append(mt.create_token("Same name", "per_user", ADMIN))
        except ValueError:
            refused.append(i)
    _run_threads(8, create)
    assert len(made) == 1 and len(refused) == 7
    assert len(mt.list_tokens()) == 1


# ===========================================================================
# logs
# ===========================================================================
def test_no_log_line_carries_a_token_or_its_hash(mt, root, logs, clock, monkeypatch):
    row, raw = mt.create_token("Bot", "per_user", ADMIN)
    wrong = "pdcmcp_" + "Q" * 43
    forged = "pdcmcp_" + "Q" * 20 + "\nFORGED RECORD"
    mt.verify(raw)
    for bad in (wrong, forged, raw + "x"):
        mt.verify(bad)
    mt.touch(row["id"])
    mt.revoke_token(row["id"], ADMIN)
    mt.verify(raw)
    with monkeypatch.context() as m:
        def boom(*a, **k):
            raise OSError(f"cannot write {raw} {_sha(raw)}\nFORGED RECORD")
        m.setattr(os, "replace", boom)
        with pytest.raises(Exception):
            mt.create_token("Second", "per_user", ADMIN)
    _file(root).write_text(f'{{"tokens": "{_sha(raw)}', encoding="utf-8")
    mt.verify(raw)
    mt.list_tokens()
    text = "\n".join(logs)
    for secret in (raw, raw[len("pdcmcp_"):], _sha(raw), wrong, _sha(wrong),
                   "Q" * 20):
        assert secret not in text
    for line in logs:
        assert "\n" not in line and "\r" not in line, line


def test_an_unreadable_file_is_logged(mt, root, logs):
    _file(root).write_bytes(b"not json at all")
    mt.verify(RAW_A)
    assert any("MCP" in ln for ln in logs), logs
