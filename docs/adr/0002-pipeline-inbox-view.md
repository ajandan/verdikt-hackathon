# ADR-0002: Dedicated pipeline inbox view + JSONB persistence for pipeline artifacts

- **Status**: Accepted
- **Date**: 2026-09-10
- **Deciders**: repo owner
- **Related**: ADR-0001, `docs/HACKATHON.md`

## Context

The inbox agent (`docgen.inbox.agent`) is shared across two flows:

1. **Template flow.** The message *is* the artifact — an inbound `.docx` /
   PDF becomes a draft `TemplateVersion` for a reviewer to approve. The
   review page exposes score, review state, message body, and the derived
   template.
2. **Pipeline flow.** The message *triggers* an artifact — the enrollment
   packet is read, reasoned over, and answered with a rendered letter.
   The reviewer's question is *"was the decision correct?"*, not *"is
   this template well-formed?"*

Until this ADR, both flows wrote to `inbox_events` and shared
`/ui/inbox/{id}`. The pipeline handler condensed the decision into two
event-level flags — `score` (highest citation score × 100) and
`review_state` (approve → `auto_approved`, deny → `rejected`,
missing_info → `pending_review`) — and discarded everything else. The
reviewer could see *what* the outcome was but not *why*.

Three concrete gaps followed:

- **No extract evidence.** The Intake object was regenerated per request
  by any code that needed to look at extracted fields, or lost entirely.
- **No citation trail.** The rules the reason stage evaluated, the match
  verdicts, and the reranker scores existed only in worker logs.
- **No artifact continuity.** The exact PDF sent to the applicant was
  discarded after `send_reply` returned. Re-rendering later would not
  reproduce it, because templates can be re-frozen between the send and
  the audit.

## Options considered

**Option 1 — Expand the shared `/ui/inbox/{id}` page conditionally.**
Add pipeline sections that render only when `event.pap_decision` is set;
keep everything on one page.

- Pros: One URL, one code path, familiar to reviewers already using the
  inbox for template flow.
- Cons: The template-flow page has an approve/reject action row keyed to
  a `TemplateVersion` that doesn't exist for pipeline events. It has an
  AI-planned test-trial table that has no analog in the pipeline flow.
  It has a mapping-wizard link that would 404 on pipeline events. The
  template already contains ~10 `{% if event.template_version_id %}`
  guards; adding a symmetric layer of pipeline guards ("only if
  `event.pap_decision`") doubles the branching. The pages diverge more
  than they overlap.

**Option 2 — Dedicated `/ui/inbox/{id}/pipeline` view with JSONB
persistence on the same `inbox_events` row.**
Keep one row per event (the poller's idempotency invariant depends on
this), add nullable `pap_intake` / `pap_decision` / `reply_attachment_blob_key`
columns, and route pipeline events through a purpose-built template. Add a
link from the shared page to the pipeline page when artifacts are present,
so reviewers who land on the wrong URL get there in one click.

- Pros: Neither page carries `{% if flow == other_flow %}` guards. The
  pipeline template can lay out extract → retrieve → reason as three
  stage-numbered cards without competing with template-flow layout. New
  reviewers see a page whose structure mirrors the pipeline itself.
- Cons: Two URLs for what is arguably one concept. Reviewers who
  bookmark the shared page miss the deeper view unless they follow the
  link.

**Option 3 — Separate `pap_inbox_events` table with 1:1 FK to
`inbox_events`.**
Store pipeline artifacts in a dedicated table, keep `inbox_events`
minimal.

- Pros: Purest separation. Migration doesn't touch existing rows.
- Cons: Every read of a pipeline event becomes a join. The
  `inbox_events` row already carries `score` and `review_state` derived
  from the decision — those are handler-owned columns, no different in
  spirit from `pap_intake`. Splitting would create two sources of truth
  for the same handler's output. Adds a table without adding evidence.

## Decision

Take **Option 2**.

Concretely:

- Alembic `0018` adds `inbox_events.pap_intake` and `pap_decision` as
  nullable JSONB. Template-flow events leave both NULL; pipeline events
  populate both after `_run_pipeline` succeeds.
- Alembic `0019` adds `inbox_events.reply_attachment_blob_key` (nullable
  String). The handler uploads the rendered PDF to
  `s3_bucket_documents` at `inbox-replies/{event.id}/{filename}` and
  stores the key.
- New route `GET /ui/inbox/{event_id}/pipeline` in
  `docgen/ui/routes.py` renders `templates/inbox_pipeline_detail.html`.
  404 when `pap_decision` is NULL. Rules text hydrated live from
  `pap_pipeline/rules/*.yaml` via `load_rules()` — no rule-text
  duplication into the event row.
- New route `GET /ui/inbox/{event_id}/reply-attachment` streams the
  stored PDF from S3, gated by the same admin-session middleware as the
  rest of `/ui/*`. `Cache-Control: private, no-store` so it isn't
  shared across sessions.
- Shared `inbox_detail.html` shows a "PAP pipeline verdict → Open
  pipeline detail" strip only when `event.pap_decision` is truthy, so
  the template flow's page stays byte-identical for template events.

## Consequences

- The reviewer answering "was this decision correct?" has all the
  evidence on one page: extracted intake, citation table with match
  verdict and rerank score, missing-fields list, rationale, and the
  exact PDF that left the building.
- `inbox_events` grows two JSONB columns and one String column, all
  nullable — no backfill required. Template-flow events remain
  unchanged on disk and in UI.
- The stored PDF is the audit artifact. Templates can be re-frozen
  later; the blob key stays valid because the bytes are frozen in blob
  storage, not regenerated on demand.
- Any future handler that produces its own artifact (e.g., prior-auth,
  benefit-verification) can follow the same shape: nullable JSONB
  columns for handler-specific evidence + a handler-specific detail
  route + a link from the shared page. Precedent set here rather than
  invented per handler.

## Follow-ups

- **Sortable citation table.** The current table is source-ordered; a
  reranker-score sort would help reviewers scan denial evidence
  quickly. Add when the demo cohort exceeds ~10 rules per case.
- **Historical intake diffs.** A follow-up letter might arrive and
  update Intake fields; today we overwrite. A `pap_intake_history`
  JSONB list would let reviewers see what changed between rounds.
  Deferred pending real multi-round cases.
- **Signed public download token.** Today the reply-attachment route
  requires an admin session. If we later want to embed the PDF link
  directly in the outbound email, adopt the same signed-token pattern
  `download_inbox_test_artifact` uses in `docgen/downloads.py`.
