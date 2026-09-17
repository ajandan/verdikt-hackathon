# 3-minute submission video — full shooting script

_Master shooting script for the Nebius × NVIDIA hackathon submission video. Target run-time **2:55**, hard cap **3:00**. Five beats, two engineered applause lines, one continuous take from cold-open to close. Public-framing rules (no employer references, no library names in prose, no competitor names) apply to voiceover **and** on-screen text. NVIDIA / Nebius / model names stay — hackathon-mandated._

## Timing overview

| # | Beat | Start | End | Duration | Applause line |
| :-: | --- | :-: | :-: | :-: | --- |
| 1 | Cold open | 0:00 | 0:20 | 0:20 | — |
| 2 | Live demo | 0:20 | 1:30 | 1:10 | — |
| 3 | Architecture beat | 1:30 | 2:22 | 0:52 | *"AI-in, deterministic-out"* · *"Then AI stops"* |
| 4 | Second-program cameo | 2:22 | 2:38 | 0:16 | *"The pipeline is the product, not the program"* |
| 5 | Close + CTA | 2:38 | 2:55 | 0:17 | — |
| | **Total** | | | **2:55** | |

**Pacing target:** 2.5 words per second. Slightly slower than conversational. Every sentence is a claim, no hedging.

---

## Beat 1 — Cold open (0:00 – 0:20)

**Voice-over:**

> In regulated document workflows, the letter that leaves the building is the artifact under audit. It has to reproduce bit-for-bit, defensibly, years later. Most AI products can't guarantee that. We built one that can.

**Visual sync:**

| Cue | Visual |
| --- | --- |
| "regulated document workflows" | Close-up: hands opening a case folder / claim envelope |
| "artifact under audit" | Cut to a rendered PDF letter, watermarked *AUDIT COPY* |
| "reproduce bit-for-bit" | Same PDF rendered twice side-by-side, diff overlay reads `0 bytes different` |
| "Most AI products can't" | Quick montage of generic chatbot bubbles fading out |
| "We built one that can" | Verdikt wordmark reveal (Instrument Serif), pipeline hero shot |

---

## Beat 2 — Live demo (0:20 – 1:30)

_Screen capture from the actual running system. No mock-ups. Every artifact shown is one the pipeline actually produced against a real intake._

**Voice-over, sub-beats aligned to what's on screen:**

**(0:20 – 0:34)** — *email lands*

> An enrollment packet arrives by email. Multiple attachments — enrollment form, income documents, insurance card, prescription. The system reads all of them.

**(0:34 – 0:56)** — *pipeline runs*

> Three stages run, one NVIDIA model per stage. Extraction reads every attachment into a structured intake. Retrieval surfaces the program's eligibility rules from a versioned knowledge base. Reasoning evaluates the intake against those rules and produces a structured verdict — approve, deny, or missing information — with citations back to the rules that fired.

**(0:56 – 1:14)** — *reviewer opens the case*

> A human reviewer opens the case. Every stage's output is visible and defensible: the extracted fields, the rules cited with retrieval scores, the reasoning behind the verdict, and the exact reply that left the building.

**(1:14 – 1:30)** — *reveal the doc package*

> And the reply isn't a single letter. It's an ordered document package — the outcome letter, the appeals notice, the benefits guide, the blank forms the applicant needs next. All rendered from human-approved templates. Composed in the order the operator declared. Same input, same package, forever.

**Visual sync:**

| Time | On-screen |
| :-: | --- |
| 0:20 | Inbox view — new applicant email highlighted, attachment count visible |
| 0:26 | Attachment thumbnails fan out: enrollment form, pay stub, insurance card, prescription |
| 0:34 | Pipeline diagram: three stages animate left-to-right (Extract → Retrieve → Reason) |
| 0:38 | Extract stage — structured intake JSON scrolls into view |
| 0:44 | Retrieve stage — versioned rules-pack YAML flashes, four rules light up in a rules table |
| 0:50 | Reason stage — verdict badge lands (APPROVE), one-sentence rationale below |
| 0:56 | Cut to `/ui/inbox/{event_id}/pipeline` — full reviewer detail page scrolls |
| 1:02 | Zoom on rule-citations table: rule ID · matched yes/no · reranker score · statement |
| 1:10 | Zoom on Reply section — Message-ID + attached PDF link |
| 1:14 | Open the reply — cover page reveals it's a doc package |
| 1:20 | Doc-package assembly log: template version + each static-doc version stamped in order |
| 1:26 | Cut to `/ui/programs` — one row per tenant × program, extraction schema visible |

---

## Beat 3 — Architecture beat (1:30 – 2:22)

_The load-bearing beat. Two engineered applause lines, both designed as one-frame text overlays. Land them cleanly: full stop before, full stop after, no rushing._

**Voice-over:**

> The industry is racing to solve this with agents. We took the opposite bet.
>
> **AI-in, deterministic-out.**
>
> We call it **Verdikt**. Three NVIDIA-family models compose the pipeline, one per stage. Multimodal extraction reads the intake documents. NeMo Retriever surfaces the eligibility rules. Nemotron reasons over both to produce a structured decision.
>
> **Then AI stops.**
>
> The reply is a byte-identical merge of human-approved templates with that decision — the outcome letter plus every static document the applicant needs, composed in the order the operator declared. No model runs at render time. No model can. Same input, same package, forever — reproducible from the audit log years later.
>
> This is where regulated verticals are converging. We built it now.

**Emphasis words:** *opposite bet*, *Verdikt*, *earn their place*, *stops*, *byte-identical*, *no model can*, *forever*, *built it now*.

**Product-name landing:** *"We call it Verdikt"* is the one time the name is spoken in this beat. Brief pause before, brief pause after, so the on-screen name callout has a word to sync to.

**Visual sync:**

| Cue | Visual |
| --- | --- |
| "opposite bet" | Split-frame: agent swarm on left greying out, deterministic pipeline on right lighting up |
| "AI-in, deterministic-out" | Full-frame text overlay — first applause line |
| "Three NVIDIA-family models" | Architecture diagram reveal, three stages highlighted |
| Model callouts (extraction / retriever / Nemotron) | Three quick highlights on the pipeline diagram, one per stage |
| "Then AI stops" | Dramatic cut to the render code path with model boxes greyed out — second applause line |
| "byte-identical merge" | Two rendered PDFs side-by-side, diff overlay reads `0 bytes different` (callback to Beat 1) |
| "composed in the order the operator declared" | Doc-package composition list scrolls |
| "reproducible from the audit log" | Audit-log UI, re-render animation lands on the same bytes |
| "We built it now" | Back to the demo hero shot |

---

## Beat 4 — Second-program cameo (2:22 – 2:38)

_Immediately after the architecture beat, same voice, same pace. Continuous take with the architecture beat, not a separate segment. Without the cameo, judges may read the submission as a one-off._

**Voice-over:**

> **The pipeline is the product, not the program.**
>
> A second manufacturer, a stricter income cap. Same email, same code — opposite verdict.
>
> New programs onboard as data, not builds.

**Applause line:** *"The pipeline is the product, not the program."* Second one-frame overlay of the video.

**Visual sync:**

| Cue | Visual |
| --- | --- |
| "pipeline is the product" | Split-screen: same pipeline diagram, two rules-pack labels swap in |
| "stricter income cap" | Brief flash of the second rules-pack YAML, one changed threshold highlighted |
| "opposite verdict" | Side-by-side decision cards — APPROVE on the left, DENY on the right |
| "data, not builds" | Back to `/ui/programs` — the rules folder is the only thing that changed |

---

## Beat 5 — Close + CTA (2:38 – 2:55)

**Voice-over:**

> Verdikt. Three NVIDIA models on Nebius Token Factory, one deterministic reply path. MIT-licensed, fully open source. Repo, live demo, and the same engineering dashboards the team uses — all linked below.

**Visual sync:**

| Cue | Visual |
| --- | --- |
| "Verdikt" | Wordmark + logo mark, centered, held |
| "Three NVIDIA models on Nebius Token Factory" | Hackathon logo lockup (Nebius × NVIDIA) |
| "MIT-licensed" | License badge |
| "Repo, live demo, dashboards" | Three URL cards animate in — repo URL, demo URL, `/engineering` dashboard URL |
| Hold on end card | Verdikt wordmark + Devpost / YouTube / GitHub icons |

---

## Delivery notes

**Pacing:** 2.5 words per second across every beat. Slightly slower than conversational. Gives the editor room for cuts and lets the applause lines breathe.

**Applause lines — three total, all engineered as one-frame text overlays:**
1. *"AI-in, deterministic-out"* (Beat 3, ~1:36)
2. *"Then AI stops"* (Beat 3, ~1:55)
3. *"The pipeline is the product, not the program"* (Beat 4, ~2:22)

Land each with a full stop before and after. No rushing into the next sentence.

---

## Hero visual moment (engineered)

_A single memorable image judges recall when comparing this entry against others in the final shortlist. This is the picture the panelist describes to a co-panelist afterwards. Positioned inside Beat 2, immediately after the verdict lands. Only one in the whole video — do not dilute._

**Beat:** ~0:53 — comes one heartbeat after the pipeline finishes and the APPROVE badge lands at 0:50. A held moment, not a transition.

**Voice-over (one word, held):** *"Signed."*

- Full beat of silence before the word (~400 ms).
- Word spoken slowly, with slight downward inflection — a period, not a question.
- Full beat of silence after (~600 ms), letting the visual finish its animation.
- Skips the 2.5 words-per-second cadence deliberately. This is a punctuation moment.

**Visual:** The APPROVE verdict badge cross-dissolves into the paper-stamp verdict card — kraft-cream paper texture, deep-green ink, the wordmark VERDIKT along the top, the meta fields (case ID / model / retrieval / date) underneath. On the word *"Signed"* the wax-stamp seal punches down in the bottom-right of the card with a subtle scale-and-settle animation. The whole card holds for ~1 second before continuing.

**Sound design:** A soft paper-and-wax "thunk" synced to the seal landing. No music swell. No other sound.

**Why:** The stamp is Verdikt's product identity made physical. Same reason a signed cheque is more memorable than a spreadsheet — judges scoring finalists remember the entry with one image they can describe. This is that image.

**Editing notes:**
- Held frame reused as the YouTube thumbnail crop and as the video-hero-shot fallback at 0:00 – 0:03.
- If the render pipeline can produce it, the stamp card should be the actual PDF paper-stamp verdict from a real case (not a mock-up). The whole video is real artifacts; this one especially.

**Product-name landing:** *"We call it Verdikt"* is the only time the name is spoken in the architecture beat. Brief pause before, brief pause after, synced to the on-screen wordmark reveal.

**Continuous take:** Beats 3 and 4 are one continuous voice take. Do not stop the record between the architecture beat and the cameo — the tempo carries the transition.

**No hedging:** every sentence is a claim. Do not add *"we think"*, *"arguably"*, *"in most cases"*. The piece works because it doesn't hedge.

**No invented numbers:** wall-clock latency, dollar amounts, applicant counts, and processing-time claims stay out of the voiceover. Beat 1 uses *"years later"* rather than a specific retention window on purpose.

---

## Latest-features coverage (as of 2026-09-15)

The script exercises these features live on screen. Every one is a real capability in the running system, not a mock-up.

| Feature | Where it lands in the video | Repo evidence |
| --- | --- | --- |
| Three NVIDIA models one-per-stage | Beats 2 + 3 | `src/docgen/providers/nebius.py`, `src/pap_pipeline/stages/` |
| Pipeline reviewer detail page | Beat 2 (0:56 – 1:14) | `/ui/inbox/{event_id}/pipeline`, migrations `0018` + `0019` |
| Rule citations with retrieval scores | Beat 2 (1:02) | `Decision.citations`, `_attach_retrieval_scores` |
| Reply is an ordered doc package | Beat 2 (1:14 – 1:30) + Beat 3 payoff | Migration `0029_doc_packages.py`, `doc_package_assemblies.resolved_items` |
| Programs table — new PAP = data change | Beat 2 (1:26) + Beat 4 payoff | Migration `0023_programs.py`, `/ui/programs` |
| Structured JSON verdict with rationale | Beat 2 (0:50) | `Decision.outcome`, `Decision.rationale` |
| Byte-identical audit-artifact retrieval | Beats 1 + 3 | `inbox_events.reply_attachment_blob_key` |
| Two-program cameo (Cardiovita → OncoBridge) | Beat 4 | `src/pap_pipeline/rules/oncobridge-pap-2026.yaml` |
| Live engineering dashboards | Beat 5 CTA | `reports/` mounted at `/engineering` |

---

## What NOT to add

- Don't explain what NeMo Retriever *is* in the voiceover — on-screen text handles the label.
- Don't say "Patient Assistance Programs" and then explain what a PAP is. Judges either know or the demo makes it obvious.
- Don't name libraries, frameworks, or competitors in prose or on-screen text. NVIDIA / Nebius / model names are the only exception (hackathon-mandated).
- Don't invent numbers. If a stat isn't measured in the running system, it stays out.
- Don't hedge. Every sentence is a claim.

---

## Alternate registers (if this one doesn't fit)

**Warmer / more human:** lead Beat 1 with a patient-perspective framing (someone waiting on an eligibility decision) and land the same architectural payoff in Beat 3. Only use numbers if they're grounded in a public source cited on-screen.

**Sharper / more contrarian:** rewrite Beat 1 as a direct challenge to the agent-everywhere trend (*"The rest of the industry is putting language models on the last mile. That's the mistake..."*). Scores higher on quality-of-idea but risks alienating a judge currently building an agent product.
