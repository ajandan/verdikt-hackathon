# Pipeline reviewer walkthrough

The path a human coordinator takes from "email landed" to "decision defensible."
Written for a live judge walkthrough or a supplemental screen-capture cut —
not part of the 3-minute headline video, because the reviewer path lives one
click deeper than the cold-open beats.

## Preconditions

- Tenant "Alderwood Specialty Pharmacy" exists with `subject_tag=alderwood`
  in `inbound_config`.
- Three frozen letter templates uploaded under the Alderwood tenant, named
  so `ilike '%approv%'`, `'%deni%'`, `'%missing%'` resolve them:
  `alderwood-pap-approval-ASP-201`, `alderwood-pap-denial-ASP-202`,
  `alderwood-pap-missing-info-ASP-203`.
- `DOCGEN_PLUGINS=pap_pipeline.inbox_hook` set on web and worker; SMTP creds
  configured; the inbox agent is polling `intake@example.com`.
- Migrations `0018` and `0019` applied.

## The reviewer path — screen by screen

### 1. Send the enrollment packet

Applicant emails `intake@example.com`:

- **Subject:** `[pap:cardiovita-pap-2026] Alderwood — enrollment for Maria Chen`
  (subject-tag substring `alderwood` is case-insensitive after the resolver
  fix in `src/docgen/inbox/resolver.py`).
- **Attachment:** a filled ASP-100 enrollment PDF (plus optional ASP-110
  financial-hardship worksheet, insurance card, prescription).

The IMAP poller writes an `inbox_events` row, resolves the tenant, and hands
the event to `pap_pipeline.inbox_hook._handler` because the plugin registered
into the agent's PAP handler slot at boot.

### 2. Pipeline runs (~3–6 seconds against Nebius)

The handler pulls attachments, calls `orchestrator.run_with_intake`, and gets
back `(Intake, Decision)`. It then:

1. Persists `intake.model_dump()` and `decision.model_dump()` to
   `pap_intake` / `pap_decision` JSONB columns.
2. Resolves the tenant's frozen letter template by outcome hint
   (`_find_reply_template`).
3. Renders the letter to PDF via `execute_merge(tv, data, output_format="pdf")`.
4. Uploads the PDF bytes to `s3_bucket_documents` at key
   `inbox-replies/{event.id}/{filename}`, stores the key on the event.
5. Sends the reply with the same bytes attached.

Worker log signature:

```
inbox_reply_sent      attachments=1  subject='Re: [pap:...] approve'
pap_decision          outcome=approve  citations=4  missing=0
inbox_processed       detail=approve  status=succeeded
```

### 3. Reviewer opens the ingest event

Navigate to `/ui/inbox`, click the applicant's row.

The **template-flow detail page** is unchanged — same score, review state,
message header, body preview. Below the score card, a new strip appears
because `event.pap_decision` is populated:

> **PAP pipeline verdict:** `approve` — [Open pipeline detail →](/)

That link is the reviewer's entry point to the pipeline-specific view.

### 4. Pipeline detail page — `/ui/inbox/{event_id}/pipeline`

Sections top to bottom:

**Outcome hero.** The `approve` / `deny` / `missing info` badge and the one
sentence rationale the reason stage produced. Everything below is the
evidence trail behind that outcome.

**Stage 1 · Extract — structured intake.** All 11 Intake fields as a
key–value list. Fields the extract stage couldn't find are shown as `—`.
For a `missing_info` outcome, the blanks are directly the signal — the
reviewer sees why the pipeline asked for more information.

**Stage 2 · Retrieve → Reason — rule citations.** A table with one row per
cited rule. Columns:

- **Rule** — human-readable ID (`CV-INC-01`, `CV-CLIN-01`, ...).
- **Matched** — the reason stage's boolean verdict against this Intake:
  `yes` badge for pass, `no` badge for fail.
- **Score** — the retrieval reranker's relevance score, preserved through
  the orchestrator's `_attach_retrieval_scores` step so the reviewer can
  see the retrieval strength independent of the match verdict.
- **Statement** — the human-authored rule text loaded live from
  `src/pap_pipeline/rules/*.yaml`, plus a `financial` / `insurance` /
  `residency` / `clinical` kind chip.

**Stage 3 · Missing information.** List of intake field names the reason
stage flagged as needed to adjudicate. Empty on approve/deny outcomes.

**Reply.**

- **Outbound Message-ID** — matches what the applicant's mail client
  shows in the reply thread.
- **Attached PDF** — link to `/ui/inbox/{id}/reply-attachment`, which
  streams the byte-identical PDF from the object store. Same bytes that left the
  building, with `Cache-Control: private, no-store` so it doesn't get
  reused across sessions.

### 5. What the reviewer can defend

Opening the PDF from step 4 and reading Stage 2's citation table together
answers the compliance-officer question directly: *"Which rules led to this
outcome, and what document did the applicant receive?"* Both artifacts are
persisted, retrievable, and reproducible — no re-running the model to
recreate them.

## Failure modes surfaced

Each of these leaves the flow alive but shows something in the UI:

| Symptom | Where it shows | Root cause hint |
| --- | --- | --- |
| `Attached PDF: (no PDF recorded — render skipped or upload failed)` | Reply section | `pap_reply_template_missing` or `pap_reply_render_failed` or `pap_reply_blob_persist_failed` in worker logs — check tenant has all three frozen letter templates uploaded |
| `Statement: (rule text not found)` in citation row | Stage 2 table | Reason stage cited a rule ID that isn't in `pap_pipeline/rules/*.yaml` — usually a rule ID typo in the LLM output, or the rule was deleted |
| Outbound Message-ID `(not sent)` | Reply section | SMTP unavailable at handler time; the blob is still stored so the reviewer can download the letter and forward manually |
| `/ui/inbox/{id}/pipeline` returns 404 | Route | Event is a template-flow event (no `pap_decision`), or predates migration `0018` |

## Talking points if the judge asks

- **"Why persist Intake and Decision separately?"** — Intake is the extract
  stage's raw output; the reviewer needs it to explain missing-info
  outcomes independent of what the reason stage did with it. Decision is
  the reason stage's verdict; the reviewer needs it to defend approve/deny.
  Coupling them into one blob would obscure which stage produced the gap.
- **"Why store the reply PDF in blob storage instead of regenerating it?"**
  — Reproducibility is a byte-identity claim, not a re-render claim. If
  the template gets edited later (allowed via publish/freeze), a fresh
  render would produce a different PDF than the applicant received. The
  stored bytes are the audit artifact; the template is the audit input.
- **"Why a separate route instead of an accordion on the shared page?"** —
  See ADR-0002. Short version: the two flows produce different evidence,
  and pretending otherwise turns the shared page into an if-else pile.
