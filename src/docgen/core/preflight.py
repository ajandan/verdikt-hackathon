"""Preflight validation — §8 of the design doc.

Moves failure from render time (compliance incident) to upload time (safe).
Every rejection is loud, cheap, and actionable.
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal
from zipfile import BadZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from jinja2 import Environment, TemplateSyntaxError, meta, nodes
from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_XML = "word/document.xml"

# Relationship types that trigger a fetch when the .docx is rendered / converted.
# Hyperlink is NOT in this set: it only navigates on user click, not on render.
_FETCHING_REL_SUFFIXES = (
    "/image",
    "/oleObject",
    "/audio",
    "/video",
    "/subDocument",
    "/externalLink",
    "/frame",
    "/media",
)

# Dunder attributes used in every known Jinja2 sandbox-escape proof-of-concept.
_DANGEROUS_ATTRS = frozenset(
    {
        "__class__",
        "__mro__",
        "__subclasses__",
        "__globals__",
        "__builtins__",
        "__base__",
        "__bases__",
        "__init__",
        "__dict__",
    }
)

_DEEP_CHAIN_THRESHOLD = 3


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class Finding:
    code: str
    severity: Severity
    message: str
    hint: str | None = None


@dataclass
class PreflightReport:
    outcome: Literal["accept", "accept-with-warnings", "reject"]
    findings: list[Finding] = field(default_factory=list)
    detected_variables: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "findings": [
                {"code": f.code, "severity": f.severity.value, "message": f.message, "hint": f.hint}
                for f in self.findings
            ],
            "detected_variables": self.detected_variables,
        }


def _extract_document_xml(docx_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        try:
            return zf.read(_DOC_XML).decode("utf-8")
        except KeyError as e:
            raise ValueError("word/document.xml missing — not a valid .docx") from e


def _extract_text_with_tags(document_xml: str) -> str:
    """Text-level flatten preserving jinja tags (which live in <w:t> runs)."""
    root = etree.fromstring(document_xml.encode("utf-8"))
    parts: list[str] = []
    for t in root.iter(f"{{{W_NS}}}t"):
        if t.text:
            parts.append(t.text)
    return "".join(parts)


def _detect_tracked_changes(document_xml: str) -> list[str]:
    root = etree.fromstring(document_xml.encode("utf-8"))
    hits = set()
    for tag in ("ins", "del", "moveFrom", "moveTo"):
        if root.find(f".//{{{W_NS}}}{tag}") is not None:
            hits.add(tag)
    return sorted(hits)


def _detect_external_asset_refs(docx_bytes: bytes) -> list[tuple[str, str, str]]:
    """Return (rels_file, rel_type_short, target) for every dangerous external ref.

    SSRF/asset-guard: DOCX .rels files can declare `TargetMode="External"` on image,
    oleObject, audio, video, subDocument, externalLink, frame, or media relationships.
    LibreOffice (used for PDF conversion) and Word both resolve these on open, which
    lets an attacker force the render worker to fetch attacker-controlled URLs or
    internal-metadata endpoints (169.254.169.254, localhost, private ranges).

    Hyperlinks are excluded — they only navigate on user click, not on render.
    """
    hits: list[tuple[str, str, str]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
            rels_names = [n for n in zf.namelist() if n.endswith(".rels")]
            for name in rels_names:
                try:
                    xml = zf.read(name)
                    root = etree.fromstring(xml)
                except (etree.XMLSyntaxError, KeyError):
                    continue
                for rel in root.iter(f"{{{_REL_NS}}}Relationship"):
                    if (rel.get("TargetMode") or "").lower() != "external":
                        continue
                    rtype = rel.get("Type") or ""
                    if any(rtype.endswith(sfx) for sfx in _FETCHING_REL_SUFFIXES):
                        short = rtype.rsplit("/", 1)[-1]
                        hits.append((name, short, rel.get("Target") or ""))
    except BadZipFile:
        pass
    return hits


def _detect_broken_field_codes(document_xml: str) -> bool:
    """Count fldChar begin/end pairs. Imbalance = broken."""
    root = etree.fromstring(document_xml.encode("utf-8"))
    begins = ends = 0
    for fc in root.iter(f"{{{W_NS}}}fldChar"):
        v = fc.get(f"{{{W_NS}}}fldCharType")
        if v == "begin":
            begins += 1
        elif v == "end":
            ends += 1
    return begins != ends


def _parse_jinja(rendered_text: str) -> tuple[nodes.Template | None, list[str], list[Finding]]:
    env = Environment()
    findings: list[Finding] = []
    try:
        ast = env.parse(rendered_text)
    except TemplateSyntaxError as e:
        findings.append(
            Finding(
                code="jinja.syntax",
                severity=Severity.ERROR,
                message=f"Jinja syntax error: {e.message} (line {e.lineno})",
                hint="Fix the tag in Word — mismatched {% %} or {{ }} is the usual cause.",
            )
        )
        return None, [], findings
    variables = sorted(meta.find_undeclared_variables(ast))
    return ast, variables, findings


def _expr_key(node: nodes.Node) -> str | None:
    """Stable dotted-path repr for Name/Getattr chains; None for unsupported shapes."""
    if isinstance(node, nodes.Name):
        return node.name
    if isinstance(node, nodes.Getattr):
        inner = _expr_key(node.node)
        return f"{inner}.{node.attr}" if inner else None
    return None


def _iter_target_names(target: nodes.Node) -> set[str]:
    if isinstance(target, nodes.Name):
        return {target.name}
    if isinstance(target, nodes.Tuple):
        return {n.name for n in target.items if isinstance(n, nodes.Name)}
    return set()


def _getattr_depth(node: nodes.Getattr) -> int:
    depth = 1
    inner: nodes.Node = node.node
    while isinstance(inner, nodes.Getattr):
        depth += 1
        inner = inner.node
    return depth


def _lint_jinja_ast(ast: nodes.Template) -> list[Finding]:
    findings: list[Finding] = []

    def walk(
        node: nodes.Node,
        loop_stack: list[set[str]],
        guard_exprs: set[str],
        in_default: bool,
        is_chain_root: bool = True,
    ) -> None:
        if isinstance(node, nodes.Include | nodes.Import | nodes.FromImport | nodes.Extends):
            kind = type(node).__name__
            findings.append(
                Finding(
                    code="jinja.dangerous_construct",
                    severity=Severity.ERROR,
                    message=f"Template uses `{kind}` — blocked in sandboxed rendering.",
                    hint="Remove the tag. Templates must be self-contained.",
                )
            )
            return

        if isinstance(node, nodes.For):
            names = _iter_target_names(node.target)
            iter_desc = _expr_key(node.iter) or "<expr>"
            iter_key = _expr_key(node.iter)
            has_guard = bool(node.else_) or (iter_key is not None and iter_key in guard_exprs)
            if not has_guard:
                findings.append(
                    Finding(
                        code="jinja.empty_loop_no_guard",
                        severity=Severity.WARNING,
                        message=f"Loop over `{iter_desc}` has no empty-collection guard.",
                        hint=(
                            f"Wrap in {{% if {iter_desc} %}}...{{% endif %}} "
                            "or add a {% else %} branch inside the {% for %} block."
                        ),
                    )
                )
            outer = set().union(*loop_stack) if loop_stack else set()
            overlap = names & outer
            if overlap:
                findings.append(
                    Finding(
                        code="jinja.loop_var_shadowed",
                        severity=Severity.WARNING,
                        message=f"Loop variable(s) {sorted(overlap)!r} shadow an enclosing loop target.",
                        hint="Rename the inner loop variable so the outer one stays reachable.",
                    )
                )
            walk(node.iter, loop_stack, guard_exprs, in_default)
            inner_stack = [*loop_stack, names]
            for c in node.body:
                walk(c, inner_stack, guard_exprs, in_default)
            for c in node.else_:
                walk(c, loop_stack, guard_exprs, in_default)
            return

        if isinstance(node, nodes.If):
            new_guards = set(guard_exprs)
            test_key = _expr_key(node.test)
            if test_key is not None:
                new_guards.add(test_key)
            walk(node.test, loop_stack, guard_exprs, in_default)
            for c in node.body:
                walk(c, loop_stack, new_guards, in_default)
            for c in node.elif_:
                walk(c, loop_stack, guard_exprs, in_default)
            for c in node.else_:
                walk(c, loop_stack, guard_exprs, in_default)
            return

        if isinstance(node, nodes.Filter):
            child_in_default = in_default or (node.name == "default")
            if node.node is not None:
                walk(node.node, loop_stack, guard_exprs, child_in_default)
            for a in node.args or []:
                walk(a, loop_stack, guard_exprs, in_default)
            for kw in node.kwargs or []:
                walk(kw, loop_stack, guard_exprs, in_default)
            if node.dyn_args is not None:
                walk(node.dyn_args, loop_stack, guard_exprs, in_default)
            if node.dyn_kwargs is not None:
                walk(node.dyn_kwargs, loop_stack, guard_exprs, in_default)
            return

        if isinstance(node, nodes.Getattr):
            if node.attr in _DANGEROUS_ATTRS:
                findings.append(
                    Finding(
                        code="jinja.dangerous_construct",
                        severity=Severity.ERROR,
                        message=f"Attribute `.{node.attr}` is a known sandbox-escape vector.",
                        hint="Remove the reference; only business-data attributes are permitted.",
                    )
                )
            depth = _getattr_depth(node)
            if is_chain_root and depth >= _DEEP_CHAIN_THRESHOLD and not in_default:
                chain = _expr_key(node) or "<expr>"
                findings.append(
                    Finding(
                        code="jinja.deep_attribute_chain",
                        severity=Severity.WARNING,
                        message=(
                            f"Deep attribute chain `{chain}` (depth {depth}) "
                            "with no `| default(...)` filter."
                        ),
                        hint=(
                            "Add `| default('')` at the end, or split into "
                            "intermediate variables to avoid NoneType errors "
                            "on missing intermediates."
                        ),
                    )
                )
            walk(node.node, loop_stack, guard_exprs, in_default, is_chain_root=False)
            return

        for c in node.iter_child_nodes():
            walk(c, loop_stack, guard_exprs, in_default)

    walk(ast, [], set(), False)
    return findings


def preflight(docx_bytes: bytes) -> PreflightReport:
    findings: list[Finding] = []

    try:
        Document(io.BytesIO(docx_bytes))
    except (PackageNotFoundError, BadZipFile) as e:
        return PreflightReport(
            outcome="reject",
            findings=[
                Finding(
                    code="opc.invalid",
                    severity=Severity.ERROR,
                    message=f"Not a valid Open XML package: {e}",
                    hint="Save the file as .docx from Word — .doc and corrupt archives are rejected.",
                )
            ],
        )

    try:
        document_xml = _extract_document_xml(docx_bytes)
    except ValueError as e:
        return PreflightReport(
            outcome="reject",
            findings=[Finding(code="opc.missing_part", severity=Severity.ERROR, message=str(e))],
        )

    tracked = _detect_tracked_changes(document_xml)
    if tracked:
        findings.append(
            Finding(
                code="docx.tracked_changes",
                severity=Severity.ERROR,
                message=f"Tracked-change residue found: {', '.join(tracked)}",
                hint="Accept or reject all tracked changes in Word before uploading.",
            )
        )

    if _detect_broken_field_codes(document_xml):
        findings.append(
            Finding(
                code="docx.broken_fields",
                severity=Severity.ERROR,
                message="Unbalanced Word field code (fldChar begin/end mismatch).",
                hint="Unlink or repair the field in Word (Ctrl+Shift+F9 on the affected field).",
            )
        )

    for rels_file, rtype, target in _detect_external_asset_refs(docx_bytes):
        findings.append(
            Finding(
                code="docx.external_asset",
                severity=Severity.ERROR,
                message=(
                    f"External {rtype} reference in {rels_file}: {target} — "
                    "LibreOffice/Word will fetch this URL on render (SSRF risk)."
                ),
                hint=(
                    "Embed the asset in the document (Insert → Picture → From File) "
                    "instead of linking to an external URL, then re-save."
                ),
            )
        )

    text = _extract_text_with_tags(document_xml)
    ast, variables, jinja_findings = _parse_jinja(text)
    findings.extend(jinja_findings)
    if ast is not None:
        findings.extend(_lint_jinja_ast(ast))

    errors = [f for f in findings if f.severity is Severity.ERROR]
    warnings = [f for f in findings if f.severity is Severity.WARNING]
    if errors:
        outcome: Literal["accept", "accept-with-warnings", "reject"] = "reject"
    elif warnings:
        outcome = "accept-with-warnings"
    else:
        outcome = "accept"

    return PreflightReport(outcome=outcome, findings=findings, detected_variables=variables)
