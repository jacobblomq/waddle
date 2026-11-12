"""KWin scripts, executed for real under Node against fake KWin globals (tests/js/kwin_mock.js).

The point of most of these tests is the read-only guarantee: a script may only ever move a
window that has Waddle's class AND one of Waddle's two exact titles, and the scripts that
report on other applications' windows never assign to anything.
"""

from __future__ import annotations

import itertools
import json
import math
import re
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
from waddle_core.window import Geometry, WindowInfo
from waddle_desktop.kwin import types
from waddle_desktop.kwin.scripts import (
    COORD_LIMIT,
    CORE_SCRIPT_NAME,
    CURSOR_SCRIPT_NAME,
    OWN_GUARD_JS,
    WINDOW_PAYLOAD_JS,
    core_script_path,
    cursor_script_path,
    render_cursor_query_script,
    render_move_script,
    render_window_at_script,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_SCRIPT = core_script_path(REPO_ROOT)
CURSOR_SCRIPT = cursor_script_path(REPO_ROOT)
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
    # No script, the move script included, calls a state-changing method on a window or on
    # workspace, or assigns to workspace; the mock records those even inside a try block.
    assert [w for w in result["writes"] if w["id"] == "workspace" or w["prop"].endswith("()")] == []
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
    assert token not in CURSOR_SCRIPT.read_text(encoding="utf-8")
    assert token not in render_move_script(OVERLAY_WINDOW_TITLE, -5, 7)
    assert token not in render_cursor_query_script()
    assert token not in render_window_at_script(-5, 7)


def test_scripts_that_describe_windows_share_one_payload_block() -> None:
    assert WINDOW_PAYLOAD_JS in CORE_SCRIPT.read_text(encoding="utf-8"), (
        "waddle-core.js must contain WINDOW_PAYLOAD_JS verbatim"
    )
    at = render_window_at_script(0, 0)
    assert WINDOW_PAYLOAD_JS in at and OWN_GUARD_JS in at


def test_only_the_move_script_assigns_anything_to_a_window() -> None:
    assign = re.compile(r"\b(?:w|window|stack\[i\]|windows\[i\]|entry\.w)\.\w+\s*=(?!=)")
    assert assign.search(render_move_script(PENGUIN_WINDOW_TITLE, 0, 0))  # the regex works
    for source in (
        CORE_SCRIPT.read_text(encoding="utf-8"),
        CURSOR_SCRIPT.read_text(encoding="utf-8"),
        render_cursor_query_script(),
        render_window_at_script(1, 2),
    ):
        assert assign.search(source) is None
        assert "frameGeometry =" not in source and "frameGeometry=" not in source


# Everything a script may call as a method, and every workspace member it may touch. All of
# these only read (connect/disconnect/start/stop act on the script's own handlers and timer).
# A new name here needs a look at what it does to the session first.
ALLOWED_METHOD_CALLS = frozenset(
    {
        "connect", "disconnect", "start", "stop", "windowList",
        "stringify", "indexOf", "toLowerCase", "slice", "replace", "push",
        "max", "round", "floor", "now",
    }
)  # fmt: skip
ALLOWED_WORKSPACE_MEMBERS = frozenset(
    {
        "windowList", "stackingOrder", "screens", "activeScreen", "activeWindow",
        "cursorPos", "currentDesktop", "windowAdded", "windowRemoved", "windowActivated",
        "screensChanged", "cursorPosChanged", "currentDesktopChanged",
    }
)  # fmt: skip


def all_script_sources() -> list[str]:
    return [
        CORE_SCRIPT.read_text(encoding="utf-8"),
        CURSOR_SCRIPT.read_text(encoding="utf-8"),
        render_move_script(PENGUIN_WINDOW_TITLE, 0, 0),
        render_cursor_query_script(),
        render_window_at_script(1, 2),
    ]


def test_scripts_call_only_known_read_only_members() -> None:
    for source in all_script_sources():
        code = re.sub(r"//[^\n]*", "", source)
        called = set(re.findall(r"\.\s*([A-Za-z_$][\w$]*)\s*\(", code))
        assert called <= ALLOWED_METHOD_CALLS, sorted(called - ALLOWED_METHOD_CALLS)
        members = set(re.findall(r"\bworkspace\s*\.\s*([A-Za-z_$][\w$]*)", code))
        assert members <= ALLOWED_WORKSPACE_MEMBERS, sorted(members - ALLOWED_WORKSPACE_MEMBERS)
        # No computed access to workspace, and nothing ever assigned to one of its members.
        assert re.search(r"\bworkspace\s*\[", code) is None
        assert re.search(r"\bworkspace\s*\.\s*[\w$]+\s*=(?!=)", code) is None
        assert "eval" not in code and "Function(" not in code


def test_cursor_script_is_a_separate_file_that_reads_no_windows() -> None:
    assert CURSOR_SCRIPT == REPO_ROOT / "kwin" / CURSOR_SCRIPT_NAME
    source = CURSOR_SCRIPT.read_text(encoding="utf-8")
    for name in ("windowList", "stackingOrder", "activeWindow", "caption", "resourceClass"):
        assert name not in source
        assert name not in render_cursor_query_script()


def test_core_script_never_assigns_window_geometry() -> None:
    source = CORE_SCRIPT.read_text(encoding="utf-8")
    assert "frameGeometry =" not in source and "frameGeometry=" not in source


# --- core script -------------------------------------------------------------------------


@needs_node
def test_core_reports_hello_outputs_and_penguin() -> None:
    result = run_js(CORE_SCRIPT, windows=[penguin(), *BYSTANDERS])

    assert [c["method"] for c in result["calls"]] == ["Hello", "Outputs", "OwnWindow"]
    assert payloads(result, "Hello") == [{"version": 2, "apiOk": True}]
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
    assert types.parse_hello(by_method["Hello"]) == types.Hello(version=2, api_ok=True)
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
    assert payloads(result, "Hello") == [{"version": 2, "apiOk": True}]
    assert payloads(result, "Outputs")[0]["active"] is None
    assert [(r["x"], r["y"]) for r in payloads(result, "OwnWindow")] == [(100, 200), (1, 2)]


@needs_node
def test_core_says_api_not_ok_when_required_apis_are_missing() -> None:
    result = run_js(CORE_SCRIPT, windows=[penguin()], omit=["windowList", "windowAdded", "screens"])
    assert payloads(result, "Hello") == [{"version": 2, "apiOk": False}]
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


# --- core script: target tracking ---------------------------------------------------------


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


A, B, SETTINGS, MENU, SUBMENU = uid(1), uid(2), uid(3), uid(4), uid(5)
TARGET_SIGNALS = 5  # caption, frame geometry, client geometry, fullscreen, output


def app(
    id_: str, cls: str, caption: str, x: int = 100, y: int = 200, **extra: Any
) -> dict[str, Any]:
    return {**window(id_, cls, caption, x, y), **extra}


def kate(**extra: Any) -> dict[str, Any]:
    return app(A, "org.kde.kate", "notes.txt — Kate", **extra)


def firefox(**extra: Any) -> dict[str, Any]:
    return app(B, "firefox", "Example — Mozilla Firefox", 300, 50, **extra)


def settings_window() -> dict[str, Any]:
    return app(SETTINGS, APP_ID, "Waddle Settings", 30, 30)


def moved(x: int, y: int = 200) -> dict[str, int]:
    return {"x": x, "y": y, "width": 180, "height": 220}


def sent(result: dict[str, Any], method: str) -> list[tuple[int, Any]]:
    """(virtual time, payload) of every call of one method."""
    return [(c["at"], json.loads(c["args"][0])) for c in result["calls"] if c["method"] == method]


@needs_node
def test_activation_payload_is_complete_and_typed() -> None:
    win = kate(
        resourceName="kate",
        desktopFileName="org.kde.kate",
        pid=4242,
        clientGeometry={"x": 100.5, "y": 228, "width": 180, "height": 192},
        fullScreen=False,
        kind="XdgToplevelWindow",
    )
    win["frameGeometry"] = {"x": 100.5, "y": 200, "width": 180, "height": 220}
    screens = [{**SCREENS[0], "scale": 1.25}, SCREENS[1]]
    result = run_js(
        CORE_SCRIPT, windows=[win], screens=screens, events=[{"type": "activate", "id": A}]
    )

    assert [c["method"] for c in result["calls"]] == ["Hello", "Outputs", "Activated"]
    [payload] = payloads(result, "Activated")
    assert payload == {
        "uuid": A,
        "caption": "notes.txt — Kate",
        "resourceClass": "org.kde.kate",
        "resourceName": "kate",
        "desktopFile": "org.kde.kate",
        "pid": 4242,
        "frame": {"x": 100.5, "y": 200, "width": 180, "height": 220},
        "client": {"x": 100.5, "y": 228, "width": 180, "height": 192},
        "output": "DP-1",
        "scale": 1.25,
        "fullscreen": False,
        "xwayland": False,
        "own": False,
    }
    assert type(payload["pid"]) is int and type(payload["fullscreen"]) is bool
    assert types.parse_activated(json.dumps(payload)) == WindowInfo(
        uuid=A,
        caption="notes.txt — Kate",
        resource_class="org.kde.kate",
        resource_name="kate",
        desktop_file="org.kde.kate",
        pid=4242,
        frame=Geometry(100.5, 200, 180, 220),
        client=Geometry(100.5, 228, 180, 192),
        output="DP-1",
        scale=1.25,
        fullscreen=False,
        xwayland=False,
        is_own=False,
    )
    assert result["writes"] == []


@needs_node
def test_the_window_active_at_load_is_reported() -> None:
    result = run_js(CORE_SCRIPT, windows=[kate(), firefox()], active=B)
    assert [p["uuid"] for p in payloads(result, "Activated")] == [B]
    assert result["connections"] == {A: 0, B: TARGET_SIGNALS}


@needs_node
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("X11Window", True),
        ("XdgToplevelWindow", False),
        ("InternalWindow", False),
        ("SomethingNew", None),
        (None, None),
    ],
)
def test_xwayland_guess_is_null_when_the_window_kind_is_unknown(
    kind: str | None, expected: bool | None
) -> None:
    win = kate(kind=kind) if kind else kate()
    result = run_js(CORE_SCRIPT, windows=[win], active=A)
    [payload] = payloads(result, "Activated")
    assert payload["xwayland"] is expected
    assert types.parse_activated(json.dumps(payload)).xwayland is expected


@needs_node
def test_activation_payload_survives_odd_window_values() -> None:
    win = kate(pid=-1, fullScreen="yes")
    win["caption"] = "x" * 5000
    win["resourceClass"] = "c" * 1000
    win["output"] = "nowhere"  # no such output: the window has none
    win["frameGeometry"] = {"x": 0, "y": 0, "width": -5, "height": 0}
    [payload] = payloads(run_js(CORE_SCRIPT, windows=[win], active=A), "Activated")
    assert payload["caption"] == "x" * 512 and payload["resourceClass"] == "c" * 256
    assert (payload["pid"], payload["output"], payload["scale"]) == (0, "", 1)
    assert payload["fullscreen"] is False
    assert payload["frame"] == {"x": 0, "y": 0, "width": 0, "height": 0}
    assert payload["client"] == payload["frame"]  # no clientGeometry: the frame stands in
    assert payload["resourceName"] == "" and payload["desktopFile"] == ""
    types.parse_activated(json.dumps(payload))


@needs_node
def test_own_windows_are_flagged_own_whatever_their_title() -> None:
    windows = [
        kate(),
        settings_window(),
        app(uid(6), APP_ID.upper(), PENGUIN_WINDOW_TITLE),
        app(uid(7), "org.example.Decoy", "Waddle Settings"),
        app(uid(8), APP_ID + ".Capture", PENGUIN_WINDOW_TITLE),
    ]
    result = run_js(
        CORE_SCRIPT,
        windows=windows,
        events=[{"type": "activate", "id": w["id"]} for w in windows],
    )
    reports = payloads(result, "Activated")
    assert [(r["uuid"], r["own"]) for r in reports] == [
        (A, False),
        (SETTINGS, True),
        (uid(6), True),
        (uid(7), False),
        (uid(8), False),
    ]
    assert reports[1]["caption"] == "Waddle Settings"
    assert types.parse_activated(json.dumps(reports[1])).is_own is True


@needs_node
def test_an_own_window_activating_does_not_replace_the_target() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[kate(), settings_window()],
        events=[
            {"type": "activate", "id": A},
            {"type": "activate", "id": SETTINGS},
            {"type": "advance", "ms": 500},
            # Waddle's own window changing is not a target change...
            {"type": "caption", "id": SETTINGS, "caption": "Waddle Settings — Privacy"},
            {"type": "geometry", "id": SETTINGS, "frameGeometry": moved(1)},
            # ...and the application the user was in is still followed.
            {"type": "caption", "id": A, "caption": "other.txt — Kate"},
            {"type": "advance", "ms": 500},
        ],
    )
    assert [p["own"] for p in payloads(result, "Activated")] == [False, True]
    changes = payloads(result, "TargetChanged")
    assert [(c["uuid"], c["caption"]) for c in changes] == [(A, "other.txt — Kate")]
    assert result["connections"][A] == TARGET_SIGNALS
    assert result["writes"] == []


@needs_node
def test_no_activation_is_reported_when_nothing_is_active() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[kate()],
        events=[
            {"type": "activate", "id": A},
            {"type": "activate", "id": None},
            {"type": "advance", "ms": 200},
            {"type": "geometry", "id": A, "frameGeometry": moved(7)},
        ],
    )
    assert len(payloads(result, "Activated")) == 1
    assert [c["frame"]["x"] for c in payloads(result, "TargetChanged")] == [7]


@needs_node
def test_target_changes_are_throttled_and_coalesced() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[kate()],
        events=[
            {"type": "activate", "id": A},  # t=0, counts as this window's message
            *({"type": "geometry", "id": A, "frameGeometry": moved(x)} for x in (1, 2, 3)),
            {"type": "advance", "ms": 99},
            {"type": "geometry", "id": A, "frameGeometry": moved(4)},
            {"type": "advance", "ms": 1},  # t=100: one message, with the latest state
            {"type": "geometry", "id": A, "frameGeometry": moved(5)},
            {"type": "caption", "id": A, "caption": "b.txt — Kate"},
            {"type": "advance", "ms": 50},  # t=150: still inside the window
            {"type": "fullscreen", "id": A, "fullScreen": True},
            {"type": "advance", "ms": 50},  # t=200: one message with all three changes
            {"type": "advance", "ms": 1000},  # quiet
            {"type": "output", "id": A, "output": "HDMI-A-1"},  # t=1200: sent at once
            {"type": "advance", "ms": 1000},
        ],
    )
    changes = sent(result, "TargetChanged")
    assert [(at, p["frame"]["x"]) for at, p in changes] == [(100, 4), (200, 5), (1200, 5)]
    assert (changes[1][1]["caption"], changes[1][1]["fullscreen"]) == ("b.txt — Kate", True)
    assert changes[2][1]["output"] == "HDMI-A-1"
    for _, payload in changes:
        assert types.parse_target_changed(json.dumps(payload)).uuid == A
    assert result["activeTimers"] == 0 and result["writes"] == []


@needs_node
def test_a_fast_stream_of_changes_is_at_most_ten_messages_a_second() -> None:
    events: list[dict[str, Any]] = [{"type": "activate", "id": A}]
    for step in range(1, 201):  # a drag: a new position every 5 ms for one second
        events.append({"type": "geometry", "id": A, "frameGeometry": moved(step)})
        events.append({"type": "advance", "ms": 5})
    events.append({"type": "advance", "ms": 500})
    changes = sent(run_js(CORE_SCRIPT, windows=[kate()], events=events), "TargetChanged")
    times = [at for at, _ in changes]
    assert len(times) == 10
    assert all(later - earlier >= 100 for earlier, later in itertools.pairwise(times))
    assert changes[-1][1]["frame"]["x"] == 200  # the final position is not lost


@needs_node
def test_an_identical_target_payload_is_never_sent_twice() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[kate()],
        events=[
            {"type": "activate", "id": A},
            {"type": "advance", "ms": 500},
            {"type": "geometry", "id": A},  # signal with nothing changed
            {"type": "caption", "id": A, "caption": "notes.txt — Kate"},  # same caption
            {"type": "advance", "ms": 500},
            {"type": "geometry", "id": A, "frameGeometry": moved(9)},  # sent at once
            {"type": "geometry", "id": A, "frameGeometry": moved(50)},
            {"type": "geometry", "id": A, "frameGeometry": moved(9)},  # back where it was
            {"type": "advance", "ms": 500},
        ],
    )
    changes = [c["args"][0] for c in result["calls"] if c["method"] == "TargetChanged"]
    assert len(changes) == 1 and json.loads(changes[0])["frame"]["x"] == 9
    assert result["activeTimers"] == 0


@needs_node
def test_without_a_timer_changes_are_sent_only_when_values_differ() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[kate()],
        omit=["QTimer"],
        events=[
            {"type": "activate", "id": A},
            {"type": "geometry", "id": A},
            {"type": "geometry", "id": A, "frameGeometry": moved(1)},
            {"type": "geometry", "id": A, "frameGeometry": moved(1)},
            {"type": "geometry", "id": A, "frameGeometry": moved(2)},
        ],
    )
    assert [c["frame"]["x"] for c in payloads(result, "TargetChanged")] == [1, 2]


@needs_node
def test_handlers_are_disconnected_from_the_previous_target() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[kate(), firefox()],
        events=[
            {"type": "activate", "id": A},
            {"type": "geometry", "id": A, "frameGeometry": moved(1)},  # pending for A
            {"type": "activate", "id": B},
            {"type": "advance", "ms": 500},
            {"type": "geometry", "id": A, "frameGeometry": moved(2)},
            {"type": "caption", "id": A, "caption": "secret.txt — Kate"},
            {"type": "fullscreen", "id": A, "fullScreen": True},
            {"type": "output", "id": A, "output": "HDMI-A-1"},
            {"type": "advance", "ms": 500},
            {"type": "geometry", "id": B, "frameGeometry": moved(3)},
        ],
    )
    assert [p["uuid"] for p in payloads(result, "Activated")] == [A, B]
    changes = payloads(result, "TargetChanged")
    assert [(c["uuid"], c["frame"]["x"]) for c in changes] == [(B, 3)]
    assert "secret" not in json.dumps(result["calls"])
    assert result["connections"] == {A: 0, B: TARGET_SIGNALS}


@needs_node
def test_switching_back_and_forth_does_not_stack_up_handlers() -> None:
    events = [{"type": "activate", "id": (A, B)[n % 2]} for n in range(20)]
    events += [
        {"type": "advance", "ms": 500},
        {"type": "geometry", "id": B, "frameGeometry": moved(3)},
    ]
    result = run_js(CORE_SCRIPT, windows=[kate(), firefox()], events=events)
    assert result["connections"] == {A: 0, B: TARGET_SIGNALS}
    assert len(payloads(result, "TargetChanged")) == 1  # not one per stale handler


@needs_node
@pytest.mark.parametrize("listed", [False, True])
def test_closing_the_target_emits_nothing_and_does_not_throw(listed: bool) -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[kate(), firefox()],
        events=[
            {"type": "activate", "id": A},
            {"type": "geometry", "id": A, "frameGeometry": moved(1)},  # pending
            {"type": "remove", "id": A, "listed": listed},
            {"type": "advance", "ms": 500},
            # KWin can still fire signals of a window that is on its way out.
            {"type": "geometry", "id": A, "frameGeometry": moved(2)},
            {"type": "caption", "id": A, "caption": "gone"},
            {"type": "advance", "ms": 500},
            {"type": "remove", "id": B},
        ],
    )
    assert [c["method"] for c in result["calls"]] == ["Hello", "Outputs", "Activated"]
    assert result["connections"][A] == 0 and result["activeTimers"] == 0


@needs_node
def test_a_target_closed_and_another_activated_is_followed() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[kate(), firefox()],
        events=[
            {"type": "activate", "id": A},
            {"type": "remove", "id": A},
            {"type": "activate", "id": B},
            {"type": "advance", "ms": 500},
            {"type": "geometry", "id": B, "frameGeometry": moved(8)},
        ],
    )
    assert [(c["uuid"], c["frame"]["x"]) for c in payloads(result, "TargetChanged")] == [(B, 8)]


@needs_node
def test_desktop_changes_are_reported_with_an_empty_object() -> None:
    result = run_js(CORE_SCRIPT, windows=[kate()], events=[{"type": "desktop", "desktop": 2}])
    assert payloads(result, "DesktopChanged") == [{}]
    assert types.parse_desktop_changed(json.dumps({})) is None
    silent = run_js(
        CORE_SCRIPT,
        windows=[kate()],
        omit=["currentDesktopChanged", "windowActivated", "activeWindow"],
        events=[{"type": "desktop", "desktop": 2}],
    )
    assert [c["method"] for c in silent["calls"]] == ["Hello", "Outputs"]


def popup(id_: str, parent: str, x: int, **extra: Any) -> dict[str, Any]:
    spec = app(id_, "org.kde.kate", "", x, 300, normalWindow=False, transientFor=parent)
    spec["frameGeometry"] = {"x": x, "y": 300, "width": 120, "height": 240}
    return {"popupWindow": True, **spec, **extra}


@needs_node
@pytest.mark.parametrize("listed", [False, True])
def test_popups_of_the_target_appear_and_disappear(listed: bool) -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[kate(), firefox()],
        events=[
            {"type": "activate", "id": A},
            {"type": "add", "window": popup(MENU, A, 110)},
            {"type": "add", "window": popup(SUBMENU, MENU, 230)},  # a menu of the menu
            {
                "type": "geometry",
                "id": SUBMENU,
                "frameGeometry": {"x": 231, "y": 300, "width": 120, "height": 240},
            },
            {"type": "geometry", "id": SUBMENU},  # nothing changed: nothing sent
            {"type": "remove", "id": SUBMENU, "listed": listed},
            {"type": "remove", "id": MENU, "listed": listed},
        ],
    )
    menu = {"uuid": MENU, "x": 110, "y": 300, "width": 120, "height": 240}
    sub = {"uuid": SUBMENU, "x": 230, "y": 300, "width": 120, "height": 240}
    assert payloads(result, "Popups") == [
        {"target": A, "popups": [menu]},
        {"target": A, "popups": [menu, sub]},
        {"target": A, "popups": [menu, {**sub, "x": 231}]},
        {"target": A, "popups": [menu]},
        {"target": A, "popups": []},
    ]
    raw = [c["args"][0] for c in result["calls"] if c["method"] == "Popups"]
    first, last = raw[0], raw[-1]
    assert types.parse_popups(first) == types.PopupSet(
        A, (types.Popup(MENU, Geometry(110, 300, 120, 240)),)
    )
    assert types.parse_popups(last) == types.PopupSet(A, ())
    assert result["connections"][MENU] == 0 and result["connections"][SUBMENU] == 0
    assert result["writes"] == []


@needs_node
def test_only_windows_that_say_they_are_popups_of_the_target_are_listed() -> None:
    not_popups = [
        popup(uid(10), B, 1),  # another application's menu
        popup(uid(11), A, 2, popupWindow=False),  # transient for the target, claims nothing
        app(uid(12), "org.kde.kate", "", popupWindow=True),  # no parent at all
        {**popup(uid(13), A, 3), "resourceClass": APP_ID},  # Waddle's own bubble
        popup(uid(14), A, 4, minimized=True),
        firefox(),  # an ordinary window
    ]
    dialog = popup(uid(15), A, 5, popupWindow=False, transient=True)
    result = run_js(
        CORE_SCRIPT,
        windows=[kate(), *not_popups],
        events=[{"type": "activate", "id": A}, {"type": "add", "window": dialog}],
    )
    assert [[p["uuid"] for p in s["popups"]] for s in payloads(result, "Popups")] == [[uid(15)]]


@needs_node
def test_popups_already_open_are_reported_on_activation_and_dropped_on_switch() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[kate(), firefox(), popup(MENU, A, 110)],
        events=[
            {"type": "activate", "id": A},
            {"type": "activate", "id": B},
            {"type": "geometry", "id": MENU, "frameGeometry": moved(1)},
            {"type": "remove", "id": MENU},
        ],
    )
    reports = payloads(result, "Popups")
    assert [(r["target"], len(r["popups"])) for r in reports] == [(A, 1)]
    assert result["connections"][MENU] == 0
    assert [c["method"] for c in result["calls"]][-2:] == ["Popups", "Activated"]


@needs_node
def test_at_most_sixteen_popups_are_listed() -> None:
    menus = [popup(uid(100 + n), A, n) for n in range(40)]
    result = run_js(CORE_SCRIPT, windows=[kate(), *menus], active=A)
    [report] = [c["args"][0] for c in result["calls"] if c["method"] == "Popups"]
    assert len(types.parse_popups(report).popups) == types.MAX_POPUPS == 16


@needs_node
def test_core_tracking_survives_missing_optional_window_apis() -> None:
    result = run_js(
        CORE_SCRIPT,
        windows=[kate()],
        omit=[
            "window.fullScreenChanged",
            "window.clientGeometryChanged",
            "window.outputChanged",
            "window.captionChanged",
        ],
        events=[
            {"type": "activate", "id": A},
            {"type": "advance", "ms": 500},
            {"type": "geometry", "id": A, "frameGeometry": moved(6)},
        ],
    )
    assert [c["frame"]["x"] for c in payloads(result, "TargetChanged")] == [6]
    assert result["connections"][A] == 1


# --- cursor script ------------------------------------------------------------------------


@needs_node
def test_cursor_script_reports_once_at_load_and_nothing_about_windows() -> None:
    result = run_js(CURSOR_SCRIPT, windows=[penguin(), *BYSTANDERS], cursor={"x": 12, "y": 34.5})
    assert [c["method"] for c in result["calls"]] == ["Cursor"]
    assert types.parse_cursor(result["calls"][0]["args"][0]) == types.CursorPos(12, 34.5)
    assert result["writes"] == [] and set(result["connections"].values()) == {0}


@needs_node
def test_cursor_stream_is_at_most_ten_a_second_with_a_trailing_final_position() -> None:
    events: list[dict[str, Any]] = []
    for step in range(1, 301):  # 1.5 s of movement, a new position every 5 ms
        events.append({"type": "cursor", "x": step, "y": 2 * step})
        events.append({"type": "advance", "ms": 5})
    events.append({"type": "advance", "ms": 1000})
    reports = sent(run_js(CURSOR_SCRIPT, cursor={"x": 0, "y": 0}, events=events), "Cursor")

    times = [at for at, _ in reports]
    assert times[0] == 0 and len(times) > 10
    assert all(later - earlier >= 100 for earlier, later in itertools.pairwise(times))
    for start in range(0, 1500, 50):  # any one-second window
        assert sum(start <= at < start + 1000 for at in times) <= 10
    assert reports[-1][1] == {"x": 300, "y": 600}  # where the pointer came to rest
    assert times[-1] <= 1600


@needs_node
def test_cursor_first_move_after_a_pause_is_sent_at_once_and_repeats_are_not() -> None:
    result = run_js(
        CURSOR_SCRIPT,
        cursor={"x": 1, "y": 1},
        events=[
            {"type": "advance", "ms": 1000},
            {"type": "cursor", "x": 5, "y": 5},  # leading edge: t=1000
            {"type": "cursor", "x": 6, "y": 6},
            {"type": "cursor", "x": 5, "y": 5},  # the burst ends where it was reported
            {"type": "advance", "ms": 1000},
            {"type": "cursor", "x": 5, "y": 5},  # signal without movement
            {"type": "advance", "ms": 1000},
        ],
    )
    assert sent(result, "Cursor") == [(0, {"x": 1, "y": 1}), (1000, {"x": 5, "y": 5})]
    assert result["activeTimers"] == 0


@needs_node
def test_cursor_stream_without_a_timer_is_still_rate_limited() -> None:
    events: list[dict[str, Any]] = []
    for step in range(1, 201):
        events.append({"type": "cursor", "x": step, "y": 0})
        events.append({"type": "advance", "ms": 5})
    result = run_js(CURSOR_SCRIPT, cursor={"x": 0, "y": 0}, omit=["QTimer"], events=events)
    times = [at for at, _ in sent(result, "Cursor")]
    assert times[0] == 0 and 5 <= len(times) <= 11
    assert all(later - earlier >= 100 for earlier, later in itertools.pairwise(times))


@needs_node
def test_cursor_script_survives_a_missing_api() -> None:
    result = run_js(CURSOR_SCRIPT, omit=["cursorPosChanged", "cursorPos"])
    assert result["calls"] == []


# --- one-shot queries ---------------------------------------------------------------------


def write_script(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "one-shot.js"
    path.write_text(source, encoding="utf-8")
    return path


@needs_node
def test_cursor_query_reports_once_and_then_nothing(tmp_path: Path) -> None:
    script = write_script(tmp_path, render_cursor_query_script())
    result = run_js(
        script,
        windows=BYSTANDERS,
        cursor={"x": -20, "y": 1500},
        events=[{"type": "cursor", "x": 1, "y": 1}, {"type": "advance", "ms": 5000}],
    )
    assert payloads(result, "Cursor") == [{"x": -20, "y": 1500}]
    assert len(result["calls"]) == 1 and result["writes"] == []
    assert result["activeTimers"] == 0


@needs_node
def test_cursor_query_says_nothing_when_the_api_is_missing(tmp_path: Path) -> None:
    script = write_script(tmp_path, render_cursor_query_script())
    assert run_js(script, omit=["cursorPos"])["calls"] == []


def at_stack() -> list[dict[str, Any]]:
    """Bottom to top. The point (150, 250) is inside every one of them except `aside`."""
    return [
        app(uid(20), "org.kde.dolphin", "Home — Dolphin"),
        app(uid(21), "firefox", "Bank — Mozilla Firefox", kind="X11Window"),
        app(uid(22), "org.kde.kate", "minimized.txt — Kate", minimized=True),
        app(uid(23), "org.kde.konsole", "elsewhere — Konsole", desktops=[2]),
        app(uid(24), "plasmashell", "", normalWindow=False),  # a panel
        app(uid(25), "org.kde.okular", "aside.pdf — Okular", 900, 900),
        app(uid(26), APP_ID, "Waddle Settings"),
        app(uid(27), APP_ID, PENGUIN_WINDOW_TITLE),
        app(uid(28), APP_ID, "Waddle Region Select"),
    ]


def window_at(tmp_path: Path, x: int, y: int, **scenario: Any) -> dict[str, Any]:
    result = run_js(write_script(tmp_path, render_window_at_script(x, y)), **scenario)
    assert result["writes"] == []
    return result


@needs_node
def test_window_at_picks_the_topmost_application_window_under_the_point(tmp_path: Path) -> None:
    result = window_at(tmp_path, 150, 250, windows=at_stack())
    [call] = result["calls"]
    assert call["method"] == "WindowAt"
    answer = types.parse_window_at(call["args"][0])
    assert (answer.x, answer.y) == (150, 250)
    assert answer.window is not None
    assert (answer.window.uuid, answer.window.caption) == (uid(21), "Bank — Mozilla Firefox")
    assert answer.window.xwayland is True and answer.window.is_own is False
    assert answer.window.frame == Geometry(100, 200, 180, 220)


@needs_node
def test_window_at_follows_the_stacking_order_not_the_window_list(tmp_path: Path) -> None:
    windows = at_stack()
    stack = [uid(21), uid(20), *(w["id"] for w in windows[2:])]  # Dolphin raised over Firefox
    [answer] = payloads(window_at(tmp_path, 150, 250, windows=windows, stack=stack), "WindowAt")
    assert answer["window"]["uuid"] == uid(20)


@needs_node
def test_window_at_includes_a_window_on_all_desktops_and_dialogs(tmp_path: Path) -> None:
    windows = at_stack()
    windows[3]["onAllDesktops"] = True  # Konsole, otherwise on desktop 2
    [answer] = payloads(window_at(tmp_path, 150, 250, windows=windows), "WindowAt")
    assert answer["window"]["uuid"] == uid(23)
    dialog = app(uid(30), "firefox", "Save As", normalWindow=False, dialog=True)
    [answer] = payloads(window_at(tmp_path, 150, 250, windows=[*at_stack(), dialog]), "WindowAt")
    assert answer["window"]["uuid"] == uid(30)


@needs_node
@pytest.mark.parametrize(("x", "y"), [(99, 250), (280, 250), (150, 199), (150, 420), (5000, 5)])
def test_window_at_reports_null_when_no_application_window_is_there(
    tmp_path: Path, x: int, y: int
) -> None:
    result = window_at(tmp_path, x, y, windows=at_stack()[:5] + at_stack()[6:])
    assert payloads(result, "WindowAt") == [{"x": x, "y": y, "window": None}]
    assert types.parse_window_at(result["calls"][0]["args"][0]) == types.WindowAt(x, y, None)


@needs_node
def test_window_at_edges_are_half_open(tmp_path: Path) -> None:
    for x, y, hit in [(100, 200, True), (279, 419, True), (280, 419, False), (279, 420, False)]:
        [answer] = payloads(window_at(tmp_path, x, y, windows=[kate()]), "WindowAt")
        assert (answer["window"] is not None) is hit


@needs_node
def test_window_at_never_reports_waddle_or_windows_under_only_waddle(tmp_path: Path) -> None:
    own_only = [w for w in at_stack() if w["resourceClass"] == APP_ID]
    result = window_at(tmp_path, 150, 250, windows=own_only)
    assert payloads(result, "WindowAt") == [{"x": 150, "y": 250, "window": None}]


@needs_node
def test_window_at_says_nothing_when_the_stacking_order_is_missing(tmp_path: Path) -> None:
    assert window_at(tmp_path, 150, 250, windows=at_stack(), omit=["stackingOrder"])["calls"] == []


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
        ("0; workspace.windowList()[0].frameGeometry = {}", 0),
    ],
)
def test_render_window_at_script_rejects_bad_coordinates(x: Any, y: Any) -> None:
    with pytest.raises(ValueError):
        render_window_at_script(x, y)


def test_render_one_shot_scripts_embed_exact_values_and_nothing_else() -> None:
    script = render_window_at_script(-COORD_LIMIT, COORD_LIMIT)
    assert f"var X = -{COORD_LIMIT};" in script and f"var Y = {COORD_LIMIT};" in script
    assert "@" not in script and "@" not in render_cursor_query_script()
    assert render_cursor_query_script() == render_cursor_query_script()
    for source in (script, render_cursor_query_script()):
        assert source.count("callDBus(") == 1
        assert json.dumps(DBUS_SERVICE) in source and json.dumps(DBUS_KWIN_INTERFACE) in source


# --- parsers for the new payloads ----------------------------------------------------------

GOOD_INFO: dict[str, Any] = {
    "uuid": A,
    "caption": "notes.txt — Kate",
    "resourceClass": "org.kde.kate",
    "resourceName": "kate",
    "desktopFile": "org.kde.kate",
    "pid": 4242,
    "frame": {"x": -10, "y": 0.5, "width": 800, "height": 600},
    "client": {"x": -10, "y": 28.5, "width": 800, "height": 572},
    "output": "DP-1",
    "scale": 1.5,
    "fullscreen": False,
    "xwayland": None,
    "own": False,
}


def info(**change: Any) -> str:
    return json.dumps({**GOOD_INFO, **change})


def test_parse_window_info_reads_every_field() -> None:
    parsed = types.parse_window_info(info(xwayland=True, own=True, fullscreen=True))
    assert parsed == WindowInfo(
        A, "notes.txt — Kate", "org.kde.kate", "kate", "org.kde.kate", 4242,
        Geometry(-10, 0.5, 800, 600), Geometry(-10, 28.5, 800, 572), "DP-1", 1.5,
        True, True, True,
    )  # fmt: skip
    assert type(parsed.frame.x) is float and type(parsed.pid) is int
    assert types.parse_activated is types.parse_window_info
    assert types.parse_target_changed is types.parse_window_info


@pytest.mark.parametrize(
    ("caption", "cleaned"),
    [
        ("a\x00b\x1b[31mc\x7f", "ab[31mc"),
        ("line one\nline two\r\ttab", "line oneline twotab"),
        ("bidi \u202egnp.exe", "bidi gnp.exe"),
        ("lone \ud800 surrogate", "lone  surrogate"),
        ("sep\u2028arator", "separator"),
        ("naïve — 日本語 🐧", "naïve — 日本語 🐧"),
        ("", ""),
        ("\x00\x01", ""),
    ],
)
def test_captions_have_control_characters_stripped_not_rejected(caption: str, cleaned: str) -> None:
    assert types.parse_window_info(info(caption=caption)).caption == cleaned


def test_long_captions_are_capped_after_cleaning() -> None:
    assert types.MAX_CAPTION_CHARS == 512
    long = types.parse_window_info(info(caption="é" * 2000)).caption
    assert long == "é" * 512
    # Stripped characters do not count towards the cap.
    mixed = types.parse_window_info(info(caption="\x00" * 600 + "t" * 600)).caption
    assert mixed == "t" * 512
    assert types.parse_window_info(info(caption="t" * 512)).caption == "t" * 512


def test_other_application_chosen_strings_are_cleaned_and_capped_too() -> None:
    parsed = types.parse_window_info(
        info(resourceClass="c\x00" * 400, resourceName="n\n", desktopFile="", output="DP\x1b-1")
    )
    assert parsed.resource_class == "c" * types.MAX_STRING_CHARS
    assert (parsed.resource_name, parsed.desktop_file, parsed.output) == ("n", "", "DP-1")


@pytest.mark.parametrize(
    "change",
    [
        {"caption": 7},
        {"caption": None},
        {"caption": ["a"]},
        {"resourceClass": None},
        {"resourceName": 1},
        {"desktopFile": {}},
        {"output": None},
        {"uuid": "penguin"},
        {"uuid": ""},
        {"uuid": "{" + A + "}"},
        {"uuid": A + "\n"},
        {"uuid": A.replace("0", "g", 1)},
        {"uuid": "../../../etc/passwd"},
        {"uuid": 5},
        {"pid": -1},
        {"pid": 1.5},
        {"pid": "42"},
        {"pid": True},
        {"pid": 2**40},
        {"frame": None},
        {"frame": [0, 0, 1, 1]},
        {"frame": {"x": 0, "y": 0, "width": 1}},
        {"frame": {"x": 100_001, "y": 0, "width": 1, "height": 1}},
        {"frame": {"x": 0, "y": -100_001, "width": 1, "height": 1}},
        {"frame": {"x": 0, "y": 0, "width": -1, "height": 1}},
        {"frame": {"x": 0, "y": 0, "width": 1, "height": 100_001}},
        {"frame": {"x": "0", "y": 0, "width": 1, "height": 1}},
        {"frame": {"x": True, "y": 0, "width": 1, "height": 1}},
        {"frame": {"x": 10**400, "y": 0, "width": 1, "height": 1}},
        {"client": {"x": None, "y": 0, "width": 1, "height": 1}},
        {"scale": 0},
        {"scale": -1},
        {"scale": 1000},
        {"scale": "1"},
        {"scale": None},
        {"fullscreen": 0},
        {"fullscreen": None},
        {"xwayland": "no"},
        {"xwayland": 0},
        {"own": None},
        {"own": "false"},
    ],
)
def test_parse_window_info_rejects_bad_types_and_ranges(change: dict[str, Any]) -> None:
    types.parse_window_info(info())  # the base payload is good
    with pytest.raises(ValueError):
        types.parse_window_info(info(**change))


@pytest.mark.parametrize("missing", sorted(GOOD_INFO))
def test_parse_window_info_requires_every_field_but_xwayland(missing: str) -> None:
    payload = json.dumps({k: v for k, v in GOOD_INFO.items() if k != missing})
    if missing == "xwayland":
        assert types.parse_window_info(payload).xwayland is None
    else:
        with pytest.raises(ValueError):
            types.parse_window_info(payload)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_window_numbers_must_be_finite(literal: str) -> None:
    for payload in (
        info().replace('"x": -10', f'"x": {literal}', 1),
        info().replace('"scale": 1.5', f'"scale": {literal}'),
        json.dumps({"x": 1, "y": 2}).replace("2", literal),
    ):
        assert literal in payload
        for parse in (types.parse_window_info, types.parse_cursor):
            with pytest.raises(ValueError):
                parse(payload)


def test_error_messages_never_contain_payload_content() -> None:
    with pytest.raises(ValueError) as caught:
        types.parse_window_info(info(caption="secret title", uuid="secret-uuid"))
    assert "secret" not in str(caught.value)


def test_zero_sized_geometry_and_limits_are_accepted() -> None:
    edge = {"x": -100_000, "y": 100_000, "width": 0, "height": 100_000}
    parsed = types.parse_window_info(info(frame=edge, pid=0, scale=16))
    assert parsed.frame == Geometry(-100_000, 100_000, 0, 100_000) and parsed.pid == 0


def test_parse_popups() -> None:
    entry = {"uuid": MENU, "x": 1.5, "y": -2, "width": 30, "height": 40}
    good = {"target": A, "popups": [entry]}
    assert types.parse_popups(json.dumps(good)) == types.PopupSet(
        A, (types.Popup(MENU, Geometry(1.5, -2, 30, 40)),)
    )
    assert types.parse_popups(json.dumps({"target": A, "popups": []})).popups == ()
    for bad in (
        {"popups": [entry]},
        {"target": "kate", "popups": []},
        {"target": A},
        {"target": A, "popups": {}},
        {"target": A, "popups": [None]},
        {"target": A, "popups": [{**entry, "uuid": "menu"}]},
        {"target": A, "popups": [{**entry, "width": -1}]},
        {"target": A, "popups": [{**entry, "x": 1e9}]},
        {"target": A, "popups": [{k: v for k, v in entry.items() if k != "height"}]},
        {"target": A, "popups": [entry] * (types.MAX_POPUPS + 1)},
    ):
        with pytest.raises(ValueError):
            types.parse_popups(json.dumps(bad))
    assert len(types.parse_popups(json.dumps({**good, "popups": [entry] * 16})).popups) == 16


def test_parse_cursor() -> None:
    assert types.parse_cursor('{"x": -5, "y": 10.25}') == types.CursorPos(-5.0, 10.25)
    for bad in (
        {"x": 1},
        {"x": "1", "y": 2},
        {"x": None, "y": 2},
        {"x": True, "y": 2},
        {"x": 100_001, "y": 2},
        {"x": 1, "y": -100_001},
        {"x": 10**400, "y": 0},
        {"x": [1], "y": 0},
    ):
        with pytest.raises(ValueError):
            types.parse_cursor(json.dumps(bad))


def test_parse_window_at() -> None:
    assert types.parse_window_at(json.dumps({"x": 1, "y": -2, "window": None})) == types.WindowAt(
        1, -2, None
    )
    found = types.parse_window_at(json.dumps({"x": 1, "y": 2, "window": GOOD_INFO}))
    assert found.window == types.parse_window_info(info())
    for bad in (
        {"x": 1, "y": 2},  # "no window" must be said, not implied
        {"x": 1.5, "y": 2, "window": None},
        {"x": 1, "y": 100_001, "window": None},
        {"x": 1, "y": 2, "window": "kate"},
        {"x": 1, "y": 2, "window": []},
        {"x": 1, "y": 2, "window": {**GOOD_INFO, "uuid": "x"}},
        {"x": 1, "y": 2, "window": {}},
    ):
        with pytest.raises(ValueError):
            types.parse_window_at(json.dumps(bad))


@pytest.mark.parametrize("payload", ["", "not json", "[]", "null", "5", b"{}", None, "[" * 100_000])
def test_new_parsers_reject_malformed_payloads(payload: Any) -> None:
    for parse in (
        types.parse_window_info,
        types.parse_popups,
        types.parse_cursor,
        types.parse_desktop_changed,
        types.parse_window_at,
    ):
        with pytest.raises(ValueError):
            parse(payload)


def test_oversized_payloads_are_rejected_before_parsing() -> None:
    with pytest.raises(ValueError):
        types.parse_window_info(info(caption="x" * types.MAX_PAYLOAD_CHARS))
    with pytest.raises(ValueError):
        types.parse_desktop_changed(json.dumps({"pad": "x" * types.MAX_PAYLOAD_CHARS}))


def test_new_dataclasses_are_frozen() -> None:
    for value, field in (
        (types.CursorPos(1, 2), "x"),
        (types.Popup(MENU, Geometry(0, 0, 1, 1)), "uuid"),
        (types.PopupSet(A, ()), "target"),
        (types.WindowAt(1, 2, None), "window"),
    ):
        with pytest.raises(AttributeError):
            setattr(value, field, 5)
