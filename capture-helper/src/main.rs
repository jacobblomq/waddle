use std::os::fd::FromRawFd;
use std::os::unix::fs::FileTypeExt;
use std::os::unix::net::UnixStream;
use std::process::ExitCode;
use std::time::Duration;

use waddle_capture::blocklist::{config_path, Blocklist};
use waddle_capture::kwin::KwinDesktop;
use waddle_capture::server::{serve, Exit, DEFAULT_IDLE_TIMEOUT};
use waddle_capture::session::Session;
use waddle_capture::{probe, VERSION};

const USAGE: &str = "usage: waddle-capture [--fd N] [--idle-timeout SECONDS]
       waddle-capture --probe [WINDOW-UUID]
       waddle-capture --version

Waddle's capture helper. It is started by waddle-desktop with one end of a
socketpair as file descriptor 3 and serves single-window captures over it.";

/// Exit status for "not started the way this program must be started".
const EXIT_USAGE: u8 = 2;
const EXIT_PROTOCOL: u8 = 3;

enum Mode {
    Serve { fd: i32, idle: Duration },
    Probe(Option<String>),
    Version,
}

fn parse_args(args: &[String]) -> Result<Mode, String> {
    let mut fd = 3;
    let mut idle = DEFAULT_IDLE_TIMEOUT;
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--version" | "-V" => return Ok(Mode::Version),
            "--probe" => {
                let uuid = iter.next().cloned();
                if iter.next().is_some() {
                    return Err("--probe takes at most one window uuid".into());
                }
                return Ok(Mode::Probe(uuid));
            }
            "--fd" => {
                fd = iter
                    .next()
                    .and_then(|v| v.parse::<i32>().ok())
                    .filter(|n| (3..=1_048_576).contains(n))
                    .ok_or("--fd needs a descriptor number of 3 or more")?;
            }
            "--idle-timeout" => {
                idle = iter
                    .next()
                    .and_then(|v| v.parse::<u64>().ok())
                    .filter(|s| (1..=86_400).contains(s))
                    .map(Duration::from_secs)
                    .ok_or("--idle-timeout needs a number of seconds between 1 and 86400")?;
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    Ok(Mode::Serve { fd, idle })
}

/// Take ownership of the inherited socket, or explain why there is none.
fn inherited_socket(fd: i32) -> Result<UnixStream, String> {
    let is_socket = std::fs::metadata(format!("/proc/self/fd/{fd}"))
        .map(|meta| meta.file_type().is_socket())
        .unwrap_or(false);
    if !is_socket {
        return Err(format!(
            "file descriptor {fd} is not a socket. waddle-capture is started by waddle-desktop \
             with a socketpair on fd {fd}; it does not listen on anything and cannot be used \
             on its own (see --probe and --version)."
        ));
    }
    // SAFETY: this is the one unavoidable `unsafe` in the crate. A descriptor inherited from
    // the parent can only be adopted through `from_raw_fd`. It was just verified to be an
    // open socket; this runs once, before any thread exists and before anything else in the
    // process could have opened or could own descriptor `fd`, so ownership is unique.
    let stream = unsafe { UnixStream::from_raw_fd(fd) };
    if stream.peer_addr().is_err() {
        return Err(format!(
            "file descriptor {fd} is not a connected Unix stream socket"
        ));
    }
    Ok(stream)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mode = match parse_args(&args) {
        Ok(mode) => mode,
        Err(message) => {
            eprintln!("waddle-capture: {message}\n{USAGE}");
            return ExitCode::from(EXIT_USAGE);
        }
    };
    match mode {
        Mode::Version => {
            println!("waddle-capture {VERSION}");
            ExitCode::SUCCESS
        }
        Mode::Probe(uuid) => {
            let report = probe::report(&mut KwinDesktop::new(), uuid.as_deref());
            println!(
                "{}",
                serde_json::to_string_pretty(&report).unwrap_or_default()
            );
            ExitCode::SUCCESS
        }
        Mode::Serve { fd, idle } => {
            let stream = match inherited_socket(fd) {
                Ok(stream) => stream,
                Err(message) => {
                    eprintln!("waddle-capture: {message}");
                    return ExitCode::from(EXIT_USAGE);
                }
            };
            let mut session = Session::new(KwinDesktop::new(), Blocklist::new(config_path()));
            match serve(stream, idle, &mut session) {
                Exit::Closed | Exit::Idle => ExitCode::SUCCESS,
                Exit::ProtocolViolation => {
                    eprintln!("waddle-capture: framing violation on the control socket, exiting");
                    ExitCode::from(EXIT_PROTOCOL)
                }
                Exit::Io => ExitCode::FAILURE,
            }
        }
    }
}
