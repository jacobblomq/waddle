//! The privacy blocklist: windows the helper refuses to capture.
//!
//! Patterns are case-insensitive substrings. Both sides are folded the same way as
//! `waddle_core.privacy.normalize` (NFKC, Unicode case folding, NFKC), and the two
//! implementations are pinned to each other by `packages/core/tests/blocklist_cases.json`. The defaults are compiled in from
//! `knowledge/privacy/default-blocklist.json`; the user's additions come from
//! `[privacy] block_classes` / `block_titles` in Waddle's `config.toml`. Nothing the user's
//! file contains - or fails to contain - can remove a default.

use std::fs;
use std::io::Read;
use std::path::PathBuf;
use std::time::SystemTime;

use serde::Deserialize;
use unicode_normalization::UnicodeNormalization;

use crate::desktop::WindowInfo;

/// Waddle's own application id; its windows are never a capture target.
pub const OWN_APP_ID: &str = "io.github.jacobblomq.Waddle";

const DEFAULTS_JSON: &str = include_str!("../../knowledge/privacy/default-blocklist.json");
/// A config file larger than this is treated as unreadable.
const MAX_CONFIG_BYTES: u64 = 1024 * 1024;
const MAX_USER_PATTERNS: usize = 512;
const MAX_PATTERN_CHARS: usize = 256;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Patterns {
    pub class: Vec<String>,
    pub title: Vec<String>,
}

/// Characters that draw nothing and that NFKC keeps: soft hyphen, combining grapheme joiner,
/// zero-width spaces and joiners, bidi marks, embeddings and isolates, word joiner and the
/// other invisible operators, variation selectors, the byte-order mark, tag characters.
/// A title reads the same with or without them, so they are dropped before matching.
/// The same set, written the same way, is in `waddle_core.privacy._INVISIBLE`.
fn is_invisible(c: char) -> bool {
    matches!(
        c,
        '\u{00AD}'
            | '\u{034F}'
            | '\u{061C}'
            | '\u{180E}'
            | '\u{200B}'..='\u{200F}'
            | '\u{202A}'..='\u{202E}'
            | '\u{2060}'..='\u{206F}'
            | '\u{FE00}'..='\u{FE0F}'
            | '\u{FEFF}'
            | '\u{E0000}'..='\u{E0FFF}'
    )
}

/// Fold text for matching: drop invisible characters, then NFKC, full Unicode case folding,
/// NFKC again. Full-width letters, ligatures, decomposed accents, case variants and a word
/// with a soft hyphen or zero-width space inside it all compare equal afterwards.
pub fn fold(text: &str) -> String {
    let composed: String = text.chars().filter(|c| !is_invisible(*c)).nfkc().collect();
    caseless::default_case_fold_str(&composed).nfkc().collect()
}

/// Fold and drop blank entries: an empty substring would match every window.
fn normalize(patterns: Vec<String>) -> Vec<String> {
    patterns
        .iter()
        .map(|p| fold(p))
        .filter(|p| !p.trim().is_empty())
        .collect()
}

#[derive(Deserialize)]
struct DefaultsFile {
    class: Vec<String>,
    title: Vec<String>,
}

/// The compiled-in defaults. A broken embedded file is a build defect and stops the helper
/// from starting at all, which is the closed failure.
pub fn default_patterns() -> Patterns {
    let file: DefaultsFile =
        serde_json::from_str(DEFAULTS_JSON).expect("embedded default-blocklist.json is valid");
    let patterns = Patterns {
        class: normalize(file.class),
        title: normalize(file.title),
    };
    assert!(
        !patterns.class.is_empty() && !patterns.title.is_empty(),
        "embedded default blocklist is empty"
    );
    patterns
}

#[derive(Deserialize, Default)]
struct ConfigFile {
    #[serde(default)]
    privacy: PrivacySection,
}

#[derive(Deserialize, Default)]
struct PrivacySection {
    #[serde(default)]
    block_classes: Vec<String>,
    #[serde(default)]
    block_titles: Vec<String>,
}

/// The user's additions from the text of `config.toml`. `None` when the file is malformed
/// (not TOML, or the keys have the wrong type, or the lists are absurdly large).
pub fn parse_user_patterns(text: &str) -> Option<Patterns> {
    let file: ConfigFile = toml::from_str(text).ok()?;
    let PrivacySection {
        block_classes,
        block_titles,
    } = file.privacy;
    let sane = |list: &[String]| {
        list.len() <= MAX_USER_PATTERNS
            && list.iter().all(|p| p.chars().count() <= MAX_PATTERN_CHARS)
    };
    if !sane(&block_classes) || !sane(&block_titles) {
        return None;
    }
    Some(Patterns {
        class: normalize(block_classes),
        title: normalize(block_titles),
    })
}

/// Where Waddle's config lives: `$XDG_CONFIG_HOME/waddle/config.toml`, else under `~/.config`.
pub fn config_path() -> Option<PathBuf> {
    let base = match std::env::var_os("XDG_CONFIG_HOME") {
        Some(dir) if !dir.is_empty() && PathBuf::from(&dir).is_absolute() => PathBuf::from(dir),
        _ => PathBuf::from(std::env::var_os("HOME").filter(|h| !h.is_empty())?).join(".config"),
    };
    Some(base.join("waddle").join("config.toml"))
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Stamp {
    NeverLooked,
    Missing,
    Present(SystemTime, u64),
    Unreadable,
}

pub struct Blocklist {
    defaults: Patterns,
    user: Patterns,
    path: Option<PathBuf>,
    stamp: Stamp,
    config_ok: bool,
}

impl Blocklist {
    /// Defaults plus whatever `path` adds; `None` means defaults only.
    pub fn new(path: Option<PathBuf>) -> Self {
        let mut list = Self {
            defaults: default_patterns(),
            user: Patterns::default(),
            path,
            stamp: Stamp::NeverLooked,
            config_ok: true,
        };
        list.refresh();
        list
    }

    /// Defaults plus the given additions, with no config file behind them.
    pub fn with_additions(classes: Vec<String>, titles: Vec<String>) -> Self {
        let mut list = Self::new(None);
        list.user = Patterns {
            class: normalize(classes),
            title: normalize(titles),
        };
        list
    }

    /// False while the user's config exists but cannot be read or parsed. The defaults (and
    /// the last good additions) keep reporting what they block, but the caller must not
    /// capture anything while this is false: the unreadable file may block more.
    pub fn config_ok(&self) -> bool {
        self.config_ok
    }

    /// Re-read the user's file if it appeared, vanished or changed. One `stat` when not.
    pub fn refresh(&mut self) {
        let Some(path) = self.path.clone() else {
            return;
        };
        let stamp = match fs::metadata(&path) {
            // A directory, FIFO or device in the file's place is not a config file (and
            // opening a FIFO would block for ever).
            Ok(meta) if !meta.is_file() => Stamp::Unreadable,
            Ok(meta) => match meta.modified() {
                Ok(mtime) => Stamp::Present(mtime, meta.len()),
                Err(_) => Stamp::Unreadable,
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Stamp::Missing,
            Err(_) => Stamp::Unreadable,
        };
        if stamp == self.stamp && stamp != Stamp::Unreadable {
            return;
        }
        self.stamp = stamp;
        match stamp {
            // No file: no additions, and nothing wrong.
            Stamp::Missing => {
                self.user = Patterns::default();
                self.config_ok = true;
            }
            Stamp::Present(_, len) if len <= MAX_CONFIG_BYTES => {
                let mut text = String::new();
                let read = fs::File::open(&path)
                    .and_then(|f| f.take(MAX_CONFIG_BYTES + 1).read_to_string(&mut text));
                // The file may have grown since the `stat`: a cut-off read is not the file.
                let whole = read.is_ok_and(|_| text.len() as u64 <= MAX_CONFIG_BYTES);
                match whole.then(|| parse_user_patterns(&text)).flatten() {
                    Some(user) => {
                        self.user = user;
                        self.config_ok = true;
                    }
                    // Keep the last good additions: a typo must not unblock anything.
                    None => self.config_ok = false,
                }
            }
            _ => self.config_ok = false,
        }
    }

    /// True when the window must not be looked at.
    pub fn is_blocked(&self, info: &WindowInfo) -> bool {
        let classes = [
            fold(&info.resource_class),
            fold(&info.resource_name),
            fold(&info.desktop_file),
        ];
        let own = fold(OWN_APP_ID);
        if classes.contains(&own) {
            return true;
        }
        let caption = fold(&info.caption);
        let class_patterns = self.defaults.class.iter().chain(&self.user.class);
        let title_patterns = self.defaults.title.iter().chain(&self.user.title);
        class_patterns
            .into_iter()
            .any(|p| classes.iter().any(|c| c.contains(p.as_str())))
            || title_patterns
                .into_iter()
                .any(|p| caption.contains(p.as_str()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};
    use std::time::Duration;

    fn window(class: &str, name: &str, desktop: &str, caption: &str) -> WindowInfo {
        WindowInfo {
            resource_class: class.into(),
            resource_name: name.into(),
            desktop_file: desktop.into(),
            caption: caption.into(),
            ..WindowInfo::default()
        }
    }

    fn defaults_only() -> Blocklist {
        Blocklist::new(None)
    }

    #[test]
    fn folding_matches_the_python_gate() {
        assert_eq!(fold("KeePassXC"), "keepassxc");
        assert_eq!(
            fold("\u{ff29}\u{ff4e}\u{ff43}\u{ff4f}"),
            "inco",
            "full-width letters"
        );
        assert_eq!(fold("STRASSE"), fold("stra\u{df}e"), "sharp s folds to ss");
        assert_eq!(
            fold("re\u{301}sume\u{301}"),
            fold("R\u{c9}SUM\u{c9}"),
            "decomposed accents"
        );
        assert_eq!(fold("\u{fb01}nance"), "finance", "ligatures");
        assert_eq!(
            fold("tax  return"),
            "tax  return",
            "whitespace is not collapsed"
        );
        assert_eq!(fold("in\u{ad}cog\u{200b}ni\u{200d}to"), "incognito");
        assert_eq!(fold("\u{2068}Sign in\u{2069}\u{feff}"), "sign in");
        assert_eq!(
            fold("e\u{200b}\u{301}"),
            "\u{e9}",
            "composes once the gap is gone"
        );
        assert_eq!(fold("\u{200b}\u{ad}"), "", "a pattern of nothing is blank");
    }

    /// The conformance cases shared with `waddle_core.privacy`. They live in the Python
    /// package's tests and are compiled in, so this crate does not build without them: the
    /// two gates cannot drift apart unnoticed.
    const SHARED_CASES: &str = include_str!("../../packages/core/tests/blocklist_cases.json");

    #[test]
    fn shared_conformance_cases() {
        #[derive(Deserialize)]
        struct Case {
            id: String,
            class: String,
            name: String,
            desktop: String,
            title: String,
            extra_classes: Vec<String>,
            extra_titles: Vec<String>,
            expect: String,
        }
        #[derive(Deserialize)]
        struct Cases {
            cases: Vec<Case>,
        }
        let text = SHARED_CASES;
        let cases: Cases = serde_json::from_str(text).expect("fixture parses");
        assert!(cases.cases.len() >= 41, "fixture is suspiciously small");
        for case in cases.cases {
            let list = Blocklist::with_additions(case.extra_classes, case.extra_titles);
            let blocked =
                list.is_blocked(&window(&case.class, &case.name, &case.desktop, &case.title));
            let expected = match case.expect.as_str() {
                "blocked" => true,
                "allowed" => false,
                other => panic!("case {}: unknown expectation {other}", case.id),
            };
            assert_eq!(blocked, expected, "case {}", case.id);
        }
    }

    #[test]
    fn embedded_defaults_load_and_are_lowercase() {
        let p = default_patterns();
        assert!(p.class.contains(&"keepassxc".to_string()));
        assert!(p.title.contains(&"incognito".to_string()));
        assert!(p
            .class
            .iter()
            .chain(&p.title)
            .all(|s| *s == s.to_lowercase() && !s.trim().is_empty()));
    }

    #[test]
    fn default_classes_match_any_of_the_three_identifiers_case_insensitively() {
        let list = defaults_only();
        assert!(list.is_blocked(&window("org.keepassxc.KeePassXC", "", "", "Database")));
        assert!(list.is_blocked(&window("", "Bitwarden", "", "")));
        assert!(list.is_blocked(&window("", "", "com.1Password.1Password", "")));
        assert!(list.is_blocked(&window("polkit-kde-authentication-agent-1", "", "", "")));
    }

    #[test]
    fn default_titles_match_the_caption_case_insensitively() {
        let list = defaults_only();
        assert!(list.is_blocked(&window(
            "firefox",
            "firefox",
            "firefox",
            "Mozilla Firefox Private Browsing"
        )));
        assert!(list.is_blocked(&window(
            "chromium",
            "",
            "",
            "New Tab - Chromium (INCOGNITO)"
        )));
        assert!(list.is_blocked(&window("firefox", "", "", "Sign in - Example")));
    }

    #[test]
    fn ordinary_windows_pass() {
        let list = defaults_only();
        assert!(!list.is_blocked(&window(
            "blender",
            "blender",
            "blender",
            "Blender - scene.blend"
        )));
        assert!(!list.is_blocked(&window(
            "org.kde.dolphin",
            "dolphin",
            "org.kde.dolphin",
            "Home - Dolphin"
        )));
        assert!(!list.is_blocked(&WindowInfo::default()));
    }

    #[test]
    fn title_patterns_are_not_applied_to_classes_or_the_reverse() {
        let list = defaults_only();
        // "incognito" is a title pattern only; "kgpg" is a class pattern only.
        assert!(!list.is_blocked(&window("incognito", "incognito", "incognito", "Notes")));
        assert!(!list.is_blocked(&window(
            "kate",
            "kate",
            "org.kde.kate",
            "kgpg notes.txt - Kate"
        )));
    }

    #[test]
    fn waddles_own_windows_are_blocked_by_exact_app_id() {
        let list = defaults_only();
        assert!(list.is_blocked(&window("io.github.jacobblomq.Waddle", "", "", "")));
        assert!(list.is_blocked(&window("", "IO.GITHUB.JACOBBLOMQ.WADDLE", "", "")));
        assert!(list.is_blocked(&window("python3", "", "io.github.jacobblomq.waddle", "")));
        // Equality, not substring: a different app is not blocked by resemblance.
        assert!(!list.is_blocked(&window("io.github.jacobblomq.WaddleViewer", "", "", "")));
    }

    #[test]
    fn user_patterns_parse_and_lowercase() {
        let p = parse_user_patterns(
            "[ui]\nstyle = \"minimal\"\n[privacy]\nblock_classes = [\"MyBank\", \"\", \"  \"]\nblock_titles = [\"Payroll\"]\n",
        )
        .unwrap();
        assert_eq!(
            p.class,
            vec!["mybank"],
            "blank entries would match everything and are dropped"
        );
        assert_eq!(p.title, vec!["payroll"]);
        assert_eq!(parse_user_patterns("").unwrap(), Patterns::default());
        assert_eq!(
            parse_user_patterns("[privacy]\n").unwrap(),
            Patterns::default()
        );
    }

    #[test]
    fn malformed_user_config_is_reported_as_such() {
        for text in [
            "[privacy\nblock_classes = []",
            "[privacy]\nblock_classes = \"mybank\"\n",
            "[privacy]\nblock_classes = [1, 2]\n",
            "privacy = 3\n",
            "[privacy]\nblock_titles = [[\"x\"]]\n",
        ] {
            assert!(parse_user_patterns(text).is_none(), "{text:?}");
        }
        let long = format!(
            "[privacy]\nblock_titles = [\"{}\"]\n",
            "x".repeat(MAX_PATTERN_CHARS + 1)
        );
        assert!(parse_user_patterns(&long).is_none());
    }

    static COUNTER: AtomicU32 = AtomicU32::new(0);

    /// A private scratch directory for one test, removed on drop.
    struct Scratch(PathBuf);
    impl Scratch {
        fn new() -> Self {
            let n = COUNTER.fetch_add(1, Ordering::SeqCst);
            let dir = std::env::temp_dir()
                .join(format!("waddle-capture-test-{}-{n}", std::process::id()));
            fs::create_dir_all(&dir).unwrap();
            Self(dir)
        }
        fn config(&self) -> PathBuf {
            self.0.join("config.toml")
        }
        /// Write the file with a distinct mtime so that the change is always noticed.
        fn write(&self, text: &str, age_secs: u64) {
            fs::write(self.config(), text).unwrap();
            let file = fs::File::options().write(true).open(self.config()).unwrap();
            file.set_modified(SystemTime::now() - Duration::from_secs(age_secs))
                .unwrap();
        }
    }
    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn user_additions_extend_the_defaults() {
        let scratch = Scratch::new();
        scratch.write(
            "[privacy]\nblock_classes = [\"MyBank\"]\nblock_titles = [\"payroll\"]\n",
            100,
        );
        let list = Blocklist::new(Some(scratch.config()));
        assert!(list.config_ok());
        assert!(list.is_blocked(&window("com.example.mybank", "", "", "")));
        assert!(list.is_blocked(&window("libreoffice", "", "", "PAYROLL 2026.ods")));
        assert!(
            list.is_blocked(&window("keepassxc", "", "", "")),
            "defaults still apply"
        );
        assert!(!list.is_blocked(&window("blender", "", "", "scene.blend")));
    }

    #[test]
    fn missing_config_means_defaults_only_and_is_not_an_error() {
        let scratch = Scratch::new();
        let list = Blocklist::new(Some(scratch.config()));
        assert!(list.config_ok());
        assert!(list.is_blocked(&window("keepassxc", "", "", "")));
        assert!(!list.is_blocked(&window("com.example.mybank", "", "", "")));
    }

    #[test]
    fn malformed_config_keeps_the_defaults() {
        let scratch = Scratch::new();
        scratch.write("this is [not toml", 100);
        let list = Blocklist::new(Some(scratch.config()));
        assert!(!list.config_ok());
        assert!(list.is_blocked(&window("keepassxc", "", "", "")));
        assert!(list.is_blocked(&window("firefox", "", "", "Private Browsing")));
    }

    #[test]
    fn config_is_reread_when_it_changes_and_a_broken_edit_keeps_the_last_good_additions() {
        let scratch = Scratch::new();
        scratch.write("[privacy]\nblock_classes = [\"mybank\"]\n", 300);
        let mut list = Blocklist::new(Some(scratch.config()));
        let bank = window("mybank", "", "", "");
        let wiki = window("mywiki", "", "", "");
        assert!(list.is_blocked(&bank) && !list.is_blocked(&wiki));

        scratch.write("[privacy]\nblock_classes = [\"mybank\", \"mywiki\"]\n", 200);
        list.refresh();
        assert!(
            list.is_blocked(&bank) && list.is_blocked(&wiki),
            "new entry picked up"
        );

        scratch.write("[privacy]\nblock_classes = \"oops\"\n", 100);
        list.refresh();
        assert!(!list.config_ok());
        assert!(
            list.is_blocked(&bank) && list.is_blocked(&wiki),
            "a typo unblocks nothing"
        );
        assert!(list.is_blocked(&window("keepassxc", "", "", "")));

        scratch.write("[privacy]\nblock_classes = []\n", 50);
        list.refresh();
        assert!(list.config_ok());
        assert!(
            !list.is_blocked(&bank) && !list.is_blocked(&wiki),
            "a valid edit can remove additions"
        );

        fs::remove_file(scratch.config()).unwrap();
        list.refresh();
        assert!(list.config_ok());
        assert!(list.is_blocked(&window("keepassxc", "", "", "")));
    }

    #[test]
    fn unchanged_file_is_not_reparsed() {
        let scratch = Scratch::new();
        scratch.write("[privacy]\nblock_classes = [\"mybank\"]\n", 100);
        let mut list = Blocklist::new(Some(scratch.config()));
        let before = list.stamp;
        list.refresh();
        assert_eq!(list.stamp, before);
        assert!(matches!(before, Stamp::Present(_, _)));
    }
}
