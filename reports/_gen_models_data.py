"""Build reports/models-data.json for the Tests > Models page.

Reads the raw benchmark output under .tmp/nebius/ (gitignored, with the
fixtures) and writes one JSON file the static page renders. Nothing here
calls a model: rerun the harnesses first, then this.

    python .tmp/nebius/bench_nemotron_vl.py      # extraction, 2026-10-18
    python reports/_gen_models_data.py

Form B is a real scanned form filled in by hand. Its field values never
reach the JSON: only field names, error types and counts. The Alderwood
fixture is fictional, so its values are kept. Labeler accuracy from
September was scored by eye and is marked as an estimate.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pap_pipeline.verify import _values_agree  # noqa: E402

NB = ROOT / ".tmp" / "nebius"
OUT = ROOT / "reports" / "models-data.json"

PRIMARY, VERIFIER = "openbmb/MiniCPM-V-4_5", "nvidia/Nemotron-3.5-Super-VL"
FIXTURES = {
    "synthetic": {"label": "Alderwood sample", "kind": "Synthetic, digitally rendered",
                  "public_values": True},
    "scan": {"label": "Form B", "kind": "Real raster scan, typed and handwritten",
             "public_values": False},
}
# Hosts and licences as recorded in docs/benchmarks/vlm-label-comparison-2026-09-16.md.
SEP_MODELS = {
    "openbmb/MiniCPM-V-4_5": "Apache-2.0",
    "google/gemma-3-27b-it": "Gemma licence",
    "zai-org/GLM-5.3-Flash": "Z.ai licence",
    "moonshotai/Kimi-K3": "Kimi K3 licence",
    "moonshotai/Kimi-K2.6": "MIT",
    "moonshotai/Kimi-K2.7-Code": "MIT",
    "MiniMaxAI/MiniMax-M3": "MiniMax licence",
}
# Form A labeler, 2026-09-16. Labeled count and time are measured; correct
# labels were estimated by eye against the annotated page renders.
LABELER_EST_CORRECT = {
    "openbmb/MiniCPM-V-4_5": 60,
    "moonshotai/Kimi-K2.7-Code": 62,
    "mistralai/pixtral-12b-2409": 63,
}
LABELER_HOST = {
    "openbmb/MiniCPM-V-4_5": "Nebius Token Factory",
    "moonshotai/Kimi-K2.7-Code": "Nebius Token Factory",
    "mistralai/pixtral-12b-2409": "Mistral API (off-Nebius)",
}


def _load(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"[models] missing {path.relative_to(ROOT)} -- run the benchmark harness first")
    return json.loads(path.read_text(encoding="utf-8"))


def _preds(per_field: list[dict]) -> dict:
    return {r["field"]: r["got"] for r in per_field}


def extraction_latest() -> tuple[dict, list[dict]]:
    raw = _load(NB / "bench_nemotron_vl.json")
    results, errors, preds = [], [], {}
    for key, runs in raw.items():
        fixture, model = key.split("|", 1)
        t = runs[0]["totals"]
        same = all(r["totals"] == t for r in runs)
        times = [r["elapsed_s"] for r in runs]
        results.append({
            "fixture": fixture, "model": model, "nvidia": model.startswith("nvidia/"),
            "match": t["match"], "total": t["total"], "pct": t["accuracy_pct"],
            "wrong": t["wrong"], "miss": t["miss"], "extra": t["extra"],
            "runs": len(runs), "identical_runs": same,
            "time_min_s": min(times), "time_max_s": max(times),
            "tokens_in": runs[0]["tokens"]["in"], "tokens_out": runs[0]["tokens"]["out"],
        })
        preds[(fixture, model)] = _preds(runs[0]["per_field"])
        public = FIXTURES[fixture]["public_values"]
        for r in runs[0]["per_field"]:
            if r["status"] == "MATCH":
                continue
            row = {"fixture": fixture, "model": model, "field": r["field"], "status": r["status"]}
            if public:
                row["expected"], row["got"] = r["expected"], r["got"]
            errors.append(row)

    # Replay dual_extract's rule: flag a field only when both readers gave a
    # value and the values disagree. A flag is "caught" when the primary was
    # wrong on that field, "false alarm" when the primary was right.
    sim = []
    for fixture in FIXTURES:
        p, v = preds[(fixture, PRIMARY)], preds[(fixture, VERIFIER)]
        primary_wrong = {e["field"] for e in errors
                         if e["fixture"] == fixture and e["model"] == PRIMARY and e["status"] == "WRONG"}
        flags = []
        for field in sorted(set(p) | set(v)):
            a, b = p.get(field), v.get(field)
            if a is None or b is None or _values_agree(a, b):
                continue
            flags.append({"field": field, "outcome": "caught" if field in primary_wrong else "false alarm"})
        n_err = sum(1 for e in errors if e["fixture"] == fixture and e["model"] == PRIMARY)
        sim.append({"fixture": fixture, "flags": flags, "primary_errors": n_err})

    return {
        "date": "2026-10-18",
        "doc": "docs/benchmarks/vlm-extract-nemotron-vl-2026-10-18.md",
        "method": ("Production extract() path, same prompt and schema for both models, temperature 0, "
                   "thinking disabled. Per-slot normalised exact match against hand-written ground truth. "
                   "Tokens are Token Factory usage, summed over the stage's calls."),
        "fixtures": [{"id": k, "label": f["label"], "kind": f["kind"], "slots": 19}
                     for k, f in FIXTURES.items()],
        "results": results,
        "errors": errors,
        "verifier_replay": sim,
    }, results


def extraction_sweep() -> dict:
    syn = _load(NB / "compare_results.json")["runs"]
    scan = _load(NB / "lly" / "compare_lly_results.json")["runs"]
    rows = []
    for model, lic in SEP_MODELS.items():
        s, f = syn.get(model, {}), scan.get(model, {})
        rows.append({
            "model": model, "licence": lic, "nvidia": model.startswith("nvidia/"),
            "synthetic_pct": s.get("totals", {}).get("accuracy_pct"),
            "scan_pct": f.get("totals", {}).get("accuracy_pct"),
            "synthetic_s": s.get("elapsed_s"), "scan_s": f.get("elapsed_s"),
        })
    return {
        "date": "2026-09-16",
        "doc": "docs/benchmarks/vlm-label-comparison-2026-09-16.md",
        "note": ("Single run per model on the extract prompt as it stood in September; it has changed since, "
                 "so MiniCPM-V's scores here differ from the October run."),
        "results": rows,
    }


def labeler() -> dict:
    raw = _load(NB / "tev" / "bench_3way.json")
    rows = [{
        "model": m, "host": LABELER_HOST.get(m, ""),
        "labeled": r["kept"], "est_correct": LABELER_EST_CORRECT.get(m),
        "time_s": round(r["time_s"], 1),
    } for m, r in raw.items()]
    return {
        "date": "2026-09-16",
        "doc": "docs/benchmarks/vlm-label-comparison-2026-09-16.md",
        "fixture": "Form A, flat PDF, 104 fields",
        "note": "Labeled count and time are measured. Correct labels were estimated by eye.",
        "results": rows,
        "validated": {"before": 24, "after": 98, "total": 104,
                      "before_mode": "text-only (auto)", "after_mode": "vision, chunked and cropped",
                      "how": "End to end through the portal, labels checked by hand"},
    }


def main() -> None:
    latest, _ = extraction_latest()
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                         cwd=ROOT).stdout.strip()
    data = {
        "product": "Verdikt",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "head_sha": sha,
        "extraction_latest": latest,
        "extraction_sweep": extraction_sweep(),
        "labeler": labeler(),
        "not_covered": [
            "Triage (Nemotron Nano): no accuracy benchmark yet; its rule-based fallback "
            "is covered by pytest.",
            "Reason (Nemotron Super): no accuracy benchmark yet; verdicts are checked case "
            "by case by the deterministic verifier and the human review gate.",
            "Rule-to-formula translation (Nemotron Super): every formula is reviewed by the "
            "rule author before it is saved; no batch benchmark.",
            "Nemotron-3.5-Super-VL as the PDF labeler: not run on Form A.",
        ],
    }
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[models] wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
