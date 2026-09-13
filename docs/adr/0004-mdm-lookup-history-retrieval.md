# ADR-0004: MDM lookup as a parallel retrieval source, slot-mediated for policy

- **Status**: Proposed
- **Date**: 2026-09-13
- **Deciders**: repo owner
- **Related**: ADR-0002, ADR-0003, `docs/HACKATHON.md`, `docs/roadmap.md`,
  landing comparison row *"MDM lookup for prior enrollments & case history"*

## Context

The pipeline today decides each case in isolation from the applicant's
prior history. `retrieve()` pulls rules from the versioned YAML pack;
`reason()` evaluates those rules against the extracted `Intake` and the
program's `extras`. Neither stage has access to the applicant's prior
enrollments, prior determinations, appeals, or terminations even when
those events materially change the correct outcome (a repeat denial in
under twelve months carries a different reply than a first-time
application, an open appeal blocks a new determination, a terminated
enrollment's reason code drives the missing-info list on re-application).

In the target market the canonical record of these prior events lives in
a master-data system (state MDM, Salesforce Health Cloud, program CRM,
custom rails). Today the analyst opens that system in a second tab,
finds the applicant, reads the history, and re-keys the relevant bits
into the case. Three concrete gaps follow:

- **Invisible to the ledger.** The verdict cites rules but not the
  historical events those rules implicitly depend on. Rollback and
  replay have no record of what history the decision was made against.
- **LLM has no lever.** The reason stage can't reference history in its
  prompts because history isn't in the payload. Prompts that try to
  reason from history-shaped hypotheticals ("if there was a prior
  denial…") produce hallucinated citations.
- **Policy leaks into inference.** Any attempt to close the gap by
  dumping raw history text into the LLM prompt makes the LLM the
  decider of *how* history matters — a policy call that belongs in the
  rules pack, not in the model.

The comparison row on the landing page markets this capability as
roadmap (◷). This ADR fixes the architecture so the roadmap claim is
honest: the seam ships in the hackathon build; wiring a real connector
is a config change, not a redesign.

## Decision

Add a second retrieval source alongside the rules pack, mediated by
named **slots** the rules pack declares, snapshotted with a content hash
so every ledger entry pins its history input the same way it already
pins its rules input.

**New stage: `stages/mdm_lookup.py`.** Runs after `extract()` and in
parallel with `retrieve()` via `asyncio.gather`. Returns a
`HistorySlice` — a bounded, tenant-scoped view of the applicant's prior
events in the same program family. Total wall-clock is `max(retrieve,
mdm_lookup)`, not their sum; both are network I/O against remote
services.

**New interface: `MdmSource` protocol.** Structural type (not ABC) with
one method:

```python
class MdmSource(Protocol):
    async def lookup(
        self,
        *,
        intake: Intake,
        program_family_id: str,
        tenant_id: str,
        config: MdmSourceConfig,
    ) -> HistorySlice: ...
```

Matches the codebase's async-function + dataclass style. Adapters are
just classes with the method; tests hand-roll fakes; no registration
step, no inheritance boilerplate.

**Null implementation ships.** `NullMdmSource.lookup()` returns a valid,
hashed `HistorySlice` with empty `events` and empty `slots` — but a real
SHA-256 over a canonicalized empty payload and a real timestamp. Every
ledger entry gains an `mdm_snapshot_hash` from day one. Every stage
downstream sees the same shape whether or not a real connector is
wired.

**New schemas: `schemas/history.py`.**

- `HistoryEvent` — one prior event: `event_id`, `event_type` (enrollment
  / determination / appeal / termination / renewal), `program_id`,
  `program_family_id`, `date`, optional `outcome` + `reason_code`,
  `source_system`, `record_hash` (sha256 of the full source record
  before the connector projected it down to allowed fields).
- `HistorySlot` — one named boolean / enum derived from events by the
  slot compiler: `name`, `value`, `from_events: list[str]` (the event
  ids that produced the value; the audit needs this to answer *why* a
  slot was true).
- `HistorySlice` — the wrapper: `member_id`, `program_family_id`,
  `events`, `slots`, `snapshot_hash`, `snapshot_at`, `source_provider`.

**Slots mediate policy — invariant, not a convention.** The reason
stage never sees raw `events`. Only `slots` are merged into
`extras["history_slots"]`. Rules in the pack reference slot names in
their `when` clauses (`prior_denial_same_program_within_12mo AND ...`);
the LLM evaluates a rule against a boolean the same way it already
evaluates `annual_household_income < 25000`. The reason prompt template
has no history-formatting branch — because there is no history to
format at that layer. Any attempt to plumb raw events into the reason
stage is a regression against this invariant and should fail review.

Consequence of the invariant: the rules author, not the model, decides
how history matters. Slot definitions live in the program pack next to
the rules; the slot compiler is a pure function of `(events,
slot_defs)`.

**Orchestrator wiring.** Two changes to `orchestrator.py`:

1. Replace the serial `retrieve(...)` with
   `asyncio.gather(retrieve(...), source.lookup(...))`. Both arms
   already return awaitable results with no cross-dependency —
   retrieval keys on the `Intake`, MDM lookup keys on `Intake` +
   `program_family_id` + `tenant_id`.
2. Add `_attach_history_citations(decision, slice)` after
   `_attach_retrieval_scores(...)`. Same stitching pattern: walk
   `decision.citations`, and for every rule citation whose rule
   referenced a slot, append a `type="history"` citation carrying the
   `event_id` (from `slot.from_events`), `slot_name`, and
   `snapshot_hash`. The reason stage stays ignorant of MDM; the *why*
   join happens after.

**`Decision.citations` extension.** Discriminated on a new `type` field:

```python
CitationType = Literal["rule", "history"]

class Citation(BaseModel):
    type: CitationType = "rule"
    rule_id: str | None = None
    matched: bool | None = None
    score: float | None = None
    # history-typed fields
    event_id: str | None = None
    slot_name: str | None = None
    snapshot_hash: str | None = None
```

Kept as one class rather than a `RuleCitation | HistoryCitation` union
so consumers that iterate `decision.citations` don't grow a type-switch
overnight. A `model_validator(mode='after')` enforces the per-type
field set. Upgrade to a discriminated union if a third citation type
lands.

**Config: `MdmSourceConfig` on `PipelineConfig`.** Splits
provider-agnostic scope (`program_families`, `max_window_days`,
`max_events`) from provider-specific transport (`base_url`, `api_key`,
`timeout`). The scope contract is enforceable across connectors — an
audit needs to answer *what any given connector was allowed to return*,
not just what it did return. Default is
`MdmSourceConfig(provider="null")`.

**Ledger contract.** Every case's ledger entry gains
`mdm_snapshot_hash` and `mdm_source_provider`. Combined with
`rules_pack_version_hash` and the intake hash, replay is deterministic
even when the underlying MDM has changed since the decision was made.
Rollback of a rules-pack version re-runs the reason stage against the
same frozen `HistorySlice` — no cross-contamination of inputs.

**PHI + tenancy invariants.**

- Connector auth and scope are tenant-configured — row-level tenancy
  (existing invariant, CI-audited) extends to MDM connector state.
- The connector projects the source record down to the allowed field
  set *before* returning; `record_hash` covers the full source record
  so an audit can re-verify against the source system without the
  slice carrying disallowed fields.
- The slice never leaves the reasoning boundary. Only citations
  (`event_id`, `snapshot_hash`, `slot_name`) persist to the ledger.
- Audit log records what was queried, what returned (hashed), which
  events the verdict cited.

## Options considered

**Option A — Raw history text into the reason prompt.**
- Pros: One-line change. LLM can reason about anything the connector
  returns.
- Cons: Makes the LLM the decider of *how* history matters. Prompts
  become policy documents; policy changes require prompt changes;
  policy reviewers can't read prompts. Kills the existing "policy lives
  in the rules pack" invariant. Non-deterministic verdict inputs
  (prompt tweaks + model updates + free-form context) break the
  content-addressed ledger story. Rejected: this is exactly the failure
  mode the pipeline exists to prevent.

**Option B — Rule loader queries MDM directly at retrieval time.**
- Pros: No new stage. Rules are the only new consumer of MDM.
- Cons: Couples the rules pack loader to a network I/O boundary — the
  rules pack today is a pure YAML load. Retrieval-time latency now
  includes an MDM round-trip per case regardless of whether any rule
  actually references history. Cross-tenant rule loading (the rules
  are tenant-shared; history is not) makes the caching story confusing.
  Rejected: the concern separation is wrong — rules and history are
  different corpora on different lifecycles.

**Option C — Chosen. Parallel MDM stage + slot-mediated slots + null
default.**
- Pros: Rules stay pure YAML. History has its own stage, its own
  config, its own hash on the ledger. Slots keep policy in the pack.
  Parallel gather adds latency only when MDM is meaningfully slower
  than retrieval. Null default lets the seam ship without a connector.
- Cons: New protocol, new schema module, new stage, new citation type,
  new config field, new ledger column. Cost is proportional to the
  capability — one moving part per invariant this ADR protects.

## Consequences

- **Roadmap row becomes architecturally true.** The comparison row
  *"MDM lookup for prior enrollments & case history"* can point at
  a real seam: every case flows through `mdm_lookup`, every ledger
  entry carries `mdm_snapshot_hash`, every rule citation can co-cite
  history events once a connector is wired. The ◷ marker stays until
  a real connector ships.
- **Reason stage contract narrows.** The reason prompt template stops
  short of history formatting. This is a load-bearing constraint —
  new PRs that thread raw events into the prompt should fail review
  citing this ADR.
- **`Decision.citations` grows a type discriminator.** Existing
  consumers (`assembler.py`, `verdict_json.py`, the letter renderer)
  need to filter by `type == "rule"` where they currently assume
  every citation is a rule. Trivial change, but the whole surface
  must migrate at once — no partial rollout.
- **Doc-package assembly picks up history references.** The merge step
  (ADR-0003) can render conditional blocks against history citations:
  *"Based on your prior determination on {{cite.date}} for
  {{cite.program}}, …"*. Merge-time fill only, no LLM in the reply
  path — the existing boundary test still holds.
- **Ledger row schema gains two columns.** `mdm_snapshot_hash` and
  `mdm_source_provider` on the pipeline event / case row. Migration is
  additive; existing rows backfill to `("sha256:…empty…", "null")` so
  every historical case is representable in the new shape.
- **CI adds one boundary test.** *"reason stage receives no raw
  `events` in `extras`."* Same shape as the existing "no LLM in the
  reply path" test — one assertion, one invariant, protects the whole
  slot-mediation contract.
- **Multi-tenancy invariant extends cleanly.** MDM connector auth and
  scope are tenant-scoped fields on the tenant config. The
  row-level-tenancy CI check treats them the same as every other
  tenant-owned column.

## Follow-ups

- **Slot compiler module.** `stages/slot_compile.py` — pure function of
  `(events, slot_defs) -> list[HistorySlot]`. Extracted from
  `mdm_lookup.py` so the connector code stays thin and the compiler is
  independently testable. Slot definitions live in the program pack
  next to rules; the pack's publish gate validates that every slot
  referenced by a rule is declared.
- **First real connector.** Target `state-mdm-v1` (generic REST + JSON
  Web Token auth) as the reference adapter. Two follow-on adapters
  (`salesforce-hc`, `custom-http`) exercise the seam under different
  auth and shape assumptions before the interface is frozen.
- **`program_family_id` resolution.** Deferred: either a new
  `program_families` table or a `family_id` column on `programs`.
  Called out here because the shape of `MdmSource.lookup(...)` bakes
  the decision in. First real connector will force the call.
- **Freshness policy for high-mutation programs.** Snapshot-at-case-open
  is the right default (reproducibility + ledger coherence). Some
  programs may want *"re-lookup if the case sits idle for > N days"* —
  add as an explicit re-snapshot event with its own hash, not a
  quiet in-place update.
- **Discriminated union for citations.** Revisit if a third citation
  type lands (e.g., `validation` citations from the connected
  validation APIs row on the comparison table). Two-shape union in one
  class is defensible; three-shape isn't.
- **Landing copy update.** Row cell text is currently generic. Once
  the slot mechanism is real code, update the copy to preview the
  citation shape — *"◷ Roadmap: rule cites the specific prior
  event that changed the answer"* — so the marketing claim matches
  the architecture.
