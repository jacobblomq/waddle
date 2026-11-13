# Waddle

A small animated penguin that lives on your KDE Plasma (Wayland) desktop and teaches you the software you are using. **Waddle is read-only**: he points, explains, and shows. He never clicks, types, changes settings, or runs commands for you.

**Status: Phase 3 of 9 — brain.** Waddle can now answer a typed question about the app in front of you, using a local model, one picture of the focused window, and the app's exact version. Phase 3 has not run on a real machine yet: see [Phase 3 on your machine](#phase-3-on-your-machine). `--demo` still plays sample content.

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

## The brain

Answers come from a local model server (Ollama) and Waddle's own small service, both in Docker containers. Nothing is published to the network: the app talks to the brain over a Unix socket in your runtime folder, the brain itself has no route to the internet, and only the one-shot download container can reach out, while a download runs.

You need Docker with the Compose plugin and the NVIDIA Container Toolkit. Waddle does not install them.

```bash
.venv/bin/waddle brain up
```

```bash
.venv/bin/waddle models pull all
```

```bash
.venv/bin/waddle doctor
```

- `waddle brain down` stops the containers and keeps the downloaded models. `waddle brain status` and `waddle brain logs` show what is running.
- `waddle models pull` takes a role (`fast_vlm`, `smart_model`, `embedder` or `all`), never a model name. Which model plays which role is in `deploy/brain.toml`. **Those model names are provisional**: they have not been pulled or measured yet, and the download shows the real sizes. Expect tens of gigabytes in total.
- `waddle brain up --native` runs Waddle's service without a container, in the foreground. It gives up the network and file boundary, so use it only if Docker is not an option.
- If the brain is not running, the penguin naps and says so when you ask. Nothing else stops working.

When you ask (`Meta+Space` or the tray), Waddle takes **one** picture of the focused window, through the same rules as above. If his eyes are closed (locked, paused, private window), the question goes without a picture or the window title, and the answer says he could not look.

For `man` pages, `tldr` pages and an app's own `--version`, Waddle uses a sandbox (`bubblewrap` and `prlimit`, from the `bubblewrap` and `util-linux` packages): no network, an empty home, everything read-only. Without them those three things are off, and doctor says so.

## Phase 3 on your machine

None of this has run with Docker, a GPU or a model: the build container has none. Please try these and tell me what you see.

| # | Check | How |
|---|---|---|
| 1 | The brain starts | `waddle brain up`, then `waddle brain status`. The first run builds an image and downloads Ollama. |
| 2 | Models download | `waddle models pull all`. If a model name is refused as unknown, tell me which: the names are provisional. |
| 3 | Doctor | `waddle doctor`: paste the docker, gpu, brain, models and sandbox sections. Two rows always say "not checked" (NVIDIA container runtime, bubblewrap version). |
| 4 | An answer | Focus Blender (or any app), press `Meta+Space`, ask "What am I looking at?". Words should stream into the bubble, with a small line of sources at the end. |
| 5 | The version is right | Ask "Which version is this?". If Waddle does not know, the bubble offers **Tell Waddle the version**. |
| 6 | Speed | How long until the first words, on the first question (model loading) and on the second. |
| 7 | Napping | `waddle brain down` while Waddle runs: he naps, asking says the brain is asleep, nothing crashes. `waddle brain up`: he wakes on his own within about half a minute. |
| 8 | Stop | Ask something long and press **Stop** mid-answer. |
| 9 | Nothing listens on the network | `ss -ltnp \| grep -i -e waddle -e ollama` should print nothing. |

Likely first failures, so you know they are not your fault: the socket's owner as seen from the host (the app refuses a socket it does not own), Ollama starting with all capabilities dropped, and the model tags.

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
