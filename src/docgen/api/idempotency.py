"""Idempotency-Key support for POST /render.

Contract (mirrors Stripe's idempotency semantics):
- Same `Idempotency-Key` + same payload → return the ORIGINAL job's response.
- Same key + DIFFERENT payload → 409 (key already used with a different body).
- No key → normal path, no dedupe.

Keys are scoped to (tenant_id, key) so two callers can safely reuse strings.
The payload hash is a SHA-256 of the canonical JSON of `data + version + locale +
output_format + caller + callback_url` — the request fingerprint that would
otherwise produce a distinct render.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from docgen.store.models import IdempotencyKey

# Max header length we accept, following common HTTP conventions.
MAX_KEY_LENGTH = 255


def hash_request(fingerprint: dict[str, Any]) -> str:
    """SHA-256 of the fingerprint dict serialized as canonical JSON."""
    canonical = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_key(raw: str | None) -> str | None:
    """Trim and validate. Returns None if absent, raises ValueError if invalid."""
    if raw is None:
        return None
    v = raw.strip()
    if not v:
        return None
    if len(v) > MAX_KEY_LENGTH:
        raise ValueError(f"Idempotency-Key exceeds {MAX_KEY_LENGTH} chars")
    return v


async def lookup(
    session: AsyncSession, tenant_id: uuid.UUID, key: str
) -> IdempotencyKey | None:
    result = await session.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.tenant_id == tenant_id,
            IdempotencyKey.key == key,
        )
    )
    return result.scalar_one_or_none()


class IdempotencyRaceMismatch(Exception):
    """Raised when a concurrent request beat us to the key with a different payload."""


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    request_hash: str,
    job_id: uuid.UUID,
) -> IdempotencyKey:
    """Persist the key -> job mapping. On a race with another request using the
    same key, return the *winning* row so the caller can honor it (echo the
    winner's job_id in the response). Raises `IdempotencyRaceMismatch` if the
    winner used a different payload — same 409 semantics as a sequential collision.

    Returning the winner's row (rather than swallowing silently) keeps the
    contract: any two calls with the same (tenant, key) get the same job_id.
    """
    row = IdempotencyKey(
        tenant_id=tenant_id, key=key, request_hash=request_hash, job_id=job_id,
    )
    session.add(row)
    try:
        await session.commit()
        return row
    except IntegrityError:
        await session.rollback()
        winner = await lookup(session, tenant_id, key)
        if winner is None:
            # Race then delete — extremely unlikely, treat as our own insert.
            return row
        if winner.request_hash != request_hash:
            raise IdempotencyRaceMismatch(
                "Idempotency-Key already used with a different payload"
            ) from None
        return winner
