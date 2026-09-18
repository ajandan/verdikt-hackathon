"""AI verdict scorer — asks a text-only NVIDIA Nemotron model whether a
rendered trial output looks correct given its input payload.

For both PDF and DOCX outputs we send the payload JSON alongside a
plain-text extract of the rendered document. PDF text extraction uses
pdfplumber (`page.extract_words()`) with a y-cluster + x sort so filled
AcroForms read in visual order instead of content-stream order. The
scorer used to rasterize PDF pages to PNG and hand them to a vision-LM,
but no NVIDIA vision model is available on Nebius Token Factory yet —
the text path is the compliant option, and Nemotron-Super handles the QA
prompt reliably.

Failure semantics: an unreachable model or an empty content response marks
the trial as FAILED with a note. This stops fallback silence from
inflating scores when reasoning models return empty content.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from lxml import etree

from docgen.core.xml_safe import safe_fromstring

log = structlog.get_logger(__name__)

_MAX_EXTRACTED_CHARS = 12_000
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_SYSTEM_PROMPT_TEXT = """You are a QA reviewer inspecting a document that was rendered from a
template. You are given (a) the frozen field schema (each field annotated with
its printed caption and the section it sits under, when known), (b) the input
payload the renderer received, and (c) the plain-text extract of the rendered
document.

Judge fidelity + semantic placement. Return STRICT JSON:

{
  "passed": true | false,
  "verdict": "<one or two sentences>"
}

Rules:
- Use the `caption` and `section` annotations on each field to locate its
  slot in the rendered text. When two fields share the same printed
  caption (e.g. "PRINT NAME" appears under both a facility section and a
  prescriber section), the section anchor is what tells them apart. Do
  NOT flag a mismatch just because two identical-looking values appear
  in the text — check the surrounding section heading first.
- `passed: true` when the render faithfully reflects the payload AND every
  value is in the right slot for its field.
- `passed: false` when values are missing or in the wrong semantic slot.
- Keep `verdict` short (<= 300 chars).
- Do NOT include any text outside the JSON object.
"""


@dataclass
class Verdict:
    """Result of scoring one trial."""

    passed: bool
    verdict: str


def _extract_docx_text(docx_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        try:
            xml = zf.read("word/document.xml")
        except KeyError:
            return ""
    root = safe_fromstring(xml)
    parts: list[str] = []
    for t in root.iter(f"{{{_W_NS}}}t"):
        if t.text:
            parts.append(t.text)
    return "\n".join(parts)


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Text extract from a rendered PDF in visual reading order.

    Delegates word extraction + row grouping to `designtime.pdf_text` so
    every consumer (verdict, field detection, geometric labelling, OCR
    prompt) shares one implementation.
    """
    from docgen.designtime.pdf_text import extract_words, group_into_visual_rows

    try:
        import pdfplumber  # type: ignore[import-untyped]

        parts: list[str] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                words = extract_words(page)
                if not words:
                    continue
                page_lines: list[str] = []
                for row in group_into_visual_rows(words):
                    line = " ".join(w.text for w in row).strip()
                    if line:
                        page_lines.append(line)
                if page_lines:
                    parts.append("\n".join(page_lines))
        return "\n".join(parts)
    except Exception:
        pass
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]

        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for page in reader.pages:
            try:
                txt = page.extract_text() or ""
            except Exception:
                txt = ""
            if txt:
                parts.append(txt)
        return "\n".join(parts)
    except Exception:
        return ""


def extract_rendered_text(rendered: bytes, ext: str) -> str:
    """Public text-extract entry point retained for compatibility."""
    if ext == "pdf":
        text = _extract_pdf_text(rendered)
    else:
        text = _extract_docx_text(rendered)
    if len(text) > _MAX_EXTRACTED_CHARS:
        text = text[:_MAX_EXTRACTED_CHARS] + "\n[truncated]"
    return text


def _fallback_verdict(reason: str) -> Verdict:
    """AI unreachable — mark the trial as FAILED so scores aren't inflated.

    Earlier revisions returned passed=True with a note; that let empty-content
    responses from reasoning models silently green-light trials. Reviewers
    now see an explicit failure with the reason and can retry manually.
    """
    return Verdict(passed=False, verdict=f"AI verdict unavailable ({reason}) — needs manual review")


def _format_schema_summary(schema: dict[str, Any] | None) -> str:
    """Per-field lines the verdict LLM uses to bind rendered text to slots.

    When available, the freeze-time-persisted `format` block gives the LLM
    the printed caption AND the section heading — critical for templates
    with similarly-labeled sections (e.g. a facility contact "PRINT NAME"
    and a prescriber signature "PRINT NAME"). Without those anchors the
    verdict LLM regularly attributes rendered text to the wrong field and
    fires false-positive FAILs.
    """
    if not schema:
        return "(no schema available)"
    fields = schema.get("fields") or []
    if not fields:
        return "(no schema fields)"
    lines: list[str] = []
    for f in fields:
        name = str(f.get("name") or "").strip()
        if not name:
            continue
        ftype = str(f.get("type") or "string")
        desc = str(f.get("description") or "").strip()
        line = f"- {name} ({ftype})"
        if desc:
            line += f": {desc}"
        fmt = f.get("format") if isinstance(f.get("format"), dict) else None
        if fmt:
            caption = str(fmt.get("caption") or "").strip()
            section = str(fmt.get("section") or "").strip()
            group = str(fmt.get("group") or "").strip()
            hints: list[str] = []
            if caption:
                hints.append(f'caption="{caption}"')
            if section:
                hints.append(f'section="{section}"')
            if group:
                hints.append(f"group={group}")
            if hints:
                line += "  [" + ", ".join(hints) + "]"
        lines.append(line)
    return "\n".join(lines) if lines else "(no schema fields)"


def _post_verdict(
    *,
    body: dict[str, Any],
    base_url: str,
    api_key: str,
    timeout_s: float,
    provider_name: str,
    model: str,
) -> Verdict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "api-key": api_key,
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if not content:
            raise ValueError("empty content in LLM response")
        # Reasoning models sometimes wrap JSON in ```json fences even when
        # response_format was requested. Strip fences before parsing.
        text = str(content).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[: -3]
            text = text.strip()
            if text.startswith("json"):
                text = text[4:].lstrip()
        parsed = json.loads(text)
    except (
        httpx.HTTPError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as e:
        log.warning(
            "inbox_test_verdict_llm_failed",
            error=f"{type(e).__name__}: {e}",
            provider=provider_name,
            model=model,
        )
        return _fallback_verdict(f"AI unreachable ({type(e).__name__})")

    passed = bool(parsed.get("passed"))
    verdict = str(parsed.get("verdict") or "").strip() or "No verdict returned."
    return Verdict(passed=passed, verdict=verdict[:1000])


def score_trial(
    *,
    payload: dict[str, Any],
    rendered: bytes | None,
    rendered_text: str,
    ext: str,
    render_error: str | None,
    provider_config: dict[str, Any] | None,
    ai_base_url: str | None,
    ai_api_key: str | None,
    ai_model: str,
    timeout: float,
    schema: dict[str, Any] | None = None,
    ai_provider: str = "",
) -> Verdict:
    """Ask the LLM if the render output matches the payload.

    Sends the text extract + payload JSON to the configured text model
    (Nemotron-Super on Nebius by default) for both PDF and DOCX outputs.

    Fallback: when the model is unreachable or returns empty content, the
    trial is marked FAILED so the score isn't silently inflated.
    """
    if render_error:
        return Verdict(passed=False, verdict=f"Render failed: {render_error}")

    cfg = provider_config or {}
    provider_name = cfg.get("provider") or ai_provider or ""
    api_key = cfg.get("api_key") or ai_api_key

    if not rendered_text.strip():
        return Verdict(
            passed=False,
            verdict="Render produced no extractable text — likely an empty or malformed document.",
        )

    base_url = cfg.get("base_url") or ai_base_url
    model = cfg.get("verdict_model") or ai_model
    timeout_s = float(cfg.get("timeout_seconds") or timeout)

    if provider_name == "nebius":
        from docgen.providers.nebius import DEFAULT_BASE_URL, DEFAULT_MODEL

        base_url = base_url or DEFAULT_BASE_URL
        model = model or DEFAULT_MODEL

    if not api_key or not base_url or provider_name not in ("nebius", "openai_compat"):
        return _fallback_verdict("no AI provider configured")

    user_prompt = (
        "Schema fields:\n"
        + _format_schema_summary(schema)
        + "\n\nInput payload:\n"
        + json.dumps(payload, indent=2, default=str)
        + "\n\nRendered document text:\n---\n"
        + rendered_text
        + "\n---"
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT_TEXT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 4000,
        # vLLM-hosted Nemotron reasoning models honour this top-level knob;
        # without it they burn the output-token budget on a <think> preamble
        # and return empty `content`. The correct key is `enable_thinking`,
        # not `thinking` — the wrong spelling silently no-ops.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    return _post_verdict(
        body=body,
        base_url=base_url,
        api_key=api_key,
        timeout_s=timeout_s,
        provider_name=provider_name,
        model=model,
    )
