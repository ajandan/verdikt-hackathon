# Judge Q&A briefing — sharper answers and honest concessions

_Material to carry into live Q&A, judge meet-and-greet, or press conversation. Not README material — the register is too pointed for public-facing docs, but exactly right when someone challenges the architecture in person._

## The sharper close

When someone claims agent-everywhere is inevitable and this pipeline architecture will look dated:

> **"Show me the pharma manufacturer signing a Corporate Integrity Agreement that permits stochastic patient communication. I'll wait."**

Delivered dry. It lands because most challengers haven't sat across the table from a compliance officer under active federal monitoring, and it forces the conversation onto ground where the pipeline architecture is unambiguously the correct answer.

## The honest concession

If someone presses on whether the pipeline is a permanent architectural commitment:

> **"The pipeline needs to keep absorbing agent capability at its internal stages. If the intake and reasoning stages stop advancing, an all-agent competitor whose compliance story just barely passes will eventually lap us. The practical answer isn't 'pipeline forever, agents never' — it's 'pipeline as the outer contract, most-capable-available agents inside each stage.'"**

Delivering this concession voluntarily is a strength, not a weakness. It shows the position is architectural, not defensive.

## The market-language reframe

For the recurring "aren't you behind the trend?" question:

> **"'Pipeline company' recruits differently than 'agent company' in 2026. If you want to hire top AI researchers, agent framing helps you. If you want to sell to a large manufacturer's compliance office, pipeline framing helps you. We picked our market and matched our language."**

## What NOT to say

- Don't disparage agents generally. The audience includes people who correctly ship agent products.
- Don't claim regulation will *always* forbid autonomous agents in healthcare. Long-term it may loosen; the argument is about the current and near-term audit ceiling.
- Don't get pulled into a "chat assistants vs. document workflows" comparison. Concede immediately that chat assistants are correctly agentic and pivot back to the audit constraint that makes regulated docs different.

## Adjacent talking points if the conversation goes deeper

**On why the render path is defended in tests, not just in docs:** the package boundary between `core/` (render) and `providers/` (AI) is enforced by import checks — a test fails if render code imports a provider. That means the "no model at render time" claim is a mechanical property of the build, not a code convention that could drift.

**On why per-tenant fine-tuned Nemotron is the natural next step:** each manufacturer's PAP has different eligibility rules. A LoRA-tuned Nemotron per tenant, hosted as a Serverless Endpoint on Nebius, gives that manufacturer weight ownership + a frozen model version that fits their validation package. Prior work (generic prompt engineering) can't cross that ownership boundary; the LoRA move can.

**On the routing table:** three NVIDIA models on one Nebius key, chosen by task shape. Multimodal extraction handles what regex cannot. Retrieval embedding + rerank surfaces relevant rules. Reasoning-tuned Nemotron produces the structured decision. Each is measurably faster or cheaper than promoting the whole workflow to a single monolithic model call, and the routing decisions are documented per call site.
