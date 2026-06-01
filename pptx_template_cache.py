"""Client-side cache of the tenant's branded .pptx template + style-spec.

The brain owns the source of truth (operator uploads + LLM analysis happens
there). The client fetches both at render time, but caches them under
`DATA_ROOT/templates_cache/` so we don't re-download on every export.

Cache TTL is short on purpose: if an operator re-uploads a template in the
admin portal, the running client picks it up within `_TTL_SECONDS` without a
restart — same spirit as `brain_client.get_app_settings()` doing a fresh
fetch on each request.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import brain_client
from logger_utils import log_with_sid
from settings import settings


_TTL_SECONDS = 60.0
_CACHE_LOCK = threading.Lock()

# Bump this whenever the on-disk spec shape changes — older caches with a
# mismatching schema marker are dropped on read so the next call refreshes
# them from the brain. v2 = "build plan" (per-shape keep/drop/replace);
# v3 = "design spec" (design.md + layout_plan.json native render).
_CACHE_SCHEMA = "v3"


def _cache_dir() -> Path:
    d = Path(settings.DATA_ROOT) / "templates_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _template_path() -> Path:
    # Schema-versioned filename so a stale v1 file from before this upgrade
    # is ignored (the rebuilt image won't see it as the current cache).
    return _cache_dir() / f"pptx_template.{_CACHE_SCHEMA}.pptx"


def _spec_path() -> Path:
    return _cache_dir() / f"pptx_template_spec.{_CACHE_SCHEMA}.json"


def _plan_path() -> Path:
    return _cache_dir() / f"layout_plan.{_CACHE_SCHEMA}.json"


def _stamp_path() -> Path:
    return _cache_dir() / f"_fetched_at.{_CACHE_SCHEMA}.txt"


def _purge_old_schema_caches() -> None:
    """Delete any older-schema cache files left over from before this upgrade.
    Safe no-op when none exist (e.g. fresh client volume). Removes the raw v1
    names plus any explicitly-versioned file whose marker != the current one."""
    d = _cache_dir()
    stale = [
        "pptx_template.pptx", "pptx_template_spec.json", "_fetched_at.txt",
        "pptx_template.v2.pptx", "pptx_template_spec.v2.json",
        "_fetched_at.v2.txt", "layout_plan.v2.json",
    ]
    for name in stale:
        try:
            (d / name).unlink(missing_ok=True)
        except Exception:
            pass


def _fresh() -> bool:
    p = _stamp_path()
    if not p.exists():
        return False
    try:
        ts = float(p.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return False
    return (time.time() - ts) < _TTL_SECONDS


def _write_stamp() -> None:
    try:
        _stamp_path().write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass


def _refresh(sid: str) -> dict:
    """Pull fresh template + spec from the brain. Always returns a dict
    `{has_template: bool, spec: dict|None, template_path: Path|None}`."""
    # Best-effort cleanup of any stale v1 cache files left over from before
    # the build-plan upgrade — does nothing on a fresh client volume.
    _purge_old_schema_caches()
    rsp = brain_client.get_pptx_template_spec()
    has = bool(rsp.get("has_template"))
    spec = rsp.get("spec") if isinstance(rsp.get("spec"), dict) else None
    deck = (spec or {}).get("deck") or {}
    log_with_sid(sid, "info", "PPTX_CACHE_REFRESH",
                 has_template=has,
                 schema=_CACHE_SCHEMA,
                 spec_version=(spec or {}).get("version"),
                 cover=deck.get("cover_slide_index"),
                 agenda=deck.get("agenda_slide_index"),
                 content=deck.get("content_slide_index"))

    template_path: Path | None = None
    if has:
        blob = brain_client.download_pptx_template()
        if blob and blob[:2] == b"PK":
            try:
                _template_path().write_bytes(blob)
                template_path = _template_path()
            except Exception as e:
                log_with_sid(sid, "warning", f"PPTX_TEMPLATE_CACHE_WRITE_FAIL: {e}")
                template_path = None
        else:
            has = False  # treat unreadable/empty download as no-template
            template_path = None
    if not has:
        # Drop any stale cached file so next render falls back to general.
        try:
            if _template_path().exists():
                _template_path().unlink()
        except Exception:
            pass

    try:
        if spec is not None:
            _spec_path().write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        elif _spec_path().exists():
            _spec_path().unlink()
    except Exception as e:
        log_with_sid(sid, "warning", f"PPTX_SPEC_CACHE_WRITE_FAIL: {e}")

    # ── version-3 layout plan (the NATIVE renderer's source of truth) ──
    layout_plan = None
    try:
        prsp = brain_client.get_pptx_layout_plan()
        if prsp.get("has_plan") and isinstance(prsp.get("plan"), dict):
            layout_plan = prsp["plan"]
    except Exception as e:
        log_with_sid(sid, "warning", f"PPTX_PLAN_FETCH_FAIL: {e}")
    try:
        if layout_plan is not None:
            _plan_path().write_text(json.dumps(layout_plan, ensure_ascii=False), encoding="utf-8")
        elif _plan_path().exists():
            _plan_path().unlink()
    except Exception as e:
        log_with_sid(sid, "warning", f"PPTX_PLAN_CACHE_WRITE_FAIL: {e}")
    log_with_sid(sid, "info", "PPTX_PLAN_REFRESH",
                 has_plan=isinstance(layout_plan, dict),
                 plan_version=(layout_plan or {}).get("version"))

    _write_stamp()
    return {"has_template": has, "spec": spec, "template_path": template_path,
            "layout_plan": layout_plan}


def get_template_and_spec(sid: str = "pptx_cache") -> dict:
    """Return `{has_template, spec, template_path, layout_plan}` for the tenant.

    Uses the on-disk cache if it is fresh; otherwise refreshes via the brain.
    Never raises — returns the empty bundle on any error so the caller falls
    back to the built-in renderer.
    """
    empty = {"has_template": False, "spec": None, "template_path": None,
             "layout_plan": None}
    with _CACHE_LOCK:
        if _fresh() and _template_path().exists() and _spec_path().exists():
            try:
                spec = json.loads(_spec_path().read_text(encoding="utf-8"))
                plan = None
                if _plan_path().exists():
                    try:
                        plan = json.loads(_plan_path().read_text(encoding="utf-8"))
                    except Exception:
                        plan = None
                return {
                    "has_template": True,
                    "spec": spec if isinstance(spec, dict) else None,
                    "template_path": _template_path(),
                    "layout_plan": plan if isinstance(plan, dict) else None,
                }
            except Exception:
                pass
        try:
            return _refresh(sid)
        except Exception as e:
            log_with_sid(sid, "warning", f"PPTX_CACHE_REFRESH_FAIL: {e}")
            return dict(empty)
