//! Length-prefixed frames: a big-endian `u32` payload length, then the payload.

use std::io::{self, ErrorKind, Read, Write};

/// Largest payload either side may send. Enforced on read and on write.
pub const MAX_FRAME: usize = 16 * 1024 * 1024;

#[derive(Debug)]
pub enum FrameError {
    /// The peer closed the stream cleanly between frames.
    Closed,
    /// The read timed out before the first byte of a frame arrived.
    Idle,
    /// The stream ended, or stalled past the timeout, in the middle of a frame.
    Truncated,
    /// The announced length exceeds [`MAX_FRAME`].
    TooLarge(u64),
    Io(io::Error),
}

fn is_timeout(error: &io::Error) -> bool {
    matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut)
}

/// Fill `buf`. `Ok(false)` means end-of-stream before the first byte.
fn read_full<R: Read>(
    reader: &mut R,
    buf: &mut [u8],
    at_boundary: bool,
) -> Result<bool, FrameError> {
    let mut filled = 0;
    while filled < buf.len() {
        match reader.read(&mut buf[filled..]) {
            Ok(0) if filled == 0 && at_boundary => return Ok(false),
            Ok(0) => return Err(FrameError::Truncated),
            Ok(n) => filled += n,
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            Err(e) if is_timeout(&e) && filled == 0 && at_boundary => return Err(FrameError::Idle),
            Err(e) if is_timeout(&e) => return Err(FrameError::Truncated),
            Err(e) => return Err(FrameError::Io(e)),
        }
    }
    Ok(true)
}

/// Read one frame. The length is checked before anything is allocated.
pub fn read_frame<R: Read>(reader: &mut R) -> Result<Vec<u8>, FrameError> {
    let mut header = [0u8; 4];
    if !read_full(reader, &mut header, true)? {
        return Err(FrameError::Closed);
    }
    let len = u32::from_be_bytes(header) as usize;
    if len > MAX_FRAME {
        return Err(FrameError::TooLarge(len as u64));
    }
    let mut payload = vec![0u8; len];
    read_full(reader, &mut payload, false)?;
    Ok(payload)
}

/// Write one frame. Refuses payloads above [`MAX_FRAME`] without writing anything.
pub fn write_frame<W: Write>(writer: &mut W, payload: &[u8]) -> io::Result<()> {
    if payload.len() > MAX_FRAME {
        return Err(io::Error::new(
            ErrorKind::InvalidInput,
            "frame exceeds MAX_FRAME",
        ));
    }
    writer.write_all(&(payload.len() as u32).to_be_bytes())?;
    writer.write_all(payload)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn round_trips_frames_back_to_back() {
        let mut wire = Vec::new();
        write_frame(&mut wire, b"{\"id\":1}").unwrap();
        write_frame(&mut wire, b"").unwrap();
        write_frame(&mut wire, &[7u8; 300]).unwrap();
        assert_eq!(&wire[..4], &[0, 0, 0, 8], "length prefix is big-endian u32");
        let mut cursor = Cursor::new(wire);
        assert_eq!(read_frame(&mut cursor).unwrap(), b"{\"id\":1}");
        assert_eq!(read_frame(&mut cursor).unwrap(), b"");
        assert_eq!(read_frame(&mut cursor).unwrap(), vec![7u8; 300]);
        assert!(matches!(read_frame(&mut cursor), Err(FrameError::Closed)));
    }

    #[test]
    fn rejects_an_oversize_length_before_reading_the_payload() {
        let mut wire = ((MAX_FRAME as u32) + 1).to_be_bytes().to_vec();
        wire.extend_from_slice(b"junk");
        assert!(matches!(
            read_frame(&mut Cursor::new(wire)),
            Err(FrameError::TooLarge(n)) if n == MAX_FRAME as u64 + 1
        ));
    }

    #[test]
    fn accepts_exactly_the_maximum() {
        let mut wire = Vec::new();
        write_frame(&mut wire, &vec![1u8; MAX_FRAME]).unwrap();
        assert_eq!(read_frame(&mut Cursor::new(wire)).unwrap().len(), MAX_FRAME);
    }

    #[test]
    fn refuses_to_write_an_oversize_frame() {
        let mut wire = Vec::new();
        assert!(write_frame(&mut wire, &vec![0u8; MAX_FRAME + 1]).is_err());
        assert!(wire.is_empty(), "nothing may reach the wire");
    }

    #[test]
    fn truncated_header_and_payload_are_errors_not_clean_closes() {
        assert!(matches!(
            read_frame(&mut Cursor::new(vec![0u8, 0])),
            Err(FrameError::Truncated)
        ));
        let mut wire = 10u32.to_be_bytes().to_vec();
        wire.extend_from_slice(b"short");
        assert!(matches!(
            read_frame(&mut Cursor::new(wire)),
            Err(FrameError::Truncated)
        ));
    }

    struct TimesOutAfter(Vec<u8>);
    impl Read for TimesOutAfter {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            if self.0.is_empty() {
                return Err(io::Error::from(ErrorKind::WouldBlock));
            }
            let n = buf.len().min(self.0.len());
            buf[..n].copy_from_slice(&self.0[..n]);
            self.0.drain(..n);
            Ok(n)
        }
    }

    #[test]
    fn timeout_between_frames_is_idle_but_inside_a_frame_is_truncation() {
        assert!(matches!(
            read_frame(&mut TimesOutAfter(vec![])),
            Err(FrameError::Idle)
        ));
        assert!(matches!(
            read_frame(&mut TimesOutAfter(vec![0, 0])),
            Err(FrameError::Truncated)
        ));
        let mut partial = 4u32.to_be_bytes().to_vec();
        partial.push(1);
        assert!(matches!(
            read_frame(&mut TimesOutAfter(partial)),
            Err(FrameError::Truncated)
        ));
    }
}
