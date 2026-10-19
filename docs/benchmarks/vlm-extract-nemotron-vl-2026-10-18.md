# Intake extraction — MiniCPM-V-4_5 vs Nemotron-3.5-Super-VL

**Date:** 2026-10-18
**Question:** should `nvidia/Nemotron-3.5-Super-VL`, newly on Nebius Token Factory serverless, replace `openbmb/MiniCPM-V-4_5` as the Extract-stage reader?
**Fixtures:** the two intake fixtures from the [2026-09-16 comparison](vlm-label-comparison-2026-09-16.md): the Alderwood filled sample (synthetic, digitally rendered, 19 slots) and Form B (real raster scan, typed and handwritten values, 19 slots).

## Method

- Production path: `pap_pipeline.stages.extract.extract()`, same prompt and schema for both models, `temperature=0`, thinking disabled (`enable_thinking: False`), as in production.
- Same per-slot scoring as September: normalised exact match against hand-written ground truth.
- 3 runs per model per fixture. Token counts are the `usage` blocks returned by Token Factory, summed over the stage's calls.
- MiniCPM-V's scores differ from September's (78.9% / 84.2%) because the extract prompt has changed since; only same-day numbers are compared here.

## Results

All three runs of each pair were identical, so one row each.

| Model | Synthetic | Form B scan | Time (synthetic / scan) | Tokens in / out (synthetic / scan) |
|---|---|---|---|---|
| `openbmb/MiniCPM-V-4_5` (current) | 14/19 (73.7%) | 17/19 (89.5%) | 3.4–5.5s / 21.8–24.4s | 2,796 / 159 · 5,783 / 568 |
| `nvidia/Nemotron-3.5-Super-VL` | **17/19 (89.5%)** | **19/19 (100%)** | 4.9–5.9s / 23.0–27.6s | 5,684 / 383 · 16,496 / 576 |

### What each model got wrong

**MiniCPM-V-4_5**
- Form B: `date_of_birth` read as `1940-07-07`; truth `1990-07-07`. Same failure class as September (`1949-07-07`): a wrong decade on a healthcare form.
- Form B: invented `country_of_residence = "United States"` where the form is blank.
- Synthetic: missed `prescribed_drug`, `mailing_state` and `has_denial_letter`; `patient_name` as `Chen Mei` (initial dropped); invented `country = "Erie"`.

**Nemotron-3.5-Super-VL**
- Synthetic: `prescribed_drug` read as `Adliruzumab`; truth `Aldurizumab`. Four edits apart, outside the drug matcher's scan tolerance, so the drug rule fails and the case does not auto-approve.
- Synthetic: `patient_name` as `Chen Mei L`; the right name in last-first order, scored wrong by the strict matcher.
- Nothing invented, nothing missed.

## Reading

- Nemotron VL is more accurate on both fixtures and made no hallucinated values. MiniCPM-V's date-of-birth error repeats from September.
- Wall-clock time is about the same; the stage is dominated by per-page calls, not model speed.
- Nemotron VL uses 2–3x the input tokens for the same pages (larger image tokenisation). Cost per case rises accordingly; this run did not price it.
- Still two fixtures. The September note stands: never trust a small benchmark alone.

## Not covered

- The design-time PDF labeler (Form A, 104 fields) was not re-run; it uses its own model setting, and its September accuracy was scored by eye.

## Reproducing

`.tmp/nebius/bench_nemotron_vl.py` (gitignored, with the fixtures); raw output in `.tmp/nebius/bench_nemotron_vl.json`. Requires `DOCGEN_AI_API_KEY`.
