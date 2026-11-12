//! What the helper needs from the session, as a trait so the gate can be tested without one.

use std::collections::BTreeMap;
use std::fmt;

use crate::pixels::{PixelError, RawMeta};

/// A KWin window UUID, normalized to lowercase without braces.
///
/// KWin prints `QUuid`s as `{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}` and accepts both forms.
#[derive(Clone, PartialEq, Eq, Hash, Debug)]
pub struct WindowId(String);

impl WindowId {
    pub fn parse(text: &str) -> Option<Self> {
        let inner = match text.strip_prefix('{') {
            Some(rest) => rest.strip_suffix('}')?,
            None => text,
        };
        let bytes = inner.as_bytes();
        if bytes.len() != 36 {
            return None;
        }
        for (i, b) in bytes.iter().enumerate() {
            let ok = match i {
                8 | 13 | 18 | 23 => *b == b'-',
                _ => b.is_ascii_hexdigit(),
            };
            if !ok {
                return None;
            }
        }
        Some(Self(inner.to_ascii_lowercase()))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// KWin's own description of a window (`org.kde.KWin.getWindowInfo`).
///
/// Every string in here is untrusted and private: it is matched against the blocklist and
/// otherwise never stored, logged or sent anywhere. `Debug` deliberately prints none of it.
#[derive(Clone, Default)]
pub struct WindowInfo {
    pub resource_class: String,
    pub resource_name: String,
    pub desktop_file: String,
    pub caption: String,
    /// The `uuid` key of the reply, when present and well-formed.
    pub uuid: Option<WindowId>,
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
    /// Numeric and boolean entries of the reply by key, for `--probe`. No strings.
    pub numbers: BTreeMap<String, f64>,
    /// Every key of the reply (names only), for `--probe`.
    pub keys: Vec<String>,
}

impl fmt::Debug for WindowInfo {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("WindowInfo")
            .field("uuid", &self.uuid)
            .finish_non_exhaustive()
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum LockState {
    Locked,
    Unlocked,
    /// logind could not be asked. Treated as not locked, and reported by `hello`.
    Unknown,
}

/// One raw window image as KWin delivered it. Lives only inside the helper.
pub struct RawCapture {
    pub meta: RawMeta,
    pub scale: f64,
    /// `windowId` from the reply metadata, as sent.
    pub window_id: Option<String>,
    pub data: Vec<u8>,
}

#[derive(Clone, PartialEq, Eq, Debug)]
pub enum DesktopError {
    /// The session bus, KWin or its ScreenShot2 interface is not there at all.
    NoCapturePath,
    /// KWin refused this executable (the `.desktop` authorization is missing or shadowed).
    NotAuthorized,
    Timeout,
    /// KWin answered with something this helper does not understand.
    BadReply(&'static str),
    /// The reply metadata describes an image this helper will not read.
    Pixels(PixelError),
    /// Any other D-Bus error; carries the sanitized error *name* only, never its message.
    Failed(String),
}

impl DesktopError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::NoCapturePath => "no_capture_path",
            Self::NotAuthorized => "not_authorized",
            Self::Timeout => "timeout",
            Self::BadReply(_) => "bad_reply",
            Self::Pixels(e) => e.code(),
            Self::Failed(_) => "capture_failed",
        }
    }

    /// A message that is safe to forward: static text or a sanitized D-Bus error name.
    pub fn message(&self) -> String {
        match self {
            Self::NoCapturePath => "org.kde.KWin.ScreenShot2 is not available".into(),
            Self::NotAuthorized => "KWin did not authorize this executable for ScreenShot2".into(),
            Self::Timeout => "KWin did not answer in time".into(),
            Self::BadReply(what) => (*what).into(),
            Self::Pixels(e) => e.message(),
            Self::Failed(name) => name.clone(),
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Regime {
    /// Plasma 6.4-6.7: callers are authorized by executable path.
    Restricted,
    /// Plasma 6.8+: any session process may call ScreenShot2.
    Open,
    Unknown,
}

impl Regime {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Restricted => "restricted",
            Self::Open => "open",
            Self::Unknown => "unknown",
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Environment {
    pub kwin: bool,
    pub screenshot2_version: Option<u32>,
    pub regime: Regime,
}

/// The session as the helper sees it. There is deliberately no method for capturing anything
/// other than one window by id.
pub trait Desktop {
    fn environment(&mut self) -> Environment;
    fn lock_state(&mut self) -> LockState;
    /// `Ok(None)` when KWin knows no such window.
    fn window_info(&mut self, id: &WindowId) -> Result<Option<WindowInfo>, DesktopError>;
    fn capture_window(&mut self, id: &WindowId) -> Result<RawCapture, DesktopError>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn window_ids_normalize_braces_and_case() {
        let plain = WindowId::parse("0A1b2c3d-0000-4000-8000-00000000abcd").unwrap();
        let braced = WindowId::parse("{0a1b2c3d-0000-4000-8000-00000000ABCD}").unwrap();
        assert_eq!(plain, braced);
        assert_eq!(plain.as_str(), "0a1b2c3d-0000-4000-8000-00000000abcd");
    }

    #[test]
    fn malformed_window_ids_are_rejected() {
        for bad in [
            "",
            "active",
            "0a1b2c3d-0000-4000-8000-00000000abc",
            "0a1b2c3d-0000-4000-8000-00000000abcde",
            "0a1b2c3d00000-4000-8000-00000000abcd",
            "{0a1b2c3d-0000-4000-8000-00000000abcd",
            "0a1b2c3d-0000-4000-8000-00000000abcd}",
            "0a1b2c3d-0000-4000-8000-00000000abcg",
            "0a1b2c3d-0000-4000-8000-00000000abcd\n",
        ] {
            assert!(WindowId::parse(bad).is_none(), "{bad:?} must not parse");
        }
    }

    #[test]
    fn debug_output_never_contains_window_text() {
        let info = WindowInfo {
            caption: "Secret title".into(),
            resource_class: "secretapp".into(),
            ..WindowInfo::default()
        };
        let text = format!("{info:?}");
        assert!(
            !text.contains("Secret") && !text.contains("secretapp"),
            "{text}"
        );
    }
}
