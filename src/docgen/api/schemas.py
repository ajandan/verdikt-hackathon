from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl

from docgen.providers.base import MappingProposal, Suggestion  # re-export

OutputFormat = Literal["docx", "pdf"]


class UploadResponse(BaseModel):
    template_id: uuid.UUID
    version_id: uuid.UUID
    version: int
    preflight: dict[str, Any]
    accepted: bool


class MappingResponse(BaseModel):
    version_id: uuid.UUID
    proposal: MappingProposal


class FreezeRequest(BaseModel):
    suggestions: list[Suggestion]
    notes: str | None = None


class FreezeResponse(BaseModel):
    template_id: uuid.UUID
    version_id: uuid.UUID
    version: int
    version_hash: str
    is_frozen: bool
    schema_: dict[str, Any] = Field(alias="schema")

    model_config = {"populate_by_name": True}


class RenderRequest(BaseModel):
    template_id: uuid.UUID
    version: int | None = None
    # BCP 47 tag or "default"; None falls back to "default" for the requested version.
    locale: str | None = None
    data: dict[str, Any]
    caller: str | None = None
    async_: bool = Field(default=False, alias="async")
    output_format: OutputFormat = "docx"
    callback_url: HttpUrl | None = None

    model_config = {"populate_by_name": True}


class RenderMappedRequest(BaseModel):
    """Render using a saved caller profile. The template + version are pinned by
    the profile so the caller only ships their native JSON (`data`)."""

    profile_id: uuid.UUID
    data: dict[str, Any]
    locale: str | None = None
    caller: str | None = None
    async_: bool = Field(default=False, alias="async")
    output_format: OutputFormat = "docx"
    callback_url: HttpUrl | None = None

    model_config = {"populate_by_name": True}


class RenderResponse(BaseModel):
    job_id: uuid.UUID
    state: str
    inline: bool = False
    output_format: OutputFormat = "docx"


class JobStatus(BaseModel):
    job_id: uuid.UUID
    state: str
    submitted_at: datetime
    completed_at: datetime | None
    error: str | None
    result_bytes: int | None
    output_format: OutputFormat | None = None


class ProvenanceEntry(BaseModel):
    render_job_id: uuid.UUID
    version_hash: str
    caller: str | None
    rendered_at: datetime


class TemplateSchemaResponse(BaseModel):
    template_id: uuid.UUID
    version: int
    version_hash: str
    schema_: dict[str, Any] = Field(alias="schema")
    json_schema: dict[str, Any]

    model_config = {"populate_by_name": True}


class PublishRequest(BaseModel):
    version: int
    # None → publish the highest frozen sub for `version` (natural default).
    subversion: int | None = None
    actor: str | None = None


class PublishResponse(BaseModel):
    template_id: uuid.UUID
    version: int
    subversion: int
    previous_version: int | None
    previous_subversion: int | None
    event_kind: str  # "publish" | "rollback"
    actor: str | None


class PublishEventResponse(BaseModel):
    version: int
    subversion: int
    previous_version: int | None
    previous_subversion: int | None
    event_kind: str
    actor: str | None
    created_at: datetime
