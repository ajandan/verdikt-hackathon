# ---- CSS build stage ---------------------------------------------------------
# Uses the standalone tailwindcss binary (bundles @tailwindcss/forms and
# @tailwindcss/typography), so no Node toolchain is needed in the build.
FROM debian:bookworm-slim AS css-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG TAILWIND_VERSION=3.4.15
RUN curl -fsSL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-x64" \
    && chmod +x /usr/local/bin/tailwindcss

WORKDIR /build
COPY tailwind.config.js ./
COPY src/docgen/ui/templates ./src/docgen/ui/templates
COPY src/docgen/ui/tailwind.src.css ./src/docgen/ui/tailwind.src.css
RUN tailwindcss \
        -c tailwind.config.js \
        -i ./src/docgen/ui/tailwind.src.css \
        -o ./src/docgen/ui/static/app.css \
        --minify

# ---- Python runtime ----------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
        libreoffice-core \
        libreoffice-writer \
        fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY sample_templates ./sample_templates
COPY reports ./reports

# Overlay the compiled Tailwind bundle produced in the css-builder stage.
COPY --from=css-builder /build/src/docgen/ui/static/app.css ./src/docgen/ui/static/app.css

RUN pip install --upgrade pip && pip install -e .

# Run as an unprivileged user so any RCE inside FastAPI / docxtpl / LibreOffice
# lands as UID 10001, not root. `chown -R` after the install so pyc caches
# written on first import don't fail on read-only /app.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home app \
    && chown -R app:app /app
USER 10001:10001

EXPOSE 8000

CMD ["docgen-web"]

# ---- Dev image (tests + linters) ---------------------------------------------
# Layered on top of the runtime image so prod stays lean. Wired by the `test`
# service in docker-compose.override.yml (profile: test). Adds `pytest`,
# `pytest-asyncio`, `ruff`, `pip-audit`, and `pip-licenses` — the same set the
# CI `[dev]` extras install.
FROM base AS dev
USER root
RUN pip install -e ".[dev]"
# The editable reinstall may rewrite egg-info under /app owned by root.
RUN chown -R app:app /app
USER 10001:10001
CMD ["pytest", "-q"]
