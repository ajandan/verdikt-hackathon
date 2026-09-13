"""Stage 2b (MDM history lookup) + the ADR-0004 seam it protects.

The null adapter that ships in the hackathon build must still produce a
valid, hashed :class:`HistorySlice` so the ledger's `mdm_snapshot_hash`
column is populated for every case, and so downstream consumers see the
same shape whether or not a real connector is wired.

The reason stage's boundary check (`_forbid_raw_history_events`) enforces
the slot-mediation invariant — history reaches reasoning as derived slots,
never as raw events. That invariant is load-bearing for the "policy lives
in the rules pack, not the LLM" claim, so it gets its own regression
test here.
"""
from __future__ import annotations

import asyncio

import pytest

from pap_pipeline.schemas import HistoryEvent, HistorySlice, HistorySlot, Intake
from pap_pipeline.schemas.history import canonical_snapshot_hash
from pap_pipeline.schemas.decision import Citation
from pap_pipeline.stages.mdm_lookup import (
    HttpMdmSource,
    MdmSourceConfig,
    NullMdmSource,
    SimulatedMdmSource,
    build_mdm_source,
)
from pap_pipeline.stages.reason import _forbid_raw_history_events


def test_null_mdm_source_returns_empty_hashed_slice() -> None:
    """The null adapter still produces a valid slice: empty events + slots,
    a real SHA-256 hash, a real timestamp, and the correct provider tag.
    Consumers can treat every case as history-carrying without branching
    on connector availability."""
    intake = Intake(referral_id="ALD-26-207802")
    slice_ = asyncio.run(
        NullMdmSource().lookup(
            intake=intake,
            program_family_id="cardiovita",
            tenant_id="tnt-x",
            config=MdmSourceConfig(provider="null"),
        )
    )
    assert slice_.member_id == "ALD-26-207802"
    assert slice_.program_family_id == "cardiovita"
    assert slice_.events == []
    assert slice_.slots == []
    assert slice_.source_provider == "null"
    assert slice_.snapshot_hash.startswith("sha256:")
    # 64 hex chars after the prefix.
    assert len(slice_.snapshot_hash) == len("sha256:") + 64
    assert slice_.snapshot_at.tzinfo is not None


def test_null_mdm_source_hash_is_stable_for_same_inputs() -> None:
    """Same (member_id, program_family_id, empty events, empty slots) →
    same hash. The ledger relies on this to prove that two runs of the
    same case saw the same history slice."""
    intake = Intake(referral_id="ALD-26-207802")
    cfg = MdmSourceConfig(provider="null")

    a = asyncio.run(NullMdmSource().lookup(
        intake=intake, program_family_id="cardiovita",
        tenant_id="tnt-x", config=cfg,
    ))
    b = asyncio.run(NullMdmSource().lookup(
        intake=intake, program_family_id="cardiovita",
        tenant_id="tnt-x", config=cfg,
    ))
    assert a.snapshot_hash == b.snapshot_hash


def test_null_mdm_source_hash_differs_for_different_members() -> None:
    """Snapshot hash must be a function of the applicant — a different
    referral id produces a different hash even for an empty slice."""
    cfg = MdmSourceConfig(provider="null")
    a = asyncio.run(NullMdmSource().lookup(
        intake=Intake(referral_id="ALD-26-000001"),
        program_family_id="cardiovita", tenant_id="tnt-x", config=cfg,
    ))
    b = asyncio.run(NullMdmSource().lookup(
        intake=Intake(referral_id="ALD-26-000002"),
        program_family_id="cardiovita", tenant_id="tnt-x", config=cfg,
    ))
    assert a.snapshot_hash != b.snapshot_hash


def test_canonical_snapshot_hash_is_key_order_independent() -> None:
    """The canonical hash sorts keys so two adapters projecting the same
    slice in different declaration orders produce byte-identical hashes."""
    ev_a = [{"event_id": "E1", "date": "2024-03-14", "program_id": "SNAP"}]
    ev_b = [{"program_id": "SNAP", "event_id": "E1", "date": "2024-03-14"}]
    ha = canonical_snapshot_hash(
        member_id="M1", program_family_id="F", events=ev_a, slots=[],
    )
    hb = canonical_snapshot_hash(
        member_id="M1", program_family_id="F", events=ev_b, slots=[],
    )
    assert ha == hb


def test_reason_stage_rejects_raw_history_events_in_extras() -> None:
    """ADR-0004 slot-mediation invariant. Raw member-history events must
    never reach the reasoning prompt — they cross the policy boundary."""
    with pytest.raises(ValueError, match="history_events"):
        _forbid_raw_history_events({"history_events": []})
    with pytest.raises(ValueError, match="history_events"):
        _forbid_raw_history_events({"history_events": [{"event_id": "X"}]})


def test_reason_stage_permits_derived_slots_in_extras() -> None:
    """The invariant is against raw events, not derived slots. Slot
    values under `history_slots` are the intended carrier."""
    # Should not raise.
    _forbid_raw_history_events(None)
    _forbid_raw_history_events({})
    _forbid_raw_history_events({"history_slots": [
        {"name": "prior_denial_in_12mo", "value": True, "from_events": ["E1"]}
    ]})
    _forbid_raw_history_events({"member_id": "MC-42"})


def test_history_citation_shape_is_enforced_by_validator() -> None:
    """The per-type field-set validator (ADR-0004) rejects malformed
    citations at the schema boundary rather than letting them leak into
    the ledger."""
    # Well-formed history citation.
    c = Citation(
        type="history",
        event_id="MDM-2024-A891",
        slot_name="prior_denial_in_12mo",
        snapshot_hash="sha256:deadbeef",
    )
    assert c.type == "history"

    # History citation missing required fields → error.
    with pytest.raises(ValueError, match="history citation requires"):
        Citation(type="history", event_id="E1")

    # History citation carrying rule fields → error.
    with pytest.raises(ValueError, match="history citation must not"):
        Citation(
            type="history",
            event_id="E1",
            slot_name="s",
            snapshot_hash="sha256:x",
            rule_id="R1",
        )

    # Rule citation carrying history fields → error.
    with pytest.raises(ValueError, match="rule citation must not"):
        Citation(rule_id="R1", matched=True, event_id="E1")


def test_history_slot_records_contributing_events() -> None:
    """Slots track the event ids that produced their value. The audit
    trail joins slot → events at that seam."""
    slot = HistorySlot(
        name="prior_denial_in_12mo",
        value=True,
        from_events=["MDM-2024-A891", "MDM-2024-B002"],
    )
    assert slot.value is True
    assert slot.from_events == ["MDM-2024-A891", "MDM-2024-B002"]


def test_orchestrator_passes_null_slice_through_gather(monkeypatch) -> None:
    """The orchestrator awaits (retrieve, mdm.lookup) in one gather and
    passes the resulting slice through unchanged. Even with the default
    NullMdmSource, run_from_intake() returns a Decision without regressions
    on the rule-citation-side wiring."""
    import json as _json

    import httpx
    from pap_pipeline.demo_data import load_canonical_intake
    from pap_pipeline.orchestrator import PipelineConfig, run_from_intake
    from pap_pipeline.stages import extract as extract_mod
    from pap_pipeline.stages import reason as reason_mod
    from pap_pipeline.stages import retrieve as retrieve_mod
    from pap_pipeline.stages.extract import ExtractClientConfig
    from pap_pipeline.stages.reason import ReasonClientConfig
    from pap_pipeline.stages.retrieve import RetrieveClientConfig

    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        body = _json.loads(request.content)
        if p.endswith("/embeddings"):
            n = len(body["input"])
            return httpx.Response(200, json={"data": [
                {"embedding": [1.0 if i == j else 0.0 for j in range(n)]}
                for i in range(n)
            ]})
        if p.endswith("/rerank"):
            return httpx.Response(200, json={"results": [
                {"index": i, "relevance_score": 1.0 - i * 0.1}
                for i in range(len(body["documents"]))
            ]})
        if p.endswith("/chat/completions"):
            content = _json.dumps({
                "outcome": "approve",
                "citations": [
                    {"rule_id": "CV-INC-01", "matched": True, "score": None},
                ],
                "missing_fields": [],
                "rationale": "ok",
            })
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return httpx.Response(404)

    real_async = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async(*args, **kwargs)

    monkeypatch.setattr(extract_mod.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(retrieve_mod.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(reason_mod.httpx, "AsyncClient", _factory)

    cfg = PipelineConfig(
        extract=ExtractClientConfig(base_url="https://x", api_key="y"),
        retrieve=RetrieveClientConfig(base_url="https://x", api_key="y"),
        reason=ReasonClientConfig(base_url="https://x", api_key="y", model="m"),
    )
    decision = asyncio.run(run_from_intake(
        load_canonical_intake(),
        config=cfg,
        program="cardiovita-pap-2026",
        k=1,
    ))
    assert decision.outcome == "approve"
    # Every citation the null seam produces is still a rule citation.
    assert all(c.type == "rule" for c in decision.citations)


def test_orchestrator_accepts_injected_mdm_source() -> None:
    """The `mdm_source` kwarg is a DI seam for tests and future adapters —
    passing one overrides the default NullMdmSource. Verified by having the
    injected source raise; the raise propagates through the gather.

    Retrieve is short-circuited via an empty explicit rules corpus so the
    only failing arm is the injected MDM source — no network mocking noise.
    """
    from pap_pipeline.demo_data import load_canonical_intake
    from pap_pipeline.orchestrator import PipelineConfig, run_from_intake
    from pap_pipeline.stages.extract import ExtractClientConfig
    from pap_pipeline.stages.mdm_lookup import MdmSourceConfig
    from pap_pipeline.stages.reason import ReasonClientConfig
    from pap_pipeline.stages.retrieve import RetrieveClientConfig

    class ExplodingMdmSource:
        async def lookup(self, **kwargs):
            raise RuntimeError("mdm boom")

    cfg = PipelineConfig(
        extract=ExtractClientConfig(base_url="https://x", api_key="y"),
        retrieve=RetrieveClientConfig(base_url="https://x", api_key="y"),
        reason=ReasonClientConfig(base_url="https://x", api_key="y", model="m"),
        mdm=MdmSourceConfig(provider="null"),
    )
    with pytest.raises(RuntimeError, match="mdm boom"):
        asyncio.run(run_from_intake(
            load_canonical_intake(),
            config=cfg,
            program="cardiovita-pap-2026",
            rules=[],  # empty corpus → retrieve returns [] with no network call
            mdm_source=ExplodingMdmSource(),
        ))


def test_simulated_mdm_source_is_deterministic_per_member() -> None:
    """Same member id → same slice. The demo depends on this: replay the
    pipeline on a fixture intake, get the same prior events cited every
    time. Determinism runs on the (member_id, program_family_id) seed."""
    cfg = MdmSourceConfig(provider="simulated")
    intake = Intake(referral_id="ALD-26-777001")

    a = asyncio.run(SimulatedMdmSource().lookup(
        intake=intake, program_family_id="cardiovita",
        tenant_id="tnt-x", config=cfg,
    ))
    b = asyncio.run(SimulatedMdmSource().lookup(
        intake=intake, program_family_id="cardiovita",
        tenant_id="tnt-x", config=cfg,
    ))
    assert [e.event_id for e in a.events] == [e.event_id for e in b.events]
    assert [(s.name, s.value) for s in a.slots] == [(s.name, s.value) for s in b.slots]
    # snapshot_hash is a function of events + slots + ids — should match too.
    assert a.snapshot_hash == b.snapshot_hash


def test_simulated_mdm_source_stamps_honest_provider_tag() -> None:
    """The slice must self-identify as synthetic. Verdict JSON, ledger,
    and logs read `source_provider` — labeling simulated data as anything
    other than "simulated" would poison audit trails."""
    intake = Intake(referral_id="ALD-26-555001")
    slice_ = asyncio.run(SimulatedMdmSource().lookup(
        intake=intake, program_family_id="cardiovita",
        tenant_id="tnt-x", config=MdmSourceConfig(provider="simulated"),
    ))
    assert slice_.source_provider == "simulated"


def test_simulated_mdm_source_produces_non_empty_for_some_members() -> None:
    """Across 30 seeded members, we should draw at least one non-empty
    scenario. The exact ratio is a probability, not a contract — this
    just guards against a regression that silently degrades every case
    to the `novel` branch."""
    non_empty = 0
    for i in range(30):
        intake = Intake(referral_id=f"ALD-26-{i:06d}")
        slice_ = asyncio.run(SimulatedMdmSource().lookup(
            intake=intake, program_family_id="cardiovita",
            tenant_id="tnt-x", config=MdmSourceConfig(provider="simulated"),
        ))
        if slice_.events:
            non_empty += 1
    assert non_empty >= 3, f"expected some non-empty slices, saw {non_empty}"


def test_simulated_mdm_source_slots_reference_only_existing_events() -> None:
    """`from_events` must name an event that exists in `events`. The
    audit-trail join breaks silently if a slot cites a phantom id."""
    for i in range(20):
        intake = Intake(referral_id=f"ALD-26-{i:06d}")
        slice_ = asyncio.run(SimulatedMdmSource().lookup(
            intake=intake, program_family_id="cardiovita",
            tenant_id="tnt-x", config=MdmSourceConfig(provider="simulated"),
        ))
        event_ids = {e.event_id for e in slice_.events}
        for slot in slice_.slots:
            for fe in slot.from_events:
                assert fe in event_ids, (
                    f"slot {slot.name} cites event {fe} not in events {event_ids}"
                )


def test_simulated_mdm_source_empty_member_returns_empty_slice() -> None:
    """When the intake has no referral id, we can't key a scenario, so we
    fall back to an empty slice with `source_provider="simulated"` still.
    The seam stays honest about what happened."""
    slice_ = asyncio.run(SimulatedMdmSource().lookup(
        intake=Intake(),  # no referral_id
        program_family_id="cardiovita", tenant_id="tnt-x",
        config=MdmSourceConfig(provider="simulated"),
    ))
    assert slice_.events == []
    assert slice_.slots == []
    assert slice_.source_provider == "simulated"


def test_http_mdm_source_posts_query_and_parses_response(monkeypatch) -> None:
    """Real-adapter shape: POST to `{base_url}/history-slices` with bearer
    auth, validate the response into HistoryEvent/HistorySlot, and re-hash
    locally. Confirms the wire contract used by the `state-mdm-v1` shape
    called out in ADR-0004 § Follow-ups."""
    import json as _json

    import httpx
    from pap_pipeline.stages import mdm_lookup as mdm_mod

    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json={
            "events": [
                {
                    "event_id": "MDM-2024-A891",
                    "event_type": "determination",
                    "program_id": "cardiovita",
                    "program_family_id": "cardiovita",
                    "date": "2024-06-14",
                    "outcome": "approve",
                    "reason_code": None,
                    "source_system": "state-mdm-v1",
                    "record_hash": "sha256:abc",
                }
            ],
            "slots": [
                {
                    "name": "prior_enrollment_active",
                    "value": True,
                    "from_events": ["MDM-2024-A891"],
                }
            ],
        })

    real_async = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async(*args, **kwargs)

    monkeypatch.setattr(mdm_mod.httpx, "AsyncClient", _factory)

    slice_ = asyncio.run(HttpMdmSource().lookup(
        intake=Intake(referral_id="MC-4242"),
        program_family_id="cardiovita",
        tenant_id="tnt-x",
        config=MdmSourceConfig(
            provider="state-mdm-v1",
            base_url="https://mdm.example.com/api",
            api_key="tok-secret",
            max_window_days=1825,
            max_events=200,
        ),
    ))
    # Correct endpoint + auth on the wire.
    assert captured["url"].endswith("/history-slices")
    assert captured["auth"] == "Bearer tok-secret"
    # Query payload carries scope + identity.
    assert captured["body"]["member_id"] == "MC-4242"
    assert captured["body"]["program_family_id"] == "cardiovita"
    assert captured["body"]["tenant_id"] == "tnt-x"
    assert captured["body"]["window_days"] == 1825
    assert captured["body"]["max_events"] == 200
    # Response parsed into typed slice + hash re-computed locally.
    assert len(slice_.events) == 1
    assert slice_.events[0].event_id == "MDM-2024-A891"
    assert len(slice_.slots) == 1
    assert slice_.slots[0].name == "prior_enrollment_active"
    assert slice_.snapshot_hash.startswith("sha256:")
    # Provider tag surfaces the connector name for the ledger.
    assert slice_.source_provider == "state-mdm-v1"


def test_http_mdm_source_rejects_missing_base_url() -> None:
    """Fail fast if config is incomplete — better than a mysterious 404
    from a rstrip('/') on None."""
    with pytest.raises(ValueError, match="base_url"):
        asyncio.run(HttpMdmSource().lookup(
            intake=Intake(referral_id="MC-1"),
            program_family_id="cardiovita",
            tenant_id="tnt-x",
            config=MdmSourceConfig(provider="state-mdm-v1"),
        ))


def test_build_mdm_source_dispatches_by_provider() -> None:
    """Factory picks the right adapter from `config.provider`. Kept as an
    explicit test so a rename or a new provider string doesn't silently
    fall through to the wrong branch."""
    assert isinstance(build_mdm_source(MdmSourceConfig(provider="null")), NullMdmSource)
    assert isinstance(build_mdm_source(MdmSourceConfig(provider="")), NullMdmSource)
    assert isinstance(build_mdm_source(MdmSourceConfig(provider="simulated")), SimulatedMdmSource)
    assert isinstance(build_mdm_source(MdmSourceConfig(provider="demo")), SimulatedMdmSource)
    # Any unrecognized provider is treated as a real-connector name and
    # routed to HttpMdmSource. The provider string flows through to the
    # slice's source_provider tag when lookup runs.
    assert isinstance(build_mdm_source(MdmSourceConfig(provider="state-mdm-v1")), HttpMdmSource)


def test_history_slice_roundtrips_via_pydantic() -> None:
    """Slice serialises + parses without drift so the ledger row schema
    (JSONB persistence) can round-trip through model_dump / model_validate
    at persist / read time without losing fidelity."""
    event = HistoryEvent(
        event_id="MDM-2024-A891",
        event_type="determination",
        program_id="SNAP",
        program_family_id="SNAP",
        date="2024-03-14",  # type: ignore[arg-type]
        outcome="deny",
        reason_code="R-403",
        source_system="state-crm-prod",
        record_hash="sha256:abc",
    )
    slot = HistorySlot(
        name="prior_denial_in_12mo",
        value=True,
        from_events=[event.event_id],
    )
    slice_ = HistorySlice(
        member_id="M-42",
        program_family_id="SNAP",
        events=[event],
        slots=[slot],
        snapshot_hash="sha256:beef",
        snapshot_at="2026-09-13T00:00:00+00:00",  # type: ignore[arg-type]
        source_provider="state-mdm-v1",
    )
    dumped = slice_.model_dump(mode="json")
    restored = HistorySlice.model_validate(dumped)
    assert restored == slice_
