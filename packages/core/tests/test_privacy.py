import itertools
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from waddle_core.identity import APP_ID
from waddle_core.privacy import (
    MAX_BLOCKLIST_BYTES,
    Allow,
    Blind,
    BlindReason,
    Blocklist,
    BlocklistError,
    BlockMatch,
    SessionState,
    gate,
    load_blocklist,
    normalize,
)
from waddle_core.window import Geometry, WindowInfo

REPO = Path(__file__).resolve().parents[3]
DEFAULTS_PATH = REPO / "knowledge" / "privacy" / "default-blocklist.json"
CASES_PATH = Path(__file__).with_name("blocklist_cases.json")

_GEO = Geometry(0.0, 0.0, 800.0, 600.0)


def window(
    resource_class: str = "blender",
    resource_name: str = "blender",
    desktop_file: str = "blender",
    caption: str = "scene.blend",
    *,
    is_own: bool = False,
) -> WindowInfo:
    return WindowInfo(
        uuid="{11111111-2222-3333-4444-555555555555}",
        caption=caption,
        resource_class=resource_class,
        resource_name=resource_name,
        desktop_file=desktop_file,
        pid=4242,
        frame=_GEO,
        client=_GEO,
        output="DP-1",
        scale=1.0,
        fullscreen=False,
        xwayland=False,
        is_own=is_own,
    )


OPEN = SessionState(locked=False, paused=False, idle=False)


# --- Blocklist parsing ---------------------------------------------------------------------


def test_from_json_reads_both_lists_and_ignores_underscore_keys() -> None:
    text = '{"_comment": "x", "_other": [1], "class": ["KeePass"], "title": ["Incognito"]}'
    assert Blocklist.from_json(text) == Blocklist(("keepass",), ("incognito",))


def test_from_json_drops_empty_and_whitespace_only_patterns() -> None:
    bl = Blocklist.from_json('{"class": ["", "  ", "\\t\\n", "a"], "title": ["", "\\u3000"]}')
    assert bl.classes == ("a",)
    assert bl.titles == ()
    # The point of dropping them: an empty pattern is a substring of every string.
    assert bl.match(window("zzz", "zzz", "zzz", "zzz")) is None


def test_patterns_keep_inner_and_edge_spaces() -> None:
    bl = Blocklist.from_json('{"class": [], "title": ["log in "]}')
    assert bl.titles == ("log in ",)
    assert bl.match(window(caption="Log in here")) is not None
    assert bl.match(window(caption="log index")) is None


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '"keepass"',
        "null",
        '{"class": ["a"]}',
        '{"title": ["a"]}',
        '{"class": "keepass", "title": []}',
        '{"class": [], "title": {"a": 1}}',
        '{"class": [1], "title": []}',
        '{"class": [], "title": ["ok", null]}',
        '{"class": [["nested"]], "title": []}',
        '{"class": [true], "title": []}',
        '{"class": [], "title": [], "titles": ["typo"]}',
        "[" * 100_000,
    ],
)
def test_from_json_rejects_malformed_input(text: str) -> None:
    with pytest.raises(BlocklistError):
        Blocklist.from_json(text)


def test_constructor_validates_and_normalizes() -> None:
    assert Blocklist(
        ("KeePass", " "), ("\uff29\uff2e\uff23\uff2f\uff27\uff2e\uff29\uff34\uff2f",)
    ) == Blocklist(("keepass",), ("incognito",))
    with pytest.raises(BlocklistError):
        Blocklist((1,), ())  # type: ignore[arg-type]
    with pytest.raises(BlocklistError):
        Blocklist("keepass", ())  # type: ignore[arg-type]


def test_merged_appends_and_never_removes() -> None:
    base = Blocklist(("keepass",), ("incognito",))
    merged = base.merged(["MyBank", ""], ("Payroll", "  "))
    assert merged.classes == ("keepass", "mybank")
    assert merged.titles == ("incognito", "payroll")
    assert base.classes == ("keepass",), "the original is unchanged"
    assert base.merged() == base
    assert merged.match(window("keepassxc")) == BlockMatch("class", 0)
    assert merged.match(window("com.example.mybank")) == BlockMatch("class", 1)
    assert merged.match(window(caption="PAYROLL.ods")) == BlockMatch("title", 1)


def test_merged_rejects_bad_extras() -> None:
    base = Blocklist(("keepass",), ())
    with pytest.raises(BlocklistError):
        base.merged([3])  # type: ignore[list-item]
    with pytest.raises(BlocklistError):
        base.merged("mybank")  # a bare string would become one pattern per letter
    with pytest.raises(BlocklistError):
        base.merged((), "payroll")


# --- Matching ------------------------------------------------------------------------------


def test_class_patterns_test_all_three_identifiers_but_not_the_caption() -> None:
    bl = Blocklist(("secret",), ())
    assert bl.match(window("org.Secret.App", "x", "x", "x")) == BlockMatch("class", 0)
    assert bl.match(window("x", "SECRETS", "x", "x")) == BlockMatch("class", 0)
    assert bl.match(window("x", "x", "my-secret", "x")) == BlockMatch("class", 0)
    assert bl.match(window("x", "x", "x", "secret")) is None


def test_title_patterns_test_only_the_caption() -> None:
    bl = Blocklist((), ("secret",))
    assert bl.match(window("x", "x", "x", "Top SECRET plans")) == BlockMatch("title", 0)
    assert bl.match(window("secret", "secret", "secret", "plans")) is None


def test_match_reports_first_pattern_and_class_before_title() -> None:
    bl = Blocklist(("aaa", "pass", "keepass"), ("data", "base"))
    assert bl.match(window("keepassxc", caption="database")) == BlockMatch("class", 1)
    assert bl.match(window("kate", "kate", "kate", "database")) == BlockMatch("title", 0)
    assert bl.match(window("kate", "kate", "kate", "baseline")) == BlockMatch("title", 1)


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        ("incognito", "\uff29\uff2e\uff23\uff2f\uff27\uff2e\uff29\uff34\uff2f"),  # full-width
        (
            "\uff49\uff4e\uff43\uff4f\uff47\uff4e\uff49\uff54\uff4f",
            "Incognito",
        ),  # full-width pattern, plain text
        ("stra\u00dfe", "STRASSE"),  # case folding, not just lowercasing
        ("strasse", "Stra\u00dfe"),
        ("r\u00e9sum\u00e9", "re\u0301sume\u0301"),  # decomposed accents
        ("finance", "\ufb01nance"),  # fi ligature
        ("2fa", "\u2461FA"),  # circled digit
        ("incognito", "In\u00adcog\u200bni\u200dto"),  # soft hyphen, zero-width space, joiner
        ("in\u200bcognito", "Incognito"),  # the same, in the pattern
        ("sign in", "\u2068Sign\u2069 in\ufeff"),  # bidi isolates, byte-order mark
        ("\u01c6", "\u01c4"),  # digraph with three case forms
    ],
)
def test_unicode_variants_do_not_evade_a_pattern(pattern: str, text: str) -> None:
    assert Blocklist((), (pattern,)).match(window(caption=f"x {text} y")) is not None
    assert Blocklist((pattern,), ()).match(window(f"x.{text}.y")) is not None


def test_normalize_is_idempotent_on_awkward_input() -> None:
    for text in [
        "\u01c5",
        "\ufb03",
        "\u0130stanbul",
        "\u0390",
        "\u1e9e",
        "\uff21\uff22\uff23",
        "re\u0301",
        "\u0345\u03a9",
        "e\u200b\u0301",
        "\u00ad\u200d\ufe0f",
        "a\U000e0041b",
    ]:
        once = normalize(text)
        assert normalize(once) == once


def test_block_match_never_carries_window_text() -> None:
    marker = "ZQ-very-private-caption-ZQ"
    bl = Blocklist(("zq-class",), ("private",))
    for win in (window(caption=marker), window("ZQ-class-marker", caption=marker)):
        result = gate(win, OPEN, bl)
        assert isinstance(result, Blind)
        rendered = repr(result) + str(result.match)
        assert "ZQ" not in rendered and "zq" not in rendered
        assert result.match is not None
        assert set(vars(result.match)) == {"field", "pattern_index"}


# --- The shipped defaults and the shared conformance fixture -----------------------------------


def test_default_blocklist_file_loads_and_is_not_empty() -> None:
    bl = load_blocklist(DEFAULTS_PATH)
    assert "keepassxc" in bl.classes and "incognito" in bl.titles
    assert len(bl.classes) >= 20 and len(bl.titles) >= 30
    assert all(p.strip() for p in bl.classes + bl.titles)


def _cases() -> list[dict[str, Any]]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert isinstance(cases, list)
    return cases


def _case_window(case: dict[str, Any]) -> WindowInfo:
    return window(case["class"], case["name"], case["desktop"], case["title"])


def _lowercase_only_verdict(case: dict[str, Any], defaults: dict[str, list[str]]) -> str:
    """An independent reference: plain lowercase substring, the helper's documented rule."""
    classes = [p.lower() for p in defaults["class"] + case["extra_classes"] if p.strip()]
    titles = [p.lower() for p in defaults["title"] + case["extra_titles"] if p.strip()]
    idents = [case["class"].lower(), case["name"].lower(), case["desktop"].lower()]
    hit = any(p in i for p in classes for i in idents) or any(
        p in case["title"].lower() for p in titles
    )
    return "blocked" if hit else "allowed"


def test_conformance_fixture_is_well_formed() -> None:
    cases = _cases()
    assert len(cases) >= 25
    ids = [c["id"] for c in cases]
    assert len(set(ids)) == len(ids)
    required = {"class", "name", "desktop", "title", "extra_classes", "extra_titles", "expect"}
    for case in cases:
        assert required <= set(case), case["id"]
        assert set(case) <= required | {"id", "unicode"}, case["id"]
        assert case["expect"] in ("blocked", "allowed"), case["id"]
        assert all(isinstance(case[k], str) for k in ("class", "name", "desktop", "title"))
        assert all(isinstance(p, str) for p in case["extra_classes"] + case["extra_titles"])
        # Own windows are decided by the gate, not by the pattern matcher.
        assert APP_ID.lower() not in (case["class"] + case["name"] + case["desktop"]).lower()
    verdicts = [c["expect"] for c in cases]
    assert verdicts.count("blocked") >= 10 and verdicts.count("allowed") >= 10


@pytest.mark.parametrize("case", _cases(), ids=lambda c: str(c["id"]))
def test_conformance_case(case: dict[str, Any]) -> None:
    bl = load_blocklist(DEFAULTS_PATH).merged(case["extra_classes"], case["extra_titles"])
    verdict = "blocked" if bl.match(_case_window(case)) is not None else "allowed"
    assert verdict == case["expect"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: str(c["id"]))
def test_conformance_unicode_flag_is_honest(case: dict[str, Any]) -> None:
    """Unflagged cases need nothing beyond lowercasing; flagged ones really do."""
    defaults = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    simple = _lowercase_only_verdict(case, defaults)
    if case.get("unicode", False):
        assert simple != case["expect"]
    else:
        assert simple == case["expect"]


# --- load_blocklist ------------------------------------------------------------------------


def test_load_blocklist_errors_propagate(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_blocklist(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'{"class": ["\xff"], "title": []}')
    with pytest.raises(BlocklistError):
        load_blocklist(bad)
    big = tmp_path / "big.json"
    big.write_text('{"class": [], "title": []}' + " " * MAX_BLOCKLIST_BYTES)
    with pytest.raises(BlocklistError):
        load_blocklist(big)
    good = tmp_path / "good.json"
    good.write_text('{"class": ["A"], "title": []}', encoding="utf-8")
    assert load_blocklist(good) == Blocklist(("a",), ())


# --- gate ----------------------------------------------------------------------------------

BLOCKLIST = Blocklist(("keepass",), ("incognito",))
WINDOWS: dict[str, WindowInfo | None] = {
    "none": None,
    "plain": window(),
    "own": window(is_own=True),
    "blocked-class": window("org.keepassxc.KeePassXC"),
    "blocked-title": window(caption="New Tab (Incognito)"),
    "own-and-blocked": window("keepassxc", is_own=True),
}


def test_gate_exhaustive_truth_table() -> None:
    allowed = 0
    combos = itertools.product([False, True], repeat=4)
    for (locked, paused, idle, lock_known), (name, win) in itertools.product(
        combos, WINDOWS.items()
    ):
        session = SessionState(locked=locked, paused=paused, idle=idle, lock_known=lock_known)
        result = gate(win, session, BLOCKLIST)
        may_look = not locked and lock_known and not paused and name == "plain"
        assert isinstance(result, Allow) == may_look, (session, name)
        if isinstance(result, Allow):
            allowed += 1
            continue
        # Precedence: locked > unknown lock > paused > no window > own > blocked.
        if locked:
            expected = BlindReason.LOCKED
        elif not lock_known:
            expected = BlindReason.UNKNOWN_LOCK
        elif paused:
            expected = BlindReason.PAUSED
        elif win is None:
            expected = BlindReason.NO_WINDOW
        elif win.is_own:
            expected = BlindReason.OWN_WINDOW
        else:
            expected = BlindReason.BLOCKED
        assert result.reason is expected, (session, name)
        assert (result.match is not None) == (expected is BlindReason.BLOCKED)
    # unlocked, known, unpaused, plain window; idle either way.
    assert allowed == 2


def test_gate_idle_alone_does_not_blind() -> None:
    assert gate(window(), SessionState(False, False, idle=True), BLOCKLIST) == Allow()


def test_gate_lock_known_defaults_to_true_and_unknown_fails_closed() -> None:
    assert SessionState(False, False, False).lock_known is True
    unknown = SessionState(locked=False, paused=False, idle=False, lock_known=False)
    assert gate(window(), unknown, Blocklist()) == Blind(BlindReason.UNKNOWN_LOCK)


def test_gate_reports_which_pattern_blocked() -> None:
    assert gate(WINDOWS["blocked-class"], OPEN, BLOCKLIST) == Blind(
        BlindReason.BLOCKED, BlockMatch("class", 0)
    )
    assert gate(WINDOWS["blocked-title"], OPEN, BLOCKLIST) == Blind(
        BlindReason.BLOCKED, BlockMatch("title", 0)
    )


@pytest.mark.parametrize("field", ["resource_class", "resource_name", "desktop_file"])
def test_gate_recognises_own_app_id_even_without_the_flag(field: str) -> None:
    base = window("python3", "python3", "python3")
    assert gate(base, OPEN, Blocklist()) == Allow()
    for value in (APP_ID, APP_ID.upper()):
        own = replace(base, **{field: value})
        assert gate(own, OPEN, Blocklist()) == Blind(BlindReason.OWN_WINDOW)
    # Exact id only: another app is not Waddle because its id starts the same way.
    lookalike = replace(base, **{field: APP_ID + "Viewer"})
    assert gate(lookalike, OPEN, Blocklist()) == Allow()


def test_gate_with_empty_blocklist_still_blinds_on_session_state() -> None:
    assert gate(window(), SessionState(True, False, False), Blocklist()) == Blind(
        BlindReason.LOCKED
    )
    assert gate(window(), SessionState(False, True, False), Blocklist()) == Blind(
        BlindReason.PAUSED
    )
    assert gate(None, OPEN, Blocklist()) == Blind(BlindReason.NO_WINDOW)


def test_results_are_immutable() -> None:
    blind = Blind(BlindReason.LOCKED)
    with pytest.raises(AttributeError):
        blind.reason = BlindReason.PAUSED  # type: ignore[misc]
    with pytest.raises(AttributeError):
        BLOCKLIST.classes = ()  # type: ignore[misc]
