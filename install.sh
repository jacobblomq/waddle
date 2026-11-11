#!/usr/bin/env bash
# Waddle installer (Phase 1). Idempotent. Never uses root: it only touches your user session
# and Waddle's own files, and records what it created so uninstall.sh can remove exactly that.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_ID="io.github.jacobblomq.Waddle"
DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.desktop"
MANIFEST="$DATA_HOME/waddle/install-manifest.txt"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1 — $2" >&2; exit 1; }; }

need uv "install it from https://docs.astral.sh/uv/ (no root needed)"

say "1/3 Python environment"
(cd "$REPO" && uv sync --no-dev)
WADDLE_BIN="$REPO/.venv/bin/waddle"
[ -x "$WADDLE_BIN" ] || { echo "waddle entry point was not created" >&2; exit 1; }

say "2/3 Desktop entry"
mkdir -p "$(dirname "$DESKTOP_FILE")" "$(dirname "$MANIFEST")"
chmod 700 "$(dirname "$MANIFEST")"
sed "s|@WADDLE_BIN@|$WADDLE_BIN|" "$REPO/kwin/$APP_ID.desktop.in" > "$DESKTOP_FILE"
grep -qxF "$DESKTOP_FILE" "$MANIFEST" 2>/dev/null || echo "$DESKTOP_FILE" >> "$MANIFEST"

say "3/3 KWin window rule"
if command -v kwriteconfig6 >/dev/null 2>&1 && command -v kreadconfig6 >/dev/null 2>&1; then
    "$WADDLE_BIN" rule install
else
    echo "kwriteconfig6/kreadconfig6 not found: skipping the window rule."
    echo "Waddle will still run, but will not stay above other windows."
fi

say "Done."
echo "Try it:   $WADDLE_BIN start --demo"
echo "Remove:   $REPO/uninstall.sh"
