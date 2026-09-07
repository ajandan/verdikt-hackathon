"""Unit tests for the heuristic PDF placement re-aligner.

Pure functions over placement dicts — no DB, no blob store, no FastAPI.
Kept in its own file so the tests can run even when docgen.main's import
chain is temporarily broken (the MCP integration has hit that before).
"""
from __future__ import annotations

from docgen.designtime.realign_pdf import (
    match_placements_by_label,
    realign_placements,
)


def _prior(name: str, page: int = 1, **kw: object) -> dict:
    base = {
        "name": name,
        "page": page,
        "x_pct": 0.2, "y_pct": 0.3, "w_pct": 0.4, "h_pct": 0.03,
        "font_size": 11.0, "anchor": "top-left",
    }
    base.update(kw)
    return base


def _new_widget(name: str, page: int = 1, **kw: object) -> dict:
    base = {
        "name": name,
        "type": "text",
        "field_type": "text",
        "page": page,
        "x_pct": 0.25, "y_pct": 0.32, "w_pct": 0.42, "h_pct": 0.03,
        "font_size": 12.0, "anchor": None,
    }
    base.update(kw)
    return base


def test_name_match_takes_new_coords_but_keeps_prior_font_and_label() -> None:
    """AcroForm re-upload with a matching widget name → the operator's
    label/font_size/type stay put; only the geometry updates. Since the new
    coords drifted from the prior (0.2→0.5 on x, well past _DRIFT_THRESHOLD),
    the placement gets flagged for review with the prior coords stashed."""
    prior = [_prior("member_name", font_size=14.0, label="Member's Full Name")]
    new = [_new_widget("member_name", x_pct=0.5, y_pct=0.6, w_pct=0.3)]
    aligned, report = realign_placements(
        prior_placements=prior, new_placements=new, new_page_count=1,
    )
    assert len(aligned) == 1
    p = aligned[0]
    assert p["x_pct"] == 0.5 and p["y_pct"] == 0.6 and p["w_pct"] == 0.3
    assert p["font_size"] == 14.0
    assert p["label"] == "Member's Full Name"
    assert p["realign_source"] == "widget_name_match"
    assert p["realign_confidence"] == 1.0
    assert p["realign_status"] == "needs_review"
    assert p["prior_coords"] == {"page": 1, "x_pct": 0.2, "y_pct": 0.3, "w_pct": 0.4, "h_pct": 0.03}
    assert report.matched_by_name == 1


def test_name_match_without_drift_stays_auto() -> None:
    """When the re-upload's widget lands within _DRIFT_THRESHOLD of the prior
    position, the match is treated as trivially confirmed — no ghost, no
    review flag, no prior_coords stashed to bloat the payload."""
    prior = [_prior("member_name")]
    new = [_new_widget("member_name",
                       x_pct=prior[0]["x_pct"], y_pct=prior[0]["y_pct"],
                       w_pct=prior[0]["w_pct"], h_pct=prior[0]["h_pct"])]
    aligned, _ = realign_placements(
        prior_placements=prior, new_placements=new, new_page_count=1,
    )
    assert aligned[0]["realign_status"] == "auto"
    assert "prior_coords" not in aligned[0]


def test_preserved_coords_stashes_prior_coords() -> None:
    """Preserved-coords placements stash prior_coords == current so the editor
    can display a Revert action (revert to itself is a no-op, but the button
    is there for API consistency). The ghost outline overlaps the pill so
    there's nothing visible — the amber outline is the operator signal."""
    prior = [_prior("claim_id")]
    aligned, _ = realign_placements(
        prior_placements=prior, new_placements=[], new_page_count=1,
    )
    p = aligned[0]
    assert "prior_coords" in p
    assert p["prior_coords"]["x_pct"] == p["x_pct"]
    assert p["prior_coords"]["y_pct"] == p["y_pct"]


def test_name_match_prefers_prior_type_over_widget_type() -> None:
    """The operator may have converted a text widget to a checkbox in the
    prior editor; the realigner must not revert that on re-upload."""
    prior = [_prior("consent", type="checkbox")]
    new = [_new_widget("consent", type="text")]  # source widget was text
    aligned, _ = realign_placements(
        prior_placements=prior, new_placements=new, new_page_count=1,
    )
    assert aligned[0]["type"] == "checkbox"


def test_no_match_preserves_coords_with_needs_review() -> None:
    """Prior placement with no name match on new (flat re-upload, or acroform
    with renamed widgets) keeps its coords but gets flagged for the operator."""
    prior = [_prior("claim_id")]
    aligned, report = realign_placements(
        prior_placements=prior, new_placements=[], new_page_count=1,
    )
    assert len(aligned) == 1
    p = aligned[0]
    assert p["x_pct"] == 0.2 and p["y_pct"] == 0.3
    assert p["realign_source"] == "preserved_coords"
    assert p["realign_confidence"] == 0.5
    assert p["realign_status"] == "needs_review"
    assert report.preserved_by_coords == 1


def test_new_widget_not_in_prior_is_added() -> None:
    """AcroForm re-upload with a widget that didn't exist before appears as
    a fresh placement — operator didn't have to add it manually."""
    prior = [_prior("member_name")]
    new = [
        _new_widget("member_name"),
        _new_widget("ssn", x_pct=0.5, y_pct=0.7, w_pct=0.3),
    ]
    aligned, report = realign_placements(
        prior_placements=prior, new_placements=new, new_page_count=1,
    )
    names = [p["name"] for p in aligned]
    assert names == ["member_name", "ssn"]
    ssn = aligned[1]
    assert ssn["realign_source"] == "new_widget"
    assert ssn["realign_status"] == "auto"
    assert report.added_from_new == 1


def test_prior_page_off_end_dropped() -> None:
    """When the re-uploaded PDF has fewer pages, placements from removed
    pages are dropped — there's nowhere for them to live."""
    prior = [_prior("p1", page=1), _prior("p3", page=3)]
    aligned, report = realign_placements(
        prior_placements=prior, new_placements=[], new_page_count=1,
    )
    assert [p["name"] for p in aligned] == ["p1"]
    assert report.dropped_off_page == 1


def test_case_sensitivity_no_match() -> None:
    """Name match is case-sensitive; a Member_Name widget doesn't match a
    prior member_name placement. Preserves-by-coords rather than double-adding.
    The editor slug pipeline is deterministic so this is a fair contract."""
    prior = [_prior("member_name")]
    new = [_new_widget("Member_Name")]
    aligned, _report = realign_placements(
        prior_placements=prior, new_placements=new, new_page_count=1,
    )
    assert len(aligned) == 2  # prior preserved + new appended
    assert aligned[0]["realign_source"] == "preserved_coords"
    assert aligned[1]["realign_source"] == "new_widget"


def test_empty_prior_returns_only_new_widgets() -> None:
    """First upload: no realign should happen (upload path guards on this),
    but if called directly the module must handle it cleanly."""
    new = [_new_widget("member_name"), _new_widget("ssn")]
    aligned, report = realign_placements(
        prior_placements=[], new_placements=new, new_page_count=1,
    )
    assert [p["name"] for p in aligned] == ["member_name", "ssn"]
    assert all(p["realign_source"] == "new_widget" for p in aligned)
    assert report.added_from_new == 2
    assert report.matched_by_name == 0


def test_prior_without_name_ignored() -> None:
    """Malformed prior with no name is skipped rather than crashing — the
    save path guards against nameless placements upstream, but the realign
    module doesn't want to be the choke point either."""
    prior = [{"page": 1, "x_pct": 0.2}]  # no name
    aligned, _report = realign_placements(
        prior_placements=prior, new_placements=[], new_page_count=1,
    )
    assert aligned == []


def test_name_match_keeps_prior_h_pct_when_new_widget_lacks_it() -> None:
    """AI-drawn or Ch-typed new widgets can arrive without h_pct — fall back
    to the prior's h_pct rather than dropping it (which would make the
    render pipeline recompute from font_size)."""
    prior = [_prior("field", h_pct=0.04)]
    new = [{"name": "field", "page": 1, "x_pct": 0.5, "y_pct": 0.5, "w_pct": 0.3}]
    aligned, _ = realign_placements(
        prior_placements=prior, new_placements=new, new_page_count=1,
    )
    assert aligned[0]["h_pct"] == 0.04


# --- Phase C: label-match reducer -------------------------------------------


def _placement(
    name: str,
    label: str,
    *,
    page: int = 1,
    status: str = "needs_review",
) -> dict:
    return {
        "name": name,
        "label": label,
        "page": page,
        "x_pct": 0.1, "y_pct": 0.1, "w_pct": 0.2, "h_pct": 0.03,
        "realign_status": status,
    }


def _detected(
    label: str,
    *,
    page: int = 1,
    x_pct: float = 0.5,
    y_pct: float = 0.5,
    w_pct: float = 0.25,
    h_pct: float = 0.04,
) -> dict:
    return {
        "page": page, "label": label,
        "x_pct": x_pct, "y_pct": y_pct, "w_pct": w_pct, "h_pct": h_pct,
    }


def test_label_match_exact_returns_new_coords() -> None:
    """Trivial case — same label text, same page. Placement takes the
    detected field's coords verbatim."""
    placements = [_placement("member_name", "Member Name")]
    detected = [_detected("Member Name", x_pct=0.4, y_pct=0.3)]
    matches = match_placements_by_label(
        placements=placements, detected_fields=detected,
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.index == 0
    assert m.x_pct == 0.4 and m.y_pct == 0.3
    assert m.similarity == 1.0
    assert m.matched_label == "Member Name"


def test_label_match_tolerates_punctuation_and_case() -> None:
    """Real-world drift: operator typed "Member's Full Name", the OCR
    label reads "member full name". Punctuation stripped, case-folded,
    token overlap comfortably above the threshold."""
    placements = [_placement("member", "Member's Full Name")]
    detected = [_detected("member full name")]
    matches = match_placements_by_label(
        placements=placements, detected_fields=detected,
    )
    assert len(matches) == 1
    assert matches[0].similarity >= 0.6


def test_label_match_below_threshold_skipped() -> None:
    """Barely-related labels (one-word overlap out of many tokens) fall
    below threshold — better to leave the placement flagged than to
    stomp its coords with a bad guess."""
    placements = [_placement("addr", "Home Mailing Address")]
    detected = [_detected("Employer Business Address Line One")]
    matches = match_placements_by_label(
        placements=placements, detected_fields=detected,
    )
    assert matches == []


def test_label_match_only_same_page() -> None:
    """A page-3 detected field must not match a page-1 placement — the
    operator's field lives on page 1, coords are page-relative."""
    placements = [_placement("sig", "Signature", page=1)]
    detected = [_detected("Signature", page=3)]
    matches = match_placements_by_label(
        placements=placements, detected_fields=detected,
    )
    assert matches == []


def test_label_match_skips_placements_without_label() -> None:
    """No label on the placement means nothing to compare against —
    silently skipped rather than raising, since malformed / legacy
    placements shouldn't fail the whole AI re-align flow."""
    placements = [
        {"name": "x", "page": 1, "realign_status": "needs_review"},
        _placement("y", "Signature"),
    ]
    detected = [_detected("Signature")]
    matches = match_placements_by_label(
        placements=placements, detected_fields=detected,
    )
    assert len(matches) == 1
    assert matches[0].index == 1


def test_label_match_only_needs_review_by_default() -> None:
    """Auto-status placements are already fine — the operator only asked
    us to re-match the flagged ones. Passing only_needs_review=False
    escapes that filter (used for the "match everything" mode)."""
    placements = [
        _placement("a", "Name", status="auto"),
        _placement("b", "Signature", status="needs_review"),
    ]
    detected = [_detected("Name"), _detected("Signature")]
    matches = match_placements_by_label(
        placements=placements, detected_fields=detected,
    )
    assert [m.index for m in matches] == [1]

    matches_all = match_placements_by_label(
        placements=placements, detected_fields=detected,
        only_needs_review=False,
    )
    assert sorted(m.index for m in matches_all) == [0, 1]


def test_label_match_greedy_one_to_one() -> None:
    """Two placements both compete for the same detected "Address" field —
    the one with the higher similarity wins, the loser goes unmatched
    (rather than both getting the same coords, which would double-assign
    a single physical field). Deterministic by (-sim, index) tiebreak."""
    placements = [
        _placement("addr1", "Mailing Address"),       # 2 tokens vs 3, sim < 1
        _placement("addr2", "Address"),                # 1 token vs 1, sim = 1
    ]
    detected = [_detected("Address")]
    matches = match_placements_by_label(
        placements=placements, detected_fields=detected,
    )
    assert len(matches) == 1
    assert matches[0].index == 1  # exact match wins


def test_label_match_returns_empty_when_no_labels() -> None:
    """Detected fields all lacked labels (label_fields_in_pdf returned
    empty for them) → no matches. Not an error state."""
    placements = [_placement("x", "Name")]
    detected = [{"page": 1, "x_pct": 0.5, "y_pct": 0.5, "w_pct": 0.2, "h_pct": 0.03}]
    matches = match_placements_by_label(
        placements=placements, detected_fields=detected,
    )
    assert matches == []
