"""Shortcut strings from the config, as values (ARCHITECTURE §4.4 "Hotkeys"). Pure: no I/O.

``parse("Meta+Shift+W")`` gives a ``KeyCombo`` that renders to the two wire formats Waddle
needs: the XDG shortcuts trigger syntax for the GlobalShortcuts portal (``LOGO+SHIFT+w``) and
the Qt key integer that KGlobalAccel's read-only conflict queries take.

Rules (VISION: no modifier-only combos; ARCHITECTURE: Esc is never registered globally):

- a combo is zero or more modifiers plus exactly one key;
- a bare or Shift-only key is refused unless it is a function key, Pause or Print, because a
  global shortcut on a plain typing key would swallow that key in every application;
- Escape is refused in any combination.

The constants are written out here so this module does not import Qt.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

__all__ = [
    "MAX_ALTERNATIVES",
    "MODIFIERS",
    "KeyCombo",
    "alternatives",
    "parse",
]

MODIFIERS: Final = ("Meta", "Ctrl", "Alt", "Shift")
"""Canonical modifier names, in the order they are written."""

MAX_ALTERNATIVES: Final = 5
_MAX_TEXT: Final = 64

_MODIFIER_ALIASES: Final[dict[str, str]] = {
    "meta": "Meta",
    "super": "Meta",
    "win": "Meta",
    "windows": "Meta",
    "logo": "Meta",
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
}

# Qt::KeyboardModifier flags, as OR-ed into a QKeyCombination's combined value.
_QT_MODIFIERS: Final[dict[str, int]] = {
    "Shift": 0x02000000,
    "Ctrl": 0x04000000,
    "Alt": 0x08000000,
    "Meta": 0x10000000,
}

# The modifier names of the XDG shortcuts specification, in the order they are written.
_PORTAL_MODIFIERS: Final[tuple[tuple[str, str], ...]] = (
    ("Meta", "LOGO"),
    ("Ctrl", "CTRL"),
    ("Alt", "ALT"),
    ("Shift", "SHIFT"),
)

# Punctuation: the character, its xkb keysym name, and its Qt::Key value (which is the
# character's code point for all of Latin-1).
_PUNCTUATION: Final[dict[str, str]] = {
    "`": "grave",
    "-": "minus",
    "=": "equal",
    "[": "bracketleft",
    "]": "bracketright",
    "\\": "backslash",
    ";": "semicolon",
    "'": "apostrophe",
    ",": "comma",
    ".": "period",
    "/": "slash",
    "+": "plus",
}

# Named keys: canonical name -> (xkb keysym name, Qt::Key value).
_NAMED: Final[dict[str, tuple[str, int]]] = {
    "Space": ("space", 0x20),
    "Tab": ("Tab", 0x01000001),
    "Backspace": ("BackSpace", 0x01000003),
    "Return": ("Return", 0x01000004),
    "Enter": ("KP_Enter", 0x01000005),
    "Insert": ("Insert", 0x01000006),
    "Delete": ("Delete", 0x01000007),
    "Pause": ("Pause", 0x01000008),
    "Print": ("Print", 0x01000009),
    "Home": ("Home", 0x01000010),
    "End": ("End", 0x01000011),
    "Left": ("Left", 0x01000012),
    "Up": ("Up", 0x01000013),
    "Right": ("Right", 0x01000014),
    "Down": ("Down", 0x01000015),
    "PageUp": ("Prior", 0x01000016),
    "PageDown": ("Next", 0x01000017),
}
_QT_F1: Final = 0x01000030
_MAX_F: Final = 24

# Other spellings people use for the keys above (lower-cased).
_KEY_ALIASES: Final[dict[str, str]] = {
    **{name.lower(): name for name in _NAMED},
    "spacebar": "Space",
    "grave": "`",
    "backtick": "`",
    "quoteleft": "`",
    "minus": "-",
    "equal": "=",
    "equals": "=",
    "bracketleft": "[",
    "bracketright": "]",
    "backslash": "\\",
    "semicolon": ";",
    "apostrophe": "'",
    "comma": ",",
    "period": ".",
    "slash": "/",
    "plus": "+",
    "del": "Delete",
    "ins": "Insert",
    "pgup": "PageUp",
    "pgdown": "PageDown",
    "pgdn": "PageDown",
    "prior": "PageUp",
    "next": "PageDown",
    "printscreen": "Print",
    "prtsc": "Print",
}
_NEVER_GLOBAL: Final = frozenset({"esc", "escape"})

# Keys that may be bound without Meta, Ctrl or Alt: nobody types with them.
_BARE_OK: Final = frozenset({"Pause", "Print"})

# Neighbours on a QWERTY board, used to propose a nearby letter.
_ROWS: Final = ("QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM")


def _is_function_key(key: str) -> bool:
    return (
        len(key) >= 2
        and key[0] == "F"
        and key[1:].isdecimal()
        and key[1] != "0"
        and 1 <= int(key[1:]) <= _MAX_F
    )


def _canonical_key(token: str) -> str:
    """The canonical name of one key token, or ``ValueError``."""
    if len(token) == 1:
        if token.isascii() and token.isalpha():
            return token.upper()
        if token.isascii() and token.isdecimal():
            return token
        if token in _PUNCTUATION:
            return token
        raise ValueError("unknown key")
    lowered = token.lower()
    if lowered in _NEVER_GLOBAL:
        raise ValueError("Escape is never registered as a global shortcut")
    if lowered in _KEY_ALIASES:
        return _KEY_ALIASES[lowered]
    upper = token.upper()
    if _is_function_key(upper):
        return upper
    raise ValueError("unknown key")


@dataclass(frozen=True)
class KeyCombo:
    """Modifiers plus one key, both canonical. Build with ``parse``; the constructor checks.

    ``mods`` is a subset of ``MODIFIERS``. ``key`` is an upper-case letter, a digit, one of
    the punctuation characters, ``F1``..``F24`` or a named key such as ``Space``.
    """

    mods: frozenset[str]
    key: str

    def __post_init__(self) -> None:
        if not isinstance(self.mods, frozenset) or not self.mods <= set(MODIFIERS):
            raise ValueError("unknown modifier")
        if not isinstance(self.key, str) or not _is_canonical(self.key):
            raise ValueError("unknown key")
        strong = self.mods & {"Meta", "Ctrl", "Alt"}
        if not strong and not (_is_function_key(self.key) or self.key in _BARE_OK):
            raise ValueError("a typing key needs Meta, Ctrl or Alt")

    def __str__(self) -> str:
        return "+".join([m for m in MODIFIERS if m in self.mods] + [self.key])

    def to_portal(self) -> str:
        """The XDG shortcuts trigger string, e.g. ``LOGO+SHIFT+w`` (``preferred_trigger``).

        UNVERIFIED (S2.4): that xdg-desktop-portal-kde 6.6 parses ``preferred_trigger`` in
        this syntax. It is a hint only; the portal's answer says what was really bound.
        """
        parts = [portal for name, portal in _PORTAL_MODIFIERS if name in self.mods]
        return "+".join([*parts, _portal_key(self.key)])

    def to_qt_int(self) -> int:
        """Qt::Key value OR-ed with Qt::KeyboardModifier flags (QKeyCombination::toCombined).

        UNVERIFIED (S2.4): for Shift plus punctuation, KGlobalAccel may store the shifted
        symbol (Meta+~) instead of Meta+Shift+`; such a combo could be missed by a conflict
        query. Letters, digits and named keys are not affected.
        """
        code = _qt_key(self.key)
        for name in self.mods:
            code |= _QT_MODIFIERS[name]
        return code


def _is_canonical(key: str) -> bool:
    if len(key) == 1:
        return (key.isascii() and (key.isupper() or key.isdecimal())) or key in _PUNCTUATION
    return key in _NAMED or _is_function_key(key)


def _portal_key(key: str) -> str:
    if len(key) == 1:
        if key in _PUNCTUATION:
            return _PUNCTUATION[key]
        return key.lower()  # letters are lower-case keysyms; digits are their own name
    if key in _NAMED:
        return _NAMED[key][0]
    return key  # F1..F24


def _qt_key(key: str) -> int:
    if len(key) == 1:
        return ord(key)  # Qt::Key_A..Z, Key_0..9 and Latin-1 punctuation are code points
    if key in _NAMED:
        return _NAMED[key][1]
    return _QT_F1 + int(key[1:]) - 1


def parse(text: str) -> KeyCombo:
    """Parse a shortcut such as ``"Meta+Space"``, ``"shift + super + w"`` or ``"Ctrl++"``.

    Modifiers may come in any order and case. Raises ``ValueError`` for anything else:
    modifier-only combos, unknown or repeated tokens, two keys, Escape, or a typing key with
    no Meta/Ctrl/Alt. The message never repeats the input.
    """
    if not isinstance(text, str):
        raise ValueError("a shortcut must be a string")
    if len(text) > _MAX_TEXT:
        raise ValueError("shortcut is too long")
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty shortcut")
    if stripped == "+":
        tokens = ["+"]
    elif stripped.endswith("+") and stripped[:-1].rstrip().endswith("+"):
        tokens = [*stripped[:-1].rstrip()[:-1].split("+"), "+"]  # "Ctrl++": the key is plus
    else:
        tokens = stripped.split("+")
    mods: set[str] = set()
    key: str | None = None
    for raw in tokens:
        token = raw.strip()
        if not token:
            raise ValueError("empty part in shortcut")
        modifier = _MODIFIER_ALIASES.get(token.lower())
        if modifier is not None:
            if modifier in mods:
                raise ValueError("modifier given twice")
            mods.add(modifier)
            continue
        if key is not None:
            raise ValueError("a shortcut has exactly one key")
        key = _canonical_key(token)
    if key is None:
        raise ValueError("modifier-only shortcuts are not allowed")
    return KeyCombo(frozenset(mods), key)


def _neighbours(key: str) -> list[str]:
    for row in _ROWS:
        index = row.find(key)
        if index >= 0 and len(key) == 1:
            return [row[i] for i in (index + 1, index - 1) if 0 <= i < len(row)]
    return []


def alternatives(combo: KeyCombo, taken: Collection[KeyCombo] = ()) -> list[KeyCombo]:
    """Up to ``MAX_ALTERNATIVES`` nearby combos to offer when ``combo`` is taken.

    Deterministic, in order of preference: the same key with Shift added, with Meta+Alt,
    with Meta+Ctrl; then the neighbouring letters with the original modifiers; then wider
    modifier sets. ``combo`` itself and anything in ``taken`` are left out. These are
    *candidates*: whether one is free is a question for ``hotkeys.find_conflicts``.
    """
    shift = combo.mods & {"Shift"}
    variants: list[tuple[frozenset[str], str]] = [
        (combo.mods | {"Shift"}, combo.key),
        (frozenset({"Meta", "Alt"}) | shift, combo.key),
        (frozenset({"Meta", "Ctrl"}) | shift, combo.key),
    ]
    variants += [(combo.mods, letter) for letter in _neighbours(combo.key)]
    variants += [
        (frozenset({"Meta", "Alt", "Shift"}), combo.key),
        (frozenset({"Meta", "Ctrl", "Shift"}), combo.key),
        (frozenset({"Ctrl", "Alt"}) | shift, combo.key),
        (frozenset({"Ctrl", "Alt", "Shift"}), combo.key),
        (frozenset({"Meta", "Ctrl", "Alt"}), combo.key),
    ]
    excluded = {combo, *taken}
    found: list[KeyCombo] = []
    for mods, key in variants:
        try:
            candidate = KeyCombo(frozenset(mods), key)
        except ValueError:
            continue
        if candidate in excluded:
            continue
        excluded.add(candidate)
        found.append(candidate)
        if len(found) == MAX_ALTERNATIVES:
            break
    return found
