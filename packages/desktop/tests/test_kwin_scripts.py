"""KWin scripts, executed for real under Node against fake KWin globals (tests/js/kwin_mock.js).

The point of most of these tests is the read-only guarantee: a script may only ever report
or move a window that has Waddle's class AND one of Waddle's two exact titles.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from waddle_core.identity import (
    APP_ID,
    DBUS_KWIN_INTERFACE,
    DBUS_PATH,
    DBUS_SERVICE,
    OVERLAY_WINDOW_TITLE,
    PENGUIN_WINDOW_TITLE,
)
from waddle_desktop.kwin import types
from waddle_desktop.kwin.scripts import (
    COORD_LIMIT,
    CORE_SCRIPT_NAME,
    OWN_GUARD_JS,
    core_script_path,
    render_move_script,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_SCRIPT = core_script_path(REPO_ROOT)
MOCK = Path(__file__).parent / "js" / "kwin_mock.js"
NODE = shutil.which("node") or shutil.which("node", path="/opt/node22/bin")

needs_node = pytest.mark.skipif(NODE is None, reason="node not found; cannot execute KWin JS")

SCREENS = [
    {"name": "DP-1", "geometry": {"x": 0, "y": 0, "width": 2560, "height": 1440}},
    {"name": "HDMI-A-1", "geometry": {"x": 2560, "y": 0, "width": 1920, "height": 1080}},
]


def window(id_: str, cls: str, caption: str, x: int = 100, y: int = 200) -> dict[str, Any]:
    return {
        "id": id_,
        "resourceClass": cls,
        "caption": caption,
        "frameGeometry": {"x": x, "y": y, "width": 180, "height": 220},
        "output": "DP-1",
    }


def penguin(x: int = 100, y: int = 200) -> dict[str, Any]:
    return window("penguin", APP_ID, PENGUIN_WINDOW_TITLE, x, y)


# Windows that must never be reported or modified, whatever else is on screen.
BYSTANDERS = [
    window("editor", "org.kde.kate", "secret-notes.txt — Kate", 10, 10),
    window("decoy", "org.example.Decoy", PENGUIN_WINDOW_TITLE, 20, 20),
    window("settings", APP_ID, "Waddle Settings", 30, 30),
    window("empty", "", "", 40, 40),
]


def run_js(script: Path, **scenario: Any) -> dict[str, Any]:
    assert NODE is not None
    scenario = {"screens": SCREENS, "activeScreen": "DP-1", **scenario, "script": str(script)}
    proc = subprocess.run(
        [NODE, str(MOCK)],
        input=json.dumps(scenario),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result: dict[str, Any] = json.loads(proc.stdout)
    assert result["error"] is None, result["error"]
    for call in result["calls"]:  # every callback goes to Waddle's own object, one string arg
        assert (call["service"], call["path"], call["interface"]) == (
            DBUS_SERVICE,
            DBUS_PATH,
            DBUS_KWIN_INTERFACE,
        )
        assert len(call["args"]) == 1 and isinstance(call["args"][0], str)
    return result


def payloads(result: dict[str, Any], method: str) -> list[Any]:
    return [json.loads(c["args"][0]) for c in result["calls"] if c["method"] == method]


def geometry(result: dict[str, Any], id_: str) -> dict[str, int]:
    return next(w["frameGeometry"] for w in result["windows"] if w["id"] == id_)


def assert_untouched(result: dict[str, Any], windows: list[dict[str, Any]]) -> None:
    ids = {w["id"] for w in windows}
    assert [w for w in result["writes"] if w["id"] in ids] == []
    for w in windows:
        assert geometry(result, w["id"]) == w["frameGeometry"]


# --- static checks -----------------------------------------------------------------------


def test_core_script_path_and_shared_guard() -> None:
    assert CORE_SCRIPT == REPO_ROOT / "kwin" / CORE_SCRIPT_NAME
    source = CORE_SCRIPT.read_text(encoding="utf-8")
    assert OWN_GUARD_JS in source, "waddle-core.js must contain OWN_GUARD_JS verbatim"
    assert OWN_GUARD_JS in render_move_script(PENGUIN_WINDOW_TITLE, 0, 0)
    assert json.dumps(APP_ID.lower()) in OWN_GUARD_JS
    assert json.dumps([PENGUIN_WINDOW_TITLE, OVERLAY_WINDOW_TITLE]) in OWN_GUARD_JS


@pytest.mark.parametrize("token", ["?.", "=>", "`", "async ", "await ", "import "])
def test_scripts_use_conservative_javascript(token: str) -> None:
    assert token not in CORE_SCRIPT.read_text(encoding="utf-8")
    assert token not in render_move_script(OVERLAY_WINDOW_TITLE, -5, 7)


def test_core_script_never_assigns_window_geometry() -> None:
    source = CORE_SCRIPT.read_text(encoding="utf-8")
    assert "frameGeometry =" not in source and "frameGeometry=" not in source


# --- core script -------------------------------------------------------------------------


@needs_node
def test_core_reports_hello_outputs_and_penguin() -> None:
    result = run_js(CORE_SCRIPT, windows=[penguin(), *BYSTANDERS])

    assert [c["method"] for c in result["calls"]] == ["Hello", "Outputs", "OwnWindow"]
    assert payloads(result, "Hello") == [{"version": 1, "apiOk": True}]
    assert payloads(result, "OwnWindow") == [
        {
            "title": PENGUIN_WINDOW_TITLE,
            "uuid": "penguin",
            "x": 100,
            "y": 200,
            "width": 180,
            "height": 220,
            "output": "DP-1",
        }
    ]
    assert result["writes"] == []
    # The payloads are exactly what the host parsers accept.
    by_method = {c["method"]: c["args"][0] for c in result["calls"]}
    assert types.parse_hello(by_method["Hello"]) == types.Hello(version=1, api_ok=True)
    assert types.parse_own_window(by_method["OwnWindow"]) == types.OwnWindow(
        PENGUIN_WINDOW_TITLE, "penguin", 100, 200, 180, 220, "DP-1"
    )
    assert types.parse_outputs(by_method["Outputs"]) == (
        [
            types.OutputInfo("DP-1", 0, 0, 2560, 1440),
            types.OutputInfo("HDMI-A-1", 2560, 0, 1920, 1080),
        ],
        "DP-1",
    )


@needs_node
def test_core_reports_moves_and_coalesces_unchanged_geometry() -> None:
    moved = {"x": 640, "y": 480, "width": 180, "height": 220}
    result = run_js(
        CORE_SCRIPT,
        windows=[penguin()],
        events=[
            {"type": "geometry", "id": "penguin"},  # signal fired, nothing changed
            {"type": "geometry", "id": "penguin", "frameGeometry": moved},
            {"type": "geometry", "id": "penguin", "frameGeometry": moved},  # same again
            {"type": "output", "id": "penguin", "output": "HDMI-A-1"},
            {"type": "output", "id": "penguin", "output": "HDMI-A-1"},
        ],
    )
    reports = payloads(result, "OwnWindow")
    assert [(r["x"], r["y"], r["output"]) for r in reports] == [
        (100, 200, "DP-1"),
        (640, 480, "DP-1"),
        (640, 480, "HDMI-A-1"),
    ]
    assert result["writes"] == []


@needs_node
def test_core_rounds_fractional_geometry() -> None:
    win = penguin()
    win["frameGeometry"] = {"x": 10.4, "y": 20.6, "width": 180.5, "height": 219.5}
    report = payloads(run_js(CORE_SCRIPT, windows=[win]), "OwnWindow")[0]
    assert all(isinstance(report[k], int) for k in ("x", "y", "width", "height"))
    assert (report["x"], report["y"]) == (10, 21)


@needs_node
def test_core_never_reports_or_modifies_other_windows() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=BYSTANDERS,
        events=[
            *({"type": "geometry", "id": w["id"]} for w in BYSTANDERS),
            {
                "type": "geometry",
                "id": "editor",
                "frameGeometry": {"x": 1, "y": 2, "width": 3, "height": 4},
            },
            {"type": "caption", "id": "editor", "caption": "passwords.kdbx — Kate"},
            {"type": "add", "window": window("late", "firefox", "My Bank — Firefox")},
            {"type": "remove", "id": "decoy"},
        ],
    )
    assert [c["method"] for c in result["calls"]] == ["Hello", "Outputs"]
    assert result["writes"] == []
    everything = json.dumps(result["calls"])
    for leaked in ("Kate", "kate", "Firefox", "firefox", "Decoy", "Settings", "Bank"):
        assert leaked not in everything


@needs_node
def test_core_reports_overlay_title_window_only_as_penguin() -> None:
    # Phase 1 reports the penguin only; the overlay is Waddle's but is not reported.
    result = run_js(CORE_SCRIPT, windows=[window("overlay", APP_ID, OVERLAY_WINDOW_TITLE)])
    assert payloads(result, "OwnWindow") == []


@needs_node
@pytest.mark.parametrize("cls", [APP_ID.lower(), APP_ID.upper(), APP_ID])
def test_core_class_comparison_is_case_insensitive(cls: str) -> None:
    result = run_js(CORE_SCRIPT, windows=[window("p", cls, PENGUIN_WINDOW_TITLE)])
    assert [r["uuid"] for r in payloads(result, "OwnWindow")] == ["p"]


@needs_node
def test_core_title_comparison_is_exact() -> None:
    windows = [
        window("lower", APP_ID, PENGUIN_WINDOW_TITLE.lower()),
        window("padded", APP_ID, PENGUIN_WINDOW_TITLE + " "),
    ]
    assert payloads(run_js(CORE_SCRIPT, windows=windows), "OwnWindow") == []


@needs_node
def test_core_picks_up_window_added_later_and_tracks_it() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[],
        events=[
            {"type": "add", "window": penguin(5, 6)},
            {
                "type": "geometry",
                "id": "penguin",
                "frameGeometry": {"x": 7, "y": 8, "width": 180, "height": 220},
            },
        ],
    )
    assert [(r["x"], r["y"]) for r in payloads(result, "OwnWindow")] == [(5, 6), (7, 8)]


@needs_node
def test_core_picks_up_caption_set_after_window_added() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[],
        events=[
            {"type": "add", "window": window("penguin", APP_ID, "")},
            {"type": "caption", "id": "penguin", "caption": PENGUIN_WINDOW_TITLE},
        ],
    )
    assert [r["uuid"] for r in payloads(result, "OwnWindow")] == ["penguin"]


@needs_node
def test_core_reports_again_for_a_recreated_window() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[penguin()],
        events=[{"type": "remove", "id": "penguin"}, {"type": "add", "window": penguin()}],
    )
    assert len(payloads(result, "OwnWindow")) == 2


@needs_node
def test_core_reports_outputs_again_when_screens_change() -> None:
    result = run_js(CORE_SCRIPT, windows=[], events=[{"type": "screens", "screens": SCREENS[:1]}])
    reports = payloads(result, "Outputs")
    assert [len(r["outputs"]) for r in reports] == [2, 1]
    assert reports[1] == {
        "outputs": [{"name": "DP-1", "x": 0, "y": 0, "width": 2560, "height": 1440}],
        "active": "DP-1",
    }


@needs_node
def test_core_survives_missing_optional_apis() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[penguin()],
        omit=[
            "screensChanged",
            "activeScreen",
            "windowRemoved",
            "window.outputChanged",
            "window.captionChanged",
        ],
        events=[
            {
                "type": "geometry",
                "id": "penguin",
                "frameGeometry": {"x": 1, "y": 2, "width": 180, "height": 220},
            }
        ],
    )
    assert payloads(result, "Hello") == [{"version": 1, "apiOk": True}]
    assert payloads(result, "Outputs")[0]["active"] is None
    assert [(r["x"], r["y"]) for r in payloads(result, "OwnWindow")] == [(100, 200), (1, 2)]


@needs_node
def test_core_says_api_not_ok_when_required_apis_are_missing() -> None:
    result = run_js(CORE_SCRIPT, windows=[penguin()], omit=["windowList", "windowAdded", "screens"])
    assert payloads(result, "Hello") == [{"version": 1, "apiOk": False}]
    assert payloads(result, "OwnWindow") == []


# --- move script -------------------------------------------------------------------------


def write_move_script(tmp_path: Path, title: str, x: int, y: int) -> Path:
    path = tmp_path / "move.js"
    path.write_text(render_move_script(title, x, y), encoding="utf-8")
    return path


@needs_node
def test_move_script_moves_only_the_titled_waddle_window(tmp_path: Path) -> None:
    script = write_move_script(tmp_path, PENGUIN_WINDOW_TITLE, -300, 77)
    overlay = window("overlay", APP_ID, OVERLAY_WINDOW_TITLE, 0, 0)
    result = run_js(script, windows=[*BYSTANDERS, overlay, penguin()])

    assert geometry(result, "penguin") == {"x": -300, "y": 77, "width": 180, "height": 220}
    assert result["writes"] == [{"id": "penguin", "prop": "frameGeometry"}]
    assert_untouched(result, [*BYSTANDERS, overlay])
    assert [c["method"] for c in result["calls"]] == ["MoveDone"]
    done = result["calls"][0]["args"][0]
    assert types.parse_move_done(done) == types.MoveDone(PENGUIN_WINDOW_TITLE, True, -300, 77)


@needs_node
def test_move_script_can_move_the_overlay_and_leaves_the_penguin(tmp_path: Path) -> None:
    script = write_move_script(tmp_path, OVERLAY_WINDOW_TITLE, 2560, 0)
    overlay = window("overlay", APP_ID.lower(), OVERLAY_WINDOW_TITLE, 0, 0)
    result = run_js(script, windows=[penguin(), overlay])
    assert geometry(result, "overlay") == {"x": 2560, "y": 0, "width": 180, "height": 220}
    assert_untouched(result, [penguin()])


@needs_node
def test_move_script_without_a_waddle_window_changes_nothing(tmp_path: Path) -> None:
    script = write_move_script(tmp_path, PENGUIN_WINDOW_TITLE, 50, 60)
    result = run_js(script, windows=BYSTANDERS)
    assert result["writes"] == []
    assert_untouched(result, BYSTANDERS)
    assert payloads(result, "MoveDone") == [
        {"title": PENGUIN_WINDOW_TITLE, "ok": False, "x": 50, "y": 60}
    ]


@needs_node
def test_move_script_does_not_move_a_decoy_of_another_class(tmp_path: Path) -> None:
    """The read-only test: same caption, different application -> never touched."""
    decoys = [
        window("decoy", "org.example.Decoy", PENGUIN_WINDOW_TITLE, 11, 12),
        window("prefix", APP_ID + ".Capture", PENGUIN_WINDOW_TITLE, 13, 14),
        window("substring", "x" + APP_ID, PENGUIN_WINDOW_TITLE, 15, 16),
    ]
    script = write_move_script(tmp_path, PENGUIN_WINDOW_TITLE, 900, 900)
    result = run_js(script, windows=decoys)
    assert result["writes"] == []
    assert_untouched(result, decoys)
    assert payloads(result, "MoveDone")[0]["ok"] is False


@needs_node
def test_move_script_reports_failure_when_the_api_is_missing(tmp_path: Path) -> None:
    script = write_move_script(tmp_path, PENGUIN_WINDOW_TITLE, 1, 2)
    result = run_js(script, windows=[penguin()], omit=["windowList"])
    assert payloads(result, "MoveDone")[0]["ok"] is False
    assert_untouched(result, [penguin()])


@pytest.mark.parametrize(
    "title",
    ["Waddle Settings", "", "waddle", 'Waddle"; workspace.windowList()[0].frameGeometry = {}; "'],
)
def test_render_move_script_rejects_other_titles(title: str) -> None:
    with pytest.raises(ValueError):
        render_move_script(title, 0, 0)


@pytest.mark.parametrize(
    ("x", "y"),
    [
        (1.5, 0),
        ("1", 0),
        (0, None),
        (True, 0),
        (0, math.inf),
        (COORD_LIMIT + 1, 0),
        (0, -COORD_LIMIT - 1),
        (10**30, 0),
    ],
)
def test_render_move_script_rejects_bad_coordinates(x: Any, y: Any) -> None:
    with pytest.raises(ValueError):
        render_move_script(PENGUIN_WINDOW_TITLE, x, y)


def test_render_move_script_embeds_exact_values() -> None:
    script = render_move_script(OVERLAY_WINDOW_TITLE, -COORD_LIMIT, COORD_LIMIT)
    assert f"var TITLE = {json.dumps(OVERLAY_WINDOW_TITLE)};" in script
    assert f"var X = -{COORD_LIMIT};" in script
    assert f"var Y = {COORD_LIMIT};" in script
    assert "@" not in script  # no unreplaced template markers


# --- payload parsers ---------------------------------------------------------------------

GOOD_WINDOW = {
    "title": "Waddle",
    "uuid": "u",
    "x": 1,
    "y": 2,
    "width": 3,
    "height": 4,
    "output": "",
}


@pytest.mark.parametrize(
    "change",
    [
        {"x": "1"},
        {"x": True},
        {"x": 1.5},
        {"x": None},
        {"x": 10**9},
        {"width": 0},
        {"height": -1},
        {"title": ""},
        {"title": 7},
        {"uuid": "u" * (types.MAX_STRING_CHARS + 1)},
        {"uuid": "\ud800"},
        {"output": "DP-1\n\x00"},
        {"x": 10**400},
        {"width": 10**400},
        {"output": None},
    ],
)
def test_parse_own_window_rejects_malformed_fields(change: dict[str, Any]) -> None:
    assert types.parse_own_window(json.dumps(GOOD_WINDOW)).output == ""
    with pytest.raises(ValueError):
        types.parse_own_window(json.dumps({**GOOD_WINDOW, **change}))


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json",
        "[]",
        "null",
        '{"title": "Waddle"}',
        '{"title":"Waddle","uuid":"u","x":NaN,"y":2,"width":3,"height":4,"output":""}',
        '{"title":"Waddle","uuid":"u","x":Infinity,"y":2,"width":3,"height":4,"output":""}',
        '{"title":"Waddle","uuid":"u","x":1e999,"y":2,"width":3,"height":4,"output":""}',
        "[" * 100_000,
        json.dumps({**GOOD_WINDOW, "pad": "x" * types.MAX_PAYLOAD_CHARS}),
        b"{}",
        None,
    ],
)
def test_parsers_reject_malformed_payloads(payload: Any) -> None:
    for parse in (
        types.parse_own_window,
        types.parse_outputs,
        types.parse_hello,
        types.parse_move_done,
    ):
        with pytest.raises(ValueError):
            parse(payload)


def test_parse_own_window_accepts_whole_floats() -> None:
    assert types.parse_own_window(json.dumps({**GOOD_WINDOW, "x": 5.0})).x == 5


@pytest.mark.parametrize(
    "payload",
    [
        {"outputs": {}, "active": None},
        {"outputs": [[]], "active": None},
        {"outputs": [{"name": "", "x": 0, "y": 0, "width": 1, "height": 1}]},
        {"outputs": [{"name": "A", "x": 0, "y": 0, "width": 0, "height": 1}]},
        {"outputs": [], "active": 3},
        {
            "outputs": [{"name": "A", "x": 0, "y": 0, "width": 1, "height": 1}]
            * (types.MAX_OUTPUTS + 1)
        },
    ],
)
def test_parse_outputs_rejects_malformed(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        types.parse_outputs(json.dumps(payload))


def test_parse_outputs_allows_no_active_output() -> None:
    assert types.parse_outputs('{"outputs": []}') == ([], None)


def test_dataclasses_are_frozen() -> None:
    info = types.OutputInfo("A", 0, 0, 1, 1)
    with pytest.raises(AttributeError):
        info.x = 5  # type: ignore[misc]
