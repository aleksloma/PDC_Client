"""Security remediation Task 1e.2 — conversation-id guard in local_store.

`ChatDataStore.get_history` / `append_history` / `truncate_conv_history`
joined `conv_id` straight into `conversations_dir / f"{conv_id}.jsonl"` with
no id guard while every other id in the codebase has one. `../../evil` read
(and rewrote) a file two levels up; `append_history` created files outside
the conversation folder.

The guard lives in the STORE (`local_store._CONV_ID_RE`, `valid_conv_id`):
readers return [] and writers no-op for an invalid id. Routes are unchanged
— tests/test_chat_busy_auto_analysis.py expects `cv_1` to reach the 409 busy
check, so no route-level 400 is asserted here.

All paths are tmp_path-rooted; the traversal targets are pre-seeded files
under tmp_path and must stay byte-identical.
"""
import pytest

import local_store
from settings import settings

CHAT = "chatguard1"
VALID = "cv_" + "0" * 16
SEED = b'{"role": "user", "content": "seeded-secret"}\n'
INVALID_IDS = [
    "../../evil",            # -> <DATA_ROOT>/chatdata/evil.jsonl
    "../../../evil",         # -> <DATA_ROOT>/evil.jsonl
    "..\\..\\evil",          # Windows separators (same target on Windows)
    "cv_1",
    "cv_" + "a" * 15,        # too short
    "cv_" + "a" * 17,        # too long
    "cv_" + "A" * 16,        # uppercase hex
    "",
    None,
]


def _files(root):
    """Every file under root, relative — the before/after snapshot."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    st = local_store.ChatDataStore(CHAT)
    # Traversal targets: one, two and three levels above conversations_dir.
    targets = [
        st.root / "evil.jsonl",             # ../evil
        tmp_path / "chatdata" / "evil.jsonl",  # ../../evil
        tmp_path / "evil.jsonl",            # ../../../evil
    ]
    for t in targets:
        t.write_bytes(SEED)
    st._targets = targets
    yield st
    local_store._DATAFRAME_CACHE.invalidate()


def _assert_untouched(tmp_path, store, before):
    for t in store._targets:
        assert t.read_bytes() == SEED, f"traversal target modified: {t}"
    assert _files(tmp_path) == before, "files changed under the data root"


# ---------------------------------------------------------------------------
# valid_conv_id
# ---------------------------------------------------------------------------
def test_valid_conv_id_accepts_canonical_ids(store):
    assert local_store.valid_conv_id(VALID) is True
    assert local_store.valid_conv_id(store.new_conversation()) is True


@pytest.mark.parametrize("bad", INVALID_IDS)
def test_valid_conv_id_rejects(bad):
    assert local_store.valid_conv_id(bad) is False


def test_conv_id_regex_is_exact():
    assert local_store._CONV_ID_RE.pattern == r"^cv_[0-9a-f]{16}$"


# ---------------------------------------------------------------------------
# Store methods fail closed on an invalid id
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", INVALID_IDS)
def test_get_history_invalid_id_returns_empty(tmp_path, store, bad):
    before = _files(tmp_path)
    assert store.get_history(bad) == []
    _assert_untouched(tmp_path, store, before)


@pytest.mark.parametrize("bad", INVALID_IDS)
def test_truncate_invalid_id_returns_empty_and_writes_nothing(tmp_path, store, bad):
    before = _files(tmp_path)
    assert store.truncate_conv_history(bad, 0) == []
    _assert_untouched(tmp_path, store, before)


@pytest.mark.parametrize("bad", INVALID_IDS)
def test_append_invalid_id_writes_nothing(tmp_path, store, bad):
    before = _files(tmp_path)
    store.append_history(bad, {"role": "user", "content": "x"})
    _assert_untouched(tmp_path, store, before)
    assert store.get_history(bad) == []


# ---------------------------------------------------------------------------
# A valid id still round-trips
# ---------------------------------------------------------------------------
def test_valid_id_round_trips(tmp_path, store):
    conv_id = store.new_conversation()
    store.append_history(conv_id, {"role": "user", "content": "q1"})
    store.append_history(conv_id, {"role": "ai", "content": "a1"})
    assert [m["content"] for m in store.get_history(conv_id)] == ["q1", "a1"]
    assert [m["content"] for m in store.truncate_conv_history(conv_id, 1)] == ["q1"]
    assert [m["content"] for m in store.get_history(conv_id)] == ["q1"]
    assert (store.conversations_dir / f"{conv_id}.jsonl").is_file()
    for t in store._targets:
        assert t.read_bytes() == SEED


# ---------------------------------------------------------------------------
# C2 — the guard must be OBSERVABLE from all three methods
# ---------------------------------------------------------------------------
# `append_history` logs CONV_ID_INVALID; `get_history` and
# `truncate_conv_history` return [] in silence, so a genuine bug (a route that
# lost its conv_id, a stale frontend id) shows up as "the conversation is
# empty" with nothing in the log to explain it. All three must log a warning
# naming the method, still return the same safe fallback, and never raise.
@pytest.fixture
def logs(monkeypatch):
    """Capture every log_with_sid call local_store makes."""
    out = []
    monkeypatch.setattr(local_store, "log_with_sid",
                        lambda sid, level, message, **ctx: out.append((sid, level, message, ctx)))
    return out


def _invalid_warnings(logs, method: str) -> list[tuple]:
    return [r for r in logs
            if r[1] == "warning" and "CONV_ID_INVALID" in r[2] and method in r[2]]


@pytest.mark.parametrize("method,call", [
    ("append_history", lambda st, bad: st.append_history(bad, {"role": "user", "content": "x"})),
    ("get_history", lambda st, bad: st.get_history(bad)),
    ("truncate_conv_history", lambda st, bad: st.truncate_conv_history(bad, 0)),
])
def test_invalid_conv_id_is_logged_by_every_method(tmp_path, store, logs, method, call):
    before = _files(tmp_path)
    call(store, "../../evil")
    hits = _invalid_warnings(logs, method)
    assert len(hits) == 1, [r[2] for r in logs]
    assert hits[0][0] == CHAT                       # tagged with the chat id
    assert "evil" in hits[0][2]                     # the offending id is named
    _assert_untouched(tmp_path, store, before)


@pytest.mark.parametrize("bad", INVALID_IDS)
def test_readers_still_return_the_safe_fallback_while_logging(tmp_path, store, logs, bad):
    before = _files(tmp_path)
    assert store.get_history(bad) == []
    assert store.truncate_conv_history(bad, 0) == []
    store.append_history(bad, {"role": "user", "content": "x"})
    assert len(_invalid_warnings(logs, "get_history")) == 1
    assert len(_invalid_warnings(logs, "truncate_conv_history")) == 1
    assert len(_invalid_warnings(logs, "append_history")) == 1
    _assert_untouched(tmp_path, store, before)


def test_a_valid_id_logs_nothing(tmp_path, store, logs):
    conv_id = store.new_conversation()
    store.append_history(conv_id, {"role": "user", "content": "q1"})
    assert [m["content"] for m in store.get_history(conv_id)] == ["q1"]
    assert store.truncate_conv_history(conv_id, 1) == store.get_history(conv_id)
    assert [r for r in logs if "CONV_ID_INVALID" in r[2]] == []
