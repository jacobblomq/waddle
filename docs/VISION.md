# PROJECT: Waddle — a read-only, on-screen software tutor for Linux

> This file is the original project brief, committed verbatim. It is the source of truth.
> Any deviation from it is recorded in [DECISIONS.md](DECISIONS.md).

You are building **Waddle**, a small animated penguin that lives on my KDE Plasma (Wayland) desktop and teaches me how to use whatever I'm working in: Blender, DaVinci Resolve, Kdenlive, GIMP, Krita, OBS, the terminal, KDE System Settings, web apps, anything. It sees the focused window, knows the **exact version** of the app, pulls documentation for **that version**, and walks me through tasks step by step, checking off each step as it sees me do it. It is also the "person inside the computer": it knows what's installed, what's running, how the hardware is doing, and why things are behaving the way they are, and it can explain all of it.

It should feel like magic: instant, accurate, never in the way, and never wrong with confidence.

---

## 0. The prime directive: Waddle is read-only

- Waddle **never** clicks, types, injects input, changes app settings, edits my files, installs/removes packages, kills processes, or runs any command that changes system state. No ydotool, no uinput, no "do it for me" mode, no shell passthrough from the model.
- Waddle may **point** (draw highlights on screen), **explain**, and **show**.
- The only things Waddle may change are **its own** files, processes, and resources (its own config/data directory, loading/unloading its own models, its own containers).
- The only clipboard write allowed is when I explicitly click a "Copy" button on a command or snippet Waddle shows me. Waddle never reads my clipboard.
- If any design choice would break this, stop and tell me instead of working around it.

---

## 1. My machine (verify each item at runtime and adapt; never assume)

- Ubuntu 26.04 with KDE Plasma 6 on Wayland. Detect DE/session via `XDG_CURRENT_DESKTOP` / `XDG_SESSION_TYPE`. Optimize for KDE Wayland; degrade gracefully (best effort) on GNOME or X11.
- NVIDIA RTX 5090 (Blackwell), 32 GB VRAM. **Confirm that every GPU library you choose (CUDA runtime, PyTorch if used, CTranslate2/faster-whisper, vLLM, OCR engine) actually supports Blackwell**, and pin versions that do.
- Docker + NVIDIA Container Toolkit available.
- Many apps (e.g., Resolve) may run under XWayland; everything must work for both native Wayland and XWayland windows.

---

## 2. Before writing any code

1. Look up the **current** versions and APIs of every library and interface you plan to use (PySide6/QML, xdg-desktop-portal ScreenCast + GlobalShortcuts, KWin scripting API and `org.kde.KWin.ScreenShot2`, AT-SPI2, Ollama API, vLLM, SearXNG, faster-whisper, Piper (use the maintained fork), Kokoro, nvidia-ml-py, sqlite-vec, trafilatura, the Blender extension format). Don't rely on memory for anything that may have changed.
2. Write `docs/ARCHITECTURE.md`, `docs/PLAN.md` (phases below), and `docs/DECISIONS.md`. Show me ARCHITECTURE and PLAN before implementing Phase 1.
3. Ask me only about decisions that are truly blocking (e.g., license). Otherwise choose sensible defaults and record each one with its reasoning in DECISIONS.md.
4. Commit this prompt as `docs/VISION.md` and treat it as the source of truth. If you deviate from it, say so in DECISIONS.md.

---

## 3. Architecture overview

Two halves joined by one local, authenticated WebSocket:

- **`waddle-desktop`** (host, native, lightweight): the penguin, overlays, sensors, hotkeys, voice I/O, system telemetry, app adapters. Must have direct access to D-Bus, PipeWire, `/proc`, and KWin, so it runs **natively, not in Docker or Flatpak**.
- **`waddle-brain`** (heavy, GPU): models, search, retrieval, guide engine, caches. Docker Compose by default; must also run natively for people who don't want Docker.

### Performance budgets (measure these and enforce them in tests/benchmarks)
| Path | Budget |
|---|---|
| Host idle CPU | ~0% (event-driven, no busy polling) |
| Host RAM | < 150 MB |
| Waddle visible after login | < 2 s (brain warms lazily) |
| Window switch → app identified | < 100 ms |
| Cached version lookup | < 50 ms |
| Question → first streamed words (models warm) | < 1.5 s |
| Screen change → step verified | < 1 s |
| Push-to-talk release → transcript | < 700 ms |

---

## 4. Host side: `waddle-desktop`

Python 3.12+, managed with `uv`. Typed, async where it helps (qasync or a clean thread boundary between Qt and asyncio).

### 4.1 The penguin and its UI (PySide6 + QML)

**The character**
- Waddle is an **original penguin drawn entirely in code** (QML Shapes / QPainter paths). No external image files, no copies of Tux or any existing mascot. Round, friendly, slightly chunky, big readable face, a little scarf whose color follows the Plasma accent color.
- Animated states (smooth, low-CPU property animations; pause animations entirely when hidden or occluded):
  `idle` (blinks, occasional waddle/foot tap), `listening` (leans in, glow while push-to-talk is held), `watching` (eyes track the active window/cursor), `thinking` (flips through a tiny book), `speaking` (beak synced to TTS amplitude), `celebrate` (small hop when a step completes), `alert` (taps the glass), `confused` (head tilt when he can't see clearly or doesn't know), `eyes-closed` (blocked window / privacy), `sleeping` (paused, quiet, idle, or screen locked; Zzz), `napping-brain` (brain unreachable).
- **Reduced motion** setting (respect the Plasma animation-speed setting) and a size slider.

**Bubble, guide card, and panels**
- Speech bubble streams text as it arrives. Short by default, with a "more" expander.
- **Guide card:** goal title, "Step 2 of 6", the current instruction with shortcuts rendered as keycaps, a ✓ checklist of earlier steps, buttons for Back / Skip / "I did it" / "Show me where" / "Why?", and a text input.
- **Step pill:** when the bubble is collapsed, a tiny pill next to Waddle keeps the current step visible.
- **History panel:** scrollable, searchable conversation history, threaded per app. Retention is configurable.
- **"Waddle's eyes" panel** (for trust and debugging): shows the last frame/crop sent to the model, the app/version/state Waddle thinks it's looking at, and which sources an answer used.
- **Settings window** (QML, follows the Plasma theme): hotkeys, voice, models, verbosity, privacy blocklist, ambient watching level, skill levels per app, data retention, "forget everything".
- **System tray icon** (StatusNotifierItem): Pause, Quiet mode, Focus mode, Hide/Show Waddle, Waddle's eyes, Settings, Quit. Its icon reflects state (watching / paused / brain down).
- Follow the Plasma light/dark color scheme and font scaling; make the bubble readable by screen readers (Qt accessibility).

**Placement and behavior on Wayland**
- Wayland clients can't position themselves or force keep-above. On first run, install a **KWin window rule** (via `kwriteconfig6` into `kwinrulesrc`, then reconfigure KWin over D-Bus) that matches Waddle's window class: keep above, skip taskbar/pager/switcher, no border, all desktops, initial position bottom-right of the primary output. The installer must be idempotent and reversible.
- Draggable; remember position **per monitor**. Option: follow the active window's monitor.
- Transparent regions are **click-through** (input region = penguin + bubble only).
- **Gets out of the way:** fades/slides aside when the cursor approaches, and moves automatically if a "Show me where" target is underneath him or the bubble.
- **Fullscreen apps and games:** when the focused window is fullscreen (reported by the KWin script), Waddle shrinks to a small edge badge or hides (configurable). A hotkey brings him back.
- **Do Not Disturb:** respect Plasma DND (notifications `Inhibited`). No proactive bubbles while it's on; answers to my own requests still work.
- **Recording/screen sharing:** optional auto-hide while OBS is recording (via the OBS adapter) or while an active PipeWire screencast is detected (read-only check).

**"Show me where" highlight layer**
- A separate full-screen, transparent, **click-through**, keep-above overlay (one per output) that draws a pulsing ring/arrow at the target. It clears on the next click/frame change or after a timeout.
- **Coordinate mapping must be correct on multi-monitor setups and with fractional scaling** (100/125/150/200%): screenshot pixels → window-relative coords → logical global coords → overlay coords per output. Include tests for this at multiple scales and monitor layouts.
- If target confidence is low, describe the location in words instead of guessing ("top-right of the Properties panel, the camera-icon tab").

### 4.2 Sensors (all event-driven)

**Active window and cursor (KWin script)**
- Ship a small KWin script that reports to Waddle over D-Bus (`callDBus`): `windowActivated` (caption, resourceClass, resourceName, pid, geometry, output, fullscreen, XWayland or not), window geometry changes, virtual desktop changes, and **cursor position on request** (`workspace.cursorPos`), throttled.
- Fall back to `kdotool` if the script can't load. Report the fallback in `waddle doctor`.

**Screen capture**
- Primary: KWin `org.kde.KWin.ScreenShot2` (`CaptureActiveWindow`, `CaptureArea`), authorized through a `.desktop` file with `X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2`.
  - ⚠️ **Verify how KWin authorizes callers.** If it matches the caller's executable path, **don't** authorize the system Python interpreter (that would grant screen capture to every Python script on the machine). Instead build a **tiny dedicated capture helper binary** (C++ or Rust) with its own path, authorize only that, and talk to it over a Unix socket.
- Fallback: xdg-desktop-portal ScreenCast over PipeWire with a persisted `restore_token`, so I'm asked for permission once, not every launch.
- Last resort: `spectacle -b -n -a`.
- Capture **only the active window**, so Waddle never sees himself or other windows.
- Each analysis gets (a) a downscaled overview of the window and (b) a **full-resolution crop around the cursor or region of interest**, so tiny 10px UI labels in Blender/Resolve stay readable.
- Frames live **in memory only**. Nothing touches disk unless I enable a debug flag.

**When Waddle looks (watching levels, configurable)**
- `on-demand` (always on): when I ask, push-to-talk, use region-select, or press "Show me where".
- `guide` (default on): while a guide is active, throttled to ≤1 fps and gated by a perceptual-hash / downscaled-diff check, so unchanged frames never reach a model.
- `glance` (default on): one low-res glance on app switch, **only** if the version is unknown or context is needed.
- `ambient` (default **off**, opt-in): occasional low-rate glances to enable proactive tips.
- **Change-gating must handle animated regions:** viewport playback in Blender, video playback in Resolve/Kdenlive, progress bars, and blinking cursors change constantly. Detect playback (via adapters or heuristics) and mask known "live" regions (viewers, timelines during playback) so they don't trigger analysis or burn GPU.

**Rewind buffer ("what just happened?")**
- While watching is active, keep a short **in-memory ring buffer** (~30 s) of low-res keyframes (stored only on meaningful change) plus adapter events. This lets Waddle answer "what did I just do?", "where did my panel go?", "why did everything turn orange?" by comparing before/after (and the app's undo history, where an adapter exposes it).
- Discarded continuously; never written to disk; disabled on blocked windows.

**Region select: "What's this?"**
- A hotkey lets me drag a rectangle over any part of the screen and ask "what does this do?" or "explain this". The crop goes to the model with full app/version context.

**Drop a file on Waddle**
- Dragging a file onto Waddle runs **read-only inspectors with timeouts**: `file`, `ffprobe`/`mediainfo` (codec, VFR, color space, bit depth), `exiftool`, image metadata, the `.blend` header (the Blender version it was saved with), font/PDF metadata, archive listings (never extract). Waddle explains what it is, which installed apps can open it, and any compatibility problems with my **installed app versions** ("this is variable-frame-rate H.264; your Resolve edition on Linux may struggle with it — here's why and what people do about it").
- Never execute, open, or import dropped files.

**Global hotkeys**
- Use the XDG `GlobalShortcuts` portal (KDE implements it), falling back to KGlobalAccel over D-Bus.
- **At install time, check every default for conflicts with my existing Plasma shortcuts** and propose alternatives. Don't use modifier-only combos.
- Defaults (all rebindable):
  - `Meta+Space` (if free): open text input
  - **Hold** a configurable key (default suggestion: `Meta+\``, verify it's free): push-to-talk, using the portal's Activated/Deactivated signals for true hold-to-talk. Document how to map PTT to a spare mouse button through Plasma's mouse-button remapping, which is great for Blender users.
  - `Meta+Shift+W`: pause/resume all watching and the mic
  - `Meta+Shift+H`: "Show me where" for the current step
  - `Meta+Shift+R`: region-select "What's this?"
  - `Meta+Shift+Q`: quiet mode

**Session state**
- Idle and lock: listen to `org.freedesktop.ScreenSaver` / logind `LockedHint`. When the screen locks, **stop all capture and the mic immediately**, and Waddle sleeps.
- Desktop notifications (**opt-in, off by default**): monitor `org.freedesktop.Notifications` so Waddle can explain a notification I just got ("what did that mean?").

**Input awareness without keylogging**
- Never read `/dev/input` and never log keystrokes. Infer actions from frame diffs, window/title changes, cursor position, AT-SPI focus/state events, and adapter events.

### 4.3 Perception stack (how Waddle understands the UI)

Use the most reliable source available, in this order, and record which source each fact came from:

1. **App adapter state** (exact truth from the app itself; §4.5)
2. **AT-SPI2 accessibility tree** (read-only): Qt and GTK apps (Kdenlive, Krita, GIMP, Konsole, System Settings, many others) expose menus, buttons, labels, checked states, focus, and often element extents. Use it to find UI elements by name/role, verify state ("the 'Proxy' checkbox is now checked"), and locate highlight targets precisely. Window-relative extents plus KWin window geometry give global coordinates on Wayland. Prune to the focused subtree before sending it to a model. Apps with custom-drawn UIs (Blender, Resolve) won't expose much; fall through.
3. **OCR** (fast, local, GPU): for exact text (version strings, labels, error messages, terminal output). More precise than a VLM for exact strings.
4. **Vision-language model**: for understanding layout, state, and intent, and for **grounding** (bounding boxes for "Show me where").

### 4.4 App identity and version detection (cached)

**Waterfall, per focused app:**
1. Resolve the real binary from the window's PID: `/proc/<pid>/exe`, `/proc/<pid>/cmdline`. Walk up the process tree for wrappers/launchers (Electron, shell scripts, AppImage runtimes).
2. Detect packaging and read version metadata (instant, side-effect-free): Flatpak (`/proc/<pid>/root/.flatpak-info`, `flatpak info`), Snap (cgroup / `SNAP_*` env in `/proc/<pid>/environ` → `snap list`), AppImage (`APPIMAGE` env → embedded metadata), deb (`dpkg -S` on the exe → `dpkg-query -W`), or tarball/opt installs (version files, directory names).
3. Run `<binary> --version` only for an **allowlist** of apps known to answer without side effects or launching a GUI, with a short timeout and no network.
4. Vision/OCR: read the title bar, status bar, or splash for a version string.
5. **Ask me nicely:** "I don't recognize this version of *Resolve* yet — could you open *Help → About* for a sec?" Waddle shows `watching`, detects the About dialog, OCRs the version, confirms in the bubble ("Got it: DaVinci Resolve 20.x Studio ✓"), and caches it. If I say "skip," he continues with version-agnostic guidance and says so.

**Also capture:**
- **Edition** (Free/Studio, LTS, nightly/alpha/beta builds; warn that docs may lag for pre-release builds).
- **UI language/locale** (so docs and menu names match what I see).
- **Active keymap/preset** where knowable (e.g., Blender's keymap preset, custom bindings via the adapter), so guides use **my** shortcuts, not just the defaults.

**Caching:** SQLite keyed by (app id, exe path, file size+mtime or package version). Updates invalidate automatically. Normalize versions (major.minor[.patch] + edition), because docs are usually versioned by major.minor.

**Web apps in the browser:** treat sites as apps when the window title identifies them (e.g., a design tool in the browser). Use the generic adapter + vision + docs. Versions often aren't visible, so say so.

**Terminal and CLI tools:** in Konsole and other terminals, OCR/vision reads visible output (errors, prompts) and Waddle explains it. For CLI help he uses **local man pages and a cached tldr dataset** (read-only). He suggests commands with a Copy button and a plain-English explanation of what each part does, and flags anything destructive or anything that needs sudo. He never runs them.

**The desktop itself is an app:** ship knowledge packs for KDE Plasma (System Settings, Dolphin, Konsole, Discover, KWin rules) and Ubuntu basics (apt/flatpak/snap, NVIDIA drivers), so "how do I change my mouse acceleration?" gets a guided walkthrough in System Settings like any other app.

### 4.5 App adapters (optional, read-only, plugin system)

Vision is the fallback; the app's own state is the truth.

**Interface:** `detect()`, `get_state() -> dict` (mode, active editor/page/workspace, selection, relevant settings, playback state), `subscribe(events)`, `undo_history()` (if available), `keymap()` (if available), `doc_sources(version)`, optional `ui_landmarks`. Adapters must be read-only. Enforce this with code review and tests, and never expose a write path.

**Blender (ship first):**
- Package as a **Blender extension** (`blender_manifest.toml`, 4.2+) with a legacy add-on fallback. Declare only the permissions it needs (localhost network).
- Serves state over a localhost/Unix socket: mode, active editor and workspace, render engine, selected/active objects and types, active material/node tree and selected nodes, modifiers on the active object, current frame and playback state, viewport shading, the recent Info log (operators I ran, which is great for "what did I just do?"), undo history names, active keymap preset.
- Poll via `bpy.app.timers` and/or depsgraph/msgbus handlers. **Never block Blender's UI thread**; keep the work tiny and cheap.
- **Must never modify the scene, preferences, or files.**

**Generic adapter:** AT-SPI + vision + docs for everything else.

**Stubs + documentation** for DaVinci Resolve (scripting API where the edition allows; otherwise AT-SPI/vision), Kdenlive (AT-SPI, D-Bus if useful), Krita (Python plugin), GIMP 3 (Python plugin), and OBS (obs-websocket, read-only requests only). For each, document exactly what state it can expose.

### 4.6 System awareness (the "person inside the computer")

A low-frequency (adaptive, 2–5 s) read-only sampler; everything else is on demand:
- **GPU** (nvidia-ml-py): temp, power, clocks, utilization, encoder/decoder load, VRAM total and **per process**.
- **CPU/RAM/swap/disk/IO/network/temps** (psutil + lm-sensors if present); top processes by CPU/RAM/IO.
- **Services and containers:** failed systemd units, recent journal errors (`journalctl -p err`), Docker container health via the Docker socket (read-only calls only).
- **Storage:** disk-space warnings, fast-filling directories (e.g., render caches) on request.
- **App crashes:** detect when a focused app's process exits abnormally. Read its known log locations (read-only) and recent journal/coredump entries, and explain what probably happened in plain language.
- **Installed-app inventory:** index `.desktop` files, Flatpak, Snap, and deb packages with versions and categories, so Waddle can answer "what do I have that can edit audio?" and "is my Blender up to date?" Track per-app focus time locally (opt-out) to prioritize doc prefetching and skill tracking.
- **Cross-reference system facts with app knowledge:** combine hardware/driver/codec facts with version-specific app knowledge ("your Resolve is using the GPU but the decoder isn't engaged for this codec, which explains the stutter"). Knowledge packs can include **platform notes** (Linux-specific limitations, edition differences, driver requirements), retrieved and cited like any other doc.
- A rolling 10-minute summary is injected into model context **only when relevant** ("why is my PC slow?", "what's using my GPU?", render/playback questions).
- **Alerts** (rate-limited, thresholds configurable): GPU temp high, VRAM nearly full, disk nearly full, container crash, failed unit, a render starting while Waddle's own models hold VRAM.
- Waddle may suggest actions ("you could close X" or "here's the command to free space") with Copy buttons, but never performs them, except managing **his own** models and containers.

### 4.7 Voice (text and push-to-talk are both first-class)

- **STT:** faster-whisper (local, GPU, Blackwell-compatible build), model size configurable, VAD trimming. Record only while PTT is held. Show a live waveform on Waddle. Optional language auto-detect.
- **TTS:** local; Piper (maintained fork) by default for lightness, Kokoro as a higher-quality option. Interruptible (any new question, Esc, or PTT stops speech). Mute toggle. Speech is a short summary; the guide card holds the detail. Voice and speed selectable.
- Voice and text go through exactly the same request pipeline.
- Show a microphone-active indicator on Waddle whenever audio is captured. The mic is never open outside PTT.

---

## 5. Brain side: `waddle-brain`

**Services (Docker Compose; every port bound to `127.0.0.1` only):**
- **Model server:** Ollama (default) or vLLM behind one abstraction. **No hardcoded model names**; config defines roles:
  - `fast_vlm` (small, ~7–8B class): frame verification, change classification, version OCR assist, UI grounding with bounding boxes.
  - `smart_model` (larger, ~27–32B class VLM): planning guides, answering questions, multi-step reasoning.
  - `embedder`: small local embedding model for retrieval.
  - Optional remote providers (OpenAI-compatible or Anthropic API) behind the same interface. **Off by default**; a visible indicator shows when one is active, and screen frames are never sent remotely unless I explicitly allow it per provider.
  - Pick the current best open models at build time, run the **model bake-off** (§11) on my GPU, and document the choice.
- **SearXNG** (self-hosted, private search; not exposed beyond localhost).
- **waddle-api** (FastAPI): orchestration, retrieval, guide engine, caches, tool layer.
- **Storage:** SQLite + sqlite-vec for versions, docs, guides, user profile, settings. No heavy vector DB unless justified in DECISIONS.md.

**Host ↔ brain link:** one WebSocket on localhost, token-authenticated (token generated at install, stored with `0600` permissions). Streams responses, state, and events.

**Model I/O discipline:**
- All structured outputs (plans, verifications, groundings, version extractions) use **pydantic schemas with schema-constrained decoding** (Ollama `format` JSON schema / vLLM guided decoding), validated, with bounded retries.
- Lean context per call: app + version + edition, adapter state, pruned AT-SPI snippet, frame(s), top-k doc chunks, guide state, short history, and system summary only when relevant. Keep a stable system-prompt prefix so model servers can reuse cached prefixes.
- The model can call only a fixed set of **read-only tools** (search docs, fetch page, get system stats, get app state, inspect file, look up man/tldr). No generic shell tool exists.

**GPU etiquette (critical: Blender and Resolve need the GPU):**
- Track VRAM continuously. When a GPU-heavy job starts (Blender render, Resolve render/playback, a game) or free VRAM drops below a threshold, unload `smart_model` (e.g., Ollama `keep_alive: 0`) and drop to `fast_vlm` or text-only mode. Reload when things calm down. Announce it once, quietly.
- **Focus mode** frees all of Waddle's VRAM instantly (tray + hotkey).
- Waddle only ever manages **his own** model processes, never other people's.

**Graceful degradation (every mode must work and be clearly shown on the penguin):**
| Condition | Behavior |
|---|---|
| Brain unreachable | `napping-brain` state; telemetry, alerts, version detection, and cached guides still work |
| No internet | Cached docs only, clearly labeled; queue doc fetches for later |
| Low VRAM | `fast_vlm` / text-only; explain that answers may be simpler |
| Capture denied | Text/voice-only mode; explain how to grant permission |
| No mic | Text only |
| AT-SPI unavailable | Skip to OCR/vision |

---

## 6. Knowledge engine (version-pinned docs)

- **Knowledge packs** (YAML, one per app): official doc domains, **versioned URL patterns** (e.g., manuals with the version in the path), release-notes pages, API references, platform notes, a domain trust ranking (official docs > official forums/bug trackers > reputable tutorials > everything else), and UI-language variants.
- **Retrieval flow:** question + app + exact version + edition + locale + current UI state → local cache → if stale or missing: SearXNG query pinned to the version → fetch (size limits, timeouts, no JS execution) → clean extraction (trafilatura) → chunk + embed → store with version tag → answer **only** from retrieved material + observed state.
- **Version honesty:** if the exact version's docs can't be found, use the nearest version and **flag it** ("This is from the 4.4 manual; menus may have moved in 4.5"). Use release notes between the two versions to warn about specific changes.
- **"What's new for you":** when an installed app updates, Waddle offers a short, personalized summary of release-note changes that affect features I actually use (based on my history). Shown once, dismissable.
- **Background prefetch:** when a new app+version is first seen, quietly index its core manual sections. Rate-limited, polite crawling that respects robots.txt and site terms. Prioritize my most-used apps.
- **Offline-first:** cached docs work with no internet.
- **Citations:** every answer and guide step can show its sources, labeled by type: `[docs 4.5]`, `[screen]`, `[Blender state]`, `[system]`, `[man page]`.

---

## 7. Guide engine (the heart of the product)

**Planning**
- A request ("make volumetric god rays", "color-match these two clips", "set up proxies in Kdenlive") → `smart_model` produces a validated plan of steps, each with: `instruction`, `shortcut` (using **my** keymap where known), `ui_target` (description + optional AT-SPI selector), `success_check` (adapter predicate preferred, else AT-SPI state, else a visual check description), `why` (the concept behind the step), `common_mistakes`, `source`.
- Before starting, check prerequisites against current state (e.g., "you're in Eevee; this guide needs Cycles — that'll be step 1").

**Execution loop**
- Show the step → observe (adapter → AT-SPI → throttled frames) → verify `success_check` → `celebrate` and advance, **or** detect drift (wrong mode, wrong menu, error dialog, mis-click) and give a gentle, specific correction based on what Waddle actually sees.
- **Error dialogs:** detect them proactively during guides and explain them in plain language.
- **Interruptions:** I can ask anything mid-guide; the guide pauses, answers, and resumes where it was.
- **Multi-app guides:** steps can span apps (e.g., render in Blender → import into Resolve). The guide follows focus changes and resumes the right step when I switch windows.
- "Show me where" → AT-SPI extents if available, else VLM grounding → highlight overlay. Low confidence → describe in words.

**Teaching modes (the "teacher" part), selectable per guide:**
- **Walkthrough:** exact steps and shortcuts.
- **Coach:** Waddle states each step's goal, not the clicks. If I stall (time-based) or ask, hints escalate: nudge → where to look → exact instruction → "Show me where".
- **Challenge:** Waddle gives me a task, watches, then reviews what I did and suggests improvements.
- Every step has a "Why?" button that explains the underlying concept briefly.

**Tutorial translator**
- If I'm watching a tutorial (video or web page) for a different version than mine, Waddle can turn it into a guide **for my installed version**, using release notes/docs to map moved menus and renamed features, and flagging anything that no longer exists.

**Guide library**
- Completed guides are saved and can be replayed. They're **re-validated** when the app version changes.
- Export/import guides as Markdown/YAML so I can share them or keep my own collection.

---

## 8. Waddle's memory of me (local, transparent, editable)

- **Skill profile per app** (beginner → expert, set at onboarding and adjusted from evidence: shortcuts I use, guides I've completed, how often I need hints). It controls how much Waddle explains.
- History of completed guides and frequently asked topics ("you did a similar setup last week — want the same approach?").
- Everything is viewable and editable in Settings, with per-app reset and a global **"forget everything"** (also `waddle forget`).
- Never stores frames, audio, or keystrokes; only derived, human-readable facts.

---

## 9. Personality & UX rules (don't be Clippy)

- **Never interrupt mid-action.** Proactive tips only when I'm idle for a moment, the tip has high confidence, and it's clearly useful.
- **Bubble budget:** default ≤3 unsolicited tips per hour, adaptively lowered when I dismiss them. System alerts are separate but still rate-limited. A "don't show tips like this again" option on every tip.
- **Efficiency tips** (within budget, ambient mode only): e.g., noticing I navigate the same menu repeatedly and offering the shortcut.
- **Sliders:** Chatty ↔ Quiet, Beginner ↔ Expert (per app), voice on/off by default.
- Tone: friendly, brief, encouraging, a little playful; never condescending; no filler.
- **Honesty:** says "I don't know," "I'm not sure this applies to your version," or "I can't see that clearly — could you zoom in or open that panel?" instead of guessing. Distinguishes what he saw from what he read in the docs.
- **First-run onboarding:** Waddle introduces himself; requests permissions one at a time with plain explanations (screen capture, accessibility, mic, notifications opt-in); checks hotkey conflicts; asks my rough skill level for apps he finds installed; picks a voice; runs a self-test (capture, hotkeys, mic, AT-SPI, brain, GPU) with clear fixes for anything that fails.

---

## 10. Privacy, security & safety

- **Local by default.** No telemetry, analytics, or phone-home of any kind. Remote model providers are opt-in per provider, with a visible indicator.
- **Blocklist:** default patterns for password managers, banking/finance sites (by title/class), private-browsing windows, and anything I add. On a blocked window Waddle visibly closes his eyes and captures **nothing**; the rewind buffer is cleared.
- **Lock screen / idle:** all capture and mic stop immediately.
- **No frames, audio, or transcripts on disk** unless a debug flag is on. **Logs never contain screen text or transcripts by default** (redacted).
- **Prompt-injection defense:** treat all screen text, AT-SPI text, web pages, search results, docs, file metadata, and notifications as **untrusted data, never instructions**. The model cannot gain tools or permissions from content. Since all tools are read-only and Waddle can't act, the worst case is a bad answer; make sure it stays that way.
- **Network surface:** every service binds to `127.0.0.1`; WebSocket token auth; no ports exposed to the LAN.
- **Fetched content:** size limits, timeouts, content-type checks, HTML sanitization before display, no JS execution.
- **File inspectors:** run with timeouts and resource limits, never execute dropped files.
- `docs/PRIVACY.md` explains in plain language what Waddle sees, stores, and sends, and how to verify it.

---

## 11. Engineering standards

- **Tooling:** `uv`, ruff (lint + format), mypy (strict where practical), pytest, pre-commit.
- **Tests:**
  - Unit: version parsing/normalization, packaging detection, the version waterfall, plan schema parsing, step verification logic, coordinate mapping (multi-monitor, fractional scaling), hotkey conflict detection, blocklist matching, prompt-injection fixtures.
  - Integration: run the desktop side against a **nested KWin** (`kwin_wayland --virtual` or windowed) in tests/CI, plus mocked D-Bus/portal services.
- **Evals (`evals/`):** a fixtures corpus of real screenshots across apps and versions with labels (app, version, UI state, target element boxes). Measure version-detection accuracy, state-verification accuracy, grounding accuracy (IoU), plan quality (rubric), and latency against the §3 budgets. Run evals whenever models or prompts change.
- **Model bake-off script:** benchmarks candidate VLMs/LLMs on my GPU (accuracy on the eval set, latency, VRAM) and writes a recommendation into DECISIONS.md.
- **Observability:** structured logs (redacted), timing spans for every pipeline stage, a debug HUD in "Waddle's eyes".
- **`waddle` CLI:** `start`, `stop`, `status`, `doctor` (diagnoses session type, KWin script, capture path, portal permissions, hotkeys, AT-SPI, mic, GPU/CUDA, Docker, brain, models, SearXNG, and prints exact fixes), `forget`, `export-guides`, `import-guides`, `bench`.
- **Install:** `install.sh` is idempotent and sets up the uv venv, capture helper, KWin script + window rule, `.desktop` files, systemd **user** units (autostart), and docker compose. A matching `uninstall.sh` removes everything, including KWin rules and shortcuts. **No Flatpak packaging** (the sandbox blocks `/proc`, KWin scripting, and restricted D-Bus interfaces).
- **Config:** single `~/.config/waddle/config.toml` (documented, with defaults). Data in `~/.local/share/waddle/`, cache in `~/.cache/waddle/`.
- Small, reviewable commits per phase. Keep DECISIONS.md current.

**License:** the Blender extension must be GPL-compatible (GPL-3.0-or-later). Ask me whether the whole repo should be GPL-3.0-or-later (simplest) or the core under a permissive license with the Blender adapter as a separate GPL component.

---

## 12. Repo layout (suggested; improve it if you have a better one)

```
waddle/
  desktop/          # PySide6/QML app: character, overlays, bubble, settings, tray, hotkeys, voice, sensors
  capture-helper/   # tiny native binary authorized for KWin ScreenShot2
  brain/            # FastAPI: orchestration, guide engine, retrieval, model clients, tools
  adapters/         # blender/ (extension + adapter), generic/, resolve/, kdenlive/, krita/, gimp/, obs/
  knowledge/        # per-app knowledge packs (YAML), incl. kde-plasma/, ubuntu/
  inspectors/       # read-only file inspectors (ffprobe, exiftool, blend header, ...)
  kwin/             # KWin script, window-rule installer, .desktop files
  deploy/           # docker-compose.yml, searxng config, systemd --user units
  evals/            # screenshot fixtures, labels, eval + bench scripts
  docs/             # VISION, ARCHITECTURE, PLAN, DECISIONS, PRIVACY, ADAPTERS
  tests/
  install.sh  uninstall.sh
```

---

## 13. Phases (finish, test, and demo each before moving on)

1. **Skeleton & character:** code-drawn penguin with all states, bubble + guide card + step pill (mock data), tray icon, KWin window rule installer, click-through, dodge behavior, theme following, reduced motion. *Done when:* Waddle sits bottom-right above all windows, animates smoothly, is draggable per monitor, and idle CPU is ~0%.
2. **Senses:** KWin script (focus, geometry, fullscreen, cursor), capture helper + ScreenShot2 with portal fallback, change-gating with live-region masking, global hotkeys with conflict check, text input, lock/idle handling, blocklist. *Done when:* Waddle names the focused app in <100 ms, captures only the active window only when allowed, and goes blind on lock or blocked windows.
3. **Brain + identity:** docker compose (Ollama, SearXNG, API), WebSocket link, full version waterfall incl. "show me Help → About", edition/locale detection, installed-app inventory, Q&A with screen context, graceful degradation. *Done when:* "what am I looking at?" in Blender gives a correct, version-aware answer, and pulling the brain's plug shows `napping-brain` without crashing.
4. **Perception + knowledge:** AT-SPI integration, OCR, knowledge packs (Blender, Resolve, Kdenlive, KDE Plasma, Ubuntu), version-pinned search/fetch/index, citations, nearest-version warnings, offline cache, man/tldr lookup for the terminal.
5. **Guide engine + Blender adapter:** structured plans, prerequisite checks, verification loop, corrections, error-dialog detection, "Show me where" highlighting (correct under fractional scaling and multi-monitor), Walkthrough/Coach/Challenge modes, "Why?". *Done when:* Waddle walks me through "add a cube, give it a glossy material, render with Cycles" and checks off every step automatically, in both Walkthrough and Coach mode.
6. **Voice:** push-to-talk with faster-whisper, Piper/Kokoro TTS, lip-sync, interruption, mic indicator.
7. **System awareness + GPU etiquette:** telemetry, alerts, crash explanation, cross-referencing system and app facts, auto-unload on render, Focus mode, "why is my PC slow?"
8. **Magic layer:** rewind buffer ("what just happened?"), region-select "What's this?", drop-a-file inspector, multi-app guides, tutorial translator, "what's new for you" after updates, skill profile, guide library export/import, efficiency tips + bubble budget, optional notification explainer.
9. **Polish & ship:** onboarding flow, settings window, `waddle doctor`, evals + model bake-off, PRIVACY.md, uninstall, stub adapters documented, README with GIFs.

---

## 14. Non-goals (don't build these)

- Any form of automation or input injection, now or "later as an option".
- Keylogging, reading `/dev/input`, or reading the clipboard.
- Cloud-by-default anything, accounts, or telemetry.
- Flatpak/Snap packaging of Waddle itself.
- Wake-word / always-listening voice (push-to-talk only in v1).
- First-class GNOME/X11 support (best effort only).

---

**Start now:** check current library/API versions and Blackwell compatibility, then write `docs/VISION.md` (this prompt), `docs/ARCHITECTURE.md`, `docs/PLAN.md`, and `docs/DECISIONS.md`, and show me ARCHITECTURE and PLAN before writing any implementation code.
