"""kdeglobals parsing (pure) and the file watcher (Qt, tmp_path)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from waddle_desktop.theme.kdeglobals import (
    kdeglobals_paths,
    load_palette,
    palette_from_kdeglobals,
    parse_kconfig,
)
from waddle_desktop.theme.palette import FALLBACK_DARK, FALLBACK_LIGHT, ThemePalette
from waddle_desktop.theme.watcher import ThemeWatcher

BREEZE_DARK = """\
[ColorEffects:Disabled]
Color=56,56,56
ColorAmount=0

[Colors:Button]
BackgroundAlternate=30,87,116
BackgroundNormal=41,44,48
ForegroundNormal=252,252,252

[Colors:Selection]
BackgroundNormal=61,174,233
ForegroundNormal=252,252,252

[Colors:View]
BackgroundAlternate=29,31,34
BackgroundNormal=20,22,24
ForegroundLink=29,153,243
ForegroundNegative=218,68,83
ForegroundNeutral=246,116,0
ForegroundNormal=252,252,252
ForegroundPositive=39,174,96

[Colors:Window]
BackgroundNormal=32,35,38
ForegroundNormal=252,252,252

[General]
AccentColor=233,61,130
ColorScheme=BreezeDark
font=Inter,11,-1,5,400,0,0,0,0,0,0,0,0,0,0,1
fixed=Hack,10,-1,5,400,0,0,0,0,0,0,0,0,0,0,1

[KDE]
AnimationDurationFactor=0.5
LookAndFeelPackage=org.kde.breezedark.desktop
"""

BREEZE_LIGHT = """\
[Colors:Button]
BackgroundNormal=252,252,252
ForegroundNormal=35,38,41

[Colors:Selection]
BackgroundNormal=61,174,233
ForegroundNormal=255,255,255

[Colors:View]
BackgroundNormal=255,255,255
ForegroundLink=41,128,185
ForegroundNegative=218,68,83
ForegroundNeutral=246,116,0
ForegroundNormal=35,38,41
ForegroundPositive=39,174,96

[Colors:Window]
BackgroundNormal=239,240,241
ForegroundNormal=35,38,41

[General]
ColorScheme=BreezeLight
font=Noto Sans,10,-1,5,400,0,0,0,0,0,0,0,0,0,0,1

[KDE]
LookAndFeelPackage=org.kde.breeze.desktop
"""


# ---- parse_kconfig ---------------------------------------------------------------------------


def test_parse_groups_subgroups_comments_and_blank_lines() -> None:
    text = (
        "top=1\n# comment\n\n[General]\nName = Breeze \n#x=y\n[A][B]\nk=v\n[Colors:Window]\nz=1\n"
    )
    assert parse_kconfig(text) == {
        "": {"top": "1"},
        "General": {"Name": "Breeze"},
        "A][B": {"k": "v"},
        "Colors:Window": {"z": "1"},
    }


def test_parse_flag_suffixes_and_localized_keys() -> None:
    text = (
        "[General][$i]\n"
        "AccentColor[$i]=1,2,3\n"
        "Shell[$e]=$HOME/bin\n"
        "Name=Plain\n"
        "Name[de]=Deutsch\n"
        "Name[de][$i]=Deutsch\n"
    )
    assert parse_kconfig(text) == {
        "General": {"AccentColor": "1,2,3", "Shell": "$HOME/bin", "Name": "Plain"}
    }


def test_parse_equals_in_value_crlf_and_junk_lines() -> None:
    text = (
        "[G]\r\nurl=http://x/?a=b&c=d\r\nempty=\r\nnot an entry\r\n=novalue\r\n[broken\r\nk=v\r\n"
    )
    assert parse_kconfig(text) == {"G": {"url": "http://x/?a=b&c=d", "empty": ""}}


def test_parse_ignores_a_leading_byte_order_mark() -> None:
    assert parse_kconfig("\ufeff[General]\nk=v\n") == {"General": {"k": "v"}}


def test_parse_last_duplicate_wins() -> None:
    assert parse_kconfig("[G]\nk=1\nk=2\n") == {"G": {"k": "2"}}


# ---- palette_from_kdeglobals -----------------------------------------------------------------


def test_breeze_dark() -> None:
    p = palette_from_kdeglobals([BREEZE_DARK])
    assert p == ThemePalette(
        accent="#e93d82",
        window_bg="#202326",
        window_fg="#fcfcfc",
        view_bg="#141618",
        view_fg="#fcfcfc",
        button_bg="#292c30",
        button_fg="#fcfcfc",
        highlight="#3daee9",
        highlighted_fg="#fcfcfc",
        positive="#27ae60",
        negative="#da4453",
        neutral="#f67400",
        link="#1d99f3",
        is_dark=True,
        font_family="Inter",
        font_point_size=11.0,
        animation_factor=0.5,
    )


def test_breeze_light_and_accent_falls_back_to_highlight() -> None:
    p = palette_from_kdeglobals([BREEZE_LIGHT])
    assert p == FALLBACK_LIGHT  # Breeze Light is exactly what the fallback encodes
    custom = BREEZE_LIGHT.replace("BackgroundNormal=61,174,233", "BackgroundNormal=200,0,100")
    p = palette_from_kdeglobals([custom])
    assert p.highlight == "#c80064"
    assert p.accent == "#c80064"


def test_empty_input_is_light_fallback() -> None:
    assert palette_from_kdeglobals([]) == FALLBACK_LIGHT
    assert palette_from_kdeglobals([""]) == FALLBACK_LIGHT


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1,2,3", "#010203"),
        (" 255 , 0 , 16 ", "#ff0010"),
        ("10,20,30,128", "#0a141e"),
        ("#AbCdEf", "#abcdef"),
        ("256,0,0", None),
        ("-1,0,0", None),
        ("1,2", None),
        ("1,2,3,4,5", None),
        ("red", None),
        ("#abc", None),
        ("#gggggg", None),
        ("1.5,2,3", None),
        ("", None),
    ],
)
def test_color_forms(value: str, expected: str | None) -> None:
    p = palette_from_kdeglobals([f"[General]\nAccentColor={value}\n"])
    assert p.accent == (expected or FALLBACK_LIGHT.highlight)


def test_dark_window_selects_dark_fallback_per_missing_key() -> None:
    p = palette_from_kdeglobals(["[Colors:Window]\nBackgroundNormal=#101010\n"])
    assert p.is_dark
    assert p.window_bg == "#101010"
    assert p.window_fg == FALLBACK_DARK.window_fg
    assert p.view_bg == FALLBACK_DARK.view_bg
    assert p.link == FALLBACK_DARK.link


def test_malformed_color_falls_back_only_for_that_key() -> None:
    broken = BREEZE_DARK.replace("ForegroundLink=29,153,243", "ForegroundLink=oops")
    p = palette_from_kdeglobals([broken.replace("BackgroundNormal=20,22,24", "x=1")])
    assert p.link == FALLBACK_DARK.link
    assert p.view_bg == FALLBACK_DARK.view_bg
    assert p.accent == "#e93d82"


@pytest.mark.parametrize(
    ("window_bg", "dark"),
    [("0,0,0", True), ("255,255,255", False), ("187,187,187", True), ("189,189,189", False)],
)
def test_is_dark_uses_relative_luminance(window_bg: str, dark: bool) -> None:
    # sRGB luminance 0.5 sits at gray ~188, not at 128
    p = palette_from_kdeglobals([f"[Colors:Window]\nBackgroundNormal={window_bg}\n"])
    assert p.is_dark is dark


def test_is_dark_weights_green_over_blue() -> None:
    green = palette_from_kdeglobals(["[Colors:Window]\nBackgroundNormal=0,255,0\n"])
    blue = palette_from_kdeglobals(["[Colors:Window]\nBackgroundNormal=0,0,255\n"])
    assert not green.is_dark
    assert blue.is_dark


def test_later_layers_override_earlier_per_key() -> None:
    system = "[General]\nAccentColor=1,1,1\nfont=Sys,9\n[KDE]\nAnimationDurationFactor=2\n"
    user = "[General]\nAccentColor[$i]=2,2,2\n"
    p = palette_from_kdeglobals([system, user])
    assert p.accent == "#020202"
    assert p.font_family == "Sys"
    assert p.animation_factor == 2.0
    assert palette_from_kdeglobals([user, system]).accent == "#010101"


@pytest.mark.parametrize(
    ("font", "family", "size"),
    [
        ("Inter,11,-1,5,400,0,0,0,0,0,0,0,0,0,0,1", "Inter", 11.0),
        ("Noto Sans Mono, 9.5 ,-1", "Noto Sans Mono", 9.5),
        ("Inter,-1,14,5", "Inter", 10.0),
        ("Inter,0", "Inter", 10.0),
        ("Inter,big", "Inter", 10.0),
        ("Inter,nan", "Inter", 10.0),
        ("Inter", "Inter", 10.0),
        (",12", "Noto Sans", 12.0),
        ("", "Noto Sans", 10.0),
    ],
)
def test_font(font: str, family: str, size: float) -> None:
    p = palette_from_kdeglobals([f"[General]\nfont={font}\n"])
    assert (p.font_family, p.font_point_size) == (family, size)


@pytest.mark.parametrize(
    ("raw", "auto", "on", "off"),
    [
        ("0.5", 0.5, 0.0, 0.5),
        ("0", 0.0, 0.0, 1.0),
        ("1", 1.0, 0.0, 1.0),
        ("25", 10.0, 0.0, 10.0),
        ("-3", 0.0, 0.0, 1.0),
        ("fast", 1.0, 0.0, 1.0),
        ("nan", 1.0, 0.0, 1.0),
        ("inf", 1.0, 0.0, 1.0),
        (None, 1.0, 0.0, 1.0),
    ],
)
def test_animation_factor(raw: str | None, auto: float, on: float, off: float) -> None:
    text = "[KDE]\n" + (f"AnimationDurationFactor={raw}\n" if raw is not None else "")
    assert palette_from_kdeglobals([text]).animation_factor == auto
    assert palette_from_kdeglobals([text], reduced_motion="auto").animation_factor == auto
    assert palette_from_kdeglobals([text], reduced_motion="on").animation_factor == on
    assert palette_from_kdeglobals([text], reduced_motion="off").animation_factor == off


# ---- paths and loading -----------------------------------------------------------------------


def test_paths_increasing_priority() -> None:
    home = Path("/home/u")
    assert kdeglobals_paths({"XDG_CONFIG_DIRS": "/a:/b"}, home) == [
        Path("/b/kdeglobals"),
        Path("/a/kdeglobals"),
        Path("/home/u/.config/kdeglobals"),
    ]
    assert kdeglobals_paths({}, home) == [
        Path("/etc/xdg/kdeglobals"),
        Path("/home/u/.config/kdeglobals"),
    ]


def test_paths_ignore_relative_and_empty_values() -> None:
    home = Path("/home/u")
    env = {"XDG_CONFIG_DIRS": "rel::/a:./x", "XDG_CONFIG_HOME": "relative/config"}
    assert kdeglobals_paths(env, home) == [
        Path("/a/kdeglobals"),
        Path("/home/u/.config/kdeglobals"),
    ]
    env = {"XDG_CONFIG_DIRS": "rel", "XDG_CONFIG_HOME": "/cfg"}
    assert kdeglobals_paths(env, home) == [Path("/etc/xdg/kdeglobals"), Path("/cfg/kdeglobals")]


def test_paths_config_home_is_a_single_directory() -> None:
    home = Path("/home/u")
    assert kdeglobals_paths({"XDG_CONFIG_HOME": "/we:ird/cfg"}, home)[-1] == Path(
        "/we:ird/cfg/kdeglobals"
    )
    assert kdeglobals_paths({"XDG_CONFIG_HOME": ""}, home)[-1] == Path("/home/u/.config/kdeglobals")


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "XDG_CONFIG_DIRS": f"{tmp_path / 'hi'}:{tmp_path / 'lo'}",
        "XDG_CONFIG_HOME": str(tmp_path / "user"),
    }


def test_load_palette_layers_files(tmp_path: Path) -> None:
    for name in ("hi", "lo", "user"):
        (tmp_path / name).mkdir()
    (tmp_path / "lo" / "kdeglobals").write_text("[General]\nAccentColor=1,1,1\nfont=Lo,8\n")
    (tmp_path / "hi" / "kdeglobals").write_text("[General]\nAccentColor=2,2,2\n")
    p = load_palette(_env(tmp_path), tmp_path)
    assert (p.accent, p.font_family) == ("#020202", "Lo")
    (tmp_path / "user" / "kdeglobals").write_text(BREEZE_DARK)
    p = load_palette(_env(tmp_path), tmp_path, reduced_motion="on")
    assert (p.accent, p.font_family, p.is_dark) == ("#e93d82", "Inter", True)
    assert p.animation_factor == 0.0


def test_load_palette_never_raises(tmp_path: Path) -> None:
    assert load_palette(_env(tmp_path), tmp_path) == FALLBACK_LIGHT  # nothing exists
    (tmp_path / "lo").mkdir()
    (tmp_path / "lo" / "kdeglobals").mkdir()  # a directory where a file should be
    (tmp_path / "hi").mkdir()
    (tmp_path / "hi" / "kdeglobals").write_bytes(
        bytes(range(256)) * 64 + b"\n[General\xff]\n\x00=\n"
    )
    (tmp_path / "user").mkdir()
    locked = tmp_path / "user" / "kdeglobals"
    locked.write_text(BREEZE_DARK)
    locked.chmod(0o000)
    try:
        expected = BREEZE_DARK if os.access(locked, os.R_OK) else ""  # root reads anyway
        assert load_palette(_env(tmp_path), tmp_path) == palette_from_kdeglobals([expected])
    finally:
        locked.chmod(0o600)


# ---- ThemeWatcher ----------------------------------------------------------------------------

DEBOUNCE = 40
QUIET = 400  # long enough for any stray event plus the debounce to have fired


def _content(accent: str) -> str:
    return f"[General]\nAccentColor={accent}\n"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)  # noqa: PTH105 - the point is the rename


class _Harness:
    def __init__(self, tmp_path: Path, qtbot: Any, *, create: bool = True) -> None:
        self.path = tmp_path / "config" / "kdeglobals"
        self.path.parent.mkdir()
        if create:
            self.path.write_text(_content("1,1,1"))
        env = {"XDG_CONFIG_DIRS": str(tmp_path / "none"), "XDG_CONFIG_HOME": str(self.path.parent)}
        paths = kdeglobals_paths(env, tmp_path)
        self.loads = 0

        def load() -> ThemePalette:
            self.loads += 1
            return load_palette(env, tmp_path)

        self.watcher = ThemeWatcher(paths, load, DEBOUNCE)
        self.seen: list[ThemePalette] = []
        self.watcher.changed.connect(self.seen.append)
        self.qtbot = qtbot
        self.watcher.start()

    def wait_for(self, count: int) -> None:
        self.qtbot.waitUntil(lambda: len(self.seen) >= count, timeout=3000)


@pytest.fixture
def harness(tmp_path: Path, qtbot: Any) -> Any:
    h = _Harness(tmp_path, qtbot)
    yield h
    h.watcher.stop()


def test_watcher_initial_palette_and_no_emission_on_start(harness: _Harness, qtbot: Any) -> None:
    assert harness.watcher.palette.accent == "#010101"
    qtbot.wait(QUIET)
    assert harness.seen == []


def test_watcher_in_place_modification_emits_once(harness: _Harness, qtbot: Any) -> None:
    harness.path.write_text(_content("9,9,9"))
    harness.wait_for(1)
    qtbot.wait(QUIET)
    assert [p.accent for p in harness.seen] == ["#090909"]
    assert harness.watcher.palette.accent == "#090909"


def test_watcher_survives_atomic_rename_twice(harness: _Harness, qtbot: Any) -> None:
    _atomic_write(harness.path, _content("2,2,2"))
    harness.wait_for(1)
    qtbot.wait(QUIET)
    _atomic_write(harness.path, _content("3,3,3"))
    harness.wait_for(2)
    qtbot.wait(QUIET)
    # and the re-added watch is a real file watch: an in-place write is still seen
    harness.path.write_text(_content("4,4,4"))
    harness.wait_for(3)
    assert [p.accent for p in harness.seen] == ["#020202", "#030303", "#040404"]


def test_watcher_identical_content_emits_nothing(harness: _Harness, qtbot: Any) -> None:
    harness.path.write_text(_content("1,1,1"))
    _atomic_write(harness.path, _content("1,1,1"))
    qtbot.wait(QUIET)
    assert harness.seen == []
    harness.path.write_text(_content("5,5,5"))  # still alive afterwards
    harness.wait_for(1)


def test_watcher_coalesces_bursts(harness: _Harness, qtbot: Any) -> None:
    for i in range(5):
        harness.path.write_text(_content(f"{10 + i},0,0"))
    harness.wait_for(1)
    qtbot.wait(QUIET)
    assert 1 <= len(harness.seen) <= 2
    assert harness.seen[-1].accent == "#0e0000"


def test_watcher_debounces_writes_spread_over_time(harness: _Harness, qtbot: Any) -> None:
    # Unlike the synchronous burst above, every write here is delivered as its own event.
    loads = harness.loads
    for i in range(5):
        harness.path.write_text(_content(f"{20 + i},0,0"))
        qtbot.wait(DEBOUNCE // 8)
    harness.wait_for(1)
    qtbot.wait(QUIET)
    assert 1 <= len(harness.seen) <= 2
    assert harness.loads - loads <= 2
    assert harness.seen[-1].accent == "#180000"


def test_watcher_ignores_unrelated_files_in_the_directory(harness: _Harness, qtbot: Any) -> None:
    loads = harness.loads
    for i in range(3):
        harness.path.parent.joinpath(f"other{i}.conf").write_text("x")
        qtbot.wait(DEBOUNCE)
    qtbot.wait(QUIET)
    assert harness.loads == loads
    assert harness.seen == []


def test_watcher_stop_prevents_emissions(harness: _Harness, qtbot: Any) -> None:
    harness.path.write_text(_content("6,6,6"))  # pending debounce is cancelled by stop()
    harness.watcher.stop()
    _atomic_write(harness.path, _content("7,7,7"))
    qtbot.wait(QUIET)
    assert harness.seen == []
    harness.watcher.start()  # restart picks up what was missed
    assert [p.accent for p in harness.seen] == ["#070707"]


def test_watcher_sees_file_created_after_start(tmp_path: Path, qtbot: Any) -> None:
    h = _Harness(tmp_path, qtbot, create=False)
    try:
        assert h.watcher.palette == FALLBACK_LIGHT
        h.path.write_text(_content("8,8,8"))
        h.wait_for(1)
        h.path.write_text(_content("9,9,9"))  # the new file is now watched itself
        h.wait_for(2)
        assert [p.accent for p in h.seen] == ["#080808", "#090909"]
    finally:
        h.watcher.stop()


def test_watcher_never_writes(harness: _Harness, qtbot: Any) -> None:
    before = (harness.path.read_bytes(), harness.path.stat().st_mtime_ns)
    harness.path.parent.joinpath("other.conf").write_text("x")  # unrelated churn in the dir
    qtbot.wait(QUIET)
    assert (harness.path.read_bytes(), harness.path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in harness.path.parent.iterdir()) == ["kdeglobals", "other.conf"]
    assert harness.seen == []
