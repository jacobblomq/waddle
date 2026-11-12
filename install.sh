#!/usr/bin/env bash
# Waddle installer. Idempotent. Never uses root: it only touches your user session
# and Waddle's own files. uninstall.sh removes exactly what this creates.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="io.github.jacobblomq.Waddle"

case "${HOME:-}" in /*) ;; *) echo "HOME must be an absolute path." >&2; exit 1 ;; esac
# XDG says to ignore relative values.
case "${XDG_DATA_HOME:-}" in /*) DATA_HOME="$XDG_DATA_HOME" ;; *) DATA_HOME="$HOME/.local/share" ;; esac
DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.desktop"
CAPTURE_BIN="$DATA_HOME/waddle/bin/waddle-capture"
CAPTURE_DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.Capture.desktop"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1 — $2" >&2; exit 1; }; }

need uv "install it from https://docs.astral.sh/uv/ (no root needed)"

# The path goes into a desktop entry's Exec= line; refuse characters that would need
# Desktop Entry escaping rather than risk a wrong launcher.
case "$DATA_HOME" in
    *" "*) echo "XDG_DATA_HOME must not contain spaces (KWin matches the helper by exact path)." >&2; exit 1 ;;
esac
case "$REPO$DATA_HOME" in
    *[\"\`\$\\%\&\|]*|*$'\n'*)
        echo "Please move the Waddle checkout (and XDG_DATA_HOME) to a path without \" \` \$ \\ % & | characters." >&2
        exit 1 ;;
esac

say "1/4 Python environment"
(cd "$REPO" && uv sync --no-dev)
WADDLE_BIN="$REPO/.venv/bin/waddle"
[ -x "$WADDLE_BIN" ] || { echo "waddle entry point was not created" >&2; exit 1; }

say "2/4 Desktop entry"
mkdir -p "$(dirname "$DESKTOP_FILE")"
sed "s|@WADDLE_BIN@|$WADDLE_BIN|" "$REPO/kwin/$APP_ID.desktop.in" > "$DESKTOP_FILE"

say "3/4 KWin window rule"
if command -v kwriteconfig6 >/dev/null 2>&1 && command -v kreadconfig6 >/dev/null 2>&1; then
    "$WADDLE_BIN" rule install
else
    echo "kwriteconfig6/kreadconfig6 not found: skipping the window rule."
    echo "Waddle will still run, but will not stay above other windows."
fi

say "4/4 Capture helper"
# A small Rust program that takes a picture of one window when Waddle asks. KWin (Plasma
# 6.4-6.7) only lets a program do that if a desktop entry names its exact path, so it is
# installed to a fixed place in Waddle's own data directory.
if command -v cargo >/dev/null 2>&1; then
    (cd "$REPO/capture-helper" && cargo build --release --locked)
    mkdir -p "$(dirname "$CAPTURE_BIN")"
    chmod 700 "$DATA_HOME/waddle" "$(dirname "$CAPTURE_BIN")"
    install -m 0700 "$REPO/capture-helper/target/release/waddle-capture" "$CAPTURE_BIN.new"
    mv -f "$CAPTURE_BIN.new" "$CAPTURE_BIN"
    sed "s|@CAPTURE_BIN@|$CAPTURE_BIN|" "$REPO/kwin/$APP_ID.Capture.desktop.in" > "$CAPTURE_DESKTOP_FILE"
else
    echo "cargo (Rust) not found: skipping the capture helper."
    echo "Waddle will run, but cannot look at windows. Install Rust from https://rustup.rs"
    echo "(no root needed) and run ./install.sh again."
fi

say "Done."
echo "Try it:   $WADDLE_BIN start --demo"
echo "Check:    $WADDLE_BIN doctor"
echo "Remove:   $REPO/uninstall.sh"
