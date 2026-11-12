//! The real session: KWin over the session bus, logind over the system bus.
//!
//! The only capture call in this crate is `CaptureWindow` below, with options that are
//! constants of this file. Nothing a request contains can change them.

use std::collections::HashMap;
use std::io::Read;
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use zbus::connection::Builder;
use zbus::{Connection, DBusError, Fd, OwnedVariant, Variant};

use crate::desktop::{
    Desktop, DesktopError, Environment, LockState, RawCapture, Regime, WindowId, WindowInfo,
};
use crate::pixels::{self, RawMeta, MAX_RAW_BYTES};

pub const KWIN_SERVICE: &str = "org.kde.KWin";
pub const KWIN_PATH: &str = "/KWin";
pub const KWIN_INTERFACE: &str = "org.kde.KWin";
pub const SCREENSHOT_PATH: &str = "/org/kde/KWin/ScreenShot2";
pub const SCREENSHOT_INTERFACE: &str = "org.kde.KWin.ScreenShot2";
const LOGIN_SERVICE: &str = "org.freedesktop.login1";
const LOGIN_MANAGER_PATH: &str = "/org/freedesktop/login1";
const LOGIN_AUTO_SESSION: &str = "/org/freedesktop/login1/session/auto";
const PROPERTIES: &str = "org.freedesktop.DBus.Properties";

/// The capture options. Fixed: the cursor, the decoration and the shadow are never included,
/// and the image is always at native resolution.
pub const CAPTURE_OPTIONS: [(&str, bool); 4] = [
    ("include-cursor", false),
    ("include-decoration", false),
    ("include-shadow", false),
    ("native-resolution", true),
];

/// Every D-Bus call must be answered within this.
pub const CALL_TIMEOUT: Duration = Duration::from_secs(2);
/// After KWin's reply, the pixels must have arrived on the pipe within this.
pub const PIPE_TIMEOUT: Duration = Duration::from_secs(2);
/// How long to wait before trying an unreachable system bus again.
const SYSTEM_BUS_RETRY: Duration = Duration::from_secs(30);

type Dict = HashMap<String, OwnedVariant>;

#[derive(Default)]
pub struct KwinDesktop {
    session: Option<Connection>,
    system: Option<Connection>,
    system_tried: Option<Instant>,
    login_session: Option<String>,
}

/// Only the characters a D-Bus error name can have; anything else would be someone's text.
fn sanitize_name(name: &str) -> String {
    name.chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_'))
        .take(120)
        .collect()
}

fn classify_name(name: &str) -> DesktopError {
    const ABSENT: [&str; 5] = [
        "ServiceUnknown",
        "UnknownMethod",
        "UnknownObject",
        "UnknownInterface",
        "NameHasNoOwner",
    ];
    let last = name.rsplit('.').next().unwrap_or("");
    if name.starts_with("org.freedesktop.DBus.Error.") {
        if ABSENT.contains(&last) {
            return DesktopError::NoCapturePath;
        }
        if matches!(last, "NoReply" | "Timeout" | "TimedOut") {
            return DesktopError::Timeout;
        }
    }
    // KWin spells it "NoAuthorized".
    if matches!(last, "NoAuthorized" | "NotAuthorized") {
        return DesktopError::NotAuthorized;
    }
    DesktopError::Failed(sanitize_name(name))
}

/// Reduce a zbus error to something safe to report. The error *message* is dropped: it is
/// another process's text.
fn classify(error: &zbus::Error) -> DesktopError {
    match error {
        zbus::Error::MethodError(name, _, _) => classify_name(name.as_str()),
        zbus::Error::FDO(fdo) => classify_name(fdo.name().as_str()),
        zbus::Error::InputOutput(io) if io.kind() == std::io::ErrorKind::TimedOut => {
            DesktopError::Timeout
        }
        zbus::Error::InputOutput(_) | zbus::Error::Connection(..) => {
            DesktopError::Failed("ConnectionLost".into())
        }
        _ => DesktopError::BadReply("unexpected D-Bus reply"),
    }
}

fn is_method_level(error: &zbus::Error) -> bool {
    matches!(error, zbus::Error::MethodError(..) | zbus::Error::FDO(_))
}

fn text(dict: &Dict, key: &str) -> String {
    match dict.get(key).map(|v| &**v) {
        Some(Variant::Str(s)) => s.as_str().to_owned(),
        _ => String::new(),
    }
}

fn number(value: &Variant<'_>) -> Option<f64> {
    Some(match value {
        Variant::U8(v) => f64::from(*v),
        Variant::I16(v) => f64::from(*v),
        Variant::U16(v) => f64::from(*v),
        Variant::I32(v) => f64::from(*v),
        Variant::U32(v) => f64::from(*v),
        Variant::I64(v) => *v as f64,
        Variant::U64(v) => *v as f64,
        Variant::F64(v) => *v,
        Variant::Bool(v) => f64::from(u8::from(*v)),
        _ => return None,
    })
}

fn unsigned(dict: &Dict, key: &str) -> Option<u32> {
    let value = number(dict.get(key)?)?;
    (value >= 0.0 && value <= f64::from(u32::MAX) && value.fract() == 0.0).then_some(value as u32)
}

/// Whether a `getWindowInfo` reply carries the text the blocklist is matched on: a string
/// `resourceClass` and a string `caption` (either may be empty). KWin always sends both. A
/// reply without them - other key names, other types - cannot be gated, and reading the
/// absence as "no class, no title, nothing to block" would open the gate.
pub fn has_gate_fields(dict: &Dict) -> bool {
    ["resourceClass", "caption"]
        .iter()
        .all(|key| matches!(dict.get(*key).map(|v| &**v), Some(Variant::Str(_))))
}

/// Turn a `getWindowInfo` reply into a [`WindowInfo`]. Untrusted input: only the expected
/// types are read, everything else is ignored.
pub fn window_info_from(dict: &Dict) -> WindowInfo {
    let numbers = dict
        .iter()
        .filter_map(|(key, value)| Some((key.clone(), number(value)?)))
        .filter(|(key, value)| key.len() <= 64 && value.is_finite())
        .collect::<std::collections::BTreeMap<_, _>>();
    let coordinate = |key: &str| numbers.get(key).copied().unwrap_or(0.0);
    let mut keys: Vec<String> = dict.keys().filter(|k| k.len() <= 64).cloned().collect();
    keys.sort();
    WindowInfo {
        resource_class: text(dict, "resourceClass"),
        resource_name: text(dict, "resourceName"),
        desktop_file: text(dict, "desktopFile"),
        caption: text(dict, "caption"),
        uuid: WindowId::parse(&text(dict, "uuid")),
        x: coordinate("x"),
        y: coordinate("y"),
        width: coordinate("width"),
        height: coordinate("height"),
        numbers,
        keys,
    }
}

/// Which authorization regime a KWin version uses, from its support information text.
///
/// S2.1 (unverified): that `supportInformation` contains a "KWin version: X.Y.Z" line on
/// Plasma 6.6. When it does not, the answer is `Unknown`, never a guess.
pub fn regime_from_support_information(text: &str) -> Regime {
    let Some(version) = text
        .lines()
        .take(200)
        .find_map(|line| line.trim().strip_prefix("KWin version:"))
    else {
        return Regime::Unknown;
    };
    let mut parts = version.trim().split('.').map(|p| p.parse::<u32>().ok());
    match (parts.next().flatten(), parts.next().flatten()) {
        (Some(6), Some(4..=7)) => Regime::Restricted,
        (Some(6), Some(minor)) if minor >= 8 => Regime::Open,
        (Some(major), Some(_)) if major >= 7 => Regime::Open,
        _ => Regime::Unknown,
    }
}

impl KwinDesktop {
    pub fn new() -> Self {
        Self::default()
    }

    fn session(&mut self) -> Result<Connection, DesktopError> {
        if let Some(connection) = &self.session {
            return Ok(connection.clone());
        }
        let connection = zbus::block_on(Builder::session().method_timeout(CALL_TIMEOUT).build())
            .map_err(|_| DesktopError::NoCapturePath)?;
        self.session = Some(connection.clone());
        Ok(connection)
    }

    /// Forget a connection that failed below the method level, so the next call reconnects.
    fn note_session_error(&mut self, error: &zbus::Error) {
        if !is_method_level(error) {
            self.session = None;
        }
    }

    fn system(&mut self) -> Option<Connection> {
        if let Some(connection) = &self.system {
            return Some(connection.clone());
        }
        if self
            .system_tried
            .is_some_and(|at| at.elapsed() < SYSTEM_BUS_RETRY)
        {
            return None;
        }
        self.system_tried = Some(Instant::now());
        let connection =
            zbus::block_on(Builder::system().method_timeout(CALL_TIMEOUT).build()).ok()?;
        self.system = Some(connection.clone());
        Some(connection)
    }

    /// The logind session object to ask. S2.5 (unverified): that `session/auto`, or the
    /// session named by `XDG_SESSION_ID`, is the graphical session whose `LockedHint` Plasma
    /// sets when the screen locks.
    fn login_session(&mut self, system: &Connection) -> String {
        if let Some(path) = &self.login_session {
            return path.clone();
        }
        let named = std::env::var("XDG_SESSION_ID")
            .ok()
            .filter(|id| {
                !id.is_empty()
                    && id.len() <= 64
                    && id.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
            })
            .and_then(|id| {
                let reply = zbus::block_on(system.call_method(
                    Some(LOGIN_SERVICE),
                    LOGIN_MANAGER_PATH,
                    Some("org.freedesktop.login1.Manager"),
                    "GetSession",
                    &(id.as_str(),),
                ))
                .ok()?;
                let path: zbus::OwnedObjectPath = reply.body().deserialize().ok()?;
                Some(path.as_str().to_owned())
            });
        let path = named.unwrap_or_else(|| LOGIN_AUTO_SESSION.to_owned());
        self.login_session = Some(path.clone());
        path
    }

    fn get_property(
        connection: &Connection,
        service: &str,
        path: &str,
        interface: &str,
        name: &str,
    ) -> zbus::Result<OwnedVariant> {
        let reply = zbus::block_on(connection.call_method(
            Some(service),
            path,
            Some(PROPERTIES),
            "Get",
            &(interface, name),
        ))?;
        reply.body().deserialize::<OwnedVariant>()
    }
}

impl Desktop for KwinDesktop {
    fn environment(&mut self) -> Environment {
        let absent = Environment {
            kwin: false,
            screenshot2_version: None,
            regime: Regime::Unknown,
        };
        let Ok(connection) = self.session() else {
            return absent;
        };
        let owned = zbus::block_on(connection.call_method(
            Some("org.freedesktop.DBus"),
            "/org/freedesktop/DBus",
            Some("org.freedesktop.DBus"),
            "NameHasOwner",
            &(KWIN_SERVICE,),
        ))
        .and_then(|reply| reply.body().deserialize::<bool>());
        match owned {
            Ok(true) => {}
            Ok(false) => return absent,
            Err(e) => {
                self.note_session_error(&e);
                return absent;
            }
        }
        let version = Self::get_property(
            &connection,
            KWIN_SERVICE,
            SCREENSHOT_PATH,
            SCREENSHOT_INTERFACE,
            "Version",
        )
        .ok()
        .and_then(|v| number(&v))
        .filter(|v| (0.0..=1e6).contains(v))
        .map(|v| v as u32);
        let regime = if version.is_some() {
            zbus::block_on(connection.call_method(
                Some(KWIN_SERVICE),
                KWIN_PATH,
                Some(KWIN_INTERFACE),
                "supportInformation",
                &(),
            ))
            .and_then(|reply| reply.body().deserialize::<String>())
            .map_or(Regime::Unknown, |text| {
                regime_from_support_information(&text)
            })
        } else {
            Regime::Unknown
        };
        Environment {
            kwin: true,
            screenshot2_version: version,
            regime,
        }
    }

    fn lock_state(&mut self) -> LockState {
        let Some(system) = self.system() else {
            return LockState::Unknown;
        };
        let path = self.login_session(&system);
        let locked = Self::get_property(
            &system,
            LOGIN_SERVICE,
            &path,
            "org.freedesktop.login1.Session",
            "LockedHint",
        );
        match locked.as_ref().map(|v| &**v) {
            Ok(Variant::Bool(true)) => LockState::Locked,
            Ok(Variant::Bool(false)) => LockState::Unlocked,
            Ok(_) => LockState::Unknown,
            Err(e) => {
                if !is_method_level(e) {
                    self.system = None;
                }
                self.login_session = None;
                LockState::Unknown
            }
        }
    }

    fn window_info(&mut self, id: &WindowId) -> Result<Option<WindowInfo>, DesktopError> {
        let connection = self.session()?;
        // KWin prints window ids in QUuid's braced form and parses both.
        let handle = format!("{{{}}}", id.as_str());
        let reply = zbus::block_on(connection.call_method(
            Some(KWIN_SERVICE),
            KWIN_PATH,
            Some(KWIN_INTERFACE),
            "getWindowInfo",
            &(handle.as_str(),),
        ));
        let reply = match reply {
            Ok(reply) => reply,
            Err(e) => {
                self.note_session_error(&e);
                return Err(classify(&e));
            }
        };
        let dict: Dict = reply
            .body()
            .deserialize()
            .map_err(|_| DesktopError::BadReply("getWindowInfo did not return a{sv}"))?;
        // KWin answers with an empty map when it knows no such window.
        if dict.is_empty() {
            return Ok(None);
        }
        if !has_gate_fields(&dict) {
            return Err(DesktopError::BadReply(
                "getWindowInfo did not describe the window's class and caption",
            ));
        }
        Ok(Some(window_info_from(&dict)))
    }

    fn capture_window(&mut self, id: &WindowId) -> Result<RawCapture, DesktopError> {
        let connection = self.session()?;
        let (reader, writer) =
            std::io::pipe().map_err(|_| DesktopError::Failed("PipeCreationFailed".into()))?;

        // Drain the pipe on a thread from the start: KWin may write before or after it
        // replies, and a pipe only buffers 64 KiB. The read is bounded, and it ends when
        // every write end is closed (ours below, KWin's when it is done).
        let (sender, receiver) = mpsc::channel();
        thread::spawn(move || {
            let mut data = Vec::new();
            let outcome = reader.take(MAX_RAW_BYTES + 1).read_to_end(&mut data);
            let _ = sender.send(outcome.map(|_| data));
        });

        let handle = format!("{{{}}}", id.as_str());
        let options: HashMap<&str, Variant<'_>> = CAPTURE_OPTIONS
            .iter()
            .map(|(key, value)| (*key, Variant::Bool(*value)))
            .collect();
        let reply = zbus::block_on(connection.call_method(
            Some(KWIN_SERVICE),
            SCREENSHOT_PATH,
            Some(SCREENSHOT_INTERFACE),
            "CaptureWindow",
            &(handle.as_str(), options, Fd::from(&writer)),
        ));
        drop(writer);
        let reply = match reply {
            Ok(reply) => reply,
            Err(e) => {
                self.note_session_error(&e);
                return Err(classify(&e));
            }
        };
        let dict: Dict = reply
            .body()
            .deserialize()
            .map_err(|_| DesktopError::BadReply("CaptureWindow did not return a{sv}"))?;
        drop(reply);

        if dict.contains_key("type") && text(&dict, "type") != "raw" {
            return Err(DesktopError::BadReply("capture result is not raw pixels"));
        }
        let field = |key| {
            unsigned(&dict, key).ok_or(DesktopError::BadReply("capture metadata is incomplete"))
        };
        let meta = RawMeta {
            width: field("width")?,
            height: field("height")?,
            stride: field("stride")?,
            format: field("format")?,
        };
        let scale = dict.get("scale").and_then(|v| number(v)).unwrap_or(1.0);
        let window_id = dict
            .contains_key("windowId")
            .then(|| text(&dict, "windowId"));
        let expected = pixels::expected_len(&meta).map_err(DesktopError::Pixels)?;

        let data = match receiver.recv_timeout(PIPE_TIMEOUT) {
            Ok(Ok(data)) => data,
            Ok(Err(_)) => return Err(DesktopError::Failed("PipeReadFailed".into())),
            Err(_) => return Err(DesktopError::Timeout),
        };
        if data.len() != expected {
            return Err(DesktopError::BadReply(
                "pixel data does not match stride * height",
            ));
        }
        Ok(RawCapture {
            meta,
            scale,
            window_id,
            data,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dict(entries: Vec<(&str, Variant<'static>)>) -> Dict {
        entries
            .into_iter()
            .map(|(k, v)| (k.to_owned(), OwnedVariant::try_from(v).unwrap()))
            .collect()
    }

    #[test]
    fn the_capture_options_are_exactly_the_fixed_four() {
        let mut options = CAPTURE_OPTIONS.to_vec();
        options.sort();
        assert_eq!(
            options,
            [
                ("include-cursor", false),
                ("include-decoration", false),
                ("include-shadow", false),
                ("native-resolution", true),
            ]
        );
    }

    #[test]
    fn window_info_reads_only_well_typed_entries() {
        let info = window_info_from(&dict(vec![
            ("resourceClass", Variant::from("org.kde.kate")),
            ("resourceName", Variant::from("kate")),
            ("desktopFile", Variant::from("org.kde.kate")),
            ("caption", Variant::from("notes.txt - Kate")),
            (
                "uuid",
                Variant::from("{0A1B2C3D-0000-4000-8000-00000000ABCD}"),
            ),
            ("x", Variant::F64(10.5)),
            ("y", Variant::I32(-20)),
            ("width", Variant::U32(800)),
            ("height", Variant::F64(600.0)),
            ("fullscreen", Variant::Bool(true)),
        ]));
        assert_eq!(info.resource_class, "org.kde.kate");
        assert_eq!(info.caption, "notes.txt - Kate");
        assert_eq!(
            info.uuid.unwrap().as_str(),
            "0a1b2c3d-0000-4000-8000-00000000abcd"
        );
        assert_eq!(
            (info.x, info.y, info.width, info.height),
            (10.5, -20.0, 800.0, 600.0)
        );
        assert_eq!(info.numbers["fullscreen"], 1.0);
        assert!(!info.numbers.contains_key("caption"));
        assert!(info.keys.contains(&"caption".to_string()), "key names only");
    }

    #[test]
    fn window_info_tolerates_wrong_types() {
        let info = window_info_from(&dict(vec![
            ("resourceClass", Variant::U32(7)),
            ("caption", Variant::Bool(true)),
            ("uuid", Variant::from("nonsense")),
            ("x", Variant::from("12")),
            ("width", Variant::F64(f64::NAN)),
        ]));
        assert_eq!(
            (info.resource_class.as_str(), info.caption.as_str()),
            ("", "")
        );
        assert!(info.uuid.is_none());
        assert_eq!((info.x, info.width), (0.0, 0.0));
    }

    /// A reply the blocklist cannot be evaluated on must not read as "nothing to block".
    #[test]
    fn a_reply_without_class_and_caption_text_is_not_a_window_description() {
        let complete = dict(vec![
            ("resourceClass", Variant::from("org.kde.kate")),
            ("caption", Variant::from("")),
            ("x", Variant::F64(1.0)),
        ]);
        assert!(has_gate_fields(&complete), "empty strings are fine");
        for broken in [
            dict(vec![("x", Variant::F64(1.0))]),
            dict(vec![("resourceClass", Variant::from("kate"))]),
            dict(vec![("caption", Variant::from("Sign in"))]),
            dict(vec![
                ("resourceClass", Variant::from("kate")),
                ("caption", Variant::U32(3)),
            ]),
            dict(vec![
                ("resourceClass", Variant::Bool(true)),
                ("caption", Variant::from("Sign in")),
            ]),
            dict(vec![
                ("windowClass", Variant::from("keepassxc")),
                ("title", Variant::from("Passwords")),
            ]),
        ] {
            assert!(!has_gate_fields(&broken));
        }
    }

    #[test]
    fn unsigned_fields_reject_negative_fractional_and_huge_values() {
        let d = dict(vec![
            ("a", Variant::U32(1920)),
            ("b", Variant::I32(-1)),
            ("c", Variant::F64(10.5)),
            ("d", Variant::U64(u64::MAX)),
            ("e", Variant::I64(1080)),
            ("f", Variant::from("5")),
        ]);
        assert_eq!(unsigned(&d, "a"), Some(1920));
        assert_eq!(unsigned(&d, "e"), Some(1080));
        for key in ["b", "c", "d", "f", "missing"] {
            assert_eq!(unsigned(&d, key), None, "{key}");
        }
    }

    #[test]
    fn regime_follows_the_kwin_version() {
        let info = |v: &str| {
            format!("KWin Support Information:\n\nVersion\n=======\nKWin version: {v}\nQt Version: 6.10.2\n")
        };
        assert_eq!(
            regime_from_support_information(&info("6.6.4")),
            Regime::Restricted
        );
        assert_eq!(
            regime_from_support_information(&info("6.4.0")),
            Regime::Restricted
        );
        assert_eq!(
            regime_from_support_information(&info("6.7.90")),
            Regime::Restricted
        );
        assert_eq!(
            regime_from_support_information(&info("6.8.0")),
            Regime::Open
        );
        assert_eq!(
            regime_from_support_information(&info("7.0.1")),
            Regime::Open
        );
        assert_eq!(
            regime_from_support_information(&info("6.3.5")),
            Regime::Unknown
        );
        assert_eq!(
            regime_from_support_information(&info("garbage")),
            Regime::Unknown
        );
        assert_eq!(
            regime_from_support_information("no version here"),
            Regime::Unknown
        );
    }

    #[test]
    fn error_names_are_classified_and_sanitized() {
        assert_eq!(
            classify_name("org.kde.KWin.ScreenShot2.Error.NoAuthorized"),
            DesktopError::NotAuthorized
        );
        assert_eq!(
            classify_name("org.freedesktop.DBus.Error.ServiceUnknown"),
            DesktopError::NoCapturePath
        );
        assert_eq!(
            classify_name("org.freedesktop.DBus.Error.UnknownMethod"),
            DesktopError::NoCapturePath
        );
        assert_eq!(
            classify_name("org.freedesktop.DBus.Error.NoReply"),
            DesktopError::Timeout
        );
        assert_eq!(
            classify_name("org.kde.KWin.ScreenShot2.Error.Cancelled"),
            DesktopError::Failed("org.kde.KWin.ScreenShot2.Error.Cancelled".into())
        );
        assert_eq!(sanitize_name("a.b c\n<script>"), "a.bcscript");
    }
}
