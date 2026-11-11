# Research baseline (2026-11-11)

Evidence behind [ARCHITECTURE.md](ARCHITECTURE.md) and [DECISIONS.md](DECISIONS.md), gathered before any code was written (VISION §2.1).

**Confidence tags.** `SRC` read in upstream source at the named branch · `PKG` package index (packages.ubuntu.com, PyPI) · `DOC` official docs/release notes · `SNIP` search snippet or third-party page only · `UNVERIFIED` not confirmed; must be checked by the named spike before we depend on it.

Nothing here was tested on the target machine. Every `UNVERIFIED` and `SNIP` item is owned by a spike in [PLAN.md](PLAN.md), and `waddle doctor` re-checks the machine-specific ones at runtime.

---

## 1. Target platform: Ubuntu 26.04 "resolute"

| Component | Version | Tag |
|---|---|---|
| Plasma / KWin (`kwin-wayland`) | 6.6.4 | PKG |
| Qt | 6.10.2 | PKG |
| KDE Frameworks | 6.24.0 | PKG |
| Python 3 (system) | 3.14.3 | PKG |
| PipeWire | 1.6.2 | PKG |
| xdg-desktop-portal / -kde | 1.21.1 / 6.6.4 | PKG |
| at-spi2-core | 2.60.0 | PKG |
| NVIDIA driver (restricted) | 595 branch (`nvidia-driver-595-open` suggested) | SNIP |
| Archive extras | `python3-pyside6.*` 6.10.2, `python3-qasync` 0.28.0, `qml6-module-org-kde-layershell` 6.6.4, `python3-pyatspi` 2.58.1, `gir1.2-atspi-2.0` 2.60.0 | PKG |
| Not in archive | `kdotool` | PKG |

- Plasma X11 session packages exist but are not installed by default. Upstream Plasma 6.8 (2026-10-14) dropped the X11 session; 26.04 stays on 6.6. `SNIP`
- **Consequence:** we design against Plasma **6.6**, and keep an eye on 6.8 where two things change (ScreenShot2 authorization, §3).

## 2. Host UI toolkit

- **PySide6 6.12.0** (2026-10-08), Python ≥3.10,<3.16, LGPL-3.0-only OR GPL-2.0/3.0-only. `PKG`
- **Click-through:** in Qt 6.10 Wayland, `QWindow.setMask()` → `wl_surface.set_input_region`; `Qt.WindowTransparentForInput` sets an empty input region. The mask affects input only, not rendering. Known flakiness: the region is not re-sent after hide/show (QTBUG-62638), so re-apply on every show. `SRC qtbase 6.10 qwaylandwindow.cpp`
- **Tray:** `QSystemTrayIcon` registers an `org.kde.StatusNotifierItem-<pid>-<n>` with the StatusNotifierWatcher. `SRC qtbase 6.10 qdbustrayicon.cpp`
- **Layer-shell:** no Python binding for LayerShellQt. Its QML module (`org.kde.layershell`) only works when PySide6's Qt equals the system Qt, i.e. the apt PySide6 6.10.2, not pip wheels. End-to-end use from PySide6 is `UNVERIFIED`.
- **QtDBus from Python:** method export works; adaptor signals / custom interface names are reported unreliable. → use `dbus-fast` instead. `SNIP`
- **Async:** qasync 0.28.0 declares Python <3.14 upstream; `QtAsyncio` is still a technical preview with missing network I/O. → neither; use a thread boundary (D-003).

## 3. KWin (Plasma 6.6 branch)

### Scripting API — `SRC kwin Plasma/6.6 workspace_wrapper.h, window.h, scripting.cpp`
- `workspace`: `activeWindow`, signal `windowActivated(window)`, `cursorPos` + `cursorPosChanged()`, `windowAdded/windowRemoved`, `currentDesktopChanged`, `screens`, `activeScreen`, `stackingOrder`, `windowList()`.
- `window`: `caption`, `resourceClass`, `resourceName`, `pid`, `frameGeometry`, `clientGeometry`, `bufferGeometry`, `output`, `fullScreen`, `internalId` (UUID), `desktopFileName`, `popupWindow`, `normalWindow`, `layer`, `keepAbove`, `skipTaskbar`, `onAllDesktops`, `minimized`.
- Window signals: `frameGeometryChanged`, `fullScreenChanged`, `captionChanged`, `outputChanged`, `desktopsChanged`, `minimizedChanged`, `closed`.
- `callDBus(service, path, interface, method, args…)` pushes to us. A reply-callback argument is `UNVERIFIED`.
- **Scripts cannot export D-Bus methods** (only `run`/`stop`). There is no inbound channel into a running script.
- **No scriptable "is XWayland" property.** A class-name heuristic is `UNVERIFIED` (spike S2.2).
- Runtime loading: `org.kde.KWin` `/Scripting` `org.kde.kwin.Scripting.loadScript(path, pluginName) → id`, `isScriptLoaded`, `unloadScript`; then `/Scripting/Script<id>` `run()`.
- Script-free lookup: `org.kde.KWin /KWin getWindowInfo(uuid) → a{sv}` (resourceClass, caption, geometry, fullscreen, …). `SRC dbusinterface.cpp`

### ScreenShot2 — `SRC kwin Plasma/6.6 org.kde.KWin.ScreenShot2.xml, screenshotdbusinterface2.cpp, utils/serviceutils.h`
- Interface version 5 on 6.6. Methods: `CaptureActiveWindow(a{sv}, h)`, `CaptureWindow(s handle, a{sv}, h)`, `CaptureArea(i,i,u,u, a{sv}, h)`, `CaptureScreen`, `CaptureActiveScreen`, `CaptureWorkspace`, `CaptureInteractive`.
- Options: `include-cursor`, `include-decoration`, `include-shadow` (default **true**), `native-resolution`, `hide-caller-windows` (v5).
- Result: raw pixels written to a caller-supplied pipe; reply carries `width`, `height`, `stride`, `format`, `scale`, `windowId`.
- **Authorization on 6.4–6.7 (our target):** KWin takes the caller's PID from the bus, reads the `/proc/<pid>/exe` symlink target, and looks for an installed application whose `Exec=` first token canonicalizes to that path. **Only the first match is consulted**, and it must list the interface in `X-KDE-DBUS-Restricted-Interfaces`. `~/.local/share/applications` counts.
  - A Python app's exe is `/usr/bin/python3.14`, so authorizing it would authorize **every** Python process, and another `.desktop` with the same interpreter could shadow ours. → **a dedicated helper binary is mandatory** (D-005).
- **On Plasma 6.8+ the check is gone entirely**: any session process may call ScreenShot2. `SRC diff 6.7↔6.8` — the helper stays for a uniform design, and `doctor` reports which regime is active.
- `KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1` disables the check; `doctor` warns if it is set.

### Window rules — `SRC kwin 6.6 rulesettings.kcfg, rules.h`
- `kwinrulesrc`: `[General] count`, `rules` (ordered group names); one group per rule.
- Keys with `<key>rule` policy: `above`, `skiptaskbar`, `skippager`, `skipswitcher`, `noborder`, `position`, `desktops`, `acceptfocus`, `layer`. Policies: 2 Force, 3 Apply, 4 Remember. Match: 1 Exact, 2 Substring, 3 RegExp.
- There is **no `onalldesktops` key**; the `desktops` value meaning "all" is `UNVERIFIED` (spike S1.2).
- Reload: `org.kde.KWin /KWin reconfigure`. `kwriteconfig6` syntax from memory, `UNVERIFIED`.

### kdotool
- 0.2.3 (2025-04-03) latest dated release; works on Plasma 6 Wayland by generating KWin scripts; not packaged in Ubuntu. `SRC`

## 4. Portals and session D-Bus

- **ScreenCast** v5 (xdp 1.21.1). `SelectSources` `types=2` restricts to windows; `persist_mode=2` + single-use `restore_token` (a new one comes back on every `Start`). KDE re-matches a restored window by app id + **title similarity**, so restores can fail after title changes. `SRC`
- **GlobalShortcuts** v2. KDE emits `Deactivated` from kglobalaccel's `globalShortcutReleased`, so hold-to-talk works by design (not runtime-tested). `SRC xdg-desktop-portal-kde 6.6`
- **Host app identity:** `org.freedesktop.host.portal.Registry.Register(app_id)` once before any portal call; `app_id` must match a `.desktop` basename. `SRC`
- **KGlobalAccel** (`org.kde.kglobalaccel /kglobalaccel`): `globalShortcutsByKey`, `globalShortcutAvailable` (read-only conflict queries), `doRegister`/`setShortcutKeys` (fallback registration), component signals `globalShortcutPressed/Released`. `SRC`
- **Notifications of other apps:** `Notify` is a method call, not a signal; observing it needs a bus-monitoring connection. Mechanism on Plasma 6.6 `UNVERIFIED` (spike S8.1).
- **DND:** `org.freedesktop.Notifications` property `Inhibited` with `PropertiesChanged` (KDE extension). `SRC`
- **Theme:** `kdeglobals` `[General] AccentColor`, `[KDE] AnimationDurationFactor` (0 = animations off). Change signal `org.kde.kconfig.notify.ConfigChanged`; exact path/payload `UNVERIFIED`. `SRC`
- **Lock:** `org.freedesktop.ScreenSaver.ActiveChanged`, logind `LockedHint` — standard, but not re-checked: `UNVERIFIED` (spike S2.5).

## 5. Accessibility (AT-SPI2)

- Qt's bridge activates only if `QT_LINUX_ACCESSIBILITY_ALWAYS_ON` is set or `org.a11y.Status` `IsEnabled`/`ScreenReaderEnabled` is true; Qt watches the property, so flipping it activates running apps. `SRC qtbase 6.10`
- Plasma's default is very likely **off**. `UNVERIFIED` → **enabling it is a session-wide setting change, so Waddle will not do it itself** (D-012).
- On Wayland, screen coordinates are unavailable; **window-relative** extents are accurate, relative to client geometry (server-side decorations) or frame geometry (CSD). `SNIP accerciser MRs`
- XWayland apps may be invisible to AT-SPI (KDE bug 493054). `SNIP`

## 6. GPU baseline: RTX 5090 (Blackwell, sm_120)

| Library | Version | Blackwell status | Tag |
|---|---|---|---|
| Driver / CUDA | ≥570 / ≥12.8 | first toolkit with sm_120 | SNIP |
| Ollama | 0.40.16 (2026-11-09) | RTX 50xx listed, 5090 named | DOC |
| vLLM | 0.33.0 (2026-11-02) | CUDA 13.0-only wheels/images, torch 2.14; no explicit 50-series statement | DOC, inferred |
| CTranslate2 | 4.9.0 (2026-11-10) | CUDA 12.8 support since 4.6.3; **INT8 disabled on sm_120** since 4.6.2 | DOC |
| faster-whisper | 1.2.1 (2025-10-31) | works with `compute_type="float16"`; `int8*` fails on 50-series | DOC + SNIP |
| onnxruntime-gpu | 1.31.1 (2026-10-26) | CUDA 13.0 + cuDNN 9 wheels; no explicit sm_120 statement | SNIP |
| PyTorch | 2.15.0 (2026-10-28) | cu130+ wheels; cu126 is for old GPUs | DOC, inferred — **we avoid PyTorch entirely** |
| nvidia-ml-py | 13.615.71 (2026-09-25) | n/a | PKG |
| NVIDIA Container Toolkit | 1.20.1 | compose `deploy.resources.reservations.devices` still current | DOC + SNIP |

- CTranslate2 wheels need the **CUDA 12** cuBLAS + cuDNN 9 userland; onnxruntime-gpu and vLLM are on **CUDA 13**. These cannot share one Python environment cleanly → separate containers (D-017).
- NVML is not PID-namespace aware: per-process VRAM must be read **on the host**, not in a container. `SNIP`
- "Inferred" rows are confirmed by the Phase 3 GPU smoke test (spike S3.1) before anything is pinned.

## 7. Model servers and candidate models

**Ollama API** `DOC`: `/api/chat` streams; `format` accepts a JSON schema; `images` is a base64 list; `keep_alive: 0` unloads; `/api/ps` gives `size_vram` per model; `/api/embed`. Defaults: `OLLAMA_NUM_PARALLEL=1`, context 4096, `OLLAMA_MAX_LOADED_MODELS=3×GPUs`. Prefix (KV) cache reuse is undocumented → measure `prompt_eval_duration` (spike S3.2).

**vLLM** `DOC`: `guided_*` was **removed in 0.12**; current API is `response_format={"type":"json_schema"}` or `structured_outputs`. One model per server process.

**Candidates for the bake-off** — all sizes/tags `SNIP`, re-verified at pull time; nothing is hardcoded (D-018):

| Role | Candidates | ~VRAM @ Q4 | License |
|---|---|---|---|
| `fast_vlm` | Qwen3.5 9B / 4B, Gemma 4 E4B, UI-Venus-2-9B (vLLM only) | 3.4–6.6 GB | Apache-2.0 (UI-Venus unverified) |
| `smart_model` | Qwen3.8 27B, Qwen3.5 27B, Gemma 4 31B | 18–20 GB | Apache-2.0 |
| `embedder` | Qwen3-Embedding 0.6B, bge-m3, EmbeddingGemma | 0.6–1.2 GB | Apache-2.0 / MIT / Gemma terms |

- Bounding-box conventions differ by family: Qwen3-VL-style `bbox_2d [x1,y1,x2,y2]` normalized 0–1000; Gemma `box_2d [y1,x1,y2,x2]` normalized 0–1000; Qwen2.5-VL absolute pixels of the resized image. → a per-model grounding codec is required.
- Mistral Small 4 is a 119B MoE and does not fit.

**OCR:** RapidOCR 3.10.0 (Apache-2.0, PP-OCRv5 models, ONNX Runtime backend) is the default candidate; PaddleOCR 3.7.0 needs Paddle's own GPU index; Surya's weights are revenue-restricted; Tesseract 5.5.4 is the CPU fallback. No small-UI-text benchmark exists → part of the eval set.

## 8. Brain libraries

| Library | Version | License | Notes |
|---|---|---|---|
| FastAPI | 0.162.4 | MIT | |
| pydantic | 2.14.3 | MIT | |
| uvicorn | 0.54.0 | BSD-3 | |
| websockets | 17.4 | BSD-3 | Python ≥3.11 |
| httpx | 0.28.1 (2024-12) | BSD-3 | slow-moving; acceptable |
| trafilatura | 2.3.1 (2026-10-06) | Apache-2.0 | `no_fallback` deprecated → `fast` |
| sqlite-vec | 0.1.9 (2026-03-31) | MIT/Apache-2.0 | pre-1.0, single maintainer, no upstream commits since 2026-05-17; brute-force KNN only (ANN exists only in undocumented alphas); bit/int8 columns + manual rescoring supported → behind an interface (D-020). Alternative: `sqliteai-vector` 1.2.0, Apache-2.0 |
| SearXNG | rolling | AGPL-3.0 | JSON needs `search.formats: [html, json]`; limiter off ⇒ no Valkey needed |
| tldr-pages | release zip `tldr-pages.en.zip` | CC-BY 4.0 | |

## 9. Voice

- **Piper:** `rhasspy/piper` redirects to **OHF-Voice/piper1-gpl**; pip `piper-tts` 1.8.0 (2026-09-04), **GPL-3.0-or-later**, project is "looking for maintainers". Per-sentence streaming via `voice.synthesize()`. Voice licences vary per model card.
- **Kokoro:** Kokoro-82M v1.0 weights Apache-2.0. `kokoro` (PyTorch) 0.9.4 is stale and capped at Python <3.13; **`kokoro-onnx` 0.6.1** (2026-08-19) runs on ONNX Runtime and needs no GPU.
- **faster-whisper:** built-in Silero VAD; model names `turbo`, `distil-large-v3.5`. The <700 ms budget on a 5090 has no published benchmark → measured in Phase 6.

## 10. Applications and adapters

- **Blender:** 5.3.0 released 2026-11-10 (`SRC` tag + PyPI `bpy`); 5.2 is the current LTS (`SNIP`); 4.5 LTS still maintained. Bundled Python: 3.11 in 4.2–5.0, 3.13 in 5.1+. Manifest `schema_version = "1.0.0"`; `[permissions] network = "reason"`; install via `blender --command extension install-file`. `SRC blender 5.3`
  - Read-only APIs: `bpy.app.timers`, `bpy.msgbus.subscribe_rna`, `bpy.app.handlers` (`depsgraph_update_post`, `undo_post`, `render_init/complete/cancel`, `animation_playback_pre/post`, …), `window_manager.operators`, `preferences.keymap.active_keyconfig`.
  - **History APIs are version-dependent** (`SRC`, second-pass verification): `wm.undo_stack` (read-only step names) exists from **5.3**; `wm.reports` (the Info log, read-only) from **5.2**; on 4.2–5.1 only `wm.operators` exists — at most 32 entries, cleared on every undo/redo. `info.report_copy` writes the clipboard, `info.select_all` changes selection, `ed.undo_history(item=N)` performs an undo → all forbidden (D-023). No handler fires per operator.
  - Whether `bpy.app.online_access` is meant to cover loopback/Unix sockets is `UNVERIFIED` (spike S5.1).
  - `.blend` header: legacy 12-byte `BLENDER[-_][vV]NNN`; 5.0+ 17-byte `BLENDER17-01vNNNN`; files may be zstd- (or, pre-3.0, gzip-) compressed.
- **DaVinci Resolve:** 21.1.2 packaged, 21.2 reported (`SNIP`). External scripting is Studio-only and must be enabled by the user; 21.1 moved Python scripting out of Free. Resolve bundles its own Qt with xcb-only platform plugins, so it is an **XWayland client**; no accessibility tree is documented. No on-disk version file is documented → version comes from the installer/package name or the About dialog. → plan for **vision only on Free** (D-024).
- **Kdenlive** 26.08: Qt widgets (AT-SPI), minimal D-Bus. **Krita** 5.3.4 / 6.0.4: PyKrita. **GIMP** 3.2.x: GObject-Introspection plug-ins. **OBS** 33: obs-websocket v5 on port 4455; `simpleobsws` (asyncio) preferred over GPL-3.0-only `obsws-python`.

## 10a. Licences (second-pass verification)

| Dependency | Licence | How we use it | Constrains our licence? |
|---|---|---|---|
| PySide6 | LGPL-3.0 / GPL | in-process | no (LGPL) |
| PyGObject, at-spi2 | LGPL | in-process | no |
| piper-tts (embeds espeak-ng) | GPL-3.0-or-later | **separate process** | only that process |
| kokoro-onnx (MIT) → `phonemizer` (GPLv3+), espeak-ng (GPL-3.0) | mixed | **separate process** | only that process |
| obsws-python / simpleobsws | GPL-3.0-only / MIT | we use simpleobsws | no |
| faster-whisper, CTranslate2, onnxruntime, RapidOCR, sqlite-vec, trafilatura, psutil, nvidia-ml-py, FastAPI stack | MIT / BSD / Apache-2.0 | in-process | no |
| Ollama (MIT), SearXNG (AGPL-3.0, unmodified) | — | separate servers over HTTP | no |
| Blender extension | must be GPL-compatible | separate component over a socket | only the extension (`SNIP` of the Blender FAQ) |

- **The only in-process GPL pull is the TTS stack.** Keeping TTS in its own process makes both licence options viable (D-001).
- Piper voices carry their dataset's licence: `en_US-lessac-medium` derives from a research-only corpus; CC0/public-domain English voices exist (`joe`, `kristin`, `ljspeech`).

## 11. Tooling

uv 0.13.7 (workspaces), ruff 0.17.4, mypy 2.4.1, pytest 9.2.0, pre-commit 4.7.0, pytest-qt 4.5.0 (PySide6), python-dbusmock 0.38.1 (templates for logind and notifications; **no portal template** — we write our own). `kwin_wayland --virtual --width --height --socket --no-lockscreen --xwayland` confirmed in source and used headless by other projects.

## 12. File inspectors

`ffprobe -v error -of json -show_format -show_streams -protocol_whitelist file -- <file>`; `mediainfo --Output=JSON`; `exiftool -json -n -fast --`. Harden with `timeout` + `prlimit` + `bwrap --unshare-all --ro-bind` and capped output; avoid playlist-style demuxers that can reference other files. (CLI flags from memory → verified by inspector tests.)
