//! Request handling and THE GATE (ARCHITECTURE §4.3, invariant I4).
//!
//! Every capture runs the same sequence, and there is no other way to reach KWin's capture
//! call:
//!
//! 1. latched -> blind (nothing else is asked or touched);
//! 2. session locked -> blind;
//! 3. KWin's own description of the window (and of every popup) must exist ...
//! 4. ... and must not match the blocklist, and the user's part of the blocklist must be
//!    readable (a config file that exists but cannot be parsed means no capture);
//! 5. capture;
//! 6. everything is checked again, and the pixels are thrown away unless it all still holds.

use std::time::Instant;

use image::RgbImage;
use serde_json::json;

use crate::blocklist::Blocklist;
use crate::change::{is_unchanged, ChangeMemory, Signature};
use crate::desktop::{Desktop, DesktopError, LockState, RawCapture, WindowId, WindowInfo};
use crate::pixels::{self, PixelError, PxRect};
use crate::protocol::{parse_request, CaptureParams, Focus, Reply, Verb, PROTOCOL_VERSION};

/// Largest device scale accepted from KWin's reply metadata.
const MAX_SCALE: f64 = 16.0;

/// Why a capture stopped without an image.
enum Stop {
    Blind(&'static str),
    Error(&'static str, String),
}

impl From<DesktopError> for Stop {
    fn from(e: DesktopError) -> Self {
        Stop::Error(e.code(), e.message())
    }
}

impl From<PixelError> for Stop {
    fn from(e: PixelError) -> Self {
        Stop::Error(e.code(), e.message())
    }
}

pub struct Session<D: Desktop> {
    desktop: D,
    blocklist: Blocklist,
    memory: ChangeMemory,
    latched: bool,
    /// Whether the last lock check actually reached logind.
    lock_check: bool,
}

impl<D: Desktop> Session<D> {
    pub fn new(desktop: D, blocklist: Blocklist) -> Self {
        Self {
            desktop,
            blocklist,
            memory: ChangeMemory::default(),
            latched: false,
            lock_check: false,
        }
    }

    pub fn desktop(&self) -> &D {
        &self.desktop
    }

    pub fn desktop_mut(&mut self) -> &mut D {
        &mut self.desktop
    }

    /// Handle one request frame. Returns the id to echo (if the request had one) and the reply.
    pub fn handle(&mut self, payload: &[u8]) -> (Option<i64>, Reply) {
        let request = match parse_request(payload) {
            Ok(request) => request,
            Err(e) => return (e.id, Reply::error(e.code, e.message)),
        };
        let reply = match request.verb {
            Verb::Ping => Reply::ok(),
            Verb::Hello => self.hello(),
            Verb::Forget => {
                self.memory.clear();
                Reply::ok()
            }
            Verb::Blind { on } => {
                self.latched = on;
                if on {
                    self.memory.clear();
                }
                Reply::ok().with("latched", json!(on))
            }
            Verb::CaptureWindow(params) => self.capture(&params),
        };
        (Some(request.id), reply)
    }

    fn hello(&mut self) -> Reply {
        let env = self.desktop.environment();
        self.lock_check = self.desktop.lock_state() != LockState::Unknown;
        self.blocklist.refresh();
        Reply::ok()
            .with("version", json!(crate::VERSION))
            .with("protocol", json!(PROTOCOL_VERSION))
            .with("kwin", json!(env.kwin))
            .with("screenshot2_version", json!(env.screenshot2_version))
            .with("permission_regime", json!(env.regime.as_str()))
            .with("lock_check", json!(self.lock_check))
            .with("config_ok", json!(self.blocklist.config_ok()))
            .with("latched", json!(self.latched))
    }

    fn capture(&mut self, params: &CaptureParams) -> Reply {
        let started = Instant::now();
        // (1) The latch comes first and costs nothing: no D-Bus call, no file access.
        if self.latched {
            return Reply::blind("latched");
        }
        match self.capture_gated(params, started) {
            Ok(reply) => reply,
            Err(Stop::Blind(reason)) => {
                // Blind means blind: forget what was known about what must not be seen.
                if reason == "locked" {
                    self.memory.clear();
                } else {
                    self.memory.remove(&params.uuid);
                }
                Reply::blind(reason)
            }
            Err(Stop::Error(code, message)) => Reply::error(code, &message),
        }
    }

    fn ensure_unlocked(&mut self) -> Result<(), Stop> {
        match self.desktop.lock_state() {
            LockState::Locked => {
                self.lock_check = true;
                Err(Stop::Blind("locked"))
            }
            LockState::Unlocked => {
                self.lock_check = true;
                Ok(())
            }
            LockState::Unknown => {
                // Fail closed: KWin can render a window that sits behind the lock screen.
                self.lock_check = false;
                Err(Stop::Error(
                    "lock_unknown",
                    "the session's lock state cannot be read, so nothing is captured".into(),
                ))
            }
        }
    }

    /// KWin's current description of `id`, if it is this window and may be looked at.
    /// `Ok(None)`: no such window (any more).
    fn inspect(&mut self, id: &WindowId) -> Result<Option<WindowInfo>, Stop> {
        let Some(info) = self.desktop.window_info(id)? else {
            return Ok(None);
        };
        if info.uuid.as_ref().is_some_and(|reported| reported != id) {
            return Ok(None);
        }
        if self.blocklist.is_blocked(&info) {
            return Err(Stop::Blind("blocked"));
        }
        // Fail closed: the user's config exists but cannot be read, so it may name this very
        // window. The helper is restarted after every idle period and then has no earlier
        // copy of the list to fall back on.
        if !self.blocklist.config_ok() {
            return Err(Stop::Error(
                "config_unreadable",
                "Waddle's config.toml cannot be read, so its block list is unknown".into(),
            ));
        }
        Ok(Some(info))
    }

    fn capture_gated(&mut self, params: &CaptureParams, started: Instant) -> Result<Reply, Stop> {
        // (2) lock
        self.ensure_unlocked()?;
        // (3) + (4) the window and every popup, before anything is captured
        self.blocklist.refresh();
        self.inspect(&params.uuid)?
            .ok_or(Stop::Blind("not_found"))?;
        let mut popups = Vec::new();
        for id in &params.popups {
            // A popup that is already gone is simply left out; a blocked one blinds everything.
            if self.inspect(id)?.is_some() {
                popups.push(id.clone());
            }
        }

        // (5) capture
        let raw = self.desktop.capture_window(&params.uuid)?;
        let mut popup_raws = Vec::new();
        for id in popups {
            // S2.1 (unverified): whether KWin accepts popup surfaces here at all. A popup
            // that cannot be captured is left out rather than failing the whole capture.
            if let Ok(popup) = self.desktop.capture_window(&id) {
                popup_raws.push((id, popup));
            }
        }

        // (6) post-check. Any `?` below drops `raw` and `popup_raws`: the pixels are gone.
        match raw.window_id.as_deref().map(WindowId::parse) {
            None => {
                // S2.1 (unverified): ScreenShot2 v5 names the captured window in its reply.
                return Err(Stop::Error(
                    "window_id_missing",
                    "KWin's reply did not name the captured window".into(),
                ));
            }
            Some(Some(captured)) if captured == params.uuid => {}
            Some(_) => return Err(Stop::Blind("not_found")),
        }
        self.ensure_unlocked()?;
        self.blocklist.refresh();
        let window = self
            .inspect(&params.uuid)?
            .ok_or(Stop::Blind("not_found"))?;
        let mut layers = Vec::new();
        for (id, popup) in popup_raws {
            let named = popup.window_id.as_deref().and_then(WindowId::parse);
            let info = self.inspect(&id)?;
            if let (Some(info), true) = (info, named.as_ref() == Some(&id)) {
                layers.push((info, popup));
            }
        }

        let scale = checked_scale(&raw)?;
        let mut image = pixels::to_rgb(&raw.meta, &raw.data)?;
        drop(raw);
        let composited = composite(&mut image, &window, layers, scale);

        let (width, height) = image.dimensions();
        let masks: Vec<PxRect> = params
            .live_regions
            .iter()
            .filter_map(|r| pixels::logical_to_px(r, scale, width, height))
            .collect();
        let signature = Signature::of_thumbnail(pixels::thumbnail(&image, &masks));
        if !params.force {
            if let Some(previous) = self.memory.get(&params.uuid) {
                if is_unchanged(previous, &signature) {
                    // The remembered frame stays the reference, so slow drift adds up.
                    return Ok(Reply::new("unchanged").with("hash", json!(previous.hash_hex())));
                }
            }
        }

        let (cx, cy) = match params.focus {
            Focus::Roi(r) => ((r.x + r.w / 2.0) * scale, (r.y + r.h / 2.0) * scale),
            Focus::Cursor(p) => (p.x * scale, p.y * scale),
            Focus::Centre => (f64::from(width) / 2.0, f64::from(height) / 2.0),
        };
        let crop = pixels::crop_rect(width, height, cx, cy, params.crop_size);
        let (overview, ow, oh) = pixels::encode_overview(&image, params.overview_max)?;
        let crop_png = pixels::encode_crop(&image, crop)?;
        drop(image);
        if overview.len().max(crop_png.len()) > crate::framing::MAX_FRAME {
            return Err(PixelError::TooLarge.into());
        }

        let mut reply = Reply::ok()
            .with("hash", json!(signature.hash_hex()))
            .with("scale", json!(scale))
            .with("width", json!(width))
            .with("height", json!(height))
            .with("overview", json!({"w": ow, "h": oh, "format": "jpeg"}))
            .with(
                "crop",
                json!({"x": crop.x, "y": crop.y, "w": crop.w, "h": crop.h, "format": "png"}),
            )
            .with("popups", json!(composited))
            .with("elapsed_ms", json!(started.elapsed().as_millis() as u64));
        reply.frames = vec![overview, crop_png];
        self.memory.put(params.uuid.clone(), signature);
        Ok(reply)
    }
}

fn checked_scale(raw: &RawCapture) -> Result<f64, Stop> {
    if raw.scale.is_finite() && raw.scale > 0.0 && raw.scale <= MAX_SCALE {
        Ok(raw.scale)
    } else {
        Err(DesktopError::BadReply("KWin reported an impossible scale").into())
    }
}

/// Draw each popup onto the window image; returns how many were drawn.
///
/// S2.1 (unverified): the offset is taken from the two windows' `getWindowInfo` positions
/// (frame geometry) times the window's scale. If the capture origin turns out to be the
/// client geometry for decorated windows, popups land off by the decoration size. Popups
/// are clipped to the window image so that its size keeps matching the window's geometry.
fn composite(
    image: &mut RgbImage,
    window: &WindowInfo,
    layers: Vec<(WindowInfo, RawCapture)>,
    scale: f64,
) -> usize {
    let mut drawn = 0;
    for (info, popup) in layers {
        let Ok(top) = pixels::to_rgb(&popup.meta, &popup.data) else {
            continue;
        };
        let dx = ((info.x - window.x) * scale).round();
        let dy = ((info.y - window.y) * scale).round();
        if !dx.is_finite() || !dy.is_finite() || dx.abs() > 1e6 || dy.abs() > 1e6 {
            continue;
        }
        pixels::overlay(image, &top, dx as i64, dy as i64);
        drawn += 1;
    }
    drawn
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::desktop::{Environment, Regime};
    use crate::pixels::{RawMeta, FORMAT_RGBA8888};
    use serde_json::Value;
    use std::collections::HashMap;

    const WIN: &str = "11111111-0000-4000-8000-000000000001";
    const POP: &str = "22222222-0000-4000-8000-000000000002";

    fn id(text: &str) -> WindowId {
        WindowId::parse(text).unwrap()
    }

    #[derive(Clone)]
    struct Picture {
        w: u32,
        h: u32,
        /// Base gray level; pixels inside `patch` get `patch_value` instead.
        gray: u8,
        patch: Option<(PxRect, u8)>,
    }

    impl Picture {
        fn flat(w: u32, h: u32, gray: u8) -> Self {
            Self {
                w,
                h,
                gray,
                patch: None,
            }
        }
        fn raw(&self) -> Vec<u8> {
            let mut data = Vec::with_capacity((self.w * self.h * 4) as usize);
            for y in 0..self.h {
                for x in 0..self.w {
                    let inside = self
                        .patch
                        .filter(|(r, _)| x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h);
                    let v = inside.map_or(self.gray, |(_, v)| v);
                    data.extend_from_slice(&[v, v, v, 255]);
                }
            }
            data
        }
    }

    enum Event {
        SetCaption(&'static str, &'static str),
        Remove(&'static str),
        Lock,
        WriteFile(std::path::PathBuf, &'static str),
    }

    #[derive(Default)]
    struct FakeDesktop {
        windows: HashMap<WindowId, WindowInfo>,
        pictures: HashMap<WindowId, Picture>,
        lock: Option<LockState>,
        /// Applied when a capture of the main window happens: the world changing mid-capture.
        on_capture: Vec<Event>,
        calls: Vec<String>,
        capture_error: Option<DesktopError>,
        reported_id: Option<Option<String>>,
        /// What the reply for any window other than `WIN` (a popup) names as captured.
        popup_reported_id: Option<String>,
        format: Option<u32>,
        scale: f64,
    }

    impl FakeDesktop {
        fn with_window(uuid: &str, class: &str, caption: &str, picture: Picture) -> Self {
            let mut fake = Self {
                scale: 1.0,
                ..Self::default()
            };
            fake.add(uuid, class, caption, (100.0, 50.0), picture);
            fake
        }
        fn add(
            &mut self,
            uuid: &str,
            class: &str,
            caption: &str,
            pos: (f64, f64),
            picture: Picture,
        ) {
            let info = WindowInfo {
                resource_class: class.into(),
                resource_name: class.into(),
                caption: caption.into(),
                uuid: Some(id(uuid)),
                x: pos.0,
                y: pos.1,
                width: f64::from(picture.w),
                height: f64::from(picture.h),
                ..WindowInfo::default()
            };
            self.windows.insert(id(uuid), info);
            self.pictures.insert(id(uuid), picture);
        }
        fn captures(&self) -> usize {
            self.calls
                .iter()
                .filter(|c| c.starts_with("capture"))
                .count()
        }
    }

    impl Desktop for FakeDesktop {
        fn environment(&mut self) -> Environment {
            self.calls.push("environment".into());
            Environment {
                kwin: true,
                screenshot2_version: Some(5),
                regime: Regime::Restricted,
            }
        }
        fn lock_state(&mut self) -> LockState {
            self.calls.push("lock".into());
            self.lock.unwrap_or(LockState::Unlocked)
        }
        fn window_info(&mut self, id: &WindowId) -> Result<Option<WindowInfo>, DesktopError> {
            self.calls.push(format!("info:{}", &id.as_str()[..8]));
            Ok(self.windows.get(id).cloned())
        }
        fn capture_window(&mut self, id: &WindowId) -> Result<RawCapture, DesktopError> {
            self.calls.push(format!("capture:{}", &id.as_str()[..8]));
            if let Some(e) = self.capture_error.clone() {
                return Err(e);
            }
            let picture = self
                .pictures
                .get(id)
                .cloned()
                .ok_or(DesktopError::Failed("NoSuchWindow".into()))?;
            if id.as_str() == WIN {
                for event in std::mem::take(&mut self.on_capture) {
                    match event {
                        Event::SetCaption(uuid, caption) => {
                            self.windows
                                .get_mut(&super::tests::id(uuid))
                                .unwrap()
                                .caption = caption.into();
                        }
                        Event::Remove(uuid) => {
                            self.windows.remove(&super::tests::id(uuid));
                        }
                        Event::Lock => self.lock = Some(LockState::Locked),
                        Event::WriteFile(path, text) => std::fs::write(path, text).unwrap(),
                    }
                }
            }
            Ok(RawCapture {
                meta: RawMeta {
                    width: picture.w,
                    height: picture.h,
                    stride: picture.w * 4,
                    format: self.format.unwrap_or(FORMAT_RGBA8888),
                },
                scale: self.scale,
                window_id: self
                    .popup_reported_id
                    .clone()
                    .filter(|_| id.as_str() != WIN)
                    .map(Some)
                    .or_else(|| self.reported_id.clone())
                    .unwrap_or_else(|| Some(format!("{{{}}}", id.as_str()))),
                data: picture.raw(),
            })
        }
    }

    fn session(fake: FakeDesktop) -> Session<FakeDesktop> {
        Session::new(fake, Blocklist::new(None))
    }

    fn send(session: &mut Session<FakeDesktop>, request: Value) -> Reply {
        let (id, reply) = session.handle(request.to_string().as_bytes());
        assert_eq!(id, request["id"].as_i64());
        reply
    }

    fn capture(session: &mut Session<FakeDesktop>, extra: Value) -> Reply {
        let mut request = json!({"id": 1, "verb": "capture_window", "uuid": WIN});
        for (k, v) in extra.as_object().unwrap() {
            request[k] = v.clone();
        }
        send(session, request)
    }

    fn decode(bytes: &[u8]) -> RgbImage {
        image::load_from_memory(bytes).unwrap().to_rgb8()
    }

    fn blender() -> FakeDesktop {
        FakeDesktop::with_window(WIN, "blender", "Blender", Picture::flat(800, 600, 90))
    }

    #[test]
    fn ok_path_checks_before_and_after_and_returns_both_images() {
        let mut s = session(blender());
        let reply = capture(&mut s, json!({}));
        assert_eq!(reply.status(), "ok", "{:?}", reply.header);
        assert_eq!(
            s.desktop().calls,
            [
                "lock",
                "info:11111111",
                "capture:11111111",
                "lock",
                "info:11111111"
            ],
            "lock and window are checked on both sides of the capture"
        );
        let h = &reply.header;
        assert_eq!(
            (
                h["width"].as_u64(),
                h["height"].as_u64(),
                h["scale"].as_f64()
            ),
            (Some(800), Some(600), Some(1.0))
        );
        assert_eq!(h["overview"], json!({"w": 800, "h": 600, "format": "jpeg"}));
        assert_eq!(
            h["crop"],
            json!({"x": 144, "y": 44, "w": 512, "h": 512, "format": "png"})
        );
        assert_eq!(h["hash"].as_str().unwrap().len(), 16);
        assert_eq!(reply.frames.len(), 2);
        assert_eq!(decode(&reply.frames[0]).dimensions(), (800, 600));
        let crop = decode(&reply.frames[1]);
        assert_eq!(crop.dimensions(), (512, 512));
        assert_eq!(crop.get_pixel(3, 3).0, [90, 90, 90]);
    }

    #[test]
    fn overview_is_downscaled_and_crop_follows_cursor_and_scale() {
        let mut fake =
            FakeDesktop::with_window(WIN, "blender", "Blender", Picture::flat(3000, 1500, 90));
        fake.scale = 1.5;
        let mut s = session(fake);
        let reply = capture(
            &mut s,
            json!({"cursor": {"x": 1000, "y": 500}, "crop_size": 200, "overview_max": 600}),
        );
        assert_eq!(
            reply.header["overview"],
            json!({"w": 600, "h": 300, "format": "jpeg"})
        );
        // cursor (1000, 500) logical * 1.5 = (1500, 750) px, minus half of 200
        assert_eq!(
            reply.header["crop"],
            json!({"x": 1400, "y": 650, "w": 200, "h": 200, "format": "png"})
        );
        assert_eq!(decode(&reply.frames[0]).dimensions(), (600, 300));
        assert_eq!(decode(&reply.frames[1]).dimensions(), (200, 200));

        let reply = capture(
            &mut s,
            json!({"roi": {"x": 1900, "y": 0, "w": 100, "h": 20}, "force": true}),
        );
        // roi centre (1950, 10) * 1.5 = (2925, 15): clamped into the bottom-right / top edge
        assert_eq!(
            reply.header["crop"],
            json!({"x": 2488, "y": 0, "w": 512, "h": 512, "format": "png"})
        );
    }

    #[test]
    fn identical_second_capture_is_unchanged_and_force_overrides() {
        let mut s = session(blender());
        let first = capture(&mut s, json!({}));
        let second = capture(&mut s, json!({}));
        assert_eq!(second.status(), "unchanged");
        assert_eq!(second.header["hash"], first.header["hash"]);
        assert!(second.frames.is_empty());
        let forced = capture(&mut s, json!({"force": true}));
        assert_eq!(forced.status(), "ok");
        assert_eq!(forced.frames.len(), 2);
    }

    #[test]
    fn a_visible_change_is_reported_and_becomes_the_new_reference() {
        let mut s = session(blender());
        assert_eq!(capture(&mut s, json!({})).status(), "ok");
        let changed = Picture {
            patch: Some((
                PxRect {
                    x: 100,
                    y: 100,
                    w: 300,
                    h: 200,
                },
                255,
            )),
            ..Picture::flat(800, 600, 90)
        };
        s.desktop_mut().pictures.insert(id(WIN), changed);
        assert_eq!(capture(&mut s, json!({})).status(), "ok");
        assert_eq!(capture(&mut s, json!({})).status(), "unchanged");
    }

    #[test]
    fn a_small_local_change_is_still_a_change() {
        // A 20 x 20 px widget flipping in a 1920 x 1080 window: invisible to the mean.
        let base = Picture::flat(1920, 1080, 60);
        let mut s = session(FakeDesktop::with_window(
            WIN,
            "blender",
            "Blender",
            base.clone(),
        ));
        assert_eq!(capture(&mut s, json!({})).status(), "ok");
        let ticked = Picture {
            patch: Some((
                PxRect {
                    x: 900,
                    y: 500,
                    w: 20,
                    h: 20,
                },
                250,
            )),
            ..base
        };
        s.desktop_mut().pictures.insert(id(WIN), ticked);
        assert_eq!(capture(&mut s, json!({})).status(), "ok");
    }

    #[test]
    fn live_regions_mask_change_detection_but_not_the_returned_images() {
        let base = Picture::flat(800, 600, 90);
        let mut s = session(FakeDesktop::with_window(
            WIN,
            "blender",
            "Blender",
            base.clone(),
        ));
        let region = json!({"live_regions": [{"x": 300, "y": 200, "w": 200, "h": 200}]});
        assert_eq!(capture(&mut s, region.clone()).status(), "ok");

        let playing = Picture {
            patch: Some((
                PxRect {
                    x: 300,
                    y: 200,
                    w: 200,
                    h: 200,
                },
                255,
            )),
            ..base.clone()
        };
        s.desktop_mut().pictures.insert(id(WIN), playing);
        assert_eq!(
            capture(&mut s, region.clone()).status(),
            "unchanged",
            "change is inside the live region"
        );
        assert_eq!(
            capture(&mut s, json!({})).status(),
            "ok",
            "without the mask it is a change"
        );

        let mut forced = region.clone();
        forced["force"] = json!(true);
        let reply = capture(&mut s, forced);
        let crop = decode(&reply.frames[1]);
        // Crop is centred: image (400, 300) is crop (256, 256), inside the live region.
        assert_eq!(
            crop.get_pixel(256, 256).0,
            [255, 255, 255],
            "masks never touch what is returned"
        );

        let outside = Picture {
            patch: Some((
                PxRect {
                    x: 0,
                    y: 0,
                    w: 200,
                    h: 200,
                },
                255,
            )),
            ..base
        };
        s.desktop_mut().pictures.insert(id(WIN), outside);
        assert_eq!(
            capture(&mut s, region).status(),
            "ok",
            "a change outside the region is seen"
        );
    }

    #[test]
    fn blocked_class_or_title_is_blind_and_nothing_is_captured() {
        for (class, caption) in [
            ("org.keepassxc.KeePassXC", "Passwords"),
            ("firefox", "Mozilla Firefox Private Browsing"),
            ("io.github.jacobblomq.Waddle", "Waddle"),
        ] {
            let mut s = session(FakeDesktop::with_window(
                WIN,
                class,
                caption,
                Picture::flat(64, 64, 1),
            ));
            let reply = capture(&mut s, json!({"force": true}));
            assert_eq!(reply.header, Reply::blind("blocked").header, "{class}");
            assert!(reply.frames.is_empty());
            assert_eq!(
                s.desktop().captures(),
                0,
                "{class}: KWin must never be asked for pixels"
            );
        }
    }

    #[test]
    fn caption_turning_blocked_during_capture_discards_the_frame() {
        let mut fake = blender();
        fake.on_capture
            .push(Event::SetCaption(WIN, "Online Banking - Mozilla Firefox"));
        let mut s = session(fake);
        let reply = capture(&mut s, json!({}));
        assert_eq!(reply.header, Reply::blind("blocked").header);
        assert!(reply.frames.is_empty());
        assert_eq!(s.desktop().captures(), 1);
        assert!(
            s.memory.is_empty(),
            "nothing is remembered of a discarded frame"
        );
    }

    #[test]
    fn a_blind_result_forgets_the_window() {
        let mut s = session(blender());
        assert_eq!(capture(&mut s, json!({})).status(), "ok");
        s.desktop_mut().windows.get_mut(&id(WIN)).unwrap().caption = "Sign in".into();
        assert_eq!(capture(&mut s, json!({})).status(), "blind");
        s.desktop_mut().windows.get_mut(&id(WIN)).unwrap().caption = "Blender".into();
        assert_eq!(
            capture(&mut s, json!({})).status(),
            "ok",
            "not 'unchanged': the memory was dropped"
        );
    }

    #[test]
    fn locked_session_is_blind_before_and_after() {
        let mut fake = blender();
        fake.lock = Some(LockState::Locked);
        let mut s = session(fake);
        assert_eq!(
            capture(&mut s, json!({})).header,
            Reply::blind("locked").header
        );
        assert_eq!(
            s.desktop().calls,
            ["lock"],
            "a locked session is not even asked about windows"
        );

        let mut fake = blender();
        fake.on_capture.push(Event::Lock);
        let mut s = session(fake);
        let reply = capture(&mut s, json!({}));
        assert_eq!(reply.header, Reply::blind("locked").header);
        assert!(reply.frames.is_empty());
    }

    #[test]
    fn unknown_lock_state_captures_nothing_and_is_reported_by_hello() {
        let mut fake = blender();
        fake.lock = Some(LockState::Unknown);
        let mut s = session(fake);
        let reply = capture(&mut s, json!({}));
        assert_eq!(reply.status(), "error");
        assert_eq!(reply.header["code"], json!("lock_unknown"));
        assert!(
            !s.desktop_mut()
                .calls
                .iter()
                .any(|c| c.starts_with("capture")),
            "no pixels are requested while the lock state is unknown"
        );
        let hello = send(&mut s, json!({"id": 2, "verb": "hello"}));
        assert_eq!(hello.header["lock_check"], json!(false));
        s.desktop_mut().lock = Some(LockState::Unlocked);
        let hello = send(&mut s, json!({"id": 3, "verb": "hello"}));
        assert_eq!(hello.header["lock_check"], json!(true));
    }

    #[test]
    fn latch_refuses_without_touching_the_desktop_and_drops_memory() {
        let mut s = session(blender());
        assert_eq!(capture(&mut s, json!({})).status(), "ok");
        let before = s.desktop().calls.len();
        assert_eq!(
            send(&mut s, json!({"id": 5, "verb": "blind", "on": true})).header["latched"],
            json!(true)
        );
        assert!(s.memory.is_empty());
        for _ in 0..3 {
            assert_eq!(
                capture(&mut s, json!({"force": true})).header,
                Reply::blind("latched").header
            );
        }
        assert_eq!(
            s.desktop().calls.len(),
            before,
            "no lock query, no window query, no capture"
        );
        send(&mut s, json!({"id": 6, "verb": "blind", "on": false}));
        assert_eq!(
            capture(&mut s, json!({})).status(),
            "ok",
            "memory was dropped, so not 'unchanged'"
        );
    }

    #[test]
    fn forget_drops_the_reference_frame() {
        let mut s = session(blender());
        assert_eq!(capture(&mut s, json!({})).status(), "ok");
        assert_eq!(
            send(&mut s, json!({"id": 9, "verb": "forget"})).status(),
            "ok"
        );
        assert_eq!(capture(&mut s, json!({})).status(), "ok");
    }

    #[test]
    fn unknown_window_is_not_found_and_never_captured() {
        let mut s = session(FakeDesktop {
            scale: 1.0,
            ..FakeDesktop::default()
        });
        assert_eq!(
            capture(&mut s, json!({})).header,
            Reply::blind("not_found").header
        );
        assert_eq!(s.desktop().captures(), 0);
    }

    #[test]
    fn window_closing_during_capture_discards_the_frame() {
        let mut fake = blender();
        fake.on_capture.push(Event::Remove(WIN));
        let mut s = session(fake);
        let reply = capture(&mut s, json!({}));
        assert_eq!(reply.header, Reply::blind("not_found").header);
        assert!(reply.frames.is_empty());
    }

    #[test]
    fn reply_naming_another_window_is_discarded() {
        let mut fake = blender();
        fake.reported_id = Some(Some("{99999999-0000-4000-8000-000000000009}".into()));
        let mut s = session(fake);
        assert_eq!(
            capture(&mut s, json!({})).header,
            Reply::blind("not_found").header
        );

        let mut fake = blender();
        fake.reported_id = Some(None);
        let mut s = session(fake);
        let reply = capture(&mut s, json!({}));
        assert_eq!(
            (reply.status(), &reply.header["code"]),
            ("error", &json!("window_id_missing"))
        );
        assert!(reply.frames.is_empty());
    }

    #[test]
    fn window_info_describing_another_window_is_not_found() {
        let mut fake = blender();
        fake.windows.get_mut(&id(WIN)).unwrap().uuid = Some(id(POP));
        let mut s = session(fake);
        assert_eq!(
            capture(&mut s, json!({})).header,
            Reply::blind("not_found").header
        );
        assert_eq!(s.desktop().captures(), 0);
    }

    fn with_popup(popup_class: &str) -> FakeDesktop {
        let mut fake =
            FakeDesktop::with_window(WIN, "blender", "Blender", Picture::flat(400, 300, 10));
        fake.scale = 2.0;
        // Window at (100, 50); popup at (130, 60) logical => (60, 20) px at scale 2.
        fake.add(
            POP,
            popup_class,
            "",
            (130.0, 60.0),
            Picture::flat(40, 30, 200),
        );
        fake
    }

    #[test]
    fn popups_are_composited_at_their_scaled_offset() {
        let mut s = session(with_popup("blender"));
        let reply = capture(&mut s, json!({"popups": [POP], "crop_size": 2048}));
        assert_eq!(reply.status(), "ok", "{:?}", reply.header);
        assert_eq!(reply.header["popups"], json!(1));
        assert_eq!(
            (
                reply.header["width"].as_u64(),
                reply.header["height"].as_u64()
            ),
            (Some(400), Some(300))
        );
        let img = decode(&reply.frames[1]);
        assert_eq!(img.dimensions(), (400, 300));
        assert_eq!(img.get_pixel(60, 20).0, [200, 200, 200]);
        assert_eq!(img.get_pixel(99, 49).0, [200, 200, 200]);
        assert_eq!(img.get_pixel(59, 20).0, [10, 10, 10]);
        assert_eq!(img.get_pixel(100, 49).0, [10, 10, 10]);
        assert_eq!(img.get_pixel(60, 50).0, [10, 10, 10]);
        let calls = &s.desktop().calls;
        assert_eq!(
            calls,
            &[
                "lock",
                "info:11111111",
                "info:22222222",
                "capture:11111111",
                "capture:22222222",
                "lock",
                "info:11111111",
                "info:22222222"
            ]
        );
    }

    #[test]
    fn a_blocked_popup_blinds_the_whole_capture_before_any_pixels() {
        let mut s = session(with_popup("org.kde.kwalletmanager"));
        let reply = capture(&mut s, json!({"popups": [POP]}));
        assert_eq!(reply.header, Reply::blind("blocked").header);
        assert_eq!(s.desktop().captures(), 0);
    }

    #[test]
    fn a_popup_turning_blocked_during_capture_blinds_the_whole_capture() {
        let mut fake = with_popup("blender");
        fake.on_capture
            .push(Event::SetCaption(POP, "Authentication Required"));
        let mut s = session(fake);
        let reply = capture(&mut s, json!({"popups": [POP]}));
        assert_eq!(reply.header, Reply::blind("blocked").header);
        assert!(reply.frames.is_empty());
    }

    #[test]
    fn a_popup_reply_naming_another_window_is_left_out() {
        for named in ["{99999999-0000-4000-8000-000000000009}", "", "not a uuid"] {
            let mut fake = with_popup("blender");
            fake.popup_reported_id = Some(named.into());
            let mut s = session(fake);
            let reply = capture(&mut s, json!({"popups": [POP], "crop_size": 2048}));
            assert_eq!(
                (reply.status(), &reply.header["popups"]),
                ("ok", &json!(0)),
                "{named:?}"
            );
            assert_eq!(
                decode(&reply.frames[1]).get_pixel(60, 20).0,
                [10, 10, 10],
                "pixels KWin did not vouch for are not composited"
            );
        }
    }

    #[test]
    fn vanished_or_uncapturable_popups_are_left_out() {
        let mut s = session(blender());
        let reply = capture(&mut s, json!({"popups": [POP]}));
        assert_eq!(
            (reply.status(), &reply.header["popups"]),
            ("ok", &json!(0)),
            "never existed"
        );

        let mut fake = with_popup("blender");
        fake.on_capture.push(Event::Remove(POP));
        let mut s = session(fake);
        let reply = capture(&mut s, json!({"popups": [POP], "crop_size": 2048}));
        assert_eq!(
            (reply.status(), &reply.header["popups"]),
            ("ok", &json!(0)),
            "closed mid-capture"
        );
        assert_eq!(
            decode(&reply.frames[1]).get_pixel(60, 20).0,
            [10, 10, 10],
            "its pixels were discarded"
        );

        let mut fake = with_popup("blender");
        fake.pictures.remove(&id(POP));
        let mut s = session(fake);
        let reply = capture(&mut s, json!({"popups": [POP]}));
        assert_eq!(
            (reply.status(), &reply.header["popups"]),
            ("ok", &json!(0)),
            "KWin refused the popup"
        );
    }

    /// A scratch `config.toml` holding `text`, removed on drop.
    struct ConfigFile(std::path::PathBuf);
    impl ConfigFile {
        fn new(tag: &str, text: &str) -> Self {
            let dir = std::env::temp_dir().join(format!(
                "waddle-capture-session-{}-{tag}",
                std::process::id()
            ));
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("config.toml"), text).unwrap();
            Self(dir)
        }
        fn path(&self) -> std::path::PathBuf {
            self.0.join("config.toml")
        }
    }
    impl Drop for ConfigFile {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    /// The helper is short-lived: it starts fresh after every idle period, so "the last good
    /// additions" do not exist then. A config that cannot be read may hold block entries the
    /// helper does not know about, and the only safe answer is no capture at all.
    #[test]
    fn an_unreadable_user_config_fails_closed_from_the_first_capture() {
        let config = ConfigFile::new("startup", "[privacy]\nblock_classes = [\"blender\"\n");
        let mut s = Session::new(blender(), Blocklist::new(Some(config.path())));
        let reply = capture(&mut s, json!({"force": true}));
        assert_eq!(
            (reply.status(), &reply.header["code"]),
            ("error", &json!("config_unreadable")),
            "{:?}",
            reply.header
        );
        assert!(reply.frames.is_empty());
        assert_eq!(s.desktop().captures(), 0, "KWin is never asked for pixels");
        let hello = send(&mut s, json!({"id": 2, "verb": "hello"}));
        assert_eq!(hello.header["config_ok"], json!(false));

        // What the defaults block is still reported as blocked.
        s.desktop_mut().windows.get_mut(&id(WIN)).unwrap().caption = "Sign in".into();
        assert_eq!(
            capture(&mut s, json!({})).header,
            Reply::blind("blocked").header
        );

        // Repairing the file restores sight (and the entry in it now applies).
        std::fs::write(config.path(), "[privacy]\nblock_classes = [\"gimp\"]\n").unwrap();
        s.desktop_mut().windows.get_mut(&id(WIN)).unwrap().caption = "Blender".into();
        assert_eq!(capture(&mut s, json!({})).status(), "ok");
    }

    #[test]
    fn a_config_breaking_during_capture_discards_the_frame() {
        let config = ConfigFile::new("during", "[privacy]\nblock_titles = [\"payroll\"]\n");
        let mut fake = blender();
        fake.on_capture.push(Event::WriteFile(
            config.path(),
            "[privacy\nnot toml any more",
        ));
        let mut s = Session::new(fake, Blocklist::new(Some(config.path())));
        let reply = capture(&mut s, json!({}));
        assert_eq!(
            (reply.status(), &reply.header["code"]),
            ("error", &json!("config_unreadable"))
        );
        assert!(reply.frames.is_empty());
        assert_eq!(s.desktop().captures(), 1);
        assert!(s.memory.is_empty());
    }

    #[test]
    fn desktop_errors_become_error_replies_with_stable_codes() {
        for (error, code) in [
            (DesktopError::NoCapturePath, "no_capture_path"),
            (DesktopError::NotAuthorized, "not_authorized"),
            (DesktopError::Timeout, "timeout"),
            (
                DesktopError::Failed("org.kde.KWin.ScreenShot2.Error.Cancelled".into()),
                "capture_failed",
            ),
        ] {
            let mut fake = blender();
            fake.capture_error = Some(error);
            let mut s = session(fake);
            let reply = capture(&mut s, json!({}));
            assert_eq!(
                (reply.status(), &reply.header["code"]),
                ("error", &json!(code))
            );
            assert!(reply.frames.is_empty());
        }
    }

    #[test]
    fn unsupported_pixel_format_and_bad_scale_are_errors() {
        let mut fake = blender();
        fake.format = Some(7); // Format_RGB16
        let mut s = session(fake);
        let reply = capture(&mut s, json!({}));
        assert_eq!(
            (reply.status(), &reply.header["code"]),
            ("error", &json!("bad_format"))
        );

        for scale in [0.0, -1.0, f64::NAN, f64::INFINITY, 64.0] {
            let mut fake = blender();
            fake.scale = scale;
            let mut s = session(fake);
            let reply = capture(&mut s, json!({}));
            assert_eq!(
                (reply.status(), &reply.header["code"]),
                ("error", &json!("bad_reply")),
                "{scale}"
            );
        }
    }

    #[test]
    fn error_messages_never_carry_window_text() {
        let mut fake = FakeDesktop::with_window(
            WIN,
            "secretapp",
            "Top Secret Title",
            Picture::flat(64, 64, 1),
        );
        fake.format = Some(99);
        let mut s = session(fake);
        let reply = capture(&mut s, json!({}));
        let text = serde_json::to_string(&reply.header).unwrap();
        assert!(
            !text.contains("Secret") && !text.contains("secretapp"),
            "{text}"
        );
    }

    #[test]
    fn unknown_verbs_and_bad_requests_touch_nothing() {
        let mut s = session(blender());
        for request in [
            json!({"id": 4, "verb": "capture_area", "x": 0, "y": 0, "w": 100, "h": 100}),
            json!({"id": 4, "verb": "capture_screen"}),
            json!({"id": 4, "verb": "capture_window", "uuid": WIN, "include_cursor": true}),
        ] {
            let reply = send(&mut s, request);
            assert_eq!(reply.status(), "error");
            assert!(reply.frames.is_empty());
        }
        assert!(s.desktop().calls.is_empty());
        let (id, reply) = s.handle(b"not json");
        assert_eq!((id, reply.status()), (None, "error"));
    }

    #[test]
    fn hello_reports_the_environment() {
        let mut s = session(blender());
        let hello = send(&mut s, json!({"id": 1, "verb": "hello"}));
        let h = &hello.header;
        assert_eq!(h["version"], json!(crate::VERSION));
        assert_eq!(h["screenshot2_version"], json!(5));
        assert_eq!(h["permission_regime"], json!("restricted"));
        assert_eq!(
            (&h["kwin"], &h["lock_check"], &h["config_ok"]),
            (&json!(true), &json!(true), &json!(true))
        );
        assert_eq!(
            send(&mut s, json!({"id": 2, "verb": "ping"})).status(),
            "ok"
        );
    }

    #[test]
    fn memory_is_bounded_across_many_windows() {
        let mut fake = FakeDesktop {
            scale: 1.0,
            ..FakeDesktop::default()
        };
        let ids: Vec<String> = (0..12)
            .map(|n| format!("{n:08x}-0000-4000-8000-00000000000a"))
            .collect();
        for uuid in &ids {
            fake.add(
                uuid,
                "blender",
                "Blender",
                (0.0, 0.0),
                Picture::flat(64, 64, 30),
            );
        }
        let mut s = session(fake);
        for uuid in &ids {
            let reply = send(
                &mut s,
                json!({"id": 1, "verb": "capture_window", "uuid": uuid}),
            );
            assert_eq!(reply.status(), "ok");
        }
        assert_eq!(s.memory.len(), crate::change::MAX_TRACKED_WINDOWS);
        let oldest = send(
            &mut s,
            json!({"id": 1, "verb": "capture_window", "uuid": ids[0]}),
        );
        assert_eq!(oldest.status(), "ok", "evicted, so it is new again");
        let newest = send(
            &mut s,
            json!({"id": 1, "verb": "capture_window", "uuid": ids[11]}),
        );
        assert_eq!(newest.status(), "unchanged");
    }

    /// `cargo test --release -- --ignored --nocapture timing` prints the pixel-work cost.
    #[test]
    #[ignore = "timing aid, not an assertion"]
    fn timing_of_a_4k_window() {
        let mut s = session(FakeDesktop::with_window(
            WIN,
            "blender",
            "Blender",
            Picture::flat(3840, 2160, 90),
        ));
        let ok = capture(&mut s, json!({}));
        let started = Instant::now();
        let unchanged = capture(&mut s, json!({}));
        println!(
            "4K ok: {} ms, unchanged: {} ms (includes building the fake image)",
            ok.header["elapsed_ms"],
            started.elapsed().as_millis()
        );
        assert_eq!(unchanged.status(), "unchanged");
    }
}
