# Beat visuals catalog — Verdikt 2:59 submission video

Written 2026-09-15 to replace the five black placeholder cards in the current
edit (beats 1, 2b, 3, 4, 5). Beats 2a, 2c, 2d already have Playwright captures
of the live app and are out of scope here.

Every beat below has a **primary** pick that is either an existing repo asset or
a live-UI screenshot we can hold as a static frame. Alternates are provided in
case the primary reads too dense at 2 s of screen time.

All beat visuals in `assets/video/screenshots/beat{N}_{primary|alt1|alt2}.png`
are 1920×1080 RGB PNGs, ready to drop into the edit.

---

## Existing repo assets found

Landing-page section captures (produced during the marketing-site build, live at
[http://localhost:8100/](http://localhost:8100/), stored in `.tmp/` — cream
background, editorial serif type, matches the Verdikt design system):

| Path | Description |
| --- | --- |
| `.tmp/section-hero.png` | Full landing hero — "Many inputs. One verdict." + AI/deterministic split copy + inline pipeline detail card. |
| `.tmp/section-hero-frame.png` | Cropped hero card, no chrome — clean editorial title. |
| `.tmp/hero-prominent.png` | Verdikt navbar + hero + APPROVED artifact card (looks like a physical letter). |
| `.tmp/section-wedge.png` | Dark section: "Agents can generate documents. They can't be trusted to author them." Three supply cards (AI, Humans, Verdikt). |
| `.tmp/section-lifecycle.png` | 8-stage lifecycle rail: Intake → Extract → Retrieve → Reason → Merge → Deliver → Audit → Rollback. Cream. |
| `.tmp/section-compare.png` | Feature comparison table — where the market ends and Verdikt begins. |
| `.tmp/section-governance.png` | Dark section: "The decision ledger is the point." Includes a mocked audit table with cross-tenant approve / deny / missing_info rows. |
| `.tmp/section-callout.png` | "Healthcare. Banking. Any workflow where the letter that leaves the building is the audit artifact." + four vertical cards. |
| `.tmp/section-cta.png` | Dark closing CTA: "Bring the AI to the case. Keep it out of the reply." + Sign in button. |
| `.tmp/section-console.png` | Marketing screenshot of the console/inbox. |
| `.tmp/intake-icons.png` | Fan-in diagram: Fax / Postal mail / Web form / Email / REST / MCP → Intake bus. |
| `.tmp/rail.png` | **The** architecture diagram: AI Zone (Ingest / Extract / Decide) vs Deterministic Zone (Deliver / Remember). Cream. |
| `.tmp/artifact-package.png` | Two-column artifact panel: Reply package (letter mock) + Verdict JSON (dark terminal). |
| `.tmp/hackathon-desktop.png` | Full Nebius × NVIDIA submission block with four cards (Reason / Retrieve / Extract / Hosted). |
| `.tmp/carousel-slide-{1,2,3,5}.png` | Marketing carousel frames — smaller, secondary. |
| `.tmp/mobile-outcome.png`, `.tmp/mobile-cmp.png` | Mobile responsive captures — not useful for 16:9 video. |

Live-console screenshots baked into the app itself (also served as static assets
at `/static/console-*.png`; 2880×1800 source resolution):

| Path | Description |
| --- | --- |
| `src/docgen/ui/static/console-dashboard.png` | Dashboard KPIs (templates / drafts / renders / warnings). |
| `src/docgen/ui/static/console-inbox.png` | Case queue table, mixed approved (green) + rejected (red) + pending rows. |
| `src/docgen/ui/static/console-pipeline.png` | Pipeline detail — Extract stage expanded, showing structured intake slots. |
| `src/docgen/ui/static/console-programs.png` | Programs table across two tenants (Alderwood Specialty Pharmacy, Meridian Health Foundation, default). |
| `src/docgen/ui/static/console-rules.png` | Rules library grouped by tenant, with kind chips (clinical / financial / insurance / residency). |
| `src/docgen/ui/static/console-rule-detail.png` | Single rule editor with citation, criterion, exception fields. |
| `src/docgen/ui/static/console-renders.png` | Renders list with success / failed / queued state chips. |
| `src/docgen/ui/static/console-tenants.png` | Tenants list with routing / MDM / quotas / keys actions. |
| `src/docgen/ui/static/console-audit.png` | Audit trail for a single case — pipeline_started → verdict_rendered → reply_draft_skipped → approved. |

Live captures produced today (1920×1080, admin-authenticated where required):

| Path | Description |
| --- | --- |
| `assets/video/screenshots/route_marketing_landing.png` | Marketing top-of-page hero at 1920×1080. |
| `assets/video/screenshots/route_dashboard.png` | Live dashboard with 317 templates / 81 drafts / 51 renders. |
| `assets/video/screenshots/route_inbox_list.png` | Live case queue at HEAD. |
| `assets/video/screenshots/route_inbox_pipeline.png` | Live pipeline detail for a real approved case. |
| `assets/video/screenshots/route_programs.png` | Live Programs table with the three tenants. |
| `assets/video/screenshots/route_rules.png` | Live Rules library. |
| `assets/video/screenshots/route_renders.png` | Live Renders with real failed_prior_attempt + ok mix. |
| `assets/video/screenshots/route_tenants.png` | Live Tenants list. |

Verdikt logo (fan-in ribbons converging on a target dot — the pipeline mark):

| Path | Description |
| --- | --- |
| `docs/logo/verdikt-mark.svg` | Full-colour SVG mark. |
| `docs/logo/verdikt-mark-mono.svg` | Monochrome variant. |
| `docs/logo/verdikt-mark-{16,32,64,512,1024}.png` | Rasterised marks; 1024 is the sizing to use for anything on-screen. |
| `docs/logo/verdikt-favicon.svg` | Favicon variant. |
| `src/docgen/ui/static/og-verdikt.png`, `reports/og-verdikt.png` | Open-Graph share card with the mark composited on a title. |

Engineering static hub (public, `/engineering/*.html`) — hero copy is strong,
but the data cards below the fold don't hydrate in a headless capture. Not
first-pick for beats but usable if we script an interactive capture:
`reports/{index,about,features,production,security-review,code-review,hackathon}.html`.

---

## Beat-by-beat recommendations

### Beat 1 — the manifesto open (~16 s)

> "In regulated document workflows, the letter that leaves the building is the
> artifact under audit. It has to reproduce bit-for-bit, defensibly, years
> later. Most AI products can't guarantee that. We built one that can."

| Beat | Recommended visual (primary) | Route / asset path | Alternate 1 | Alternate 2 | Notes |
| --- | --- | --- | --- | --- | --- |
| 1 | `.tmp/section-wedge.png` (dark card: "Agents can generate documents. They can't be trusted to author them." + three supply cards) | `assets/video/screenshots/beat1_primary.png` (padded 1920×1080, ink background) | `.tmp/section-hero-frame.png` — clean marketing hero card, "Many inputs. One verdict." → `beat1_alt1.png` | `.tmp/hero-prominent.png` — hero + APPROVED artifact card, sets up the "letter that leaves the building" phrase → `beat1_alt2.png` | The wedge headline is the exact thesis of the VO — the contrast between what agents can generate and what they can be trusted to author lands the "most AI products can't guarantee that" line. Static hold shot; no interaction needed. |

### Beat 2b — the three stages (~26 s)

> "Three stages run, one NVIDIA model per stage. Extraction reads every
> attachment into a structured intake. Retrieval surfaces the program's
> eligibility rules from a versioned knowledge base. Reasoning evaluates the
> intake against those rules and produces a structured verdict — approve, deny,
> or missing information — with citations back to the rules that fired."

| Beat | Recommended visual (primary) | Route / asset path | Alternate 1 | Alternate 2 | Notes |
| --- | --- | --- | --- | --- | --- |
| 2b | `.tmp/intake-icons.png` — six channels fanning into the Intake bus | `assets/video/screenshots/beat2b_primary.png` | `.tmp/section-lifecycle.png` — 8-stage rail (Intake / Extract / Retrieve / Reason … all named) → `beat2b_alt1.png` | `src/docgen/ui/static/console-pipeline.png` — live Extract stage of a real case, structured intake slots visible → `beat2b_alt2.png` | Extract / Retrieve / Reason is easier to *hear* than *see*; a fan-in diagram gives the viewer's eye something to track for 6 s while the VO names each stage. Static hold. Alt 1 is a stronger choice if the edit already used intake-icons in beat 2a. |

### Beat 3 — architecture beat, the load-bearing one (~52 s)

> "The industry's racing to solve this with agents. We took the opposite bet.
> AI-in, deterministic-out. We call it Verdikt. Three NVIDIA models, one per
> stage. Multimodal extraction reads the intake. NeMo Retriever surfaces the
> rules. Nemotron reasons to a structured decision. Then AI stops. The reply is
> a byte-identical merge of human-approved templates with that decision —
> outcome letter plus every static document the applicant needs, composed as
> the operator declared. No model runs at render time. No model can. Same
> input, same package, forever. This is where regulated verticals are
> converging. We built it now."

| Beat | Recommended visual (primary) | Route / asset path | Alternate 1 | Alternate 2 | Notes |
| --- | --- | --- | --- | --- | --- |
| 3 | `.tmp/rail.png` — AI Zone (Ingest / Extract / Decide) ← seam → Deterministic Zone (Deliver / Remember) | `assets/video/screenshots/beat3_primary.png` | `.tmp/artifact-package.png` — Reply package (letter) + Verdict JSON (dark), the two-artifact seam → `beat3_alt1.png` | `.tmp/section-lifecycle.png` — 8-stage lifecycle, useful if we want the third-model beat and the rollback beat to share one visual → `beat3_alt2.png` | This is the beat that has to be crystal clear about the AI/deterministic seam — `rail.png` labels it in words above the diagram and shows AI stopping at Reason. Static hold; ideally cut the shot in at "AI-in, deterministic-out" and let it read for 8-10 s. |

### Beat 4 — second manufacturer, opposite verdict (~17 s)

> "The pipeline is the product, not the program. A second manufacturer, a
> stricter income cap. Same email, same code — opposite verdict. New programs
> onboard as data, not builds."

| Beat | Recommended visual (primary) | Route / asset path | Alternate 1 | Alternate 2 | Notes |
| --- | --- | --- | --- | --- | --- |
| 4 | `src/docgen/ui/static/console-inbox.png` — case queue with green **approved** and red **rejected** rows sharing the same page | `assets/video/screenshots/beat4_primary.png` | `src/docgen/ui/static/console-programs.png` — three programs (Alderwood, Meridian, default) side-by-side, same code path → `beat4_alt1.png` | `.tmp/section-compare.png` — comparison table, "A pipeline product on the outside, a governance product on the inside" → `beat4_alt2.png` | The inbox row list is the single most direct visualisation of "same email, opposite verdict" — approved and rejected chips co-exist in one queue. If we want to double-anchor "new programs onboard as data" the alt1 programs table is the cleaner tell. Static hold. |

### Beat 5 — close + CTA (~18 s)

> "Verdikt. Three NVIDIA models on Nebius Token Factory, one deterministic
> reply path. MIT-licensed, fully open source. Repo, live demo, and the same
> engineering dashboards the team uses — all linked below."

| Beat | Recommended visual (primary) | Route / asset path | Alternate 1 | Alternate 2 | Notes |
| --- | --- | --- | --- | --- | --- |
| 5 | `.tmp/section-cta.png` — dark closing card, "Bring the AI to the case. Keep it out of the reply." + Sign in button | `assets/video/screenshots/beat5_primary.png` | `docs/logo/verdikt-mark-1024.png` — the Verdikt fan-in mark on cream, brand close → `beat5_alt1.png` | `.tmp/hackathon-desktop.png` — Nebius × NVIDIA submission block, four model cards → `beat5_alt2.png` | The dark CTA is the same card the site closes with, so it doubles as brand recall. If the video ends on a lower-third with the repo URL, alt1 (bare logo) may be a cleaner backdrop. Static hold. Alt 2 is best if the judge audience wants proof of stack callouts before the CTA. |

---

## What's in the box (final beat visuals)

| File | Source | Size |
| --- | --- | --- |
| `beat1_primary.png` | `.tmp/section-wedge.png`, padded on ink | 1920×1080 |
| `beat1_alt1.png` | `.tmp/section-hero-frame.png`, padded on ink | 1920×1080 |
| `beat1_alt2.png` | `.tmp/hero-prominent.png`, padded on ink | 1920×1080 |
| `beat2b_primary.png` | `.tmp/intake-icons.png`, padded on cream | 1920×1080 |
| `beat2b_alt1.png` | `.tmp/section-lifecycle.png`, padded on cream | 1920×1080 |
| `beat2b_alt2.png` | `console-pipeline.png`, padded on cream | 1920×1080 |
| `beat3_primary.png` | `.tmp/rail.png`, padded on cream | 1920×1080 |
| `beat3_alt1.png` | `.tmp/artifact-package.png`, padded on cream | 1920×1080 |
| `beat3_alt2.png` | `.tmp/section-lifecycle.png`, padded on cream | 1920×1080 |
| `beat4_primary.png` | `console-inbox.png`, padded on cream | 1920×1080 |
| `beat4_alt1.png` | `console-programs.png`, padded on cream | 1920×1080 |
| `beat4_alt2.png` | `.tmp/section-compare.png`, padded on cream | 1920×1080 |
| `beat5_primary.png` | `.tmp/section-cta.png`, padded on ink | 1920×1080 |
| `beat5_alt1.png` | `verdikt-mark-1024.png` (logo), padded on cream | 1920×1080 |
| `beat5_alt2.png` | `.tmp/hackathon-desktop.png`, padded on cream | 1920×1080 |

Regeneration script: `tmp/resize_beats.py` (Pillow, `contain` mode). Rerun after
any source refresh with `python tmp/resize_beats.py`.

## One suggestion that isn't in the file list

If the edit has room for a subtle brand stinger between beats — e.g. between
beat 4 and beat 5 — the Verdikt mark on a cream card (`beat5_alt1.png`) with a
short cross-fade would work well as a two-beat transition. It's the only visual
in the set that reads as pure brand.
