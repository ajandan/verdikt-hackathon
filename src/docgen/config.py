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

    # Alternate labeler: Mistral OCR (placement editor "OCR Label" button).
    # Independent of ai_provider — set the key to enable, leave blank to hide
    # the button in the UI.
    mistral_api_key: str | None = None
    mistral_ocr_model: str = "mistral-ocr-latest"
    # Vision-capable chat model that reads the OCR transcript + annotated PNG
    # and matches numbered widgets to captions.
    mistral_chat_model: str = "pixtral-12b-2409"
    mistral_timeout_seconds: float = 180.0

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
