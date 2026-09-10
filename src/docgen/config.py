from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOCGEN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://docgen:docgen@localhost:5432/docgen"
    database_url_sync: str = "postgresql+psycopg://docgen:docgen@localhost:5432/docgen"

    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_region: str = "us-east-1"
    s3_bucket_templates: str = "docgen-templates"
    s3_bucket_documents: str = "docgen-documents"

    ai_provider: Literal["stub", "openai_compat", "nebius"] = "stub"
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    # Empty by default so provider-specific defaults win (e.g. Nebius picks a
    # current Nemotron variant). Operators override per-deploy via env.
    ai_model: str = ""
    ai_timeout_seconds: float = 60.0

    # Model used for the inbox test-verdict scoring pass. Kept separate from
    # `ai_model` because the primary design-time model (Nemotron-Nano) is a
    # reasoning model that spirals on QA tasks and exhausts its output tokens
    # on the reasoning trace, returning empty content. Nemotron-Super with
    # thinking disabled is fast + reliable for one-shot JSON verdicts.
    # Blank falls back to `ai_model`.
    ai_verdict_model: str = "nvidia/nemotron-3-super-120b-a12b"

    # Where the design-time preflight + AI mapping proposal runs.
    #   inline      — in-process, same worker as the web request (current).
    #   subprocess  — shell out to the standalone preflight_job CLI. Same
    #                 contract as the Nebius Serverless Job image, so this
    #                 mode is a local proxy for the real thing until the
    #                 Nebius Jobs API is wired.
    #   nebius_job  — submit to Nebius Serverless Jobs. NOT YET IMPLEMENTED;
    #                 selecting this raises at startup so it can't silently
    #                 fall back to inline.
    preflight_backend: Literal["inline", "subprocess", "nebius_job"] = "inline"
    preflight_subprocess_timeout_seconds: float = 300.0

    # PDF field labeler — reasoning model used by the placement editor's
    # "AI Label fields" / "AI Realign" buttons and the inbox PDF ingest.
    # Reuses DOCGEN_AI_API_KEY + DOCGEN_AI_BASE_URL. Detection itself is
    # deterministic geometry (pdfplumber) with no model call; the labeler only
    # matches already-detected rects to their printed captions using the
    # page's OCR text. Nemotron-Super is the default because it produces
    # reliable one-shot JSON without a reasoning-mode preamble.
    pdf_labeler_model: str = "nvidia/nemotron-3-super-120b-a12b"

    sync_render_max_bytes: int = Field(default=512 * 1024)
    pdf_conversion_timeout_seconds: float = 60.0

    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    # Deployment mode. Defaults to production so a forgotten DOCGEN_ENV never
    # silently downgrades a real deploy to dev semantics. Dev must opt in
    # explicitly via DOCGEN_ENV=development.
    env: Literal["development", "production"] = "production"

    # Admin portal / session auth. In production, an empty password hash
    # refuses startup (see main._startup_checks) — /ui/login otherwise 503s.
    admin_user: str = "admin"
    admin_password_hash: str = ""  # bcrypt hash; empty = admin bootstrap disabled

    # No default: forces the operator to supply a real value. Startup fails
    # with a clear pydantic ValidationError if unset, eliminating the
    # ship-the-default footgun.
    session_secret: str = Field(...)

    # HMAC secret for signed job-download tokens (the `?token=` on
    # `/jobs/<id>/result` URLs that MCP hands out). Optional — when unset we
    # fall back to `session_secret`, which is fine for single-purpose deploys
    # but couples the blast radius: rotating the session secret to invalidate
    # admin logins would also invalidate every outstanding download URL.
    # Set this to decouple the two rotation domains.
    download_token_secret: str | None = None

    # Webhooks. See session_secret above for why no default.
    webhook_signing_secret: str = Field(...)
    webhook_max_attempts: int = 5
    webhook_timeout_seconds: float = 10.0

    # Rate limits per tenant
    rate_limit_renders_per_minute: int = 60
    rate_limit_uploads_per_hour: int = 60

    # First-run dev experience — when true, generate a bootstrap API key at startup.
    bootstrap_key: bool = False

    # Security posture (§3): production toggles for cookie + transport hardening.
    # Behind a TLS-terminating proxy the app sees http:// on the wire; flip these
    # on so cookies still get Secure and HSTS still ships. Both default off so
    # local dev (plain http://localhost) keeps working.
    secure_cookies: bool = False
    hsts_enabled: bool = False

    # Comma-separated IPs / CIDRs of proxies whose X-Forwarded-For headers we
    # trust. Empty (default) = never trust XFF, always use the direct peer.
    # Trusting XFF from an untrusted source lets any caller spoof their source
    # IP for rate limiting and audit logs. Set to e.g. "10.0.0.0/8,127.0.0.1"
    # when behind a reverse proxy.
    trusted_proxies: str = ""

    # MCP transport DNS-rebinding protection: comma-separated Host header values
    # the streamable-http endpoint will accept. Empty = default (localhost only,
    # will 421 on any public hostname). Set to your public FQDN when serving
    # MCP behind a reverse proxy, e.g. "quire.example.com,localhost".
    mcp_allowed_hosts: str = ""

    # Public base URL of the deployment (scheme + host, no trailing slash), used
    # to render fully-qualified download URLs in MCP tool responses. When empty
    # the tools return relative paths only. Example: "https://quire.example.com".
    public_base_url: str = ""

    # Inbox agent (Phase 1). Off by default so a fresh clone or a deploy with
    # no mailbox credentials doesn't crash-loop the poller. Turn on only when
    # inbox_* + smtp_* below are filled in and the mailbox has been verified.
    inbox_enabled: bool = False
    # IMAP fetch — poll on an interval, mark UNSEEN messages as read on
    # successful ingest. Any provider works; a single-purpose demo account
    # with an app-specific password is the simplest option.
    inbox_imap_host: str = ""
    inbox_imap_port: int = 993
    inbox_username: str = ""
    inbox_password: str = ""
    inbox_mailbox: str = "INBOX"
    inbox_poll_interval_seconds: float = 60.0
    # Ceiling on attachment size the agent will accept per email. Anything
    # larger is logged as skipped rather than pulled into memory.
    inbox_max_attachment_bytes: int = 20 * 1024 * 1024

    # SMTP send — the reply-all path uses these credentials. Kept separate
    # from IMAP so operators can point outbound at a relay while pulling
    # inbound from a different mailbox if they want to.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    # RFC 5322 From header on outbound replies. Falls back to smtp_username
    # if empty. Keep this the same as the polled mailbox so replies thread.
    smtp_from: str = ""
    smtp_use_starttls: bool = True

    # Deep-link base used in the "Review in admin portal" line of the reply
    # email. Empty = omit the link. Usually matches public_base_url.
    admin_base_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
