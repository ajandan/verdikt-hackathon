"""Deterministic render — §6 steps 6–8.

Pure function over (tagged template bytes, validated payload) → DOCX bytes.
No AI, no network, no side effects. This module is the single "bone" per §5.
"""
from __future__ import annotations

import io

from docxtpl import DocxTemplate
from jinja2.sandbox import SandboxedEnvironment


class RenderError(Exception):
    pass


def _build_env() -> SandboxedEnvironment:
    # Defense-in-depth against the linter: even if a template with `__class__` or
    # similar sandbox-escape vectors slips through preflight (e.g., API-only upload
    # path that skips the check), the render fails loudly instead of leaking state.
    #
    # autoescape=True escapes `<`, `>`, `&`, `"`, `'` in every substituted value.
    # The render target is XML (word/document.xml), so a payload value containing
    # `</w:t>` or `&` would otherwise corrupt the package or escape run boundaries.
    # Callers that need intentional markup (bold, hyperlinks) must use docxtpl's
    # RichText helper, which docxtpl marks as `Markup` and autoescape passes through.
    return SandboxedEnvironment(autoescape=True)


def merge_docx(template_bytes: bytes, context: dict) -> bytes:
    try:
        tpl = DocxTemplate(io.BytesIO(template_bytes))
        tpl.render(context, jinja_env=_build_env())
        out = io.BytesIO()
        tpl.save(out)
        return out.getvalue()
    except Exception as e:
        raise RenderError(f"Merge failed: {type(e).__name__}: {e}") from e
