"""REST endpoints for the pap_pipeline JSON intake channel.

One route so far: ``POST /pap/programs/{slug}/intake`` — a caller submits
a structured JSON payload keyed by the program's extraction slots (either
by slot ``name`` or ``source_field``) and gets back the pipeline's
verdict inline.

Auth reuses :class:`docgen.auth.ApiKeyAuthMiddleware` — the same Bearer
tokens that gate ``/templates`` and ``/render``. Tenant is resolved from
the API key; the ``{slug}`` in the path is scoped to that tenant so two
tenants can register a program with the same slug without collision.

The pipeline runs sync in-process inside a thread-pool executor (see
:func:`pap_pipeline.api_hook.run_json_intake_sync`) to keep the FastAPI
event loop free during the Nemotron round-trip.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from docgen.api.deps import get_tenant_id
from docgen.config import get_settings
from pap_pipeline.api_hook import (
    CHANNEL_REST,
    ApiIntakeResult,
    IntakeValidationFailed,
    PipelineExecutionFailed,
    ProgramNotFound,
    run_json_intake_sync,
)

log = structlog.get_logger(__name__)


router = APIRouter(prefix="/pap", tags=["pap"])


# Hard cap on the JSON intake body. Real payloads are ~few KB (structured
# form fields, not attachments); anything much larger is either a caller
# bug or an attempt to burn the Pydantic parser budget on unbounded input.
# Enforced by inspecting Content-Length before FastAPI hands the body to
# the pydantic model — a chunked upload with no Content-Length still gets
# capped implicitly because Uvicorn's default h11_max_incomplete_event
# rejects oversize frames.
_MAX_INTAKE_BODY_BYTES = 512 * 1024  # 512 KB


class PapIntakeRequest(BaseModel):
    """One JSON intake submission.

    ``intake`` is the structured payload — keys are either the program's
    slot ``name`` values or their ``source_field`` values (mixing is
    fine; canonical name wins on collision). Values are coerced to each
    slot's declared type; type failures come back as a 400 with a list of
    per-field errors.

    ``actor`` is a caller-supplied label for the reviewer UI — e.g. the
    CRM user or ticket number that submitted this case. Optional; we
    default to the API key's short prefix. Never send secrets here — it's
    persisted verbatim on the case row.
    """

    intake: dict[str, Any] = Field(
        ...,
        description="Structured intake fields keyed by slot name or source_field.",
    )
    actor: str | None = Field(
        default=None,
        max_length=120,
        description="Optional caller label surfaced on the case row.",
    )
    callback_url: str | None = Field(
        default=None,
        max_length=1024,
        description=(
            "HTTPS URL that receives the final verdict when the program "
            "requires reviewer approval. Ignored for programs that "
            "auto-approve M2M submissions. Signed with an "
            "X-Verdikt-Signature HMAC header derived from the caller's API "
            "key so the receiver can verify authenticity."
        ),
    )


class PapIntakeErrorField(BaseModel):
    field: str
    message: str


class PapIntakeResponse(BaseModel):
    """Sync verdict + case reference for a JSON intake submission."""

    case_id: str = Field(..., description="Short human-readable ID (`QC-0042`).")
    event_id: uuid.UUID = Field(..., description="Underlying inbox_events row id.")
    status: str = Field(
        ...,
        description=(
            "`succeeded` — verdict is final and returned inline. "
            "`queued` — case is holding for reviewer approval; the final "
            "verdict will be POSTed to `callback_url` if supplied. "
            "`failed` — pipeline error; check `error`."
        ),
    )
    review_state: str | None = Field(
        default=None,
        description="pending_review / auto_approved / approved / rejected.",
    )
    outcome: str = Field(
        ...,
        description=(
            "approve / deny / missing_info. For queued cases this is the "
            "*draft* verdict — the reviewer's decision is authoritative."
        ),
    )
    decision: dict[str, Any] = Field(..., description="Full Decision dict.")
    intake: dict[str, Any] = Field(..., description="Coerced Intake dict.")
    unknown_keys: list[str] = Field(
        default_factory=list,
        description=(
            "Payload keys that didn't match any slot in the program's schema. "
            "Surfaced so a caller can catch typos, but they're never fatal."
        ),
    )
    callback_url: str | None = Field(
        default=None,
        description="Echo of the callback URL we'll POST the final verdict to.",
    )
    requires_review: bool = Field(
        default=False,
        description=(
            "True when this program holds M2M submissions for reviewer "
            "approval — signals to the caller that `outcome` is a draft."
        ),
    )


class PapIntakeValidationErrorResponse(BaseModel):
    detail: str
    errors: list[PapIntakeErrorField]
    missing_required: list[str] = Field(default_factory=list)
    unknown_keys: list[str] = Field(default_factory=list)
    event_id: uuid.UUID | None = None


@router.post(
    "/programs/{slug}/intake",
    response_model=PapIntakeResponse,
)
async def submit_pap_intake(
    slug: str,
    body: PapIntakeRequest,
    request: Request,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
) -> PapIntakeResponse:
    """Submit a structured JSON intake against a program's extraction schema.

    Returns 201 with the pipeline's verdict inline once retrieve + reason
    finish. Validation failures (bad types, missing required fields)
    return 400 and the case row is still written to the reviewer queue so
    the failed attempt is auditable — the response includes its
    ``event_id`` for follow-up.

    Rejects bodies larger than :data:`_MAX_INTAKE_BODY_BYTES` with a 413.
    The cap runs on the Content-Length header so an oversized upload is
    refused before it lands in memory. Chunked uploads without a
    Content-Length are handled by Starlette's own request-body limits.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_INTAKE_BODY_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"intake body exceeds {_MAX_INTAKE_BODY_BYTES // 1024} KB cap"
                    ),
                )
        except ValueError:
            raise HTTPException(status_code=400, detail="malformed content-length") from None

    actor = body.actor or _actor_from_request(request)
    settings = get_settings()

    try:
        result: ApiIntakeResult = await run_in_threadpool(
            run_json_intake_sync,
            tenant_id,
            slug,
            body.intake,
            settings,
            actor=actor,
            channel=CHANNEL_REST,
            callback_url=body.callback_url,
        )
    except ValueError as e:
        # Raised by run_json_intake_sync for malformed callback_url etc.
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ProgramNotFound as e:
        # 404 (never 403) for cross-tenant / missing-program lookups so we
        # don't leak whether a slug exists in some other tenant.
        raise HTTPException(status_code=404, detail=str(e)) from e
    except IntakeValidationFailed as e:
        raise HTTPException(
            status_code=400,
            detail={
                "detail": "intake validation failed",
                "errors": [
                    {"field": err.field, "message": err.message} for err in e.errors
                ],
                "missing_required": e.missing_required,
                "unknown_keys": e.unknown_keys,
                "event_id": str(e.event_id),
            },
        ) from e
    except PipelineExecutionFailed as e:
        # Pipeline failure after validation passed — the case row is
        # already marked failed for the reviewer queue; the caller gets a
        # 500 with the event_id so support can trace it.
        raise HTTPException(
            status_code=500,
            detail={
                "detail": "pipeline execution failed",
                "message": str(e),
                "event_id": str(e.event_id),
            },
        ) from e

    from fastapi.responses import JSONResponse

    payload = PapIntakeResponse(
        case_id=result.case_id,
        event_id=result.event_id,
        status=result.status,
        review_state=result.review_state,
        outcome=result.outcome,
        decision=result.decision,
        intake=result.intake,
        unknown_keys=result.unknown_keys,
        callback_url=result.callback_url,
        requires_review=result.requires_review,
    )
    # 201 for a final verdict, 202 when the case is queued for reviewer
    # approval — matches how Stripe/Plaid signal "not final yet".
    status_code = 202 if result.status == "queued" else 201
    return JSONResponse(
        content=payload.model_dump(mode="json"),
        status_code=status_code,
    )


def _actor_from_request(request: Request) -> str:
    """Default actor label when the caller didn't supply one.

    Uses whatever hint the API-key middleware left on request.state —
    typically the key's display name or short prefix. Falls back to
    ``rest`` so the case row is never empty.
    """
    for attr in ("api_key_name", "api_key_prefix", "api_key_id"):
        v = getattr(request.state, attr, None)
        if v:
            return str(v)[:100]
    return "rest"


__all__ = ["router"]
