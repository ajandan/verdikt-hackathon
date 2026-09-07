"""Architectural boundary tests — encode the security claim from §12.

'The AI service is a separate module that only ever sees blank templates at design time.
The render path has no model in it. This is enforced architecturally, not by policy.'

If any of these tests fail, the deterministic-boundary claim can no longer be made —
that is a security regression, not a style violation. Fix the import, don't loosen the test.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "docgen"


def _imports_of(pkg: Path) -> set[str]:
    """Every dotted import name referenced by any .py file in pkg (recursively)."""
    names: set[str] = set()
    for f in pkg.rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
    return names


def _forbidden_present(imports: set[str], forbidden: set[str]) -> set[str]:
    hits: set[str] = set()
    for imp in imports:
        for f in forbidden:
            if imp == f or imp.startswith(f + "."):
                hits.add(imp)
    return hits


FORBIDDEN_ON_RENDER_PATH = {
    "docgen.providers",
    "docgen.designtime.mapper",
    "openai",
    "anthropic",
    "httpx",  # AI adapter uses httpx; render path must not.
}


def test_core_never_imports_ai_layer() -> None:
    """core/ is the deterministic bone — no AI, no network, no design-time modules."""
    imports = _imports_of(SRC / "core")
    hits = _forbidden_present(imports, FORBIDDEN_ON_RENDER_PATH)
    assert not hits, (
        f"core/ imports forbidden modules on the render path: {sorted(hits)}. "
        "This breaks §12's deterministic-boundary claim."
    )


def test_render_actor_never_imports_ai_layer() -> None:
    """jobs/render.py is the async render entry — same constraint as core/."""
    tree = ast.parse((SRC / "jobs" / "render.py").read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    hits = _forbidden_present(imports, FORBIDDEN_ON_RENDER_PATH)
    assert not hits, (
        f"jobs/render.py imports forbidden modules: {sorted(hits)}. "
        "Runtime render path must be provably AI-free."
    )


def test_designtime_mapper_only_reads_blank_templates() -> None:
    """mapper.py is allowed to touch AI, but must not import job/render or accept payload data."""
    src = (SRC / "designtime" / "mapper.py").read_text(encoding="utf-8")
    assert "jobs.render" not in src, "mapper.py must not reach into the render layer."
    assert "RenderJob" not in src, "mapper.py must not touch runtime job records."


@pytest.mark.parametrize("module_dir", ["core", "store"])
def test_no_stray_openai_or_anthropic_imports(module_dir: str) -> None:
    """Defence in depth: even indirect SDK imports must not appear in non-AI packages."""
    imports = _imports_of(SRC / module_dir)
    hits = _forbidden_present(imports, {"openai", "anthropic"})
    assert not hits, f"{module_dir}/ imports LLM SDKs: {sorted(hits)}"
