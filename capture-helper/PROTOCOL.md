# waddle-capture wire protocol (version 1)

`waddle-capture` is Waddle's capture helper (ARCHITECTURE §4.3). This document is the
contract between it and its parent, `waddle-desktop`. The Rust side is
`capture-helper/src/{framing,protocol,session}.rs`; the Python side is
`packages/desktop/src/waddle_desktop/sensors/capture_client.py`.

## 1. Transport

- The parent creates a Unix `SOCK_STREAM` socketpair and starts the helper with one end
  inherited as **file descriptor 3** (or the descriptor named by `--fd N`, N >= 3).
- The helper never listens, binds or connects to anything except the D-Bus session and
  system buses. If the descriptor is not a connected Unix stream socket it prints one line
  to stderr and exits with status 2 without doing anything else.
- The helper exits with status 0 when the parent closes the socket, or when no request
  arrives for the idle timeout (default 120 s, `--idle-timeout SECONDS`, 1..86400).
- A framing violation by the parent (section 2) ends the helper with status 3.

Command line:

```
waddle-capture [--fd N] [--idle-timeout SECONDS]    serve (the normal mode)
waddle-capture --probe [WINDOW-UUID]                print a JSON report for spike S2.1
waddle-capture --version                            print "waddle-capture X.Y.Z"
```

## 2. Framing

Every message, in both directions, is one or more **frames**:

```
+----------------------------+----------------------+
| length: u32, big-endian    | payload: length bytes |
+----------------------------+----------------------+
```

- Maximum payload: **16 MiB** (16 777 216 bytes), enforced by both sides on read and on
  write. A larger announced length is a framing violation: the reader must not read the
  payload and must stop using the stream.
- A zero-length payload is a valid frame.
- End-of-stream between frames is a clean close; inside a frame it is a violation.

## 3. Requests

A request is exactly one frame holding a UTF-8 JSON object:

```json
{"id": 7, "verb": "capture_window", "...": "verb-specific fields"}
```

- `id`: integer (i64), chosen by the parent, echoed in the reply.
- `verb`: one of `hello`, `ping`, `capture_window`, `blind`, `forget`.
- Unknown verbs, unknown fields, missing fields and mistyped fields are all refused with
  an `error` reply; nothing is done. **There is no verb for an area, an output, a
  workspace or an interactive capture**, and no field reaches KWin as an option.
- Requests are handled strictly one at a time, in order. The parent sends the next request
  after reading the complete previous reply.

## 4. Replies

A reply is one **header frame** (a UTF-8 JSON object) followed by the binary frames the
header announces:

```json
{"id": 7, "status": "ok", "frames": [48211, 151300], "...": "status-specific fields"}
```

- `id`: the request's id, or `null` when the request frame was not a JSON object with an
  integer `id`.
- `status`: `"ok"`, `"unchanged"`, `"blind"` or `"error"`.
- `frames`: always present. The byte length of each binary frame that follows, in order.
  It is `[]` for everything except a successful `capture_window`, where it has exactly two
  entries: the overview, then the crop.

### Error replies

```json
{"id": 7, "status": "error", "code": "bad_request", "message": "...", "frames": []}
```

`message` is fixed text or a sanitized D-Bus error *name*. It never contains a window
title, a class, a path or any other text taken from the session.

| `code` | Meaning |
|---|---|
| `bad_request` | Not JSON, no integer `id`, no `verb`, or a missing/unknown/mistyped/out-of-range field |
| `unknown_verb` | The verb does not exist |
| `no_capture_path` | The session bus, `org.kde.KWin` or its ScreenShot2 interface is absent (the portal fallback is not built) |
| `not_authorized` | KWin refused this executable (`.desktop` authorization missing or shadowed) |
| `timeout` | KWin did not answer within 2 s, or the pixels did not arrive within 2 s of its answer |
| `capture_failed` | Any other D-Bus error; `message` is its name |
| `bad_reply` | KWin's answer was malformed (metadata missing, impossible geometry or scale, wrong byte count) |
| `bad_format` | The image is in a `QImage::Format` the helper does not convert |
| `too_large` | The raw image exceeds 256 MiB, or an encoded image exceeds one frame |
| `lock_unknown` | logind did not answer, so the lock state is unknown; nothing is captured |
| `config_unreadable` | The user's `config.toml` exists but cannot be read or parsed, so its block entries are unknown; nothing is captured until it is fixed or removed |
| `window_id_missing` | KWin's reply did not name the window it captured, so the frame was discarded |
| `encode_failed` | JPEG/PNG encoding failed |

## 5. Verbs

### `hello`

```json
{"id": 1, "verb": "hello"}
```
```json
{"id": 1, "status": "ok", "frames": [],
 "version": "0.2.0", "protocol": 1,
 "kwin": true, "screenshot2_version": 5, "permission_regime": "restricted",
 "lock_check": true, "config_ok": true, "latched": false}
```

| Field | Type | Meaning |
|---|---|---|
| `version` | string | helper version |
| `protocol` | int | this document's version |
| `kwin` | bool | `org.kde.KWin` has an owner on the session bus |
| `screenshot2_version` | int or null | the interface's `Version` property; null when absent |
| `permission_regime` | `"restricted"` \| `"open"` \| `"unknown"` | restricted = Plasma 6.4-6.7 (authorized by executable path); open = 6.8+; derived from KWin's version, `unknown` when it cannot be read |
| `lock_check` | bool | logind answered the lock query. `false` means the helper **cannot** see the lock state; every capture then fails with `lock_unknown` |
| `config_ok` | bool | `false` while the user's `config.toml` exists but cannot be read or parsed (captures then fail with `config_unreadable`; see §6) |
| `latched` | bool | the blind latch |

### `ping`

`{"id": 2, "verb": "ping"}` -> `{"id": 2, "status": "ok", "frames": []}`. No side effects.

### `blind`

```json
{"id": 3, "verb": "blind", "on": true}
```
```json
{"id": 3, "status": "ok", "latched": true, "frames": []}
```

`on: true` latches the helper: every `capture_window` answers `blind`/`latched` without
any D-Bus call until `on: false` arrives, and all change-detection memory is dropped at
once. Because requests are serialized, a `blind` sent while a capture is running takes
effect after that capture's reply; the parent must discard that reply.
`waddle_desktop.sensors.capture_client` does this itself: once `set_blind(True)` has been
called, captures requested before it return `blind`/`latched` and their images are dropped.

### `forget`

`{"id": 4, "verb": "forget"}` -> `{"id": 4, "status": "ok", "frames": []}`. Drops all
change-detection memory (the per-window hashes and 32 x 32 thumbnails). The next capture
of any window is `ok`, never `unchanged`.

### `capture_window`

```json
{"id": 5, "verb": "capture_window",
 "uuid": "0a1b2c3d-0000-4000-8000-00000000abcd",
 "popups": ["..."],
 "roi": {"x": 120, "y": 40, "w": 300, "h": 200},
 "overview_max": 1024,
 "crop_size": 512,
 "live_regions": [{"x": 0, "y": 0, "w": 640, "h": 360}],
 "force": false}
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `uuid` | string, required | | KWin window id (`internalId`), with or without braces, any case |
| `popups` | array of uuid, max 8 | `[]` | the window's transient popups to composite onto it |
| `roi` | `{x,y,w,h}` numbers | absent | region of interest in **window-logical** coordinates; the crop is centred on its centre |
| `cursor` | `{x,y}` numbers | absent | pointer position in window-logical coordinates; the crop is centred on it. Mutually exclusive with `roi`. With neither, the crop is centred on the window |
| `overview_max` | int, 64..4096 | 1024 | longest side of the overview in pixels; smaller windows are not enlarged |
| `crop_size` | int, 16..2048 | 512 | side of the square native-resolution crop in pixels |
| `live_regions` | array of `{x,y,w,h}`, max 64 | `[]` | window-logical rectangles ignored by change detection (video, clocks). They are **not** masked in the returned images |
| `force` | bool | false | return images even when nothing changed |

Coordinates must be finite with magnitude <= 1 000 000; `w` and `h` must be >= 0.
Window-logical means logical pixels relative to the top-left of the captured image;
the helper multiplies by the capture's `scale`.

Possible replies:

**`ok`** - header, then two binary frames:

```json
{"id": 5, "status": "ok", "frames": [48211, 151300],
 "hash": "8f3c00a1b2c3d4e5",
 "scale": 1.5, "width": 2880, "height": 1620,
 "overview": {"w": 1024, "h": 576, "format": "jpeg"},
 "crop": {"x": 1200, "y": 400, "w": 512, "h": 512, "format": "png"},
 "popups": 0,
 "elapsed_ms": 84}
```

| Field | Meaning |
|---|---|
| `hash` | 16 lowercase hex digits: the 64-bit difference hash of this frame |
| `scale` | device scale KWin reported for the capture |
| `width`, `height` | size of the captured window image in native pixels |
| `overview` | size of binary frame 1: the whole window, downscaled, JPEG quality 80 |
| `crop` | rectangle (native pixels, always inside the image) of binary frame 2: a lossless PNG at native resolution. Smaller than `crop_size` only when the window is |
| `popups` | how many of the requested popups were composited |
| `elapsed_ms` | time spent in the helper for this request |

**`unchanged`** - `{"id": 5, "status": "unchanged", "hash": "...", "frames": []}`.
The frame matches the last one **sent** for this window; `hash` is that frame's hash.
The comparison does not look at `roi`/`cursor`: a different crop of an unchanged window
needs `force`.

**`blind`** - `{"id": 5, "status": "blind", "reason": "...", "frames": []}`.

| `reason` | Meaning |
|---|---|
| `latched` | the parent latched the helper (`blind`) |
| `locked` | logind reports the session locked, before or after the capture |
| `blocked` | the window or one of its popups matches the blocklist or is one of Waddle's own, before or after the capture |
| `not_found` | KWin knows no such window (before, or no longer after the capture), or what it captured/described was a different window |

**`error`** - see section 4.

## 6. The gate

Enforced inside the helper around every capture, in this order. A capture that stops at
any step returns no image, and any pixels already read are dropped.

1. **Latch.** Latched -> `blind`/`latched`. Nothing else happens.
2. **Lock.** logind `LockedHint` of the caller's session (`XDG_SESSION_ID`, else
   `/org/freedesktop/login1/session/auto`) true -> `blind`/`locked`. logind unreachable:
   `error`/`lock_unknown` and nothing is captured (fail closed); `hello.lock_check` is false.
3. **Window.** `org.kde.KWin /KWin getWindowInfo(uuid)`; an empty result, or a result whose
   `uuid` is a different window -> `blind`/`not_found`. A non-empty result without a string
   `resourceClass` and a string `caption` cannot be gated -> `error`/`bad_reply`.
4. **Blocklist.** `resourceClass`, `resourceName` or `desktopFile` containing a class
   pattern, or `caption` containing a title pattern (substring, after folding both sides:
   invisible characters dropped, then NFKC + Unicode case folding + NFKC, as
   `waddle_core.privacy.normalize` does; whitespace is not collapsed), or any of
   the three equal to `io.github.jacobblomq.Waddle` -> `blind`/`blocked`. Otherwise, if
   the user's config file exists but cannot be read -> `error`/`config_unreadable`.
   Steps 3-4 run for the window and for every popup **before anything is captured**. A
   popup that does not exist is skipped; a blocked popup blinds the whole capture.
5. **Capture.** `org.kde.KWin.ScreenShot2.CaptureWindow(s handle, a{sv} options, h pipe)`
   at `/org/kde/KWin/ScreenShot2`, once for the window and once per popup, with exactly:
   `include-cursor=false`, `include-decoration=false`, `include-shadow=false`,
   `native-resolution=true`. No other ScreenShot2 method is called anywhere in the helper.
6. **Post-check.** The reply's `windowId` must be the requested window; steps 2-4 run
   again. If anything fails now (a tab switched to a blocked title, the screen locked,
   the window closed) the pixels are discarded and the reply is `blind`. A popup that
   vanished or cannot be verified is left out; a popup that became blocked blinds the
   whole capture.

Patterns are the compiled-in `knowledge/privacy/default-blocklist.json` plus
`[privacy] block_classes` / `block_titles` from `$XDG_CONFIG_HOME/waddle/config.toml`
(`~/.config` when unset), re-read when the file's mtime or size changes. A missing file
means no additions. An unreadable or malformed file (not a regular file, over 1 MiB, not
TOML, wrongly typed keys, more than 512 patterns in a list, a pattern over 256 characters)
makes `hello.config_ok` false and **stops all capturing** with `config_unreadable` until it
is fixed or removed: the file may block windows the helper cannot know about, and a helper
that was just started has no earlier copy of the list. The defaults and the additions last
read successfully still answer `blind`/`blocked` meanwhile. Blank patterns are ignored.

After a `blind` result the helper forgets its change-detection memory: all of it for
`locked` and `latched`, the window's for `blocked` and `not_found`.

## 7. Pixel work

- **Input.** Raw pixels from KWin's pipe; `width * height * 4` and `stride * height` must
  each be <= 256 MiB, `stride >= width * 4`, and exactly `stride * height` bytes must
  arrive within 2 s. Accepted `QImage::Format` values: 4 `RGB32`, 5 `ARGB32`,
  6 `ARGB32_Premultiplied`, 16 `RGBX8888`, 17 `RGBA8888`, 18 `RGBA8888_Premultiplied`.
  Everything is converted to opaque RGB8.
- **Popups** are drawn at `(popup.x - window.x, popup.y - window.y) * scale`, clipped to
  the window image.
- **Change detection** uses a 32 x 32 grayscale box-average thumbnail of the composited
  image with `live_regions` left out, and a 64-bit difference hash of a 9 x 8 reduction
  of it. A frame is `unchanged` when, against the last frame sent for the same window,
  all of these hold: Hamming distance <= 2 (`HASH_MAX_HAMMING`), mean absolute thumbnail
  difference < 1.5 gray levels (`MAD_UNCHANGED_BELOW`), and no single thumbnail cell
  differs by 8 or more (`CELL_UNCHANGED_BELOW`). State is kept for the 8 most recently
  used windows (`MAX_TRACKED_WINDOWS`).
- **Output.** Overview: triangle-filter downscale, JPEG quality 80. Crop: PNG, native
  resolution, no resampling.
- Raw pixels never leave the helper. Nothing is written to any file. Window titles and
  classes are never logged or sent.

## 8. `--probe`

Prints one JSON object to stdout and exits 0. It contains numbers, booleans, key names
and D-Bus error names only - no pixels and no window text.

```json
{"helper_version": "0.2.0", "kwin": true, "screenshot2_version": 5,
 "permission_regime": "restricted", "lock_check": true, "locked": false,
 "window_info": {"keys": ["caption", "x", "..."], "numbers": {"x": 100, "width": 1280, "...": 0},
                 "uuid_matches": true},
 "capture": {"attempted": true, "ok": true, "width": 1920, "height": 1200, "stride": 7680,
             "format": 6, "scale": 1.5, "bytes": 9216000,
             "window_id_present": true, "window_id_matches": true,
             "frame_px_expected": {"w": 1920, "h": 1200}}}
```

Without a uuid only the environment is reported (`capture.attempted` is false). On
failure `capture` is `{"attempted": true, "ok": false, "error": "<code>", "error_name": "<D-Bus error name>"}`.
The probe applies the lock and blocklist checks before capturing.
