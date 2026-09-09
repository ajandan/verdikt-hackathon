# Why a decision pipeline, not an agent swarm

_README-ready justification for the architectural choice. Load-bearing paragraphs a judge or a compliance-minded engineer reads before deciding whether to keep scrolling._

The default 2026 answer to *"let AI do PAP intake"* is an agent workflow — a chorus of language models reading documents, retrieving rules, drafting letters, and shipping decisions autonomously. It's the shape most of the industry is building. It's also, increasingly, the shape compliance officers are quietly refusing to sign off on.

We took the opposite architectural bet: **AI-in, deterministic-out.**

That pipeline is called **Verdikt**. It runs in named, observable stages, and AI is called only where it genuinely earns its place:

- **Intake extraction.** A multimodal model reads the enrollment packet — pay stubs, insurance cards, prescriptions, denial letters — and emits a structured intake object. Vision handles what regex cannot. Nothing else runs at this stage.
- **Rule retrieval.** An embedding-and-rerank pass surfaces the program's current eligibility rules from a versioned knowledge base. The rules themselves are human-authored and human-versioned; the model's job is to find the ones that apply to *this* case.
- **Eligibility reasoning.** A reasoning model evaluates the extracted intake against the retrieved rules and produces a decision — approve, deny, or request specific missing information — with citations back to the rules that fired.

Every stage above is observable, versionable, and independently testable. Every artifact between stages is a structured object a human reviewer can read.

**Then AI stops.**

The outbound reply is a byte-identical merge of a human-approved template with the pipeline's structured decision. No model runs at render time. No model *can* — the package boundary between render and provider layers is enforced in tests. A given decision plus a given template version produces exactly the same PDF, forever, and that PDF can be re-produced from the audit log years later.

That last property is the whole point. In regulated document workflows — Patient Assistance Programs, prior authorization, benefit verification, MedWatch reporting, informed consent — the letter that leaves the building is the artifact under audit. It has to be reproducible bit-for-bit, defensible against a validation package, and free of unpredictable model drift. LLM-in-the-loop letter generation cannot make any of those guarantees. A deterministic merge over a structured decision can make all of them.

None of this is a rejection of AI. It is a rejection of AI in the wrong place. The pipeline uses three NVIDIA-family models — multimodal extraction (`nv-ingest`), retrieval embedding and rerank (NeMo Retriever NIMs), and reasoning (Nemotron on Nebius Token Factory) — each deployed at the stage where its capability matters and nowhere else. What we removed is not intelligence. It is non-determinism from the output surface.

## But aren't agents the future?

The strongest version of the counter-argument goes: agents in 2026 reflect on their own reasoning, chain novel workflows, adapt to unseen document formats without retraining. NVIDIA's own Agent Toolkit exists because the whole industry is composing around agentic patterns. A pipeline is a 2022 architecture in 2026 clothing.

That's coherent, and for chat assistants, code agents, and personal-productivity copilots, it's correct. It misapplies here for five specific reasons.

**The pipeline doesn't forbid agents — it forbids nondeterminism at the output.** The intake and reasoning stages are agent-capable today and will absorb every capability jump the agent world produces, wrapped in NeMo Agent Toolkit or whatever comes next. The architectural boundary is not "no agents." It is "no language model on the code path that generates the audited artifact."

**Regulatory constraint is physics-level, not preference.** For products under FDA GxP validation, DOJ oversight, or a Corporate Integrity Agreement, the requirement *"output is reproducible from the input, forever"* doesn't relax as agents get smarter. If anything, it hardens — an FDA warning letter for "AI-generated patient communication with drift" is a legal artifact that lives on the record for two decades.

**Agent capability growth increases the value of the boundary, not decreases it.** As agents get smarter, the delta between "what a smart agent might produce" and "what a human-approved template says" gets larger, more unpredictable, and harder to audit. Tighter constraints on where the agent's output can land become *more* valuable as capability rises, not less.

**The coding-agent analogy is instructive.** Coding agents in modern developer tools are genuinely agentic and correctly so — because every artifact they produce goes through human review and a test suite before running in production. There's a checkpoint. A hub processing 40,000 PAP letters a month cannot human-review every letter, so the artifact must be reproducibly derivable from structured input. Same rule: no checkpoint, no unbounded generation.

**Futurism cuts the other way.** The GDPR / EU AI Act / evolving FDA AI-guidance trajectory is toward provenance, replay, and constrained output. The EU AI Act categorizes healthcare eligibility decisions as *high-risk* — triggering documentation, human oversight, and reproducibility requirements that map exactly onto pipeline architectures and exactly against unconstrained agent architectures. Building audit-defensible in 2026 is *ahead* of the regulatory curve, not behind the capability curve.

The one-line answer: **we use agents everywhere they earn their place; what we don't do is put a language model on the code path that generates the audited artifact — and as agents get more capable, that boundary gets more valuable, not less.**

## The forward-looking version

We are not the entrant *avoiding* agents. We are the entrant who took the same underlying models everyone else is using — multimodal extraction, retrieval embedding + rerank, reasoning-tuned Nemotron — and composed them so the output surface stays reproducible. That is a *more* sophisticated architectural claim than "we chained agents and hoped." It's also the shape the regulated verticals are converging toward as their compliance teams catch up with the tooling.
