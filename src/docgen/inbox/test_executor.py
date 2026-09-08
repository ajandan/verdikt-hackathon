"""Multi-test executor: plan N payloads → render each → verdict each.

Sits between the inbox agent and the render pipeline. For an
already-frozen TemplateVersion, it:

  1. Asks the AI planner for N varied payloads (Task 13).
  2. Runs each through execute_merge, gets docx or pdf bytes.
  3. Persists each artifact to the documents blob bucket under a stable
     `renders/inbox-test/<event_id>/<ordinal>.<ext>` key.
  4. Asks the AI verdict scorer for a pass/fail + one-sentence
     explanation per trial (Task 14).
  5. Inserts one InboxTestResult row per trial.

Returns the list of persisted rows so the reply builder can render the
summary table without a second DB round-trip.

Failure model: any one trial failing to render still gets a row (blob_key
NULL, verdict = "Render failed: ..."), so the reviewer sees the failure
in the summary table rather than a silent gap in ordinals.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.orm import Session

from docgen.config import Settings
from docgen.core.render_pipeline import execute_merge
from docgen.inbox.test_planner import DEFAULT_TRIAL_COUNT, Trial, plan_trials
from docgen.inbox.test_verdict import extract_rendered_text, score_trial
from docgen.store.blob import put_object
from docgen.store.models import InboxTestResult, TemplateVersion

log = structlog.get_logger(__name__)


@dataclass
class ExecutedTrial:
    """A rendered + scored trial, ready for the reply builder + UI."""

    ordinal: int
    name: str
    description: str
    payload: dict[str, Any]
    rendered: bytes | None
    ext: str
    render_error: str | None
    passed: bool
    verdict: str
    blob_key: str | None
    # Populated after the row is committed so the reply builder can mint a
    # signed download URL without a second SELECT. None when the batch
    # insert failed (blob_key/verdict still readable, but no downloadable
    # row exists in Postgres).
    test_result_id: uuid.UUID | None = None


def _render_one(
    tv: TemplateVersion,
    payload: dict[str, Any],
    *,
    settings: Settings,
) -> tuple[bytes | None, str, str | None]:
    """One (rendered_bytes, ext, error) tuple — mirrors the old _test_render."""
    output_format = "pdf" if tv.pdf_flavor is not None else "docx"
    ext = "pdf" if output_format == "pdf" else "docx"
    try:
        rendered = execute_merge(tv, payload, output_format, settings=settings)
    except Exception as e:
        return None, ext, f"render_failed: {type(e).__name__}: {e}"
    return rendered, ext, None


def _blob_key(event_id: uuid.UUID, ordinal: int, ext: str) -> str:
    return f"renders/inbox-test/{event_id}/{ordinal}.{ext}"


def _content_type(ext: str) -> str:
    if ext == "pdf":
        return "application/pdf"
    return (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    )


def execute_test_matrix(
    session: Session,
    *,
    event_id: uuid.UUID,
    tv: TemplateVersion,
    tenant_ai_provider_config: dict[str, Any] | None,
    settings: Settings,
    count: int = DEFAULT_TRIAL_COUNT,
) -> list[ExecutedTrial]:
    """Plan + render + verdict + persist N trials for one inbox event.

    Persists both blobs (S3) and rows (Postgres). The InboxTestResult rows
    are committed as a single batch at the end so a mid-loop failure
    doesn't leave the parent event with a partial table.
    """
    trials: list[Trial]
    try:
        trials = plan_trials(
            tv.schema or {},
            provider_config=tenant_ai_provider_config,
            ai_base_url=settings.ai_base_url,
            ai_api_key=settings.ai_api_key,
            ai_model=settings.ai_model,
            ai_provider=settings.ai_provider,
            timeout=settings.ai_timeout_seconds,
            count=count,
        )
    except Exception as e:
        log.warning(
            "inbox_test_matrix_planner_failed",
            event_id=str(event_id),
            error=f"{type(e).__name__}: {e}",
        )
        return []

    executed: list[ExecutedTrial] = []
    rows: list[InboxTestResult] = []

    for i, trial in enumerate(trials, start=1):
        rendered, ext, render_error = _render_one(
            tv, trial.payload, settings=settings
        )

        blob_key: str | None = None
        if rendered is not None:
            blob_key = _blob_key(event_id, i, ext)
            try:
                put_object(
                    settings.s3_bucket_documents,
                    blob_key,
                    rendered,
                    content_type=_content_type(ext),
                )
            except Exception as e:
                # Blob write failure shouldn't kill the whole matrix — mark
                # the trial as unrenderable so the reviewer still sees the
                # row + verdict, just without a download link.
                log.warning(
                    "inbox_test_matrix_blob_write_failed",
                    event_id=str(event_id),
                    ordinal=i,
                    error=f"{type(e).__name__}: {e}",
                )
                blob_key = None
                render_error = render_error or f"blob_write_failed: {e}"

        rendered_text = ""
        if rendered is not None and render_error is None:
            try:
                rendered_text = extract_rendered_text(rendered, ext)
            except Exception as e:
                log.warning(
                    "inbox_test_matrix_extract_failed",
                    event_id=str(event_id),
                    ordinal=i,
                    error=f"{type(e).__name__}: {e}",
                )

        verdict = score_trial(
            payload=trial.payload,
            rendered_text=rendered_text,
            render_error=render_error,
            provider_config=tenant_ai_provider_config,
            ai_base_url=settings.ai_base_url,
            ai_api_key=settings.ai_api_key,
            ai_model=settings.ai_model,
            ai_provider=settings.ai_provider,
            timeout=settings.ai_timeout_seconds,
            schema=tv.schema or None,
        )

        executed.append(
            ExecutedTrial(
                ordinal=i,
                name=trial.name,
                description=trial.description,
                payload=trial.payload,
                rendered=rendered,
                ext=ext,
                render_error=render_error,
                passed=verdict.passed,
                verdict=verdict.verdict,
                blob_key=blob_key,
            )
        )
        rows.append(
            InboxTestResult(
                inbox_event_id=event_id,
                ordinal=i,
                name=trial.name,
                data_json=trial.payload,
                blob_key=blob_key,
                ext=ext if blob_key else None,
                verdict=verdict.verdict,
                passed=verdict.passed,
            )
        )

    if rows:
        try:
            session.add_all(rows)
            session.commit()
            # Refresh so the auto-generated ids are visible to the caller.
            for row in rows:
                session.refresh(row)
            for trial, row in zip(executed, rows):
                trial.test_result_id = row.id
        except Exception as e:
            session.rollback()
            log.warning(
                "inbox_test_matrix_row_insert_failed",
                event_id=str(event_id),
                error=f"{type(e).__name__}: {e}",
            )
    return executed
