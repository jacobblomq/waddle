//! Portal ScreenCast fallback - NOT IMPLEMENTED.
//!
//! ARCHITECTURE §4.3 describes a weaker fallback for sessions without
//! `org.kde.KWin.ScreenShot2`: an `org.freedesktop.portal.ScreenCast` stream with a *window*
//! source that the user picks (never a monitor source), stopped on blind and whenever the
//! target differs. It needs PipeWire and spike S2.6 first, and is deliberately left out of
//! this build.
//!
//! Until it exists, a capture on a session without ScreenShot2 fails with the error code
//! below, and `hello` reports `screenshot2_version: null`.

/// The error code a capture returns when no capture path is available.
pub const NO_CAPTURE_PATH: &str = "no_capture_path";

/// Whether the portal fallback is compiled in. It is not.
pub const AVAILABLE: bool = false;
