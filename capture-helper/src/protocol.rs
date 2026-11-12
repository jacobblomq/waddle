//! Requests and replies exchanged with the parent process. See `PROTOCOL.md`.

use serde::Deserialize;
use serde_json::{json, Map, Value};

use crate::desktop::WindowId;

pub const PROTOCOL_VERSION: u32 = 1;

pub const DEFAULT_OVERVIEW_MAX: u32 = 1024;
pub const MIN_OVERVIEW_MAX: u32 = 64;
pub const MAX_OVERVIEW_MAX: u32 = 4096;
pub const DEFAULT_CROP_SIZE: u32 = 512;
pub const MIN_CROP_SIZE: u32 = 16;
/// 2048 x 2048 RGB is 12 MiB raw, so the PNG always fits one frame.
pub const MAX_CROP_SIZE: u32 = 2048;
pub const MAX_POPUPS: usize = 8;
pub const MAX_LIVE_REGIONS: usize = 64;
/// Largest magnitude accepted for any window-logical coordinate.
pub const MAX_COORD: f64 = 1_000_000.0;

/// A rectangle in window-logical coordinates.
#[derive(Clone, Copy, Debug, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

/// A point in window-logical coordinates.
#[derive(Clone, Copy, Debug, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Point {
    pub x: f64,
    pub y: f64,
}

fn default_overview_max() -> u32 {
    DEFAULT_OVERVIEW_MAX
}
fn default_crop_size() -> u32 {
    DEFAULT_CROP_SIZE
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawCaptureParams {
    uuid: String,
    #[serde(default)]
    popups: Vec<String>,
    #[serde(default)]
    roi: Option<Rect>,
    #[serde(default)]
    cursor: Option<Point>,
    #[serde(default = "default_overview_max")]
    overview_max: u32,
    #[serde(default = "default_crop_size")]
    crop_size: u32,
    #[serde(default)]
    live_regions: Vec<Rect>,
    #[serde(default)]
    force: bool,
}

/// Where the native-resolution crop is centred.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Focus {
    Roi(Rect),
    Cursor(Point),
    /// Neither given: the centre of the window.
    Centre,
}

#[derive(Clone, Debug, PartialEq)]
pub struct CaptureParams {
    pub uuid: WindowId,
    pub popups: Vec<WindowId>,
    pub focus: Focus,
    pub overview_max: u32,
    pub crop_size: u32,
    pub live_regions: Vec<Rect>,
    pub force: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Verb {
    Hello,
    Ping,
    Forget,
    Blind { on: bool },
    CaptureWindow(CaptureParams),
}

#[derive(Clone, Debug, PartialEq)]
pub struct Request {
    pub id: i64,
    pub verb: Verb,
}

/// Why a request was refused before doing anything. `id` is known unless the frame was not
/// even a JSON object with an integer `id`.
#[derive(Clone, Debug, PartialEq)]
pub struct RequestError {
    pub id: Option<i64>,
    pub code: &'static str,
    pub message: &'static str,
}

fn coord_ok(v: f64) -> bool {
    v.is_finite() && v.abs() <= MAX_COORD
}

fn rect_ok(r: &Rect) -> bool {
    coord_ok(r.x) && coord_ok(r.y) && coord_ok(r.w) && coord_ok(r.h) && r.w >= 0.0 && r.h >= 0.0
}

fn validate(raw: RawCaptureParams) -> Result<CaptureParams, &'static str> {
    let uuid = WindowId::parse(&raw.uuid).ok_or("uuid is not a window UUID")?;
    if raw.popups.len() > MAX_POPUPS {
        return Err("too many popups");
    }
    let mut popups = Vec::with_capacity(raw.popups.len());
    for text in &raw.popups {
        let id = WindowId::parse(text).ok_or("popup is not a window UUID")?;
        if id != uuid && !popups.contains(&id) {
            popups.push(id);
        }
    }
    let focus = match (raw.roi, raw.cursor) {
        (Some(_), Some(_)) => return Err("roi and cursor are mutually exclusive"),
        (Some(roi), None) if rect_ok(&roi) => Focus::Roi(roi),
        (None, Some(p)) if coord_ok(p.x) && coord_ok(p.y) => Focus::Cursor(p),
        (None, None) => Focus::Centre,
        _ => return Err("roi or cursor out of range"),
    };
    if !(MIN_OVERVIEW_MAX..=MAX_OVERVIEW_MAX).contains(&raw.overview_max) {
        return Err("overview_max out of range");
    }
    if !(MIN_CROP_SIZE..=MAX_CROP_SIZE).contains(&raw.crop_size) {
        return Err("crop_size out of range");
    }
    if raw.live_regions.len() > MAX_LIVE_REGIONS {
        return Err("too many live_regions");
    }
    if !raw.live_regions.iter().all(rect_ok) {
        return Err("live_regions out of range");
    }
    Ok(CaptureParams {
        uuid,
        popups,
        focus,
        overview_max: raw.overview_max,
        crop_size: raw.crop_size,
        live_regions: raw.live_regions,
        force: raw.force,
    })
}

/// Parse one request frame. Unknown verbs and unknown or mistyped fields are errors: there is
/// no way to smuggle an option to KWin through this function.
pub fn parse_request(payload: &[u8]) -> Result<Request, RequestError> {
    let bad = |id, message| RequestError {
        id,
        code: "bad_request",
        message,
    };
    let value: Value =
        serde_json::from_slice(payload).map_err(|_| bad(None, "request is not valid JSON"))?;
    let Value::Object(mut fields) = value else {
        return Err(bad(None, "request is not a JSON object"));
    };
    let id = fields
        .remove("id")
        .and_then(|v| v.as_i64())
        .ok_or_else(|| bad(None, "request has no integer id"))?;
    let verb = match fields.remove("verb") {
        Some(Value::String(verb)) => verb,
        _ => return Err(bad(Some(id), "request has no verb")),
    };
    let no_fields = |fields: &Map<String, Value>| {
        if fields.is_empty() {
            Ok(())
        } else {
            Err(bad(Some(id), "unexpected field"))
        }
    };
    let verb = match verb.as_str() {
        "hello" => no_fields(&fields).map(|()| Verb::Hello)?,
        "ping" => no_fields(&fields).map(|()| Verb::Ping)?,
        "forget" => no_fields(&fields).map(|()| Verb::Forget)?,
        "blind" => match (fields.remove("on"), fields.is_empty()) {
            (Some(Value::Bool(on)), true) => Verb::Blind { on },
            _ => return Err(bad(Some(id), "blind needs exactly one boolean field 'on'")),
        },
        "capture_window" => {
            let raw: RawCaptureParams =
                serde_json::from_value(Value::Object(fields)).map_err(|_| {
                    bad(
                        Some(id),
                        "capture_window has a missing, unknown or mistyped field",
                    )
                })?;
            Verb::CaptureWindow(validate(raw).map_err(|message| bad(Some(id), message))?)
        }
        _ => {
            return Err(RequestError {
                id: Some(id),
                code: "unknown_verb",
                message: "unknown verb",
            })
        }
    };
    Ok(Request { id, verb })
}

/// A reply: one JSON header and the binary frames that follow it.
#[derive(Debug, Clone, PartialEq)]
pub struct Reply {
    pub header: Map<String, Value>,
    pub frames: Vec<Vec<u8>>,
}

impl Reply {
    pub fn new(status: &str) -> Self {
        let mut header = Map::new();
        header.insert("status".into(), json!(status));
        Self {
            header,
            frames: Vec::new(),
        }
    }

    pub fn with(mut self, key: &str, value: Value) -> Self {
        self.header.insert(key.into(), value);
        self
    }

    pub fn ok() -> Self {
        Self::new("ok")
    }

    pub fn blind(reason: &str) -> Self {
        Self::new("blind").with("reason", json!(reason))
    }

    pub fn error(code: &str, message: &str) -> Self {
        Self::new("error")
            .with("code", json!(code))
            .with("message", json!(message))
    }

    pub fn status(&self) -> &str {
        self.header
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("")
    }

    /// The header frame as sent: the reply plus the request id and the sizes of the binary
    /// frames that follow.
    pub fn header_bytes(&self, id: Option<i64>) -> Vec<u8> {
        let mut header = self.header.clone();
        header.insert("id".into(), json!(id));
        header.insert(
            "frames".into(),
            json!(self.frames.iter().map(Vec::len).collect::<Vec<_>>()),
        );
        serde_json::to_vec(&Value::Object(header)).expect("a JSON map always serializes")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const UUID: &str = "11111111-2222-4333-8444-555555555555";
    const POPUP: &str = "{AAAAAAAA-2222-4333-8444-555555555555}";

    fn parse(text: &str) -> Result<Request, RequestError> {
        parse_request(text.as_bytes())
    }

    #[test]
    fn capture_defaults() {
        let req = parse(&format!(
            r#"{{"id":7,"verb":"capture_window","uuid":"{UUID}"}}"#
        ))
        .unwrap();
        assert_eq!(req.id, 7);
        let Verb::CaptureWindow(p) = req.verb else {
            panic!("wrong verb")
        };
        assert_eq!(p.uuid.as_str(), UUID);
        assert_eq!((p.overview_max, p.crop_size, p.force), (1024, 512, false));
        assert_eq!(p.focus, Focus::Centre);
        assert!(p.popups.is_empty() && p.live_regions.is_empty());
    }

    #[test]
    fn capture_full_parameters() {
        let req = parse(&format!(
            r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","popups":["{POPUP}","{POPUP}","{UUID}"],
               "roi":{{"x":1,"y":2,"w":3,"h":4}},"overview_max":512,"crop_size":256,
               "live_regions":[{{"x":0,"y":0,"w":10,"h":10}}],"force":true}}"#
        ))
        .unwrap();
        let Verb::CaptureWindow(p) = req.verb else {
            panic!("wrong verb")
        };
        assert_eq!(
            p.popups.len(),
            1,
            "duplicates and the target itself are dropped"
        );
        assert_eq!(p.popups[0].as_str(), "aaaaaaaa-2222-4333-8444-555555555555");
        assert_eq!(
            p.focus,
            Focus::Roi(Rect {
                x: 1.0,
                y: 2.0,
                w: 3.0,
                h: 4.0
            })
        );
        assert!(p.force);
    }

    #[test]
    fn simple_verbs() {
        assert_eq!(
            parse(r#"{"id":1,"verb":"hello"}"#).unwrap().verb,
            Verb::Hello
        );
        assert_eq!(parse(r#"{"id":1,"verb":"ping"}"#).unwrap().verb, Verb::Ping);
        assert_eq!(
            parse(r#"{"id":1,"verb":"forget"}"#).unwrap().verb,
            Verb::Forget
        );
        assert_eq!(
            parse(r#"{"id":1,"verb":"blind","on":true}"#).unwrap().verb,
            Verb::Blind { on: true }
        );
    }

    #[test]
    fn there_is_no_verb_for_anything_but_one_window() {
        for verb in [
            "capture_area",
            "capture_screen",
            "capture_workspace",
            "capture",
            "CaptureWindow",
        ] {
            let err = parse(&format!(r#"{{"id":3,"verb":"{verb}","uuid":"{UUID}"}}"#)).unwrap_err();
            assert_eq!((err.id, err.code), (Some(3), "unknown_verb"), "{verb}");
        }
    }

    #[test]
    fn rejects_unknown_and_mistyped_fields() {
        let cases = [
            format!(r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","include-cursor":true}}"#),
            format!(
                r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","area":{{"x":0,"y":0,"w":1,"h":1}}}}"#
            ),
            format!(r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","force":1}}"#),
            format!(r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","overview_max":-1}}"#),
            format!(r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","overview_max":63}}"#),
            format!(r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","crop_size":4096}}"#),
            format!(
                r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","roi":{{"x":0,"y":0,"w":-1,"h":1}}}}"#
            ),
            format!(
                r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","cursor":{{"x":1e300,"y":0}}}}"#
            ),
            format!(
                r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","cursor":{{"x":1,"y":0}},"roi":{{"x":0,"y":0,"w":1,"h":1}}}}"#
            ),
            r#"{"id":1,"verb":"capture_window","uuid":"not-a-uuid"}"#.to_string(),
            r#"{"id":1,"verb":"capture_window"}"#.to_string(),
            format!(r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","popups":["x"]}}"#),
            r#"{"id":1,"verb":"blind"}"#.to_string(),
            r#"{"id":1,"verb":"blind","on":"yes"}"#.to_string(),
            r#"{"id":1,"verb":"blind","on":true,"extra":1}"#.to_string(),
            r#"{"id":1,"verb":"ping","extra":1}"#.to_string(),
            r#"{"id":1}"#.to_string(),
            r#"{"id":1,"verb":5}"#.to_string(),
        ];
        for case in cases {
            let err = parse(&case).unwrap_err();
            assert_eq!((err.id, err.code), (Some(1), "bad_request"), "{case}");
        }
    }

    #[test]
    fn requests_without_a_usable_id_are_refused_with_a_null_id() {
        for case in [
            "",
            "nonsense",
            "[1]",
            r#"{"verb":"ping"}"#,
            r#"{"id":"1","verb":"ping"}"#,
            r#"{"id":1.5,"verb":"ping"}"#,
        ] {
            let err = parse(case).unwrap_err();
            assert_eq!((err.id, err.code), (None, "bad_request"), "{case}");
        }
    }

    #[test]
    fn too_many_popups_or_regions() {
        let popups: Vec<String> = (0..9)
            .map(|i| format!("\"{i:08x}-2222-4333-8444-555555555555\""))
            .collect();
        let text = format!(
            r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","popups":[{}]}}"#,
            popups.join(",")
        );
        assert_eq!(parse(&text).unwrap_err().code, "bad_request");
        let regions = vec![r#"{"x":0,"y":0,"w":1,"h":1}"#; MAX_LIVE_REGIONS + 1].join(",");
        let text = format!(
            r#"{{"id":1,"verb":"capture_window","uuid":"{UUID}","live_regions":[{regions}]}}"#
        );
        assert_eq!(parse(&text).unwrap_err().code, "bad_request");
    }

    #[test]
    fn header_announces_id_and_frame_sizes() {
        let mut reply = Reply::ok().with("hash", json!("00ff"));
        reply.frames = vec![vec![0; 5], vec![0; 9]];
        let header: Value = serde_json::from_slice(&reply.header_bytes(Some(4))).unwrap();
        assert_eq!(
            header,
            json!({"id":4,"status":"ok","hash":"00ff","frames":[5,9]})
        );
        let header: Value =
            serde_json::from_slice(&Reply::blind("locked").header_bytes(None)).unwrap();
        assert_eq!(
            header,
            json!({"id":null,"status":"blind","reason":"locked","frames":[]})
        );
    }
}
