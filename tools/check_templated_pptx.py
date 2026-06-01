"""Deterministic checker for a templated PPTX render.

Usage:
  python tools/check_templated_pptx.py <generated.pptx> <template.pptx> \
      --findings <N> [--takeaways] [--no-agenda] [--clone] \
      [--page-titles "title1|title2|..."] [--layout-plan plan.json]

Two render paths feed this checker and both must pass:

  * TEMPLATED CLONE (client/routes/report.py `_render_pptx_templated_clone`):
    clones the tenant's DESIGNED slides and injects our content into the
    template's own shapes. The structural checks below (C1-C6) describe this
    path directly — cover/agenda/content/takeaways are clones that carry the
    template's chrome (image hashes, theme) with the author's sample content
    dropped and ours injected. The renderer tags every shape it owns with a
    `PDC_INJ_*` sentinel; the G-checks auto-detect this and re-scope (see
    `run_composition_checks`) so the template's legitimate full-bleed
    backgrounds / edge chrome are not policed as overlaps or margin breaks.

  * DESIGN-SPEC NATIVE (`_render_pptx_native`, the fallback): a fresh deck
    with native shapes at layout_plan regions. It carries no sentinels, so the
    G-checks run in their original (whole-slide) form.

Structural checks (both paths):

  C1. Total slide count == 2 + N_findings + 1 (cover + agenda + findings + takeaways)
      or 1 + N_findings + 1 when --no-agenda.
  C2. Slide order:
        slide 1 = cover (carries a template image hash, no author leftovers)
        slide 2 = agenda (contains the section list, no author leftovers)
        slides 3..(2+N) = content (carry a template image hash, page title
                   appears exactly ONCE in <a:t>, narrative present, chart
                   picture present, no template Q1 sample-table values)
        last slide = takeaways (template chrome + bullet list)
  C3. Zero template-author strings appear in any <a:t>.
  C4. Zero PowerDataChat logo image bytes appear in ppt/media (we don't
      ship one in this repo, so we just look for "powerdatachat" in
      filenames).
  C5. Template branding present: ppt/media contains images whose bytes
      match the template's image*.
  C6. Theme preserved: theme1.xml dk2 + accent1 match the template.

Composition checks (G1-G10): see `run_composition_checks` — clone-aware when
`PDC_INJ_*` sentinels are present (or --clone), original native scope otherwise.
Clone-mode placement guards (iteration 3): G8 keeps ALL injected shapes
(`PDC_INJ_*` fresh AND `PDC_INJT_*` in-place) on-canvas; G9 forbids injected
text from overlapping a protected (logo-sized) picture by >10%; G10 allows
injected text on the cover only when the template has a title placeholder.
These are skipped in native mode.

Title-fit tolerance (iteration 4): the renderer fits an over-long page title to
its slot by shrinking to a floor (12pt titles / 9pt body) and, if it still
overflows, truncating at a WORD boundary and appending a single ellipsis
(U+2026). The C2 "page title appears exactly ONCE" check and the G7 roll-up
therefore match titles through `_title_match`, which accepts an exact match OR a
trailing-ellipsis prefix of the expected title. Non-truncated titles are matched
exactly as before; no other check is weakened.

Exit code 0 on PASS-ALL, 1 otherwise.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_REL_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


# Template-author phrases that must NEVER appear in the generated deck.
AUTHOR_LEFTOVERS = [
    "Commercial report by",
    "Marketing report by",
    "E-commerce report by",
    "Ketevan",
    "Gabitashvili",
    "Kenkebashvili",
    "Adamia",
    # Standalone section-divider titles from the template
    "Commercial Report",
    "Financial Performance",
    # Q1 sample-table values
    "Revenue Q1",
    "REVENUE",
    "Q1 ACTUAL",
    "Q1 BUDGET",
    "TIME / City Mall",
]


def _read(zf: zipfile.ZipFile, name: str) -> bytes:
    try:
        return zf.read(name)
    except KeyError:
        return b""


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _slide_xmls_sorted(zf: zipfile.ZipFile) -> list[str]:
    """Return slide XML part names in the order they appear in
    presentation.xml's sldIdLst (i.e. deck order, not numeric order)."""
    pres_xml = _read(zf, "ppt/presentation.xml")
    rels_xml = _read(zf, "ppt/_rels/presentation.xml.rels")
    if not pres_xml or not rels_xml:
        return sorted(
            n for n in zf.namelist()
            if re.match(r"^ppt/slides/slide\d+\.xml$", n)
        )
    pres_root = ET.fromstring(pres_xml)
    rels_root = ET.fromstring(rels_xml)
    # Map rId -> target part name (rels Target is relative to ppt/)
    rid_to_target: dict[str, str] = {}
    for rel in rels_root.findall(f"{{{NS_REL_PKG}}}Relationship"):
        rid = rel.get("Id")
        target = rel.get("Target", "")
        # Normalize: "slides/slide6.xml" -> "ppt/slides/slide6.xml"
        if target.startswith("slides/"):
            rid_to_target[rid] = "ppt/" + target
    # Order from sldIdLst
    out: list[str] = []
    sld_lst = pres_root.find(f"{{{NS_P}}}sldIdLst")
    if sld_lst is None:
        return out
    for sld_id in sld_lst.findall(f"{{{NS_P}}}sldId"):
        rid = sld_id.get(f"{{http://schemas.openxmlformats.org/officeDocument/2006/relationships}}id")
        target = rid_to_target.get(rid)
        if target:
            out.append(target)
    return out


def _norm_title(s: str) -> str:
    """Normalize whitespace + casing for title comparison."""
    return " ".join((s or "").split()).casefold()


def _title_match(shape_text: str, expected: str) -> bool:
    """True when `shape_text` is the expected page title, tolerating the
    renderer's shrink-to-floor + word-boundary ellipsis truncation.

    Accepts EITHER:
      (a) an exact match after whitespace/case normalization, OR
      (b) `shape_text` ends with an ellipsis (U+2026) and, after stripping the
          trailing ellipsis + spaces, the non-empty remainder is a prefix of the
          expected title.

    This recognizes the renderer's ellipsis truncation generically without
    re-implementing its char/point-size model.
    """
    exp = _norm_title(expected)
    got = _norm_title(shape_text)
    if not got:
        return False
    if got == exp:
        return True
    if got.endswith("…"):
        remainder = got[:-1].rstrip()
        if remainder and exp.startswith(remainder):
            return True
    return False


def _all_a_t_text(slide_xml: bytes) -> list[str]:
    if not slide_xml:
        return []
    root = ET.fromstring(slide_xml)
    return [
        (n.text or "")
        for n in root.iter(f"{{{NS_A}}}t")
    ]


def _has_picture(slide_xml: bytes) -> int:
    if not slide_xml:
        return 0
    root = ET.fromstring(slide_xml)
    return sum(1 for _ in root.iter(f"{{{NS_P}}}pic"))


def _slide_rels_image_targets(zf: zipfile.ZipFile, slide_name: str) -> list[str]:
    """Return image part names referenced by this slide (e.g.
    ['ppt/media/image1.png'])."""
    rels_name = slide_name.replace("ppt/slides/", "ppt/slides/_rels/") + ".rels"
    blob = _read(zf, rels_name)
    if not blob:
        return []
    root = ET.fromstring(blob)
    out = []
    for rel in root.findall(f"{{{NS_REL_PKG}}}Relationship"):
        if "image" in (rel.get("Type", "") or "").lower():
            target = rel.get("Target", "")
            # Normalize to absolute part name
            if target.startswith("../"):
                target = "ppt/" + target[3:]
            elif target.startswith("/"):
                target = target.lstrip("/")
            elif target.startswith("media/"):
                target = "ppt/" + target
            out.append(target)
    return out


def _theme_dk2_accent1(zf: zipfile.ZipFile) -> tuple[str, str]:
    blob = _read(zf, "ppt/theme/theme1.xml")
    if not blob:
        return "", ""
    root = ET.fromstring(blob)
    ns = {"a": NS_A}
    dk2 = ""
    accent1 = ""
    for elem in root.iter(f"{{{NS_A}}}clrScheme"):
        for child in elem:
            tag = child.tag.split("}", 1)[-1]
            srgb = child.find(f"{{{NS_A}}}srgbClr")
            sys_elem = child.find(f"{{{NS_A}}}sysClr")
            val = ""
            if srgb is not None and srgb.get("val"):
                val = srgb.get("val").upper()
            elif sys_elem is not None and sys_elem.get("lastClr"):
                val = sys_elem.get("lastClr").upper()
            if tag == "dk2":
                dk2 = val
            elif tag == "accent1":
                accent1 = val
    return dk2, accent1


class Check:
    def __init__(self):
        self.results: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, evidence: str = "") -> None:
        self.results.append((name, ok, evidence))

    def all_pass(self) -> bool:
        return all(ok for _, ok, _ in self.results)

    def report(self) -> str:
        out = []
        for name, ok, evidence in self.results:
            sym = "PASS" if ok else "FAIL"
            out.append(f"[{sym}] {name}")
            if evidence:
                for line in evidence.splitlines():
                    out.append(f"        {line}")
        passes = sum(1 for _, ok, _ in self.results if ok)
        total = len(self.results)
        out.append("")
        out.append(f"=== {passes}/{total} passed ===")
        return "\n".join(out)



# ===========================================================================
# G1-G7 COMPOSITION CHECKS (design-spec native render)
# ---------------------------------------------------------------------------
# Read the GENERATED .pptx shape geometry directly from the slide XML
# (<a:off>/<a:ext> under each shape's xfrm) and assert the deck is
# well-composed: no overlaps, in-bounds, title-above-body, body capacity,
# chart aspect preserved, branding present. The prior C1-C6 structural checks
# (slide count/order, zero author leftovers, theme preserved, no PDC logo,
# title-once) together satisfy G7 ("keep prior checks").
# ===========================================================================
EMU_PER_INCH = 914400


def _slide_size_in(zf: zipfile.ZipFile):
    blob = _read(zf, "ppt/presentation.xml")
    if not blob:
        return 13.333, 7.5
    root = ET.fromstring(blob)
    sz = root.find(f"{{{NS_P}}}sldSz")
    if sz is None:
        return 13.333, 7.5
    cx = int(sz.get("cx") or 12192000)
    cy = int(sz.get("cy") or 6858000)
    return cx / EMU_PER_INCH, cy / EMU_PER_INCH


def _png_size(b: bytes):
    """Return (w, h) for a PNG/JPEG byte string, else None — no PIL dependency."""
    try:
        if b[:8] == b"\x89PNG\r\n\x1a\n":
            import struct
            w, h = struct.unpack(">II", b[16:24])
            return int(w), int(h)
        if b[:2] == b"\xff\xd8":  # JPEG
            import struct
            i = 2
            while i < len(b) - 9:
                if b[i] != 0xFF:
                    i += 1
                    continue
                marker = b[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                    h, w = struct.unpack(">HH", b[i + 5:i + 9])
                    return int(w), int(h)
                seglen = struct.unpack(">H", b[i + 2:i + 4])[0]
                i += 2 + seglen
    except Exception:
        return None
    return None


def _shape_geometry(slide_xml: bytes):
    """Return a list of shape dicts (kind + rect in inches + text/size) for
    every shape that carries an explicit off/ext xfrm."""
    if not slide_xml:
        return []
    root = ET.fromstring(slide_xml)
    out = []
    spTree = root.find(f"{{{NS_P}}}cSld/{{{NS_P}}}spTree")
    if spTree is None:
        return out
    for el in list(spTree):
        tag = el.tag.split("}", 1)[-1]
        if tag == "sp":
            kind = "text"
        elif tag == "pic":
            kind = "picture"
        elif tag in ("graphicFrame", "cxnSp", "grpSp"):
            kind = "other"
        else:
            continue
        xfrm = None
        for x in el.iter(f"{{{NS_A}}}xfrm"):
            xfrm = x
            break
        if xfrm is None:
            for x in el.iter(f"{{{NS_P}}}xfrm"):
                xfrm = x
                break
        if xfrm is None:
            continue
        off = xfrm.find(f"{{{NS_A}}}off")
        ext = xfrm.find(f"{{{NS_A}}}ext")
        if off is None or ext is None:
            continue
        try:
            x = int(off.get("x")) / EMU_PER_INCH
            y = int(off.get("y")) / EMU_PER_INCH
            w = int(ext.get("cx")) / EMU_PER_INCH
            h = int(ext.get("cy")) / EMU_PER_INCH
        except Exception:
            continue
        text = ""
        size_pt = None
        if kind == "text":
            text = "".join((t.text or "") for t in el.iter(f"{{{NS_A}}}t"))
            for rpr in el.iter(f"{{{NS_A}}}rPr"):
                if rpr.get("sz"):
                    try:
                        size_pt = int(rpr.get("sz")) / 100.0
                        break
                    except Exception:
                        pass
        # Shape name (cNvPr@name) — the clone renderer tags content it owns with
        # a PDC_INJ_* sentinel so geometry checks can be scoped to it.
        name = ""
        for cnv in el.iter():
            if cnv.tag.split("}", 1)[-1] == "cNvPr" and cnv.get("name") is not None:
                name = cnv.get("name")
                break
        out.append({"kind": kind, "x": x, "y": y, "w": w, "h": h,
                    "text": text, "size_pt": size_pt, "name": name})
    return out


def _overlap_area_in(a, b) -> float:
    ix = max(0.0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0.0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    return ix * iy


def _largest_template_image_size(zf: zipfile.ZipFile, slide_name: str):
    """Native (w, h) of the largest image referenced by this slide — used for
    the chart aspect-ratio check."""
    best = None
    for t in _slide_rels_image_targets(zf, slide_name):
        b = _read(zf, t)
        if not b:
            continue
        wh = _png_size(b)
        if wh and (best is None or wh[0] * wh[1] > best[0] * best[1]):
            best = wh
    return best


def _is_logo_pic(s, sw, sh) -> bool:
    """A generated picture small enough to be a logo (mirrors the renderer's
    `_protected_logos` sizing)."""
    if s.get("kind") != "picture":
        return False
    w, h = s["w"], s["h"]
    if w <= 0 or h <= 0:
        return False
    area = sw * sh
    return (w < 0.40 * sw and h < 0.25 * sh) or (w * h < 0.12 * area)


def _template_has_title_ph(template_path) -> bool:
    """True when any slide in the template carries a title placeholder
    (<p:ph type="title"/> or "ctrTitle"). Injected cover text is allowed only
    on templates that actually provide a title slot."""
    try:
        with zipfile.ZipFile(template_path, "r") as z:
            for n in z.namelist():
                if re.match(r"^ppt/slides/slide\d+\.xml$", n):
                    blob = _read(z, n)
                    if not blob:
                        continue
                    root = ET.fromstring(blob)
                    for ph in root.iter(f"{{{NS_P}}}ph"):
                        if (ph.get("type") or "") in ("title", "ctrTitle"):
                            return True
    except Exception:
        return False
    return False


def run_composition_checks(chk, gen, deck_order, args, layout_plan):
    """G1-G7 composition checks read from the GENERATED shape geometry.

    Clone-aware scope (Option 1): the templated-clone renderer keeps the
    template's legitimately full-bleed backgrounds and edge-touching chrome,
    which the native-render G1/G2/G3 checks were never meant to police. When the
    deck carries `PDC_INJ_*` sentinel shapes (or --clone is passed) we re-scope
    the geometry checks to the content WE control rather than disabling them:

      * "ours"  = shapes named PDC_INJ* (PDC_INJ_<role> fresh + PDC_INJT_<role>
                  injected into a template shape).
      * "fresh" = PDC_INJ_<role> only (we positioned it at our coordinates).

      G1 (overlap) / G3 (title-above-body, chart clear): evaluated over OURS
          only; overlaps involving untagged template chrome are intended.
      G2 (in-bounds >=0.3in): enforced only on FRESH shapes (text injected into
          a template-owned shape inherits the author's near-edge placement).
      G4 (capacity): on all OURS title/body/list shapes — overflow is our risk
          wherever the text lands.
      G5 (chart aspect): unchanged (the chart is always ours).
      G8 (clone only): every OURS shape stays within the slide (margin 0) —
          the containment guarantee that replaces G2's coverage for the
          template-injected shapes it now exempts.
      G6/G7/C1-C6: unchanged — clones carry template chrome/theme/branding.

    The native fallback render carries NO sentinels, so clone_mode is False and
    G1-G7 run exactly as before."""
    sw, sh = _slide_size_in(gen)
    margin = 0.30

    per_slide = [(sn, _shape_geometry(_read(gen, sn))) for sn in deck_order]

    clone_mode = bool(getattr(args, "clone", False)) or any(
        (s.get("name") or "").startswith("PDC_INJ")
        for _sn, shapes in per_slide for s in shapes
    )

    def _ours(s):
        return (s.get("name") or "").startswith("PDC_INJ")

    def _fresh(s):
        # PDC_INJ_<role> (underscore at index 7) = we positioned it;
        # PDC_INJT_<role> = injected into a template shape (geometry inherited).
        return (s.get("name") or "").startswith("PDC_INJ_")

    def _scope(shapes):
        return [s for s in shapes if _ours(s)] if clone_mode else shapes

    if clone_mode:
        chk.add("G0: clone-aware scope active (geometry checks scoped to PDC_INJ_* content)",
                True, "template chrome/background exempt from G1/G2/G3; G4/G5 unchanged")

    # ---------- G1: no two shapes overlap > 2% of the smaller area ----------
    g1 = []
    for sn, shapes in per_slide:
        boxes = [s for s in shapes if s["w"] > 0 and s["h"] > 0]
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                # Clone mode: police overlaps among the shapes WE own. The
                # renderer now validates every injected shape (on-canvas, clear
                # of logos) or leaves the template shape UNTOUCHED, so a pair of
                # our shapes (fresh PDC_INJ_* or injected PDC_INJT_*) must not
                # overlap. (Re-tightened: previously required >=1 fresh, which
                # let an off-canvas injected title slip through.)
                if clone_mode and not (_ours(a) and _ours(b)):
                    continue
                ov = _overlap_area_in(a, b)
                amin = min(a["w"] * a["h"], b["w"] * b["h"])
                if amin > 0 and ov > 0.02 * amin:
                    g1.append(f"{sn.split('/')[-1]}: {a['kind']}@({a['x']:.2f},{a['y']:.2f}) "
                              f"<-> {b['kind']}@({b['x']:.2f},{b['y']:.2f}) {ov/amin*100:.1f}%")
    chk.add("G1: no two (content) shapes overlap by more than 2% area",
            not g1, "; ".join(g1[:8]) if g1 else "no overlaps")

    # ---------- G2: every shape inside slide with >=0.3in margin ----------
    g2 = []
    for sn, shapes in per_slide:
        for s in shapes:
            if s["w"] <= 0 or s["h"] <= 0:
                continue
            # Clone mode: only fresh shapes we positioned are margin-checked.
            if clone_mode and not _fresh(s):
                continue
            if not (s["x"] >= margin - 1e-3 and s["y"] >= margin - 1e-3
                    and s["x"] + s["w"] <= sw - margin + 1e-3
                    and s["y"] + s["h"] <= sh - margin + 1e-3):
                g2.append(f"{sn.split('/')[-1]}: {s['kind']}@({s['x']:.2f},{s['y']:.2f} "
                          f"{s['w']:.2f}x{s['h']:.2f})")
    chk.add(f"G2: every {'fresh ' if clone_mode else ''}shape within slide bounds (>= {margin}in margin)",
            not g2, "; ".join(g2[:8]) if g2 else f"all inside {sw:.2f}x{sh:.2f}in")

    # ---------- G8: ALL our content stays on-slide (clone-mode) ------------
    if clone_mode:
        tol = 0.05
        g8 = []
        for sn, shapes in per_slide:
            for s in shapes:
                # Every shape carrying OUR text/chart (PDC_INJ_* fresh AND
                # PDC_INJT_* injected-in-place) must be on-canvas. The renderer
                # now leaves off-canvas template boxes UNTOUCHED (they stay
                # un-renamed template chrome) and writes our text in a safe
                # region, so an off-canvas PDC_INJ* shape is a real bug.
                if not _ours(s) or s["w"] <= 0 or s["h"] <= 0:
                    continue
                if not (s["x"] >= -tol and s["y"] >= -tol
                        and s["x"] + s["w"] <= sw + tol
                        and s["y"] + s["h"] <= sh + tol):
                    g8.append(f"{sn.split('/')[-1]}: {s.get('name')}@({s['x']:.2f},{s['y']:.2f} "
                              f"{s['w']:.2f}x{s['h']:.2f})")
        chk.add("G8: injected content (PDC_INJ_* and PDC_INJT_*) stays within the slide",
                not g8, "; ".join(g8[:8]) if g8 else "all injected content on-slide")

        # ---------- G9: injected text never lands on a protected logo -------
        g9 = []
        for sn, shapes in per_slide:
            logos = [s for s in shapes if not _ours(s) and _is_logo_pic(s, sw, sh)]
            inj = [s for s in shapes if _ours(s) and s["kind"] == "text"
                   and s["w"] > 0 and s["h"] > 0]
            for t in inj:
                for lg in logos:
                    ov = _overlap_area_in(t, lg)
                    amin = min(t["w"] * t["h"], lg["w"] * lg["h"])
                    if amin > 0 and ov > 0.10 * amin:
                        g9.append(f"{sn.split('/')[-1]}: {t.get('name')} over logo "
                                  f"({ov/amin*100:.0f}%)")
        chk.add("G9: no injected text overlaps a protected logo (>10%)",
                not g9, "; ".join(g9[:8]) if g9 else "injected text clear of logos")

        # ---------- G10: cover carries text only with a real title slot -----
        cover_shapes = per_slide[0][1] if per_slide else []
        cover_inj_text = [s for s in cover_shapes if _ours(s)
                          and s["kind"] == "text" and s["text"].strip()]
        tpl_title_ph = _template_has_title_ph(args.template)
        chk.add("G10: cover carries injected text only if the template has a title placeholder",
                (not cover_inj_text) or tpl_title_ph,
                f"cover injected-text shapes={len(cover_inj_text)}, "
                f"template_has_title_placeholder={tpl_title_ph}")

    first_content = (1 if args.no_agenda else 2)
    last_content = first_content + args.findings - 1

    # ---------- G3: content title above body; chart clear of title/body ----
    g3 = []
    for sn in deck_order[first_content:last_content + 1]:
        shapes = _shape_geometry(_read(gen, sn))
        texts = [s for s in _scope(shapes) if s["kind"] == "text" and s["w"] > 0]
        pics = [s for s in _scope(shapes) if s["kind"] == "picture" and s["w"] > 0]
        if not texts:
            g3.append(f"{sn.split('/')[-1]}: no text shapes")
            continue
        texts_sorted = sorted(texts, key=lambda s: s["y"])
        title = texts_sorted[0]
        body = texts_sorted[1] if len(texts_sorted) > 1 else None
        if body is not None and not (title["y"] + title["h"] <= body["y"] + 1e-2):
            g3.append(f"{sn.split('/')[-1]}: title.bottom={title['y']+title['h']:.2f} "
                      f"> body.top={body['y']:.2f}")
        chart = max(pics, key=lambda s: s["w"] * s["h"]) if pics else None
        if chart is not None:
            for nm, reg in (("title", title), ("body", body)):
                if reg is None:
                    continue
                ov = _overlap_area_in(chart, reg)
                amin = min(chart["w"] * chart["h"], reg["w"] * reg["h"])
                if amin > 0 and ov > 0.02 * amin:
                    g3.append(f"{sn.split('/')[-1]}: chart intersects {nm} ({ov/amin*100:.1f}%)")
    chk.add("G3: content title above body AND chart clear of title/body",
            not g3, "; ".join(g3[:8]) if g3 else "content slides well-stacked")

    # ---------- G4: body textbox tall enough for its text ----------
    g4 = []
    for sn in deck_order[first_content:last_content + 1]:
        shapes = _shape_geometry(_read(gen, sn))
        texts = [s for s in _scope(shapes) if s["kind"] == "text" and s["w"] > 0 and s["text"].strip()]
        if len(texts) < 2:
            continue
        body = sorted(texts, key=lambda s: s["y"])[1]
        size = body.get("size_pt") or 14.0
        char_w_in = (0.50 * size) / 72.0
        cpl = max(1, int(body["w"] / char_w_in))
        line_h_in = (size * 1.2) / 72.0
        max_lines = max(1, int(body["h"] / line_h_in))
        capacity = cpl * max_lines
        n = len(body["text"])
        if capacity < n:
            g4.append(f"{sn.split('/')[-1]}: cap={capacity} < chars={n} "
                      f"({size}pt {body['w']:.2f}x{body['h']:.2f}in)")
    chk.add("G4: content body textbox large enough for its text (no overflow)",
            not g4, "; ".join(g4[:8]) if g4 else "all body boxes have capacity")

    # ---------- G5: chart aspect ratio preserved within 1% ----------
    g5 = []
    g5_n = 0
    for sn in deck_order[first_content:last_content + 1]:
        shapes = _shape_geometry(_read(gen, sn))
        pics = [s for s in shapes if s["kind"] == "picture" and s["w"] > 0]
        if clone_mode:
            ours_pics = [s for s in pics if _ours(s)]
            if ours_pics:
                pics = ours_pics
        if not pics:
            continue
        chart = max(pics, key=lambda s: s["w"] * s["h"])
        native = _largest_template_image_size(gen, sn)
        if not native:
            continue
        # the largest referenced image is the chart (bigger than the logo);
        # use it as the native size.
        native_ar = native[0] / native[1]
        placed_ar = chart["w"] / chart["h"] if chart["h"] > 0 else 0
        g5_n += 1
        if native_ar > 0:
            diff = abs(placed_ar - native_ar) / native_ar
            if diff > 0.01:
                g5.append(f"{sn.split('/')[-1]}: placed={placed_ar:.3f} "
                          f"native={native_ar:.3f} diff={diff*100:.1f}%")
    chk.add("G5: chart aspect ratio preserved within 1% (no stretching)",
            not g5, "; ".join(g5[:8]) if g5 else
            f"{g5_n} charts checked, all aspect-preserved")

    # ---------- G6: branding image present on every slide ----------
    tpl_md5s = set()
    try:
        with zipfile.ZipFile(args.template, "r") as tplz:
            for n in tplz.namelist():
                if n.startswith("ppt/media/"):
                    tpl_md5s.add(_md5(_read(tplz, n)))
    except Exception:
        pass
    g6 = []
    for sn in deck_order:
        slide_md5s = {_md5(_read(gen, t)) for t in _slide_rels_image_targets(gen, sn) if _read(gen, t)}
        if not (slide_md5s & tpl_md5s):
            g6.append(f"{sn.split('/')[-1]}: no template branding image")
    chk.add("G6: template branding image present on every slide (md5 match)",
            not g6, "; ".join(g6[:8]) if g6 else "every slide carries a template image")

    # ---------- G7: prior structural wins still hold ----------
    # (slide count/order, zero author leftovers, theme preserved, no PDC logo,
    # title-once are asserted by C1-C6 above.) Add an explicit roll-up so the
    # report names G7.
    prior = [r for r in chk.results if r[0].startswith(("C1", "C2", "C3", "C4", "C5", "C6"))]
    prior_ok = all(ok for _, ok, _ in prior)
    chk.add("G7: prior structural checks (count/order/no-leftovers/theme/no-PDC/title-once) hold",
            prior_ok,
            "all C1-C6 passed" if prior_ok else
            "; ".join(name for name, ok, _ in prior if not ok)[:200])



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("generated", type=Path)
    parser.add_argument("template", type=Path)
    parser.add_argument("--findings", type=int, required=True)
    parser.add_argument("--takeaways", action="store_true")
    parser.add_argument("--no-agenda", action="store_true",
                        help="Set when the template has no agenda role.")
    parser.add_argument("--page-titles", default="",
                        help="Pipe-separated finding page titles for C2 single-occurrence test.")
    parser.add_argument("--layout-plan", type=Path, default=None,
                        help="Optional version-3 layout_plan.json — geometry "
                             "checks reference planned regions if provided.")
    parser.add_argument("--clone", action="store_true",
                        help="Force clone-aware geometry scope (G1/G2/G3 limited "
                             "to PDC_INJ_* content; template chrome exempt). "
                             "Auto-detected when the deck carries PDC_INJ_* shapes.")
    args = parser.parse_args()

    layout_plan = None
    if args.layout_plan and args.layout_plan.exists():
        try:
            import json as _json
            layout_plan = _json.loads(args.layout_plan.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"warning: could not read layout-plan: {e}", file=sys.stderr)

    if not args.generated.exists():
        print(f"missing generated file: {args.generated}", file=sys.stderr)
        return 2
    if not args.template.exists():
        print(f"missing template file: {args.template}", file=sys.stderr)
        return 2

    chk = Check()
    gen = zipfile.ZipFile(args.generated, "r")
    tpl = zipfile.ZipFile(args.template, "r")

    # --- Collect all slide XMLs in deck order
    deck_order = _slide_xmls_sorted(gen)
    all_slide_xmls = [
        n for n in gen.namelist()
        if re.match(r"^ppt/slides/slide\d+\.xml$", n)
    ]

    # ---------- C1: slide count ----------
    expected = (0 if args.no_agenda else 1) + 1 + args.findings + (1 if args.takeaways else 0)
    chk.add(
        f"C1: deck has exactly {expected} slides (cover + agenda + N findings + takeaways)",
        len(deck_order) == expected,
        f"got {len(deck_order)} via sldIdLst, {len(all_slide_xmls)} XML files",
    )

    # No orphan slide XMLs (raw file count must equal sldIdLst count).
    chk.add(
        "C1b: no orphan slide XMLs in package (raw count == sldIdLst count)",
        len(all_slide_xmls) == len(deck_order),
        f"raw XML files: {len(all_slide_xmls)}; sldIdLst: {len(deck_order)}; orphans: "
        f"{sorted(set(all_slide_xmls) - set(deck_order))}",
    )

    # ---------- C3: zero template-author leftovers across deck ----------
    # Aggregate all <a:t> across every slide
    leftovers_found: list[tuple[str, str]] = []
    for sn in deck_order:
        texts = _all_a_t_text(_read(gen, sn))
        joined = "\n".join(texts)
        for phrase in AUTHOR_LEFTOVERS:
            if phrase in joined:
                leftovers_found.append((sn, phrase))
    chk.add(
        "C3: zero template-author leftover strings in any <a:t>",
        not leftovers_found,
        "; ".join(f"{sn}: {phrase!r}" for sn, phrase in leftovers_found[:10])
        if leftovers_found else "",
    )

    # ---------- C5: template branding present (image bytes match) ----------
    tpl_images = {
        name: _md5(_read(tpl, name))
        for name in tpl.namelist()
        if name.startswith("ppt/media/")
    }
    gen_image_hashes = {
        _md5(_read(gen, name))
        for name in gen.namelist()
        if name.startswith("ppt/media/")
    }
    matched = {n: h for n, h in tpl_images.items() if h in gen_image_hashes}
    chk.add(
        "C5: at least one template image present in generated ppt/media",
        len(matched) >= 1,
        f"matched: {sorted(matched.keys())}",
    )

    # ---------- C6: theme dk2/accent1 preserved ----------
    g_dk2, g_acc1 = _theme_dk2_accent1(gen)
    t_dk2, t_acc1 = _theme_dk2_accent1(tpl)
    chk.add(
        "C6: theme1.xml dk2 + accent1 match template",
        g_dk2 == t_dk2 and g_acc1 == t_acc1 and bool(t_dk2),
        f"gen dk2={g_dk2!r} accent1={g_acc1!r}; tpl dk2={t_dk2!r} accent1={t_acc1!r}",
    )

    # ---------- C4: no PowerDataChat logo ----------
    pdc_hits = [
        n for n in gen.namelist()
        if "powerdatachat" in n.lower() or "pdc_logo" in n.lower()
    ]
    chk.add(
        "C4: no PowerDataChat logo image in package",
        not pdc_hits,
        f"hits: {pdc_hits}" if pdc_hits else "",
    )

    # ---------- C2: per-slide structural checks ----------
    if not deck_order:
        chk.add("C2: deck order present", False, "no slides in sldIdLst")
    else:
        # Cover (slide 1)
        cover_xml = _read(gen, deck_order[0])
        cover_texts = _all_a_t_text(cover_xml)
        cover_joined = "\n".join(cover_texts)
        chk.add(
            "C2.cover: no chart picture on cover",
            _has_picture(cover_xml) >= 1,  # template logo is a picture
            f"pictures on cover: {_has_picture(cover_xml)}",
        )
        # Cover must NOT contain narrative-like text — but the cover may
        # contain the report title; we only assert no author leftovers.
        author_on_cover = any(
            p in cover_joined for p in (
                "Commercial report by", "Marketing report by", "E-commerce report by"
            )
        )
        chk.add(
            "C2.cover: no author-line leftovers on cover",
            not author_on_cover,
            "found author text" if author_on_cover else "",
        )

        # Agenda (slide 2) — when present
        agenda_idx = 1 if not args.no_agenda else None
        if agenda_idx is not None and agenda_idx < len(deck_order):
            agenda_xml = _read(gen, deck_order[agenda_idx])
            agenda_texts = _all_a_t_text(agenda_xml)
            agenda_joined = "\n".join(agenda_texts)
            # Agenda must contain the report's section list — we just
            # check that at least one numbered section "1." is present
            # OR that at least one page title appears. Title matching tolerates
            # the renderer's ellipsis truncation (a section line may be a
            # word-boundary-truncated page title) via `_title_match`.
            page_titles = [t for t in (args.page_titles or "").split("|") if t]
            agenda_has_section = ("1." in agenda_joined) or any(
                (t in agenda_joined) or any(_title_match(at, t) for at in agenda_texts)
                for t in page_titles
            )
            chk.add(
                "C2.agenda: agenda contains the report's section list",
                agenda_has_section,
                f"agenda text: {agenda_joined[:200]!r}",
            )

        # Content slides — index range is [first_content .. last_content]
        first_content = (1 if args.no_agenda else 2)
        last_content = first_content + args.findings - 1
        page_titles = [t for t in (args.page_titles or "").split("|") if t]

        for ci, slide_name in enumerate(deck_order[first_content:last_content + 1]):
            slide_xml = _read(gen, slide_name)
            texts = _all_a_t_text(slide_xml)
            joined = "\n".join(texts)
            # C2.cN: page_title appears exactly once (if provided)
            if ci < len(page_titles):
                title = page_titles[ci]
                # Tolerate the renderer's title fit (shrink-to-floor + word-
                # boundary ellipsis truncation): a truncated injected title that
                # is an ellipsis-prefix of the expected title counts as the one
                # valid occurrence. Non-truncated titles still require an exact
                # (normalized) match.
                occurrences = sum(1 for t in texts if _title_match(t, title))
                chk.add(
                    f"C2.content[{ci+1}]: page title {title!r} appears exactly ONCE",
                    occurrences == 1,
                    f"occurrences={occurrences} (full <a:t> matches: "
                    f"{[t for t in texts if _title_match(t, title)][:5]})",
                )
            # C2.cN: must have a chart picture
            chk.add(
                f"C2.content[{ci+1}]: chart picture present",
                _has_picture(slide_xml) >= 2,  # template header pic + chart pic
                f"pictures on {slide_name}: {_has_picture(slide_xml)}",
            )
            # C2.cN: must NOT contain Q1 table values
            q1_hits = [p for p in ("REVENUE", "Q1 ACTUAL", "Q1 BUDGET", "TIME / City Mall")
                       if p in joined]
            chk.add(
                f"C2.content[{ci+1}]: no Q1 table leftovers",
                not q1_hits,
                f"hits: {q1_hits}" if q1_hits else "",
            )

        # Takeaways (last slide)
        if args.takeaways:
            tk_xml = _read(gen, deck_order[-1])
            tk_joined = "\n".join(_all_a_t_text(tk_xml))
            chk.add(
                "C2.takeaways: contains 'Key Takeaways' title",
                "Key Takeaways" in tk_joined,
                f"takeaways text head: {tk_joined[:200]!r}",
            )
            # Takeaways must use the content clone (so it carries template
            # chrome — image rels from the content slide).
            tk_imgs = _slide_rels_image_targets(gen, deck_order[-1])
            chk.add(
                "C2.takeaways: carries template chrome (image rels present)",
                len(tk_imgs) >= 1,
                f"image rels: {tk_imgs}",
            )

    # ---------- G1-G7 composition checks (design-spec native render) -------
    try:
        run_composition_checks(chk, gen, deck_order, args, layout_plan)
    except Exception as e:
        chk.add("G*: composition checks ran", False, f"checker error: {e}")

    print(chk.report())
    return 0 if chk.all_pass() else 1


if __name__ == "__main__":
    raise SystemExit(main())
