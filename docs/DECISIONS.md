# Decisions

One entry per decision: what was chosen, why, and what would change it. Entries marked **⚠ deviates from VISION** differ from the brief; the first section holds the questions you have answered. Evidence lives in [RESEARCH.md](RESEARCH.md).

Status key: `proposed` (not yet built) · `accepted` · `superseded by D-xxx`.

---

## Answered by you (2026-11-11)

### D-001 Licence: GPL-3.0-or-later for the whole repository
- **Options.** (A) Whole repository GPL-3.0-or-later. (B) Permissive core (Apache-2.0) with the Blender extension and the TTS service as separate GPL-3.0-or-later components.
- **What research found.** The Blender extension must be GPL-compatible either way. The only other GPL pull is the TTS stack (Piper, and espeak-ng through both Piper's and Kokoro's phonemizers). The architecture already runs TTS and the Blender extension as separate processes behind sockets, so **both options are viable** and neither forces a design change.
- **Recommendation: A.** One licence, no boundary to police, every dependency is compatible, and it matches the ecosystem Waddle lives in (KDE, Blender). Choose B only if you want others to be able to embed the core in non-GPL software.
- **Status:** accepted — you chose option A.

### D-012 Waddle does not enable accessibility itself ⚠ deviates from VISION
- **Context.** Qt apps publish their AT-SPI tree only when the session-wide `org.a11y.Status.IsEnabled` flag (or `QT_LINUX_ACCESSIBILITY_ALWAYS_ON`) is set. On Plasma it is very likely off by default.
- **Decision.** Setting that flag changes a system setting that affects every app, so under the prime directive Waddle will not do it. Onboarding explains what it does and what it costs, and shows the one command with a Copy button; you run it. Until then AT-SPI is reported as unavailable and Waddle uses OCR/vision.
- **Alternative if you prefer:** you explicitly authorize Waddle to set this one flag at onboarding (and unset it on uninstall). Say so and I will record it as a sanctioned exception alongside the KWin rule.
- **Status:** accepted — you run the command.

### D-036 You install in-app components; Waddle only connects ⚠ deviates from VISION (clarifies)
- **Context.** Installing the Blender extension writes into Blender's extensions directory and preferences. The same goes for Krita/GIMP plug-ins and for enabling obs-websocket. That is changing another application's configuration.
- **Decision.** Onboarding shows the file and the exact command (for Blender: `blender --command extension install-file …`) with a Copy button; you run it. Waddle never changes Blender's "Allow Online Access" preference; if the adapter's socket turns out to need it (spike S5.1), the adapter reports "unavailable" and the choice comes back to you.
- **Alternative if you prefer:** you authorize `install.sh` to install and remove the Blender extension. It would then be added to the sanctioned list in ARCHITECTURE §1 and to the uninstall manifest.
- **Status:** accepted — you install them.

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
- **Mechanism.** Scripts cannot receive calls, so host-initiated moves use a generated one-shot script (load, run, unload) with the geometry baked in; following the active monitor lives in `waddle-core.js`.
- **Rule scope.** The rule matches only the penguin window and the highlight overlay, not Settings/History/Eyes. The brief's rule list is kept as is; the penguin window avoids taking focus on show through window flags, not a rule, so the card's input field can still be typed into.
- **Dodging.** A Wayland client sees no pointer outside its own surfaces, so "cursor approaches" is true proximity only while cursor tracking is already loaded; otherwise it triggers on touching the penguin body after a short dwell.
- **Why.** A Wayland client cannot position itself, and rewriting rules plus reconfiguring KWin for every move is slow and disruptive. Moving one's own window is within "its own resources"; every script checks class and title, and a test asserts no other window is touched.
- **Open.** Whether keep-above is enough over fullscreen apps and open menus is spike S1.2.
- **Status:** proposed.

### D-005 Dedicated Rust capture helper that also owns pixel processing ⚠ deviates from VISION (scope of "tiny")
- **Verified.** KWin 6.6 authorizes ScreenShot2 by `/proc/<pid>/exe` matched against `.desktop` `Exec=`, first match wins. Authorizing Python would authorize every Python process. On Plasma 6.8+ there is no check at all.
- **Decision.** `waddle-capture` (Rust, `zbus`) at its own path with its own `.desktop`. It is spawned with an inherited socketpair (no listening socket), exposes a single verb (`capture_window(uuid)`), checks lock state and the blocklist before **and after** each capture (discarding the frame if the title became blocked in between), latches a `blind` command, and performs masking, change detection, downscaling, cropping, and encoding.
- **What it does not guarantee.** That the target is the window you are working in is decided by the desktop and the KWin script; the helper enforces lock, blocklist, and pause only.
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
- **Would change if:** S1.2 shows layer-shell is the only way to stay visible above fullscreen windows or menus.
- **Status:** proposed.

### D-013 `kdotool` is a documented optional fallback only
- Not packaged in Ubuntu 26.04 and works by the same mechanism we use directly (generated KWin scripts). `doctor` reports it if present; `getWindowInfo` polling is the last resort.
- **Status:** proposed.

### D-014 Popups are composited by window id, never captured by area
- **Why.** Menus are separate surfaces and may be absent from a window capture; an area grab would include other applications, violating "only the active window". Each popup passes the same helper checks.
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
- **Cost.** Voice needs the brain. With the brain down Waddle is text-only — and has no model to answer with anyway. Versions that can only be read by OCR (title bar, About dialog) also wait for the brain; cached versions and the metadata, `--version`, and caption-pattern steps still work. This narrows the brief's "version detection still works" for that case.
- **Status:** proposed.

### D-017 Separate GPU worker images, no PyTorch in ours
- `ollama` (upstream), `waddle-ocr` (onnxruntime-gpu, CUDA 13) and `waddle-voice` (CTranslate2, CUDA 12 userland). `waddle-api` itself is CPU-only.
- OCR and STT are separate worker processes so the VRAM governor can terminate them: freeing tensors usually leaves the CUDA context allocated, and only process exit returns it.
- **Why.** CTranslate2 wheels need CUDA 12 cuBLAS/cuDNN 9 while onnxruntime-gpu ships CUDA 13 builds; mixing them in one environment is fragile. Avoiding PyTorch (Kokoro via ONNX, OCR via ONNX) removes the largest dependency and its own Blackwell matrix.
- **Blackwell.** Ollama and CTranslate2 document 50-series support (CTranslate2 with `float16` only). onnxruntime-gpu and vLLM are inferred from their CUDA 13 builds; both are in spike S3.1 and are not pinned until they pass. The vLLM provider itself is built in Phase 9, before the bake-off.
- **Status:** proposed.

### D-018 Model roles in config; provisional candidates only
- Provisional defaults for the bake-off: `fast_vlm` Qwen3.5 9B, `smart_model` Qwen3.8 27B, `embedder` Qwen3-Embedding 0.6B; challengers Gemma 4 E4B/31B, Qwen3.5 4B/27B, UI-Venus-2 (vLLM).
- Tags and sizes come from search snippets and are re-verified at pull time (S3.3). No model name appears in code; each config entry names its grounding codec.
- **Status:** proposed; final choice written here by the Phase 9 bake-off.

### D-019 Nothing is published; only `waddle-fetch` and SearXNG can reach the internet ⚠ deviates from VISION (stricter)
- The brief says every port binds to `127.0.0.1`. Instead no service publishes a port (see D-034). The model server and `waddle-api` sit on internal networks with no route out; pulling models is an explicit command.
- Search, fetch, and HTML extraction run in `waddle-fetch`, which has egress but no token, no database mount, and no access to the model network.
- **Why.** `waddle-api` holds frames, history, and the profile; if it could also reach the internet, a prompt-injected model would have both secrets and a channel. Splitting them makes "the worst case is a bad answer" a property of the topology.
- **Exception.** Enabling a remote model provider gives the API a route to that provider through a compose profile, with a visible indicator.
- **Native mode** loses these network boundaries; `doctor` says so.
- **Status:** proposed.

### D-020 SQLite + FTS5 + sqlite-vec 0.1.9 as a derived index
- Canonical embeddings are float32 BLOBs in an ordinary table; the `vec0` table is rebuilt from them and partitioned by app; BM25 and vector results are fused above a `VectorStore` interface. Fallbacks: numpy brute force (automatic if the extension cannot load), `sqliteai-vector`.
- **Why.** sqlite-vec is pre-1.0, brute-force only, and upstream has been quiet since May 2026 — adequate for our scale, but not something to be locked into. No heavy vector database is justified.
- **Status:** proposed; S4.4 measures latency.

### D-025 `balanced` is the default VRAM profile
- `fast_vlm`, embedder, STT, and OCR stay resident (~9 GB); `smart_model` loads on demand and unloads after idle.
- **Why.** Everything warm is ~26–28 GB of weights alone (context caches and CUDA contexts come on top), which leaves nothing for Blender or Resolve.
- **Cost and mitigation.** The first hard question after a pause pays a cold load. `smart_model` is pre-warmed when the text input opens, push-to-talk is pressed, or a guide starts, and Waddle says "waking up" if it is still loading. The 1.5 s budget is defined for warm models; cold starts are reported separately.
- **Status:** proposed; the numbers are estimates until S3.2.

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

## Privacy and safety narrowing

### D-033 Region-select captures one window ⚠ deviates from VISION
- The brief says "drag a rectangle over any part of the screen". The rectangle is resolved to the topmost non-Waddle window under it; that window goes through the normal gated capture and the crop is cut in the helper.
- **Why.** A compositor area grab includes every app in the rectangle, bypasses the per-window blocklist check, and would contain Waddle himself. This keeps one capture verb and one gate.
- **Effect.** A selection spanning two apps explains the one under most of it; a blocked window under the rectangle means no capture.
- **Status:** proposed; S8.2 covers Plasma panels.

### D-034 The link is a WebSocket over a Unix socket ⚠ deviates from VISION (stricter)
- `$XDG_RUNTIME_DIR/waddle/brain.sock` in a `0700` directory, bind-mounted into `waddle-api`; the token remains as a second factor.
- **Why.** On a TCP port, while the brain is down any local process could bind the port and receive the host's token, frames, and audio; browsers can also reach localhost ports. A user-private socket removes both problems and leaves Waddle with no TCP listeners at all.
- **Status:** proposed.

### D-035 No `spectacle` fallback ⚠ deviates from VISION
- **Why.** `spectacle -b` writes an image file (contradicting "frames live in memory only"), runs outside the helper's gate and fixed options, and depending on your Spectacle settings may save to Pictures or copy to the clipboard.
- **Effect.** The chain is ScreenShot2 → portal window stream → text/voice-only mode with an explanation.
- **Would change if:** you would rather have it as an explicit opt-in.
- **Status:** proposed.

### D-037 No click sensing and no global Esc ⚠ deviates from VISION
- The brief has the highlight clear "on the next click" and Esc stop speech. A read-only Wayland client cannot see clicks outside its own surfaces, and binding Esc globally would take it away from every other app.
- **Decision.** The highlight clears on a frame change of the target, a focus change, step verification, or a timeout. Speech stops on push-to-talk, a new request, a click on the penguin, tray Mute, or the quiet-mode hotkey; Esc works while a Waddle window has focus.
- **Status:** proposed.

### D-038 Tutorial translator works from a URL or text you supply ⚠ deviates from VISION (narrows)
- **Why.** On-screen video is a masked live region and audio is never captured, so Waddle cannot follow a playing video. A page URL or a pasted transcript goes through the normal fetch rules.
- **Status:** proposed.

### D-039 History stores questions and answers as text
- The History panel in the brief requires it; the brief also says "no transcripts on disk". Reading the two together: your questions (typed, or transcribed from push-to-talk) and Waddle's answers are stored with configurable retention; audio, frames, and raw screen text are not. History can be set to in-memory only, and nothing is recorded for blocked windows.
- **Would change if:** you read "no transcripts" as covering spoken questions too — then in-memory-only becomes the default for voice.
- **Status:** proposed.

### D-040 `waddle forget` forgets everything, guides included
- Plain `forget` removes history, profile, focus time, positions, guide records, identity cache, doc cache, and the screencast permission, and — after offering an export — the guide library (`--keep-guides` keeps it). `--app <id>` limits it to one app.
- **Why.** The brief says "forget everything"; a partial default would be a surprise.
- **Status:** proposed.

### D-041 Fetch and search are not model-addressable
- The model cannot supply a URL: fetch takes a search-result id or a pack template filled from the normalized version. Search queries are assembled by code with at most one short quoted screen string. Private, loopback, and link-local targets are refused, including after redirects.
- **Residual.** A short search query reaches SearXNG's upstream engines; injected content could influence a few words of it. Stated in PRIVACY.md.
- **Status:** proposed.

### D-042 Host tools take typed, validated arguments
- `inspect_file` takes an opaque handle minted when you drop a file, never a path. `man`/`tldr` take a validated page name. Tool calls must belong to an open request of yours.
- **Why.** The brain reads the internet and runs models; it must not be able to make the unsandboxed host probe arbitrary paths.
- **Status:** proposed.

### D-043 Observation subprocesses are sandboxed and fail closed
- File inspectors, `man`, and allowlisted `--version` calls run under `bwrap` with no network, a read-only view, and resource limits. If the sandbox cannot be created the step is skipped and reported.
- **Why.** Dropped files are hostile input to parsers with real vulnerability history.
- **Status:** proposed.

---

## Process

### D-031 Runtime-verified claims
- Everything tagged `SNIP` or `UNVERIFIED` in RESEARCH.md is owned by a named spike in PLAN.md and is not relied on until that spike passes. `waddle doctor` re-checks the machine-specific ones on every run.
- **Status:** accepted.

### D-044 The design was reviewed before being shown
- Five independent reviews (read-only/privacy, Wayland feasibility, GPU/performance, security, coverage of the brief), each finding then challenged by a second reviewer. 40 findings survived and were folded in; the largest were the model-controlled egress path (D-019, D-041), the area-capture hole (D-033), the unauthenticated direction of the TCP link (D-034), and Focus mode not actually freeing VRAM (D-017).
- **Status:** accepted.

### D-045 Typing happens in a separate Ask window ⚠ deviates from VISION (clarifies)
- The penguin window never accepts keyboard focus, so clicking Waddle does not pull focus away from Blender or whatever you are in. The guide card's input field opens a small focus-accepting Ask window instead (the same window the text-input hotkey will open in Phase 2). Esc closes it; that Esc is local to the window.
- **Unverified.** Whether KWin honors the no-focus window flag for a Wayland toplevel is part of spike S1.4. If it does not, the fallback is a "do not accept focus" key in the window rule.
- **Status:** accepted for Phase 1; revisit after S1.4.

### D-046 The pid file is a lock, not a claim
- Waddle holds an exclusive `flock` on its pid file while it runs. `waddle stop` and `waddle status` trust the lock, not the file's contents or a process name, so a stale file can never make `stop` signal an unrelated process, and two instances cannot both start.
- **Why.** The first version matched "waddle" in the process command line; review showed it could terminate an unrelated process after a crash and pid reuse.
- **Status:** accepted.

### D-047 Phase 1 review outcome
- Each of the five modules was reviewed by an independent second agent, and the integrated code by two more lenses (shell correctness; read-only and security). 13 confirmed findings were fixed: position memory being overwritten by KWin's own placement, the stop/pid issue above, the input mask trailing the dodge animation, uninstall deleting the only record of the window rule, legacy-format `kwinrulesrc` rules being unlisted, and a structural guard that missed most ways of starting a process.
- A third lens (completeness and mutation-testing of the tests) did not run: it hit a usage limit. I did the completeness pass by hand (PLAN "Phase 1 status"); the mutation-testing of my own shell tests was **not** done.
- **Status:** accepted.

### D-032 `install.sh` never uses root
- It prints the exact commands for anything that needs root (for example installing `nvidia-container-toolkit`) instead of running them.
- **Why.** Consistent with the read-only stance: Waddle's installer changes only your user session and Waddle's own files.
- **Status:** proposed.
