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
- [x] Five-lens design review with adversarial verification; confirmed findings folded in
- [x] Your decisions: GPL-3.0-or-later (D-001); you enable accessibility (D-012); you install in-app components (D-036)
- [ ] **Your review** of ARCHITECTURE and PLAN, including the deviations in ARCHITECTURE §12
- [ ] Repo scaffolding once approved: uv workspace, ruff, mypy, pytest, pre-commit, CI, licence file

---

## Phase 1 — Skeleton & character

**Spikes**
- **S1.1** Memory and idle CPU on Plasma 6.6: a bare PySide6 6.12 + QML window, then the same process with every planned host import loaded (pydantic, dbus-fast, websockets, gi/Atspi, QtMultimedia, psutil, nvidia-ml-py) plus one fullscreen transparent overlay. Is 150 MB (PSS) realistic, with which scene-graph backend, and if not, how much do on-demand child processes for AT-SPI and audio recover? 🖥
- **S1.2** Window rule: exact `kwinrulesrc` keys and values for "all desktops" and position; how to scope the rule to the penguin window and overlay only (class + title, or per-window app id); `kwriteconfig6` syntax; reversible install/remove. **Stacking:** with a fullscreen active app, and with an open menu/dropdown, are the penguin and the overlay visible using a keep-above rule, a forced layer rule, or layer-shell? 🖥
- **S1.3** Layer-shell via the distro PySide6 + `org.kde.layershell`: does it work, and is it worth an optional backend?
- **S1.4** Input mask on KWin Wayland: `setMask` for penguin ∪ bubble, re-applied after hide/show; `WindowTransparentForInput` on a fullscreen overlay; a window that does not take focus on show but accepts it on click; file drag-and-drop onto a masked window. 🖥
- **S1.5** KWin scripting lifecycle: `loadScript`/`unloadScript`, `callDBus` payload shapes and reply callbacks, load-run-unload latency of a one-shot move script, behavior across a KWin restart.

**Build**
- `waddle-core`: state enum, message types, config schema.
- Penguin drawn in QML Shapes; all eleven states with transitions; scarf follows accent color; reduced motion and size slider.
- Bubble (streaming text, "more"), guide card (keycaps, checklist, buttons, input), step pill — driven by a mock data source.
- Tray icon with the full menu; state-reflecting icon.
- Placement: rule installer/remover; a minimal `waddle-core.js` (own-geometry reporting, follow-monitor) and the host D-Bus service; one-shot move scripts; drag; per-monitor position memory; dodge (dwell, mask shrink, timed return).
- Bubble and card expose accessible names and roles.
- Theme: read and watch `kdeglobals` (colors, accent, fonts, animation factor); light/dark.
- `waddle start|stop|status`, config loader, structured redacted logging.

**Tests**
- Unit: config, state machine transitions, rule installer against a temp `kwinrulesrc` (idempotent, reversible, leaves other rules byte-identical), theme parsing.
- QML: every state loads and animates under `offscreen`; animations are stopped when not visible; accessible names present (QAccessible).
- Integration (nested KWin): the move script never changes a window that is not Waddle's; dodge does not fire while the card is open or during a press.
- Budget: RSS and CPU-over-60-s measured under nested KWin and recorded.

**Done when**
- Waddle sits bottom-right above all windows, animates smoothly, drags, and remembers position per monitor. 🖥
- Transparent areas pass clicks through. 🖥
- Idle CPU ~0 % and memory < 150 MB, measured. 🖥
- Visible < 2 s from user-unit start to first presented frame, measured. 🖥
- Uninstalling the rule restores `kwinrulesrc` exactly.

**Demo:** a short screen recording cycling through all states, a drag across two monitors, and the measured numbers.

### Phase 1 status

**Built and passing headless; not yet run on a real Plasma session.** Phase 1 is not "done" by its own gate until the checks in the README pass on your machine.

| Item | State |
|---|---|
| Penguin, eleven moods, reduced motion, pause when not exposed | built, tested headless (software renderer only) |
| Bubble, guide card with keycaps, step pill, mock data (`--demo`) | built, tested headless |
| Tray icon and menu | built; Quiet mode, Focus mode, Waddle's eyes, and Settings are shown disabled until their phases |
| Window rule installer | built, tested against a stand-in for `kwriteconfig6`; never run against the real tool or KWin |
| KWin script, one-shot move scripts, D-Bus link | built, tested under Node with a fake KWin and on a private bus; never loaded into KWin |
| Click-through input mask, dodge | built, tested as logic; the offscreen platform cannot apply masks, so the effect itself is unverified |
| Per-monitor position memory | built, tested with simulated KWin reports |
| Theme following (colors, accent, font, animation speed) | built, tested on fixture files |
| CLI `start` / `stop` / `status` / `rule` / `config`, `install.sh`, `uninstall.sh` | built, tested in a sandbox home |
| Idle CPU and memory | **indicative only**: 0.25 % and 105 MB PSS on the offscreen software renderer in a container. Real numbers need your machine. |

**Not done in Phase 1**

- Spikes S1.1–S1.5 all need your session and have not been run. S1.3 (layer-shell) was not attempted.
- The nested-KWin integration tests promised above do not exist: there is no KWin in the build container. They move to the start of Phase 2, where the capture work needs the same harness.
- The size slider is a config key (`ui.size_scale`, applied at start); there is no settings UI until Phase 9.
- The default bottom-right position uses a fixed margin and does not know about panels; it may overlap one.
- The window rule sets no initial position (the script moves Waddle after he appears, so he may be visible elsewhere for a moment), and its "all desktops" value is unconfirmed.
- If `kdeglobals` has no color groups (possible with an untouched default scheme), Waddle falls back to light colors.
- Whether clicking the penguin leaves keyboard focus with your app depends on KWin honoring a window flag; unverified.
- The highlight overlay window itself arrives with "Show me where" in Phase 5; the rule already covers it.

---

## Phase 2 — Senses

**Spikes**
- **S2.1** Does a window capture include open menus/popups? Does `CaptureWindow(uuid)` accept popup surfaces and Plasma shell windows? For a server-decorated Qt app, a client-decorated GTK app, and an XWayland app at 100/125/150 %: does the captured size equal client, frame, or buffer geometry × scale, and where is the image origin? 🖥
- **S2.2** A reliable XWayland-vs-native signal from a KWin script (or from the host). 🖥
- **S2.4** GlobalShortcuts portal: host-registry app id, bind flow, `Deactivated` on key release, which defaults collide on your machine. 🖥
- **S2.5** Lock/idle signals actually emitted on Plasma 6.6 (ScreenSaver vs logind).
- **S2.6** Portal ScreenCast window source: does the restore token survive title changes and reboots, and can a stream be started and stopped per capture without a dialog? 🖥
- **S2.7** Read-only detection of other apps' active screencasts on PipeWire, excluding Waddle's own stream.

**Build**
- KWin scripts: full `waddle-core.js` (focus, geometry, fullscreen, popups, target tracking that ignores Waddle's own windows) and `waddle-cursor.js`.
- `waddle-capture` (Rust): single verb `capture_window(uuid)`; in-helper gate (lock + blocklist checked before and after each capture, latched `blind`); popup compositing; live-region masking, change detection, overview + crop, encoding; portal window-stream fallback.
- Privacy gate; blocklist with defaults (password managers, banking, private browsing); eyes-closed state; rewind-buffer clearing.
- Watching levels (`on-demand`, `guide`, `glance`, `ambient`) and the ≤1 fps throttle.
- Hotkeys with install-time conflict check and alternatives; text input popup.
- Proactivity gate (DND, quiet mode, focus mode, idle, fullscreen, budget) — every unsolicited bubble goes through it from now on.
- Lock/idle/DND handling; fullscreen badge-or-hide setting with a "bring Waddle back" hotkey; auto-hide during other apps' screencasts.
- "Waddle's eyes" panel v1: last overview and crop, app/version/state, capture path.
- App identity steps 1–2 (binary resolution, packaging) — enough to *name* the focused app.
- `waddle doctor` v1: session, KWin scripts, capture path and authorization regime, hotkeys.

**Tests**
- Unit: privacy-gate and proactivity-gate truth tables, blocklist matching, hotkey conflict detection, change-gate with synthetic "live region" frames, process-tree resolvers from `/proc` fixtures.
- Helper: Rust unit tests for hashing/masking/encoding; a fake ScreenShot2 service for protocol tests; tests that the helper refuses when locked or latched even if asked, and discards a frame whose window title became blocked between check and capture.
- Integration (nested KWin): focus events arrive with correct fields; the cursor script only exists while requested; blocked window ⇒ zero capture calls.
- Structural: no files written during capture; no listening sockets.

**Done when**
- Focused app named in < 100 ms (measured span). 🖥
- Only the active window is captured, only when the gate allows. 🖥
- Lock or a blocked window ⇒ eyes closed, no capture, buffer cleared — verified in the integration test and on your machine. 🖥
- All default hotkeys are conflict-free or have accepted alternatives. 🖥

**Demo:** "Waddle's eyes" panel (early version) showing the live crop and overview while switching apps, then going dark on a password manager and on lock.

---

### Phase 2 status

Built and tested headless (Python suite plus 131 Rust tests against a fake KWin, ScreenShot2 and logind). **Nothing has run against your Plasma session yet**, so every spike below is still open.

Done:
- Capture helper with the in-helper gate; KWin focus, popup and cursor scripts; privacy gate and block list; watching levels; proactivity gate; app identity steps 1–2; lock/DND tracking; hotkey conflict check and portal binding; eyes-closed and sleeping states; fullscreen badge/hide/stay with bring-back; "Waddle's eyes" v1; `waddle doctor`; installer builds and registers the helper.

Not done (see D-054):
- Portal ScreenCast fallback (S2.6) — stub only.
- Auto-hide during another app's screencast (S2.7) — setting exists, no effect yet.
- Nested-KWin integration tests — no KWin in the build container.
- Region-select and the cursor stream are built but unused until Phases 6 and 5.
- "Idle" for tips: Waddle cannot tell when you last typed (he never reads input), so the 1.5 s idle rule is not enforced yet.

On your machine 🖥 (README has the steps):
1. `waddle doctor` — session, helper, authorization regime, lock state, hotkey conflicts.
2. S2.1 — open Waddle's eyes; does a picture of the focused window appear, with open menus included, at the right size?
3. S2.5 — lock the screen: eyes closed/asleep, picture gone; unlock: he can look again.
4. Block list — focus a private-browsing window or a password manager: eyes closed, no picture.
5. S2.4 — the shortcut dialog on first start; do Ask, Pause, Quiet and Bring-back fire?
6. "Named in" under 100 ms for a few apps (deb, Flatpak, Snap, AppImage if you have them).
7. S2.2 — does the Window row say XWayland for an X11 app and Wayland for a native one?

---

## Phase 3 — Brain + identity

**Spikes**
- **S3.1** GPU smoke test on the 5090 for every GPU image: Ollama, vLLM (upstream CUDA 13 image), onnxruntime-gpu (CUDA 13), CTranslate2 (CUDA 12, `float16`). Also: per-PID VRAM left behind after unload versus after process exit. Pin the versions that pass. 🖥
- **S3.2** Ollama: schema-constrained output with images; prefix-cache reuse (`prompt_eval_duration`); `keep_alive` unload timing; VRAM per candidate at the chosen `num_ctx` (`size_vram` vs `size`, to catch partial CPU offload); cold-load time per `smart_model` candidate. Replaces the estimate table in ARCHITECTURE §5.4. 🖥
- **S3.3** Verify candidate model tags, sizes, licences, and grounding output conventions at pull time. 🖥

**Build**
- `deploy/docker-compose.yml`: `waddle-api`, `ollama`, `waddle-ocr`, `waddle-fetch`, `searxng`; internal networks; no published ports; containers run as your UID; native-run mode.
- Link protocol (`waddle-core`) over the Unix socket, token as second factor, message-size limits, reconnect with backoff, `napping-brain` state.
- Host tool contract: typed arguments validated on the host, request-scoped, capped.
- `ModelProvider` abstraction with the Ollama implementation; role config; schema-constrained calls with bounded retries.
- Sandboxed `readonly_exec` (bwrap + prlimit, fail closed).
- Version waterfall, remaining steps: allowlisted `--version`, caption patterns, OCR of title/About (basic), the "open Help → About" flow with confirmation; edition, channel, locale; identity cache.
- Installed-app inventory.
- Q&A with screen context; streamed answers into the bubble; source labels `[screen]`/`[system]`; `smart_model` pre-warm on input open.
- `Capabilities` object and the degradation table.
- `waddle doctor`: Docker, GPU/CUDA, brain, models, SearXNG.

**Tests**
- Unit: version parsing/normalization, each packaging detector, the full waterfall from fixtures, cache invalidation, protocol round-trips, schema retry logic with a fake provider, injection fixtures (screen text that says "ignore previous instructions…").
- Integration: brain killed mid-answer ⇒ no crash, `napping-brain`, clean resume; connections without the token rejected; the compose file publishes no ports and `waddle-api` has no route to the internet; no Waddle process listens on TCP; forged tool calls (unknown tool, no open request, over the cap) rejected; log canaries.

**Done when**
- "What am I looking at?" in Blender gives a correct, version-aware answer. 🖥
- First streamed words < 1.5 s with warm models (measured; cold start reported separately). 🖥
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
- OCR worker hardened for small UI text (upscaling, crops).
- Knowledge-pack schema and packs: Blender, Resolve, Kdenlive, KDE Plasma, Ubuntu.
- Retrieval: code-built search queries; fetch by search-result id or pack template only; fetch policy (address-range denial, redirect re-validation, size caps, robots.txt, content types); trafilatura; chunker; embedder; hybrid search; nearest-version fallback with release-note diffing; offline cache; prefetch queue. Every outbound query and URL shown in Waddle's eyes.
- Citations end to end (`Fact.source` → chips in the bubble); "sources" view in Waddle's eyes.
- Terminal support: man and tldr lookup on the host; command cards with Copy, per-part explanation, and destructive/sudo flags.

**Tests**
- Unit: pack validation, URL templating per version, nearest-version selection, chunking, fetch limits, sanitizer, RRF fusion, command-risk classifier, AT-SPI pruning on recorded trees.
- Injection fixtures for web pages, docs, AT-SPI text, window titles, and adapter-supplied names — asserting no outbound request to an unlisted URL or private address, and no host tool call with a path, slash, or leading-dash argument.
- Fetch policy: loopback, private, link-local, and compose-subnet targets refused, including after a redirect.
- Offline test: network disabled ⇒ cached answer, labeled.

**Done when**
- A Blender how-to question is answered from the manual for *your* version with `[docs X.Y]` citations. 🖥
- With an unknown version's docs missing, the answer says which version it used and what may differ.
- First words < 1.5 s warm, re-measured with retrieval enabled. 🖥
- In Kdenlive, Waddle reads a checkbox state through AT-SPI (after you enable it). 🖥
- A Konsole error is explained and a suggested command carries a Copy button and a risk flag. 🖥

---

## Phase 5 — Guide engine + Blender adapter

**Spikes**
- **S5.1** Blender extension: Unix socket server under the `network` permission; behavior of `bpy.app.online_access`; timer cost on a heavy scene; install via `blender --command extension install-file`. 🖥
- **S5.2** Grounding accuracy of the candidate `fast_vlm`s on Blender/Resolve screenshots → confidence threshold for "describe in words instead".

**Build**
- Blender extension + legacy shim + host client; tiered history (5.3 / 5.2 / older); render events; the install flow per D-036.
- Plan schema, planner prompt, prerequisite checks against current state.
- Guide runner on the host: predicates (adapter → AT-SPI → visual), drift detection, corrections, error-dialog detection, pause/resume on questions.
- Coordinate module and highlight overlay (clears on frame change, focus change, verification, or timeout); penguin steps aside from targets; words instead of a ring when geometry and image disagree.
- Event-driven capture triggers during a guide, with the low-rate backstop for visual-only apps.
- Modes: Walkthrough, Coach (timed hint escalation), Challenge (review at the end); "Why?".
- Guide library on disk (YAML), replay; guides flagged for re-validation when their app's version changes.

**Tests**
- Unit: plan schema, predicate language (parser + evaluator, no code execution), runner state machine (every transition), hint escalation timing, coordinate mapping property tests at 100/125/150/175/200 % across monitor layouts and XWayland.
- Structural: AST check that the Blender extension never calls `bpy.ops`, never assigns to `bpy` data, never writes files.
- Blender headless (`blender -b`) test of the snapshot builder where CI allows.

**Done when**
- "Add a cube, give it a glossy material, render with Cycles" is walked through with every step checked off automatically, in Walkthrough and Coach modes. 🖥
- "Show me where" lands on the target at 100 % and at a fractional scale, on both monitors. 🖥
- Screen change → step verified < 1 s: p50/p95 measured separately for an adapter check and for a visual-only check with the adapter disabled. 🖥

---

## Phase 6 — Voice

**Spikes**
- **S6.1** PTT-release → transcript latency on the 5090 for `turbo` and `distil-large-v3.5`, decoding once at release, at ~2/10/30 s utterances; the incremental strategy only if that misses the budget. STT cold-load time. 🖥
- **S6.2** Piper vs Kokoro-ONNX first-audio latency and quality; choice of default CC0 voice.

**Build:** `waddle-voice` sidecar (STT with VAD, TTS engines behind one interface); host mic capture bound strictly to PTT; waveform; playback with amplitude-driven beak; interruption; mute; voice/speed settings; mic indicator; documentation for mapping push-to-talk to a spare mouse button in Plasma.

**Tests:** the mic object does not exist outside PTT (unit + integration with a fake audio source); lock during PTT stops capture; interruption cancels within one audio buffer; transcripts never reach logs.

**Done when:** hold-to-talk question answered by voice; release → transcript < 700 ms (measured) 🖥; push-to-talk, a new question, or a click on Waddle silences speech immediately. 🖥

---

## Phase 7 — System awareness + GPU etiquette

**Build:** telemetry sampler (NVML, psutil, sensors); rolling summary; alert engine with thresholds and cooldowns, routed through the proactivity gate; event sources (failed units, Docker events, focused-app exit status); journal/coredump readers; GET-only Docker client; crash explanation; relevance-gated injection of the system summary; VRAM governor with profiles and hysteresis, including terminating the STT and OCR GPU workers; Focus mode (tray + hotkey); platform notes in knowledge packs.

**Tests:** alert rate-limiting; no alert or tip bubble while DND is on; governor policy as a pure state machine (render start/stop, low VRAM, focus mode, flapping); `readonly_exec` allowlist; the Docker client cannot issue a non-GET to foreign containers; telemetry works with no NVIDIA GPU present.

**Done when:** "why is my PC slow?" names the real top consumer 🖥; starting a Cycles render unloads `smart_model` once, quietly, and reloads later 🖥; after Focus mode, host NVML shows no Waddle process holding VRAM, within a measured time. 🖥

---

## Phase 8 — Magic layer

**Spikes**
- **S8.1** Observing other apps' notifications on Plasma 6.6 with a single-match monitoring connection that exists only while the opt-in is on.
- **S8.2** Resolving a dragged rectangle to the window under it (including Plasma panels) from a KWin script.

**Build:** rewind buffer and "what just happened?"; region-select "What's this?" (single-window capture + crop); drop-a-file inspectors (sandboxed with `bwrap` + `prlimit` + timeouts, `.blend` header parser incl. the 5.0+ format and compressed files) with compatibility reasoning against installed versions; multi-app guides; tutorial translator (from a URL or text you supply); "what's new for you"; skill profile with per-app reset; guide export/import (Markdown/YAML); efficiency tips with the bubble budget and "don't show tips like this"; opt-in notification explainer.

**Tests:** region-select over a blocked window ⇒ zero capture calls; inspectors on hostile fixtures (huge, truncated, playlist-style media, zip bombs are listed not extracted); buffer cleared on blind; tip budget arithmetic; import of a malicious guide file cannot execute anything; profile never stores raw text from the screen.

**Done when:** each feature has a scripted demo, and `waddle forget` provably empties the profile and history.

---

## Phase 9 — Polish & ship

**Build:** vLLM provider and compose profile; OpenAI-compatible and Anthropic providers, off by default, with `allow_frames` / `allow_screen_text` and the remote-active indicator; onboarding (permissions one at a time, hotkey check, skill levels, voice, self-test); full Settings window; History panel; complete `waddle doctor` with exact fixes; debug HUD in Waddle's eyes; eval corpus + harness, re-run in CI when model config or prompts change; **model bake-off** on your GPU with the recommendation written to DECISIONS.md 🖥; `PRIVACY.md`; `ADAPTERS.md` with the Resolve/Kdenlive/Krita/GIMP/OBS stubs (OBS-recording auto-hide arrives with the OBS stub); `install.sh`/`uninstall.sh` hardened; README with GIFs.

**Tests:** a recording fake remote provider receives no image and no screen-derived string unless the matching flag is on; the indicator is set whenever a remote provider served a request.

**Done when:** a clean Ubuntu 26.04 KDE install goes from `install.sh` to a working guide; `uninstall.sh` leaves no trace (rules, shortcuts, units, containers, files); all §3 budgets pass in `waddle bench`; evals are recorded as the baseline.

---

## Cross-cutting, from Phase 1 onward

- **CI on every commit:** ruff, mypy, pytest, cargo test/clippy, the structural read-only checks, and the nested-KWin integration suite.
- **Budgets are tests.** Each phase adds its timing spans to `waddle bench`.
- **DECISIONS.md is updated in the same commit** as the code that embodies the decision.
- **Eval fixtures are captured only on your command** (`waddle eval capture`): one frame at a time, with a preview, blocklist enforced, saved where you choose. Nothing is uploaded or committed without your approval per file. Collecting a few per phase means Phase 9's corpus already exists.
- **Log canaries:** a CI test pushes marker strings through window titles, OCR, transcripts, URLs, and tool arguments and asserts none reach the logs.

## Top risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Host RAM budget (PySide6 + QML baseline) | medium–high | S1.1 first; lazy windows; no numpy/pixels in host; tell you early if 150 MB is not reachable |
| Menus/popups missing from window capture | medium | S2.1; composite popups by UUID |
| Penguin/overlay not visible above fullscreen apps or open menus | medium | S1.2 stacking test; layer rule or layer-shell; words when a target is inside a popup |
| Capture origin/scale differs per toolkit, breaking highlights | medium | S2.1 matrix; size-mismatch check falls back to words |
| Custom-drawn UIs (Blender, Resolve) give AT-SPI nothing; grounding accuracy decides "Show me where" | high | adapter-first for Blender; S5.2 threshold; words when unsure |
| All models + your render do not fit in 32 GB | certain | `balanced` default; governor; Focus mode |
| Plasma 6.8 changes (no ScreenShot2 check) arriving via backport/upgrade | low now | helper works in both regimes; `doctor` reports it |
| Piper upstream looking for maintainers; sqlite-vec quiet | medium | both behind interfaces with a named fallback |
| I cannot run KDE/GPU/Blender in my build environment | certain | nested-KWin CI for what can be automated; explicit 🖥 steps for the rest |
