"""AI verdict scorer — asks the design-time LLM whether a rendered trial
output looks correct given its input payload.

Runs alongside the multi-test executor: for each trial, extract text from
the rendered artifact (docx or pdf), hand it to the LLM with the input
payload, and get back a boolean pass/fail plus a one-sentence explanation.
The verdict is what the reviewer reads in the email summary table and on
the admin portal so they can approve/reject with context, not just a
green-tick/red-cross.

Best-effort semantics mirror the planner: no AI provider configured, or
any HTTP/JSON error, yields a "not evaluated" verdict marked as passing
(so the trial isn't spuriously failed) with an explanation the reviewer
can see.
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

log = structlog.get_logger(__name__)

# Cap the extracted text handed to the model — a long form is ~15 KB, well
# under this. Keeps the request bounded when someone ingests a novel-length
# template.
_MAX_EXTRACTED_CHARS = 12_000
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_SYSTEM_PROMPT = """You are a QA reviewer inspecting a document that was rendered from a
template. You are given (a) the input payload the renderer received and (b) the
plain-text extract of the rendered document.

Judge whether every payload value appears somewhere in the rendered output — the
correct value, in a plausible location. If the rendered output looks garbled
(placeholder tokens still present, values missing or truncated in a way that
suggests a mapping bug), fail the trial.

Return STRICT JSON:

{
  "passed": true | false,
  "verdict": "<one sentence explaining what you saw>"
}

Rules:
- `passed: true` when the render faithfully reflects the payload.
- `passed: false` when values are missing, placeholders leak through
  (e.g. "{{ ... }}" or "[MEMBER_NAME]"), or the output looks structurally wrong.
- Keep `verdict` short (<= 200 chars). Reference concrete evidence.
- Do NOT include any text outside the JSON object.
"""


@dataclass
class Verdict:
    """Result of scoring one trial."""

    passed: bool
    verdict: str


def _extract_docx_text(docx_bytes: bytes) -> str:
    """Flatten every <w:t> run into a plain string."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        try:
            xml = zf.read("word/document.xml")
        except KeyError:
            return ""
    root = etree.fromstring(xml)
    parts: list[str] = []
    for t in root.iter(f"{{{_W_NS}}}t"):
        if t.text:
            parts.append(t.text)
    return "\n".join(parts)


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Text extract from PDF. Best-effort — returns "" if no extractor works."""
    try:
        import pdfplumber  # type: ignore[import-not-found]

        parts: list[str] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                txt = page.extract_text() or ""
                if txt:
                    parts.append(txt)
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
    """Return plain-text extract for the verdict prompt.

    Truncates at `_MAX_EXTRACTED_CHARS` so a huge template can't blow the
    context window on the verdict call.
    """
    if ext == "pdf":
        text = _extract_pdf_text(rendered)
    else:
        text = _extract_docx_text(rendered)
    if len(text) > _MAX_EXTRACTED_CHARS:
        text = text[:_MAX_EXTRACTED_CHARS] + "\n[truncated]"
    return text


def _fallback_verdict(reason: str) -> Verdict:
    """No AI available — pass the trial with a note so it's not silently green."""
    return Verdict(passed=True, verdict=f"Not AI-evaluated: {reason}")


def score_trial(
    *,
    payload: dict[str, Any],
    rendered_text: str,
    render_error: str | None,
    provider_config: dict[str, Any] | None,
    ai_base_url: str | None,
    ai_api_key: str | None,
    ai_model: str,
    timeout: float,
) -> Verdict:
    """Ask the LLM if the render output matches the payload.

    When the render failed outright, we short-circuit with a fail verdict
    that names the error — no need to burn a model call.
    """
    if render_error:
        return Verdict(passed=False, verdict=f"Render failed: {render_error}")
    if not rendered_text.strip():
        return Verdict(
            passed=False,
            verdict="Render produced no extractable text — likely an empty or malformed document.",
        )

    cfg = provider_config or {}
    provider_name = cfg.get("provider") or ""
    base_url = cfg.get("base_url") or ai_base_url
    api_key = cfg.get("api_key") or ai_api_key
    model = cfg.get("model") or ai_model
    timeout_s = float(cfg.get("timeout_seconds") or timeout)

    if provider_name == "nebius":
        from docgen.providers.nebius import DEFAULT_BASE_URL, DEFAULT_MODEL

        base_url = base_url or DEFAULT_BASE_URL
        model = model or DEFAULT_MODEL

    if not api_key or not base_url or provider_name not in ("nebius", "openai_compat"):
        return _fallback_verdict("no AI provider configured")

    user_prompt = (
        "Input payload:\n"
        + json.dumps(payload, indent=2, default=str)
        + "\n\nRendered document text:\n---\n"
        + rendered_text
        + "\n---"
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
    }
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
        parsed = json.loads(content)
    except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as e:
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
