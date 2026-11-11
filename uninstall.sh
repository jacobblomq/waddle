#!/usr/bin/env bash
# Removes what install.sh created: the window rule, the desktop entry, and (with --purge)
# Waddle's own config, data and cache. Never uses root.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="io.github.jacobblomq.Waddle"

case "${HOME:-}" in /*) ;; *) echo "HOME must be an absolute path." >&2; exit 1 ;; esac
# XDG says to ignore relative values. An empty or relative value must never reach rm.
case "${XDG_DATA_HOME:-}" in /*) DATA_HOME="$XDG_DATA_HOME" ;; *) DATA_HOME="$HOME/.local/share" ;; esac
case "${XDG_CONFIG_HOME:-}" in /*) CONFIG_HOME="$XDG_CONFIG_HOME" ;; *) CONFIG_HOME="$HOME/.config" ;; esac
case "${XDG_CACHE_HOME:-}" in /*) CACHE_HOME="$XDG_CACHE_HOME" ;; *) CACHE_HOME="$HOME/.cache" ;; esac

WADDLE_BIN="$REPO/.venv/bin/waddle"
# Written by `waddle rule install`: the only record of what Waddle added to kwinrulesrc.
RULES_MANIFEST="$DATA_HOME/waddle/kwin-rules.json"

if [ -x "$WADDLE_BIN" ]; then
    "$WADDLE_BIN" stop || true
fi

rule_problem=""
if [ -f "$RULES_MANIFEST" ]; then
    if [ ! -x "$WADDLE_BIN" ]; then
        rule_problem="the Python environment is missing (run ./install.sh first)"
    elif ! command -v kwriteconfig6 >/dev/null 2>&1 || ! command -v kreadconfig6 >/dev/null 2>&1; then
        rule_problem="kwriteconfig6/kreadconfig6 are not available"
    elif ! "$WADDLE_BIN" rule remove; then
        rule_problem="removing it failed"
    fi
fi

# A fixed path: nothing read from a file decides what gets deleted.
rm -f -- "$DATA_HOME/applications/$APP_ID.desktop"
rm -f -- "$DATA_HOME/waddle/install-manifest.txt"

if [ -n "$rule_problem" ] && [ -f "$RULES_MANIFEST" ]; then
    echo "Waddle's KWin window rule is still installed: $rule_problem." >&2
    echo "Kept $DATA_HOME/waddle so it can still be removed cleanly." >&2
    echo "Fix the problem, then run ./uninstall.sh again (or: waddle rule remove)." >&2
    if [ "${1:-}" = "--purge" ]; then
        rm -rf -- "$CONFIG_HOME/waddle" "$CACHE_HOME/waddle"
    fi
    exit 1
fi

if [ "${1:-}" = "--purge" ]; then
    rm -rf -- "$CONFIG_HOME/waddle" "$DATA_HOME/waddle" "$CACHE_HOME/waddle"
    echo "Removed Waddle's config, data and cache."
else
    echo "Kept your Waddle config and data. Run with --purge to delete them too."
fi
echo "Uninstalled."
