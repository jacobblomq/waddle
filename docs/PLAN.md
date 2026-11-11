# Waddle build plan

Status: **proposed — awaiting review before Phase 1.**
Phases follow [VISION.md §13](VISION.md). Design: [ARCHITECTURE.md](ARCHITECTURE.md). Evidence and its gaps: [RESEARCH.md](RESEARCH.md).

## How each phase runs

1. **Spikes first.** Each phase opens with short throwaway experiments (`Sx.y`) that settle the facts research could not confirm. A spike ends with a line in DECISIONS.md, and may change the phase's design before real code is written.
2. **Build** in small commits, with tests in the same commit as the code.
3. **Gate.** A phase is done only when its *Done when* list passes, its budgets are measured, and you have seen the demo.
4. **Stop and ask** if anything would bend the read-only rule.

**A constraint on me:** I am building in a cloud container with no KDE session, GPU, microphone, Blender, or Resolve. Everything that can be automated runs there (unit tests, nested `kwin_wayland --virtual` integration tests, mocked D-Bus). Anything marked **🖥 on your machine** needs you to run a command and paste the output, or look at the result; each phase lists exactly those steps.

---

## Phase 0 — Foundations *(this change)*

- [x] Research current versions and APIs → `RESEARCH.md`
- [x] `VISION.md`, `ARCHITECTURE.md`, `PLAN.md`, `DECISIONS.md`
- [ ] **Your decisions:** licence (D-001), accessibility enabling (D-012), and the deviations in ARCHITECTURE §12
- [ ] Repo scaffolding once approved: uv workspace, ruff, mypy, pytest, pre-commit, CI, licence file

---

## Phase 1 — Skeleton & character

**Spikes**
- **S1.1** Bare PySide6 6.12 + QML window RSS and idle CPU on Plasma 6.6 → is 150 MB realistic, and with which scene-graph backend? 🖥
- **S1.2** Window rule: exact `kwinrulesrc` keys and values for "all desktops", "no focus", and position; `kwriteconfig6` syntax; reversible install/remove. 🖥
- **S1.3** Layer-shell via the distro PySide6 + `org.kde.layershell`: does it work, and is it worth an optional backend?
- **S1.4** Input mask on KWin Wayland: `setMask` for penguin ∪ bubble, re-applied after hide/show; `WindowTransparentForInput` on a fullscreen overlay. 🖥

**Build**
- `waddle-core`: state enum, message types, config schema.
- Penguin drawn in QML Shapes; all eleven states with transitions; scarf follows accent color; reduced motion and size slider.
- Bubble (streaming text, "more"), guide card (keycaps, checklist, buttons, input), step pill — driven by a mock data source.
- Tray icon with the full menu; state-reflecting icon.
- Placement: rule installer/remover, drag, per-monitor position memory, dodge on hover.
- Theme: read and watch `kdeglobals` (colors, accent, fonts, animation factor); light/dark.
- `waddle start|stop|status`, config loader, structured redacted logging.

**Tests**
- Unit: config, state machine transitions, rule installer against a temp `kwinrulesrc` (idempotent, reversible, leaves other rules byte-identical), theme parsing.
- QML: every state loads and animates under `offscreen`; animations are stopped when not visible.
- Budget: RSS and CPU-over-60-s measured under nested KWin and recorded.

**Done when**
- Waddle sits bottom-right above all windows, animates smoothly, drags, and remembers position per monitor. 🖥
- Transparent areas pass clicks through. 🖥
- Idle CPU ~0 % and RSS < 150 MB, measured. 🖥
- Uninstalling the rule restores `kwinrulesrc` exactly.

**Demo:** a short screen recording cycling through all states, a drag across two monitors, and the measured numbers.

---

## Phase 2 — Senses

**Spikes**
- **S2.1** Does `CaptureActiveWindow` include open menus/popups? Does `CaptureWindow(uuid)` accept popup surfaces? What do `scale`/`format`/`stride` look like at 125 % and 150 %? 🖥
- **S2.2** A reliable XWayland-vs-native signal from a KWin script (or from the host). 🖥
- **S2.3** `loadScript`/`unloadScript` lifecycle, `callDBus` payload shapes, behavior across a KWin restart.
- **S2.4** GlobalShortcuts portal: host-registry app id, bind flow, `Deactivated` on key release, which defaults collide on your machine. 🖥
- **S2.5** Lock/idle signals actually emitted on Plasma 6.6 (ScreenSaver vs logind).
- **S2.6** Portal ScreenCast window restore: does the token survive title changes and reboots? 🖥

**Build**
- KWin scripts `waddle-core.js` and `waddle-cursor.js`; D-Bus service on the host (`dbus-fast`); own-window movement restricted to Waddle's class.
- `waddle-capture` (Rust): ScreenShot2, in-helper gate (lock + blocklist), live-region masking, change detection, overview + crop, encoding; portal fallback; `spectacle` last resort.
- Privacy gate; blocklist with defaults (password managers, banking, private browsing); eyes-closed state; rewind-buffer clearing.
- Watching levels (`on-demand`, `guide`, `glance`, `ambient`) and the ≤1 fps throttle.
- Hotkeys with install-time conflict check and alternatives; text input popup.
- Lock/idle/DND/fullscreen handling; auto-hide during screencasts.
- App identity steps 1–2 (binary resolution, packaging) — enough to *name* the focused app.
- `waddle doctor` v1: session, KWin scripts, capture path and authorization regime, hotkeys.

**Tests**
- Unit: gate truth table, blocklist matching, hotkey conflict detection, change-gate with synthetic "live region" frames, process-tree resolvers from `/proc` fixtures.
- Helper: Rust unit tests for hashing/masking/encoding; a fake ScreenShot2 service for protocol tests; a test that the helper refuses when the lock flag is set even if asked.
- Integration (nested KWin): focus events arrive with correct fields; the cursor script only exists while requested; blocked window ⇒ zero capture calls.
- Structural: no files written during capture; no listening sockets.

**Done when**
- Focused app named in < 100 ms (measured span). 🖥
- Only the active window is captured, only when the gate allows. 🖥
- Lock or a blocked window ⇒ eyes closed, no capture, buffer cleared — verified in the integration test and on your machine. 🖥
- All default hotkeys are conflict-free or have accepted alternatives. 🖥

**Demo:** "Waddle's eyes" panel (early version) showing the live crop and overview while switching apps, then going dark on a password manager and on lock.

---

## Phase 3 — Brain + identity

**Spikes**
- **S3.1** GPU smoke test on the 5090 for every GPU image we ship: Ollama, onnxruntime-gpu (CUDA 13), CTranslate2 (CUDA 12, `float16`). Pin the versions that pass. 🖥
- **S3.2** Ollama: schema-constrained output with images, prefix-cache reuse (`prompt_eval_duration`), `keep_alive` unload timing, VRAM per candidate model. 🖥
- **S3.3** Verify candidate model tags, sizes, licences, and grounding output conventions at pull time. 🖥

**Build**
- `deploy/docker-compose.yml`: `waddle-api`, `ollama`, `searxng`; internal network; single published port; native-run mode.
- Link protocol (`waddle-core`), token auth, reconnect with backoff, `napping-brain` state.
- `ModelProvider` abstraction with the Ollama implementation; role config; schema-constrained calls with bounded retries.
- Version waterfall steps 3–5: allowlisted `--version`, OCR/vision of title/About (basic), the "open Help → About" flow with confirmation; edition, channel, locale; identity cache.
- Installed-app inventory.
- Q&A with screen context; streamed answers into the bubble; source labels `[screen]`/`[system]`.
- `Capabilities` object and the degradation table.
- `waddle doctor`: Docker, GPU/CUDA, brain, models, SearXNG.

**Tests**
- Unit: version parsing/normalization, each packaging detector, the full waterfall from fixtures, cache invalidation, protocol round-trips, schema retry logic with a fake provider, injection fixtures (screen text that says "ignore previous instructions…").
- Integration: brain killed mid-answer ⇒ no crash, `napping-brain`, clean resume; unauthenticated WS connections rejected; compose file publishes exactly one port on `127.0.0.1`.

**Done when**
- "What am I looking at?" in Blender gives a correct, version-aware answer. 🖥
- First streamed words < 1.5 s with warm models (measured). 🖥
- Cached version lookup < 50 ms.
- Stopping the brain shows `napping-brain` and nothing crashes.

---

## Phase 4 — Perception + knowledge

**Spikes**
- **S4.1** AT-SPI on your session: default state of `org.a11y.Status`, extents for Qt6/GTK4/XWayland apps, origin (client vs frame) per toolkit. 🖥
- **S4.2** Getting `gi` (PyGObject) into the uv environment on system Python 3.14 without weakening the lock.
- **S4.3** OCR accuracy on 10 px UI text from real Blender/Resolve/Kdenlive crops: RapidOCR/PP-OCRv5 vs Tesseract, with and without 2× upscale. 🖥 (screenshots)
- **S4.4** sqlite-vec 0.1.9 latency at 100k/500k chunks at our embedding dimension, versus the numpy fallback.

**Build**
- AT-SPI client (own thread, timeouts): find by role/name, states, extents, focused-subtree pruning; onboarding step that explains how *you* enable accessibility.
- OCR service in `waddle-api`.
- Knowledge-pack schema and packs: Blender, Resolve, Kdenlive, KDE Plasma, Ubuntu.
- Retrieval: SearXNG query builder, fetcher (limits, robots.txt, content types), trafilatura, chunker, embedder, hybrid search, nearest-version fallback with release-note diffing, offline cache, prefetch queue.
- Citations end to end (`Fact.source` → chips in the bubble); "sources" view in Waddle's eyes.
- Terminal support: man and tldr lookup on the host; command cards with Copy, per-part explanation, and destructive/sudo flags.

**Tests**
- Unit: pack validation, URL templating per version, nearest-version selection, chunking, fetch limits, sanitizer, RRF fusion, command-risk classifier, AT-SPI pruning on recorded trees.
- Injection fixtures for web pages, docs, and AT-SPI text.
- Offline test: network disabled ⇒ cached answer, labeled.

**Done when**
- A Blender how-to question is answered from the manual for *your* version with `[docs X.Y]` citations. 🖥
- With an unknown version's docs missing, the answer says which version it used and what may differ.
- In Kdenlive, Waddle reads a checkbox state through AT-SPI (after you enable it). 🖥
- A Konsole error is explained and a suggested command carries a Copy button and a risk flag. 🖥

---

## Phase 5 — Guide engine + Blender adapter

**Spikes**
- **S5.1** Blender extension: Unix socket server under the `network` permission; behavior of `bpy.app.online_access`; timer cost on a heavy scene; install via `blender --command extension install-file`. 🖥
- **S5.2** Grounding accuracy of the candidate `fast_vlm`s on Blender/Resolve screenshots → confidence threshold for "describe in words instead".

**Build**
- Blender extension + legacy shim + host client; tiered history (5.3 / 5.2 / older); render events.
- Plan schema, planner prompt, prerequisite checks against current state.
- Guide runner on the host: predicates (adapter → AT-SPI → visual), drift detection, corrections, error-dialog detection, pause/resume on questions.
- Coordinate module and highlight overlay; penguin steps aside from targets.
- Modes: Walkthrough, Coach (timed hint escalation), Challenge (review at the end); "Why?".
- Guide library on disk (YAML), replay.

**Tests**
- Unit: plan schema, predicate language (parser + evaluator, no code execution), runner state machine (every transition), hint escalation timing, coordinate mapping property tests at 100/125/150/175/200 % across monitor layouts and XWayland.
- Structural: AST check that the Blender extension never calls `bpy.ops`, never assigns to `bpy` data, never writes files.
- Blender headless (`blender -b`) test of the snapshot builder where CI allows.

**Done when**
- "Add a cube, give it a glossy material, render with Cycles" is walked through with every step checked off automatically, in Walkthrough and Coach modes. 🖥
- "Show me where" lands on the target at 100 % and at a fractional scale, on both monitors. 🖥
- Screen change → step verified < 1 s (measured). 🖥

---

## Phase 6 — Voice

**Spikes**
- **S6.1** PTT-release → transcript latency on the 5090 for `turbo` and `distil-large-v3.5` with streamed audio. 🖥
- **S6.2** Piper vs Kokoro-ONNX first-audio latency and quality; choice of default CC0 voice.

**Build:** `waddle-voice` sidecar (STT with VAD, TTS engines behind one interface); host mic capture bound strictly to PTT; waveform; playback with amplitude-driven beak; interruption; mute; voice/speed settings; mic indicator.

**Tests:** the mic object does not exist outside PTT (unit + integration with a fake audio source); lock during PTT stops capture; interruption cancels within one audio buffer; transcripts never reach logs.

**Done when:** hold-to-talk question answered by voice; release → transcript < 700 ms (measured) 🖥; Esc or a new question silences speech immediately. 🖥

---

## Phase 7 — System awareness + GPU etiquette

**Build:** telemetry sampler (NVML, psutil, sensors); rolling summary; alert engine with thresholds and cooldowns; journal/unit/coredump readers; GET-only Docker client; crash explanation; relevance-gated injection of the system summary; VRAM governor with profiles and hysteresis; Focus mode (tray + hotkey); platform notes in knowledge packs.

**Tests:** alert rate-limiting; governor policy as a pure state machine (render start/stop, low VRAM, focus mode, flapping); `readonly_exec` allowlist; the Docker client cannot issue a non-GET to foreign containers; telemetry works with no NVIDIA GPU present.

**Done when:** "why is my PC slow?" names the real top consumer 🖥; starting a Cycles render unloads `smart_model` once, quietly, and reloads later 🖥; Focus mode frees all Waddle VRAM within a couple of seconds (measured). 🖥

---

## Phase 8 — Magic layer

**Build:** rewind buffer and "what just happened?"; region-select "What's this?"; drop-a-file inspectors (sandboxed with `bwrap` + `prlimit` + timeouts, `.blend` header parser incl. the 5.0+ format and compressed files) with compatibility reasoning against installed versions; multi-app guides; tutorial translator; "what's new for you"; skill profile; guide export/import (Markdown/YAML); efficiency tips with the bubble budget and "don't show tips like this"; opt-in notification explainer.

**Tests:** inspectors on hostile fixtures (huge, truncated, playlist-style media, zip bombs are listed not extracted); buffer cleared on blind; tip budget arithmetic; import of a malicious guide file cannot execute anything; profile never stores raw text from the screen.

**Done when:** each feature has a scripted demo, and `waddle forget` provably empties the profile and history.

---

## Phase 9 — Polish & ship

**Build:** onboarding (permissions one at a time, hotkey check, skill levels, voice, self-test); full Settings window; History panel; complete `waddle doctor` with exact fixes; eval corpus + harness; **model bake-off** on your GPU with the recommendation written to DECISIONS.md 🖥; `PRIVACY.md`; `ADAPTERS.md` with the Resolve/Kdenlive/Krita/GIMP/OBS stubs; `install.sh`/`uninstall.sh` hardened; README with GIFs.

**Done when:** a clean Ubuntu 26.04 KDE install goes from `install.sh` to a working guide; `uninstall.sh` leaves no trace (rules, shortcuts, units, containers, files); all §3 budgets pass in `waddle bench`; evals are recorded as the baseline.

---

## Cross-cutting, from Phase 1 onward

- **CI on every commit:** ruff, mypy, pytest, cargo test/clippy, the structural read-only checks, and the nested-KWin integration suite.
- **Budgets are tests.** Each phase adds its timing spans to `waddle bench`.
- **DECISIONS.md is updated in the same commit** as the code that embodies the decision.
- **Evals start early:** from Phase 3 every 🖥 demo also contributes labeled screenshots (with your consent, kept out of the repo unless you say otherwise) so Phase 9's corpus already exists.

## Top risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Host RAM budget (PySide6 + QML baseline) | medium–high | S1.1 first; lazy windows; no numpy/pixels in host; tell you early if 150 MB is not reachable |
| Menus/popups missing from window capture | medium | S2.1; composite popups by UUID |
| Custom-drawn UIs (Blender, Resolve) give AT-SPI nothing; grounding accuracy decides "Show me where" | high | adapter-first for Blender; S5.2 threshold; words when unsure |
| All models + your render do not fit in 32 GB | certain | `balanced` default; governor; Focus mode |
| Plasma 6.8 changes (no ScreenShot2 check) arriving via backport/upgrade | low now | helper works in both regimes; `doctor` reports it |
| Piper upstream looking for maintainers; sqlite-vec quiet | medium | both behind interfaces with a named fallback |
| I cannot run KDE/GPU/Blender in my build environment | certain | nested-KWin CI for what can be automated; explicit 🖥 steps for the rest |
