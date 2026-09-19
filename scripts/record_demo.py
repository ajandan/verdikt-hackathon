"""Playwright-driven recorder for the submission video's Beat 2 live-UI clips.

Records WebM clips against a locally-running Verdikt stack, one per Beat 2
sub-beat, with scripted dwell/scroll choreographies timed to match
docs/demo-scripts/voiceover-elevenlabs.md.

Beats produced (default: 2a + 2c + 2d):
    beat2a.webm       ~14 s   Email lands              /ui/inbox
    beat2c.webm       ~20 s   Reviewer opens case      /ui/inbox/{id}/pipeline
    beat2d.webm       ~18 s   Doc package reveal       reply PDF + /ui/programs

Optional live-UI fallback for Beat 2b (originally editorial motion graphics):
    beat2b_alt.webm   ~22 s   Pipeline stages          /ui/inbox/{id}/pipeline (slow scroll)

Beats 1, 3, 4, 5 are motion graphics / static cards and are NOT recorded here.
See docs/demo-scripts/playwright-recording.md for setup + iteration workflow.

Preconditions:
    - docker compose stack up (postgres, redis, minio, web, worker)
    - Alderwood tenant seeded, doc package bound for the approve outcome
    - At least one processed inbox event with pap_decision populated
    - Playwright installed: pip install playwright && playwright install chromium
    - DOCGEN_ADMIN_PASSWORD env var set (plaintext — the script logs in)

Usage:
    python scripts/record_demo.py                     # 2a + 2c + 2d
    python scripts/record_demo.py --beat 2c
    python scripts/record_demo.py --beat 2b_alt       # opt-in Beat 2b fallback
    python scripts/record_demo.py --headed --slow 500 # watch it drive the browser
    python scripts/record_demo.py --list-events       # print candidate event IDs

Env vars:
    DOCGEN_BASE_URL       default http://localhost:8000
    DOCGEN_ADMIN_USER     default admin
    DOCGEN_ADMIN_PASSWORD required (plaintext)
    DOCGEN_EVENT_ID       optional; auto-discovered from /ui/inbox if unset
    DEMO_OUTPUT_DIR       default assets/video/captures
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import Browser, Page, async_playwright

BASE_URL = os.environ.get("DOCGEN_BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_USER = os.environ.get("DOCGEN_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("DOCGEN_ADMIN_PASSWORD")
EVENT_ID_OVERRIDE = os.environ.get("DOCGEN_EVENT_ID")
OUTPUT_DIR = Path(os.environ.get("DEMO_OUTPUT_DIR", "assets/video/captures"))
VIEWPORT = {"width": 1920, "height": 1080}


def _log(msg: str) -> None:
    print(f"[record_demo] {msg}", file=sys.stderr, flush=True)


async def _login_and_capture_session(browser: Browser) -> dict:
    """Log in once, return storage state to reuse across per-beat contexts."""
    context = await browser.new_context(viewport=VIEWPORT)
    page = await context.new_page()
    _log(f"logging in at {BASE_URL}/ui/login as {ADMIN_USER}")
    await page.goto(f"{BASE_URL}/ui/login", wait_until="networkidle")
    # Adjust selectors here if the login form uses different names.
    await page.fill('input[name="username"]', ADMIN_USER)
    await page.fill('input[name="password"]', ADMIN_PASSWORD or "")
    await page.click('button[type="submit"]')
    await page.wait_for_load_state("networkidle")
    if "/login" in page.url:
        raise RuntimeError(
            "Login failed — verify DOCGEN_ADMIN_USER / DOCGEN_ADMIN_PASSWORD "
            f"(landed on {page.url})"
        )
    storage = await context.storage_state()
    await context.close()
    return storage


async def _discover_event_id(browser: Browser, storage: dict) -> str:
    """Find an inbox event whose pipeline detail page exists (i.e. has pap_decision).

    Nav links like `/ui/inbox/humanloop` and non-event slugs are skipped —
    the discovery only considers UUID-shaped path segments. Then each candidate
    is probed until one returns a non-404 pipeline page.
    """
    import re as _re

    context = await browser.new_context(viewport=VIEWPORT, storage_state=storage)
    page = await context.new_page()
    await page.goto(f"{BASE_URL}/ui/inbox", wait_until="networkidle")
    candidates = await page.evaluate(
        """
        () => {
            const seen = new Set();
            const out = [];
            for (const a of document.querySelectorAll('a[href*="/ui/inbox/"]')) {
                const m = a.getAttribute('href').match(/\\/ui\\/inbox\\/([^/?#]+)/);
                if (!m) continue;
                const slug = m[1];
                if (seen.has(slug)) continue;
                seen.add(slug);
                out.push(slug);
            }
            return out;
        }
        """
    )
    uuid_re = _re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    candidates = [c for c in candidates if uuid_re.match(c)]
    _log(f"discovered {len(candidates)} UUID-shaped event candidates")
    for slug in candidates:
        resp = await page.goto(f"{BASE_URL}/ui/inbox/{slug}/pipeline", wait_until="load")
        if resp and resp.status == 200:
            await context.close()
            return slug
        _log(f"  skip {slug} (pipeline page returned {resp.status if resp else 'no response'})")
    await context.close()
    raise RuntimeError(
        "No inbox event has a pipeline detail page — send a test enrollment "
        "packet and wait for the pipeline to process it before recording."
    )


# ---- Choreographies (dwell budgets sum to the target beat duration) --------


async def _choreograph_2(page: Page, _event_id: str) -> None:
    """Beat 2 — Program page: schema, rules, and reply bindings (~15 s dwell).

    Narration timing (Liam @ 1.05x, beat2.mp3 = 15.05 s):
      0:00-0:04  "In Verdikt, an assistance program is data."
      0:04-0:07  "Each program defines the fields to extract from the intake"  <- Schema
      0:07-0:09  "the rules to check eligibility"                              <- Rules
      0:09-0:12  "and the reply templates to send"                             <- Bindings
      0:12-0:15  "Onboarding a new program is filling out a form..."           <- hold Bindings

    Transitions are timed so each tab is on-screen while the narration names it.
    """
    await page.goto(f"{BASE_URL}/ui/programs", wait_until="load")
    await page.wait_for_timeout(600)  # brief programs list
    first_program = page.locator("a[href*='/ui/programs/']").first
    if not await first_program.count():
        _log("no program row found on /ui/programs; recording landing only")
        await page.wait_for_timeout(15_000)
        return
    program_href = await first_program.get_attribute("href")
    await page.goto(f"{BASE_URL}{program_href}", wait_until="load")
    await page.wait_for_timeout(200)  # settle briefly — page just loaded, Schema visible

    async def _jump_to(section_id: str) -> None:
        # Prefer clicking the actual tab anchor — the page's own JS then
        # highlights the tab and scrolls to the matching section. Fall back
        # to any element whose id or [data-section] matches.
        link = page.locator(f'a[href="#{section_id}"]').first
        if await link.count():
            try:
                await link.scroll_into_view_if_needed()
                await link.click()
                return
            except Exception:
                pass
        await page.evaluate(
            f"""
            () => {{
                const candidates = [
                    '#{section_id}',
                    '#section-{section_id}',
                    '#{section_id}-section',
                    '[data-section=\"{section_id}\"]',
                    '[data-tab=\"{section_id}\"]',
                    'section[aria-labelledby=\"tab-{section_id}\"]',
                ];
                for (const sel of candidates) {{
                    const el = document.querySelector(sel);
                    if (el) {{ el.scrollIntoView({{behavior: 'smooth', block: 'start'}}); return; }}
                }}
            }}
            """
        )

    # Silencedetect on Nate's beat2.mp3 gives narration cues (in output time):
    #   1.2-3.67s   "In Verdikt, an assistance program is data."
    #   3.67-6.55s  "Each program defines the fields to extract from the intake"
    #   6.55-8.81s  "the rules to check eligibility"                             <- Rules
    #   8.81-11.06s "and the reply templates to send"                            <- Bindings
    #   11.06-16s   "Onboarding a new program is filling out a form..."         <- hold
    # Assembly trims first 0.7s of capture (-ss 0.7), so capture-time = output + 0.7.
    # Setup ends at capture t=~3.3s -> output ~2.6s.

    # Schema dwell — cover "fields to extract" narration (output 2.6s to 6.5s)
    await page.wait_for_timeout(3_900)
    # Rules — cover "rules to check" narration (output 6.5s to 9.0s)
    await _jump_to("rules")
    await page.wait_for_timeout(2_500)
    # Reply bindings — cover "reply templates" + "onboarding" hold (output 9.0s to 16s)
    await _jump_to("bindings")
    await page.wait_for_timeout(7_300)


async def _choreograph_4(page: Page, event_id: str) -> None:
    """Beat 4 — Pipeline runs → merged PDF reveal (~23 s dwell, 25 s beat).

    Narration timing (Liam @ 1.05x, beat4.mp3 = 22.85 s):
      0:00-0:03  "When a case arrives, the pipeline runs."
      0:03-0:06  "Three NVIDIA models, one per stage."               -> outcome hero
      0:06-0:10  "Extraction reads the intake into structured fields"-> Extract slots
      0:10-0:13  "Retrieval surfaces the eligibility rules"          -> Retrieve citations
      0:13-0:16  "produces a structured verdict — approve, deny..."   -> verdict + rationale
      0:16-0:23  "with citations back to the rules that fired"       -> merged PDF scroll
    """

    url = f"{BASE_URL}/ui/inbox/{event_id}/pipeline"
    await page.goto(url, wait_until="load")
    await page.evaluate(
        """
        () => {
            for (const el of document.querySelectorAll('section, div, article')) {
                const h = el.querySelector('h1, h2, h3, h4');
                if (h && /inbound attachments/i.test(h.textContent || '')) {
                    el.style.display = 'none';
                }
            }
            // Rename sub-stage headings to the 4-stage vocabulary
            // (Ingest / Decide / Deliver / Remember) that the intro sets up.
            const renames = [
                [/Extract\\s*[—\\-–]\\s*structured intake/i, 'Ingest — structured intake'],
                [/Extract/g, 'Ingest'],
                [/Retrieve\\s*[→\\->]\\s*Reason\\s*[→\\->]\\s*rule citations/i, 'Decide — rule citations'],
                [/Retrieve\\s*[→\\->]\\s*Reason/i, 'Decide'],
                [/Queued reply/i, 'Deliver — queued reply'],
                [/Downstream JSON verdict/i, 'Deliver — downstream JSON verdict'],
            ];
            for (const el of document.querySelectorAll('h1, h2, h3, h4')) {
                let t = el.textContent || '';
                for (const [rx, rep] of renames) t = t.replace(rx, rep);
                if (t !== el.textContent) el.textContent = t;
            }
        }
        """
    )
    # Narration cues (Liam @1.05, beat4.mp3 = 25.87s):
    #  0:03  "Three NVIDIA models, one per stage"
    #  0:05  "nv-ingest reads the intake into structured fields"      -> Extract section
    #  0:09  "NeMo Retriever NIMs surface the eligibility rules"      -> Retrieve citations
    #  0:14  "Nemotron Super evaluates ... structured verdict"         -> verdict/reply
    #  0:20  "with citations back to the rules that fired"
    # PDF viewer takes over from ~t=17 through end.
    # Absolute Y positions probed from the pipeline detail page (after hiding
    # Inbound attachments): Extract H2 at 513px, Retrieve->Reason H2 at 2737px,
    # Missing info H2 at 3287px, Queued reply H2 at 3438px, Downstream JSON
    # verdict H2 at 3634px. Deterministic scrollTo per stage instead of
    # heading-text search (the text-based jumps silently missed the → glyph
    # in the actual heading).
    await page.wait_for_timeout(2_500)   # 0-2.5s: outcome hero + verdict badge

    # Ingest (MiniCPM-V) chip 4-8s -> Extract/Ingest section
    await page.evaluate("window.scrollTo({top: 450, behavior: 'smooth'})")
    await page.wait_for_timeout(5_500)   # 2.5-8s

    # NeMo Retriever chip 8-11s -> Decide → rule citations
    await page.evaluate("window.scrollTo({top: 2680, behavior: 'smooth'})")
    await page.wait_for_timeout(3_000)   # 8-11s

    # Nemotron Super chip 11-15s -> verdict rationale + Deliver preview
    await page.evaluate("window.scrollTo({top: 3320, behavior: 'smooth'})")
    await page.wait_for_timeout(4_500)   # 11-15.5s

    # 15.5-20s: dwell on Downstream JSON verdict (Deliver output)
    await page.evaluate("window.scrollTo({top: 3800, behavior: 'smooth'})")
    await page.wait_for_timeout(4_500)   # 15.5-20s


async def _choreograph_6(page: Page, _event_id: str) -> None:
    """Beat 6 — Cases inbox showing mixed approve/deny badges (~11 s dwell)."""
    await page.goto(f"{BASE_URL}/ui/inbox", wait_until="networkidle")
    await page.wait_for_timeout(3_000)
    # Try clicking "REVIEW" or "Rejected" tab if present to surface denials.
    tab = page.locator("button:has-text('Rejected'), a:has-text('Rejected')").first
    if await tab.count():
        await tab.click()
        await page.wait_for_timeout(2_500)
    await page.evaluate("window.scrollBy({top: 200, left: 0, behavior: 'smooth'})")
    await page.wait_for_timeout(3_000)


async def _choreograph_7(page: Page, event_id: str) -> None:
    """Beat 7 — Audit tab: AI stages · human review · system render (~8 s dwell).

    Navigates to the dedicated /audit tab (not /pipeline), which is where the
    who-did-what timeline lives.
    """
    url = f"{BASE_URL}/ui/inbox/{event_id}/audit"
    await page.goto(url, wait_until="networkidle")
    await page.wait_for_timeout(3_500)
    await page.evaluate("window.scrollBy({top: 300, left: 0, behavior: 'smooth'})")
    await page.wait_for_timeout(4_500)


BEAT_CHOREOGRAPHIES = {
    "2": _choreograph_2,
    "4": _choreograph_4,
    "6": _choreograph_6,
    "7": _choreograph_7,
}

DEFAULT_BEATS = ["2", "4", "6", "7"]


# ---- Recording harness ------------------------------------------------------


async def _record(browser: Browser, storage: dict, beat: str, event_id: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    context = await browser.new_context(
        viewport=VIEWPORT,
        storage_state=storage,
        record_video_dir=str(OUTPUT_DIR),
        record_video_size=VIEWPORT,
    )
    page = await context.new_page()
    choreo = BEAT_CHOREOGRAPHIES[beat]
    _log(f"recording beat{beat} against event_id={event_id}")
    await choreo(page, event_id)  # type: ignore[misc]
    tmp_path = await page.video.path()
    await context.close()  # finalizes video file
    final = OUTPUT_DIR / f"beat{beat}.webm"
    if final.exists():
        final.unlink()
    Path(tmp_path).rename(final)
    _log(f"wrote {final}")
    return final


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Record Beat 2 live-UI clips for the submission video. "
            "See docs/demo-scripts/playwright-recording.md for setup."
        )
    )
    parser.add_argument(
        "--beat",
        choices=[*BEAT_CHOREOGRAPHIES, "all"],
        default="all",
        help="Which beat to record (default: 2a + 2c + 2d; 2b_alt is opt-in).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window during recording (debugging).",
    )
    parser.add_argument(
        "--slow",
        type=int,
        default=0,
        help="Playwright slow_mo in ms — inserts a delay between each action.",
    )
    parser.add_argument(
        "--list-events",
        action="store_true",
        help="Print the auto-discovered event_id and exit without recording.",
    )
    args = parser.parse_args()

    if not ADMIN_PASSWORD:
        _log("DOCGEN_ADMIN_PASSWORD is required (plaintext).")
        sys.exit(2)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not args.headed,
            slow_mo=args.slow,
        )
        try:
            storage = await _login_and_capture_session(browser)
            event_id = EVENT_ID_OVERRIDE or await _discover_event_id(browser, storage)
            _log(f"using event_id={event_id}")

            if args.list_events:
                print(event_id)
                return

            beats = DEFAULT_BEATS if args.beat == "all" else [args.beat]
            for beat in beats:
                await _record(browser, storage, beat, event_id)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
