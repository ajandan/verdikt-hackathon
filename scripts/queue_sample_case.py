"""Queue one scanned enrollment bundle for human review (smoke test / demo).

Runs the full pipeline on a PDF as if it had arrived by email: reads it,
decides, builds the reply package and parks the case in the reviewer queue.

Run in the web container, after scripts/seed_demo.py:
    python scripts/queue_sample_case.py [path/to/bundle.pdf]
With no argument it uses deploy/demo-seed/sample-bundle.pdf. The case appears
under /ui/inbox with review state "pending_review".
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from docgen.config import get_settings
from docgen.inbox.imap_poller import InboundAttachment, InboundMessage
from docgen.store.db import SyncSessionLocal
from docgen.store.models import InboxEvent, Tenant
from pap_pipeline.inbox_hook import _handler

TENANT_NAME = "Alderwood Specialty Pharmacy"
PROGRAM = "cardiovita-pap-2026"
DEFAULT_PDF = Path(__file__).resolve().parents[1] / "deploy" / "demo-seed" / "sample-bundle.pdf"


def main() -> None:
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    pdf = pdf_path.read_bytes()
    settings = get_settings()
    with SyncSessionLocal() as ses:
        tenant = ses.query(Tenant).filter(Tenant.name == TENANT_NAME).one()
        msg = InboundMessage(
            message_id=f"<sample-{uuid.uuid4()}@example.invalid>",
            from_addr="intake@alderwood.example.invalid",
            to_addrs=["intake@example.invalid"],
            cc_addrs=[],
            subject=f"[pap:{PROGRAM}] Enrollment bundle (scanned)",
            received_at=datetime.now(timezone.utc),
            body_text="Scanned enrollment bundle received for review.",
            in_reply_to=None,
            references=[],
            attachments=[InboundAttachment(filename=pdf_path.name, content=pdf, kind="pdf")],
        )
        ev = InboxEvent(
            message_id=msg.message_id, from_addr=msg.from_addr, to_addr=msg.to_addrs[0],
            cc_addrs=[], subject=msg.subject, received_at=msg.received_at, status="pending",
            body_text=msg.body_text, tenant_id=tenant.id,
        )
        ses.add(ev)
        ses.commit()
        ses.refresh(ev)
        result = _handler(ses, event=ev, msg=msg, tenant=tenant, program=PROGRAM, settings=settings)
        ses.refresh(ev)
        print(f"outcome: {result.detail}")
        print(f"event:   {ev.id}  status={ev.status}  review_state={ev.review_state}")
        print(f"open:    /ui/inbox/{ev.id}/pipeline")


if __name__ == "__main__":
    main()
