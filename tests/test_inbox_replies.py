"""Reply-parser tests for the inbox agent.

Only exercises pure functions (`parse_decision`, `sender_matches`) — the
DB-backed lookup and the dispatcher hand-off are covered by manual demo
runs that need Postgres + Redis + IMAP.
"""
from __future__ import annotations

import uuid

from docgen.inbox.replies import parse_decision, sender_matches
from docgen.store.models import InboxEvent


def _event(from_addr: str, cc: list[str] | None = None) -> InboxEvent:
    return InboxEvent(
        id=uuid.uuid4(),
        message_id="<parent@x>",
        from_addr=from_addr,
        cc_addrs=cc or [],
        subject="",
        received_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        status="succeeded",
    )


def test_parse_decision_bare_approve():
    assert parse_decision("APPROVE") == "approve"
    assert parse_decision("approve") == "approve"
    assert parse_decision("  Approve  ") == "approve"


def test_parse_decision_bare_reject():
    assert parse_decision("REJECT") == "reject"
    assert parse_decision("reject.") == "reject"


def test_parse_decision_tolerates_punctuation():
    assert parse_decision("APPROVE!") == "approve"
    assert parse_decision("Reject...") == "reject"


def test_parse_decision_first_word_wins():
    # Real-world: users often write "APPROVE — ship it".
    assert parse_decision("APPROVE — publish it") == "approve"
    assert parse_decision("Reject, wrong template") == "reject"


def test_parse_decision_skips_quoted_reply_below():
    body = (
        "APPROVE\n"
        "\n"
        "On Tue, Mar 5, 2026 at 10:00 AM Bot <bot@x> wrote:\n"
        "> Template ingested…\n"
        "> Score 92.5\n"
    )
    assert parse_decision(body) == "approve"


def test_parse_decision_skips_preamble_line_only():
    # Some clients put the "On <date> wrote:" preamble ABOVE the quote and
    # BELOW the user's reply body starting with the user's blank line.
    body = "On Tue, Mar 5, 2026, Bot wrote:\n> body\n\nAPPROVE\n"
    # The first non-quoted, non-preamble line is "APPROVE" — the preamble
    # is skipped, the quoted "> body" is skipped, and the empty line
    # between them is skipped.
    assert parse_decision(body) == "approve"


def test_parse_decision_returns_none_for_ambiguous_reply():
    # Human comment — leave to the admin portal for review.
    assert parse_decision("Can you resend this? I lost the attachment.") is None


def test_parse_decision_empty_body():
    assert parse_decision("") is None
    assert parse_decision("\n\n\n") is None


def test_parse_decision_case_variants():
    assert parse_decision("Approved") is None  # only exact keyword counts
    assert parse_decision("Rejecting this one") is None


def test_sender_matches_exact_original():
    ev = _event("alice@customer.com")
    assert sender_matches("alice@customer.com", ev)
    assert sender_matches("ALICE@customer.com", ev)


def test_sender_matches_cc_address():
    ev = _event("alice@customer.com", cc=["ops@customer.com", "qa@customer.com"])
    assert sender_matches("ops@customer.com", ev)
    assert sender_matches("QA@customer.com", ev)


def test_sender_matches_rejects_stranger():
    ev = _event("alice@customer.com", cc=["ops@customer.com"])
    assert not sender_matches("stranger@customer.com", ev)
    assert not sender_matches("", ev)


def test_sender_matches_tolerates_junk_in_cc_list():
    # cc_addrs might contain non-strings if the DB row was hand-edited.
    ev = _event("alice@customer.com", cc=["ops@customer.com", 123, None])  # type: ignore
    assert sender_matches("ops@customer.com", ev)
    assert not sender_matches("nobody@x", ev)
