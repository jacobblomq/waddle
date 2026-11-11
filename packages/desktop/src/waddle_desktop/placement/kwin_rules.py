"""Install, check and remove Waddle's KWin window rules (ARCHITECTURE §4.2, D-004).

Two rules — the penguin window and the highlight overlay — are written to ``kwinrulesrc`` with
``kwriteconfig6``. A manifest in Waddle's data dir records every key that was written and what
``[General]`` looked like before, so ``remove`` deletes exactly those keys and nothing else.

The rule values are plain data (``_FLAGS``, ``desired_rules``). Several are UNVERIFIED on a real
Plasma 6.6 session and are marked ``S1.2``; adjusting them needs no change to the installer.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from typing import Final

from waddle_core.identity import APP_ID, OVERLAY_WINDOW_TITLE, PENGUIN_WINDOW_TITLE
from waddle_desktop import log
from waddle_desktop.placement.own_exec import (
    KWIN_RECONFIGURE,
    OWN_GROUP_PREFIX,
    OwnExecError,
    run_own,
    validate,
)

PENGUIN_GROUP: Final = "waddle-penguin"
OVERLAY_GROUP: Final = "waddle-overlay"

_EXACT: Final = "1"  # match type: 1 Exact, 2 Substring, 3 RegExp
_FORCE: Final = "2"  # rule policy: 2 Force, 3 Apply initially, 4 Remember
_APPLY_INITIALLY: Final = "3"

# Applied to both windows.
_FLAGS: Final[Mapping[str, str]] = {
    "above": "true",
    "aboverule": _FORCE,
    "skiptaskbar": "true",
    "skiptaskbarrule": _FORCE,
    "skippager": "true",
    "skippagerrule": _FORCE,
    "skipswitcher": "true",
    "skipswitcherrule": _FORCE,
    "noborder": "true",
    "noborderrule": _FORCE,
    # S1.2 UNVERIFIED: there is no "onalldesktops" key; an empty desktop list is assumed to
    # mean "all desktops". Confirm against a rule saved by the KDE settings module.
    "desktops": "",
    "desktopsrule": _FORCE,
}

Run = Callable[[Sequence[str]], CompletedProcess[str]]


@dataclass(frozen=True)
class RuleSpec:
    group: str
    description: str
    title: str
    extra: Mapping[str, str] = field(default_factory=dict)

    def entries(self) -> dict[str, str]:
        """Every key/value of this rule's group, in the order it is written."""
        return {
            "Description": self.description,
            "wmclass": APP_ID,
            "wmclassmatch": _EXACT,
            # S1.2 UNVERIFIED: that a Qt Wayland window reports APP_ID as its class, and that
            # matching the class alone (not "name class") is what `false` selects here.
            "wmclasscomplete": "false",
            "title": self.title,
            "titlematch": _EXACT,
            **self.extra,
        }


def desired_rules(position: tuple[int, int] | None) -> list[RuleSpec]:
    """The rules Waddle wants. ``position`` is the penguin's initial top-left corner, if known.

    S1.2 UNVERIFIED: whether two rules (one per exact title) are needed, or one would do.
    """
    penguin = dict(_FLAGS)
    if position is not None:
        x, y = position
        if x < 0 or y < 0:  # "-5,10" would be read as an option by kwriteconfig6
            raise ValueError("initial position must not be negative")
        # S1.2 UNVERIFIED: that KWin honours a position rule for a Wayland window. "Apply
        # initially" so a later drag by the user is not overridden.
        penguin["position"] = f"{x},{y}"
        penguin["positionrule"] = _APPLY_INITIALLY
    return [
        RuleSpec(PENGUIN_GROUP, "Waddle penguin", PENGUIN_WINDOW_TITLE, penguin),
        RuleSpec(OVERLAY_GROUP, "Waddle highlight overlay", OVERLAY_WINDOW_TITLE, _FLAGS),
    ]


def _config(rules_file: Path, group: str, key: str) -> list[str]:
    return ["--file", str(rules_file), "--group", group, "--key", key]


def _write(rules_file: Path, group: str, key: str, value: str) -> list[str]:
    return ["kwriteconfig6", *_config(rules_file, group, key), value]


def _delete(rules_file: Path, group: str, key: str) -> list[str]:
    return ["kwriteconfig6", *_config(rules_file, group, key), "--delete"]


def plan_install(
    current_general_rules: Sequence[str], specs: Iterable[RuleSpec], rules_file: Path
) -> list[list[str]]:
    """The ``kwriteconfig6`` calls that write ``specs`` and list them after the existing rules.

    Pure. The groups are written before ``[General]`` so KWin never sees a listed rule whose
    group does not exist yet.
    """
    specs = list(specs)
    plan = [
        _write(rules_file, spec.group, key, value)
        for spec in specs
        for key, value in spec.entries().items()
    ]
    rules = list(current_general_rules)
    rules += [spec.group for spec in specs if spec.group not in rules]
    plan.append(_write(rules_file, "General", "rules", ",".join(rules)))
    plan.append(_write(rules_file, "General", "count", str(len(rules))))
    return plan


def plan_remove(
    current_general_rules: Sequence[str],
    groups: Mapping[str, Iterable[str]],
    rules_file: Path,
    *,
    general_before: Mapping[str, str] | None = None,
) -> list[list[str]]:
    """The calls that unlist ``groups`` (group name -> keys Waddle wrote) and delete those keys.

    Pure. ``general_before`` holds the raw ``count``/``rules`` values read before the first
    install: a key that was absent then is deleted rather than left behind empty.
    """
    before = general_before or {}
    rules = [name for name in current_general_rules if name not in groups]
    plan = []
    for key, value in (("rules", ",".join(rules)), ("count", str(len(rules)))):
        absent_before = not rules and not before.get(key)
        plan.append(
            _delete(rules_file, "General", key)
            if absent_before
            else _write(rules_file, "General", key, value)
        )
    plan += [_delete(rules_file, group, key) for group, keys in groups.items() for key in keys]
    return plan


def _check(plan: Iterable[Sequence[str]]) -> None:
    """Raise ``OwnExecError`` if any call of ``plan`` is off the own-resources allowlist."""
    for argv in plan:
        validate(argv)


@dataclass(frozen=True)
class RuleStatus:
    installed: bool  # a manifest exists and every recorded rule is listed in General/rules
    groups: list[str]  # recorded rules that are currently listed
    drift: list[str]  # what no longer matches the manifest; empty when all is as written


@dataclass(frozen=True)
class _Manifest:
    general_before: dict[str, str]
    groups: dict[str, dict[str, str]]


class RuleInstaller:
    def __init__(self, rules_file: Path, manifest_file: Path, run: Run = run_own) -> None:
        self._rules_file = rules_file
        self._manifest_file = manifest_file
        self._run = run

    def status(self) -> RuleStatus:
        manifest = self._load_manifest()
        if manifest is None:
            return RuleStatus(installed=False, groups=[], drift=[])
        listed = self._listed_rules()
        drift: list[str] = []
        for group, entries in manifest.groups.items():
            if group not in listed:
                drift.append(f"{group}: not listed in General/rules")
            drift += [
                f"{group}/{key}"
                for key, value in entries.items()
                if self._read(group, key) != value
            ]
        if self._read("General", "count") != str(len(listed)):
            drift.append("General/count")
        groups = [group for group in manifest.groups if group in listed]
        return RuleStatus(len(groups) == len(manifest.groups), groups, drift)

    def install(self, position: tuple[int, int] | None = None) -> bool:
        """Write the rules, or repair them. Returns False when nothing needed to change."""
        specs = desired_rules(position)
        wanted = {spec.group: spec.entries() for spec in specs}
        old = self._load_manifest()
        if old is not None and old.groups == wanted and not self.status().drift:
            return False
        listed = self._listed_rules()
        plan: list[list[str]] = []
        if old is None:
            before = {key: self._read("General", key) for key in ("rules", "count")}
            kept = [name for name in listed if name not in wanted]
            if len(kept) != len(listed):
                # Our groups are listed although no manifest exists (it was lost). They were
                # not there before Waddle, so remove() must not put them back.
                before = {"rules": ",".join(kept), "count": str(len(kept)) if kept else ""}
        else:
            before = old.general_before
            plan += [  # keys written by an earlier install that are no longer wanted
                _delete(self._rules_file, group, key)
                for group, entries in old.groups.items()
                for key in entries
                if key not in wanted.get(group, {})
            ]
            listed = [name for name in listed if name in wanted or name not in old.groups]
        plan += plan_install(listed, specs, self._rules_file)
        _check(plan)  # refuse the whole plan before anything is written
        # Record first: if a write fails half-way, remove() still knows what to undo.
        self._save_manifest(_Manifest(before, wanted))
        self._execute(plan)
        log.event("kwin_rules_installed", rules=len(wanted))
        return True

    def remove(self) -> bool:
        """Delete what the manifest records. Returns False when nothing was installed."""
        manifest = self._load_manifest()
        if manifest is None:
            return False
        if self._rules_file.exists():
            plan = plan_remove(
                self._listed_rules(),
                manifest.groups,
                self._rules_file,
                general_before=manifest.general_before,
            )
            _check(plan)
            self._execute(plan)
        self._manifest_file.unlink()
        log.event("kwin_rules_removed", rules=len(manifest.groups))
        return True

    def reconfigure(self) -> None:
        """Ask KWin to re-read its rules. KWin may not be running, so failure is only logged."""
        try:
            code = self._run(list(KWIN_RECONFIGURE)).returncode
        except OwnExecError:
            code = -1
        if code != 0:
            log.warning("kwin_reconfigure_failed", code=code)

    # -- reading and writing through own_exec ------------------------------------------------

    def _read(self, group: str, key: str) -> str:
        if not self._rules_file.exists():
            return ""
        result = self._run(["kreadconfig6", *_config(self._rules_file, group, key)])
        if result.returncode != 0:
            raise OwnExecError(f"kreadconfig6 exited with {result.returncode}")
        return result.stdout.removesuffix("\n")

    def _listed_rules(self) -> list[str]:
        return [name for name in self._read("General", "rules").split(",") if name]

    def _execute(self, plan: Iterable[Sequence[str]]) -> None:
        for argv in plan:
            result = self._run(argv)
            if result.returncode != 0:
                raise OwnExecError(f"{argv[0]} exited with {result.returncode}")

    # -- manifest (one of Waddle's own files) ------------------------------------------------

    def _load_manifest(self) -> _Manifest | None:
        try:
            raw = json.loads(self._manifest_file.read_text(encoding="utf-8"))
            # The manifest decides what remove() unlists and deletes, so one that names a
            # group that is not Waddle's is not trusted at all.
            if not all(str(group).startswith(OWN_GROUP_PREFIX) for group in raw["groups"]):
                raise ValueError("manifest names a foreign group")
            return _Manifest(
                {str(k): str(v) for k, v in raw["general_before"].items()},
                {
                    str(group): {str(k): str(v) for k, v in entries.items()}
                    for group, entries in raw["groups"].items()
                },
            )
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, AttributeError):
            log.warning("kwin_rules_manifest_unreadable")
            return None

    def _save_manifest(self, manifest: _Manifest) -> None:
        """Atomic replace; the temporary file is created with mode 0600 by ``mkstemp``."""
        parent = self._manifest_file.parent
        parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "rules_file": str(self._rules_file),
            "general_before": manifest.general_before,
            "groups": manifest.groups,
        }
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".kwin-rules-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._manifest_file)  # noqa: PTH105 - tmp is a str from mkstemp
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)  # noqa: PTH108
            raise
