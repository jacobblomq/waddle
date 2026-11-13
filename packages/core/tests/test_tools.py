"""The host tool contract: the closed list of things the brain may ask the host for."""

import re
from typing import Any

import pytest
from pydantic import ValidationError

from waddle_core import tools
from waddle_core.tools import (
    ManArgs,
    NoArgs,
    SystemStatsArgs,
    TldrArgs,
    ToolError,
    ToolName,
    validate_tool_call,
)


def refused(tool: Any, args: Any, code: str) -> None:
    with pytest.raises(ToolError) as caught:
        validate_tool_call(tool, args)
    assert caught.value.code == code
    assert str(caught.value) == code  # arguments never leak into the error text


# ---- the tool list -----------------------------------------------------------------------


def test_the_tool_list_is_closed_and_every_tool_has_a_schema() -> None:
    assert {t.value for t in ToolName} == {"get_app_state", "get_system_stats", "man", "tldr"}
    assert set(tools.ARGS_FOR) == set(ToolName)


def test_tool_names_fit_the_link_field() -> None:
    assert all(len(t.value) <= 32 for t in ToolName)  # link.ToolCall.tool max_length


def test_caps_are_sane() -> None:
    assert tools.MAX_TOOL_CALLS_PER_REQUEST == 4
    assert tools.MAX_TOOL_RESULT_BYTES == 32 * 1024


@pytest.mark.parametrize(
    "tool",
    [
        "",
        "sh",
        "bash",
        "run",
        "exec",
        "MAN",
        "Man",
        "man ",
        " man",
        "man\n",
        "man\x00",
        "man;ls",
        "GET_APP_STATE",
        "ToolName.MAN",
        "get-app-state",
        "m\u0430n",  # Cyrillic a
        "\uff4dan",  # full-width m
    ],
    ids=repr,
)
def test_unknown_tool_is_refused(tool: str) -> None:
    refused(tool, {}, "unknown_tool")
    refused(tool, {"name": "ls"}, "unknown_tool")


@pytest.mark.parametrize("tool", [None, 1, 1.5, True, b"man", ["man"], ("man",), {"man": 1}, {}])
def test_tool_name_that_is_not_text_is_refused(tool: Any) -> None:
    refused(tool, {}, "unknown_tool")


@pytest.mark.parametrize(
    "args",
    [None, "", "ls", b"ls", 1, 1.5, True, ["ls"], ("name", "ls"), [("name", "ls")], {"name"}],
    ids=repr,
)
@pytest.mark.parametrize("tool", list(ToolName))
def test_arguments_that_are_not_a_dict_are_refused(tool: ToolName, args: Any) -> None:
    refused(tool.value, args, "bad_arguments")


def test_a_ready_made_arguments_object_is_not_a_dict() -> None:
    refused("man", ManArgs(name="ls"), "bad_arguments")


def test_unknown_tool_is_reported_before_bad_arguments() -> None:
    refused("nope", "not a dict", "unknown_tool")


# ---- get_app_state -----------------------------------------------------------------------


def test_get_app_state_takes_no_arguments() -> None:
    name, args = validate_tool_call("get_app_state", {})
    assert name is ToolName.GET_APP_STATE and type(args) is NoArgs


@pytest.mark.parametrize("args", [{"x": 1}, {"name": "ls"}, {"": ""}, {1: 2}, {None: None}])
def test_get_app_state_refuses_any_argument(args: dict[Any, Any]) -> None:
    refused("get_app_state", args, "bad_arguments")


# ---- get_system_stats --------------------------------------------------------------------


@pytest.mark.parametrize("topic", ["cpu", "memory", "disk", "gpu", "uptime"])
def test_system_stats_topics(topic: str) -> None:
    name, args = validate_tool_call("get_system_stats", {"topic": topic})
    assert name is ToolName.GET_SYSTEM_STATS
    assert type(args) is SystemStatsArgs and args.topic == topic


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"topic": ""},
        {"topic": "CPU"},
        {"topic": "cpu "},
        {"topic": "cpu\n"},
        {"topic": "network"},
        {"topic": "processes"},
        {"topic": "cpu;id"},
        {"topic": None},
        {"topic": 0},
        {"topic": True},
        {"topic": ["cpu"]},
        {"topic": b"cpu"},
        {"topic": "cpu", "extra": 1},
        {"topic": "cpu", "pid": 1},
        {"Topic": "cpu"},
    ],
    ids=repr,
)
def test_system_stats_bad_arguments(args: dict[str, Any]) -> None:
    refused("get_system_stats", args, "bad_arguments")


# ---- man and tldr ------------------------------------------------------------------------

GOOD_NAMES = [
    "ls",
    "a",
    "7z",
    "g++",
    "systemd.unit",
    "git-commit",
    "x86_64-linux-gnu-gcc",
    "Xorg",
    "ld.so",
    "a" * 64,
]
BAD_NAMES = [
    "",
    "a" * 65,
    "-",
    "-h",
    "--help",
    "-Pcat",
    "--pager=sh",
    ".",
    "..",
    ".hidden",
    "_x",
    "+x",
    "/etc/passwd",
    "a/b",
    "../ls",
    "a\\b",
    "ls -la",
    " ls",
    "ls ",
    "ls\t",
    "ls\n",
    "\nls",
    "ls\r",
    "ls\x00",
    "ls\x00--help",
    "ls;id",
    "ls|id",
    "ls&id",
    "ls&&id",
    "$(id)",
    "`id`",
    "${HOME}",
    "$HOME",
    "a>b",
    "a<b",
    "a*",
    "a?",
    "[a]",
    "{a,b}",
    "~root",
    "a'b",
    'a"b',
    "a#b",
    "a!b",
    "a=b",
    "a:b",
    "a,b",
    "a(1)",
    "a%b",
    "a@b",
    "http://x",
    "l\u0455",  # Cyrillic s
    "\u217cs",  # small roman numeral fifty, looks like l
    "\uff4c\uff53",  # full-width ls
    "ls\u200b",  # zero-width space
    "ls\u202e",  # right-to-left override
    "caf\u00e9",
    "e\u0301",  # e + combining accent
    "\u0661",  # Arabic-Indic digit one
    "\u00b2",  # superscript two (a "digit" to str.isdigit)
    "\U0001f427",
    "ls\u2028",
    "ls\ud800",  # lone surrogate
]


@pytest.mark.parametrize("tool,model", [("man", ManArgs), ("tldr", TldrArgs)])
@pytest.mark.parametrize("good", GOOD_NAMES)
def test_good_names_are_accepted(tool: str, model: type, good: str) -> None:
    name, args = validate_tool_call(tool, {"name": good})
    assert name.value == tool and type(args) is model
    assert args.name == good  # type: ignore[union-attr]


@pytest.mark.parametrize("tool", ["man", "tldr"])
@pytest.mark.parametrize("bad", BAD_NAMES, ids=repr)
def test_bad_names_are_refused(tool: str, bad: str) -> None:
    refused(tool, {"name": bad}, "bad_arguments")


@pytest.mark.parametrize("tool", ["man", "tldr"])
@pytest.mark.parametrize(
    "bad", [None, 1, 1.5, True, b"ls", ["ls"], ("ls",), {"ls": 1}, {"name": "ls"}], ids=repr
)
def test_name_is_never_coerced_to_text(tool: str, bad: Any) -> None:
    refused(tool, {"name": bad}, "bad_arguments")


def test_name_must_be_exactly_a_str() -> None:
    class Sneaky(str):
        """A str whose text conversions lie; validation must look at the real value."""

        def __str__(self) -> str:
            return "ls"

    refused("man", {"name": Sneaky("--help")}, "bad_arguments")


@pytest.mark.parametrize("tool", ["man", "tldr"])
def test_name_is_required_and_extras_are_refused(tool: str) -> None:
    refused(tool, {}, "bad_arguments")
    refused(tool, {"name": "ls", "extra": 1}, "bad_arguments")
    refused(tool, {"name": "ls", "args": ["-P", "sh"]}, "bad_arguments")
    refused(tool, {"name": "ls", "path": "/usr/share/man"}, "bad_arguments")
    refused(tool, {"Name": "ls"}, "bad_arguments")
    refused(tool, {"name": "ls", 1: 2}, "bad_arguments")


@pytest.mark.parametrize("section", ["1", "2", "3", "4", "5", "6", "7", "8"])
def test_man_sections(section: str) -> None:
    _, args = validate_tool_call("man", {"name": "ls", "section": section})
    assert isinstance(args, ManArgs) and args.section == section


def test_man_section_is_optional() -> None:
    _, args = validate_tool_call("man", {"name": "ls"})
    assert isinstance(args, ManArgs) and args.section is None
    _, args = validate_tool_call("man", {"name": "ls", "section": None})
    assert isinstance(args, ManArgs) and args.section is None


@pytest.mark.parametrize(
    "section",
    [1, 1.0, True, "", "0", "9", "10", "1 ", " 1", "1\n", "3p", "n", "-k", "\uff11", b"1", ["1"]],
    ids=repr,
)
def test_man_bad_section(section: Any) -> None:
    refused("man", {"name": "ls", "section": section}, "bad_arguments")


def test_tldr_has_no_section() -> None:
    refused("tldr", {"name": "ls", "section": "1"}, "bad_arguments")


# ---- the returned objects and the pattern itself -----------------------------------------


def test_arguments_are_frozen() -> None:
    _, args = validate_tool_call("man", {"name": "ls"})
    with pytest.raises(ValidationError):
        args.name = "--help"  # type: ignore[misc,union-attr]


def test_the_callers_dict_is_not_changed() -> None:
    given = {"name": "ls", "section": "1"}
    validate_tool_call("man", given)
    assert given == {"name": "ls", "section": "1"}


def test_models_are_strict_when_built_directly() -> None:
    for build in (
        lambda: ManArgs(name=5),  # type: ignore[arg-type]
        lambda: ManArgs(name="ls", section=1),  # type: ignore[arg-type]
        lambda: TldrArgs(name="-x"),
        lambda: SystemStatsArgs(topic="net"),  # type: ignore[arg-type]
        lambda: NoArgs(x=1),  # type: ignore[call-arg]
    ):
        with pytest.raises(ValidationError):
            build()


def test_name_pattern_agrees_with_the_validator() -> None:
    # NAME_PATTERN is public; anyone matching it by hand must use fullmatch-like semantics.
    # (With re.match, "$" also matches before a trailing newline.)
    pattern = re.compile(tools.NAME_PATTERN)
    for good in GOOD_NAMES:
        assert pattern.fullmatch(good)
    for bad in BAD_NAMES:
        assert not pattern.fullmatch(bad), repr(bad)
