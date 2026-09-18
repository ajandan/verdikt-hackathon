"""_intake_from_extraction logs when it drops non-canonical keys.

This is the diagnostic that would have made QC-0060 obvious the first
time it happened. The extraction dict had 68 populated form-native
keys; ``_intake_from_extraction`` silently kept only the 1 that matched
an Intake canonical name and dropped the other 67 without a peep. This
test locks in the WARN so future schema drift surfaces immediately.

Uses ``capsys`` instead of ``caplog`` because structlog is configured to
write to stdout directly (not through stdlib logging) — pytest's
caplog captures stdlib handler output but not the raw stdout stream.
"""
from __future__ import annotations

from pap_pipeline.orchestrator import _intake_from_extraction

_EVENT = "intake_coerce_dropped_non_canonical_keys"


def test_dropped_non_canonical_keys_logged_at_warn(capsys):
    """Non-canonical keys land in the WARN log with a scannable sample."""
    raw = {
        # Canonical → coerced, no log
        "prescribed_drug": "Cardiovita",
        "prescriber_npi": "3167245083",
        # Form-native → dropped, must be logged
        "ins_uninsured": True,
        "rx_medication": "Cardiovita (valsartan)",
        "patient_first_name": "Maria",
        "patient_last_name": "Chen",
        "patient_zip": "95128",
    }
    intake = _intake_from_extraction(raw)

    assert intake.prescribed_drug == "Cardiovita"
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert _EVENT in combined, (
        f"expected structlog event '{_EVENT}' in output, got: {combined[:400]!r}"
    )
    # A few of the dropped names should appear in the sample list so
    # the operator can eyeball the mismatch from a log line alone.
    for expected in ("ins_uninsured", "rx_medication", "patient_zip"):
        assert expected in combined


def test_no_log_when_every_key_is_canonical(capsys):
    """Clean input → no diagnostic noise."""
    raw = {"prescribed_drug": "Cardiovita", "prescriber_npi": "1234567890"}
    _intake_from_extraction(raw)
    captured = capsys.readouterr()
    assert _EVENT not in (captured.out + captured.err)


def test_empty_extraction_dict_logs_nothing(capsys):
    """None / empty input is a legit early-boot case (extract stage
    returned nothing readable) — no WARN, just an empty Intake."""
    intake = _intake_from_extraction({})
    assert intake.prescribed_drug is None
    captured = capsys.readouterr()
    assert "intake_coerce" not in (captured.out + captured.err)
