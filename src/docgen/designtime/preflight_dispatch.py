"""Backend-agnostic dispatch for design-time preflight + AI mapping.

Same contract regardless of backend:
    (template_bytes, kind, provider_config?) -> MappingProposal

Backends:
    inline      — call preflight_job.run_preflight_and_label in-process.
                  Zero infra dependencies; matches the pre-dispatch behavior
                  and stays the default so nothing regresses.
    subprocess  — write the template to a temp dir and shell out to the
                  standalone CLI. Same JSON contract as the Nebius
                  Serverless Job image, so this mode is a local proxy for
                  the real thing while the Jobs API integration is being
                  wired up.
    nebius_job  — submit to Nebius Serverless Jobs. Not yet implemented;
                  raises loudly rather than silently downgrading, so a
                  misconfigured prod deploy can't ship a fake-async story.

The mapping.py entry point can call `dispatch_preflight_and_label` and
stay ignorant of which backend it hit; the JSON on the way back is
always the same shape.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from docgen.config import get_settings
from docgen.designtime.preflight_job import run_preflight_and_label
from docgen.providers.base import MappingProposal


class PreflightBackendError(RuntimeError):
    """The selected backend is unavailable or misconfigured at call time."""


async def dispatch_preflight_and_label(
    *,
    template_bytes: bytes,
    kind: str = "docx",
    provider_config: dict[str, Any] | None = None,
    backend: str | None = None,
) -> tuple[MappingProposal, dict[str, Any]]:
    """Run preflight + AI mapping via the configured backend.

    Returns (proposal, result_dict). The result_dict is the same JSON the
    Nebius Serverless Job would emit, so callers can persist / audit it
    without knowing which backend produced it.
    """
    chosen = backend or get_settings().preflight_backend

    if chosen == "inline":
        result = await run_preflight_and_label(
            template_bytes=template_bytes,
            kind=kind,
            provider_config=provider_config,
        )
    elif chosen == "subprocess":
        result = await _run_via_subprocess(
            template_bytes=template_bytes,
            kind=kind,
            provider_config=provider_config,
        )
    elif chosen == "nebius_job":
        raise PreflightBackendError(
            "preflight_backend='nebius_job' is not yet implemented. "
            "Register the job on Nebius and land the submit-then-poll "
            "client before enabling this mode. Use 'inline' or "
            "'subprocess' in the meantime."
        )
    else:  # pragma: no cover — pydantic Literal keeps us out of here.
        raise PreflightBackendError(f"unknown preflight_backend: {chosen!r}")

    proposal = MappingProposal.model_validate(result["mapping"])
    return proposal, result


async def _run_via_subprocess(
    *,
    template_bytes: bytes,
    kind: str,
    provider_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fork the CLI. Same contract the Serverless Job container fulfills."""
    if kind != "docx":
        raise NotImplementedError(f"subprocess backend supports 'docx' only; got {kind!r}")

    settings = get_settings()
    template_hash = hashlib.sha256(template_bytes).hexdigest()[:12]

    with tempfile.TemporaryDirectory(prefix="qpj-") as tmp:
        tmp_dir = Path(tmp)
        input_path = tmp_dir / f"{template_hash}.docx"
        output_path = tmp_dir / f"{template_hash}.result.json"
        input_path.write_bytes(template_bytes)

        argv: list[str] = [
            sys.executable,
            "-m",
            "docgen.designtime.preflight_job",
            "--input",
            str(input_path),
            "--out",
            str(output_path),
            "--kind",
            kind,
        ]
        if provider_config is not None:
            cfg_path = tmp_dir / "provider.json"
            cfg_path.write_text(json.dumps(provider_config), encoding="utf-8")
            argv.extend(["--provider-config", str(cfg_path)])

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.preflight_subprocess_timeout_seconds,
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise PreflightBackendError(
                f"preflight subprocess timed out after "
                f"{settings.preflight_subprocess_timeout_seconds:.0f}s"
            ) from exc

        if proc.returncode != 0:
            raise PreflightBackendError(
                f"preflight subprocess exited {proc.returncode}: "
                f"{stderr.decode(errors='replace').strip()}"
            )

        if not output_path.exists():
            raise PreflightBackendError(
                "preflight subprocess did not write a result file. "
                f"stdout: {stdout.decode(errors='replace').strip()[:400]}"
            )

        return json.loads(output_path.read_text(encoding="utf-8"))
