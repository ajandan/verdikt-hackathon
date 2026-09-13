"""Stage 2b — MDM history lookup (parallel to rule retrieval).

Runs after ``extract()``, in parallel with ``retrieve()``. Returns a
:class:`HistorySlice` snapshotted at case-open time. The reason stage sees
the slice's ``slots`` (merged into extras); rules reference slot names;
citations later record which events satisfied which slots.

The seam is real end-to-end today: the orchestrator calls this stage on
every case, the default :class:`NullMdmSource` returns an empty slice, and
the reason stage treats an empty ``slots`` list as "no history-derived
facts to consider". Wiring a real connector is a config-only change — no
schema, ledger, or orchestrator edits required.

Three adapters ship. :class:`NullMdmSource` is the production-safe default.
:class:`HttpMdmSource` is the reference shape for real connectors —
matches the ``state-mdm-v1`` REST + bearer-token contract in ADR-0004.
:class:`SimulatedMdmSource` is the demo adapter: deterministic-per-member
fake history so the pipeline shows the citation UX even without a real
MDM wired. :func:`build_mdm_source` picks between them from
``MdmSourceConfig.provider`` so the choice is a config-only knob.

See ADR-0004 for the parallel-gather + slot-mediation architecture and the
rejected alternatives (raw context to the LLM; rules-loader queries MDM).
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from pap_pipeline.schemas.history import (
    HistoryEvent,
    HistorySlice,
    HistorySlot,
    canonical_snapshot_hash,
)
from pap_pipeline.schemas.intake import Intake


@dataclass(frozen=True)
class MdmSourceConfig:
    """Tenant-scoped connector config.

    Split provider-agnostic scope (``program_families``, ``max_window_days``,
    ``max_events``) from provider-specific transport (``base_url``,
    ``api_key``, ``timeout``) so the scope contract is enforceable across
    connectors. An audit needs to answer *what any given connector was
    allowed to return*, not just what it did return.
    """

    provider: str = "null"
    program_families: tuple[str, ...] = field(default_factory=tuple)
    max_window_days: int = 1825  # 5y default
    max_events: int = 200
    base_url: str | None = None
    api_key: str | None = None
    timeout: float = 30.0


class MdmSource(Protocol):
    """Pluggable retrieval source for member history.

    Structural typing — any class with an ``async def lookup(...)`` matching
    this signature satisfies the protocol. Matches the async-function +
    dataclass style used elsewhere in the pipeline; tests hand-roll fakes
    without needing to inherit anything.
    """

    async def lookup(
        self,
        *,
        intake: Intake,
        program_family_id: str,
        tenant_id: str,
        config: MdmSourceConfig,
    ) -> HistorySlice: ...


class NullMdmSource:
    """No-op source. Returns an empty, hashed slice.

    Ships in the hackathon build. Keeps the seam real end-to-end: the
    orchestrator always produces a :class:`HistorySlice`, the reason stage
    always sees ``slots=[]``, and the ledger always carries a snapshot hash
    (the empty-slice hash) — so the ledger schema and downstream consumers
    don't change when a real connector is later wired.
    """

    async def lookup(
        self,
        *,
        intake: Intake,
        program_family_id: str,
        tenant_id: str,
        config: MdmSourceConfig,
    ) -> HistorySlice:
        member_id = _member_id(intake)
        digest = canonical_snapshot_hash(
            member_id=member_id,
            program_family_id=program_family_id,
            events=[],
            slots=[],
        )
        return HistorySlice(
            member_id=member_id,
            program_family_id=program_family_id,
            events=[],
            slots=[],
            snapshot_hash=digest,
            snapshot_at=datetime.now(timezone.utc),
            source_provider="null",
        )


def _member_id(intake: Intake) -> str:
    """Resolve the identifier the connector will key on.

    Extracted so real adapters can override the resolution rule — some
    programs key on SSN-last-4 + DOB, others on issued member #. For the
    null source we fall back to the form-supplied referral id, which is
    enough to make the snapshot hash a function of the applicant.
    """
    return intake.referral_id or ""


class HttpMdmSource:
    """Reference REST connector — the ``state-mdm-v1`` shape from ADR-0004.

    POSTs the query to ``{base_url}/history-slices`` with bearer auth, then
    validates the response into a :class:`HistorySlice`. Re-hashes the
    projected slice locally rather than trusting the server's hash — the
    ledger's replay guarantee depends on the hash being computed on our
    side of the boundary, over the field set we're persisting.

    ``snapshot_at`` is stamped locally for the same reason: the ledger's
    time-of-decision reflects when *this* pipeline saw the slice, not
    when the connector last updated its cache.
    """

    async def lookup(
        self,
        *,
        intake: Intake,
        program_family_id: str,
        tenant_id: str,
        config: MdmSourceConfig,
    ) -> HistorySlice:
        if not config.base_url:
            raise ValueError(
                "HttpMdmSource requires MdmSourceConfig.base_url — "
                "set config.mdm to a non-null provider config."
            )
        member_id = _member_id(intake)
        payload = {
            "member_id": member_id,
            "program_family_id": program_family_id,
            "tenant_id": tenant_id,
            "window_days": config.max_window_days,
            "max_events": config.max_events,
        }
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        url = f"{config.base_url.rstrip('/')}/history-slices"
        async with httpx.AsyncClient(timeout=config.timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        try:
            events = [
                HistoryEvent.model_validate(e) for e in (data.get("events") or [])
            ]
            slots = [
                HistorySlot.model_validate(s) for s in (data.get("slots") or [])
            ]
        except ValidationError as e:
            raise RuntimeError(
                f"MDM response did not match HistoryEvent/HistorySlot schema: {e}"
            ) from e

        digest = canonical_snapshot_hash(
            member_id=member_id,
            program_family_id=program_family_id,
            events=[e.model_dump(mode="json") for e in events],
            slots=[s.model_dump(mode="json") for s in slots],
        )
        return HistorySlice(
            member_id=member_id,
            program_family_id=program_family_id,
            events=events,
            slots=slots,
            snapshot_hash=digest,
            snapshot_at=datetime.now(timezone.utc),
            source_provider=config.provider or "http",
        )


# Scenario weights for :class:`SimulatedMdmSource`. Tuned so the demo has
# variety — most applicants novel, a meaningful minority carrying prior
# events the reply letter can reference. Sum must be 1.0.
_SIM_SCENARIO_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("novel", 0.40),
    ("prior_approve_active", 0.25),
    ("prior_deny_recent", 0.20),
    ("prior_approve_renewal_due", 0.15),
)


def _sim_rng(member_id: str, program_family_id: str) -> random.Random:
    """Stable per-member RNG.

    ``random.Random(str)`` seeding routes through ``hash()`` which is
    process-randomized under ``PYTHONHASHSEED=random`` (default on 3.3+).
    Derive an explicit 64-bit int from SHA-256 so the scenario draw is
    stable across runs, processes, and workers.
    """
    seed_bytes = hashlib.sha256(
        f"{member_id}|{program_family_id}".encode("utf-8")
    ).digest()[:8]
    return random.Random(int.from_bytes(seed_bytes, "big"))


class SimulatedMdmSource:
    """Deterministic-per-member fake MDM for demos and offline development.

    Not a mock or stub — it's a real :class:`MdmSource` implementation
    that returns plausible synthetic history. Every case gets the same
    scenario every time (seeded on ``member_id`` + ``program_family_id``)
    so the demo is reproducible: rerun the pipeline on the same intake,
    see the same prior enrollment cited.

    ``source_provider`` is stamped as ``"simulated"`` so verdict JSON,
    ledger entries, and logs are honest about the origin — nothing about
    this slice pretends to be a real MDM read.

    Scenarios are drawn from :data:`_SIM_SCENARIO_WEIGHTS`; each scenario
    emits both events and the derived slots a real slot compiler would
    produce, so downstream code sees the same shape it will see under a
    live connector.
    """

    async def lookup(
        self,
        *,
        intake: Intake,
        program_family_id: str,
        tenant_id: str,
        config: MdmSourceConfig,
    ) -> HistorySlice:
        member_id = _member_id(intake)
        # Empty member id → no way to key the slice; return the same empty
        # slice the null source would. Keeps the "no crash on partial
        # intakes" property honest.
        if not member_id:
            events: list[HistoryEvent] = []
            slots: list[HistorySlot] = []
        else:
            events, slots = _draw_scenario(
                rng=_sim_rng(member_id, program_family_id),
                member_id=member_id,
                program_family_id=program_family_id,
            )

        digest = canonical_snapshot_hash(
            member_id=member_id,
            program_family_id=program_family_id,
            events=[e.model_dump(mode="json") for e in events],
            slots=[s.model_dump(mode="json") for s in slots],
        )
        return HistorySlice(
            member_id=member_id,
            program_family_id=program_family_id,
            events=events,
            slots=slots,
            snapshot_hash=digest,
            snapshot_at=datetime.now(timezone.utc),
            source_provider="simulated",
        )


def _draw_scenario(
    *,
    rng: random.Random,
    member_id: str,
    program_family_id: str,
) -> tuple[list[HistoryEvent], list[HistorySlot]]:
    """Draw one scenario and materialize its events + slots.

    Dates are computed from the current UTC date so the demo always looks
    fresh; the seed governs *which* scenario and *how many days ago*
    within each scenario's band. Slot values are what a real slot
    compiler would emit — pre-computing them here lets the demo show the
    history-mediated verdict without waiting on the slot-compiler PR.
    """
    choices, weights = zip(*_SIM_SCENARIO_WEIGHTS)
    scenario = rng.choices(choices, weights=weights, k=1)[0]
    today = datetime.now(timezone.utc).date()

    if scenario == "novel":
        return [], []

    prefix = _short_member_prefix(member_id)

    if scenario == "prior_approve_active":
        days_ago = rng.randint(120, 240)
        approve_date = today - timedelta(days=days_ago)
        event = _sim_event(
            event_id=f"MDM-{prefix}-APP-{approve_date.year}",
            event_type="determination",
            program_family_id=program_family_id,
            date=approve_date,
            outcome="approve",
        )
        return [event], [
            HistorySlot(
                name="prior_enrollment_active",
                value=True,
                from_events=[event.event_id],
            ),
            HistorySlot(
                name="last_determination_outcome",
                value="approve",
                from_events=[event.event_id],
            ),
            HistorySlot(
                name="last_determination_days_ago",
                value=days_ago,
                from_events=[event.event_id],
            ),
        ]

    if scenario == "prior_deny_recent":
        days_ago = rng.randint(30, 150)
        deny_date = today - timedelta(days=days_ago)
        event = _sim_event(
            event_id=f"MDM-{prefix}-DEN-{deny_date.year}",
            event_type="determination",
            program_family_id=program_family_id,
            date=deny_date,
            outcome="deny",
            reason_code="INCOME-OVER-THRESHOLD",
        )
        return [event], [
            HistorySlot(
                name="prior_enrollment_active",
                value=False,
                from_events=[event.event_id],
            ),
            HistorySlot(
                name="last_determination_outcome",
                value="deny",
                from_events=[event.event_id],
            ),
            HistorySlot(
                name="last_determination_days_ago",
                value=days_ago,
                from_events=[event.event_id],
            ),
        ]

    # prior_approve_renewal_due
    days_ago = rng.randint(290, 360)
    approve_date = today - timedelta(days=days_ago)
    event = _sim_event(
        event_id=f"MDM-{prefix}-APP-{approve_date.year}",
        event_type="enrollment",
        program_family_id=program_family_id,
        date=approve_date,
        outcome="approve",
    )
    return [event], [
        HistorySlot(
            name="prior_enrollment_active",
            value=True,
            from_events=[event.event_id],
        ),
        HistorySlot(
            name="last_determination_outcome",
            value="approve",
            from_events=[event.event_id],
        ),
        HistorySlot(
            name="renewal_due_within_90d",
            value=True,
            from_events=[event.event_id],
        ),
    ]


def _sim_event(
    *,
    event_id: str,
    event_type: str,
    program_family_id: str,
    date,  # datetime.date
    outcome: str | None = None,
    reason_code: str | None = None,
) -> HistoryEvent:
    """Wrap the shared HistoryEvent construction so scenarios stay terse.

    ``record_hash`` covers the simulated record's identity fields so two
    runs of the same scenario produce the same hash — same replay
    guarantee a real connector needs to make, exercised end-to-end even
    in the demo.
    """
    record_payload = f"{event_id}|{event_type}|{date.isoformat()}|{outcome or ''}"
    record_hash = "sha256:" + hashlib.sha256(record_payload.encode("utf-8")).hexdigest()
    return HistoryEvent(
        event_id=event_id,
        event_type=event_type,  # type: ignore[arg-type]
        program_id=program_family_id,
        program_family_id=program_family_id,
        date=date,
        outcome=outcome,  # type: ignore[arg-type]
        reason_code=reason_code,
        source_system="simulated-mdm",
        record_hash=record_hash,
    )


def _short_member_prefix(member_id: str) -> str:
    """8-hex-char slug derived from the member id.

    Keeps synthetic event ids readable in demo UIs while staying stable
    per member. Not a security boundary — just a display detail.
    """
    return hashlib.sha256(member_id.encode("utf-8")).hexdigest()[:8].upper()


def build_mdm_source(config: MdmSourceConfig) -> "MdmSource":
    """Pick the adapter matching ``config.provider``.

    Called from the orchestrator when the caller didn't inject an
    explicit ``mdm_source``. Providers:

      - ``"null"`` — :class:`NullMdmSource` (empty slice; production-safe).
      - ``"simulated"`` — :class:`SimulatedMdmSource` (demo path).
      - anything else — :class:`HttpMdmSource` (real REST connector).
        Provider string flows through to ``source_provider`` on the slice
        so the ledger records *which* connector was used, not just that
        one was.

    Fail-open to the null source when ``provider`` is blank — the seam
    is meant to be always-on, and an empty provider is the same signal
    as "no connector wired yet".
    """
    provider = (config.provider or "null").strip().lower()
    if provider in ("", "null", "none", "off", "disabled"):
        return NullMdmSource()
    if provider in ("simulated", "sim", "fake", "demo"):
        return SimulatedMdmSource()
    return HttpMdmSource()


__all__ = [
    "HttpMdmSource",
    "MdmSource",
    "MdmSourceConfig",
    "NullMdmSource",
    "SimulatedMdmSource",
    "build_mdm_source",
]
