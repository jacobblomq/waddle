# Waddle

A small animated penguin that lives on your KDE Plasma (Wayland) desktop and teaches you the software you are using. **Waddle is read-only**: he points, explains, and shows. He never clicks, types, changes settings, or runs commands for you.

**Status: Phase 1 of 9 — skeleton and character.** The penguin, his bubble, guide card, tray icon, and window placement exist. He cannot see your screen or answer questions yet; `--demo` plays sample content so you can try the UI.

- What it will become: [docs/VISION.md](docs/VISION.md)
- How it is built: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- What happens when: [docs/PLAN.md](docs/PLAN.md)
- Why each choice was made: [docs/DECISIONS.md](docs/DECISIONS.md)

## Try Phase 1

You need Ubuntu 26.04 (or similar) with KDE Plasma 6 on Wayland, and [uv](https://docs.astral.sh/uv/).

```bash
./install.sh
```

```bash
.venv/bin/waddle start --demo
```

`install.sh` never uses root. It creates the Python environment, a desktop entry, and one KWin window rule (keep above, no taskbar entry, no border, all desktops) for Waddle's own window. `./uninstall.sh` removes all three; add `--purge` to delete Waddle's config and data too.

Things to try: click the penguin (opens a sample guide), drag him, rest the pointer on him (he steps aside), and use the tray icon's **Demo** menu to cycle his moods.

## Phase 1 has not run on a real desktop yet

It was built and tested in a container with no KDE session: 433 automated tests pass headless, and the KWin scripts were exercised under Node with a fake KWin. **Whether it behaves on your Plasma session is unverified.** These are the checks only you can do; please tell me what you see.

| # | Check | How |
|---|---|---|
| 1 | Waddle appears bottom-right, above other windows, with no border or taskbar entry | start him and look |
| 2 | Empty space around him passes clicks through to the app underneath | click just beside him |
| 3 | Clicking him does not take keyboard focus away from your app | type in your app after clicking him |
| 4 | Dragging works, and he returns to the same spot on each monitor after a restart | drag, `waddle stop`, start again |
| 5 | He stays visible over a fullscreen app and over an open menu | open a fullscreen window; open a dropdown |
| 6 | Scarf follows your accent color; changing the Plasma color scheme updates him live | System Settings → Colors |
| 7 | With animations disabled in Plasma he holds still | System Settings → Animations → speed to "instant" |
| 8 | Memory and idle CPU are within budget (< 150 MB, ~0 %) | command below, after leaving him alone for a minute |
| 9 | `./uninstall.sh` leaves `~/.config/kwinrulesrc` exactly as it was | `cp` it before installing, `diff` after uninstalling |

```bash
.venv/bin/waddle status --measure 60
```

If the script could not load or the rule did not apply, this shows why:

```bash
.venv/bin/waddle rule status
```

Known gaps in Phase 1 are listed at the end of [docs/PLAN.md](docs/PLAN.md#phase-1-status).

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

Licence: GPL-3.0-or-later.
