# Testing Verdikt through the email inbox

End-to-end test of the real intake path: send an email with an enrollment
packet attached, watch the pipeline read it, decide against the program's
rules, render a reply from a frozen template, and park it for a human.

This is the complete product path. Nothing here is a harness or a mock.

For the reviewer's screen-by-screen path once the event lands, see
[`pipeline-reviewer-walkthrough.md`](pipeline-reviewer-walkthrough.md). This
document is about getting an event to land in the first place.

## Addresses

| Role | Address |
| --- | --- |
| **Send from** | any address you control — Gmail, Outlook, a work account |
| **Send to** | `quire.template.agent@gmail.com` |

Any sender works. The subject tag `alderwood` routes the message to the
Alderwood tenant before the sender is ever looked at, so the From: address
does not matter. The demo mailbox is a single system inbox; which tenant a
message belongs to is decided per-message, not per-mailbox.

## How a message is routed

Two independent decisions, in this order.

**1. Which tenant?** (`src/docgen/inbox/resolver.py`)

- **Subject tag** wins first: any tenant whose `inbound_config.subject_tag` is
  a case-insensitive substring of the subject. Alderwood's is `alderwood`.
- **Sender domain** otherwise: the `From:` domain must be in
  `inbound_config.sender_domains`. Alderwood's list contains `gmail.com`, so
  any Gmail sender routes there. That is deliberate for the demo and is the
  kind of thing to tighten before real traffic — `From:` is spoofable, which is
  why domain routing additionally requires an `Authentication-Results` header
  showing DMARC/DKIM/SPF pass when one is present.

Put `alderwood` in the subject. It is what makes routing work from any
address: without it, only allow-listed sender domains route.

**2. Is it a PAP case, and which program?** (`src/docgen/inbox/agent.py`)

The subject must carry `[pap]` or `[pap:<program-slug>]`, case-insensitive. A
bare `[pap]` uses the tenant default; `[pap:cardiovita-pap-2026]` names the
program explicitly. **Without the tag the mail goes to the template-ingest
path instead** — a different feature entirely, and the usual reason a test
"does nothing".

## Sample documents

In [`samples/email-test/`](../../samples/email-test):

| File | What it is |
| --- | --- |
| `ASP-100-enrollment-maria-chen.pdf` | Enrollment & prescription referral, 2 pages |
| `ASP-110-income-worksheet-maria-chen.pdf` | Household income worksheet, 1 page |

Both are flat PDFs with no form fields, so they exercise the vision extraction
path rather than AcroForm reads — the same path a faxed or scanned packet takes.

The applicant is **Maria L. Chen**, San Jose CA, uninsured, household of **2**,
annual household income **$60,000**, diagnosed with chronic heart failure and
prescribed **Cardiovita** by Elena Vargas, MD (NPI 3167245083). Those five
facts are what the four CardioVita rules read — drug, prescriber, insurance
status, residency and income. Entirely fictional: `.example.com` addresses,
555 phone numbers, an invented drug and an invented pharmacy.

## Three tests worth running

CardioVita's income rule (`CV-INC-01`) caps household income at 400% of the
2026 FPL. For a household of 2 that is **$84,600**, and $60,000 clears it.

| # | Subject tag | Attach | Expected outcome | Why |
| --- | --- | --- | --- | --- |
| 1 | `[pap:cardiovita-pap-2026]` | both PDFs | **approve** | $60,000 ≤ $84,600, uninsured, US address, valid NPI |
| 2 | `[pap:cardiovita-pap-2026]` | ASP-100 only | **missing_info** | `CV-INC-01` returns missing_info rather than deny when PAP is requested but the ASP-110 worksheet is absent |
| 3 | `[pap:oncobridge-pap-2026]` | both PDFs | **deny** | OncoBridge caps at 250% FPL = $52,875; same packet, same code, opposite verdict |

Test 2 is the one worth doing first. It proves the system distinguishes "this
application fails" from "I cannot decide this yet" — which is the difference
between a wrong answer and an honest one.

Test 3 needs an `oncobridge-pap-2026` program **under the Alderwood tenant**, since
programs are looked up per tenant after the subject tag picks one. Its rules
ship in `src/pap_pipeline/rules/OB-*.yaml`, but no such program exists under
Alderwood today, so set it up first or skip this test. It is left out of the
judge-facing PDF for that reason.

## The email

```
From: <your-test-sender>@example.com
To: <demo-inbox>@example.com
Subject: [pap:cardiovita-pap-2026] Alderwood — enrollment for Maria Chen
```

Body — the content does not drive the decision, the attachments do, but send
something a person would actually write:

```
Hello,

Please find attached the completed patient assistance enrollment packet for
Maria L. Chen, referral ALD-26-207741.

Enclosed:
  - ASP-100 Patient Enrollment & Prescription Referral (2 pages)
  - ASP-110 Household Income Worksheet (1 page)

The patient is currently uninsured and is requesting assistance under the
CardioVita program. Please let me know if anything further is needed.

Thank you,
Bayview Cardiology Associates
```

Attach both PDFs. Send.

## What should happen

The poller checks every **60 seconds** and reads only **unread** mail, so allow
up to a minute. Watch it:

```bash
docker compose logs -f worker web | grep -i -E "inbox|pap_|verdict|stage_"
```

In order: the message is fetched and an `inbox_events` row is written; the
tenant resolves to Alderwood; the `[pap:...]` tag hands the event to the PAP
handler; attachments are persisted; ingest, triage, retrieve and reason run;
the decision is merged into the frozen template for its outcome; the reply is
rendered and **held**.

Then open the UI:

```
http://localhost:8100/ui/inbox
```

The newest event opens onto the pipeline detail page, one row per stage, each
with the model that handled it and the time it took, plus the rules that fired
with their citations and the rendered reply.

## Nothing sends on its own

The reply is rendered and parked in `pending_review`. Every outcome —
approve, deny, missing_info — stops there; only `routed` skips it. Nothing
reaches the applicant until a human opens the event and approves it. That gate
is in `src/pap_pipeline/inbox_hook.py` (`_OUTCOME_TO_REVIEW_STATE`), not in
configuration, so it cannot be switched off by accident.

Expect no email back until you click Approve.

## Re-testing

The poller marks a message `\Seen` only after it has processed it, so a crash
mid-run leaves the mail to be retried on the next pass. The flip side: **a
message that has already been processed will not be picked up again.** To
re-test, send a fresh email — don't forward or re-open the old one. Vary the
subject slightly so the two events are easy to tell apart.

## When nothing happens

| Symptom | Cause | Fix |
| --- | --- | --- |
| No `inbox_events` row at all | Poller off, or IMAP credentials rejected | `DOCGEN_INBOX_ENABLED=true` and an app password, not the account password |
| Event logged, `tenant_id` empty | Subject tag missing and sender domain not allow-listed | Put `alderwood` in the subject |
| Event routed but treated as a template | `[pap]` tag missing from the subject | Add `[pap:cardiovita-pap-2026]` |
| `pap handler not registered (DOCGEN_PLUGINS)` | Plugin not loaded at boot | `DOCGEN_PLUGINS=pap_pipeline.inbox_hook` on **both** web and worker, then restart |
| `pap: no attachments to process` | Attachments stripped, or over the size cap | `DOCGEN_INBOX_MAX_ATTACHMENT_BYTES` defaults to 20 MB |
| Decision made, no reply rendered | No package bound to that outcome for the program | Bind approve/deny/missing_info packages for the program |

Verify the running configuration:

```bash
docker compose exec web printenv | grep -E "DOCGEN_(PLUGINS|INBOX_ENABLED|INBOX_USERNAME)"
```
