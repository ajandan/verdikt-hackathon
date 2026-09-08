"""Parse APPROVE / REJECT decisions from reply-to-notification emails.

The inbox agent notifies senders about ingested templates; senders reply
"APPROVE" or "REJECT" to close the loop without opening the admin portal.
This module pulls the decision out of a reply body and finds the parent
event via the In-Reply-To header.

Design notes:
  - The parser walks lines top-down and stops at the first non-quoted,
    non-preamble line. Reply clients typically put the user's response
    at the top and quote the original below.
  - A stray "hey, can you double-check?" reply returns None so the agent
    leaves the event in `pending_review` for a human to handle.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from docgen.store.models import InboxEvent

# Legal decision keywords, matched case-insensitively.
_DECISION_APPROVE = "approve"
_DECISION_REJECT = "reject"

# Trailing punctuation we tolerate on decision words ("APPROVE!", "reject.").
_PUNCTUATION = ".!?,;:"
# Markdown/rich-text formatting characters clients wrap around words when
# users bold or italicize their reply — Gmail's compose-with-format emits
# "*APPROVE*" when the user bolds it, which otherwise breaks the match.
_FORMATTING_CHARS = "*_~`\"'"


def _is_quoted(line: str) -> bool:
    return line.startswith(">") or line.startswith("|")


def _is_preamble(line: str) -> bool:
    """Detect the "On <date>, <sender> wrote:" line most clients prepend."""
    lower = line.lower()
    return lower.startswith("on ") and lower.rstrip().endswith("wrote:")


def _normalize_word(word: str) -> str:
    return word.strip().strip(_FORMATTING_CHARS).rstrip(_PUNCTUATION).strip(_FORMATTING_CHARS).lower()


def parse_decision(body: str) -> str | None:
    """Return "approve" | "reject" | None from a reply body.

    Considers only the top-most non-quoted, non-preamble line. Matches
    both bare ("APPROVE") and leading-word ("APPROVE — publish it") forms.
    Returns None when the first meaningful line isn't a decision, so
    ambiguous replies fall through to human review.
    """
    if not body:
        return None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _is_quoted(line) or _is_preamble(line):
            continue

        bare = _normalize_word(line)
        if bare == _DECISION_APPROVE:
            return _DECISION_APPROVE
        if bare == _DECISION_REJECT:
            return _DECISION_REJECT

        first_word = _normalize_word(line.split(None, 1)[0])
        if first_word == _DECISION_APPROVE:
            return _DECISION_APPROVE
        if first_word == _DECISION_REJECT:
            return _DECISION_REJECT

        # First real line isn't a decision — treat as a human comment.
        return None
    return None


def find_parent_event(session: Session, in_reply_to: str | None) -> InboxEvent | None:
    """Look up the notification an inbound message is replying to.

    Matches on the Message-ID we set when sending the notification (stored
    in `reply_message_id`). Returns None when the reply doesn't correspond
    to any known notification — the dispatcher then treats the mail as a
    fresh ingest.
    """
    if not in_reply_to:
        return None
    return (
        session.query(InboxEvent)
        .filter(InboxEvent.reply_message_id == in_reply_to)
        .one_or_none()
    )


def sender_matches(reply_from: str, event: InboxEvent) -> bool:
    """Reject spoofed approvals: the reply's sender must be the original
    requester or someone who was CC'd on the notification thread.
    """
    if not reply_from:
        return False
    target = reply_from.strip().lower()
    if event.from_addr.strip().lower() == target:
        return True
    for cc in event.cc_addrs or []:
        if isinstance(cc, str) and cc.strip().lower() == target:
            return True
    return False
