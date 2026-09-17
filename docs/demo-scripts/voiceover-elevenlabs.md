# ElevenLabs-ready voiceover script — 3-minute submission video

_Per-beat scripts with `<break time="Xs" />` tags at every engineered pause. The story arc walks judges from intake documents → the program that defines eligibility → the human-approved reply templates → the AI pipeline → the deterministic render → the second-program cameo → the audit trail → the CTA. Every beat has a hand-composed visual under it; the screen shows exactly what the voice is saying._

## Recommended ElevenLabs settings

_Full rationale + invocation lives in `docs/demo-scripts/voice-choice.md`. The values below are the ones used to render the submission audio._

| Setting | Value | Why |
| --- | --- | --- |
| **Model** | `eleven_multilingual_v2` | Honors `<break time="..." />` reliably. |
| **Voice** | Nate (`kTVcdVhzYgIburyGmKK0`) — "Engaging and Confident Innovator" | Voice Library / professional tier. Warm confident narrator register. Free-tier fallback: Liam (`TX3LPaxmHKxFdv7VOQHJ`). |
| **Stability** | `0.5` | Balanced. |
| **Similarity boost** | `0.75` | Standard. |
| **Style exaggeration** | `0.0` | Narration, not performance. |
| **Speed** | `1.10` | Calibrated to fit the ~180 s `BEAT_DURATIONS` budget in `scripts/assemble_video.sh`. Do not lower without retuning the array. |
| **Speaker boost** | on | |
| **Output format** | `mp3_44100_128` | 44.1 kHz stereo MP3. |

**Filename convention (required by the assembly script):**

```
audio/beat0a.mp3     intro: title card (silent-ish, one line)
audio/beat0b.mp3     intro: what is a PAP
audio/beat0c.mp3     intro: why AI, this way (AI in, deterministic out)
audio/beat0d.mp3     intro: end-to-end architecture
audio/beat1.mp3      intake documents arrive
audio/beat2.mp3      program: eligibility as data
audio/beat3.mp3      output docs: templates + human approved
audio/beat4.mp3      pipeline runs: ingest → triage → decide
audio/beat5.mp3      then AI stops (deterministic merge)
audio/beat6.mp3      second-program cameo
audio/beat7.mp3      auditable end-to-end
audio/beat8.mp3      CTA (Verdikt tech stack)
```

Files land under `assets/video/audio/`.

---

## Beat 0a — Title card (4 s)

```
Verdikt.<break time="0.4s" /> Nebius times NVIDIA Hackathon submission.
```

**Visual:** cream title card — Verdikt wordmark centered under the mark, "Nebius × NVIDIA · Global AI Hackathon 2026" lockup at the top, submitter line "Submitted by Ajanthan Anandarajan · ajandan" at the bottom above the repo URL and MIT license badge.

---

## Beat 0b — What is a PAP (11 s)

```
Thousands apply for patient assistance programs when insurance won't cover their medication.<break time="0.3s" /> Each response, a compliance-audited letter.<break time="0.3s" /> Today, it's siloed — every rule change waits on developers.
```

**Visual:** three-column scene — applicant portrait card on the left, a fanned stack of documents (enrollment, pay stub, prescription + RECEIVED stamp) in the center, coordinator portrait card on the right with a "by hand" badge. Arrows labeled *applies* and *reviews* connect them. Footer band: "Today, this is a manual process."

---

## Beat 0c — Why AI, this way (9 s)

```
You could throw an agent at it.<break time="0.3s" /> But letters under audit must reproduce bit-for-bit,<break time="0.2s" /> years later.<break time="0.4s" /> Most AI can't.<break time="0.5s" /> So we draw a line —<break time="0.2s" /> AI where it earns its place,<break time="0.2s" /> deterministic code where it doesn't.
```

**Visual:** split-frame — left half greyed-out "throw an agent at it" world with a six-cell swarm and a rose call-out about bit-for-bit reproduction, right half the highlighted Verdikt approach with three verb cards (Reads / Decides / Ships) and a dark promise band: *"Same input. Same output. Forever."*

---

## Beat 0d — End-to-end architecture (8 s)

```
Every channel fans into one pipeline.<break time="0.3s" /> Five named stages —<break time="0.2s" /> Ingest,<break time="0.15s" /> Triage,<break time="0.15s" /> Decide,<break time="0.15s" /> Deliver,<break time="0.15s" /> Remember.<break time="0.3s" /> Every outcome traced.
```

**Visual:** end-to-end architecture — six intake channels fan into a bordered AI Zone (Ingest / Triage / Decide, each with its Nebius Token Factory model chip) with a Program Configuration touchpoint below, a *Stop* gate divider, then the Deterministic Zone (Deliver / Remember) with a Human Review touchpoint. Outcome strip footer: *Same input → Same package → Forever.*

---

## Beat 1 — Intake documents arrive (18 s)

```
An applicant applies for a patient assistance program.<break time="0.4s" /> They send an enrollment form,<break time="0.15s" /> pay stubs,<break time="0.15s" /> an insurance card,<break time="0.15s" /> a prescription.<break time="0.5s" /> This is the intake — everything the case begins with.
```

**Visual:** hand-composed layout showing the intake stack (enrollment form + pay stub + insurance card + prescription), design-system aesthetic, cream paper cards.

---

## Beat 2 — Programs: eligibility as data (22 s)

```
In Verdikt, an assistance program is data.<break time="0.5s" /> Each program defines the fields to extract from the intake,<break time="0.3s" /> the rules to check eligibility,<break time="0.3s" /> and the reply templates to send.<break time="0.6s" /> Onboarding a new program is filling out a form — not shipping a build.
```

**Visual:** a "Program: Cardiovita PAP 2026" card with three sections — Extraction schema (field list), Rules pack (YAML), Reply templates (frozen list).

---

## Beat 3 — Output docs: templates + human approved (24 s)

```
The reply is never just a letter.<break time="0.4s" /> It's an ordered document package — the outcome letter,<break time="0.15s" /> an appeals notice,<break time="0.15s" /> a benefits guide,<break time="0.15s" /> the blank forms the applicant needs next.<break time="0.6s" /> Every one is a human-approved template.<break time="0.4s" /> Once frozen,<break time="0.2s" /> no model can change it.
```

**Visual:** doc package composition — outcome letter + appeals notice + benefits guide + blank forms, each stamped with a `FROZEN · sha256:…` badge.

---

## Beat 3b — Outcome → doc package mapping (19 s)

```
Each outcome maps to its own composed package.<break time="0.5s" /> Approve — the letter,<break time="0.15s" /> appeals notice,<break time="0.15s" /> benefits guide.<break time="0.5s" /> Deny — the letter,<break time="0.15s" /> appeals guide,<break time="0.15s" /> resubmission form.<break time="0.5s" /> Missing information — the letter,<break time="0.15s" /> a checklist of what's needed.<break time="0.5s" /> The operator declares the mapping.
```

**Visual:** three-row mapping — approve → 4-item kit, deny → 3-item kit, missing_info → 2-item kit — each with a *frozen* stamp. Bottom band: *"programs.package_bindings · JSONB · no AI decides composition"*.

---

## Beat 4 — Pipeline runs: ingest → triage → decide (26 s)

```
When a case arrives, the pipeline runs.<break time="0.3s" /> Ingest — a vision-language model reads every attachment.<break time="0.3s" /> Triage — a classifier picks the route.<break time="0.15s" /> Refills to the pharmacy,<break time="0.15s" /> missing info to CRM,<break time="0.15s" /> fresh intakes continue.<break time="0.3s" /> Decide — embeddings retrieve the rules, a reasoning model produces the verdict — approve,<break time="0.15s" /> deny,<break time="0.15s" /> or missing info — with citations.<break time="0.3s" /> All on Nebius Token Factory.
```

**Visual:** horizontal three-stage pipeline — **Ingest** [Vision-language] → **Triage** [Classifier] → **Decide** [Reasoning + embeddings], each stage a card with model chip + one-line input/output. All three chips branded **Nebius Token Factory**.

---

## Beat 5 — Then AI stops (21 s)

```
Then AI stops.<break time="0.8s" /> Deliver — a byte-identical merge of the verdict with a human-approved template.<break time="0.3s" /> Every template is human-reviewed.<break time="0.3s" /> Here's the approval letter,<break time="0.15s" /> from Alderwood's template — sha256 pinned.<break time="0.3s" /> No model at render time.<break time="0.3s" /> Same input,<break time="0.15s" /> same package,<break time="0.15s" /> forever.
```

**Visual:** AI ZONE / DETERMINISTIC ZONE split — left half fades out with the AI stages; right half lights up with the verdict + frozen template merging into a rendered PDF, `byte-identical · sha256:…` stamp.

**Applause line:** *"Then AI stops."* — 1.5 s pause is the one-frame overlay window.

---

## Beat 6 — Same code, opposite verdict (17 s)

```
The pipeline is the product,<break time="0.2s" /> not the program.<break time="1.2s" /> A second program.<break time="0.3s" /> A stricter income cap.<break time="0.4s" /> Same email.<break time="0.2s" /> Same code.<break time="0.3s" /> Opposite verdict.
```

**Visual:** two verdict cards side by side — Cardiovita PAP · APPROVE (seal-500 border) vs OncoBridge PAP · DENY (rose border) — same pipeline diagram connecting them.

**Applause line:** *"The pipeline is the product, not the program."* — 1.2 s pause is the one-frame overlay window.

---

## Beat 7 — Auditable end-to-end (15 s)

```
Remember — every stage is auditable.<break time="0.3s" /> AI events,<break time="0.15s" /> human approvals,<break time="0.15s" /> system deliveries — all stamped,<break time="0.3s" /> all reproducible from the audit log,<break time="0.3s" /> years later.
```

**Visual:** vertical audit trail — intake received · fields extracted · rules cited · verdict rationale · rendered letter — each row stamped with a timestamp + version hash.

---

## Beat 8 — Verdikt CTA (12 s)

```
Verdikt.<break time="0.4s" /> NVIDIA and open-source models on Nebius Token Factory.<break time="0.3s" /> One deterministic reply.<break time="0.3s" /> MIT-licensed.<break time="0.2s" /> Links below.
```

**Visual:** dark hero — Verdikt wordmark + Nebius × NVIDIA lockup + three tech chips (Vision-language, Classifier, Reasoning — all on Nebius Token Factory) + MIT License badge + three URL cards (Repo / Demo / Dashboards).

---

## Timing summary

| Beat | Budget | Story move |
| :-: | :-: | --- |
| 1 | 18 s | What arrives — intake documents |
| 2 | 22 s | Program = data (not code) |
| 3 | 24 s | Reply is a doc package of frozen human-approved templates |
| 4 | 30 s | Pipeline runs — ingest, triage, decide on Nebius Token Factory, cited verdict |
| 5 | 25 s | Then AI stops — deterministic byte-identical merge |
| 6 | 17 s | Cameo — same pipeline, opposite verdict |
| 7 | 15 s | Auditable end-to-end |
| 8 | 15 s | CTA — Verdikt tech stack + repo/demo/dashboards |
| **Total** | **166 s = 2:46** | |

## What NOT to change

- Don't remove the `<break>` tags — they're the applause-line timing that the visual overlays sync to.
- Don't inject stats or numbers — the whole piece is designed numberless. Adding "50k applicants" or "3–6 seconds" unbalances the beats and violates the project's no-invented-numbers rule.
- Don't switch narrator voices between beats. Generate all 8 files with Liam + the settings above.
