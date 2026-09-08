"""Dramatiq actor + self-scheduling poller entrypoint for the inbox agent.

Design:
  - `poll_inbox` is a single actor that connects to the mailbox, fetches
    UNSEEN, processes each, and re-enqueues itself with a delay equal to
    `settings.inbox_poll_interval_seconds`. Simpler than adding a real
    scheduler (APScheduler etc.) and keeps the whole loop inside Redis.
  - On startup the worker calls `bootstrap_inbox_poller()` which enqueues
    the first tick — but ONLY when `settings.inbox_enabled` is true and
    the IMAP + SMTP knobs look filled in. Avoids a crash-loop when a
    fresh deploy hasn't set credentials yet.

Cluster-wide serialization (two Redis locks):
  - BOOTSTRAP_LOCK protects the initial enqueue on cluster boot. With N
    worker processes, only the first to SET NX wins; the rest skip.
    The lock TTL is refreshed on every successful poll so a healthy
    chain keeps ownership; if the chain dies, TTL expires and the next
    bootstrap can restart it.
  - POLL_LOCK protects each individual poll tick as defense-in-depth
    (e.g. against stale delayed messages left in Redis from before the
    dedup fix). A poll that fails to acquire this lock no-ops and does
    NOT re-schedule (the active poll will).

Each processed message is marked \\Seen only after the InboxEvent row is
committed. A crash mid-poll leaves any un-committed messages UNSEEN so
the next tick retries them; a message that produced an event but crashed
before ack stays UNSEEN too, which is safe because the unique
constraint on message_id makes the retry idempotent.
"""
from __future__ import annotations

import dramatiq
import structlog

from docgen.config import get_settings
from docgen.inbox.agent import dispatch_message
from docgen.inbox.imap_poller import ImapError, ImapPoller
from docgen.jobs.broker import _broker
from docgen.store.db import sync_session
from docgen.store.models import InboxEvent

log = structlog.get_logger(__name__)

BOOTSTRAP_LOCK_KEY = "docgen:inbox:bootstrap"
POLL_LOCK_KEY = "docgen:inbox:poll_active"


def _config_ready() -> bool:
    s = get_settings()
    if not s.inbox_enabled:
        return False
    required = [s.inbox_imap_host, s.inbox_username, s.inbox_password, s.smtp_host]
    return all(bool(x) for x in required)


def _bootstrap_ttl() -> int:
    # Generous — 3x poll interval, min 5 min — so a healthy chain always
    # refreshes before expiry but a dead chain releases in reasonable time.
    return max(300, int(get_settings().inbox_poll_interval_seconds * 3))


def _poll_lock_ttl() -> int:
    # 1.5x poll interval — long enough that a slow poll (30s IMAP timeout
    # + processing) holds the lock through completion, short enough that
    # a crashed worker releases it before the next tick fires.
    return max(120, int(get_settings().inbox_poll_interval_seconds * 1.5))


@dramatiq.actor(max_retries=0, time_limit=5 * 60 * 1000)
def poll_inbox() -> None:
    """One poll tick: fetch UNSEEN, process each, then re-enqueue."""
    settings = get_settings()
    if not settings.inbox_enabled:
        log.info("inbox_poll_disabled")
        return

    # Defense-in-depth: if another poll tick is already running (e.g. a
    # stale delayed message from before the bootstrap dedup landed), no-op
    # and don't re-schedule — the active tick will.
    acquired = _broker.client.set(POLL_LOCK_KEY, "1", nx=True, ex=_poll_lock_ttl())
    if not acquired:
        log.info("inbox_poll_skipped", reason="another_poll_in_flight")
        return

    try:
        with ImapPoller(settings) as poller:
            for msg in poller.fetch_unseen():
                # Dedupe: if we've already produced an event for this Message-ID
                # (crash + retry), skip re-processing but still ack the mail so
                # it doesn't stay UNSEEN forever.
                with sync_session() as session:
                    existing = (
                        session.query(InboxEvent)
                        .filter(InboxEvent.message_id == msg.message_id)
                        .first()
                    )
                    if existing is not None and existing.status in ("succeeded", "failed", "skipped"):
                        if msg.uid is not None:
                            poller.mark_seen(msg.uid)
                        log.info(
                            "inbox_duplicate_skipped",
                            message_id=msg.message_id,
                            existing_status=existing.status,
                        )
                        continue

                with sync_session() as session:
                    try:
                        result = dispatch_message(session, msg=msg, settings=settings)
                        log.info(
                            "inbox_processed",
                            event_id=str(result.event_id),
                            status=result.status,
                            detail=result.detail,
                        )
                    except Exception:
                        log.exception(
                            "inbox_process_crash", message_id=msg.message_id
                        )
                        # Do NOT mark \\Seen on crash — next tick retries.
                        continue

                # Only ack the mailbox after the event row is committed.
                if msg.uid is not None:
                    poller.mark_seen(msg.uid)
    except ImapError as e:
        log.warning("inbox_poll_imap_error", error=str(e))
    finally:
        # Refresh the bootstrap lock so a healthy chain retains ownership
        # against future worker restarts. If this chain dies, the TTL
        # expires and the next bootstrap can start a fresh chain.
        try:
            _broker.client.set(BOOTSTRAP_LOCK_KEY, "1", ex=_bootstrap_ttl())
        except Exception as e:
            log.warning("inbox_lock_refresh_failed", error=str(e))
        # Release the per-poll lock and schedule the next tick. Even on
        # error we keep polling — transient IMAP failures shouldn't stop
        # the loop.
        try:
            _broker.client.delete(POLL_LOCK_KEY)
        except Exception:
            pass
        _schedule_next_tick()


def _schedule_next_tick() -> None:
    settings = get_settings()
    if not settings.inbox_enabled:
        return
    delay_ms = int(max(1.0, settings.inbox_poll_interval_seconds) * 1000)
    poll_inbox.send_with_options(delay=delay_ms)


def bootstrap_inbox_poller() -> None:
    """Enqueue the first tick on worker startup, if config is ready.

    Cluster-wide dedup: only the first worker to acquire BOOTSTRAP_LOCK
    enqueues. The others log and skip so we don't spawn N parallel poll
    chains when N worker processes boot together.
    """
    if not _config_ready():
        log.info("inbox_poller_not_bootstrapped", reason="config_incomplete")
        return
    try:
        acquired = _broker.client.set(
            BOOTSTRAP_LOCK_KEY, "1", nx=True, ex=_bootstrap_ttl()
        )
    except Exception as e:
        # If Redis is unreachable, fall through to enqueue anyway — better
        # to risk a duplicate than to leave the poller silent.
        log.warning("inbox_bootstrap_lock_failed", error=str(e))
        acquired = True
    if not acquired:
        log.info("inbox_poller_bootstrap_skipped", reason="another_worker_holds_lock")
        return
    poll_inbox.send()
    log.info("inbox_poller_bootstrapped")
