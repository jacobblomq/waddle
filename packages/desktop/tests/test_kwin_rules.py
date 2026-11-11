"""KWin rule installer: idempotent, repairs drift, and removal restores the file exactly."""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from waddle_core.identity import APP_ID, OVERLAY_WINDOW_TITLE, PENGUIN_WINDOW_TITLE
from waddle_desktop.placement import kwin_rules
from waddle_desktop.placement.kwin_rules import (
    OVERLAY_GROUP,
    PENGUIN_GROUP,
    RuleInstaller,
    RuleSpec,
    desired_rules,
    plan_install,
    plan_remove,
)
from waddle_desktop.placement.own_exec import KWIN_RECONFIGURE, OwnExecError, run_own, validate

FAKE = str(Path(__file__).with_name("fake_kwriteconfig6.py"))
RESOLVER = {
    "kwriteconfig6": [sys.executable, "-I", "-S", FAKE, "kwriteconfig6"],
    "kreadconfig6": [sys.executable, "-I", "-S", FAKE, "kreadconfig6"],
    "dbus-send": [sys.executable, "-c", "raise SystemExit(1)"],  # KWin is not running
}

USER_RULE_1 = """\
[1f0e7a52-9c3b-4d1e-8a77-0b5d2c9e4f10]
Description=Terminal on desktop 2
desktops=3b2c1d0e-aaaa-bbbb-cccc-000000000002
desktopsrule=2
title[$e]=$HOME — Konsole
  a strange indented line without an equals sign
wmclass=konsole
wmclassmatch=1
"""
USER_RULE_2 = """\
[my rule]
Description[de]=Fenster für Firefox
opacityactive=90
opacityactiverule=2
wmclass=firefox
# trailing comment, no newline after the last group"""
USER_FILE = (
    "# sorted by hand, please keep\n"
    + USER_RULE_1
    + "\n[General]\ncount=2\nrules=1f0e7a52-9c3b-4d1e-8a77-0b5d2c9e4f10,my rule\n\n"
    + USER_RULE_2
    + "\n"
)
USER_RULES = ["1f0e7a52-9c3b-4d1e-8a77-0b5d2c9e4f10", "my rule"]
RC = Path("/home/u/.config/kwinrulesrc")


class Recorder:
    """Runs argv through the real guard and the double, and remembers what was asked."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> CompletedProcess[str]:
        self.calls.append(list(argv))
        return run_own(argv, resolver=RESOLVER)

    def writes(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "kwriteconfig6"]


@pytest.fixture
def run() -> Recorder:
    return Recorder()


@pytest.fixture
def rules_file(tmp_path: Path) -> Path:
    return tmp_path / "config" / "kwinrulesrc"


@pytest.fixture
def manifest_file(tmp_path: Path) -> Path:
    return tmp_path / "data" / "waddle" / "kwin-rules.json"


@pytest.fixture
def installer(rules_file: Path, manifest_file: Path, run: Recorder) -> RuleInstaller:
    rules_file.parent.mkdir()
    return RuleInstaller(rules_file, manifest_file, run)


def read(rules_file: Path, group: str, key: str) -> str:
    argv = ["kreadconfig6", "--file", str(rules_file), "--group", group, "--key", key]
    return run_own(argv, resolver=RESOLVER).stdout.removesuffix("\n")


def files_under(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if not p.is_dir())


# -- rule data ----------------------------------------------------------------------------


def test_desired_rules_match_only_the_penguin_and_the_overlay() -> None:
    penguin, overlay = desired_rules(None)
    assert (penguin.group, overlay.group) == ("waddle-penguin", "waddle-overlay")
    assert penguin.entries()["title"] == PENGUIN_WINDOW_TITLE
    assert overlay.entries()["title"] == OVERLAY_WINDOW_TITLE
    for spec in (penguin, overlay):
        entries = spec.entries()
        assert entries["wmclass"] == APP_ID
        assert (entries["wmclassmatch"], entries["titlematch"]) == ("1", "1")  # exact
        for flag in ("above", "skiptaskbar", "skippager", "skipswitcher", "noborder"):
            assert (entries[flag], entries[f"{flag}rule"]) == ("true", "2")
        assert entries["desktopsrule"] == "2"


def test_position_is_included_only_when_given_and_only_for_the_penguin() -> None:
    for spec in desired_rules(None):
        assert "position" not in spec.entries()
        assert "positionrule" not in spec.entries()
    penguin, overlay = desired_rules((1700, 950))
    assert penguin.entries()["position"] == "1700,950"
    assert penguin.entries()["positionrule"] == "3"  # apply initially
    assert "position" not in overlay.entries()
    with pytest.raises(ValueError, match="negative"):
        desired_rules((-10, 5))


# -- planning -----------------------------------------------------------------------------


def test_plan_install_is_pure_and_appends_after_existing_rules() -> None:
    current = ["a", "b"]
    specs = desired_rules((10, 20))
    plan = plan_install(current, specs, RC)

    assert plan == plan_install(current, specs, RC)
    assert current == ["a", "b"]
    head = ["kwriteconfig6", "--file", str(RC), "--group", "General", "--key"]
    assert plan[-2:] == [
        [*head, "rules", "a,b,waddle-penguin,waddle-overlay"],
        [*head, "count", "4"],
    ]
    expected_keys = [(spec.group, key) for spec in specs for key in spec.entries()]
    assert [(argv[4], argv[6]) for argv in plan[:-2]] == expected_keys
    for argv in plan:
        validate(argv)  # every planned call is on the own-resources allowlist


def test_plan_install_does_not_list_a_group_twice() -> None:
    plan = plan_install(["waddle-overlay", "a"], desired_rules(None), RC)
    assert plan[-2][-1] == "waddle-overlay,a,waddle-penguin"
    assert plan[-1][-1] == "3"


def test_plan_install_takes_any_spec_list() -> None:
    spec = RuleSpec("waddle-extra", "Extra", "Waddle Extra", {"above": "true"})
    plan = plan_install([], [spec], RC)
    assert [argv[-1] for argv in plan[-2:]] == ["waddle-extra", "1"]
    assert ["kwriteconfig6", *plan[0][1:5], "--key", "above", "true"] in plan


def test_plan_remove_unlists_only_our_groups_and_deletes_only_recorded_keys() -> None:
    current = ["a", "waddle-penguin", "b", "waddle-overlay"]
    groups = {"waddle-penguin": ["above", "title"], "waddle-overlay": ["above"]}
    plan = plan_remove(current, groups, RC)

    head = ["kwriteconfig6", "--file", str(RC), "--group"]
    assert plan == plan_remove(current, groups, RC)
    assert plan == [
        [*head, "General", "--key", "rules", "a,b"],
        [*head, "General", "--key", "count", "2"],
        [*head, "waddle-penguin", "--key", "above", "--delete"],
        [*head, "waddle-penguin", "--key", "title", "--delete"],
        [*head, "waddle-overlay", "--key", "above", "--delete"],
    ]
    for argv in plan:
        validate(argv)


def test_plan_remove_deletes_general_keys_that_did_not_exist_before() -> None:
    groups = {"waddle-penguin": ["above"]}
    plan = plan_remove(["waddle-penguin"], groups, RC, general_before={"rules": "", "count": ""})
    assert [argv[6:] for argv in plan[:2]] == [["rules", "--delete"], ["count", "--delete"]]
    # ...but an explicit, empty rule list the user already had is written back as it was.
    plan = plan_remove(["waddle-penguin"], groups, RC, general_before={"rules": "", "count": "0"})
    assert [argv[6:] for argv in plan[:2]] == [["rules", "--delete"], ["count", "0"]]


# -- installing ---------------------------------------------------------------------------


def test_install_into_no_file(
    installer: RuleInstaller, rules_file: Path, manifest_file: Path, tmp_path: Path
) -> None:
    assert installer.status().installed is False
    assert installer.install(position=(40, 60)) is True

    assert read(rules_file, "General", "rules") == "waddle-penguin,waddle-overlay"
    assert read(rules_file, "General", "count") == "2"
    assert read(rules_file, PENGUIN_GROUP, "position") == "40,60"
    for spec in desired_rules((40, 60)):
        for key, value in spec.entries().items():
            assert read(rules_file, spec.group, key) == value
    status = installer.status()
    assert (status.installed, status.groups, status.drift) == (
        True,
        [PENGUIN_GROUP, OVERLAY_GROUP],
        [],
    )
    assert files_under(tmp_path) == ["config/kwinrulesrc", "data/waddle/kwin-rules.json"]


def test_install_preserves_the_users_rules_byte_for_byte(
    installer: RuleInstaller, rules_file: Path, tmp_path: Path
) -> None:
    rules_file.write_text(USER_FILE, encoding="utf-8")
    assert installer.install() is True

    content = rules_file.read_text(encoding="utf-8")
    assert content.startswith("# sorted by hand, please keep\n" + USER_RULE_1 + "\n[General]\n")
    assert USER_RULE_2 + "\n" in content
    assert content.index("[my rule]") < content.index("[waddle-penguin]")
    assert read(rules_file, "General", "rules").split(",") == [
        *USER_RULES,
        PENGUIN_GROUP,
        OVERLAY_GROUP,
    ]
    assert read(rules_file, "General", "count") == "4"
    assert read(rules_file, OVERLAY_GROUP, "title") == OVERLAY_WINDOW_TITLE
    assert "position" not in content
    assert files_under(tmp_path) == ["config/kwinrulesrc", "data/waddle/kwin-rules.json"]


@pytest.mark.parametrize("original", [None, USER_FILE], ids=["no file", "user rules"])
def test_second_install_writes_nothing(
    installer: RuleInstaller, rules_file: Path, run: Recorder, original: str | None
) -> None:
    if original is not None:
        rules_file.write_text(original, encoding="utf-8")
    assert installer.install(position=(5, 6)) is True
    after_first = rules_file.read_bytes()
    assert run.writes() != []

    run.calls.clear()
    assert installer.install(position=(5, 6)) is False
    assert run.writes() == []
    assert rules_file.read_bytes() == after_first


def test_install_then_remove_restores_the_users_file_exactly(
    installer: RuleInstaller, rules_file: Path, manifest_file: Path, tmp_path: Path
) -> None:
    rules_file.write_text(USER_FILE, encoding="utf-8")
    installer.install(position=(40, 60))
    assert rules_file.read_text(encoding="utf-8") != USER_FILE

    assert installer.remove() is True
    assert rules_file.read_bytes() == USER_FILE.encode("utf-8")
    assert not manifest_file.exists()
    assert installer.status().installed is False
    assert files_under(tmp_path) == ["config/kwinrulesrc"]


LEGACY_FILE = """[1]
Description=Firefox on desktop 2
wmclass=firefox
desktops=abc
desktopsrule=2

[2]
Description=Konsole small
wmclass=konsole

[General]
count=2
"""


def test_legacy_numbered_rules_stay_listed_and_are_restored_exactly(
    installer: RuleInstaller, rules_file: Path
) -> None:
    """A pre-`rules=` kwinrulesrc: writing our own list must not unlist the user's rules."""
    rules_file.write_text(LEGACY_FILE, encoding="utf-8")
    installer.install()
    listed = read(rules_file, "General", "rules").split(",")
    assert listed[:2] == ["1", "2"] and set(listed[2:]) == {PENGUIN_GROUP, OVERLAY_GROUP}
    assert read(rules_file, "General", "count") == "4"

    assert installer.remove() is True
    assert rules_file.read_bytes() == LEGACY_FILE.encode("utf-8")


@pytest.mark.parametrize("ending", ["\n", "\n\n\n", ""], ids=["blank", "blanks", "no newline"])
def test_remove_restores_a_file_with_an_unusual_ending(
    installer: RuleInstaller, rules_file: Path, ending: str
) -> None:
    original = (USER_FILE.rstrip("\n") + ending).encode("utf-8")
    rules_file.write_bytes(original)
    installer.install(position=(1, 2))
    installer.remove()
    restored = rules_file.read_bytes()
    # A missing final newline is the one thing the line-based double cannot give back.
    assert restored == (original if ending else original + b"\n")


def test_reinstall_after_a_lost_manifest_does_not_record_our_groups_as_the_users(
    installer: RuleInstaller, rules_file: Path, manifest_file: Path
) -> None:
    rules_file.write_text(USER_FILE, encoding="utf-8")
    installer.install()
    manifest_file.unlink()

    assert installer.install() is True
    before = json.loads(manifest_file.read_text(encoding="utf-8"))["general_before"]
    assert before == {"rules": ",".join(USER_RULES), "count": "2"}
    assert installer.remove() is True
    assert rules_file.read_bytes() == USER_FILE.encode("utf-8")


def test_reinstall_after_a_lost_manifest_into_our_own_file_removes_cleanly(
    installer: RuleInstaller, rules_file: Path, manifest_file: Path
) -> None:
    installer.install()
    manifest_file.unlink()
    installer.install()
    installer.remove()
    assert rules_file.read_bytes() == b""


def test_a_plan_the_guard_would_refuse_writes_nothing_at_all(
    installer: RuleInstaller,
    rules_file: Path,
    manifest_file: Path,
    run: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules_file.write_text(USER_FILE, encoding="utf-8")
    bad = [
        RuleSpec("waddle-penguin", "ok", "Waddle", {"above": "true"}),
        RuleSpec("waddle-overlay", "ok", "Waddle Highlight", {"position": "-5,1"}),
    ]
    monkeypatch.setattr(kwin_rules, "desired_rules", lambda _position: bad)

    with pytest.raises(OwnExecError):
        installer.install()
    assert run.writes() == []
    assert not manifest_file.exists()
    assert rules_file.read_text(encoding="utf-8") == USER_FILE


def test_a_manifest_naming_a_foreign_group_is_not_acted_on(
    installer: RuleInstaller, rules_file: Path, manifest_file: Path, run: Recorder
) -> None:
    rules_file.write_text(USER_FILE, encoding="utf-8")
    installer.install()
    installed = rules_file.read_bytes()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["groups"]["my rule"] = {"wmclass": "firefox"}
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    run.calls.clear()

    assert installer.status().installed is False
    assert installer.remove() is False
    assert run.writes() == []
    assert rules_file.read_bytes() == installed


def test_install_then_remove_from_no_file_leaves_an_empty_file(
    installer: RuleInstaller, rules_file: Path, manifest_file: Path
) -> None:
    installer.install(position=(40, 60))
    assert installer.remove() is True
    assert rules_file.read_bytes() == b""
    assert not manifest_file.exists()


def test_remove_keeps_rules_the_user_added_after_install(
    installer: RuleInstaller, rules_file: Path
) -> None:
    rules_file.write_text(USER_FILE, encoding="utf-8")
    installer.install()
    listed = read(rules_file, "General", "rules")
    with rules_file.open("a", encoding="utf-8") as handle:
        handle.write("\n[later]\nwmclass=dolphin\n")
    text = rules_file.read_text(encoding="utf-8")
    text = text.replace(f"rules={listed}", f"rules=later,{listed}").replace("count=4", "count=5")
    rules_file.write_text(text, encoding="utf-8")

    installer.remove()
    assert read(rules_file, "General", "rules").split(",") == ["later", *USER_RULES]
    assert read(rules_file, "General", "count") == "3"
    assert "[later]\nwmclass=dolphin\n" in rules_file.read_text(encoding="utf-8")
    assert "waddle" not in rules_file.read_text(encoding="utf-8")


def test_remove_when_nothing_is_installed_is_a_no_op(
    installer: RuleInstaller, rules_file: Path, run: Recorder, tmp_path: Path
) -> None:
    assert installer.remove() is False
    assert run.calls == []
    assert files_under(tmp_path) == []

    rules_file.write_text(USER_FILE, encoding="utf-8")
    assert installer.remove() is False
    assert run.writes() == []
    assert rules_file.read_text(encoding="utf-8") == USER_FILE


def test_remove_does_not_touch_waddle_groups_it_has_no_manifest_for(
    installer: RuleInstaller, rules_file: Path, manifest_file: Path
) -> None:
    installer.install()
    installed = rules_file.read_bytes()
    manifest_file.unlink()
    assert installer.remove() is False
    assert rules_file.read_bytes() == installed


# -- drift and repair ---------------------------------------------------------------------


def test_manually_deleted_rule_is_reported_and_repaired(
    installer: RuleInstaller, rules_file: Path, run: Recorder
) -> None:
    rules_file.write_text(USER_FILE, encoding="utf-8")
    installer.install()
    good = rules_file.read_bytes()

    # The user deletes the penguin rule the way the settings module would: group and listing.
    text = rules_file.read_text(encoding="utf-8")
    start = text.index("\n[waddle-penguin]\n")
    text = text[:start] + text[text.index("\n[waddle-overlay]\n") :]
    text = text.replace(",waddle-penguin", "").replace("count=4", "count=3")
    rules_file.write_text(text, encoding="utf-8")

    status = installer.status()
    assert status.installed is False
    assert status.groups == [OVERLAY_GROUP]
    assert "waddle-penguin: not listed in General/rules" in status.drift
    assert "waddle-penguin/above" in status.drift
    assert not any(item.startswith("waddle-overlay") for item in status.drift)

    assert installer.install() is True
    status = installer.status()
    assert (status.installed, status.drift) == (True, [])
    assert read(rules_file, "General", "rules").split(",") == [
        *USER_RULES,
        OVERLAY_GROUP,
        PENGUIN_GROUP,
    ]
    assert read(rules_file, "General", "count") == "4"
    assert read(rules_file, PENGUIN_GROUP, "above") == "true"
    assert len(rules_file.read_bytes()) == len(good)

    assert installer.remove() is True
    assert rules_file.read_bytes() == USER_FILE.encode("utf-8")


def test_an_edited_key_and_a_wrong_count_are_drift(
    installer: RuleInstaller, rules_file: Path
) -> None:
    installer.install()
    text = rules_file.read_text(encoding="utf-8")
    text = text.replace("count=2", "count=7").replace("noborder=true", "noborder=false", 1)
    rules_file.write_text(text, encoding="utf-8")

    status = installer.status()
    assert status.installed is True  # still listed, just not as written
    assert status.drift == ["waddle-penguin/noborder", "General/count"]

    assert installer.install() is True
    assert installer.status().drift == []
    assert "noborder=false" not in rules_file.read_text(encoding="utf-8")


def test_changing_and_dropping_the_position_updates_the_rule(
    installer: RuleInstaller, rules_file: Path
) -> None:
    installer.install(position=(1, 2))
    assert installer.install(position=(3, 4)) is True
    assert read(rules_file, PENGUIN_GROUP, "position") == "3,4"

    assert installer.install() is True
    assert "position" not in rules_file.read_text(encoding="utf-8")
    assert installer.status().drift == []
    assert installer.install() is False

    installer.remove()
    assert rules_file.read_bytes() == b""


# -- manifest -----------------------------------------------------------------------------


def test_manifest_records_exactly_what_was_written_with_mode_0600(
    installer: RuleInstaller, rules_file: Path, manifest_file: Path, run: Recorder
) -> None:
    rules_file.write_text(USER_FILE, encoding="utf-8")
    installer.install(position=(40, 60))

    assert stat.S_IMODE(manifest_file.stat().st_mode) == 0o600
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest["general_before"] == {"rules": ",".join(USER_RULES), "count": "2"}
    assert manifest["groups"] == {spec.group: spec.entries() for spec in desired_rules((40, 60))}
    written = {(argv[4], argv[6]) for argv in run.writes() if argv[4] != "General"}
    recorded = {(group, key) for group, keys in manifest["groups"].items() for key in keys}
    assert written == recorded


def test_manifest_is_replaced_atomically(
    installer: RuleInstaller, manifest_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer.install(position=(1, 2))
    before = manifest_file.read_bytes()
    replaced: list[tuple[str, str]] = []

    def failing_replace(src: str | Path, dst: str | Path) -> None:
        replaced.append((str(src), str(dst)))
        assert Path(src).parent == manifest_file.parent  # same directory, so rename is atomic
        assert stat.S_IMODE(Path(src).stat().st_mode) == 0o600
        raise OSError("disk full")

    monkeypatch.setattr(kwin_rules.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        installer.install(position=(3, 4))

    assert [dst for _, dst in replaced] == [str(manifest_file)]
    assert manifest_file.read_bytes() == before  # the old manifest is intact
    assert list(manifest_file.parent.iterdir()) == [manifest_file]  # no temp file left


def test_manifest_is_written_before_the_first_config_write(
    rules_file: Path, manifest_file: Path
) -> None:
    rules_file.parent.mkdir()

    def run(argv: Sequence[str]) -> CompletedProcess[str]:
        if argv[0] == "kwriteconfig6":
            assert manifest_file.exists()
            return CompletedProcess(argv, 1, "", "")
        return run_own(argv, resolver=RESOLVER)

    with pytest.raises(OwnExecError, match="kwriteconfig6 exited with 1"):
        RuleInstaller(rules_file, manifest_file, run).install()
    assert manifest_file.exists()


def test_unreadable_manifest_counts_as_not_installed(
    installer: RuleInstaller, manifest_file: Path
) -> None:
    manifest_file.parent.mkdir(parents=True)
    manifest_file.write_text('{"groups": [1, 2]', encoding="utf-8")
    assert installer.status().installed is False
    assert installer.remove() is False


# -- reconfigure --------------------------------------------------------------------------


def test_reconfigure_sends_the_fixed_call_and_tolerates_failure(
    installer: RuleInstaller, run: Recorder
) -> None:
    installer.reconfigure()  # the double exits 1, as dbus-send does without KWin
    assert run.calls == [list(KWIN_RECONFIGURE)]


def test_reconfigure_tolerates_a_missing_dbus_send(rules_file: Path, manifest_file: Path) -> None:
    def run(argv: Sequence[str]) -> CompletedProcess[str]:
        return run_own(argv, resolver={})

    RuleInstaller(rules_file, manifest_file, run).reconfigure()
