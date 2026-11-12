//! `waddle-capture`: Waddle's gated, single-window capture helper.
//!
//! KWin authorizes `org.kde.KWin.ScreenShot2` callers by executable path, so capture lives in
//! this dedicated binary. It is strictly read-only: it asks KWin for the pixels of **one
//! window at a time**, never an area, an output or the whole desktop, and it enforces the
//! privacy gate (latch, lock, blocklist) itself, before and after every capture. Raw pixels
//! never leave the process and nothing is ever written to disk.
//!
//! The wire protocol to the parent process is specified in `PROTOCOL.md`.

pub mod blocklist;
pub mod change;
pub mod desktop;
pub mod framing;
pub mod kwin;
pub mod pixels;
pub mod portal;
pub mod probe;
pub mod protocol;
pub mod server;
pub mod session;

/// The helper's own version, reported by `hello` and `--version`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
