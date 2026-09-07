# Quire demo — YouTube script (target: 2:45, hard cap 3:00)

Written for a screen-capture + voiceover. Every beat maps to one of the four
hackathon judging criteria — pointed out in brackets so nothing gets cut in
editing that shouldn't be.

## Pre-flight before hitting record

- Deploy is live at `https://quire.<yourdomain>` per
  [`docs/deploy-nebius.md`](deploy-nebius.md).
- `DOCGEN_AI_PROVIDER=nebius` and Nebius Token Factory key are set.
- Sample template preloaded (`sample_templates/denial_letter.docx`).
- Terminal open in a second workspace showing `docker compose logs -f web`
  for the "AI request lands at Nebius" beat.
- Browser zoom to 125 %. Hide bookmarks bar.

---

## Scene 1 — Cold open (0:00 – 0:20) — [Quality of idea]

**Screen:** cut between two documents on screen side-by-side: an insurance
denial letter and a mortgage disclosure. Both have visible blanks.

**Voiceover:**
> Every regulated business runs on documents like these. Denials, statements,
> disclosures — thousands a day, each one templated, each one legally
> auditable. The blanks are what change; the shape doesn't. So why does every
> new template still take a lawyer, a developer, and a week?

## Scene 2 — The Quire idea (0:20 – 0:40) — [Quality of idea]

**Screen:** Quire logo, then the tagline "Design-time AI. Deterministic
render."

**Voiceover:**
> Quire flips the AI split. Use a model **once**, when the template is first
> mapped — to figure out where the blanks are and what they mean. Then get
> out of the way. Every render after that is a pure, byte-identical merge.
> No model in the render path. Auditable by construction.

## Scene 3 — Upload + AI mapping (0:40 – 1:30) — [Technical implementation]

**Screen:** admin UI, empty templates list. Click **"Or try a sample →"**.

**Voiceover, timed to actions:**
> This is the sample template — a real insurance denial letter with blanks
> like `[MEMBER_NAME]`, `[POLICY_NUMBER]`, a date, an amount. Quire uploads
> it, runs preflight, and hands it to the design-time LLM…

**Screen:** cut briefly to the terminal — one outbound HTTPS request lands at
`api.studio.nebius.com`. Overlay a small caption: **"NVIDIA Nemotron on
Nebius Token Factory."**

> …an NVIDIA Nemotron model, hosted on Nebius Token Factory. The model
> proposes typed fields — `member_name` is a string, `denial_date` is a
> date, `amount_owed` is a number — and Jinja tags to replace each blank.
> The human reviews and adjusts in the mapping wizard.

**Screen:** mapping wizard, fields visible, click Freeze.

> Freeze pins the schema. Publish makes it callable.

## Scene 4 — Deterministic render (1:30 – 2:05) — [Technical implementation, Design]

**Screen:** REST Swagger UI at `/docs`. Show the `POST /renders` endpoint,
fill in the JSON payload, execute. Downloaded `.docx` opens.

**Voiceover:**
> One published version, three transports: the admin UI, this REST endpoint,
> and an MCP server for LLM callers. All three route through the same domain
> layer.

**Screen:** run the same render call twice; show a checksum equality
overlay.

> Same input, byte-identical output. Every time. No model in the render
> loop. This is the audit trail you can hand to a compliance officer.

## Scene 5 — MCP path (2:05 – 2:30) — [Design, Potential impact]

**Screen:** a Claude / MCP-capable client connected to
`https://quire.<yourdomain>/mcp/`. Ask it: *"Render a denial letter for
Jane Doe, policy 12345, amount $842."*

**Voiceover:**
> Same rendering pipeline, called from an agent. The MCP transport returns
> a short-lived signed URL to the rendered PDF. The agent never sees the
> template internals; the render stays deterministic.

## Scene 6 — Close (2:30 – 2:50) — [Potential impact]

**Screen:** three-bullet summary card:
- Design-time AI on Nebius Token Factory (NVIDIA Nemotron)
- Deterministic render, audit-friendly
- MIT-licensed, single Docker Compose deploy

**Voiceover:**
> Quire — AI where it's cheap and reviewable, boring where it matters.
> Repo, deploy runbook, and this demo are linked below. Thanks for
> watching.

**Screen:** end card with GitHub URL and MIT badge.

---

## Editing notes

- Do NOT show the Nebius API key, session secret, or admin password on
  screen at any point. If the terminal beat exposes them, cut that frame.
- The "byte-identical" claim is verifiable: run the two renders with the
  same input JSON and diff the resulting `.docx` bytes. If the demo VM is
  under load and rendering is slow, pre-record that beat.
- Keep the total under 3:00 for the hackathon rule. If tight, cut Scene 5
  (MCP) — Scenes 1–4 and 6 are the core narrative.
