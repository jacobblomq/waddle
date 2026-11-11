#!/usr/bin/env bash
# Removes what install.sh created: the window rule, the desktop entry, and (with --purge)
# Waddle's own config, data and cache. Never uses root.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
MANIFEST="$DATA_HOME/waddle/install-manifest.txt"
WADDLE_BIN="$REPO/.venv/bin/waddle"

if [ -x "$WADDLE_BIN" ]; then
    "$WADDLE_BIN" stop || true
    if command -v kwriteconfig6 >/dev/null 2>&1 && command -v kreadconfig6 >/dev/null 2>&1; then
        "$WADDLE_BIN" rule remove || echo "Could not remove the window rule automatically." >&2
    fi
fi

if [ -f "$MANIFEST" ]; then
    while IFS= read -r path; do
        # Only ever delete Waddle's own desktop entries, whatever the manifest says.
        case "$path" in
            "$DATA_HOME"/applications/io.github.jacobblomq.Waddle*.desktop) rm -f -- "$path" ;;
        esac
    done < "$MANIFEST"
    rm -f -- "$MANIFEST"
fi

if [ "${1:-}" = "--purge" ]; then
    rm -rf -- "$CONFIG_HOME/waddle" "$DATA_HOME/waddle" "$CACHE_HOME/waddle"
    echo "Removed Waddle's config, data and cache."
else
    echo "Kept your Waddle config and data. Run with --purge to delete them too."
fi
echo "Uninstalled."
