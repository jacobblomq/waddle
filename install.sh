#!/usr/bin/env bash
# Waddle installer (Phase 1). Idempotent. Never uses root: it only touches your user session
# and Waddle's own files. uninstall.sh removes exactly what this creates.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="io.github.jacobblomq.Waddle"

case "${HOME:-}" in /*) ;; *) echo "HOME must be an absolute path." >&2; exit 1 ;; esac
# XDG says to ignore relative values.
case "${XDG_DATA_HOME:-}" in /*) DATA_HOME="$XDG_DATA_HOME" ;; *) DATA_HOME="$HOME/.local/share" ;; esac
DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.desktop"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1 — $2" >&2; exit 1; }; }

need uv "install it from https://docs.astral.sh/uv/ (no root needed)"

# The path goes into a desktop entry's Exec= line; refuse characters that would need
# Desktop Entry escaping rather than risk a wrong launcher.
case "$REPO" in
    *[\"\`\$\\%\&\|]*|*$'\n'*)
        echo "Please move the Waddle checkout to a path without \" \` \$ \\ % & | characters." >&2
        exit 1 ;;
esac

say "1/3 Python environment"
(cd "$REPO" && uv sync --no-dev)
WADDLE_BIN="$REPO/.venv/bin/waddle"
[ -x "$WADDLE_BIN" ] || { echo "waddle entry point was not created" >&2; exit 1; }

say "2/3 Desktop entry"
mkdir -p "$(dirname "$DESKTOP_FILE")"
sed "s|@WADDLE_BIN@|$WADDLE_BIN|" "$REPO/kwin/$APP_ID.desktop.in" > "$DESKTOP_FILE"

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
