"""Design-time step 3 (§6): AI proposes Jinja tags + typed schema for a draft version.

Reads the *blank* template from blob storage, extracts its text, hands it to the
tenant's configured provider (Tenant.ai_provider_config overlays global env). Never
called at render time.
"""
from __future__ import annotations

import uuid

from jinja2 import Environment, meta, nodes
from sqlalchemy.ext.asyncio import AsyncSession

from docgen.config import get_settings
from docgen.core.preflight import _extract_document_xml, _extract_text_with_tags, _parse_jinja
from docgen.core.schema_infer import FieldSpec
from docgen.providers import get_provider
from docgen.providers.base import MappingProposal, Suggestion
from docgen.store.blob import get_object
from docgen.store.models import Template, TemplateVersion, Tenant


async def propose_mapping_for_version(session: AsyncSession, version_id: uuid.UUID) -> MappingProposal:
    tv = await session.get(TemplateVersion, version_id)
    if tv is None:
        raise ValueError(f"Template version {version_id} not found")
    if tv.is_frozen:
        raise ValueError("Version is frozen — start a new draft version to re-map.")

    template = await session.get(Template, tv.template_id)
    tenant_config: dict | None = None
    if template is not None:
        tenant = await session.get(Tenant, template.tenant_id)
        if tenant is not None and tenant.ai_provider_config:
            tenant_config = dict(tenant.ai_provider_config)

    settings = get_settings()
    docx_bytes = get_object(settings.s3_bucket_templates, tv.blob_key)
    document_xml = _extract_document_xml(docx_bytes)
    text = _extract_text_with_tags(document_xml)

    provider = get_provider(tenant_config)
    proposal = await provider.propose_mapping(text)
    _backfill_orphan_suggestions(proposal, text)
    return proposal


def _backfill_orphan_suggestions(proposal: MappingProposal, template_text: str) -> None:
    """Append stub Suggestion rows for jinja variables the AI didn't cover.

    Preflight is the authoritative list of variables in the template. Any variable
    detected by preflight but not referenced in the AI's proposed jinja gets a
    default row so the user can complete the mapping in the review UI. Without
    this, the freeze-time schema-contract check rejects the version with no
    in-UI path to recover.
    """
    ast, detected, _findings = _parse_jinja(template_text)
    if not detected:
        return

    env = Environment()
    covered: set[str] = set()
    for s in proposal.suggestions:
        try:
            covered |= meta.find_undeclared_variables(env.parse(s.jinja))
        except Exception:
            continue

    inferred = _infer_variable_types(ast, detected)

    for var in sorted(set(detected) - covered):
        jinja_tag = "{{ " + var + " }}"
        proposal.suggestions.append(
            Suggestion(
                placeholder=jinja_tag,  # identity replacement — tag is already in the docx
                jinja=jinja_tag,
                field=FieldSpec(
                    name=var,
                    type=inferred.get(var, "string"),
                    required=True,
                ),
                source="orphan_backfill",
            )
        )


def _infer_variable_types(ast: nodes.Template | None, detected: list[str]) -> dict[str, str]:
    """Guess field types by walking the Jinja AST usage of each free variable.

    Rules (conservative — only classify when the signal is unambiguous):
      * Only in `{% if X %}` / `{% elif X %}` test → boolean
      * Only in `{% for _ in X %}` iterable → list
      * Anything else (any use in `{{ ... }}`) → string default (not returned)

    A variable that appears in both contexts (e.g. `{% if x %}...{{ x }}...`)
    stays a string — the user is displaying its value, so it isn't a plain flag.

    Solves the checkbox footgun: Salesforce `Proof_X__c` fields only appear in
    `{% if %}` guards, so we default them to `boolean` and the runtime string
    `"false"` gets coerced to False instead of being truthy.
    """
    if ast is None:
        return {}

    output_vars: set[str] = set()
    condition_vars: set[str] = set()
    iter_vars: set[str] = set()

    def top_names(node: nodes.Node) -> set[str]:
        # find_all excludes self, so a bare Name node needs its own entry.
        names = {n.name for n in node.find_all(nodes.Name)}
        if isinstance(node, nodes.Name):
            names.add(node.name)
        return names

    def walk(node: nodes.Node) -> None:
        if isinstance(node, nodes.Output):
            for c in node.nodes:
                output_vars.update(top_names(c))
        elif isinstance(node, nodes.If):
            condition_vars.update(top_names(node.test))
            for c in node.body:
                walk(c)
            for c in node.else_:
                walk(c)
        elif isinstance(node, nodes.For):
            iter_vars.update(top_names(node.iter))
            for c in node.body:
                walk(c)
            for c in getattr(node, "else_", None) or []:
                walk(c)
        else:
            for c in node.iter_child_nodes():
                walk(c)

    walk(ast)

    types: dict[str, str] = {}
    for v in detected:
        if v in condition_vars and v not in output_vars and v not in iter_vars:
            types[v] = "boolean"
        elif v in iter_vars and v not in output_vars and v not in condition_vars:
            types[v] = "list"
    return types
