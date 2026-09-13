# ADR-0003: Outbound artifact = doc package, not single letter

- **Status**: Accepted
- **Date**: 2026-09-12
- **Deciders**: repo owner
- **Related**: ADR-0002, `docs/HACKATHON.md`, `docs/doc-generation-service-design.md`

## Context

Until this ADR, the pipeline emitted one outbound artifact per case: a
single reply letter, rendered from the program's bound
`TemplateVersion`, merged with the pipeline's structured verdict, and
attached to the outbound email. `Program.enrollment_template_version_id`
carried the binding — one template per program, regardless of outcome.

Real-world reply traffic is not one document. An approval reply carries
a cover letter *plus* a static enrollee guide, a benefits summary, and
sometimes a preauthorized fillable form; a denial reply carries a cover
letter *plus* a static appeals-rights notice and the applicable rule
excerpt; a `missing_info` reply carries a cover letter *plus* the
original blank enrollment form and a static field-help guide.

Three concrete gaps followed from the single-template model:

- **No place for static content.** Guides, appeals notices, and blank
  forms are byte-stable PDFs authored outside the template engine.
  Nowhere to store them; nowhere to bind them to a program.
- **No outcome fan-out.** The same program needs a different reply
  bundle depending on the verdict. One `template_version_id` per
  program can't express that.
- **No composition record.** Even when an operator manually attached a
  static PDF alongside the generated letter, the audit trail carried
  only the letter's `version_hash`. The static PDF and its version
  were not part of the case's provenance.

## Decision

Model the outbound artifact as an **ordered doc package** —
tenant-scoped, versioned by its items, selected per program by outcome.

**New entities.**

- `static_documents` — tenant-scoped store for stable PDFs
  (guides, notices, blank forms). Versioned. Not rendered — served
  as-is. Bytes hashed on upload; new upload for same name = new
  version.
- `doc_packages` — tenant-scoped, ordered list of items where each
  item is `{kind: template|static, ref_id}`. No version pin on items:
  the package auto-follows the latest version of each referenced
  template or static doc (see *Version pinning* below).
- `programs.outcomes[]` — declared outcome vocabulary per program
  (JSONB array of outcome slugs).
- `programs.package_bindings{}` — JSONB map `outcome → package_id`.
  Every outcome in `outcomes[]` must have a binding before the
  program can be marked publishable.

**Version pinning.** Package items reference `template.id` and
`static_document.id` — not a version. At assembly time the pipeline
picks the latest published version of each. Rationale: static content
in this domain (appeals notices, benefits guides) is expected to be
kept current; operators changing a static doc want the change to flow
through to live packages without touching every binding. Templates are
already governed by their own publish lifecycle — that gate is where
version discipline lives. If a pin is ever needed for regulated
content, add a nullable `pinned_version_id` on the item without
disturbing the auto-follow default.

**No fillable-PDF field fill.** Static PDFs are bytes-in, bytes-out.
Any dynamic content lives in the template item, which the Quire
template engine already handles through the frozen schema + AcroForm
or flat-PDF placement pipelines. This keeps the assembler contract
narrow: render templates, concatenate PDFs, done.

**Assembler stage.** New pipeline stage runs after the verdict is
signed:

1. Resolve `program.package_bindings[verdict.outcome]` →
   `doc_package_id`. Missing binding = pipeline error, not a silent
   fall-through to the old single-template path.
2. For each item in package order:
   - `kind=template`: render through the existing Quire render path
     with the verdict + intake as the payload → PDF bytes.
   - `kind=static`: fetch the latest version's blob → PDF bytes.
3. Concatenate via `pypdf.PdfWriter` in declared order.
4. Persist the merged PDF to
   `inbox-replies/{event.id}/{filename}.pdf` (reuses the ADR-0002
   audit-artifact slot).
5. Write a `doc_package_assemblies` provenance row stamping the
   package id, the resolved (template_version_id / static_document_version_id)
   tuple per item, the merged blob key, and the verdict's `case_id`.
6. Attach the merged PDF to the outbound email; expose
   `package_url` in the JSON verdict for CRM pickup.

**Validation invariants (enforced at publish, not at runtime).**

- Every rule's emitted outcome ∈ program's `outcomes[]`.
- Every entry in `outcomes[]` has a `package_bindings` entry.
- Every referenced `doc_package_id` is tenant-owned by the same tenant
  as the program.

## Options considered

**Option A — Keep single template per program, let operators concat
static PDFs by hand at reply time.**
- Pros: No schema change, no assembler stage.
- Cons: Loses the "config not code" principle — every case reverts to
  human intervention. Loses provenance for the static parts. Doesn't
  scale to a second program without repeating the same manual bundle.
  Rejected: this is exactly what the pipeline exists to eliminate.

**Option B — Per-outcome template variants on the program (three
templates: approved/denied/pended).**
- Pros: Minimal schema change (add outcome key next to
  `enrollment_template_version_id`).
- Cons: Doesn't compose. If two programs share the same appeals-rights
  notice, they each need it embedded in their denial template.
  Duplicates static content into template blobs, breaking the "static
  is static" audit story. Rejected: templates are the wrong container
  for static reference material.

**Option C — Chosen. Static docs + doc packages + outcome bindings.**
- Pros: Static and dynamic content each live where they belong;
  packages compose them; bindings fan out per outcome; existing render
  pipeline unchanged for the template items.
- Cons: Three new entities, one new pipeline stage, one new admin
  screen for each entity. Cost is real but proportional to the
  capability.

## Consequences

- **Outbound artifact contract changes.** The stored
  `reply_attachment_blob_key` is now the merged PDF, not a single
  render output. The signed JSON verdict gains a `package_url` field
  pointing at the same blob so downstream CRM systems can pick up the
  full bundle without going through email.
- **Program lifecycle gains a gate.** A program with declared outcomes
  but missing bindings can't be marked `publishable`. The admin screen
  surfaces the missing bindings explicitly rather than letting a
  program silently run with the wrong package.
- **`programs.enrollment_template_version_id` becomes optional.**
  Legacy programs seeded before this ADR keep the column populated;
  the assembler treats a program with a template binding but no
  package bindings as a single-item "approved-only" implicit package
  during migration. Once bindings are set, the legacy column is
  ignored. A follow-up migration will drop the column after every
  live program is migrated to bindings.
- **Provenance gets a new append-only table.** Every assembled package
  produces a `doc_package_assemblies` row — the audit answer to "what
  exact bytes did this case receive?" is now a single primary-key
  lookup, resolving the merged blob key and every item's resolved
  version.
- **Second-program story stays a data change.** OncoBridge (the cameo
  in the demo) adds its own `static_documents`, `doc_packages`, and
  `programs.package_bindings` — no new code. The rule that "onboarding
  a program is data, not build" survives the extension.

## Follow-ups

- **Version pin escape hatch.** Add `pinned_version_id` on package
  items when a regulated buyer asks for it. Deferred until a real
  request lands — the auto-follow default is easier to defend on
  first-run than "why does this document lag by three weeks."
- **Package preview.** Admin screen currently lists items in order;
  a "Preview merged PDF" action that runs the assembler against a
  sample intake would let operators sanity-check ordering and page
  breaks before binding. Ship once the assembler is stable.
- **Static-doc superseding.** Uploading a new version of a static doc
  currently doesn't touch older versions. If regulated content ever
  needs "supersede all prior versions" semantics (e.g., an appeals
  notice whose old text is now legally invalid), add a `superseded_by`
  pointer on `static_document_versions` and have the assembler
  refuse to serve superseded versions with an actionable error.
- **Cross-tenant package sharing.** If a customer runs multiple
  tenants under one org, they may want to share a canonical
  "master appeals notice" across all of them. Deferred — the
  tenant-scoped default is safer to start with.
