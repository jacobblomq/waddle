//! The request loop over the inherited socket.

use std::io::Write;
use std::os::unix::net::UnixStream;
use std::time::Duration;

use crate::desktop::Desktop;
use crate::framing::{read_frame, write_frame, FrameError};
use crate::session::Session;

/// Idle time after which the helper exits when the parent sends nothing.
pub const DEFAULT_IDLE_TIMEOUT: Duration = Duration::from_secs(120);
/// A parent that stops reading replies for this long is gone.
const WRITE_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum Exit {
    /// The parent closed the socket.
    Closed,
    Idle,
    /// The parent broke the framing rules; the stream cannot be trusted any further.
    ProtocolViolation,
    Io,
}

/// Serve requests until the socket closes, the idle timeout passes, or the framing breaks.
/// One request is handled at a time, in order.
pub fn serve<D: Desktop>(mut stream: UnixStream, idle: Duration, session: &mut Session<D>) -> Exit {
    if stream.set_read_timeout(Some(idle)).is_err()
        || stream.set_write_timeout(Some(WRITE_TIMEOUT)).is_err()
    {
        return Exit::Io;
    }
    loop {
        let payload = match read_frame(&mut stream) {
            Ok(payload) => payload,
            Err(FrameError::Closed) => return Exit::Closed,
            Err(FrameError::Idle) => return Exit::Idle,
            Err(FrameError::TooLarge(_) | FrameError::Truncated) => return Exit::ProtocolViolation,
            Err(FrameError::Io(_)) => return Exit::Io,
        };
        let (id, reply) = session.handle(&payload);
        drop(payload);
        let mut sent = write_frame(&mut stream, &reply.header_bytes(id));
        for frame in &reply.frames {
            sent = sent.and_then(|()| write_frame(&mut stream, frame));
        }
        if sent.and_then(|()| stream.flush()).is_err() {
            return Exit::Io;
        }
    }
}
