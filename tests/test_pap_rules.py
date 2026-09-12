"""Rule-loader smoke tests.

Every YAML file under src/pap_pipeline/rules/ must parse into a valid Rule,
and every rule id must be unique across the directory. This catches drift
between the YAML authoring and the pydantic schema without needing pgvector
or the retriever NIMs to be running.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pap_pipeline.schemas import Rule

RULES_DIR = Path(__file__).parent.parent / "src" / "pap_pipeline" / "rules"


def _yaml_files() -> list[Path]:
    return sorted(RULES_DIR.glob("*.yaml"))


def test_rules_directory_is_not_empty() -> None:
    """Phase 1 authors four demo rules — an empty directory is almost certainly a bug."""
    assert _yaml_files(), (
        f"No .yaml rule files found under {RULES_DIR}. "
        "Phase 1 must ship at least the four demo rules."
    )


@pytest.mark.parametrize("path", _yaml_files(), ids=lambda p: p.name)
def test_rule_yaml_parses_into_schema(path: Path) -> None:
    """Each YAML rule file must validate cleanly against the Rule pydantic model."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rule = Rule.model_validate(data)
    assert rule.id, f"{path.name}: rule id is empty"
    assert rule.title.strip(), f"{path.name}: rule title is empty"
    assert rule.criterion.strip(), f"{path.name}: rule criterion is empty"


def test_rule_ids_are_unique() -> None:
    """Two rules with the same id would collide in citations and in pgvector."""
    ids = [Rule.model_validate(yaml.safe_load(p.read_text(encoding="utf-8"))).id for p in _yaml_files()]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"Duplicate rule ids: {sorted(duplicates)}"


def test_rule_filenames_match_ids() -> None:
    """Filename == rule id keeps grep-by-id working when reading citations."""
    for path in _yaml_files():
        rule = Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert path.stem == rule.id, (
            f"{path.name} declares id={rule.id!r}; filename must match the rule id."
        )
