"""Heuristic re-alignment of prior placements onto a re-uploaded PDF.

When a PDF template gets a new version uploaded, the raw preflight would
either (a) replace all prior placements with the new widget rects
(acroform), or (b) leave the placements list empty (flat) — both of which
throw away the editor work invested in the previous version.

This module preserves that work by matching each prior placement to a
position on the new upload, and stamping a confidence + status on each so
the operator knows which need review. Two heuristics ordered by
reliability:

  1. Name match against new AcroForm widgets. If the new upload has a
     /Btn or /Tx widget whose name matches a prior placement's `name`,
     use the widget's rect verbatim. Confidence 1.0 — a name match is
     unambiguous.

  2. Preserve page-relative coordinates. Fall through for prior
     placements that don't hit case 1 (flat re-upload, or acroform with
     renamed widgets). Reuse the prior's x_pct/y_pct/w_pct/h_pct
     wholesale — if the layout is close, the position is still right;
     if it drifted, the operator will nudge in the editor. Confidence
     0.5, status "needs_review" so the editor flags it.

Anything the new upload has that we couldn't match to a prior placement
gets appended as a brand-new "auto" placement (typical for acroform
re-uploads that add a field). Prior placements whose page number falls
off the end of the new PDF are dropped rather than orphaned, since there
is nowhere for them to live.

Not covered here (out of scope for phase A, tracked as follow-ups):
  - Nearest-neighbor matching by anchor text (needs OCR or a text
    extraction step that we don't run at preflight today).
  - AI fallback for heavy layout drift (needs a vision-model call —
    piggyback on the existing /placements/ai-detect endpoint).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class RealignReport:
    """Summary of what happened during realign — surfaced in audit + UI."""
    matched_by_name: int = 0
    preserved_by_coords: int = 0
    added_from_new: int = 0
    dropped_off_page: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "matched_by_name": self.matched_by_name,
            "preserved_by_coords": self.preserved_by_coords,
            "added_from_new": self.added_from_new,
            "dropped_off_page": self.dropped_off_page,
        }


RealignStatus = Literal["auto", "needs_review"]

# When a name-matched widget's position drifts by more than this fraction on
# any axis, we flip status to needs_review so the operator eyeballs it. On a
# Letter page (612pt wide) 1% is ~6 points ≈ 8px @ 150 DPI preview — visibly
# noticeable but not nag-worthy for sub-pixel shifts.
_DRIFT_THRESHOLD = 0.01


def _stamp(
    placement: dict[str, Any],
    *,
    source: str,
    confidence: float,
    status: RealignStatus,
    prior_coords: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach realign metadata to a placement dict (non-destructive copy)."""
    out = dict(placement)
    out["realign_source"] = source
    out["realign_confidence"] = round(confidence, 3)
    out["realign_status"] = status
    if prior_coords is not None:
        out["prior_coords"] = prior_coords
    return out


def _prior_coords_snapshot(prior: dict[str, Any]) -> dict[str, Any]:
    """Freeze just the geometry fields — the editor's ghost outline reads
    these to draw a "here's where it was" indicator. Kept minimal so a
    round-trip through the save path doesn't bloat the JSON payload."""
    return {
        "page": int(prior.get("page") or 1),
        "x_pct": float(prior.get("x_pct") or 0),
        "y_pct": float(prior.get("y_pct") or 0),
        "w_pct": float(prior.get("w_pct") or 0),
        "h_pct": float(prior.get("h_pct") or 0),
    }


def _drifted(prior: dict[str, Any], new: dict[str, Any]) -> bool:
    """True when a name-matched widget moved enough to be worth flagging."""
    if int(prior.get("page") or 1) != int(new.get("page") or 1):
        return True
    for k in ("x_pct", "y_pct", "w_pct"):
        if abs(float(prior.get(k) or 0) - float(new.get(k) or 0)) > _DRIFT_THRESHOLD:
            return True
    return False


def realign_placements(
    *,
    prior_placements: list[dict[str, Any]],
    new_placements: list[dict[str, Any]],
    new_page_count: int,
) -> tuple[list[dict[str, Any]], RealignReport]:
    """Merge prior placements with new preflight output.

    `new_placements` is the raw output of `preflight_pdf(new_pdf).initial_placements`
    — non-empty for acroform (widget-harvested), empty for flat.

    Returns (aligned_placements, report). The aligned list is what should
    be persisted as the new version's `pdf_field_placements.placements`.
    """
    report = RealignReport()

    # Build a name -> new placement index for O(1) match lookup. Names on
    # the new side are the widget's /T (or /TU-slugged); on the prior side
    # they're whatever the operator picked in the editor. Case-sensitive
    # match — the editor slug pipeline is deterministic so a prior
    # "member_name" only matches a new "member_name", not "Member_Name".
    new_by_name: dict[str, dict[str, Any]] = {
        str(p["name"]): p for p in new_placements if p.get("name")
    }
    consumed_new_names: set[str] = set()

    aligned: list[dict[str, Any]] = []

    for prior in prior_placements:
        pname = str(prior.get("name") or "")
        if not pname:
            continue

        prior_page = int(prior.get("page") or 1)
        if prior_page > new_page_count:
            report.dropped_off_page += 1
            continue

        match = new_by_name.get(pname)
        if match is not None:
            # Case 1: exact name match on a harvested widget. Take the
            # new rect wholesale — the operator's payload key stays put,
            # only the geometry updates. When the new position drifted
            # more than _DRIFT_THRESHOLD from prior, flip status to
            # needs_review so the editor draws a ghost of the old
            # location and prompts the operator to accept or revert.
            drifted = _drifted(prior, match)
            aligned.append(_stamp(
                {
                    **prior,  # keep label, font_size, type, anchor, locked, etc.
                    "page": match["page"],
                    "x_pct": match["x_pct"],
                    "y_pct": match["y_pct"],
                    "w_pct": match["w_pct"],
                    "h_pct": match.get("h_pct") or prior.get("h_pct"),
                    # Prefer the prior's `type` if set — the operator may
                    # have converted a text field to checkbox/radio in the
                    # editor and we don't want the acroform harvest to
                    # revert that. Only take the new type when the prior
                    # didn't have one (legacy placements).
                    "type": prior.get("type") or match.get("type"),
                    "field_type": prior.get("field_type") or match.get("field_type"),
                },
                source="widget_name_match",
                confidence=1.0,
                status="needs_review" if drifted else "auto",
                prior_coords=_prior_coords_snapshot(prior) if drifted else None,
            ))
            consumed_new_names.add(pname)
            report.matched_by_name += 1
            continue

        # Case 2: no name match. Keep the prior's coordinates. Same page,
        # same fractions — if the new PDF's layout is close, this still
        # lands in the right spot; if not, the editor's "needs review"
        # flag prompts the operator to check. prior_coords == current
        # here (that's the whole point of "preserved"), so the ghost
        # overlaps the pill and is effectively invisible — the amber
        # outline alone signals "verify this is still on the right label."
        aligned.append(_stamp(
            prior,
            source="preserved_coords",
            confidence=0.5,
            status="needs_review",
            prior_coords=_prior_coords_snapshot(prior),
        ))
        report.preserved_by_coords += 1

    # Case 3: brand-new widgets on the new upload that didn't match any
    # prior placement. Common when the operator adds a field to the source
    # PDF between versions.
    for np_ in new_placements:
        name = str(np_.get("name") or "")
        if not name or name in consumed_new_names:
            continue
        aligned.append(_stamp(
            np_,
            source="new_widget",
            confidence=1.0,
            status="auto",
        ))
        report.added_from_new += 1

    return aligned, report


# --- Phase C: label-based match against freshly detected fields --------------
#
# When name-match yields few hits (e.g. flat re-upload where widget names
# don't survive), the operator can trigger the AI re-detect flow: server
# runs detect_fields_in_pdf + label_fields_in_pdf on the current version's
# PDF, then this reducer pairs each needs-review placement to a detected
# field by comparing their labels. Kept in its own function so it can be
# unit-tested without any vision / OCR calls.

_LABEL_TOKEN_RE = re.compile(r"[a-z0-9]+")
_LABEL_MATCH_THRESHOLD = 0.6


def _normalize_label(text: str | None) -> frozenset[str]:
    """Normalize a label to a word-token set for Jaccard comparison.

    Case-fold, drop punctuation, split on non-alphanumeric. "Member's Full
    Name" and "member full name (required)" both collapse to
    {"member","s","full","name"} vs {"member","full","name","required"} —
    Jaccard = 3/6 = 0.5, which sits just under the default threshold.
    Anything shorter or with more overlap comfortably passes."""
    if not text:
        return frozenset()
    return frozenset(_LABEL_TOKEN_RE.findall(text.lower()))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


@dataclass
class LabelMatch:
    """One placement paired to a freshly-detected field by label similarity."""
    index: int  # index into the input placements list
    page: int
    x_pct: float
    y_pct: float
    w_pct: float
    h_pct: float
    matched_label: str
    similarity: float


def match_placements_by_label(
    *,
    placements: list[dict[str, Any]],
    detected_fields: list[dict[str, Any]],
    only_needs_review: bool = True,
    threshold: float = _LABEL_MATCH_THRESHOLD,
) -> list[LabelMatch]:
    """Greedy one-to-one match of placements → detected fields by label.

    Each detected_fields entry must have `page`, `x_pct`, `y_pct`, `w_pct`,
    `h_pct`, and `label` (the string label_fields_in_pdf produced).
    Placements must have `label`, `page`, and (when only_needs_review) a
    `realign_status`. Matches are computed page-by-page: we compute all
    (placement, detected) pairs above threshold, sort by descending
    similarity, and consume each side at most once — so a detected field
    can't be double-assigned to two placements just because they share
    tokens with it. Placements missing a label are silently skipped —
    nothing to match on.

    Returns the matches; caller applies them to the placements list."""
    candidates: dict[int, list[tuple[int, int, float]]] = {}
    # (page -> list of (placement_index, detected_index, similarity))

    placement_tokens: dict[int, frozenset[str]] = {}
    for i, p in enumerate(placements):
        if only_needs_review and p.get("realign_status") != "needs_review":
            continue
        label = p.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        placement_tokens[i] = _normalize_label(label)

    detected_by_page: dict[int, list[tuple[int, dict[str, Any], frozenset[str]]]] = {}
    for j, d in enumerate(detected_fields):
        label = d.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        page = int(d.get("page") or 1)
        detected_by_page.setdefault(page, []).append((j, d, _normalize_label(label)))

    for i, p_tokens in placement_tokens.items():
        page = int(placements[i].get("page") or 1)
        for j, _d, d_tokens in detected_by_page.get(page, []):
            sim = _jaccard(p_tokens, d_tokens)
            if sim >= threshold:
                candidates.setdefault(page, []).append((i, j, sim))

    matches: list[LabelMatch] = []
    for _page, page_cands in candidates.items():
        # Greedy: highest similarity first, each side consumed once. Ties
        # broken by placement index for determinism.
        page_cands.sort(key=lambda t: (-t[2], t[0], t[1]))
        used_p: set[int] = set()
        used_d: set[int] = set()
        for i, j, sim in page_cands:
            if i in used_p or j in used_d:
                continue
            used_p.add(i)
            used_d.add(j)
            d = detected_fields[j]
            matches.append(LabelMatch(
                index=i,
                page=int(d.get("page") or 1),
                x_pct=float(d.get("x_pct") or 0),
                y_pct=float(d.get("y_pct") or 0),
                w_pct=float(d.get("w_pct") or 0),
                h_pct=float(d.get("h_pct") or 0),
                matched_label=str(d.get("label") or ""),
                similarity=round(sim, 3),
            ))

    matches.sort(key=lambda m: m.index)
    return matches
