# Verdikt perf + hygiene sweep

- **When:** 2026-09-17T17:31:57.358103+00:00
- **HEAD:** `6b0ad1a`
- **Browser:** Chromium 145.0.7632.6 (headless, viewport 1440x900)
- **Server:** http://localhost:8100 (Docker `quire-hackathon-web-1`, assumed warm before run)
- **Method:** sequential, 500ms pause between pages, cold context per URL then warm re-hit

## Summary

| # | URL | TTFB (ms) | DCL (ms) | Load (ms) | Resources | Transfer (KB) | Verdict |
|---|---|---:|---:|---:|---:|---:|---|
| 1 | `/` | 37 | 665 | 799 | 3 | 632.2 | **NOTE** |
| 2 | `/engineering/about.html` | 17 | 137 | 331 | 3 | 48.7 | **OK** |
| 3 | `/engineering/domain.html` | 13 | 321 | 349 | 3 | 0.0 | **OK** |
| 4 | `/engineering/thesis.html` | 30 | 428 | 475 | 3 | 0.0 | **OK** |
| 5 | `/engineering/index.html` | 19 | 243 | 318 | 8 | 95.5 | **OK** |
| 6 | `/engineering/features.html` | 20 | 461 | 502 | 4 | 0.3 | **OK** |
| 7 | `/engineering/dashboard.html` | 13 | 161 | 223 | 5 | 10.0 | **OK** |
| 8 | `/engineering/code-review.html` | 14 | 240 | 287 | 4 | 0.3 | **OK** |
| 9 | `/engineering/security-review.html` | 15 | 256 | 284 | 4 | 0.3 | **OK** |
| 10 | `/engineering/production.html` | 16 | 207 | 245 | 4 | 0.3 | **OK** |
| 11 | `/engineering/hackathon.html` | 14 | 219 | 250 | 4 | 13.4 | **OK** |

## Warm vs cold delta

| URL | Cold Load (ms) | Warm Load (ms) | Δ Load (ms) | Cold KB | Warm KB | Δ KB |
|---|---:|---:|---:|---:|---:|---:|
| `/` | 799 | 490 | 309 | 632.2 | 0.0 | 632.2 |
| `/engineering/about.html` | 331 | 198 | 133 | 48.7 | 0.0 | 48.7 |
| `/engineering/domain.html` | 349 | 287 | 62 | 0.0 | 0.0 | 0.0 |
| `/engineering/thesis.html` | 475 | 280 | 195 | 0.0 | 0.0 | 0.0 |
| `/engineering/index.html` | 318 | 248 | 70 | 95.5 | 1.5 | 94.0 |
| `/engineering/features.html` | 502 | 285 | 217 | 0.3 | 0.3 | 0.0 |
| `/engineering/dashboard.html` | 223 | 229 | -6 | 10.0 | 0.6 | 9.4 |
| `/engineering/code-review.html` | 287 | 237 | 50 | 0.3 | 0.3 | 0.0 |
| `/engineering/security-review.html` | 284 | 207 | 77 | 0.3 | 0.3 | 0.0 |
| `/engineering/production.html` | 245 | 315 | -70 | 0.3 | 0.3 | 0.0 |
| `/engineering/hackathon.html` | 250 | 225 | 25 | 13.4 | 0.3 | 13.1 |

## Security headers (top-level document)

| URL | content-security-policy | strict-transport-security | x-frame-options | referrer-policy | permissions-policy |
|---|---|---|---|---|---|
| `/` | present | MISSING | present | present | present |
| `/engineering/about.html` | present | MISSING | present | present | present |
| `/engineering/domain.html` | present | MISSING | present | present | present |
| `/engineering/thesis.html` | present | MISSING | present | present | present |
| `/engineering/index.html` | present | MISSING | present | present | present |
| `/engineering/features.html` | present | MISSING | present | present | present |
| `/engineering/dashboard.html` | present | MISSING | present | present | present |
| `/engineering/code-review.html` | present | MISSING | present | present | present |
| `/engineering/security-review.html` | present | MISSING | present | present | present |
| `/engineering/production.html` | present | MISSING | present | present | present |
| `/engineering/hackathon.html` | present | MISSING | present | present | present |

## Accessibility smoke

| URL | <img> | missing alt | <h1> count | <html lang> |
|---|---:|---:|---:|---|
| `/` | 9 | 0 | 1 | en |
| `/engineering/about.html` | 0 | 0 | 1 | en |
| `/engineering/domain.html` | 0 | 0 | 1 | en |
| `/engineering/thesis.html` | 0 | 0 | 1 | en |
| `/engineering/index.html` | 0 | 0 | 1 | en |
| `/engineering/features.html` | 0 | 0 | 1 | en |
| `/engineering/dashboard.html` | 0 | 0 | 1 | en |
| `/engineering/code-review.html` | 0 | 0 | 1 | en |
| `/engineering/security-review.html` | 0 | 0 | 1 | en |
| `/engineering/production.html` | 0 | 0 | 1 | en |
| `/engineering/hackathon.html` | 0 | 0 | 1 | en |

## Findings

### BLOCKER
- (none)

### NOTE
- `/` — NOTE transfer=632.2KB > 500KB

### PRAISE
- `/engineering/about.html` — PRAISE fast+lean load=331ms transfer=48.7KB
- `/engineering/domain.html` — PRAISE fast+lean load=349ms transfer=0.0KB
- `/engineering/index.html` — PRAISE fast+lean load=318ms transfer=95.5KB
- `/engineering/dashboard.html` — PRAISE fast+lean load=223ms transfer=10.0KB
- `/engineering/code-review.html` — PRAISE fast+lean load=287ms transfer=0.3KB
- `/engineering/security-review.html` — PRAISE fast+lean load=284ms transfer=0.3KB
- `/engineering/production.html` — PRAISE fast+lean load=245ms transfer=0.3KB
- `/engineering/hackathon.html` — PRAISE fast+lean load=250ms transfer=13.4KB

## Top 3 recommendations

1. **Trim payload on `/`** (cold 632.2 KB). Check for unused JS, uncompressed JSON, and full-size PNGs that could be optimized or lazy-loaded.

## Raw data

- `reports/perf-data.json` — full per-URL numbers, headers, a11y
- `reports/perf-shots/*.png` — one full-page screenshot per URL
