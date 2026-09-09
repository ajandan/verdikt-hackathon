"""Standalone preflight + AI mapping proposal — packaged for Nebius Serverless Jobs.

Deliberately app-decoupled: no DB session, no blob storage, no tenant model. The
container reads a blank template from disk (mounted volume / downloaded blob),
runs preflight, asks the configured provider for a Jinja-tag + typed-field
proposal, and writes a self-contained JSON result. The web app kicks it off
asynchronously at upload time and polls / receives the result via object
storage, keeping the request path free of multi-second inference latency.

Contract (input):
    template bytes on disk (--input path)
    kind = "docx" (prototype scope — .pdf detect/label follows once the
                   Nemotron labeler path lands in this job container)
    provider config = same overlay shape as Tenant.ai_provider_config
        { provider, base_url, api_key, model, timeout_seconds }
    (falls back to DOCGEN_AI_* env if not supplied)

Contract (output JSON):
    {
      "kind": "docx",
      "template_hash": "<sha256 hex>",
      "elapsed_seconds": <float>,
      "provider": "nebius" | "stub" | "openai_compat",
      "preflight": PreflightReport.to_dict(),
      "mapping":  { "suggestions": [...], "notes": str | None },
    }
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from docgen.core.preflight import (
    _extract_document_xml,
    _extract_text_with_tags,
    preflight,
)
from docgen.providers import get_provider


async def run_preflight_and_label(
    *,
    template_bytes: bytes,
    kind: str = "docx",
    provider_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if kind != "docx":
        raise NotImplementedError(
            f"preflight_job prototype supports 'docx' only; got {kind!r}"
        )

    started = time.perf_counter()
    template_hash = hashlib.sha256(template_bytes).hexdigest()

    report = preflight(template_bytes)

    document_xml = _extract_document_xml(template_bytes)
    text = _extract_text_with_tags(document_xml)

    provider = get_provider(provider_config)
    proposal = await provider.propose_mapping(text)

    elapsed = time.perf_counter() - started
    return {
        "kind": kind,
        "template_hash": template_hash,
        "elapsed_seconds": round(elapsed, 3),
        "provider": provider.name,
        "preflight": report.to_dict(),
        "mapping": proposal.model_dump(),
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m docgen.designtime.preflight_job",
        description=(
            "Standalone preflight + AI mapping proposal. Reads a blank template, "
            "writes a self-contained result JSON. Designed for Nebius Serverless Jobs."
        ),
    )
    parser.add_argument("--input", "-i", required=True, help="Path to input .docx template.")
    parser.add_argument(
        "--out",
        "-o",
        default="-",
        help="Path to write result JSON (default: stdout).",
    )
    parser.add_argument("--kind", default="docx", choices=["docx"], help="Template kind.")
    parser.add_argument(
        "--provider-config",
        default=None,
        help=(
            "Path to JSON file with provider overrides "
            "(provider, base_url, api_key, model, timeout_seconds). "
            "Omit to use DOCGEN_AI_* env vars."
        ),
    )
    args = parser.parse_args(argv)

    template_bytes = Path(args.input).read_bytes()
    provider_config: dict[str, Any] | None = None
    if args.provider_config:
        provider_config = json.loads(Path(args.provider_config).read_text(encoding="utf-8"))

    result = asyncio.run(
        run_preflight_and_label(
            template_bytes=template_bytes,
            kind=args.kind,
            provider_config=provider_config,
        )
    )
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.out == "-":
        sys.stdout.write(payload + "\n")
    else:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
