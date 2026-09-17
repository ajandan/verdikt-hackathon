# 60-second video narration — architecture beat (+ cameo)

> **Superseded by [`video-3min.md`](video-3min.md)** — the full shooting script that folds this beat into a five-beat 2:55 master (cold open, live demo, architecture beat, cameo, close). Kept here as a detail reference for the middle beat only; the master is the source of truth for the recorded video.

_Voice-over script covering the ~1:30 to ~2:45 window of the demo video. The 60-second architecture beat lands the "AI-in, deterministic-out" claim; a 15-second cameo immediately after lands the "platform, not bespoke" claim. Two beats, two applause lines, still inside the 3-minute cap with margin._

## Script

> In regulated document workflows — Patient Assistance Programs, prior authorization, benefit verification — the letter that leaves the building is the artifact under audit. It has to be reproducible bit-for-bit, defensible against validation, free of unpredictable model drift.
>
> Most of the industry is racing to solve this with agents. We took the opposite bet.
>
> **AI-in, deterministic-out.**
>
> We call it **Verdikt**. It uses three NVIDIA-family models where they earn their place. Multimodal extraction reads the intake documents. NeMo Retriever surfaces the eligibility rules. Nemotron reasons over both to produce a structured decision.
>
> **Then AI stops.**
>
> The reply is a byte-identical merge of a human-approved template with that decision. No model runs at render time. No model can. Same input, same output, forever — reproducible from the audit log years later.
>
> This is the architecture regulated verticals are converging toward. We built it now.

## Second-program cameo (~15s, ~2:30 to 2:45)

_Immediately after the architecture beat. Same voice, same pace — this is one continuous take with the architecture beat, not a separate segment. The cameo lands the "platform, not bespoke" claim; without it, judges may read the submission as a one-off._

> **The pipeline is the product, not the program.**
>
> A second manufacturer, a stricter income cap. Same email, same code — opposite verdict.
>
> New programs onboard as data, not builds.

**Applause line:** *"The pipeline is the product, not the program."* Second one-frame overlay of the video.

**Cameo visual sync:**

| Cue | Visual |
| --- | --- |
| "pipeline is the product" | split-screen: same pipeline diagram, two rules-pack labels |
| "stricter income cap" | brief flash of the second rules-pack YAML, one changed threshold highlighted |
| "opposite verdict" | side-by-side Decision cards — APPROVE on the left, DENY on the right |
| "data, not builds" | back to the pipeline diagram; the rules folder is the only thing that changed |

## Delivery notes

**Pacing:** ~2.5 words per second. Slightly slower than conversational. Gives the editor room for cuts and lets the two standalone lines breathe.

**Two guaranteed applause lines:** *"AI-in, deterministic-out"* and *"Then AI stops."* Both are engineered to be one-frame text overlays. Land them cleanly — full stop before and after, no rushing.

**Emphasis words:** *audit*, *opposite bet*, *Verdikt*, *earn their place*, *stops*, *byte-identical*, *no model can*, *forever*, *built it now*.

**Product-name landing:** *"We call it Verdikt"* is the one time the name is spoken in the 60-second beat. Land it clean — brief pause before, brief pause after — so the on-screen name callout (architecture diagram reveal, ~1:45) has a word to sync to.

## Visual sync suggestions

| Cue | Visual |
| --- | --- |
| "letter that leaves the building" | cut to the actual rendered PDF |
| "AI-in, deterministic-out" | architecture diagram reveal |
| Three model callouts | three quick highlights on the pipeline diagram |
| "Then AI stops" | dramatic cut to the template-merge code path with the model boxes greyed out |
| "reproducible from the audit log" | audit log UI or a re-render animation |
| "We built it now" | back to the demo hero shot |

## What NOT to add

- Don't try to explain what NeMo Retriever *is* in the narration — the on-screen text handles that. Voice keeps moving.
- Don't say "Patient Assistance Programs" and then explain what PAP is. Judges either know or the demo makes it obvious.
- Don't hedge. The whole piece works because every sentence is a claim. Adding "we think" or "arguably" anywhere would collapse it.

## Alternate registers (if this one doesn't fit)

**Warmer / more human:** lead with a patient story ("Every year, four million Americans apply for patient assistance programs. Most wait five to fifteen days for a first response...") and land the same architectural payoff.

**Sharper / more contrarian:** rewrite as a direct challenge to the agent-everywhere trend ("The rest of the industry is putting language models on the last mile. That's the mistake..."). Scores higher on quality-of-idea but risks alienating a judge currently building an agent product.
