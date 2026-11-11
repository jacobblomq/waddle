# Waddle architecture

Status: **proposed — awaiting review before Phase 1.**
Source of truth: [VISION.md](VISION.md). Evidence: [RESEARCH.md](RESEARCH.md). Reasoning and deviations: [DECISIONS.md](DECISIONS.md) (`D-xxx`). Open spikes: [PLAN.md](PLAN.md) (`Sx.y`).

Target: Ubuntu 26.04, Plasma **6.6** Wayland, Qt 6.10, system Python 3.14, RTX 5090.

---

## 1. Invariants

These hold in every phase. Each is enforced by a test or a structural property, not by convention.

| # | Invariant | How it is enforced |
|---|---|---|
| I1 | **Read-only.** Waddle never injects input, changes another app's state, edits user files, or runs state-changing commands. | No input-injection dependency exists in the tree (CI denylist: `uinput`, `ydotool`, `xdotool`, `/dev/input`, `libei`, RemoteDesktop portal). Subprocesses go through one `readonly_exec` module with a fixed argv allowlist. The model has no shell tool. |
| I2 | **Own resources only.** The only writes are to Waddle's config/data/cache dirs, its own windows, its own models and containers, plus the explicitly sanctioned KWin rule and shortcut registrations. | Path guard on all file writes; Docker client implements `GET` only, except against Waddle's own compose project. |
| I3 | **Pixels are contained.** Raw frames exist only inside the capture helper; encoded frames exist only in memory. | Helper returns encoded bytes over an inherited socket; no temp files; a test greps the data dirs after an end-to-end run. |
| I4 | **Blind means blind.** On lock, on a blocked window, or when paused, nothing is captured and the rewind buffer is cleared. | The gate is evaluated in the desktop process **and again inside the capture helper** (§4.3). |
| I5 | **Content is data, never instructions.** Screen text, AT-SPI text, web pages, file metadata, and notifications cannot grant tools or permissions. | Fixed tool registry; untrusted content is wrapped in typed envelopes; injection fixtures run in CI. |
| I6 | **Local network surface only.** One published port, on `127.0.0.1`, token-authenticated. | Compose publishes only `waddle-api`; model and search services sit on an unpublished Docker network. |
| I7 | **The mic is open only while push-to-talk is held.** | The audio source object is created on `Activated` and destroyed on `Deactivated`; there is no other code path that opens it. |

Three places where a literal reading of the vision would break I1, and what we do instead, are called out in §12.

---

## 2. System overview

```
 ┌──────────────────────────── host (native, user session) ─────────────────────────────┐
 │                                                                                       │
 │  KWin 6.6 ──callDBus──▶ ┌─────────────────────── waddle-desktop ──────────────────┐  │
 │   ├ waddle-core.js      │  UI thread (Qt/QML)        │  I/O thread (asyncio)       │  │
 │   └ waddle-cursor.js    │   penguin · bubble · card  │   D-Bus (dbus-fast)         │  │
 │                         │   overlays · tray · panels │   sensors · gate · identity │  │
 │  ScreenShot2 ◀──D-Bus── │                            │   telemetry · adapters      │  │
 │        ▲                │   ◀── queued Qt signals ──▶│   guide runner · WS client  │  │
 │        │                └───────────┬────────────────┴───────────┬─────────────────┘  │
 │  ┌─────┴──────────┐   socketpair    │                            │ a11y thread        │
 │  │ waddle-capture │◀────────────────┘                            │ (AT-SPI, GLib)     │
 │  │ (Rust, tiny)   │  encoded frames only                         │                    │
 │  └────────────────┘                                              │                    │
 │   Blender ext ◀── unix socket (read-only state) ─────────────────┘                    │
 └──────────────────────────────────────┬────────────────────────────────────────────────┘
                                        │  ws://127.0.0.1:<port>  (token, JSON + binary)
 ┌──────────────────────────────────────┴──────── waddle-brain (Docker Compose) ─────────┐
 │  waddle-api (FastAPI)  ── orchestration · planner · retrieval · tools · OCR           │
 │       │            │             │                                                    │
 │   ollama / vLLM   searxng    waddle-voice (STT + TTS)        SQLite + sqlite-vec      │
 │   (no egress)    (egress)    (CUDA 12 userland)              (bind-mounted data dir)  │
 └────────────────────────────────────────────────────────────────────────────────────────┘
```

**What lives where, and why**

| Concern | Host | Brain | Reason |
|---|---|---|---|
| Rendering, overlays, hotkeys, mic capture, audio playback | ✔ | | needs the session |
| Window/cursor/lock/DND sensing, privacy gate | ✔ | | must work with the brain down |
| App identity + version waterfall steps 1–3, inventory | ✔ | | needs `/proc`, package DBs; <100 ms |
| System telemetry, alerts | ✔ | | NVML per-process data is host-only |
| Adapters, AT-SPI, file inspectors, man/tldr | ✔ | | need host processes and files |
| Guide **runner** (step state machine, deterministic checks) | ✔ | | <1 s verification; cached guides work offline (D-021) |
| Guide **planner**, visual verification, drift correction, Q&A | | ✔ | needs models |
| OCR, STT, TTS inference | | ✔ | keeps the host under 150 MB (D-016) |
| Search, fetch, extract, embed, retrieve | | ✔ | isolates all internet access in containers |
| Docs, guides, history, profile storage | mirror of guides | ✔ | |

The host is useful on its own: with the brain stopped it still shows the penguin, names apps and versions, raises system alerts, and replays saved guides whose checks are deterministic.

---

## 3. Repository layout

A `uv` workspace with one lockfile. Changes from the suggested layout are small and listed in D-002.

```
waddle/
  packages/
    core/            waddle-core: pydantic schemas, WS protocol, guide state machine,
                     predicates, version normalization, coordinate math. Pure Python, no I/O.
    desktop/         waddle-desktop: PySide6/QML app, sensors, identity, telemetry, CLI
      src/waddle_desktop/{ui,qml,sensors,identity,telemetry,guide,voice,link,cli}/
    brain/           waddle-brain: FastAPI app, model clients, retrieval, planner, tools, OCR
    voice/           waddle-voice: STT/TTS sidecar service
  capture-helper/    Rust crate: waddle-capture
  adapters/
    blender/         extension (blender_manifest.toml) + legacy add-on shim + host client
    generic/  resolve/  kdenlive/  krita/  gimp/  obs/
  inspectors/        read-only file inspectors + sandbox wrapper
  knowledge/         packs: blender/ resolve/ kdenlive/ kde-plasma/ ubuntu/ …
  kwin/              waddle-core.js, waddle-cursor.js, rule installer, .desktop files
  deploy/            docker-compose.yml, Dockerfiles, searxng/settings.yml, systemd user units
  evals/             fixtures, labels, eval + bake-off scripts
  docs/  tests/  install.sh  uninstall.sh
```

`waddle-core` is the contract between the halves: the brain and the desktop import the same schema and protocol definitions, so they cannot drift.

---

## 4. Host: `waddle-desktop`

### 4.1 Process and threading model

One Python process plus one native child.

- **UI thread** — Qt event loop. QML scene, tray, overlays. No blocking calls, no D-Bus, no sockets.
- **I/O thread** — one asyncio loop: `dbus-fast` (session, system, KWin callbacks), the WebSocket client, adapter sockets, subprocess inspectors, the telemetry sampler, the guide runner.
- **a11y thread** — AT-SPI via PyGObject with its own GLib loop. Isolated because accessibility calls into a hung app can block; every call has a timeout and the thread can be abandoned and restarted.
- **`waddle-capture`** — Rust child process, spawned on demand, exits after an idle period.

The UI and I/O threads talk only through queued Qt signals carrying immutable `waddle-core` messages (D-003). This replaces qasync, which does not declare support for the system's Python 3.14, and QtAsyncio, which is still a preview.

**Budgets.** Idle CPU ~0 %: nothing polls; the only periodic work is the 2–5 s telemetry sample and a sparse blink timer, and all animations stop when Waddle is hidden, occluded, or asleep. RAM < 150 MB is the tightest budget — a bare PySide6 + QML process is already most of it — so the host never imports numpy, never holds raw pixels, loads Settings/History/Eyes windows lazily, and runs no model code. Both budgets are measured in CI from Phase 1 (helper RSS is reported alongside the Python process).

### 4.2 Windows, placement, and the penguin

Wayland gives a client no say over position or stacking, so placement is a three-part arrangement (D-004):

1. **KWin window rule** (installed once, idempotent, reversible) matching Waddle's window class: keep above, skip taskbar/pager/switcher, no border, all desktops, do not accept focus, initial position. Written with `kwriteconfig6` into `kwinrulesrc`, then `org.kde.KWin.reconfigure`. The installer records exactly what it added so `uninstall.sh` removes only that.
2. **Waddle's own KWin script** moves *Waddle's own windows* at runtime (restore per-monitor position, follow the active monitor, step aside from a highlight target). It refuses to touch any window whose class is not Waddle's.
3. **User drag** uses `QWindow.startSystemMove()`; the script reports Waddle's new geometry so the position is remembered per output.

Windows:

| Window | Size | Input | Notes |
|---|---|---|---|
| Penguin + bubble/card | small, grows with bubble | mask = penguin ∪ bubble | mask re-applied on every show (QTBUG-62638) |
| Highlight overlay | fullscreen, one per output | `WindowTransparentForInput` | exists only while a highlight is visible |
| Region-select overlay | fullscreen, one per output | takes input while dragging | only after the hotkey |
| Settings, History, Eyes | normal windows | normal | lazy-loaded |

**Dodging.** Pointer entry into the penguin's input region (a normal hover event on our own surface) triggers fade + slide inside the window; no global pointer tracking is needed for this.

**The character** is drawn entirely with QML `Shape` paths: body, belly, face, beak, feet, flippers, scarf. State is one enum property; each state is a QML `State` with transitions, so adding a state never touches drawing code. The scarf color binds to the Plasma accent color; animation duration multiplies by `AnimationDurationFactor` (0 ⇒ reduced motion). Colors and fonts are read from `kdeglobals` and watched for changes, because pip-installed PySide6 ships its own Qt and cannot load the system Plasma theme plugin (D-006).

A layer-shell backend would be the more native way to do overlays, but it requires the distro's PySide6 build. It sits behind the same `Placement` interface as an optional backend to evaluate in spike S1.3; the KWin-rule path is the supported one.

### 4.3 Capture: `waddle-capture`

KWin 6.6 authorizes ScreenShot2 callers by executable path, first match wins. Authorizing the Python interpreter would grant screen capture to every Python script on the machine, so capture lives in a dedicated Rust binary at its own path with its own `.desktop` entry (D-005).

```
desktop ──socketpair(fd 3)──▶ waddle-capture ──D-Bus──▶ KWin ScreenShot2
        ◀── encoded frames ──                 ◀─ pipe ── raw pixels
```

- **No listening socket.** The helper is spawned by the desktop with an inherited socketpair and exits when it closes.
- **Narrow verbs:** `capture_active_window`, `capture_window(uuid)`, `capture_area` (only for user-drawn region-select). No screen or workspace capture is compiled in.
- **Options fixed in the helper:** `include-shadow=false`, `include-decoration=false`, `native-resolution=true`, `include-cursor=false`.
- **The gate is enforced twice.** The desktop decides whether to ask; the helper independently checks logind `LockedHint` and matches the target window's class/title (from KWin's own `getWindowInfo`) against the blocklist file before every capture. A bug in the Python gate cannot leak a blocked window.
- **All pixel work happens here:** live-region masking, change detection (downscaled diff + perceptual hash), downscaling the overview, cutting the full-resolution crop, encoding. The reply is `unchanged`, or `{overview, crop, scale, window geometry, hash}`. Raw frames never cross into Python (I3) and unchanged frames never reach a model.
- **Fallback chain**, reported by `doctor`: ScreenShot2 → portal ScreenCast (window source, `persist_mode=2`, restore token stored `0600`) → `spectacle -b -n -a`. The portal path is implemented in the helper too, so the host needs no GStreamer.

**Honest threat model.** Any process running as you could launch the helper itself; path-based authorization cannot prevent that, and on Plasma 6.8+ KWin performs no check at all. What the helper buys is: no accidental grant to every Python process, a capture surface limited to single windows, and a privacy gate that holds even if the desktop process is buggy. `PRIVACY.md` will say exactly this.

**Menus and popups** are separate surfaces and may be missing from an active-window capture. The helper captures the active window plus its own transient popups (by UUID, reported by the KWin script) and composites them at their offsets — never an area grab, which would include other apps. Whether `CaptureWindow` accepts popup surfaces is spike S2.1.

### 4.4 Sensors

Everything is pushed; nothing polls.

**KWin scripts**, loaded at runtime through `org.kde.kwin.Scripting.loadScript` and unloaded on exit (D-007). Nothing is written to `kwinrc`; if KWin restarts, a name-owner watch reloads them.

- `waddle-core.js` (always loaded): `windowActivated` → `{uuid, caption, resourceClass, resourceName, desktopFileName, pid, frameGeometry, clientGeometry, output, scale, fullScreen, xwayland}`; geometry/fullscreen/caption changes of the active window (coalesced); desktop changes; transient popups of the active window; Waddle's own window geometry. Delivered by `callDBus` to the desktop's service.
- `waddle-cursor.js` (loaded only while needed — `watching`, an active guide, a visible highlight): throttled `cursorPosChanged`, ≤10 Hz. Scripts cannot receive calls, so **loading and unloading this script is the on/off switch**; a one-shot script answers single "where is the cursor now?" requests.
- Fallback: `kdotool` if scripting is unavailable, then `getWindowInfo` polling at a low rate as a last resort; `doctor` says which is active.

**Session state** (D-Bus signals): screen lock (`org.freedesktop.ScreenSaver.ActiveChanged` and logind `LockedHint`), idle, DND (`Inhibited`), theme changes, active PipeWire screencasts (read-only), and — opt-in only — notifications.

**Hotkeys:** register the app id with the host portal registry, then bind through the GlobalShortcuts portal; `Activated`/`Deactivated` give true hold-to-talk. Before proposing defaults, the installer asks KGlobalAccel which keys are taken (`globalShortcutsByKey`, read-only) and offers alternatives. KGlobalAccel registration is the fallback.

**The privacy gate** is one small pure function: `(window, session state, config) → allow | blind(reason)`. Every capture, AT-SPI read, rewind-buffer write, and adapter read passes through it. On `blind`, the penguin closes his eyes, the rewind buffer is cleared, and the helper is told to drop its last frame.

**Rewind buffer:** ~30 s ring of encoded low-res keyframes (written only on meaningful change) plus adapter and window events, in memory in the desktop process.

### 4.5 Perception sources

Every observed fact is a `Fact{value, source, confidence, observed_at}` where `source ∈ adapter | atspi | ocr | vlm | kwin | system | docs`. The source travels all the way to the citation chip in the bubble.

1. **Adapter** (§4.7) — authoritative.
2. **AT-SPI** — find by role/name, read states, get window-relative extents; pruned to the focused subtree before anything is sent to a model. Unavailable ⇒ fall through silently.
3. **OCR** (brain) — exact strings.
4. **VLM** (brain) — layout, state, intent, grounding.

**Accessibility is not switched on by Waddle** (§12).

### 4.6 App identity and version

A pipeline of small pure resolvers over a `ProcessSnapshot` (so each is unit-testable from fixtures):

`window pid → real binary` (walk wrappers: Electron, shell launchers, AppImage runtime, Flatpak `bwrap`) → `packaging` (Flatpak `.flatpak-info`, Snap cgroup/env, AppImage, `dpkg -S`, `/opt` version files) → `allowlisted --version` (timeout, network-less sandbox) → `OCR of title/About` (brain) → `ask the user`.

Results: `{app_id, name, version (major.minor.patch), edition, channel, locale, keymap, packaging, confidence, source}`, cached in the host SQLite keyed by `(app_id, exe path, size+mtime | package version)`. Cached lookup is one indexed read. Web apps are identified from the browser window title against knowledge-pack patterns and are reported as "version not visible".

The installed-app inventory (`.desktop` files, Flatpak, Snap, dpkg) is built lazily and refreshed on file-change notifications for the relevant directories.

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

There is no method that takes a mutation. Adapters are discovered through Python entry points; the wire protocol to in-app components is request/response with a closed set of read verbs.

**Blender extension** (`blender_manifest.toml`, GPL-3.0-or-later, with a legacy `bl_info` shim):
- A timer on Blender's main thread builds a small immutable snapshot (mode, workspace, editor, engine, selection, active material/nodes, modifiers, frame, playback, shading, recent operators, keymap preset) only when something changed and a client is connected.
- A background thread serves that snapshot over a Unix socket in `$XDG_RUNTIME_DIR/waddle/` (mode `0600`). The thread never touches `bpy`.
- Render start/finish handlers feed GPU etiquette (§5.4).
- **Read-only by construction:** a CI test parses the extension's AST and fails on any `bpy.ops` call, any assignment to a `bpy` data attribute, or any file write.
- **History is tiered by Blender version**, always by pure property reads: 5.3+ reads `wm.undo_stack` (step names) and `wm.reports` (the Info log); 5.2 LTS reads `wm.reports` only; 4.2–5.1 has only `wm.operators`, a best-effort list of at most 32 recent operators that Blender clears on every undo. The adapter reports which tier it is on, so Waddle can say "I can't see your undo history on this Blender version" instead of guessing. `bpy.ops.info.*` and `bpy.ops.ed.undo_history` are never called: they write the clipboard, change selection, or perform an undo.
- The extension runs on both Python 3.11 (Blender 4.2–5.0) and 3.13 (5.1+), with no third-party dependencies.

Stubs with documented capabilities: Resolve (an XWayland client with no usable accessibility tree, so vision is the baseline; on Studio, if you have already enabled external scripting, an allowlist of getters such as `GetCurrentPage` and `IsRenderingInProgress`; nothing is ever installed into Resolve's script folders), Kdenlive (AT-SPI), Krita, GIMP 3, OBS (obs-websocket read requests only).

### 4.8 System awareness

One sampler coroutine, 2–5 s adaptive (slower when idle or locked): NVML (temperature, power, utilization, encoder/decoder, per-process VRAM), psutil, sensors. On demand: failed units and `journalctl -p err`, Docker health (GET-only client), disk growth, coredumps, crash logs.

A 10-minute rolling summary is kept as a few hundred bytes of aggregates. The alert engine is a table of `(predicate, threshold, cooldown)` rows evaluated on each sample. All of this is host-side, so alerts work with the brain down.

### 4.9 Voice I/O on the host

The host only moves audio. While PTT is held it streams 16 kHz PCM to the brain and draws the waveform; on release it sends `end`. TTS audio streams back and is played through Qt Multimedia, with the output level driving the beak. Any new request, Esc, or PTT cancels playback immediately.

### 4.10 The guide runner

The step state machine lives in `waddle-core` and runs on the host:

```
PLANNED → SHOWING → OBSERVING ──check passes──▶ CELEBRATE → next step
                       │  ▲
            drift ─────┘  └──── PAUSED (user asked a question) 
                       ▼
                  CORRECTING
```

Each step's `success_check` is tried in order of reliability: adapter predicate → AT-SPI state → visual check. The first two are evaluated locally in milliseconds on events; only the third costs a model call, and only when the helper reports a changed frame. Coach-mode hint escalation is a timer on `OBSERVING`. Multi-app guides key each step by app id and resume on focus change.

Predicates are a small declarative language (`state.engine == "CYCLES"`, `atspi(role=checkbox, name~"Proxy").checked`), never code, so guides from disk or from a model cannot execute anything (I5).

### 4.11 Coordinates

One module in `waddle-core`, one type per space, explicit conversions only:

```
ImagePx(x, y)            pixels in the captured buffer
  ÷ capture scale
WindowLogical(x, y)      relative to the captured window's client origin
  + clientGeometry.topLeft        (from KWin, logical, may be fractional)
GlobalLogical(x, y)      compositor space
  − output.geometry.topLeft
OverlayLocal(output, x, y)
```

Model outputs enter as `ImagePx` after the per-model grounding codec normalizes them (0–1000 `[x1,y1,x2,y2]`, Gemma's `[y1,x1,y2,x2]`, or absolute pixels). AT-SPI extents enter as `WindowLogical`, with a per-toolkit origin correction (client vs frame geometry) and a separate path for XWayland clients. Property tests cover 100/125/150/175/200 % scales, mixed-scale multi-monitor layouts, negative-origin outputs, and XWayland.

---

## 5. Brain: `waddle-brain`

### 5.1 Services

| Service | Image | GPU | Published | Egress |
|---|---|---|---|---|
| `waddle-api` | ours (Python 3.13, CUDA 13 runtime for OCR) | yes (small) | `127.0.0.1:<port>` | yes (doc fetch) |
| `ollama` (default) or one `vllm` per role | upstream | yes | **no** | **no** (internal network) |
| `searxng` | upstream, unmodified | no | **no** | yes |
| `waddle-voice` | ours (CUDA 12 userland for CTranslate2) | yes (small) | **no** | no |

Only `waddle-api` is reachable from the host. The model server sits on an internal Docker network with no route out, so a model server cannot phone home even if it wanted to; model pulls are an explicit `waddle models pull` step that attaches a network for the duration. SearXNG runs with the limiter off, so no Valkey container is needed.

The same services run natively from the `uv` workspace for people who do not want Docker (`waddle brain --native`).

### 5.2 Link protocol

One WebSocket, `Authorization: Bearer <token>` on the handshake (token generated at install, `0600`), versioned envelopes defined in `waddle-core`:

```
{ "v": 1, "id": "...", "type": "ask" | "answer.delta" | "frame" | "verify" | "ground"
                       | "guide.plan" | "stt.audio" | "tts.audio" | "tool.call" | "state" ... }
```

JSON for control, binary frames for images and audio. The **brain calls back into the host** for the read-only tools that need the host (`get_app_state`, `get_system_stats`, `inspect_file`, `man`, `tldr`); the host serves these from a fixed registry and rejects anything else.

### 5.3 Model layer

```python
class ModelProvider(Protocol):
    async def chat(self, role: Role, messages, *, schema: type[BaseModel] | None, images=()) -> Stream
    async def embed(self, texts: list[str]) -> list[Vector]
    async def load(self, role: Role) -> None
    async def unload(self, role: Role) -> None
    async def status(self) -> list[LoadedModel]
```

- Implementations: Ollama (`format` = JSON schema), vLLM (`response_format` json_schema — the old `guided_*` API is gone), OpenAI-compatible remote, Anthropic remote. Remote providers are off by default and carry a per-provider `allow_frames` flag that defaults to false.
- Roles (`fast_vlm`, `smart_model`, `embedder`) map to model names **only in config**. Each model entry also names its grounding codec.
- Structured calls: pydantic schema → constrained decoding → validate → at most two repair retries → typed failure the UI can show honestly.
- Prompts are assembled as `stable system prefix | app+version block | retrieved docs | observation | question` so the prefix cache is reused.

### 5.4 GPU etiquette

A VRAM governor in `waddle-api` owns every load/unload decision. Inputs: host telemetry (free VRAM, per-process usage), adapter render events, "fullscreen game focused", Focus mode. Policy:

| Profile | Loaded | Approx. VRAM |
|---|---|---|
| `full` | fast + smart + embedder + STT + OCR | ~26–28 GB |
| `balanced` (default) | fast + embedder + STT + OCR resident; smart on demand, unloaded after idle | ~9 GB resident |
| `lean` | fast + embedder only | ~7 GB |
| `focus` | nothing | 0 |

On a render start or low free VRAM the governor steps down a profile, tells the user once, and steps back up after a quiet period with hysteresis. It only ever addresses Waddle's own Ollama/vLLM instances.

`full` leaves almost nothing for Blender or Resolve, which is why it is not the default. The exact numbers come from the bake-off.

### 5.5 Knowledge engine

```
question + AppIdentity + UI state
   → hybrid retrieve (FTS5 BM25 + vector KNN, filtered by app + version)
   → enough? ──no──▶ SearXNG (query pinned to version, pack's trusted domains first)
                      → fetch (size/time/content-type limits, robots.txt, no JS)
                      → trafilatura → chunk → embed → store with {app, version, url, trust, fetched_at}
   → answer from retrieved chunks + observed facts only, with citations
```

- **Knowledge packs** (YAML, schema-validated): doc domains, versioned URL templates, release-notes sources, trust tiers, locale variants, platform notes, live-region hints for change-gating, version-detection hints (About-dialog patterns, `--version` allowlist entry).
- **Nearest-version fallback** is a first-class result: the retriever returns `{chunks, version_used, exact: false}` and the answer template must surface it.
- **Storage:** one SQLite file: a chunks table, an FTS5 index, embeddings as plain float32 BLOBs (the canonical copy), and a sqlite-vec `vec0` table as a **derived, rebuildable** index partitioned by app. BM25 and vector results are fused in Python above a small `VectorStore` interface (D-020). sqlite-vec is pre-1.0, brute-force, and has a single maintainer who has been quiet since May 2026; because the canonical vectors are ordinary BLOBs, swapping in a numpy or `sqliteai-vector` backend is a rebuild, not a migration.
- **Prefetch** is a low-priority queue with per-domain rate limits, ordered by focus time.

### 5.6 Safety in the brain

- Untrusted text is always placed in a delimited data block with its source label, never in the system or instruction position.
- The tool registry is static and read-only; tool results are also treated as untrusted.
- Fetched HTML is reduced to text/Markdown by trafilatura and rendered by the host as plain rich text with an allowlist of tags; no remote resources are loaded.
- Logs are structured and redacted: message types, timings, sizes, and hashes — never screen text, transcripts, or prompts unless the debug flag is on.

---

## 6. Voice sidecar

`waddle-voice` holds faster-whisper (`float16` — INT8 is disabled on Blackwell) and the TTS engines. Audio arrives while the key is held, so on release only the tail needs decoding; VAD trims silence. Piper is the default voice engine and Kokoro (ONNX) the higher-quality option; both stream per sentence. Both pull in GPL code (Piper itself, and espeak-ng through both engines' phonemizers), and Piper's maintenance is uncertain, so TTS sits behind an interface in its own process and is never imported by the desktop or the API (D-016). Voices are downloaded on first run, not committed; the default is a CC0/public-domain voice, because the commonly used `lessac` voice derives from a research-only corpus.

---

## 7. Data and configuration

| Path | Contents |
|---|---|
| `~/.config/waddle/config.toml` | all settings, documented defaults |
| `~/.config/waddle/token` | link token, `0600` |
| `~/.local/share/waddle/desktop.db` | identity cache, inventory, focus time, positions |
| `~/.local/share/waddle/guides/` | guide library as YAML (works offline, shareable) |
| `~/.local/share/waddle/brain/` | brain SQLite (docs, history, profile) — bind-mounted |
| `~/.cache/waddle/` | tldr dataset, model-independent caches |

`waddle forget` deletes history, profile, and focus time; `--all` also removes docs and guides. Nothing here ever contains frames, audio, or keystrokes.

---

## 8. Degradation

A single `Capabilities` object (capture path, AT-SPI, mic, brain, internet, models loaded, VRAM profile) is recomputed on events. The penguin's state, the tray icon, the answer templates ("I can't see your screen right now, so…"), and `waddle doctor` all read from it, so the UI cannot claim an ability the system does not have.

| Condition | Penguin | Still works |
|---|---|---|
| Brain unreachable | `napping-brain` | telemetry, alerts, identity (steps 1–3), saved guides with deterministic checks |
| No internet | normal + "offline" chip | cached docs, labeled |
| Low VRAM | normal + note once | `fast_vlm` or text-only |
| Capture denied | `confused` on visual asks | text/voice Q&A; adapter + AT-SPI guides |
| No mic | — | text |
| AT-SPI off | — | OCR/vision |

---

## 9. Performance design

| Budget | Mechanism |
|---|---|
| Idle CPU ~0 % | push-only sensors; animations stopped when not visible; cursor script unloaded when idle |
| RAM < 150 MB | no numpy/pixels/models in the host; lazy windows; measured in CI |
| Visible < 2 s | penguin window first; everything else (D-Bus, DB, link) after first frame |
| Switch → identified < 100 ms | KWin push + indexed cache hit; cold path resolves asynchronously and updates |
| Cached version < 50 ms | single SQLite read |
| First words < 1.5 s | warm `fast_vlm`/`smart_model`, stable prompt prefix, retrieval in parallel with capture |
| Change → verified < 1 s | local predicates first; helper-side change gating; `fast_vlm` with schema output |
| PTT → transcript < 700 ms | audio streamed during hold; decode tail only |

Every stage emits a timing span; `waddle bench` and the eval suite fail when a budget regresses.

---

## 10. Testing strategy

- **Unit** (`waddle-core`, resolvers, gate, predicates, coordinates, codecs, pack schema, hotkey conflicts, blocklist, injection fixtures) — no Qt, no D-Bus, fast.
- **Integration** — `dbus-run-session` + `kwin_wayland --virtual` with our scripts loaded and test client windows; `python-dbusmock` for logind/notifications; a hand-written mock for the portals (none exists upstream).
- **Structural** — the read-only denylist, the adapter AST check, "no frames on disk", "no listening sockets except the one", "compose publishes one port".
- **Evals** — labeled screenshots → version, state, grounding IoU, plan rubric, latency. The model bake-off writes its recommendation into DECISIONS.md.

---

## 11. Install and uninstall

`install.sh` (idempotent): build the uv environment and the capture helper; install both `.desktop` files; register the window rule; generate the token; write systemd **user** units; `docker compose pull`. It prints, and does not perform, anything needing root (for example a missing `nvidia-container-toolkit`). A manifest of everything it created drives `uninstall.sh`, which also removes the rule, the shortcuts, and the portal permissions.

---

## 12. Where this differs from the vision

Each is recorded in DECISIONS.md. The first three are prime-directive calls and need your nod.

1. **Waddle does not turn accessibility on.** Qt apps expose AT-SPI only when the session-wide `org.a11y.Status.IsEnabled` flag (or an environment variable) is set. Flipping it changes a system setting and affects every app, so onboarding explains it and shows you the one command with a Copy button; you run it. Until then AT-SPI is simply reported as unavailable. (D-012)
2. **Blender undo names and the Info log depend on your Blender version.** Read-only APIs for them exist only from 5.2 (Info log) and 5.3 (undo names). On older versions the only workaround writes the clipboard and changes selection, so Waddle does not use it and falls back to a partial recent-operators list. (D-023)
3. **The KWin script moves Waddle's own windows.** This is the only practical way to reposition on Wayland. It is restricted to Waddle's window class and covered by a test. (D-004)
4. **STT, TTS, and OCR inference run in the brain**, not on the host; the host only captures and plays audio. Voice therefore needs the brain. (D-016)
5. **The guide runner is on the host**; the planner and visual checks stay in the brain. (D-021)
6. **KWin scripts are loaded at runtime**, not installed into `kwinrc`. (D-007)
7. **Change-gating and encoding live in the capture helper**, making it somewhat more than "tiny" in exchange for keeping raw pixels out of Python. (D-005)
8. **Model and search containers are not published at all** (stricter than "bound to 127.0.0.1"). (D-019)
