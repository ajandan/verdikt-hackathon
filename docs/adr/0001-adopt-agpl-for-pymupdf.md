# ADR-0001: Adopt AGPL-3.0 for the whole project (to keep pymupdf)

- **Status**: Accepted
- **Date**: 2026-09-04
- **Deciders**: repo owner
- **Related**: [#2](https://github.com/ajandan/docGen/issues/2)

## Context

The design-time PDF stack relies on **pymupdf** (`fitz`) for four things the
rest of our PDF toolkit does not cover:

1. Vector-drawing extraction (`page.get_drawings()`) — used by
   `src/docgen/designtime/pdf_field_detector.py` to detect rectangles,
   underlines, and check-glyphs that mark form fields on a flat PDF.
2. Text extraction with per-span bounding boxes (`page.get_text("rawdict")`) —
   used to avoid marking body-text underlines as fields.
3. Rasterization to PNG (`page.get_pixmap`) — used for the placement editor's
   page preview and for the detector's annotated overlay.
4. Overlay drawing directly onto an existing PDF page (`page.draw_rect`,
   `page.insert_text`) — used to produce the detector's annotated preview.

pymupdf is distributed under **AGPL-3.0**. Our CI license scanner
(`scripts/license_check.py`) previously assumed a closed-source-compatible
allowlist and fails on AGPL. This ADR resolves that block.

## Options considered

**Option 1 — Accept AGPL, relicense the whole project as AGPL-3.0**
- Pros: 30-minute change. Zero engineering risk to the detector. Keeps the
  same code paths working exactly as they do in prod today.
- Cons: The entire project inherits AGPL. Anyone running a modified copy as a
  network service must offer their source under AGPL. Rules out shipping a
  closed-source commercial product built on this codebase without first
  replacing pymupdf.

**Option 2 — Replace pymupdf with an Apache/MIT stack**
- Candidates: `pypdfium2` (Apache-2.0, wraps PDFium/BSD-3) for rasterization
  and text-with-bboxes; `pdfplumber` (MIT, wraps pdfminer.six/MIT) for vector
  drawings (`page.lines`, `page.rects`, `page.curves`); `reportlab` +
  `pypdf` (already deps) for overlay drawing via merge, mirroring the render
  pipeline pattern.
- Pros: Keeps the permissive-license posture. Preserves optionality for
  future commercial use.
- Cons: ~1–2 days of work with real regression risk on the detector
  (`pdf_field_detector.py` is ~600 lines of geometry heuristics tuned to
  pymupdf's coordinate conventions and drawing categorisation). Not worth
  spending now against sitting P0 gaps like automated backups.

**Option 3 — Split into an optional `docgen[pdf-vision]` extra**
- Pros: Keeps the main package license-clean; users opt into AGPL only if
  they install the extra.
- Cons: The detector is a first-class part of the flat-PDF workflow, not an
  optional add-on. Splitting it adds an import-gated code path that either
  works or doesn't depending on install flags — the CI matrix and error
  UX cost more than the licensing wins for a personal-scope project.

## Decision

Take **Option 1**. Adopt AGPL-3.0 for the whole project.

Concretely:

- Add the canonical AGPL-3.0 text as `LICENSE` at the repo root.
- Update `scripts/license_check.py` to reflect the new posture:
  the project is now AGPL, so upstream (A)GPL-family deps are compatible.
  Add `pymupdf` to `EXPLICIT_EXCEPTIONS` with a reference back to this ADR.
- Note the license in `README.md` and set `license` metadata in
  `pyproject.toml`.

## Consequences

- **Distribution constraint**: any third party who runs a modified copy of
  this codebase as a network service must offer their modified source
  under AGPL to the users of that service. Personal-scope hosting on
  `quire.aismith.ai` is fine — we make the source available via the public
  GitHub repository.
- **Future commercial flexibility**: closing this codebase for sale later
  requires first replacing `pymupdf` (Option 2 becomes a prerequisite).
  That's a known, bounded piece of work; not one we do speculatively.
- **New (A)GPL deps** must still be reviewed. AGPL propagates through this
  project's license because we've accepted it once — that does not
  auto-approve additional AGPL/GPL deps. Each one gets an ADR of its own or
  an entry in `EXPLICIT_EXCEPTIONS`.

## Follow-ups

- If we ever hit a real need to close-source or dual-license, revisit as an
  Option 2 execution ticket. The pymupdf replacement plan is sketched
  above and should still be viable.
