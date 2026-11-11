# Waddle architecture

Status: **proposed — awaiting review before Phase 1.**
Source of truth: [VISION.md](VISION.md). Evidence: [RESEARCH.md](RESEARCH.md). Reasoning and deviations: [DECISIONS.md](DECISIONS.md) (`D-xxx`). Open spikes: [PLAN.md](PLAN.md) (`Sx.y`).

Target: Ubuntu 26.04, Plasma **6.6** Wayland, Qt 6.10, system Python 3.14, RTX 5090.

Nothing in this document has been run on the target machine yet. Mechanisms that rest on unconfirmed facts name the spike that settles them.

---

## 1. Invariants

These hold in every phase. Each is enforced by a test or a structural property, not by convention.

| # | Invariant | How it is enforced |
|---|---|---|
| I1 | **Read-only.** Waddle never injects input, changes another app's state, edits user files, or runs state-changing commands against anything but itself. | No input-injection dependency exists in the tree (CI denylist: `uinput`, `ydotool`, `xdotool`, `/dev/input`, `libei`, RemoteDesktop portal). Observation subprocesses go through `readonly_exec` (fixed argv allowlist, sandboxed, §4.12). The model has no shell tool. |
| I2 | **Own resources only.** Writes are limited to Waddle's config/data/cache/runtime dirs, its own windows, models, and containers, plus the sanctioned session registrations listed below. | A second, separately enumerated `own_resources_exec` list (rule install/remove, KWin `reconfigure`, `loadScript`, compose with a fixed project name, systemd user units), callable only from the installer, the CLI, and the VRAM governor. Path guard on file writes. |
| I3 | **Pixels are contained.** Raw frames exist only inside the capture helper; encoded frames exist only in memory. | Helper returns encoded bytes over an inherited socket. A test greps Waddle's data, cache, and runtime dirs after an end-to-end run. The only exceptions are the debug flag and the explicit `waddle eval capture` command. |
| I4 | **Blind means blind.** On lock, on a blocked window, or when paused, nothing is captured and the rewind buffer is cleared. | Evaluated in the desktop process; lock and blocklist are **re-checked inside the helper before and after every capture**, and pause/blind is latched in the helper (§4.3). |
| I5 | **Content is data, never instructions.** Everything not typed by you or shipped in the first-party tree is untrusted (§5.6 lists it). | Static tool registry with typed, validated arguments; untrusted text only ever enters labelled data blocks; injection fixtures in CI. |
| I6 | **No network listeners.** The host↔brain link is a WebSocket over a user-private Unix socket; no service publishes a TCP port. | Compose file has no `ports:`; a test asserts no Waddle process listens on TCP. |
| I7 | **The mic is open only while push-to-talk is held.** | The audio source object is created on `Activated` and destroyed on `Deactivated`; no other code path opens it. |
| I8 | **No model-controlled egress.** A model cannot choose where data is sent. | The API container has no route out. Fetches accept only URLs that came from a search result or a knowledge-pack template; search queries are built by code (§5.5). |

**Sanctioned session registrations** (the complete list of things Waddle changes outside its own files): one KWin window rule; its own KWin scripts, loaded at runtime; its global shortcuts; its portal app registration and screencast permission. Each is recorded in an install manifest and removed by `uninstall.sh`.

Places where a literal reading of the vision would break an invariant, and what we do instead, are in §12.

---

## 2. System overview

```
 ┌──────────────────────────── host (native, user session) ─────────────────────────────┐
 │                                                                                       │
 │  KWin 6.6 ──callDBus──▶ ┌─────────────────────── waddle-desktop ──────────────────┐  │
 │   ├ waddle-core.js      │  UI thread (Qt/QML)        │  I/O thread (asyncio)       │  │
 │   └ waddle-cursor.js    │   penguin · bubble · card  │   D-Bus (dbus-fast)         │  │
 │                         │   overlays · tray · panels │   sensors · gates · identity│  │
 │  ScreenShot2 ◀──D-Bus── │                            │   telemetry · adapters      │  │
 │        ▲                │   ◀── queued Qt signals ──▶│   guide runner · link       │  │
 │        │                └───────────┬────────────────┴───────────┬─────────────────┘  │
 │  ┌─────┴──────────┐   socketpair    │                            │ a11y thread        │
 │  │ waddle-capture │◀────────────────┘                            │ (AT-SPI, GLib)     │
 │  │ (Rust)         │  encoded frames only                         │                    │
 │  └────────────────┘                                              │                    │
 │   Blender ext ◀── unix socket (read-only state) ─────────────────┘                    │
 └──────────────────────────────────────┬────────────────────────────────────────────────┘
                 WebSocket over $XDG_RUNTIME_DIR/waddle/brain.sock  (0700 dir, token)
 ┌──────────────────────────────────────┴──────── waddle-brain (Docker Compose) ─────────┐
 │  waddle-api ── orchestration · planner · retrieval · tool layer · VRAM governor       │
 │     │ (no route out)                                                                  │
 │     ├── ollama / vLLM        models network, no egress                                │
 │     ├── waddle-voice         STT + TTS workers (CUDA 12 userland)                     │
 │     ├── waddle-ocr           OCR worker (CUDA 13 userland)                            │
 │     └── waddle-fetch ──▶ searxng ──▶ internet     the only containers with egress     │
 │  SQLite + FTS5 + sqlite-vec  (bind-mounted data dir, 0700)                            │
 └────────────────────────────────────────────────────────────────────────────────────────┘
```

**What lives where, and why**

| Concern | Host | Brain | Reason |
|---|---|---|---|
| Rendering, overlays, hotkeys, mic capture, audio playback | ✔ | | needs the session |
| Window/cursor/lock/DND sensing, privacy and proactivity gates | ✔ | | must work with the brain down |
| App identity (process, packaging, `--version`, caption patterns), inventory | ✔ | | needs `/proc`, package DBs; <100 ms |
| System telemetry, alerts | ✔ | | NVML per-process data is host-only |
| Adapters, AT-SPI, file inspectors, man/tldr | ✔ | | need host processes and files |
| Guide **runner** (step state machine, deterministic checks) | ✔ | | <1 s verification; saved guides work offline (D-021) |
| Guide **planner**, visual verification, drift correction, Q&A | | ✔ | needs models |
| OCR, STT, TTS inference | | ✔ | keeps the host under 150 MB (D-016) |
| Search, fetch, extract | | ✔ (`waddle-fetch`) | all internet access in one small container that holds no user data |
| Docs, history, profile storage | guide library | ✔ | |

The host is useful on its own: with the brain stopped it still shows the penguin, names apps and (cached or metadata-derived) versions, raises system alerts, and replays saved guides whose checks are deterministic.

---

## 3. Repository layout

A `uv` workspace with one lockfile (D-002).

```
waddle/
  packages/
    core/            waddle-core: pydantic schemas, link protocol, guide state machine,
                     predicates, version normalization, coordinate math. Pure Python, no I/O.
    desktop/         waddle-desktop: PySide6/QML app, sensors, identity, telemetry, CLI
      src/waddle_desktop/{ui,qml,sensors,identity,telemetry,guide,voice,link,cli}/
    brain/           waddle-api: FastAPI app, model clients, retrieval, planner, tools, governor
    fetch/           waddle-fetch: search + fetch + extraction service
    voice/  ocr/     GPU worker services
  capture-helper/    Rust crate: waddle-capture
  adapters/
    blender/         extension (blender_manifest.toml) + legacy add-on shim + host client
    generic/  resolve/  kdenlive/  krita/  gimp/  obs/
  inspectors/        read-only file inspectors
  knowledge/         packs: blender/ resolve/ kdenlive/ kde-plasma/ ubuntu/ …
  kwin/              waddle-core.js, waddle-cursor.js, rule installer, .desktop files
  deploy/            docker-compose.yml, Dockerfiles, searxng/settings.yml, systemd user units
  evals/             labels, eval + bake-off scripts (fixture images live outside the repo)
  docs/  tests/  install.sh  uninstall.sh
```

`waddle-core` is the contract between the halves: both import the same schema and protocol definitions, so they cannot drift.

---

## 4. Host: `waddle-desktop`

### 4.1 Process and threading model

One Python process plus one native child.

- **UI thread** — Qt event loop. QML scene, tray, overlays. No blocking calls, no D-Bus, no sockets.
- **I/O thread** — one asyncio loop: `dbus-fast`, the link client, adapter sockets, sandboxed subprocesses, the telemetry sampler, the guide runner.
- **a11y thread** — AT-SPI via PyGObject with its own GLib loop. Isolated because accessibility calls into a hung app can block; every call has a timeout.
- **`waddle-capture`** — Rust child process, spawned on demand, exits after an idle period.

The UI and I/O threads talk only through queued Qt signals carrying immutable `waddle-core` messages (D-003).

**Budgets.**
- *Idle CPU ~0 %.* Sensors are pushed, never polled. The only periodic work is the 2–5 s telemetry sample and a sparse blink timer; all animations stop when Waddle is hidden, occluded, or asleep.
- *RAM < 150 MB* is measured as the PSS of the `waddle-desktop` process with every planned import loaded; `waddle-capture` is reported and capped separately. This is the tightest budget — a bare PySide6 + QML process is most of it. The host therefore never imports numpy, never holds raw pixels, loads Settings/History/Eyes lazily, and runs no model code. Spike S1.1 measures the real floor before Phase 1 builds on it; if it cannot be met, the fallback is moving AT-SPI and audio into on-demand child processes, and if that is not enough I will tell you rather than quietly miss it.

### 4.2 Windows, placement, and the penguin

A Wayland client has no say over position or stacking, so placement is a three-part arrangement (D-004):

1. **KWin window rule** (installed once, idempotent, reversible) scoped to the **penguin window and the highlight overlay only** — matched by class plus exact title, or a per-window app id if Qt allows (S1.2). It sets keep above, skip taskbar/pager/switcher, no border, all desktops, initial position. Settings, History, Eyes, and the region-select overlay are ordinary windows and are not matched. The installer writes with `kwriteconfig6`, calls `org.kde.KWin.reconfigure`, and records what it added so removal restores the file exactly.
2. **Waddle's own KWin scripts** move *Waddle's own windows*. Following the active monitor is handled inside `waddle-core.js`. Host-initiated moves (restore a saved position, step aside from a highlight) use a short generated one-shot script with the target geometry baked in: load, run, unload — the same pattern as a one-shot cursor query. Every script checks the window class and title first; a test asserts no other window is ever touched.
3. **User drag** uses `QWindow.startSystemMove()`; `waddle-core.js` reports Waddle's new geometry so the position is remembered per output.

| Window | Size | Input | Notes |
|---|---|---|---|
| Penguin + bubble/card | small, grows with bubble | mask = penguin ∪ bubble | does not take focus when shown, accepts it when you click the card's input field; mask re-applied on every show (QTBUG-62638) |
| Highlight overlay | fullscreen, one per output | `WindowTransparentForInput` | exists only while a highlight is visible |
| Region-select overlay | fullscreen, one per output | takes input while dragging | only after the hotkey |
| Settings, History, Eyes | normal | normal | lazy-loaded |

**Stacking is not yet proven.** An active fullscreen window, and open menus, may sit above a keep-above window. Spike S1.2 tests the penguin and the overlay against a fullscreen app and an open dropdown using a plain keep-above rule, a forced layer rule, and layer-shell (S1.3). If nothing rule-based clears popups, a target inside an open popup is described in words.

**Dodging.** A read-only Wayland client gets no pointer data outside its own surfaces, so "the cursor approaches" is implemented in two tiers:
- When `waddle-cursor.js` is already loaded (watching, guide active), true proximity is used.
- Otherwise: on pointer entry into the penguin *body*, with no bubble/card open and no drag-and-drop entering, after a short dwell without a press (a press within the dwell is a click or the start of a drag), the penguin slides aside and the input mask shrinks to its new footprint so clicks reach the app underneath. He returns on a timer, not on pointer-leave, to avoid oscillation.

**Fullscreen apps.** `waddle-core.js` reports `fullScreen`; Waddle shrinks to an edge badge or hides (setting), and a "bring Waddle back" hotkey restores him.

**The character** is drawn entirely with QML `Shape` paths: body, belly, face, beak, feet, flippers, scarf. State is one enum property; each of the eleven states is a QML `State` with transitions. The scarf binds to the Plasma accent color; animation durations multiply by `AnimationDurationFactor` (0 ⇒ reduced motion). Colors and fonts are read from `kdeglobals` and watched, because pip-installed PySide6 ships its own Qt and cannot load the Plasma theme plugin (D-006). Bubble and card expose accessible names and roles for screen readers.

### 4.3 Capture: `waddle-capture`

KWin 6.6 authorizes ScreenShot2 callers by executable path, first match wins. Authorizing the Python interpreter would grant screen capture to every Python script on the machine, so capture lives in a dedicated Rust binary at its own path with its own `.desktop` entry (D-005).

```
desktop ──socketpair(fd 3)──▶ waddle-capture ──D-Bus──▶ KWin ScreenShot2
        ◀── encoded frames ──                 ◀─ pipe ── raw pixels
```

- **No listening socket.** The helper is spawned by the desktop with an inherited socketpair and exits when it closes.
- **One verb: `capture_window(uuid)`.** No area, screen, or workspace capture is compiled in. The desktop passes the *target* window — the last active window that is not one of Waddle's own (so typing in Waddle's input field does not change what he looks at).
- **Options fixed in the helper:** `include-shadow=false`, `include-decoration=false`, `native-resolution=true`, `include-cursor=false`.
- **The gate is enforced again in the helper, around the capture.** Before capturing it checks logind `LockedHint` and matches the target's class and title (from KWin's own `getWindowInfo`) against the blocklist file. After capturing it re-reads the window info and the `windowId` in the reply, and **discards the frame** if the title became blocked (a browser tab switch) or the id differs. A `blind` command from the desktop is latched: the helper refuses everything until it is lifted.
- **All pixel work happens here:** live-region masking, change detection (downscaled diff + perceptual hash), the downscaled overview, the full-resolution crop, encoding. The reply is `unchanged`, or `{overview, crop, scale, geometry, hash}`. Raw frames never cross into Python (I3) and unchanged frames never reach a model.

**What the helper does and does not guarantee.** The helper independently enforces *lock, blocklist, and pause*. That the target is the window you are actually working in is decided by the desktop and the KWin script; the helper cannot verify it. Any process running as you could launch the helper itself — path-based authorization cannot prevent that, and on Plasma 6.8+ KWin performs no check at all. What the helper buys is: no accidental grant to every Python process, a capture surface limited to single non-blocked windows, and a privacy gate that survives a bug in the Python code. `PRIVACY.md` will say exactly this.

**Menus and popups** are separate surfaces and may be missing from a window capture. The helper captures the target plus its own transient popups (by UUID, reported by the KWin script, each passing the same checks) and composites them at their offsets (D-014). Whether `CaptureWindow` accepts popup surfaces is spike S2.1.

**Region-select** stays a single-window capture: the rectangle you drag is resolved to the topmost non-Waddle window under it, that window is captured through the normal gated path, and the crop is cut in the helper. A selection never includes a second application, and a blocked window under the rectangle means eyes closed and no capture (D-033).

**Fallback** (reported by `doctor`): if ScreenShot2 is unavailable, the helper uses the portal ScreenCast with a window source. This mode is weaker and the UI says so: the portal streams the one window *you* pick, so each new app needs a pick; frames are taken only while that window is the target; the stream is stopped on blind and whenever the target differs; a monitor source is never requested. The `spectacle` last resort from the brief is **not** implemented (D-035).

### 4.4 Sensors and gates

Everything is pushed.

**KWin scripts**, loaded at runtime through `org.kde.kwin.Scripting.loadScript` and unloaded on exit (D-007). Nothing is written to `kwinrc`; a name-owner watch reloads them if KWin restarts.

- `waddle-core.js` (always loaded): `windowActivated` → `{uuid, caption, resourceClass, resourceName, desktopFileName, pid, frameGeometry, clientGeometry, bufferGeometry, output, scale, fullScreen, xwayland}`; coalesced geometry/fullscreen/caption changes of the target; desktop changes; the target's transient popups; Waddle's own window geometry. Activations of Waddle's own windows are reported as such and do not change the target.
- `waddle-cursor.js` (loaded only while needed): throttled `cursorPosChanged`, ≤10 Hz. Scripts cannot receive calls, so **loading and unloading this script is the on/off switch**.
- Fallbacks: `kdotool` if present, then low-rate `getWindowInfo` polling; `doctor` says which is active.

**Session state** (D-Bus signals and property changes): screen lock (`org.freedesktop.ScreenSaver.ActiveChanged` and logind `LockedHint`), idle, DND (`Inhibited`), theme changes, failed systemd units, Docker events. Other apps' active screencasts are detected read-only for auto-hide, excluding Waddle's own portal stream (mechanism: S2.7).

**Notifications** (opt-in, off by default) are not a signal: seeing other apps' notifications means observing `Notify` method calls on the session bus. This needs a dedicated monitoring connection that exists only while the opt-in is on, with a single match rule; the mechanism on Plasma 6.6 is spike S8.1. Contents stay in memory (the last few, short lifetime), are cleared on lock and pause, are subject to the blocklist by sending app, and never reach history unless you ask about one.

**Hotkeys.** Register the app id with the host portal registry, then bind through the GlobalShortcuts portal; `Activated`/`Deactivated` give true hold-to-talk. Before proposing defaults the installer asks KGlobalAccel which keys are taken (read-only query) and offers alternatives. Actions: text input, push-to-talk, pause, "Show me where", region-select, quiet mode, Focus mode, bring Waddle back. Esc is never registered globally.

**The privacy gate** is one pure function: `(window, session state, config) → allow | blind(reason)`. Every capture, AT-SPI read, rewind-buffer write, adapter read, and host tool call passes through it. On `blind`, the penguin closes his eyes, the rewind buffer is cleared, and the helper is latched.

**The proactivity gate** is a second pure function, owning the anti-Clippy rules: `(kind: answer | alert | tip, DND, quiet mode, focus mode, idle, fullscreen, budget state, muted categories) → show | defer | drop`. Every unsolicited bubble goes through it. Answers to your own requests always pass. Quiet mode means no tips and no speech; alerts are shown silently. The tip budget (≤3/hour, lowered when you dismiss) and "don't show tips like this" live here, so alerts (Phase 7) and tips (Phase 8) only register categories.

**Rewind buffer:** ~30 s ring of encoded low-res keyframes (written only on meaningful change) plus adapter and window events, in memory in the desktop process.

### 4.5 Perception sources

Every observed fact is a `Fact{value, source, confidence, observed_at}` where `source ∈ adapter | atspi | ocr | vlm | kwin | system | docs`. The source travels to the citation chip in the bubble.

1. **Adapter** (§4.7) — authoritative for state *values*. Names it reports (objects, materials, files) are still untrusted text.
2. **AT-SPI** — find by role/name, read states, get window-relative extents; scoped to the target window and pruned to the focused subtree. Unavailable ⇒ fall through silently. **Waddle does not switch accessibility on** (§12).
3. **OCR** (brain) — exact strings.
4. **VLM** (brain) — layout, state, intent, grounding.

### 4.6 App identity and version

A pipeline of small pure resolvers over a `ProcessSnapshot`, each unit-testable from fixtures:

1. `window pid → real binary` (walk wrappers: Electron, shell launchers, AppImage runtime, Flatpak `bwrap`).
2. `packaging` (Flatpak `.flatpak-info`, Snap cgroup/env, AppImage, `dpkg -S`, `/opt` version files). From `/proc/<pid>/environ` only `SNAP_*`, `APPIMAGE`, and `FLATPAK_ID` are extracted; the raw block is never stored, logged, or sent.
3. `allowlisted --version` in the sandbox (§4.12).
4. `caption patterns` — a version regex from the knowledge pack over the window title. No pixels, no brain.
5. `OCR of title bar / About dialog` (brain).
6. `ask you` — the "open Help → About" flow.

Result: `{app_id, name, version, edition, channel, locale, keymap, packaging, confidence, source}`, cached in the host SQLite keyed by `(app_id, exe path, size+mtime | package version)`. A cached lookup is one indexed read. With the brain down, steps 1–4 and the cache still work; versions that need OCR wait until it is back.

Web apps are identified from the browser window title against pack patterns and reported as "version not visible". The installed-app inventory is built lazily and refreshed on file-change notifications.

### 4.7 Adapters

```python
class Adapter(Protocol):
    id: str
    def detect(self, app: AppIdentity) -> bool: ...
    async def get_state(self) -> AppState: ...
    def subscribe(self) -> AsyncIterator[AdapterEvent]: ...
    async def undo_history(self) -> list[str] | None: ...
    async def keymap(self) -> Keymap | None: ...
    def doc_sources(self, version: Version) -> list[DocSource]: ...
    def ui_landmarks(self) -> list[Landmark]: ...
```

There is no method that takes a mutation. The wire protocol to in-app components is request/response with a closed set of read verbs.

**In-app components are installed by you, not by Waddle.** Installing a Blender extension, a Krita or GIMP plug-in, or enabling obs-websocket changes another application's configuration. Onboarding shows the file and the exact command with a Copy button; Waddle only connects once the component is there (D-036).

**Blender extension** (`blender_manifest.toml`, GPL-3.0-or-later, with a legacy `bl_info` shim):
- A timer on Blender's main thread builds a small immutable snapshot (mode, workspace, editor, engine, selection, active material/nodes, modifiers, frame, playback, shading, history, keymap preset) only when something changed and a client is connected.
- A background thread serves that snapshot over a Unix socket in `$XDG_RUNTIME_DIR/waddle/` (`0600`). The thread never touches `bpy`.
- Render start/finish handlers feed GPU etiquette (§5.4).
- **Read-only by construction:** a CI test parses the extension's AST and fails on any `bpy.ops` call, any assignment to a `bpy` data attribute, or any file write.
- **History is tiered by Blender version**, always by pure property reads: 5.3+ reads `wm.undo_stack` (step names) and `wm.reports` (the Info log); 5.2 LTS reads `wm.reports` only; 4.2–5.1 has only `wm.operators`, a list of at most 32 recent operators that Blender clears on every undo. The adapter reports its tier, so Waddle can say "I can't see your undo history on this Blender version". `bpy.ops.info.*` and `bpy.ops.ed.undo_history` are never called: they write the clipboard, change selection, or perform an undo (D-023).
- Runs on Python 3.11 (Blender 4.2–5.0) and 3.13 (5.1+), with no third-party dependencies.
- Waddle never changes Blender's "Allow Online Access" preference. If spike S5.1 shows the socket needs it, the adapter reports "unavailable" and the choice comes back to you.

**Stubs with documented capabilities:** Resolve (an XWayland client with no usable accessibility tree, so vision is the baseline; on Studio, only if you have already enabled external scripting, an allowlist of getters such as `GetCurrentPage` and `IsRenderingInProgress`; nothing is installed into Resolve's script folders — D-024), Kdenlive (AT-SPI), Krita, GIMP 3, OBS (obs-websocket read requests only).

### 4.8 System awareness

One sampler coroutine, 2–5 s adaptive (slower when idle or locked): NVML (temperature, power, utilization, encoder/decoder, per-process VRAM), psutil, sensors.

Event-driven where the system offers events: systemd unit failures (`PropertiesChanged`), the Docker events stream (GET-only client), and the exit status of the focused app's process for crash detection. On demand only: `journalctl -p err`, disk growth, coredumps, crash logs.

A 10-minute rolling summary is kept as a few hundred bytes of aggregates. The alert engine is a table of `(predicate, threshold, cooldown)` rows, and its output goes through the proactivity gate. All of this is host-side, so alerts work with the brain down.

### 4.9 Voice I/O on the host

The host only moves audio. While PTT is held it streams 16 kHz PCM to the brain and draws the waveform; on release it sends `end`. TTS audio streams back and is played through Qt Multimedia, with the output level driving the beak.

Speech stops on push-to-talk, any new request, a click on the penguin, tray Mute, or the quiet-mode hotkey. Esc stops it only while a Waddle window has keyboard focus — binding Esc globally would steal it from every other app (D-037).

### 4.10 The guide runner

The step state machine lives in `waddle-core` and runs on the host:

```
PLANNED → SHOWING → OBSERVING ──check passes──▶ CELEBRATE → next step
                       │  ▲
            drift ─────┘  └──── PAUSED (you asked a question)
                       ▼
                  CORRECTING
```

Each step's `success_check` is tried in order of reliability: adapter predicate → AT-SPI state → visual check. The first two are evaluated locally, in milliseconds, on events. The third costs a capture and a model call.

**When the runner looks.** Captures during `OBSERVING` are triggered by events — KWin caption/geometry/popup changes, adapter and AT-SPI events, the cursor settling — rate-limited to ≤1 fps. For visual-only apps with no such events, a low-rate timer is the backstop (the one deliberate exception to "nothing polls", active only during a guide). The helper drops unchanged frames before any model sees them.

Coach-mode hint escalation is a timer on `OBSERVING`. Multi-app guides key each step by app id and resume on focus change. Saved guides are flagged for re-validation when the identity cache reports a new version of their app.

**Predicates are a small declarative language** (`state.engine == "CYCLES"`, `atspi(role=checkbox, name~"Proxy").checked`), never code, so guides from disk or from a model cannot execute anything (D-015). `~` is a substring match with a length cap; `atspi()` is scoped to the step's app window and passes the privacy gate.

**The highlight** clears on a helper-reported frame change of the target, a focus change, step verification, or a timeout. Clicks are not observed (D-037).

### 4.11 Coordinates

One module in `waddle-core`, one type per space, explicit conversions only:

```
ImagePx(x, y)            pixels in the captured buffer
  ÷ capture scale
WindowLogical(x, y)      relative to the captured window's origin
  + window origin               (from KWin, logical, may be fractional)
GlobalLogical(x, y)      compositor space
  − output.geometry.topLeft
OverlayLocal(output, x, y)
```

- Model outputs enter as `ImagePx` after the per-model grounding codec normalizes them (0–1000 `[x1,y1,x2,y2]`, Gemma's `[y1,x1,y2,x2]`, or absolute pixels).
- AT-SPI extents enter as `WindowLogical` with a per-toolkit origin correction, and a separate path for XWayland clients.
- **Which origin and scale a capture really has** — client, frame, or buffer geometry, for server-decorated Qt, client-decorated GTK, and XWayland windows at fractional scales — is measured by spike S2.1 and may be per-toolkit. The geometry used is always the KWin snapshot for the captured UUID. If the image size disagrees with geometry × scale by more than a pixel (the window moved, or an unknown decoration case), the highlight is dropped and the location is described in words.
- Property tests cover 100/125/150/175/200 % scales, mixed-scale multi-monitor layouts, negative-origin outputs, client-side decorations, and XWayland.

### 4.12 Sandboxed execution

Everything the host runs to *observe* goes through `readonly_exec`: a fixed table of argv templates, no shell, and one sandbox profile — `bwrap --unshare-all --die-with-parent --new-session`, read-only binds of system directories plus only the target file, tmpfs home and `/tmp`, no D-Bus, Wayland, or runtime dir, a cleared environment, `prlimit` on memory/CPU/file size/process count, capped output, and a wall-clock kill.

This covers file inspectors (`file`, `ffprobe` with `-protocol_whitelist file`, `mediainfo`, `exiftool`, archive *listing*), `man`, and allowlisted `--version` calls. Dropped files are hostile input to parsers with real CVE history, so this **fails closed**: if the sandbox cannot be created, the step is skipped, the UI says so, and `doctor` reports it. The `.blend` header parser is a bounded read of the first bytes, also inside the sandbox when the file is compressed.

---

## 5. Brain: `waddle-brain`

### 5.1 Services

| Service | Image | GPU | Networks | Holds user data |
|---|---|---|---|---|
| `waddle-api` | ours (Python 3.13, CPU-only) | no | models, svc — **no route out** | yes |
| `ollama` (default) or one `vllm` per role | upstream | yes | models — no route out | no |
| `waddle-voice` | ours (CUDA 12 userland) | yes | svc | no |
| `waddle-ocr` | ours (CUDA 13 userland) | yes | svc | no |
| `waddle-fetch` | ours (CPU) | no | svc, egress | **no** — no token, no DB mount |
| `searxng` | upstream, unmodified | no | egress | no |

- No service publishes a port. The host reaches `waddle-api` through a Unix socket in a `0700` runtime directory bind-mounted into the container (D-034).
- `waddle-api` holds frames, history, and the profile, and cannot reach the internet. `waddle-fetch` can reach the internet and holds nothing. Enabling a remote model provider is the one thing that gives the API a route out, through a compose profile limited to that provider; the penguin and tray show it (§5.3).
- Model pulls are an explicit `waddle models pull` step that attaches a network for the duration.
- Containers run as your UID; the data directory is `0700`, so `waddle forget` and uninstall can delete it without root. Upstream services run at log levels that record no prompts or queries, checked by a test.
- **Native mode** (`waddle brain --native`) runs the same services from the uv workspace, with separate environments for the CUDA 12 and CUDA 13 workers. It keeps the Unix socket but loses the container network boundaries; `doctor` says so.

### 5.2 Link protocol and the host tool contract

One WebSocket over `$XDG_RUNTIME_DIR/waddle/brain.sock`. A token (generated at install, `0600`, mounted read-only into the container, compared in constant time) is a second factor. Envelopes are versioned and defined in `waddle-core`; JSON for control, binary frames for images and audio; a maximum message size is enforced on both sides.

```
{ "v": 1, "id": "...", "type": "ask" | "answer.delta" | "frame" | "verify" | "ground"
                       | "guide.plan" | "stt.audio" | "tts.audio" | "tool.call" | "state" ... }
```

The brain is the half that reads the internet and runs models, so what it may ask of the unsandboxed host is fixed and narrow. Schemas live in `waddle-core` and are validated **on the host**:

| Tool | Arguments | Notes |
|---|---|---|
| `get_app_state()` | none | adapter + identity for the current target; passes the privacy gate |
| `get_system_stats(topic)` | enum | typed, size-capped; journal and crash-log text only for a system question you asked |
| `inspect_file(handle)` | opaque, expiring handle | minted only when you drop a file; **never a path** |
| `man(name, section?)`, `tldr(name)` | name matching `^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`, section enum | fixed argv, `--` terminator, pager variables cleared, sandboxed |

A `tool.call` must reference an open request of yours and is capped per request; anything else is rejected and logged.

### 5.3 Model layer

```python
class ModelProvider(Protocol):
    async def chat(self, role: Role, messages, *, schema: type[BaseModel] | None, images=()) -> Stream
    async def embed(self, texts: list[str]) -> list[Vector]
    async def load(self, role: Role) -> None
    async def unload(self, role: Role) -> None
    async def status(self) -> list[LoadedModel]
```

- **Implementations:** Ollama (`format` = JSON schema); vLLM (`response_format` json_schema — the old `guided_*` API is gone; one process per role); OpenAI-compatible and Anthropic remote providers.
- **Remote providers are off by default.** Each has two flags, both defaulting to false: `allow_frames`, and `allow_screen_text` (OCR, AT-SPI, window titles, rewind events, adapter state, file metadata, notifications). With both off, a remote provider receives only your question, retrieved public doc chunks, and the app name and version. OCR, STT, TTS, and embedding are always local. While a remote provider is serving requests the penguin wears a badge and the tray icon changes; Waddle's eyes shows what was sent to whom.
- **Roles** (`fast_vlm`, `smart_model`, `embedder`) map to model names only in config. Each entry also sets its grounding codec, a fixed `num_ctx` (changing it reloads the model), and its keep-alive policy.
- **Structured calls:** pydantic schema → constrained decoding → validate → at most two repair retries → a typed failure the UI can show honestly.
- **Prompt assembly:** `stable system prefix | app+version block | retrieved docs | observation | question`. The app+version block holds only schema-validated fields. The assembler enforces a token budget per role and trims doc chunks itself rather than letting the server truncate.

### 5.4 GPU etiquette

A VRAM governor in `waddle-api` owns every load and unload. Inputs: host telemetry (free VRAM, per-process usage), adapter render events, "fullscreen game focused", Focus mode.

| Profile | Loaded | Weights only, Q4 (estimate) |
|---|---|---|
| `full` | fast + smart + embedder + STT + OCR | ~26–28 GB |
| `balanced` (default) | fast + embedder + STT + OCR resident; smart on demand | ~9 GB resident |
| `lean` | fast + embedder | ~7 GB |
| `focus` | nothing | 0 |

- These are sums of model weights from published sizes. Context/KV cache, vision encoders, and per-process CUDA contexts come on top; real numbers are measured in spike S3.2 and replace this table.
- `full` leaves almost nothing for Blender or Resolve, which is why it is not the default (D-025).
- **`smart_model` in `balanced`:** its idle timeout is a config key. It is pre-warmed when the text input opens, push-to-talk is pressed, or a guide starts. If it is still loading when the question arrives, the penguin shows `thinking` with an honest "waking up" line. The 1.5 s first-words budget is defined, and measured, for warm models; `waddle bench` reports cold starts separately.
- **Release mechanism.** Ollama/vLLM roles unload via `keep_alive: 0` or process stop. STT and OCR run in GPU worker processes that the governor terminates in `lean`/`focus`; a process that merely frees its tensors usually keeps its CUDA context, so only exit returns the memory. In those profiles OCR falls back to the CPU provider on demand, and the first push-to-talk reloads STT. Focus mode is done when host NVML shows no Waddle PID holding VRAM.
- On a render start or low free VRAM the governor steps down a profile, tells you once, and steps back up after a quiet period with hysteresis. It only ever addresses Waddle's own processes and containers.

### 5.5 Knowledge engine

```
question + AppIdentity + UI state
   → hybrid retrieve (FTS5 BM25 + vector KNN, filtered by app + version)
   → enough? ──no──▶ waddle-fetch: search (SearXNG) → fetch → extract
                      → chunk → embed → store with {app, version, url, trust, fetched_at}
   → answer from retrieved chunks + observed facts only, with citations
```

- **Knowledge packs** (YAML, schema-validated): doc domains, versioned URL templates, release-notes sources, trust tiers, locale variants, platform notes, live-region hints for change-gating, version-detection hints (caption and About-dialog patterns, `--version` allowlist entry).
- **Egress rules (I8).**
  - *Search queries are built by code* from the app, the normalized version, and your question, plus at most one short, visible, quoted screen string such as an error message. They are single-line, length-capped, and budgeted per request.
  - *Fetch takes no free-form URL.* It accepts a result id from a search in the same request, or a pack URL template filled from the normalized `Version` type — never from raw OCR text or model output.
  - *Fetch policy:* http/https on ports 80/443 only; resolve, then connect to the vetted address; reject loopback, private, link-local, CGNAT, ULA, multicast, IPv4-mapped ranges and the compose subnets; re-validate every redirect (max 5); cap compressed and decompressed size; no cookies or credentials; robots.txt honored; no JavaScript.
  - Every outbound query and URL is listed in Waddle's eyes.
  - *Residual, stated plainly:* a short search query does reach SearXNG's upstream engines. Injected content could at most influence a few words of one. `PRIVACY.md` will say so.
- **Nearest-version fallback** is a first-class result: the retriever returns `{chunks, version_used, exact: false}` and the answer template must surface it.
- **Storage:** one SQLite file: a chunks table, an FTS5 index, embeddings as plain float32 BLOBs (the canonical copy), and a sqlite-vec `vec0` table as a **derived, rebuildable** index partitioned by app. BM25 and vector results are fused in Python above a small `VectorStore` interface (D-020). sqlite-vec is pre-1.0, brute-force, and has a single maintainer who has been quiet since May 2026; because the canonical vectors are ordinary BLOBs, swapping in a numpy or `sqliteai-vector` backend is a rebuild, not a migration.
- **Prefetch** is a low-priority queue with per-domain rate limits, ordered by focus time.
- **Tutorial translator** input is a URL or text you give Waddle (a page or a transcript), fetched under the same rules. On-screen video is a masked live region and audio is never captured, so Waddle does not "watch" a tutorial (D-038).

### 5.6 Safety in the brain

- **Untrusted input** is everything not typed by you or shipped in the first-party tree: screen and OCR text, window captions, AT-SPI text, adapter-supplied names, command lines, journal and crash-log text, man/tldr output, web pages, search results, docs, file metadata, imported guides and packs, notifications. It is placed only in labelled data blocks with length caps and control/bidi characters stripped — never in the system or instruction position.
- **The tool registry is static**; tool results are untrusted too. Combined with I8, an injected instruction has no tool that sends data somewhere of its choosing.
- **Output sinks.** Answers render as plain rich text with a tag allowlist and no remote resources; links show their real host and are never opened automatically. Command cards are a typed schema; the destructive/sudo classifier is deterministic host code that defaults to "risky" when it cannot parse a command; the Copy payload is byte-identical to what is displayed, with no hidden or bidi characters.
- **Guide import** uses a strict schema with size and step caps and re-runs the command classifier.
- **Logs** are an allowlist of fields: message type, timings, sizes, enum values, app id. Screen text, transcripts, prompts, window captions, file paths, URLs, search queries, tool arguments, and command lines are never logged unless the debug flag is on. A CI test injects canary strings through each of those channels and greps the logs.

---

## 6. Voice sidecar

`waddle-voice` holds faster-whisper (`float16` — INT8 is disabled on Blackwell) and the TTS engines.

**STT latency.** Audio is streamed and buffered in the sidecar while the key is held. The default strategy is one decode at release, with VAD trimming silence; faster-whisper has no incremental API, so "decode only the tail" would need a commit-at-pauses scheme, adopted only if spike S6.1 shows the simple approach misses 700 ms on realistic utterance lengths.

**TTS.** Piper is the default engine and Kokoro (ONNX) the higher-quality option; both stream per sentence. Both pull in GPL code (Piper itself, and espeak-ng through both engines' phonemizers), and Piper's maintenance is uncertain, so TTS sits behind an interface in its own process and is never imported by the desktop or the API (D-016). Voices are downloaded on first run, not committed; the default is a CC0/public-domain voice, because the commonly used `lessac` voice derives from a research-only corpus.

---

## 7. Data and configuration

| Path | Contents |
|---|---|
| `~/.config/waddle/config.toml` | all settings, documented defaults |
| `~/.config/waddle/token` | link token, `0600` |
| `~/.local/share/waddle/desktop.db` | identity cache, inventory, focus time, positions |
| `~/.local/share/waddle/guides/` | guide library as YAML (works offline, shareable) |
| `~/.local/share/waddle/brain/` | brain SQLite (docs, history, profile), `0700` |
| `~/.cache/waddle/` | tldr dataset, other caches |
| `$XDG_RUNTIME_DIR/waddle/` | sockets, one-shot KWin scripts (tmpfs, `0700`) |

**What is stored about you:** the skill profile, completed-guide records, focus time, and conversation history — your questions (typed, or transcribed from push-to-talk) and Waddle's answers, as text, with configurable retention. History can be set to in-memory only. Nothing is recorded for blocked windows. Frames, audio, keystrokes, and raw screen text are never stored (D-039).

**Forgetting.** `waddle forget` removes everything derived from you: history, profile, focus time, saved positions, guide records, the identity cache, the doc cache, and the screencast permission. It offers to export your guide library first and removes it too unless you pass `--keep-guides`. `--app <id>` (and per-app reset in Settings) limits it to one app. If the brain is stopped, the host deletes the brain's data files directly.

---

## 8. Degradation

A single `Capabilities` object (capture path, AT-SPI, mic, brain, internet, models loaded, VRAM profile, remote provider active) is recomputed on events. The penguin's state, the tray icon, the answer templates ("I can't see your screen right now, so…"), and `waddle doctor` all read from it, so the UI cannot claim an ability the system does not have.

| Condition | Penguin | Still works |
|---|---|---|
| Brain unreachable | `napping-brain` | telemetry, alerts, identity (cached, or without OCR), saved guides with deterministic checks |
| No internet | normal + "offline" chip | cached docs, labeled; fetches queued |
| Low VRAM | normal + note once | `fast_vlm` or text-only |
| Capture denied | `confused` on visual asks | text/voice Q&A; adapter + AT-SPI guides |
| No mic | — | text |
| AT-SPI off | — | OCR/vision |
| Remote provider active | badge | everything; indicator stays on |

---

## 9. Performance design

| Budget | Mechanism | Measured |
|---|---|---|
| Idle CPU ~0 % | pushed sensors; animations stopped when not visible; cursor script unloaded when idle | Phase 1, 60 s average |
| RAM < 150 MB | no numpy/pixels/models in the host; lazy windows | S1.1, then every phase (PSS, all imports loaded) |
| Visible < 2 s | penguin window first; D-Bus, DB, and link after the first frame | Phase 1, unit start → first presented frame |
| Switch → identified < 100 ms | KWin push + indexed cache hit; cold path resolves asynchronously | Phase 2 |
| Cached version < 50 ms | single SQLite read | Phase 3 |
| First words < 1.5 s (warm) | resident `fast_vlm`, pre-warmed `smart_model`, stable prompt prefix, retrieval in parallel with capture | Phase 3; again in Phase 4 with retrieval on |
| Change → verified < 1 s | adapter/AT-SPI checks: milliseconds on the event. Visual checks: measured from the first captured frame that shows the change | Phase 5, p50/p95 per tier |
| PTT → transcript < 700 ms | audio buffered in the sidecar during the hold; one decode at release | S6.1, by utterance length |

Every stage emits a timing span; `waddle bench` and the eval suite fail when a budget regresses.

---

## 10. Testing strategy

- **Unit** (`waddle-core`, resolvers, both gates, predicates, coordinates, codecs, pack schema, hotkey conflicts, blocklist, tool-argument validation, fetch policy, injection fixtures) — no Qt, no D-Bus, fast.
- **Integration** — `dbus-run-session` + `kwin_wayland --virtual` with our scripts loaded and test client windows; `python-dbusmock` for logind/notifications; a hand-written mock for the portals (none exists upstream).
- **Structural** — the read-only denylist, the adapter AST check, "no frames in Waddle's directories", "no TCP listeners", "compose has no published ports and the API has no egress", log canaries.
- **Evals** — labeled screenshots → version, state, grounding IoU, plan rubric, latency; re-run in CI whenever model config or prompt files change. Fixtures are captured only by an explicit `waddle eval capture` command, one frame at a time with a preview, blocklist enforced, saved where you choose. Nothing is uploaded or committed without your approval per file.

---

## 11. Install and uninstall

`install.sh` (idempotent, never uses root): build the uv environment and the capture helper; install both `.desktop` files; register the window rule; generate the token; write systemd **user** units; `docker compose pull`. It prints, and does not run, anything needing root (for example installing `nvidia-container-toolkit`). It does not install anything into other applications. A manifest of everything it created drives `uninstall.sh`, which also removes the rule, the shortcuts, and the portal permissions.

---

## 12. Where this differs from the vision

Each is recorded in DECISIONS.md.

**Prime-directive calls** (1 and 2 confirmed by you; 3 awaits your review)

1. **Waddle does not turn accessibility on.** Qt apps expose AT-SPI only when a session-wide flag is set. Flipping it changes a system setting that affects every app, so onboarding explains it and shows you the one command; you run it. (D-012)
2. **You install the in-app components** (the Blender extension; later Krita/GIMP plug-ins, obs-websocket). Installing them changes another application's configuration. (D-036)
3. **The KWin scripts move Waddle's own windows.** This is the only practical way to reposition on Wayland; restricted to Waddle's windows and tested. (D-004)

**Narrowed for privacy or safety**

4. **Region-select captures one window, not an arbitrary screen area.** An area grab would include other apps and bypass the blocklist. (D-033)
5. **No `spectacle` fallback.** It writes image files and runs outside the gate. (D-035)
6. **No click sensing and no global Esc.** The highlight clears on frame change, focus change, or timeout; Esc stops speech only when a Waddle window has focus. (D-037)
7. **Blender undo names and the Info log depend on your Blender version** (5.3+ and 5.2+ respectively); older versions get a partial operator list. (D-023)
8. **Tutorial translator takes a URL or text you supply**; it does not watch video. (D-038)
9. **"Cursor approaches" is on-touch** unless cursor tracking is already running. (D-004)

**Structural**

10. **STT, TTS, and OCR inference run in the brain.** Voice, and OCR-based version reading, need the brain. (D-016)
11. **The guide runner is on the host**; planner and visual checks stay in the brain. (D-021)
12. **KWin scripts are loaded at runtime**, not installed into `kwinrc`. (D-007)
13. **The capture helper also does change-gating and encoding**, so it is more than "tiny". (D-005)
14. **The link is a WebSocket over a Unix socket, and nothing publishes a TCP port** — stricter than "bound to 127.0.0.1". (D-034, D-019)
15. **History stores your questions and Waddle's answers as text**, which the History panel requires; raw transcripts of screen content are not stored. (D-039)
