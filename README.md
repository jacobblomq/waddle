# Waddle

A small animated penguin that lives on your KDE Plasma (Wayland) desktop and teaches you the software you are using. **Waddle is read-only**: he points, explains, and shows. He never clicks, types, changes settings, or runs commands for you.

**Status: Phase 2 of 9 — senses.** Waddle now knows which app is focused, can take a picture of that one window when you open his eyes panel, and closes his eyes on locked screens and private windows. He cannot answer questions yet (Phase 3); `--demo` still plays sample content.

- What it will become: [docs/VISION.md](docs/VISION.md)
- How it is built: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- What happens when: [docs/PLAN.md](docs/PLAN.md)
- Why each choice was made: [docs/DECISIONS.md](docs/DECISIONS.md)

## Install

You need Ubuntu 26.04 (or similar) with KDE Plasma 6 on Wayland, [uv](https://docs.astral.sh/uv/), and Rust's `cargo` for the capture helper ([rustup.rs](https://rustup.rs), no root needed).

```bash
./install.sh
```

```bash
.venv/bin/waddle doctor
```

```bash
.venv/bin/waddle start --demo
```

`install.sh` never uses root. It creates the Python environment, two desktop entries, one KWin window rule for Waddle's own window, and builds the capture helper into `~/.local/share/waddle/bin/`. `./uninstall.sh` removes all of it; add `--purge` to delete Waddle's config and data too.

The guide in demo mode is canned sample content about Blender. Waddle does not read your screen for it.

## What Waddle can see, and when

- He always knows **which window is focused** (class, title, size) from his KWin script. Titles are never logged.
- He takes a **picture only of the focused window**, and in this phase only while **Waddle's eyes** (tray menu) is open. The picture stays in memory and is dropped when you close the panel.
- **Eyes closed, no picture:** screen locked, lock state unknown, paused, or a window on the private list (password managers, banking, private browsing, plus your own entries under `[privacy]` in `config.toml`).
- Shortcuts: Ask `Meta+Space`, Pause `Meta+Shift+W`, Quiet `Meta+Shift+Q`, Bring Waddle back `Meta+Shift+B`. A key that is already taken is not grabbed; the system dialog lets you pick another. The tray menu shows each key next to its action, so you don't have to remember them.

## Phase 2 has not run on a real desktop yet

It was built in a container with no KDE session and tested against stand-ins for KWin and logind. These are the checks only you can do; please tell me what you see.

| # | Check | How |
|---|---|---|
| 1 | Doctor is all ✓ | `waddle doctor`; paste the output if not |
| 2 | He can see the focused window | tray → **Waddle's eyes…**, then click another app; a picture of it should appear |
| 3 | Open menus are in the picture | open a menu in that app, press **Look now** |
| 4 | Lock closes his eyes | lock the screen, unlock: the picture is gone and the panel said "locked" in between |
| 5 | Private windows close his eyes | focus a private-browsing window: penguin shuts his eyes, panel shows no picture |
| 6 | Shortcuts work | a system dialog appears on first start; then try the four keys above |
| 7 | "Named in" is under 100 ms | the row in the eyes panel, for a few different apps |
| 8 | Fullscreen | a fullscreen app makes him small and faint; `Meta+Shift+B` brings him back |

If the eyes panel says `not_authorized`, that is the helper not being recognised by KWin. Logging out and back in may fix it (I have not been able to test this); either way, tell me.

The Phase 1 checks that are still open (theme change, reduced motion, uninstall leaving `kwinrulesrc` untouched) are listed in [docs/PLAN.md](docs/PLAN.md#phase-1-status).

## Development

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run mypy
```

```bash
cd capture-helper && cargo test
```

Licence: GPL-3.0-or-later.
