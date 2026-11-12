//! `--probe`: a JSON report about this session's capture path, for spike S2.1.
//!
//! It prints numbers, booleans, key names and D-Bus error names. It never prints pixels,
//! window titles, classes or any other text taken from a window.

use serde_json::{json, Map, Value};

use crate::blocklist::{config_path, Blocklist};
use crate::desktop::{Desktop, LockState, WindowId};

/// Build the report. `uuid` is the window to try; without one only the environment is
/// reported, because KWin offers no script-free way to ask which window is active.
pub fn report<D: Desktop>(desktop: &mut D, uuid: Option<&str>) -> Value {
    let env = desktop.environment();
    let lock = desktop.lock_state();
    let mut out = Map::new();
    out.insert("helper_version".into(), json!(crate::VERSION));
    out.insert("kwin".into(), json!(env.kwin));
    out.insert("screenshot2_version".into(), json!(env.screenshot2_version));
    out.insert("permission_regime".into(), json!(env.regime.as_str()));
    out.insert("lock_check".into(), json!(lock != LockState::Unknown));
    out.insert("locked".into(), json!(lock == LockState::Locked));

    let Some(text) = uuid else {
        out.insert(
            "capture".into(),
            json!({
                "attempted": false,
                "reason": "no window uuid given: run with --probe <uuid>",
                "hint": "KWin has no script-free call for the active window. One way to get a uuid: run `qdbus6 org.kde.KWin /KWin org.kde.KWin.queryWindowInfo`, click the window, and copy its uuid field.",
            }),
        );
        return Value::Object(out);
    };
    let Some(id) = WindowId::parse(text) else {
        out.insert(
            "capture".into(),
            json!({"attempted": false, "reason": "argument is not a window uuid"}),
        );
        return Value::Object(out);
    };
    if lock == LockState::Locked {
        out.insert(
            "capture".into(),
            json!({"attempted": false, "reason": "session is locked"}),
        );
        return Value::Object(out);
    }

    // The same gate as a real capture: a blocked window is not captured, not even to be
    // measured.
    let blocklist = Blocklist::new(config_path());
    let info = match desktop.window_info(&id) {
        Ok(Some(info)) => info,
        Ok(None) => {
            out.insert(
                "capture".into(),
                json!({"attempted": false, "reason": "KWin knows no such window"}),
            );
            return Value::Object(out);
        }
        Err(e) => {
            out.insert(
                "capture".into(),
                json!({"attempted": false, "reason": "getWindowInfo failed", "error": e.code(), "error_name": e.message()}),
            );
            return Value::Object(out);
        }
    };
    out.insert(
        "window_info".into(),
        json!({
            "keys": info.keys,
            "numbers": info.numbers,
            "uuid_matches": info.uuid.as_ref() == Some(&id),
        }),
    );
    if blocklist.is_blocked(&info) {
        out.insert(
            "capture".into(),
            json!({"attempted": false, "reason": "window is on the blocklist"}),
        );
        return Value::Object(out);
    }
    if !blocklist.config_ok() {
        out.insert(
            "capture".into(),
            json!({"attempted": false, "reason": "config.toml cannot be read, so the blocklist is unknown"}),
        );
        return Value::Object(out);
    }

    let capture = match desktop.capture_window(&id) {
        Ok(raw) => {
            let reported = raw.window_id.as_deref().and_then(WindowId::parse);
            json!({
                "attempted": true,
                "ok": true,
                "width": raw.meta.width,
                "height": raw.meta.height,
                "stride": raw.meta.stride,
                "format": raw.meta.format,
                "scale": raw.scale,
                "bytes": raw.data.len(),
                "window_id_present": raw.window_id.is_some(),
                "window_id_matches": reported.as_ref() == Some(&id),
                // For comparing against geometry: what the frame would measure in pixels.
                "frame_px_expected": {
                    "w": (info.width * raw.scale).round(),
                    "h": (info.height * raw.scale).round(),
                },
            })
        }
        Err(e) => {
            json!({"attempted": true, "ok": false, "error": e.code(), "error_name": e.message()})
        }
    };
    out.insert("capture".into(), capture);
    Value::Object(out)
}
