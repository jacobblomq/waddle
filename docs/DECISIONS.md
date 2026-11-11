# Decisions

One entry per decision: what was chosen, why, and what would change it. Entries marked **⚠ deviates from VISION** differ from the brief; entries marked **❓ needs you** are waiting on your answer. Evidence lives in [RESEARCH.md](RESEARCH.md).

Status key: `proposed` (not yet built) · `accepted` · `superseded by D-xxx`.

---

## Needs you

### D-001 Licence ❓
- **Options.** (A) Whole repository GPL-3.0-or-later. (B) Permissive core (Apache-2.0) with the Blender extension and the TTS service as separate GPL-3.0-or-later components.
- **What research found.** The Blender extension must be GPL-compatible either way. The only other GPL pull is the TTS stack (Piper, and espeak-ng through both Piper's and Kokoro's phonemizers). The architecture already runs TTS and the Blender extension as separate processes behind sockets, so **both options are viable** and neither forces a design change.
- **Recommendation: A.** One licence, no boundary to police, every dependency is compatible, and it matches the ecosystem Waddle lives in (KDE, Blender). Choose B only if you want others to be able to embed the core in non-GPL software.
- **Status:** waiting for your answer. No licence file is committed yet.

### D-012 Waddle does not enable accessibility itself ❓ ⚠ deviates from VISION
- **Context.** Qt apps publish their AT-SPI tree only when the session-wide `org.a11y.Status.IsEnabled` flag (or `QT_LINUX_ACCESSIBILITY_ALWAYS_ON`) is set. On Plasma it is very likely off by default.
- **Decision.** Setting that flag changes a system setting that affects every app, so under the prime directive Waddle will not do it. Onboarding explains what it does and what it costs, and shows the one command with a Copy button; you run it. Until then AT-SPI is reported as unavailable and Waddle uses OCR/vision.
- **Alternative if you prefer:** you explicitly authorize Waddle to set this one flag at onboarding (and unset it on uninstall). Say so and I will record it as a sanctioned exception alongside the KWin rule.
- **Status:** proposed; default is "you run it".

---

## Structure

### D-002 Repo layout: a uv workspace under `packages/`
- `packages/{core,desktop,brain,voice}` instead of top-level `desktop/` and `brain/`; everything else as the brief suggests.
- **Why.** A shared `waddle-core` package (schemas, protocol, guide state machine, coordinates) is the contract between the halves; a workspace gives one lockfile and lets the brain image install without Qt.
- **Status:** proposed.

### D-003 Thread boundary instead of qasync
- UI thread runs Qt only; one asyncio thread runs all I/O; they exchange immutable messages through queued Qt signals.
- **Why.** The system Python is 3.14; qasync upstream declares <3.14, and `QtAsyncio` is still a technical preview with missing network I/O. The brief allows either approach.
- **Status:** proposed.

### D-008 Host Python = system Python 3.14; brain images = Python 3.13
- **Why.** The host needs the distro's `gi` bindings for AT-SPI, which are built for the system interpreter. PySide6 6.12 supports 3.14. Brain images pin 3.13 for the widest wheel coverage (CTranslate2, onnxruntime-gpu).
- **Would change if:** spike S4.2 shows a clean way to build PyGObject inside the uv environment.
- **Status:** proposed.

### D-009 `dbus-fast` for all D-Bus on the host
- **Why.** PySide6's QtDBus can export methods, but adaptor signals and custom interface names are reported unreliable. An asyncio D-Bus library fits the I/O thread.
- **Status:** proposed.

### D-010 Application id `io.github.jacobblomq.Waddle`
- Used for the `.desktop` basename, window class, D-Bus name, and portal registration; the capture helper is `io.github.jacobblomq.Waddle.Capture`.
- **Why.** A reverse-DNS id is required by the portal host registry and must match a `.desktop` file. Tell me if you would rather use a domain you own.
- **Status:** proposed.

---

## Host

### D-004 Placement = KWin rule + Waddle's own KWin script ⚠ deviates from VISION (addition)
- The rule (as specified) sets the static properties. Runtime repositioning — restoring per-monitor position, following the active monitor, stepping aside from a highlight — is done by Waddle's KWin script moving **Waddle's own windows only**.
- **Why.** A Wayland client cannot position itself, and rewriting rules plus reconfiguring KWin for every move is slow and disruptive. Moving one's own window is within "its own resources"; the script checks the window class and a test asserts it never touches another window.
- **Status:** proposed.

### D-005 Dedicated Rust capture helper that also owns pixel processing ⚠ deviates from VISION (scope of "tiny")
- **Verified.** KWin 6.6 authorizes ScreenShot2 by `/proc/<pid>/exe` matched against `.desktop` `Exec=`, first match wins. Authorizing Python would authorize every Python process. On Plasma 6.8+ there is no check at all.
- **Decision.** `waddle-capture` (Rust, `zbus`) at its own path with its own `.desktop`. It is spawned with an inherited socketpair (no listening socket), exposes only single-window and user-drawn-area capture, re-checks lock state and the blocklist itself, and performs masking, change detection, downscaling, cropping, and encoding.
- **Why Rust.** No Qt dependency, a single static binary, memory safety for the one component that touches raw pixels.
- **Why more than "tiny".** Keeping raw frames out of Python serves the RAM budget and the "pixels are contained" invariant, and puts the privacy gate in the smallest auditable unit.
- **Limit, stated plainly.** Any process running as you can execute the helper; path-based authorization cannot prevent that. The helper narrows exposure; it is not a sandbox boundary.
- **Status:** proposed.

### D-006 Theme is read from `kdeglobals`, not from the Qt platform theme
- **Why.** uv-managed PySide6 wheels bundle their own Qt and cannot load the distro's Plasma integration plugin. Reading colors, accent, fonts, and `AnimationDurationFactor` directly (and watching for changes) is deterministic and keeps PySide6 pinned in the lockfile.
- **Trade-off.** Waddle's widgets are our own QML, styled to match Plasma, not Breeze controls.
- **Status:** proposed.

### D-007 KWin scripts are loaded at runtime, not installed ⚠ deviates from VISION
- Loaded through `org.kde.kwin.Scripting.loadScript` when Waddle starts, unloaded when it stops, reloaded if KWin restarts. Nothing is written to `kwinrc`.
- **Why.** No persistent change to your KWin configuration, nothing to clean up, and the scripts cannot run without Waddle. It also gives the cursor stream its on/off switch: scripts cannot receive D-Bus calls, so loading and unloading `waddle-cursor.js` is how the stream is started and stopped.
- **Status:** proposed.

### D-011 Layer-shell is an optional backend, not the default
- **Why.** It is the more native overlay mechanism, but its QML module needs the distro's PySide6 (system Qt), which conflicts with D-006/D-008's pinned wheels, and it is untested from PySide6. Spike S1.3 decides whether it earns a place behind the `Placement` interface.
- **Status:** proposed.

### D-013 `kdotool` is a documented optional fallback only
- Not packaged in Ubuntu 26.04 and works by the same mechanism we use directly (generated KWin scripts). `doctor` reports it if present; `getWindowInfo` polling is the last resort.
- **Status:** proposed.

### D-014 Popups are composited by window id, never captured by area
- **Why.** Menus are separate surfaces and may be absent from an active-window capture; an area grab would include other applications, violating "only the active window".
- **Depends on:** spike S2.1.
- **Status:** proposed.

### D-015 Predicates are a declarative language
- Guide `success_check`s and prerequisites are parsed expressions over adapter state and AT-SPI queries, never Python.
- **Why.** Guides come from models and from imported files; neither may execute code.
- **Status:** proposed.

### D-021 The guide runner lives on the host ⚠ deviates from VISION
- Planner, visual verification, and drift correction stay in the brain; the step state machine and deterministic checks run on the host from `waddle-core`.
- **Why.** Adapter and AT-SPI checks then resolve in milliseconds with no network hop (the <1 s budget), and saved guides keep working while the brain naps, as the degradation table requires.
- **Status:** proposed.

### D-022 The host owns the identity cache and inventory; the brain owns docs, history, and profile
- **Why.** Identity must work with the brain down; knowledge must sit next to the embedder. The guide library is plain YAML on the host for the same reason and for easy sharing.
- **Status:** proposed.

---

## Brain

### D-016 STT, TTS, and OCR inference run in the brain ⚠ deviates from VISION
- The host captures and plays audio; `waddle-voice` (STT + TTS) and OCR (inside `waddle-api`) do the inference.
- **Why.** A Whisper model or OCR runtime in the desktop process would break the 150 MB budget several times over. A separate TTS process also keeps GPL code (Piper, espeak-ng) out of the desktop and API processes.
- **Cost.** Voice needs the brain. With the brain down Waddle is text-only — and has no model to answer with anyway.
- **Status:** proposed.

### D-017 Three GPU images, no PyTorch
- `ollama` (upstream), `waddle-api` (onnxruntime-gpu, CUDA 13) and `waddle-voice` (CTranslate2, CUDA 12 userland).
- **Why.** CTranslate2 wheels need CUDA 12 cuBLAS/cuDNN 9 while onnxruntime-gpu ships CUDA 13 builds; mixing them in one environment is fragile. Avoiding PyTorch (Kokoro via ONNX, OCR via ONNX) removes the largest dependency and its own Blackwell matrix.
- **Blackwell.** Ollama and CTranslate2 document 50-series support (CTranslate2 with `float16` only). onnxruntime-gpu and vLLM are inferred from their CUDA 13 builds and must pass spike S3.1 before being pinned.
- **Status:** proposed.

### D-018 Model roles in config; provisional candidates only
- Provisional defaults for the bake-off: `fast_vlm` Qwen3.5 9B, `smart_model` Qwen3.8 27B, `embedder` Qwen3-Embedding 0.6B; challengers Gemma 4 E4B/31B, Qwen3.5 4B/27B, UI-Venus-2 (vLLM).
- Tags and sizes come from search snippets and are re-verified at pull time (S3.3). No model name appears in code; each config entry names its grounding codec.
- **Status:** proposed; final choice written here by the Phase 9 bake-off.

### D-019 Only `waddle-api` is published; the model server has no route out ⚠ deviates from VISION (stricter)
- The brief says every port binds to `127.0.0.1`. Ollama/vLLM, SearXNG, and the voice sidecar are instead on an internal Docker network and not published at all; the model server's network has no egress, and pulling models is an explicit command.
- **Why.** Smaller surface, and "models cannot phone home" becomes a property of the network rather than a promise.
- **Status:** proposed.

### D-020 SQLite + FTS5 + sqlite-vec 0.1.9 as a derived index
- Canonical embeddings are float32 BLOBs in an ordinary table; the `vec0` table is rebuilt from them and partitioned by app; BM25 and vector results are fused above a `VectorStore` interface. Fallbacks: numpy brute force (automatic if the extension cannot load), `sqliteai-vector`.
- **Why.** sqlite-vec is pre-1.0, brute-force only, and upstream has been quiet since May 2026 — adequate for our scale, but not something to be locked into. No heavy vector database is justified.
- **Status:** proposed; S4.4 measures latency.

### D-025 `balanced` is the default VRAM profile
- `fast_vlm`, embedder, STT, and OCR stay resident (~9 GB); `smart_model` loads on demand and unloads after idle.
- **Why.** Everything warm is ~26–28 GB of 32 GB, which leaves nothing for Blender or Resolve. The cost is a cold load on the first hard question after a pause; the 1.5 s budget is defined for warm models.
- **Status:** proposed; numbers confirmed by S3.2.

### D-026 vLLM uses `response_format`/`structured_outputs`
- The brief mentions "guided decoding"; the `guided_*` parameters were removed in vLLM 0.12. One vLLM process per role.
- **Status:** proposed.

### D-027 SearXNG without Valkey, JSON enabled
- Limiter off for a private instance, so no Valkey container; `search.formats: [html, json]`; a generated `secret_key`.
- **Status:** proposed.

---

## Adapters and voice

### D-023 Blender history is tiered by version ⚠ deviates from VISION (partially)
- 5.3+: `wm.undo_stack` and `wm.reports`. 5.2 LTS: `wm.reports` only. 4.2–5.1: `wm.operators` (≤32 entries, cleared on undo).
- **Why.** These are the only strictly read-only sources. The common workaround for older versions (`info.report_copy`) writes the clipboard and changes selection; `ed.undo_history(item=N)` performs an undo. Both are forbidden.
- **Effect.** On Blender older than 5.2, "what did I just do?" relies on the partial operator list plus frame comparison, and Waddle says so.
- **Status:** proposed.

### D-024 Resolve: vision is the baseline; scripting only on Studio, only if you already enabled it
- Resolve is an XWayland client with no documented accessibility tree. External scripting is Studio-only and needs a user-set preference. Waddle never installs a script into Resolve's folders and calls only an allowlist of getters.
- **Status:** proposed.

### D-028 OBS via `simpleobsws`
- MIT and asyncio-native, versus GPL-3.0-only `obsws-python`. Read requests only.
- **Status:** proposed.

### D-029 TTS: Piper default, Kokoro-ONNX option, CC0 default voice
- Piper is the maintained fork (`OHF-Voice/piper1-gpl`, `piper-tts` 1.8.0), though the project is seeking maintainers; Kokoro runs through `kokoro-onnx` (the PyTorch package is stale and capped below Python 3.13).
- Voices are downloaded at first run with their model cards. The default is a CC0/public-domain English voice, because the popular `lessac` voice derives from a research-only corpus.
- **Status:** proposed; S6.2 picks the voice.

### D-030 STT: faster-whisper 1.2.1 on CTranslate2 ≥4.6.3 with `float16`
- INT8 is disabled on Blackwell. Default model chosen by S6.1 between `turbo` (multilingual) and `distil-large-v3.5` (English).
- **Status:** proposed.

---

## Process

### D-031 Runtime-verified claims
- Everything tagged `SNIP` or `UNVERIFIED` in RESEARCH.md is owned by a named spike in PLAN.md and is not relied on until that spike passes. `waddle doctor` re-checks the machine-specific ones on every run.
- **Status:** accepted.

### D-032 `install.sh` never uses root
- It prints the exact commands for anything that needs root (for example installing `nvidia-container-toolkit`) instead of running them.
- **Why.** Consistent with the read-only stance: Waddle's installer changes only your user session and Waddle's own files.
- **Status:** proposed.
