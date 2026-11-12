"""Shortcut parsing and the two wire formats (pure)."""

from __future__ import annotations

import dataclasses

import pytest

from waddle_core.config import HotkeysConfig
from waddle_desktop.sensors import keys
from waddle_desktop.sensors.keys import KeyCombo, alternatives, parse

SHIFT, CTRL, ALT, META = 0x02000000, 0x04000000, 0x08000000, 0x10000000

# text -> (modifiers, key, canonical text)
ACCEPTED: dict[str, tuple[set[str], str, str]] = {
    "Meta+Space": ({"Meta"}, "Space", "Meta+Space"),
    "Meta+`": ({"Meta"}, "`", "Meta+`"),
    "Meta+Shift+W": ({"Meta", "Shift"}, "W", "Meta+Shift+W"),
    "Ctrl+Alt+F12": ({"Ctrl", "Alt"}, "F12", "Ctrl+Alt+F12"),
    "shift+meta+w": ({"Meta", "Shift"}, "W", "Meta+Shift+W"),
    "W+Meta": ({"Meta"}, "W", "Meta+W"),
    "SUPER+space": ({"Meta"}, "Space", "Meta+Space"),
    "Win+E": ({"Meta"}, "E", "Meta+E"),
    "Control+Alt+Delete": ({"Ctrl", "Alt"}, "Delete", "Ctrl+Alt+Delete"),
    "  Meta + Shift + h  ": ({"Meta", "Shift"}, "H", "Meta+Shift+H"),
    "alt+shift+ctrl+meta+x": ({"Meta", "Ctrl", "Alt", "Shift"}, "X", "Meta+Ctrl+Alt+Shift+X"),
    "Meta+1": ({"Meta"}, "1", "Meta+1"),
    "Ctrl++": ({"Ctrl"}, "+", "Ctrl++"),
    "Ctrl+Plus": ({"Ctrl"}, "+", "Ctrl++"),
    "Meta+grave": ({"Meta"}, "`", "Meta+`"),
    "Meta+Backtick": ({"Meta"}, "`", "Meta+`"),
    "Meta+/": ({"Meta"}, "/", "Meta+/"),
    "Meta+[": ({"Meta"}, "[", "Meta+["),
    "Alt+\\": ({"Alt"}, "\\", "Alt+\\"),
    "Meta+PgUp": ({"Meta"}, "PageUp", "Meta+PageUp"),
    "Meta+pagedown": ({"Meta"}, "PageDown", "Meta+PageDown"),
    "Meta+Return": ({"Meta"}, "Return", "Meta+Return"),
    "Meta+Tab": ({"Meta"}, "Tab", "Meta+Tab"),
    "Ctrl+Left": ({"Ctrl"}, "Left", "Ctrl+Left"),
    "f5": (set(), "F5", "F5"),
    "Shift+F24": ({"Shift"}, "F24", "Shift+F24"),
    "Pause": (set(), "Pause", "Pause"),
    "PrintScreen": (set(), "Print", "Print"),
    "Logo+Del": ({"Meta"}, "Delete", "Meta+Delete"),
}

REJECTED: dict[str, str] = {
    "": "empty",
    "   ": "blank",
    "Meta": "modifier only",
    "Meta+Shift": "modifiers only",
    "Ctrl+Alt+Shift+Meta": "all modifiers, no key",
    "Meta+": "trailing plus with no key",
    "+W": "leading plus",
    "Meta++W": "empty part",
    "Meta+W+E": "two keys",
    "Meta+Meta+W": "modifier twice",
    "Super+Win+W": "modifier twice through aliases",
    "Meta+Hyper+W": "unknown modifier is an unknown key, then two keys",
    "Meta+Escape": "Escape is never global",
    "Esc": "Escape is never global",
    "Ctrl+Alt+Esc": "Escape is never global",
    "W": "bare typing key",
    "Shift+W": "Shift alone is still typing",
    "Space": "bare typing key",
    "Shift+`": "shifted punctuation is typing",
    "Meta+F0": "no such function key",
    "Meta+F25": "function key out of range",
    "Meta+F01": "not a canonical function key",
    "Meta+é": "outside the table",
    "Meta+~": "shifted symbol is not a key",
    "Meta+Ω": "non-ASCII letter",
    "Meta+٣": "non-ASCII digit",
    "Meta+VolumeUp": "unknown named key",
    "Meta-W": "wrong separator",
    "Meta+" + "W" * 80: "too long",
}


@pytest.mark.parametrize("text", list(ACCEPTED))
def test_parse_accepts(text: str) -> None:
    mods, key, canonical = ACCEPTED[text]
    combo = parse(text)
    assert combo == KeyCombo(frozenset(mods), key)
    assert str(combo) == canonical
    assert parse(str(combo)) == combo  # the canonical text round-trips


@pytest.mark.parametrize("text", list(REJECTED))
def test_parse_rejects(text: str) -> None:
    with pytest.raises(ValueError) as caught:
        parse(text)
    if len(text.strip()) > 3:
        assert text.strip() not in str(caught.value)  # the message never echoes the input


def test_the_tables_are_big_enough_to_mean_something() -> None:
    assert len(ACCEPTED) + len(REJECTED) >= 50


def test_parse_rejects_non_strings() -> None:
    with pytest.raises(ValueError):
        parse(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mods", "key"),
    [
        (frozenset({"Hyper"}), "W"),
        (frozenset({"Meta"}), "w"),  # not canonical
        (frozenset({"Meta"}), "Escape"),
        (frozenset({"Meta"}), ""),
        (frozenset(), "W"),
        (frozenset({"Shift"}), "Space"),
    ],
)
def test_the_constructor_enforces_the_same_rules(mods: frozenset[str], key: str) -> None:
    with pytest.raises(ValueError):
        KeyCombo(mods, key)


def test_combo_is_frozen_and_hashable() -> None:
    combo = parse("Meta+W")
    with pytest.raises(dataclasses.FrozenInstanceError):
        combo.key = "E"  # type: ignore[misc]
    assert len({combo, parse("w+super")}) == 1


PORTAL: dict[str, str] = {
    "Meta+Space": "LOGO+space",
    "Meta+Shift+W": "LOGO+SHIFT+w",
    "Meta+`": "LOGO+grave",
    "Ctrl+Alt+F12": "CTRL+ALT+F12",
    "Meta+Ctrl+Alt+Shift+X": "LOGO+CTRL+ALT+SHIFT+x",
    "Meta+1": "LOGO+1",
    "Ctrl++": "CTRL+plus",
    "Meta+-": "LOGO+minus",
    "Meta+=": "LOGO+equal",
    "Meta+[": "LOGO+bracketleft",
    "Meta+]": "LOGO+bracketright",
    "Meta+\\": "LOGO+backslash",
    "Meta+;": "LOGO+semicolon",
    "Meta+'": "LOGO+apostrophe",
    "Meta+,": "LOGO+comma",
    "Meta+.": "LOGO+period",
    "Meta+/": "LOGO+slash",
    "Meta+PageUp": "LOGO+Prior",
    "Meta+PageDown": "LOGO+Next",
    "Meta+Return": "LOGO+Return",
    "Meta+Backspace": "LOGO+BackSpace",
    "Alt+Tab": "ALT+Tab",
    "Ctrl+Delete": "CTRL+Delete",
    "Pause": "Pause",
    "Shift+F5": "SHIFT+F5",
}


@pytest.mark.parametrize("text", list(PORTAL))
def test_to_portal(text: str) -> None:
    assert parse(text).to_portal() == PORTAL[text]


QT: dict[str, int] = {
    "Meta+Space": META | 0x20,
    "Meta+Shift+W": META | SHIFT | 0x57,
    "Meta+`": META | 0x60,
    "Ctrl+Alt+F12": CTRL | ALT | 0x0100003B,
    "Meta+Ctrl+Alt+Shift+A": META | CTRL | ALT | SHIFT | 0x41,
    "Meta+Z": META | 0x5A,
    "Meta+0": META | 0x30,
    "Meta+9": META | 0x39,
    "Ctrl++": CTRL | 0x2B,
    "Meta+/": META | 0x2F,
    "F1": 0x01000030,
    "Shift+F24": SHIFT | 0x01000047,
    "Meta+Tab": META | 0x01000001,
    "Meta+Backspace": META | 0x01000003,
    "Meta+Return": META | 0x01000004,
    "Meta+Enter": META | 0x01000005,
    "Ctrl+Insert": CTRL | 0x01000006,
    "Ctrl+Alt+Delete": CTRL | ALT | 0x01000007,
    "Pause": 0x01000008,
    "Print": 0x01000009,
    "Meta+Home": META | 0x01000010,
    "Meta+End": META | 0x01000011,
    "Meta+Left": META | 0x01000012,
    "Meta+Up": META | 0x01000013,
    "Meta+Right": META | 0x01000014,
    "Meta+Down": META | 0x01000015,
    "Meta+PageUp": META | 0x01000016,
    "Meta+PageDown": META | 0x01000017,
}


@pytest.mark.parametrize("text", list(QT))
def test_to_qt_int(text: str) -> None:
    assert parse(text).to_qt_int() == QT[text]


def test_qt_ints_fit_a_signed_32_bit_dbus_int_and_are_distinct() -> None:
    combos = [parse(text) for text in {*ACCEPTED, *QT, *PORTAL}]
    values = {combo: combo.to_qt_int() for combo in combos}
    assert all(0 < value < 2**31 for value in values.values())
    assert len(set(values.values())) == len(set(combos))
    assert len({combo.to_portal() for combo in combos}) == len(set(combos))


def test_every_default_hotkey_parses_and_they_are_all_different() -> None:
    config = HotkeysConfig()
    combos = [parse(getattr(config, name)) for name in HotkeysConfig.model_fields]
    assert len(set(combos)) == len(combos) == 8
    assert parse(config.ask).to_portal() == "LOGO+space"
    assert parse(config.push_to_talk).to_portal() == "LOGO+grave"


def test_alternatives_for_a_letter() -> None:
    assert [str(c) for c in alternatives(parse("Meta+W"))] == [
        "Meta+Shift+W",
        "Meta+Alt+W",
        "Meta+Ctrl+W",
        "Meta+E",
        "Meta+Q",
    ]


def test_alternatives_keep_shift_and_skip_what_is_already_the_combo() -> None:
    assert [str(c) for c in alternatives(parse("Meta+Shift+W"))] == [
        "Meta+Alt+Shift+W",
        "Meta+Ctrl+Shift+W",
        "Meta+Shift+E",
        "Meta+Shift+Q",
        "Ctrl+Alt+Shift+W",
    ]


def test_alternatives_for_a_key_without_neighbours() -> None:
    assert [str(c) for c in alternatives(parse("Meta+Space"))] == [
        "Meta+Shift+Space",
        "Meta+Alt+Space",
        "Meta+Ctrl+Space",
        "Meta+Alt+Shift+Space",
        "Meta+Ctrl+Shift+Space",
    ]


def test_alternatives_at_the_end_of_a_row_have_one_neighbour() -> None:
    assert "Meta+O" in [str(c) for c in alternatives(parse("Meta+P"))]
    assert all(c.key in {"P", "O"} for c in alternatives(parse("Meta+P")))


@pytest.mark.parametrize("text", [*ACCEPTED])
def test_alternatives_are_deterministic_valid_distinct_and_capped(text: str) -> None:
    combo = parse(text)
    first = alternatives(combo)
    assert first == alternatives(combo)
    assert 1 <= len(first) <= keys.MAX_ALTERNATIVES
    assert combo not in first
    assert len(set(first)) == len(first)
    for candidate in first:
        assert parse(str(candidate)) == candidate  # each one is a legal shortcut


def test_alternatives_leave_out_taken_combos() -> None:
    taken = {parse("Meta+Shift+W"), parse("Meta+E")}
    result = alternatives(parse("Meta+W"), taken=taken)
    assert not taken & set(result)
    assert [str(c) for c in result] == [
        "Meta+Alt+W",
        "Meta+Ctrl+W",
        "Meta+Q",
        "Meta+Alt+Shift+W",
        "Meta+Ctrl+Shift+W",
    ]
