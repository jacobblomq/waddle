//! End-to-end tests: the real `waddle-capture` binary, driven over a socketpair, talking to
//! fake `org.kde.KWin` / ScreenShot2 / logind services on a private bus.
//!
//! Skipped (with a printed reason) when no `dbus-daemon` is installed.

use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use waddle_capture::framing::{read_frame, write_frame, FrameError, MAX_FRAME};
use zbus::{Fd, OwnedVariant, Variant};

const HELPER: &str = env!("CARGO_BIN_EXE_waddle-capture");
const WIN: &str = "11111111-aaaa-4bbb-8ccc-000000000001";
const OTHER: &str = "22222222-aaaa-4bbb-8ccc-000000000002";
const FORMAT_ARGB32_PREMULTIPLIED: u32 = 6;
/// Bytes of junk after every row, to prove stride padding is honoured end to end.
const STRIDE_PADDING: u32 = 12;

// ---------------------------------------------------------------- fake session state

#[derive(Clone)]
struct Window {
    class: String,
    caption: String,
    x: f64,
    y: f64,
    width: u32,
    height: u32,
    blue: u8,
}

#[derive(Default)]
struct State {
    windows: HashMap<String, Window>,
    /// Every getWindowInfo argument, as received.
    info_calls: Vec<String>,
    /// Every CaptureWindow call: handle and options rendered as text.
    capture_calls: Vec<(String, BTreeMap<String, String>)>,
    lock_reads: usize,
    locked: bool,
    /// Applied when CaptureWindow is called: the title changing while the capture runs.
    caption_on_capture: Option<(String, String)>,
    /// D-Bus error to answer CaptureWindow with.
    capture_error: Option<&'static str>,
    /// How the fake treats the pipe: "" writes everything, "short" drops the last row,
    /// "hold" keeps the pipe open and writes nothing.
    pipe_mode: &'static str,
    scale: f64,
    /// getWindowInfo answers under different key names, as a changed KWin might.
    renamed_info_keys: bool,
}

impl State {
    fn dbus_calls(&self) -> usize {
        self.info_calls.len() + self.capture_calls.len() + self.lock_reads
    }
}

type Shared = Arc<Mutex<State>>;

fn normalize(uuid: &str) -> String {
    uuid.trim_start_matches('{')
        .trim_end_matches('}')
        .to_ascii_lowercase()
}

fn variant<T>(value: T) -> OwnedVariant
where
    Variant<'static>: From<T>,
{
    OwnedVariant::try_from(Variant::from(value)).unwrap()
}

/// The synthetic window image: red follows x, green follows y, blue is the window's.
fn pixel(x: u32, y: u32, blue: u8) -> [u8; 3] {
    [(x % 256) as u8, (y % 256) as u8, blue]
}

fn raw_image(window: &Window, scale: f64) -> (u32, u32, u32, Vec<u8>) {
    let w = (f64::from(window.width) * scale) as u32;
    let h = (f64::from(window.height) * scale) as u32;
    let stride = w * 4 + STRIDE_PADDING;
    let mut data = Vec::with_capacity((stride * h) as usize);
    for y in 0..h {
        for x in 0..w {
            let [r, g, b] = pixel(x, y, window.blue);
            let word = 0xff00_0000u32 | u32::from(r) << 16 | u32::from(g) << 8 | u32::from(b);
            data.extend_from_slice(&word.to_ne_bytes());
        }
        data.extend_from_slice(&[0xEE; STRIDE_PADDING as usize]);
    }
    (w, h, stride, data)
}

struct FakeKwin(Shared);

#[zbus::interface(name = "org.kde.KWin")]
impl FakeKwin {
    #[zbus(name = "getWindowInfo")]
    fn get_window_info(&self, uuid: String) -> HashMap<String, OwnedVariant> {
        let mut state = self.0.lock().unwrap();
        state.info_calls.push(uuid.clone());
        let key = normalize(&uuid);
        let Some(window) = state.windows.get(&key) else {
            return HashMap::new();
        };
        if state.renamed_info_keys {
            return HashMap::from([
                ("windowClass".to_string(), variant(window.class.clone())),
                ("title".to_string(), variant(window.caption.clone())),
                ("uuid".to_string(), variant(format!("{{{key}}}"))),
                ("width".to_string(), variant(f64::from(window.width))),
                ("height".to_string(), variant(f64::from(window.height))),
            ]);
        }
        HashMap::from([
            ("resourceClass".to_string(), variant(window.class.clone())),
            ("resourceName".to_string(), variant(window.class.clone())),
            ("desktopFile".to_string(), variant(window.class.clone())),
            ("caption".to_string(), variant(window.caption.clone())),
            ("uuid".to_string(), variant(format!("{{{key}}}"))),
            ("x".to_string(), variant(window.x)),
            ("y".to_string(), variant(window.y)),
            ("width".to_string(), variant(f64::from(window.width))),
            ("height".to_string(), variant(f64::from(window.height))),
            ("fullscreen".to_string(), variant(false)),
        ])
    }

    #[zbus(name = "supportInformation")]
    fn support_information(&self) -> String {
        "KWin Support Information:\n\nVersion\n=======\nKWin version: 6.6.4\nQt Version: 6.10.2\n"
            .into()
    }
}

#[derive(Debug, zbus::DBusError)]
#[zbus(prefix = "org.kde.KWin.ScreenShot2.Error")]
enum ShotError {
    #[zbus(error)]
    ZBus(zbus::Error),
    NoAuthorized(String),
    Cancelled(String),
}

struct FakeScreenShot(Shared);

#[zbus::interface(name = "org.kde.KWin.ScreenShot2")]
impl FakeScreenShot {
    #[zbus(name = "CaptureWindow")]
    fn capture_window(
        &self,
        handle: String,
        options: HashMap<String, OwnedVariant>,
        pipe: zbus::OwnedFd,
    ) -> Result<HashMap<String, OwnedVariant>, ShotError> {
        let mut state = self.0.lock().unwrap();
        let rendered = options
            .iter()
            .map(|(k, v)| (k.clone(), format!("{:?}", **v)))
            .collect();
        state.capture_calls.push((handle.clone(), rendered));
        match state.capture_error {
            // The message imitates a service leaking window text into its error.
            Some("NoAuthorized") => {
                return Err(ShotError::NoAuthorized("Secret Caption leaked".into()))
            }
            Some(_) => return Err(ShotError::Cancelled("Secret Caption leaked".into())),
            None => {}
        }
        let key = normalize(&handle);
        let Some(window) = state.windows.get(&key).cloned() else {
            return Err(ShotError::Cancelled("no such window".into()));
        };
        if let Some((uuid, caption)) = state.caption_on_capture.take() {
            state.windows.get_mut(&uuid).unwrap().caption = caption;
        }
        let (w, h, stride, data) = raw_image(&window, state.scale);
        // Like KWin: reply first, write the pixels from another thread.
        let pipe: OwnedFd = Fd::from(pipe).try_into().map_err(ShotError::ZBus)?;
        let mode = state.pipe_mode;
        thread::spawn(move || {
            let mut file = fs::File::from(pipe);
            match mode {
                "short" => {
                    let _ = file.write_all(&data[..data.len() - stride as usize]);
                }
                "hold" => thread::sleep(Duration::from_secs(5)),
                _ => {
                    let _ = file.write_all(&data);
                }
            }
        });
        Ok(HashMap::from([
            ("type".to_string(), variant("raw")),
            ("width".to_string(), variant(w)),
            ("height".to_string(), variant(h)),
            ("stride".to_string(), variant(stride)),
            ("format".to_string(), variant(FORMAT_ARGB32_PREMULTIPLIED)),
            ("scale".to_string(), variant(state.scale)),
            ("windowId".to_string(), variant(format!("{{{key}}}"))),
        ]))
    }

    #[zbus(property, name = "Version")]
    fn version(&self) -> u32 {
        5
    }
}

struct FakeLoginSession(Shared);

#[zbus::interface(name = "org.freedesktop.login1.Session")]
impl FakeLoginSession {
    #[zbus(property, name = "LockedHint")]
    fn locked_hint(&self) -> bool {
        let mut state = self.0.lock().unwrap();
        state.lock_reads += 1;
        state.locked
    }
}

// ---------------------------------------------------------------- the rig

static COUNTER: AtomicU32 = AtomicU32::new(0);

struct Rig {
    daemon: Child,
    helper: Option<Child>,
    socket: Option<UnixStream>,
    scratch: PathBuf,
    address: String,
    state: Shared,
    next_id: i64,
}

#[derive(Clone, Copy, PartialEq)]
enum Services {
    All,
    /// KWin answers about windows but has no ScreenShot2 object.
    NoScreenShot,
    /// Nothing on the bus at all.
    None,
}

fn dbus_daemon_available() -> bool {
    let found = Command::new("dbus-daemon")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|s| s.success());
    if !found {
        eprintln!("SKIPPED: dbus-daemon is not installed, cannot run the integration tests");
    }
    found
}

impl Rig {
    fn start(services: Services) -> Option<Self> {
        Self::start_with(services, &[], None)
    }

    fn start_with(services: Services, helper_args: &[&str], config: Option<&str>) -> Option<Self> {
        if !dbus_daemon_available() {
            return None;
        }
        let n = COUNTER.fetch_add(1, Ordering::SeqCst);
        let scratch =
            std::env::temp_dir().join(format!("waddle-capture-it-{}-{n}", std::process::id()));
        fs::create_dir_all(scratch.join("waddle")).unwrap();
        if let Some(text) = config {
            fs::write(scratch.join("waddle/config.toml"), text).unwrap();
        }

        let mut daemon = Command::new("dbus-daemon")
            .args(["--session", "--nofork", "--print-address=1"])
            .arg(format!(
                "--address=unix:path={}",
                scratch.join("bus").display()
            ))
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("dbus-daemon starts");
        let mut address = String::new();
        BufReader::new(daemon.stdout.take().unwrap())
            .read_line(&mut address)
            .unwrap();
        let address = address.trim().to_string();
        assert!(
            address.starts_with("unix:"),
            "dbus-daemon printed no address"
        );

        let state: Shared = Arc::new(Mutex::new(State {
            scale: 1.0,
            ..State::default()
        }));
        if services != Services::None {
            let (ready, wait) = mpsc::channel();
            let (shared, bus) = (state.clone(), address.clone());
            thread::spawn(move || {
                zbus::block_on(async move {
                    let mut builder = zbus::connection::Builder::address(bus.as_str())
                        .name("org.kde.KWin")
                        .name("org.freedesktop.login1")
                        .serve_at("/KWin", FakeKwin(shared.clone()))
                        .serve_at(
                            "/org/freedesktop/login1/session/auto",
                            FakeLoginSession(shared.clone()),
                        );
                    if services == Services::All {
                        builder =
                            builder.serve_at("/org/kde/KWin/ScreenShot2", FakeScreenShot(shared));
                    }
                    let _connection = builder.build().await.expect("fake services start");
                    ready.send(()).unwrap();
                    std::future::pending::<()>().await;
                });
            });
            wait.recv_timeout(Duration::from_secs(10))
                .expect("fake services are up");
        }

        let mut rig = Self {
            daemon,
            helper: None,
            socket: None,
            scratch,
            address,
            state,
            next_id: 0,
        };
        rig.spawn_helper(helper_args);
        Some(rig)
    }

    fn helper_command(&self) -> Command {
        // `sh` moves the socket it gets as stdin to descriptor 3, where the helper expects it.
        let mut command = Command::new("sh");
        command
            .args(["-c", "exec \"$0\" \"$@\" 3<&0 </dev/null", HELPER])
            .env("DBUS_SESSION_BUS_ADDRESS", &self.address)
            .env("DBUS_SYSTEM_BUS_ADDRESS", &self.address)
            .env("XDG_CONFIG_HOME", &self.scratch)
            .env_remove("XDG_SESSION_ID")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        command
    }

    fn spawn_helper(&mut self, args: &[&str]) {
        let (ours, theirs) = UnixStream::pair().unwrap();
        ours.set_read_timeout(Some(Duration::from_secs(20)))
            .unwrap();
        let helper = self
            .helper_command()
            .args(args)
            .stdin(Stdio::from(OwnedFd::from(theirs)))
            .spawn()
            .unwrap();
        self.helper = Some(helper);
        self.socket = Some(ours);
    }

    fn add_window(&self, uuid: &str, class: &str, caption: &str) {
        let window = Window {
            class: class.into(),
            caption: caption.into(),
            x: 100.0,
            y: 50.0,
            width: 640,
            height: 400,
            blue: 10,
        };
        self.state
            .lock()
            .unwrap()
            .windows
            .insert(uuid.into(), window);
    }

    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap()
    }

    fn send_raw(&mut self, payload: &[u8]) {
        write_frame(self.socket.as_mut().unwrap(), payload).unwrap();
    }

    fn read_header(&mut self) -> Value {
        let frame = read_frame(self.socket.as_mut().unwrap()).expect("a reply header");
        serde_json::from_slice(&frame).expect("header is JSON")
    }

    /// Send one request and read its complete reply: header plus announced binary frames.
    fn request(&mut self, mut body: Value) -> (Value, Vec<Vec<u8>>) {
        self.next_id += 1;
        body["id"] = json!(self.next_id);
        self.send_raw(body.to_string().as_bytes());
        let header = self.read_header();
        assert_eq!(
            header["id"],
            json!(self.next_id),
            "reply echoes the request id"
        );
        let sizes: Vec<usize> = header["frames"]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| n.as_u64().unwrap() as usize)
            .collect();
        let frames: Vec<Vec<u8>> = sizes
            .iter()
            .map(|_| read_frame(self.socket.as_mut().unwrap()).unwrap())
            .collect();
        assert_eq!(
            frames.iter().map(Vec::len).collect::<Vec<_>>(),
            sizes,
            "announced sizes are exact"
        );
        (header, frames)
    }

    fn capture(&mut self, uuid: &str, extra: Value) -> (Value, Vec<Vec<u8>>) {
        let mut body = json!({"verb": "capture_window", "uuid": uuid});
        for (key, value) in extra.as_object().unwrap() {
            body[key] = value.clone();
        }
        self.request(body)
    }

    /// Wait for the helper to exit; panics if it is still running after `limit`.
    fn wait_exit(&mut self, limit: Duration) -> (Option<i32>, String) {
        let mut helper = self.helper.take().unwrap();
        let deadline = Instant::now() + limit;
        loop {
            if let Some(status) = helper.try_wait().unwrap() {
                let mut stderr = String::new();
                helper
                    .stderr
                    .take()
                    .unwrap()
                    .read_to_string(&mut stderr)
                    .unwrap();
                return (status.code(), stderr);
            }
            if Instant::now() > deadline {
                let _ = helper.kill();
                panic!("helper still running after {limit:?}");
            }
            thread::sleep(Duration::from_millis(20));
        }
    }
}

impl Drop for Rig {
    fn drop(&mut self) {
        self.socket.take();
        if let Some(mut helper) = self.helper.take() {
            let _ = helper.kill();
            let _ = helper.wait();
        }
        let _ = self.daemon.kill();
        let _ = self.daemon.wait();
        let _ = fs::remove_dir_all(&self.scratch);
    }
}

fn blind(reason: &str) -> Value {
    json!({"status": "blind", "reason": reason})
}

fn without_id(header: &Value) -> Value {
    let mut copy = header.clone();
    let fields = copy.as_object_mut().unwrap();
    fields.remove("id");
    fields.remove("frames");
    copy
}

// ---------------------------------------------------------------- tests

#[test]
fn ok_path_returns_correct_sizes_and_decodable_images() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "blender", "Blender");
    let (header, frames) = rig.capture(
        WIN,
        json!({"overview_max": 320, "crop_size": 128, "cursor": {"x": 100, "y": 300}}),
    );
    assert_eq!(header["status"], "ok", "{header}");
    assert_eq!(
        (header["width"].as_u64(), header["height"].as_u64()),
        (Some(640), Some(400))
    );
    assert_eq!(header["scale"].as_f64(), Some(1.0));
    assert_eq!(
        header["overview"],
        json!({"w": 320, "h": 200, "format": "jpeg"})
    );
    assert_eq!(
        header["crop"],
        json!({"x": 36, "y": 236, "w": 128, "h": 128, "format": "png"})
    );
    assert_eq!(header["hash"].as_str().map(str::len), Some(16));
    assert!(header["elapsed_ms"].is_u64());
    assert_eq!(frames.len(), 2);

    let overview = image::load_from_memory_with_format(&frames[0], image::ImageFormat::Jpeg)
        .unwrap()
        .to_rgb8();
    assert_eq!(overview.dimensions(), (320, 200));
    // Overview pixel (50, 100) is source (100..102, 200..202): red ~100, green ~200, blue 10.
    let p = overview.get_pixel(50, 100).0;
    assert!(
        p[0].abs_diff(101) < 12 && p[1].abs_diff(201) < 12 && p[2] < 40,
        "{p:?}"
    );

    let crop = image::load_from_memory_with_format(&frames[1], image::ImageFormat::Png)
        .unwrap()
        .to_rgb8();
    assert_eq!(crop.dimensions(), (128, 128));
    for (x, y) in [(0u32, 0u32), (5, 9), (127, 127)] {
        assert_eq!(
            crop.get_pixel(x, y).0,
            pixel(x + 36, y + 236, 10),
            "lossless, padding skipped"
        );
    }
}

#[test]
fn fractional_scale_is_reported_and_used_for_the_crop() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "blender", "Blender");
    rig.state().scale = 1.5;
    let (header, frames) = rig.capture(
        WIN,
        json!({"crop_size": 100, "roi": {"x": 200, "y": 100, "w": 40, "h": 20}}),
    );
    assert_eq!(header["status"], "ok", "{header}");
    assert_eq!(
        (header["width"].as_u64(), header["height"].as_u64()),
        (Some(960), Some(600))
    );
    assert_eq!(header["scale"].as_f64(), Some(1.5));
    // roi centre (220, 110) * 1.5 = (330, 165); minus 50
    assert_eq!(
        header["crop"],
        json!({"x": 280, "y": 115, "w": 100, "h": 100, "format": "png"})
    );
    assert_eq!(
        header["overview"],
        json!({"w": 960, "h": 600, "format": "jpeg"})
    );
    assert_eq!(
        image::load_from_memory(&frames[1])
            .unwrap()
            .to_rgb8()
            .get_pixel(0, 0)
            .0,
        pixel(280, 115, 10)
    );
}

#[test]
fn second_identical_capture_is_unchanged_then_force_and_real_changes_are_ok() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "blender", "Blender");
    let (first, _) = rig.capture(WIN, json!({}));
    assert_eq!(first["status"], "ok");
    let (second, frames) = rig.capture(WIN, json!({}));
    assert_eq!(
        without_id(&second),
        json!({"status": "unchanged", "hash": first["hash"]})
    );
    assert!(frames.is_empty());
    let (forced, frames) = rig.capture(WIN, json!({"force": true}));
    assert_eq!(forced["status"], "ok");
    assert_eq!(frames.len(), 2);

    rig.state().windows.get_mut(WIN).unwrap().blue = 250;
    let (changed, _) = rig.capture(WIN, json!({}));
    assert_eq!(changed["status"], "ok", "the window's content changed");

    let (forgot, _) = rig.request(json!({"verb": "forget"}));
    assert_eq!(forgot["status"], "ok");
    let (after, _) = rig.capture(WIN, json!({}));
    assert_eq!(after["status"], "ok", "forget drops the reference frame");
}

#[test]
fn blocked_class_or_title_is_blind_and_capture_is_never_called() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "org.keepassxc.KeePassXC", "Vault");
    rig.add_window(OTHER, "firefox", "Mozilla Firefox Private Browsing");
    let own = "33333333-aaaa-4bbb-8ccc-000000000003";
    rig.add_window(own, "io.github.jacobblomq.Waddle", "Waddle");
    for uuid in [WIN, OTHER, own] {
        let (header, frames) = rig.capture(uuid, json!({"force": true}));
        assert_eq!(without_id(&header), blind("blocked"), "{uuid}");
        assert!(frames.is_empty());
    }
    let state = rig.state();
    assert!(
        state.capture_calls.is_empty(),
        "CaptureWindow must never be called for a blocked window"
    );
    assert_eq!(state.info_calls.len(), 3, "each was looked up exactly once");
}

#[test]
fn user_config_adds_to_the_blocklist_and_a_broken_one_keeps_the_defaults() {
    let config = "[privacy]\nblock_classes = [\"MyBank\"]\nblock_titles = [\"payroll\"]\n";
    let Some(mut rig) = Rig::start_with(Services::All, &[], Some(config)) else {
        return;
    };
    rig.add_window(WIN, "com.example.mybank", "Accounts");
    rig.add_window(OTHER, "libreoffice", "Payroll 2026.ods");
    let fine = "33333333-aaaa-4bbb-8ccc-000000000003";
    rig.add_window(fine, "blender", "Blender");
    assert_eq!(without_id(&rig.capture(WIN, json!({})).0), blind("blocked"));
    assert_eq!(
        without_id(&rig.capture(OTHER, json!({})).0),
        blind("blocked")
    );
    assert_eq!(rig.capture(fine, json!({})).0["status"], "ok");
    assert_eq!(
        rig.request(json!({"verb": "hello"})).0["config_ok"],
        json!(true)
    );

    // Break the file (new size, so the change is noticed whatever the mtime granularity).
    fs::write(
        rig.scratch.join("waddle/config.toml"),
        "[privacy\nthis is not toml at all",
    )
    .unwrap();
    rig.add_window(fine, "kwalletmanager5", "Wallet");
    assert_eq!(
        without_id(&rig.capture(fine, json!({})).0),
        blind("blocked"),
        "defaults survive"
    );
    assert_eq!(
        without_id(&rig.capture(WIN, json!({})).0),
        blind("blocked"),
        "last good additions survive"
    );
    assert_eq!(
        rig.request(json!({"verb": "hello"})).0["config_ok"],
        json!(false)
    );
    assert!(rig.state().capture_calls.len() == 1);

    // A window nothing known blocks: the unreadable file may name it, so no capture.
    rig.add_window(OTHER, "blender", "Blender");
    let (header, frames) = rig.capture(OTHER, json!({"force": true}));
    assert_eq!(
        (header["status"].as_str(), header["code"].as_str()),
        (Some("error"), Some("config_unreadable"))
    );
    assert!(frames.is_empty());
    assert!(rig.state().capture_calls.len() == 1, "KWin was not asked");
}

/// The helper restarts after every idle period, so it regularly meets the config with no
/// "last good" copy in memory. A broken or oversized file must then mean no capture.
#[test]
fn a_config_that_is_broken_or_huge_at_startup_means_no_capture() {
    let huge = format!(
        "[privacy]\nblock_classes = [\"blender\"]\n# {}\n",
        "x".repeat(1024 * 1024)
    );
    for config in [
        "[privacy]\nblock_classes = [\"blender\"\n",
        "[privacy]\nblock_classes = \"blender\"\n",
        "\u{0}\u{1}\u{2}",
        huge.as_str(),
    ] {
        let Some(mut rig) = Rig::start_with(Services::All, &[], Some(config)) else {
            return;
        };
        rig.add_window(WIN, "blender", "Blender");
        let (header, frames) = rig.capture(WIN, json!({}));
        assert_eq!(
            (header["status"].as_str(), header["code"].as_str()),
            (Some("error"), Some("config_unreadable")),
            "{header}"
        );
        assert!(frames.is_empty());
        assert!(rig.state().capture_calls.is_empty());
        assert_eq!(
            rig.request(json!({"verb": "hello"})).0["config_ok"],
            json!(false)
        );
    }
}

/// If KWin's window description lacks the class and caption the blocklist is matched on,
/// the gate cannot be evaluated; that must not be read as "nothing to block".
#[test]
fn window_info_without_class_and_caption_means_no_capture() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "org.keepassxc.KeePassXC", "Passwords");
    rig.state().renamed_info_keys = true;
    let (header, frames) = rig.capture(WIN, json!({}));
    assert_eq!(
        (header["status"].as_str(), header["code"].as_str()),
        (Some("error"), Some("bad_reply")),
        "{header}"
    );
    assert!(frames.is_empty());
    assert!(rig.state().capture_calls.is_empty());
}

#[test]
fn caption_turning_blocked_between_the_checks_is_blind_and_sends_no_image() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "firefox", "Blender Manual - Mozilla Firefox");
    rig.state().caption_on_capture = Some((WIN.into(), "Online Banking - Mozilla Firefox".into()));
    let (header, frames) = rig.capture(WIN, json!({}));
    assert_eq!(without_id(&header), blind("blocked"));
    assert_eq!(header["frames"], json!([]));
    assert!(frames.is_empty());
    assert_eq!(
        rig.state().capture_calls.len(),
        1,
        "the capture did happen; its pixels were discarded"
    );
    assert_eq!(
        rig.state().info_calls.len(),
        2,
        "window info was read before and after"
    );
    // Nothing else is queued on the socket: the very next frame is the reply to this ping.
    let (pong, _) = rig.request(json!({"verb": "ping"}));
    assert_eq!(without_id(&pong), json!({"status": "ok"}));
    // And the discarded frame left no reference behind.
    rig.state().windows.get_mut(WIN).unwrap().caption = "Blender Manual - Mozilla Firefox".into();
    assert_eq!(rig.capture(WIN, json!({})).0["status"], "ok");
}

#[test]
fn latched_is_blind_without_any_dbus_call() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "blender", "Blender");
    assert_eq!(rig.capture(WIN, json!({})).0["status"], "ok");
    let before = rig.state().dbus_calls();
    assert!(
        before >= 5,
        "sanity: the unlatched capture did talk to the fakes ({before})"
    );

    let (latched, _) = rig.request(json!({"verb": "blind", "on": true}));
    assert_eq!(
        without_id(&latched),
        json!({"status": "ok", "latched": true})
    );
    for _ in 0..3 {
        let (header, frames) = rig.capture(WIN, json!({"force": true}));
        assert_eq!(without_id(&header), blind("latched"));
        assert!(frames.is_empty());
    }
    assert_eq!(
        rig.state().dbus_calls(),
        before,
        "no lock read, no window lookup, no capture"
    );

    rig.request(json!({"verb": "blind", "on": false}));
    let (header, _) = rig.capture(WIN, json!({}));
    assert_eq!(
        header["status"], "ok",
        "the latch dropped the reference frame, so this is not 'unchanged'"
    );
}

#[test]
fn locked_session_is_blind() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "blender", "Blender");
    rig.state().locked = true;
    let (header, _) = rig.capture(WIN, json!({}));
    assert_eq!(without_id(&header), blind("locked"));
    assert!(rig.state().capture_calls.is_empty() && rig.state().info_calls.is_empty());
    rig.state().locked = false;
    assert_eq!(rig.capture(WIN, json!({})).0["status"], "ok");
}

#[test]
fn unknown_uuid_is_not_found() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    let (header, frames) = rig.capture(OTHER, json!({}));
    assert_eq!(without_id(&header), blind("not_found"));
    assert!(frames.is_empty());
    assert!(rig.state().capture_calls.is_empty());
}

#[test]
fn the_fixed_options_reach_kwin_exactly() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "blender", "Blender");
    assert_eq!(
        rig.capture(WIN, json!({"force": true, "crop_size": 64})).0["status"],
        "ok"
    );
    let state = rig.state();
    let (handle, options) = &state.capture_calls[0];
    assert_eq!(normalize(handle), WIN);
    let expected: BTreeMap<String, String> = [
        ("include-cursor", "Bool(false)"),
        ("include-decoration", "Bool(false)"),
        ("include-shadow", "Bool(false)"),
        ("native-resolution", "Bool(true)"),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_string(), v.to_string()))
    .collect();
    assert_eq!(
        options, &expected,
        "exactly these four options, nothing else"
    );
}

#[test]
fn unknown_verbs_are_errors_and_make_no_dbus_call() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "blender", "Blender");
    for body in [
        json!({"verb": "capture_area", "x": 0, "y": 0, "w": 800, "h": 600}),
        json!({"verb": "capture_screen", "name": "DP-1"}),
        json!({"verb": "capture_workspace"}),
        json!({"verb": "capture_window", "uuid": WIN, "include-cursor": true}),
        json!({"verb": "capture_window", "uuid": "active"}),
    ] {
        let (header, frames) = rig.request(body.clone());
        assert_eq!(header["status"], "error", "{body}");
        assert!(frames.is_empty());
    }
    let (header, _) = rig.request(json!({"verb": "capture_area"}));
    assert_eq!(header["code"], "unknown_verb");
    assert_eq!(rig.state().dbus_calls(), 0, "nothing reached the bus");

    rig.send_raw(b"this is not json");
    let header = rig.read_header();
    assert_eq!(
        (&header["id"], &header["status"], &header["code"]),
        (&Value::Null, &json!("error"), &json!("bad_request"))
    );
    assert_eq!(
        rig.request(json!({"verb": "ping"})).0["status"],
        "ok",
        "the stream stays usable"
    );
}

#[test]
fn hello_describes_the_session() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    let (hello, _) = rig.request(json!({"verb": "hello"}));
    assert_eq!(hello["status"], "ok");
    assert_eq!(hello["version"], env!("CARGO_PKG_VERSION"));
    assert_eq!(hello["protocol"], 1);
    assert_eq!(hello["kwin"], true);
    assert_eq!(hello["screenshot2_version"], 5);
    assert_eq!(hello["permission_regime"], "restricted");
    assert_eq!(hello["lock_check"], true);
    assert_eq!(hello["config_ok"], true);
}

#[test]
fn kwin_errors_are_reported_by_name_without_their_message() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "blender", "Blender");
    rig.state().capture_error = Some("NoAuthorized");
    let (header, frames) = rig.capture(WIN, json!({}));
    assert_eq!(
        (&header["status"], &header["code"]),
        (&json!("error"), &json!("not_authorized"))
    );
    assert!(frames.is_empty());

    rig.state().capture_error = Some("Cancelled");
    let (header, _) = rig.capture(WIN, json!({}));
    assert_eq!(
        (&header["status"], &header["code"]),
        (&json!("error"), &json!("capture_failed"))
    );
    assert_eq!(
        header["message"],
        "org.kde.KWin.ScreenShot2.Error.Cancelled"
    );
    assert!(
        !header.to_string().contains("Secret"),
        "the error's free text is never forwarded"
    );

    rig.state().capture_error = None;
    assert_eq!(
        rig.capture(WIN, json!({})).0["status"],
        "ok",
        "the helper recovers"
    );
}

#[test]
fn a_stalled_or_short_pixel_pipe_is_an_error_not_a_hang() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "blender", "Blender");
    rig.state().pipe_mode = "hold";
    let started = Instant::now();
    let (header, frames) = rig.capture(WIN, json!({}));
    assert_eq!(
        (&header["status"], &header["code"]),
        (&json!("error"), &json!("timeout")),
        "{header}"
    );
    assert!(frames.is_empty());
    let waited = started.elapsed();
    assert!(
        waited >= Duration::from_millis(1500) && waited < Duration::from_millis(4000),
        "{waited:?}"
    );

    rig.state().pipe_mode = "short";
    let (header, frames) = rig.capture(WIN, json!({}));
    assert_eq!(
        (&header["status"], &header["code"]),
        (&json!("error"), &json!("bad_reply")),
        "{header}"
    );
    assert!(frames.is_empty());

    rig.state().pipe_mode = "";
    assert_eq!(
        rig.capture(WIN, json!({})).0["status"],
        "ok",
        "the helper recovers"
    );
}

#[test]
fn missing_screenshot2_is_no_capture_path() {
    let Some(mut rig) = Rig::start(Services::NoScreenShot) else {
        return;
    };
    rig.add_window(WIN, "blender", "Blender");
    let (hello, _) = rig.request(json!({"verb": "hello"}));
    assert_eq!(
        (&hello["kwin"], &hello["screenshot2_version"]),
        (&json!(true), &Value::Null)
    );
    assert_eq!(hello["permission_regime"], "unknown");
    let (header, frames) = rig.capture(WIN, json!({}));
    assert_eq!(
        (&header["status"], &header["code"]),
        (&json!("error"), &json!("no_capture_path"))
    );
    assert!(frames.is_empty());
}

#[test]
fn without_kwin_or_logind_the_helper_answers_but_cannot_capture() {
    let Some(mut rig) = Rig::start(Services::None) else {
        return;
    };
    let (hello, _) = rig.request(json!({"verb": "hello"}));
    assert_eq!(
        (&hello["kwin"], &hello["lock_check"]),
        (&json!(false), &json!(false))
    );
    assert_eq!(hello["screenshot2_version"], Value::Null);
    let (header, _) = rig.capture(WIN, json!({}));
    assert_eq!(
        (&header["status"], &header["code"]),
        (&json!("error"), &json!("lock_unknown"))
    );
}

#[test]
fn helper_exits_when_the_socket_closes() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    assert_eq!(rig.request(json!({"verb": "ping"})).0["status"], "ok");
    rig.socket.take();
    let (code, _) = rig.wait_exit(Duration::from_secs(5));
    assert_eq!(code, Some(0));
}

#[test]
fn helper_exits_after_the_idle_timeout() {
    let Some(mut rig) = Rig::start_with(Services::All, &["--idle-timeout", "1"], None) else {
        return;
    };
    assert_eq!(rig.request(json!({"verb": "ping"})).0["status"], "ok");
    let started = Instant::now();
    let (code, _) = rig.wait_exit(Duration::from_secs(6));
    assert_eq!(code, Some(0));
    assert!(
        started.elapsed() >= Duration::from_millis(800),
        "it waited for the timeout"
    );
    // Our end is still open: the helper left on its own, and reading now sees end-of-stream.
    assert!(matches!(
        read_frame(rig.socket.as_mut().unwrap()),
        Err(FrameError::Closed)
    ));
}

#[test]
fn oversize_request_frame_ends_the_helper() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    let announced = (MAX_FRAME as u32 + 1).to_be_bytes();
    rig.socket.as_mut().unwrap().write_all(&announced).unwrap();
    let (code, stderr) = rig.wait_exit(Duration::from_secs(5));
    assert_eq!(code, Some(3), "{stderr}");
    assert!(matches!(
        read_frame(rig.socket.as_mut().unwrap()),
        Err(FrameError::Closed)
    ));
}

fn run_without_socket(script: &str, args: &[&str]) -> (Option<i32>, String, String) {
    let output = Command::new("sh")
        .args(["-c", script, HELPER])
        .args(args)
        .env(
            "DBUS_SESSION_BUS_ADDRESS",
            "unix:path=/nonexistent/waddle-test-bus",
        )
        .env(
            "DBUS_SYSTEM_BUS_ADDRESS",
            "unix:path=/nonexistent/waddle-test-bus",
        )
        .stdin(Stdio::null())
        .output()
        .unwrap();
    (
        output.status.code(),
        String::from_utf8_lossy(&output.stdout).into_owned(),
        String::from_utf8_lossy(&output.stderr).into_owned(),
    )
}

#[test]
fn helper_refuses_to_start_without_a_socket_on_fd_3() {
    // fd 3 closed, fd 3 a regular file, fd 3 a pipe: none is a socket.
    for script in [
        "exec \"$0\" \"$@\" 3<&-",
        "exec \"$0\" \"$@\" 3</dev/null",
        "echo | exec \"$0\" \"$@\" 3<&0",
    ] {
        let started = Instant::now();
        let (code, stdout, stderr) = run_without_socket(script, &[]);
        assert_eq!(code, Some(2), "{script}: {stderr}");
        assert!(stderr.contains("not a socket"), "{script}: {stderr}");
        assert!(stdout.is_empty());
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "it must fail immediately"
        );
    }
}

#[test]
fn custom_fd_number_is_honoured() {
    let (ours, theirs) = UnixStream::pair().unwrap();
    let mut child = Command::new("sh")
        .args(["-c", "exec \"$0\" --fd 7 7<&0 </dev/null 3<&-", HELPER])
        .env(
            "DBUS_SESSION_BUS_ADDRESS",
            "unix:path=/nonexistent/waddle-test-bus",
        )
        .env(
            "DBUS_SYSTEM_BUS_ADDRESS",
            "unix:path=/nonexistent/waddle-test-bus",
        )
        .stdin(Stdio::from(OwnedFd::from(theirs)))
        .spawn()
        .unwrap();
    let mut ours = ours;
    ours.set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    write_frame(&mut ours, br#"{"id":1,"verb":"ping"}"#).unwrap();
    let header: Value = serde_json::from_slice(&read_frame(&mut ours).unwrap()).unwrap();
    assert_eq!(header, json!({"id": 1, "status": "ok", "frames": []}));
    drop(ours);
    assert_eq!(child.wait().unwrap().code(), Some(0));
}

#[test]
fn version_and_bad_arguments() {
    let (code, stdout, _) = run_without_socket("exec \"$0\" \"$@\"", &["--version"]);
    assert_eq!(code, Some(0));
    assert_eq!(
        stdout.trim(),
        format!("waddle-capture {}", env!("CARGO_PKG_VERSION"))
    );
    for args in [
        &["--listen"][..],
        &["--fd", "1"],
        &["--idle-timeout", "0"],
        &["--idle-timeout"],
    ] {
        let (code, _, stderr) = run_without_socket("exec \"$0\" \"$@\"", args);
        assert_eq!(code, Some(2), "{args:?}");
        assert!(stderr.contains("usage:"), "{args:?}");
    }
}

fn probe(rig: &Rig, args: &[&str]) -> Value {
    let output = Command::new(HELPER)
        .arg("--probe")
        .args(args)
        .env("DBUS_SESSION_BUS_ADDRESS", &rig.address)
        .env("DBUS_SYSTEM_BUS_ADDRESS", &rig.address)
        .env("XDG_CONFIG_HOME", &rig.scratch)
        .env_remove("XDG_SESSION_ID")
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0));
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(
        !text.contains("Probe Caption") && !text.contains("probeclass"),
        "no window text: {text}"
    );
    serde_json::from_str(&text).expect("probe prints one JSON document")
}

#[test]
fn probe_reports_metadata_and_never_window_text() {
    let Some(rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "probeclass", "Probe Caption");
    rig.state().scale = 1.25;

    let report = probe(&rig, &[WIN]);
    assert_eq!(report["screenshot2_version"], 5);
    assert_eq!(report["permission_regime"], "restricted");
    let capture = &report["capture"];
    assert_eq!(
        (&capture["attempted"], &capture["ok"]),
        (&json!(true), &json!(true)),
        "{report}"
    );
    assert_eq!(
        (capture["width"].as_u64(), capture["height"].as_u64()),
        (Some(800), Some(500))
    );
    assert_eq!(
        capture["stride"].as_u64(),
        Some(800 * 4 + u64::from(STRIDE_PADDING))
    );
    assert_eq!(capture["format"], FORMAT_ARGB32_PREMULTIPLIED);
    assert_eq!(capture["scale"].as_f64(), Some(1.25));
    assert_eq!(capture["window_id_matches"], true);
    assert_eq!(
        report["window_info"]["numbers"]["width"].as_f64(),
        Some(640.0)
    );
    assert_eq!(report["window_info"]["numbers"]["x"].as_f64(), Some(100.0));
    assert!(
        report["window_info"]["keys"]
            .as_array()
            .unwrap()
            .contains(&json!("caption")),
        "key names only"
    );

    let none = probe(&rig, &[]);
    assert_eq!(none["capture"]["attempted"], false);
    assert_eq!(none["kwin"], true);

    rig.state().capture_error = Some("NoAuthorized");
    let denied = probe(&rig, &[WIN]);
    assert_eq!(
        (&denied["capture"]["ok"], &denied["capture"]["error"]),
        (&json!(false), &json!("not_authorized"))
    );

    rig.state().capture_error = None;
    rig.add_window(OTHER, "keepassxc", "Probe Caption");
    let before = rig.state().capture_calls.len();
    let blocked = probe(&rig, &[OTHER]);
    assert_eq!(blocked["capture"]["attempted"], false);
    assert_eq!(
        rig.state().capture_calls.len(),
        before,
        "a blocked window is not captured even to measure it"
    );
}

#[test]
fn popups_are_composited_end_to_end() {
    let Some(mut rig) = Rig::start(Services::All) else {
        return;
    };
    rig.add_window(WIN, "blender", "Blender");
    rig.add_window(OTHER, "blender", "");
    {
        let mut state = rig.state();
        let popup = state.windows.get_mut(OTHER).unwrap();
        (popup.x, popup.y, popup.width, popup.height, popup.blue) = (300.0, 150.0, 50, 40, 222);
    }
    let (header, frames) = rig.capture(WIN, json!({"popups": [OTHER], "crop_size": 2048}));
    assert_eq!(
        (&header["status"], &header["popups"]),
        (&json!("ok"), &json!(1)),
        "{header}"
    );
    let image = image::load_from_memory(&frames[1]).unwrap().to_rgb8();
    assert_eq!(image.dimensions(), (640, 400));
    // Popup origin (300-100, 150-50) = (200, 100); its own pixel (0, 0) lands there.
    assert_eq!(image.get_pixel(200, 100).0, pixel(0, 0, 222));
    assert_eq!(image.get_pixel(249, 139).0, pixel(49, 39, 222));
    assert_eq!(image.get_pixel(199, 100).0, pixel(199, 100, 10));
    assert_eq!(image.get_pixel(250, 139).0, pixel(250, 139, 10));

    rig.state().windows.get_mut(OTHER).unwrap().class =
        "org.kde.polkit-kde-authentication-agent-1".into();
    let calls = rig.state().capture_calls.len();
    let (header, frames) = rig.capture(WIN, json!({"popups": [OTHER], "force": true}));
    assert_eq!(
        without_id(&header),
        blind("blocked"),
        "a blocked popup blinds the whole capture"
    );
    assert!(frames.is_empty());
    assert_eq!(
        rig.state().capture_calls.len(),
        calls,
        "and nothing was captured"
    );
}

// ---------------------------------------------------------------- structural

fn source_files(dir: &std::path::Path, out: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            source_files(&path, out);
        } else {
            out.push(path);
        }
    }
}

/// The helper must contain no call to any ScreenShot2 method other than `CaptureWindow`.
#[test]
fn source_tree_names_no_other_capture_method() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut files = vec![root.join("Cargo.toml")];
    source_files(&root.join("src"), &mut files);
    assert!(files.len() >= 10, "source tree not found");
    let forbidden = [
        "CaptureArea",
        "CaptureScreen",
        "CaptureWorkspace",
        "CaptureInteractive",
        "CaptureActive",
    ];
    let mut hits = Vec::new();
    let mut saw_capture_window = false;
    for path in &files {
        for (number, line) in fs::read_to_string(path).unwrap().lines().enumerate() {
            let code = line.split("//").next().unwrap_or("");
            saw_capture_window |= code.contains("\"CaptureWindow\"");
            if forbidden.iter().any(|name| code.contains(name)) {
                hits.push(format!("{}:{}", path.display(), number + 1));
            }
        }
    }
    assert!(
        saw_capture_window,
        "the scan must be looking at the code that calls KWin"
    );
    assert!(
        hits.is_empty(),
        "other ScreenShot2 methods referenced in code: {hits:?}"
    );
}

/// Every string literal in the source that looks like a ScreenShot2 method name is
/// `CaptureWindow`, so no spelling of another method can hide from the list above.
#[test]
fn the_only_capture_method_literal_is_capture_window() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut files = Vec::new();
    source_files(&root.join("src"), &mut files);
    let mut literals = Vec::new();
    for path in &files {
        let text = fs::read_to_string(path).unwrap();
        for (index, _) in text.match_indices("\"Capture") {
            let rest = &text[index + 1..];
            let end = rest
                .find(|c: char| !c.is_ascii_alphanumeric())
                .unwrap_or(rest.len());
            literals.push(rest[..end].to_string());
        }
        // Nothing assembles a method name at run time for the screenshot interface.
        assert!(
            !text.contains("concat!(\"Capture") && !text.contains("format!(\"Capture"),
            "{}",
            path.display()
        );
    }
    assert!(!literals.is_empty());
    assert!(
        literals.iter().all(|name| name == "CaptureWindow"),
        "{literals:?}"
    );
    // And the screenshot interface is addressed from exactly two places: the `Version`
    // property read and the one capture call.
    let kwin = fs::read_to_string(root.join("src/kwin.rs")).unwrap();
    let code: String = kwin
        .lines()
        .map(|line| line.split("//").next().unwrap_or(""))
        .collect::<Vec<_>>()
        .join("\n");
    assert_eq!(
        code.matches("SCREENSHOT_INTERFACE").count(),
        3,
        "definition + 2 uses"
    );
    assert_eq!(
        code.matches("SCREENSHOT_PATH").count(),
        3,
        "definition + 2 uses"
    );
    for path in &files {
        if path.file_name().is_some_and(|name| name != "kwin.rs") {
            let text = fs::read_to_string(path).unwrap();
            assert!(
                !text.contains("\"org.kde.KWin.ScreenShot2\"") && !text.contains("SCREENSHOT_"),
                "{} addresses the screenshot interface",
                path.display()
            );
        }
    }
}

/// Pixels are contained (I3): outside its tests, the helper's source has no way to create,
/// write, rename or copy a file, start a process, or print anything but fixed text.
#[test]
fn source_tree_never_writes_files_or_starts_processes() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut files = Vec::new();
    source_files(&root.join("src"), &mut files);
    assert!(files.len() >= 10, "source tree not found");
    for path in &files {
        let text = fs::read_to_string(path).unwrap();
        // Unit tests live at the end of each file and may use scratch files.
        let code = text.split("#[cfg(test)]").next().unwrap_or("");
        for needle in [
            "fs::write",
            "File::create",
            "create_new",
            "OpenOptions",
            "File::options",
            ".write(true)",
            ".append(true)",
            "fs::rename",
            "fs::copy",
            "hard_link",
            "symlink",
            "create_dir",
            "tempfile",
            "memfd",
            "Command::new",
            "process::Command",
            "dbg!(",
            "log::",
            "tracing::",
        ] {
            assert!(!code.contains(needle), "{} uses {needle}", path.display());
        }
        // Only `main.rs` talks to the terminal, and never about a window.
        let prints =
            code.contains("println!(") || code.contains("eprint!(") || code.contains("print!(");
        assert!(
            !prints || path.file_name().is_some_and(|name| name == "main.rs"),
            "{} prints",
            path.display()
        );
    }
}

/// The helper has no way to accept a connection: no listener type appears in its source.
#[test]
fn source_tree_never_listens() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut files = Vec::new();
    source_files(&root.join("src"), &mut files);
    for path in &files {
        let text = fs::read_to_string(path).unwrap();
        for needle in [
            "UnixListener",
            "TcpListener",
            "TcpStream",
            "UdpSocket",
            ".bind(",
        ] {
            assert!(
                !text.contains(needle),
                "{} mentions {needle}",
                path.display()
            );
        }
    }
}
