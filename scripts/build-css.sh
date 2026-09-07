#!/usr/bin/env bash
# scripts/build-css.sh
# Compile Tailwind CSS on Linux/macOS. Downloads the standalone tailwindcss
# binary on first run (~30 MB, cached in .tailwind/) — no Node required.
#
# Usage:
#   ./scripts/build-css.sh          # one-shot minified build
#   ./scripts/build-css.sh --watch  # rebuild on template change

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="$ROOT/.tailwind"
TAILWIND_VERSION="${TAILWIND_VERSION:-3.4.15}"

case "$(uname -sm)" in
    "Darwin arm64")  ASSET="tailwindcss-macos-arm64" ;;
    "Darwin x86_64") ASSET="tailwindcss-macos-x64" ;;
    "Linux aarch64") ASSET="tailwindcss-linux-arm64" ;;
    "Linux x86_64")  ASSET="tailwindcss-linux-x64" ;;
    *) echo "Unsupported platform: $(uname -sm)"; exit 1 ;;
esac

BIN="$BIN_DIR/tailwindcss"
if [ ! -x "$BIN" ]; then
    echo "Downloading standalone tailwindcss v${TAILWIND_VERSION} ($ASSET)..."
    mkdir -p "$BIN_DIR"
    curl -fsSL -o "$BIN" \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/${ASSET}"
    chmod +x "$BIN"
    echo "Cached at $BIN"
fi

ARGS=(
    -c "$ROOT/tailwind.config.js"
    -i "$ROOT/src/docgen/ui/tailwind.src.css"
    -o "$ROOT/src/docgen/ui/static/app.css"
)
if [ "${1:-}" = "--watch" ] || [ "${1:-}" = "-w" ]; then
    ARGS+=(--watch)
else
    ARGS+=(--minify)
fi

exec "$BIN" "${ARGS[@]}"
