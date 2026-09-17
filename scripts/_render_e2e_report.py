"""One-shot: re-render the E2E markdown report from the persisted JSON.

Use when tweaking the report template without paying another Nebius call.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from browser_e2e import RunData, PhaseResult, write_md  # noqa: E402

data_json = json.loads((REPO / "reports" / "browser-e2e-data.json").read_text(encoding="utf-8"))

# Rehydrate dataclass instances.
phases = [PhaseResult(**p) for p in data_json.pop("phases")]
run = RunData(**data_json)
run.phases = phases
write_md(run)
print("wrote reports/browser-e2e-report.md")
