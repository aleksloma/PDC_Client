"""Client-side local storage.

Same shape and lifecycle as the B2C `storage.py`:
  - `UserStore(sid)` — temporary per-session workspace under DATA_ROOT/sessions/<sid>.
    Files dropped here by `/upload` live here until `/generate_chatdata` clones
    them into a permanent `ChatDataStore`.
  - `ChatDataStore(chat_id)` — permanent per-chat store under
    DATA_ROOT/chatdata/<chat_id>. Holds the canonical file copies, meta, and
    per-conversation JSONL.
  - `AuthStore` — email-keyed user profile + active_chats/conversations index.

Raw data files NEVER leave this server. Per AI_CONSTITUTION Article V, history
is JSONL with atomic appends.
"""
from __future__ import annotations

import io
import json
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from settings import settings
from logger_utils import log_with_sid
from excel_table_detector import load_excel_sheets, _EXTRACTED_TEXT_ABOVE_TABLE  # noqa: F401 — re-exported


_LOCK = threading.RLock()


def _data_root() -> Path:
    root = Path(settings.DATA_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_email(email: str) -> str:
    return email.strip().lower().replace("/", "_").replace("\\", "_")


def _load_one_file(path: Path) -> dict[str, pd.DataFrame]:
    """Load one tabular file. Returns {key: df}. Excel sheets yield
    "<filename>::<sheet>" keys (single-sheet excels keep the bare filename).

    Excel files run through the validated 6-stage detection pipeline
    (`excel_table_detector.load_excel_sheets`) — same algorithm the global B2C
    app uses, so multi-row headers, ListObjects, label-vs-measure splits,
    multilingual total-row drops, and text-above-the-table extraction all work.
    """
    out: dict[str, pd.DataFrame] = {}
    try:
        ext = path.suffix.lower()
        if ext in (".xls", ".xlsx", ".xlsm"):
            out.update(load_excel_sheets(path, path.name))
        elif ext == ".csv":
            out[path.name] = pd.read_csv(path)
        elif ext == ".tsv":
            out[path.name] = pd.read_csv(path, sep="\t")
        elif ext == ".json":
            out[path.name] = pd.read_json(path)
        elif ext == ".parquet":
            out[path.name] = pd.read_parquet(path)
    except Exception as e:
        log_with_sid(path.name, "warning", f"FILE_LOAD_ERROR {path}: {e}")
    return out


# ---------------------------------------------------------------------------
# AuthStore — user profile (email-only)
# ---------------------------------------------------------------------------
class AuthStore:
    """Email-only profile storage. The enterprise build has no password,
    no subscription plan upgrades, etc. — just the email as identifier."""

    def ensure_user(self, email: str) -> dict:
        email = _safe_email(email)
        udir = _data_root() / "users" / email
        udir.mkdir(parents=True, exist_ok=True)
        profile_path = udir / "profile.json"
        if profile_path.exists():
            try:
                return json.loads(profile_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        profile = {"email": email, "created_at": _now()}
        profile_path.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
        log_with_sid(email, "info", "USER_PROFILE_CREATED")
        return profile

    def get_profile(self, email: str) -> Optional[dict]:
        email = _safe_email(email)
        p = _data_root() / "users" / email / "profile.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def update_profile(self, email: str, *, new_email: str = None) -> dict:
        """Only the email is editable. Anything else is ignored."""
        with _LOCK:
            email = _safe_email(email)
            prof = self.get_profile(email) or {"created_at": _now()}
            if new_email and _safe_email(new_email) != email:
                # Cannot change identity — the email IS the identity.
                # Silently ignore to avoid breaking the page.
                pass
            prof["email"] = email
            (_data_root() / "users" / email / "profile.json").write_text(
                json.dumps(prof, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return prof

    def list_active_chats(self, email: str) -> list[dict]:
        email = _safe_email(email)
        p = _data_root() / "users" / email / "active_chats.jsonl"
        if not p.exists():
            return []
        out = []
        for idx, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
            try:
                row = json.loads(line)
            except Exception:
                continue
            row.setdefault("_seq", idx)   # stable file-order tiebreak
            out.append(row)
        # Newest first (descending created_at). Rows missing created_at sort to
        # the bottom (treated as oldest); file order breaks ties. Stable so the
        # rest of the app sees a single consistent ordering.
        out.sort(key=lambda r: (r.get("created_at") or "", r.get("_seq", 0)),
                 reverse=True)
        for r in out:
            r.pop("_seq", None)
        return out

    def list_conversations(self, email: str) -> list[dict]:
        email = _safe_email(email)
        p = _data_root() / "users" / email / "conversations.jsonl"
        if not p.exists():
            return []
        out = []
        for idx, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
            try:
                row = json.loads(line)
            except Exception:
                continue
            row.setdefault("_seq", idx)
            out.append(row)
        # Same newest-first ordering as active chats (descending by updated_at,
        # then created_at; file order breaks ties). Missing timestamps sort last.
        out.sort(key=lambda r: (r.get("updated_at") or r.get("created_at") or "",
                                r.get("_seq", 0)),
                 reverse=True)
        for r in out:
            r.pop("_seq", None)
        return out

    def get_active_chat_names(self, email: str) -> set[str]:
        return {row.get("title", "") for row in self.list_active_chats(email) if row.get("title")}

    def record_active_chat(self, email: str, chat_id: str, title: str, files: list[str]) -> None:
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "active_chats.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "chat_id": chat_id, "title": title, "files": files,
                    "created_at": _now(),
                }, ensure_ascii=False) + "\n")

    def record_conversation(self, email: str, chat_id: str, conv_id: str, title: str,
                             *, shared_by: str = "") -> None:
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "conversations.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "conv_id": conv_id, "chat_id": chat_id, "title": title,
                "created_at": _now(),
            }
            if shared_by:
                row["shared_by"] = shared_by
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def record_shared_chat(self, email: str, chat_id: str, title: str,
                            files: list[str], shared_by: str) -> None:
        """Record a chat in the recipient's active_chats so they can open it."""
        with _LOCK:
            email = _safe_email(email)
            existing_ids = {row.get("chat_id") for row in self.list_active_chats(email)}
            if chat_id in existing_ids:
                return
            p = _data_root() / "users" / email / "active_chats.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "chat_id": chat_id, "title": title, "files": files,
                    "created_at": _now(),
                    "shared_by": shared_by,
                }, ensure_ascii=False) + "\n")

    def rename_active_chat(self, email: str, chat_id: str, new_title: str) -> bool:
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "active_chats.jsonl"
            if not p.exists():
                return False
            out_lines = []
            found = False
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("chat_id") == chat_id:
                        rec["title"] = new_title
                        found = True
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
                except Exception:
                    out_lines.append(line)
            p.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
            return found

    def rename_conversation(self, email: str, conv_id: str, new_title: str) -> bool:
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "conversations.jsonl"
            if not p.exists():
                return False
            out_lines = []
            found = False
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("conv_id") == conv_id:
                        rec["title"] = new_title
                        found = True
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
                except Exception:
                    out_lines.append(line)
            p.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
            return found

    def delete_conversation(self, email: str, conv_id: str) -> bool:
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "conversations.jsonl"
            if not p.exists():
                return False
            out_lines = []
            removed = False
            chat_id = None
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("conv_id") == conv_id:
                        removed = True
                        chat_id = rec.get("chat_id")
                        continue
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
                except Exception:
                    out_lines.append(line)
            p.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
            # Remove the actual JSONL too
            if chat_id:
                conv_path = _data_root() / "chatdata" / chat_id / "conversations" / f"{conv_id}.jsonl"
                try:
                    if conv_path.exists():
                        conv_path.unlink()
                except Exception:
                    pass
            return removed

    def deactivate_chat(self, email: str, chat_id: str) -> bool:
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "active_chats.jsonl"
            if not p.exists():
                return False
            out_lines = []
            removed = False
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("chat_id") == chat_id:
                        removed = True
                        continue
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
                except Exception:
                    out_lines.append(line)
            p.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
            return removed


# ---------------------------------------------------------------------------
# UserStore — per-session temp area (mirror of B2C UserStore)
# ---------------------------------------------------------------------------
class UserStore:
    """Per-session temporary workspace.

    SID is the session id used by `dashboard.js` for the upload flow. Files
    live here only between `/upload` and `/generate_chatdata`. After that
    they are cloned into a permanent `ChatDataStore`.
    """

    def __init__(self, sid: str):
        self.sid = sid
        self.root = _data_root() / "sessions" / sid
        self.files_dir = self.root / "files"
        self.meta_path = self.root / "meta.json"
        self._ensure_layout()

    def _ensure_layout(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        if not self.meta_path.exists():
            self.meta_path.write_text(json.dumps({"files": []}, ensure_ascii=False, indent=2), encoding="utf-8")

    def reset_all(self):
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self._ensure_layout()

    def save_upload(self, filename: str, content: bytes) -> Path:
        out = self.files_dir / filename
        out.write_bytes(content)
        meta = self.read_meta()
        if filename not in [f.get("file_name") for f in meta.get("files", [])]:
            meta.setdefault("files", []).append({"file_name": filename, "schema": {}})
            self.write_meta(meta)
        return out

    def read_meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {"files": []}

    def write_meta(self, meta: dict):
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_dataframes(self) -> dict[str, pd.DataFrame]:
        dfs: dict[str, pd.DataFrame] = {}
        for fp in sorted(self.files_dir.iterdir()):
            if fp.is_file() and not fp.name.startswith("."):
                dfs.update(_load_one_file(fp))
        return dfs


# ---------------------------------------------------------------------------
# ChatDataStore — permanent per-chat workspace
# ---------------------------------------------------------------------------
class ChatDataStore:
    """Per-chat permanent workspace under chatdata/<chat_id>."""

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.root = _data_root() / "chatdata" / chat_id
        self.files_dir = self.root / "files"
        self.meta_path = self.root / "meta.json"
        self.conversations_dir = self.root / "conversations"
        self._ensure_layout()

    def _ensure_layout(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        if not self.meta_path.exists():
            self.meta_path.write_text(json.dumps({"files": []}, ensure_ascii=False, indent=2), encoding="utf-8")

    def clone_from_user_store(self, user_store: UserStore) -> None:
        for fp in user_store.files_dir.iterdir():
            if fp.is_file() and not fp.name.startswith("."):
                shutil.copy2(fp, self.files_dir / fp.name)
        # Copy meta wholesale (already contains schema entries)
        try:
            self.meta_path.write_text(user_store.meta_path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass

    def read_meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {"files": []}

    def write_meta(self, meta: dict):
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def set_welcome(self, msg: str):
        (self.root / "welcome.txt").write_text(msg or "", encoding="utf-8")

    def get_welcome(self) -> str:
        p = self.root / "welcome.txt"
        if not p.exists():
            return ""
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return ""

    def set_suggested_questions(self, questions: list[str]):
        (self.root / "suggested_questions.json").write_text(
            json.dumps(questions or [], ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_suggested_questions(self) -> list[str]:
        p = self.root / "suggested_questions.json"
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []

    def load_dataframes(self) -> dict[str, pd.DataFrame]:
        dfs: dict[str, pd.DataFrame] = {}
        for fp in sorted(self.files_dir.iterdir()):
            if fp.is_file() and not fp.name.startswith("."):
                dfs.update(_load_one_file(fp))
        return dfs

    def schema_docs(self) -> dict:
        """Build {filename: {file_description, fields: {col: {...}}}} for the brain."""
        out = {}
        for file_entry in self.read_meta().get("files", []) or []:
            name = file_entry.get("file_name")
            if not name:
                continue
            schema = file_entry.get("schema") or {}
            out[name] = {
                "file_description": file_entry.get("file_description", ""),
                "fields": (schema.get("fields") if isinstance(schema, dict) else {}) or {},
            }
        return out

    def new_conversation(self, title: str = "New conversation") -> str:
        conv_id = "cv_" + secrets.token_hex(8)
        (self.conversations_dir / f"{conv_id}.jsonl").touch()
        return conv_id

    def copy_conv_to_new(self, source_conv_id: str) -> str:
        """Snapshot copy of a conversation into a fresh conv_id, used for
        conversation-level sharing. Verbatim port of global
        `storage.ChatDataStore.copy_conv_to_new`.

        Returns the new conv_id. After this point both conversations evolve
        independently (no sync).
        """
        new_conv_id = self.new_conversation()
        source_history = self.get_history(source_conv_id)
        if source_history:
            new_path = self.conversations_dir / f"{new_conv_id}.jsonl"
            with new_path.open("w", encoding="utf-8") as f:
                for msg in source_history:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return new_conv_id

    def add_share_recipients(self, recipients: list[str]) -> list[str]:
        """Append distinct emails to `meta.json["sharing"]["shared_with"]`.
        Returns the new (previously-absent) recipients only."""
        with _LOCK:
            meta = self.read_meta()
            sharing = meta.get("sharing") or {"shared_with": []}
            existing = set(sharing.get("shared_with") or [])
            new = [r for r in recipients if r and r not in existing]
            existing.update(new)
            sharing["shared_with"] = sorted(existing)
            meta["sharing"] = sharing
            self.write_meta(meta)
        return new

    def append_history(self, conv_id: str, message: dict) -> None:
        p = self.conversations_dir / f"{conv_id}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(message, ensure_ascii=False) + "\n")

    def get_history(self, conv_id: str) -> list[dict]:
        p = self.conversations_dir / f"{conv_id}.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def truncate_conv_history(self, conv_id: str, keep_count: int) -> list[dict]:
        """Rewrite the conv JSONL keeping only the first `keep_count` messages.

        Used by edit-regenerate to drop the last human turn (and everything after
        it) before re-running with the edited question. Verbatim port of global
        `storage.ChatDataStore.truncate_conv_history`.
        """
        history = self.get_history(conv_id)
        if keep_count >= len(history):
            return history
        truncated = history[:keep_count]
        p = self.conversations_dir / f"{conv_id}.jsonl"
        with p.open("w", encoding="utf-8") as fh:
            for msg in truncated:
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return truncated


def get_chat_meta_owner(chat_id: str) -> Optional[str]:
    """Convenience: read the owner email out of meta.json."""
    p = _data_root() / "chatdata" / chat_id / "meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("owner")
    except Exception:
        return None


def chat_exists(chat_id: str) -> bool:
    return (_data_root() / "chatdata" / chat_id / "meta.json").exists()


def user_owns_conversation(email: str, chat_id: str, conv_id: str) -> bool:
    """True when `conv_id` is recorded in THIS user's own conversations index
    for `chat_id`.

    Conversation snapshots for shared recipients live in the owner's
    `ChatDataStore` conversations dir alongside the owner's own conversations,
    so directory presence is NOT proof of access. The per-user
    `conversations.jsonl` index is the source of truth for which conversations
    a given user may read/continue/export. Used to scope shared (non-owner)
    recipients to their own conversations within a shared chat."""
    try:
        for c in AuthStore().list_conversations(email):
            if c.get("conv_id") == conv_id and c.get("chat_id") == chat_id:
                return True
    except Exception:
        return False
    return False
