# Playwright recording — Beat 2 live-UI captures

The submission video's Beat 2 (0:18 – 1:32) is the only beat that shows the real running system. This doc covers automated capture via [`scripts/record_demo.py`](../../scripts/record_demo.py) — a Playwright-driven recorder that drives the admin UI through the exact sequence in [`video-3min.md`](video-3min.md), with dwell times that line up with the voice-over cues in [`voiceover-elevenlabs.md`](voiceover-elevenlabs.md).

The other beats (cold open, architecture, cameo, close) are motion graphics / cards — not recorded here. Build them in a video editor or generate as still frames the assembly script overlays.

---

## What gets recorded

| File | Duration | Route | Voice-over sub-beat |
| --- | :-: | --- | --- |
| `beat2a.webm` | ~14 s | `/ui/inbox` → open the applicant email | 2a — Email lands |
| `beat2c.webm` | ~20 s | `/ui/inbox/{id}/pipeline` — outcome hero + citations table + reply section | 2c — Reviewer opens the case |
| `beat2d.webm` | ~18 s | reply-attachment PDF → `/ui/programs` → program detail | 2d — Doc package reveal |
| `beat2b_alt.webm` *(opt-in)* | ~22 s | `/ui/inbox/{id}/pipeline` — slow scroll through Stage 1 → 2 → 3 | 2b fallback (if you'd rather live-UI than motion graphics) |

Output directory defaults to `assets/video/captures/` (git-ignored, safe to blow away between takes).

---

## Preconditions

You need a locally-running Verdikt stack with real seed data before recording. The pipeline detail page has nothing to render if no events have been processed.

**1. Bring the stack up**

```bash
docker compose up -d postgres redis minio minio-init
docker compose run --rm web alembic upgrade head
docker compose up -d web worker
```

**2. Seed a tenant with an inbound-config**

Any tenant works, but the shooting script narrates as if the tenant is Alderwood. Use whatever slug matches your seeded data — the recorder picks the first event it finds, so the tenant name is only reflected in what you see on screen, not in the script logic.

Minimum required for the pipeline detail page to render:
- A tenant row with `inbound_config` (`subject_tag`, etc.)
- Three frozen letter templates under that tenant (names matching `%approv%`, `%deni%`, `%missing%`)
- A doc package bound to the `approve` outcome for that tenant × program (migration `0029`)
- A program row (`programs` table, migration `0023`) with an `extraction_schema`

**3. Process at least one inbound email**

Send a test enrollment packet to `quire.template.agent@gmail.com` (or your dev-mode inbox). Wait for the worker to process it — the row in `/ui/inbox` should show a verdict badge before you run the recorder.

**4. Install Playwright**

```bash
pip install playwright
playwright install chromium
```

**5. Set the admin password env var**

```bash
export DOCGEN_ADMIN_PASSWORD='your-plaintext-admin-password'
```

The recorder logs in through the UI, so it needs the plaintext password (not the hash from `.env`). Never commit this.

---

## Running

**Record all three default beats (2a + 2c + 2d):**

```bash
python scripts/record_demo.py
```

Writes `assets/video/captures/beat2a.webm`, `beat2c.webm`, `beat2d.webm`.

**Record a single beat:**

```bash
python scripts/record_demo.py --beat 2c
```

Iterate here — tune the choreography, run, watch, adjust. Cheaper than re-running the whole batch.

**Record the Beat 2b fallback (opt-in):**

```bash
python scripts/record_demo.py --beat 2b_alt
```

Use this if you'd rather have a live-UI scroll for Beat 2b than motion graphics. Trade-off: less punchy than a proper animated pipeline diagram, but real and reproducible.

**Watch it drive the browser (debugging):**

```bash
python scripts/record_demo.py --beat 2c --headed --slow 500
```

Opens a visible Chromium window with a 500 ms delay between actions. Good for spotting when a selector didn't hit.

**Discover the event ID without recording:**

```bash
python scripts/record_demo.py --list-events
```

Prints the first candidate `event_id` from `/ui/inbox`. Handy if you want to pin a specific event with `DOCGEN_EVENT_ID=…` in future runs.

---

## Env-var reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOCGEN_BASE_URL` | `http://localhost:8000` | Root URL of the running stack. |
| `DOCGEN_ADMIN_USER` | `admin` | Admin username for the login form. |
| `DOCGEN_ADMIN_PASSWORD` | *(required)* | Plaintext admin password. |
| `DOCGEN_EVENT_ID` | *(auto)* | Pin the capture to a specific inbox event. Otherwise the first row from `/ui/inbox` is used. |
| `DEMO_OUTPUT_DIR` | `assets/video/captures` | Where the `beat*.webm` files land. |

---

## Iteration workflow

1. Run `python scripts/record_demo.py --beat 2c --headed --slow 300` and watch it drive the browser.
2. If a selector doesn't hit (element not found, wrong section scrolled to), open `scripts/record_demo.py` and adjust the CSS selector in the relevant `_choreograph_*` function. Selectors marked with fallback `page.evaluate("window.scrollBy(...)")` degrade to a blind scroll if the primary selector misses, so bad footage is more common than a hard crash.
3. Re-run just that beat. When it looks right, drop the `--headed` and record the final take clean.
4. Adjust dwell times — every `await page.wait_for_timeout(X_000)` maps directly to on-screen dwell in the final cut. If a section lands too fast to read on screen, bump the timeout.

**Dwell budgets per beat** (must sum to the target duration in the voice-over script):

| Beat | Sum of dwell timeouts | Voice-over target |
| --- | :-: | :-: |
| 2a | 13 s | ~14 s |
| 2c | 22 s (with intra-scroll motion) | ~20 s |
| 2d | 18 s | ~18 s |
| 2b_alt | 21 s | ~22 s |

Slight overruns are fine — the assembly script trims each clip to match the audio duration for that sub-beat.

---

## Common failures + fixes

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Login failed — landed on /ui/login` | Wrong password, or the login form's `name` attributes changed. | Verify `DOCGEN_ADMIN_PASSWORD`. If the form changed, update the selectors in `_login_and_capture_session`. |
| `No inbox events found` | No processed inbound emails, or the inbox list uses a different attribute than `data-event-id` / `a[href^="/ui/inbox/"]`. | Send a test enrollment packet and wait. If the DOM shape differs, update `_discover_event_id`'s `page.evaluate` snippet. |
| Recording is 0 bytes / very short | Chromium didn't launch — usually a missing `playwright install chromium` step. | Re-run `playwright install chromium`. |
| PDF beat is blank | The reply-attachment endpoint returns `Content-Disposition: attachment` (triggers a download instead of inline PDF). | Configure the endpoint to inline for demo mode, or replace the PDF step in `_choreograph_2d` with a screenshot of the attachment card. |
| Video is stretched / cropped | Viewport doesn't match `record_video_size`. | Both are set to 1920×1080 in the script; if you edit one, edit both. |
| Selector `Retrieve` / `Reply` misses | The pipeline detail page changed heading text. | Update the `has-text('...')` locators in `_choreograph_2c` to match current headings, or fall back to the blind-scroll branch that's already in the script. |

---

## What the recorder does NOT capture

- **Cursor styling.** Playwright's default cursor is fine but not polished — no smooth-motion or click-ripple effects. If you want a hand-crafted cursor, use a screen recorder (OBS) for the take you care about most.
- **Motion graphics.** Beat 2b's pipeline-diagram animation, Beat 3's applause-line overlays, Beat 4's split-screen cameo — all editorial. Build in an editor or as separate MP4 assets and the assembly script overlays them at the right timecode.
- **Audio.** The recorder never captures audio. Voice-over is generated separately from `voiceover-elevenlabs.md` and mixed in during assembly.

---

## After recording

Hand off the `.webm` files to the assembly script (to be written next). The assembly script:
- Reads each `beat*.webm` and the corresponding `audio/beat*.mp3`
- Places both at the target timecode from the shooting script
- Fills gaps with silence + placeholder cards
- Outputs a single MP4 master at `assets/video/verdikt-submission.mp4`

For now, spot-check each `.webm` in VLC / your player of choice to verify the choreography lands cleanly before generating audio.
