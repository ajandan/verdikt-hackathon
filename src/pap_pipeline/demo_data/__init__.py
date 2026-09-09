"""Sample PAP application packets used for demo runs and tests.

Right now this holds the canonical demo intake — the exact patient the video
is built around, expressed as a validated Intake so tests can exercise the
retrieve + reason stages without the extract stage having to run. Realistic
attachment fixtures (pay stubs, insurance cards, prescriptions, denial
letters) that drive the extract stage end-to-end will land here alongside
the intake in Phase 2 when nv-ingest is wired up.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from pap_pipeline.schemas import Intake

_DATA_DIR = Path(__file__).parent


def load_canonical_intake() -> Intake:
    """Return the demo patient as a validated Intake object.

    Sourced from `canonical_intake.yaml` — see that file for why the numbers
    are what they are (the income figure is pinned to make the future
    OncoBridge cameo flip approve→deny on the income rule alone).
    """
    data = yaml.safe_load((_DATA_DIR / "canonical_intake.yaml").read_text(encoding="utf-8"))
    return Intake.model_validate(data)


__all__ = ["load_canonical_intake"]
