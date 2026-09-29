"""Deck QA previews (`DATA_ROOT/pptx_previews/preview_<sid>.html`) are not
kept: nothing serves them, so the native render's preview is removed as soon
as the deck is built, and leftovers from an earlier release are swept once at
startup. The sweep never creates the folder."""
import routes.report as report
from settings import settings


def test_the_native_render_leaves_no_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(report, "_layout_plan_usable", lambda plan: True)
    monkeypatch.setattr(report, "_spec_deck_usable", lambda spec: False)
    import pptx_template_cache
    monkeypatch.setattr(pptx_template_cache, "get_template_and_spec",
                        lambda sid: {"has_template": True, "template_path": "t.pptx",
                                     "spec": None, "layout_plan": {"version": 3}})

    def fake_native(qa, structure, sid, path, plan):
        report._write_html_preview([], 13.33, 7.5, sid)
        assert (tmp_path / "pptx_previews" / f"preview_{sid}.html").is_file()
        return b"deck"

    monkeypatch.setattr(report, "_render_pptx_native", fake_native)
    assert report._render_pptx([], {}, "s_0000000000000abc") == b"deck"
    assert list((tmp_path / "pptx_previews").glob("*")) == []


def test_the_startup_sweep_removes_leftovers(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    base = tmp_path / "pptx_previews"
    base.mkdir()
    for n in range(3):
        (base / f"preview_s_{n}.html").write_text("x", encoding="utf-8")
    (base / "keep.txt").write_text("x", encoding="utf-8")
    assert report.sweep_html_previews() == 3
    assert [p.name for p in base.iterdir()] == ["keep.txt"]


def test_the_sweep_does_not_create_the_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    assert report.sweep_html_previews() == 0
    assert not (tmp_path / "pptx_previews").exists()
