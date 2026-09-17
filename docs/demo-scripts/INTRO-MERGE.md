# INTRO-MERGE — one-shot patch for `scripts/assemble_video.sh`

Prepend the four intro beats (`0a` title card, `0b` PAP domain, `0c` AI-in/deterministic-out
thesis, `0d` end-to-end architecture) to the front of the beat table.

The three arrays below stay 1:1 with each other and 1:1 with the existing beats. Just swap
the whole block — `BEAT_IDS`, `BEAT_DURATIONS`, `BEAT_TITLES`, `BEAT_SUBTITLES` — for the
`# ---- Beat table` section as-is.

## Measured audio (ffprobe · after generation on 2026-09-15)

| Beat | Audio (s) | Target (s) | Tail (s) | Notes |
| :--: | :-------: | :--------: | :------: | ----- |
| 0a   | 3.90      | **5**      | 1.10     | Held frame — spec says ~5s title card |
| 0b   | 12.86     | **13**     | 0.14     | Tight — 12.86 &lt; 13 + 0.3 so no trim warning |
| 0c   | 9.89      | **11**     | 1.11     | |
| 0d   | 8.45      | **9**      | 0.55     | |

**Intro subtotal:** `5 + 13 + 11 + 9 = 38 s`
**Body (unchanged):** `146 s`
**New total:** `184 s = 3:04`

> 3:04 sits 4 s over the 3:00 hackathon cap. The measured intro (38 s) is inside the
> spec's 32-38 s window. Per the task brief, the operator will trim middle-beat targets
> slightly (e.g., knock 4 s off beats 4/5) to land exactly at 180 s.

## Drop-in replacement for the `# ---- Beat table` block

Replace the current three arrays around line 65 in `scripts/assemble_video.sh` with:

```bash
BEAT_IDS=(0a 0b 0c 0d 1 2 3 3b 4 5 6 7 8)
# Story arc: intro (title -> PAP domain -> AI-in/deterministic-out thesis -> architecture)
# then intake -> program -> doc package -> outcome->package mapping -> pipeline ->
# deterministic -> cameo -> audit -> CTA.
# Intro targets: 5+13+11+9=38s; body unchanged 146s; total 184s = 3:04.
BEAT_DURATIONS=(5 13 11 9 14 17 18 19 27 16 13 10 12)   # sum = 184s = 3:04
BEAT_TITLES=(
    "VERDIKT"
    "PATIENT ASSISTANCE PROGRAMS"
    "AI IN, DETERMINISTIC OUT"
    "END-TO-END"
    "INTAKE DOCUMENTS"
    "PROGRAM AS DATA"
    "DOC PACKAGE"
    "OUTCOME MAPPING"
    "PIPELINE"
    "THEN AI STOPS"
    "OPPOSITE VERDICT"
    "AUDITABLE"
    "VERDIKT"
)
BEAT_SUBTITLES=(
    "Nebius × NVIDIA · Global AI Hackathon 2026"
    "thousands apply · each response a compliance-audited letter"
    "AI reads · rules decide · templates ship"
    "every channel · one pipeline · every outcome traced"
    "everything the case begins with"
    "fields | rules | reply templates"
    "human-approved | frozen | forever"
    "operator declares the mapping"
    "extract | retrieve | reason"
    "byte-identical merge"
    "same code | opposite outcome"
    "audit log | reproducible"
    "MIT | repo | demo | dashboards"
)
```

## Sanity-check checklist before running `bash scripts/assemble_video.sh`

- [ ] `assets/video/designs/beat0{a,b,c,d}.html` exist
- [ ] `assets/video/screenshots/beat0{a,b,c,d}_primary.png` exist (1920×1080 each)
- [ ] `assets/video/audio/beat0{a,b,c,d}.mp3` exist (measured above)
- [ ] Old segments cleared: `rm -f assets/video/segments/beat0*.mp4`
- [ ] `docs/demo-scripts/voiceover-elevenlabs.md` has the four new `## Beat 0a` … `## Beat 0d` sections

The `render_image_beat` path in the assembly script already handles PNG screenshots —
no code changes needed beyond the array swap above. The `body::before` Verdikt mark in
`common.css` renders on all four intros automatically (0a suppresses it in favor of the
large centered mark; 0b/0c/0d inherit the small top-right mark).

## Optional: land exactly at 3:00 (180 s)

If you want to trim the body to make total = 180 s, drop 4 s across the middle beats.
Suggested distribution:

```bash
# Cut 2s from beat 4 (pipeline, currently 27s) and 2s from beat 5 (then AI stops,
# currently 16s). Both have generous tails today.
BEAT_DURATIONS=(5 13 11 9 14 17 18 19 25 14 13 10 12)   # sum = 180s = 3:00
```

Verify with:

```bash
python -c "print(sum([5,13,11,9,14,17,18,19,25,14,13,10,12]))"   # 180
```
