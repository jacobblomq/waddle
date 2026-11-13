"""Review follow-ups for the version waterfall: transient probe failures and "Release Candidate"."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from waddle_desktop.identity.cache import UNKNOWN_IDENTITY, AppIdentity, IdentityCache
from waddle_desktop.identity.packaging import Packaging
from waddle_desktop.identity.packs import load_packs
from waddle_desktop.identity.version import (
    Channel,
    Version,
    detect_channel,
    parse_version,
    version_text,
)
from waddle_desktop.identity.waterfall import refine_identity, sandbox_probe
from waddle_desktop.sandbox.readonly_exec import ExecResult, Reason

GOOD = load_packs(Path(__file__).parent / "fixtures" / "apps" / "packs").get("good")
assert GOOD is not None  # probe "-V" prints "good <version>"


class Sandbox:
    """Stands in for ``ReadOnlyExec.probe_version``: answers from a script, in order."""

    def __init__(self, *results: ExecResult) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self, exe: Path, flag: str) -> ExecResult:
        self.calls += 1
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


@pytest.fixture
def exe(tmp_path: Path) -> Path:
    path = tmp_path / "opt" / "good"
    path.parent.mkdir()
    path.write_bytes(b"\x7fELF")
    return path


def app(exe: Path) -> AppIdentity:
    return dataclasses.replace(
        UNKNOWN_IDENTITY, app_id="goodapp", name="good", exe=str(exe), packaging=Packaging.OPT
    )


@pytest.mark.parametrize(
    "transient", [Reason.TIMEOUT, Reason.FAILED, Reason.NOT_INSTALLED, Reason.UNAVAILABLE]
)
async def test_a_probe_that_did_not_run_cleanly_is_tried_again(
    exe: Path, tmp_path: Path, transient: Reason
) -> None:
    cache = IdentityCache(tmp_path / "state" / "identity.sqlite")
    sandbox = Sandbox(
        ExecResult(False, "", transient, 3000), ExecResult(True, "good 3.1", Reason.OK, 20)
    )
    probe = sandbox_probe(sandbox)

    first = await refine_identity(app(exe), caption="", pack=GOOD, probe=probe, cache=cache)
    assert first.needs_user and first.identity.version is None

    second = await refine_identity(app(exe), caption="", pack=GOOD, probe=probe, cache=cache)
    assert sandbox.calls == 2  # the bad run was not remembered as "no version"
    assert str(second.identity.version) == "3.1" and second.identity.source == "version-probe"


@pytest.mark.parametrize("reason", [Reason.OK, Reason.TRUNCATED])
async def test_a_clean_run_without_a_version_is_remembered(
    exe: Path, tmp_path: Path, reason: Reason
) -> None:
    cache = IdentityCache(tmp_path / "state" / "identity.sqlite")
    sandbox = Sandbox(ExecResult(True, "usage: good [file]", reason, 20))
    probe = sandbox_probe(sandbox)

    for _ in range(3):
        refined = await refine_identity(app(exe), caption="", pack=GOOD, probe=probe, cache=cache)
        assert refined.needs_user
    assert sandbox.calls == 1


@pytest.mark.parametrize(
    ("text", "expected", "raw"),
    [
        ("Blender 5.0 Release Candidate", Version(5, 0, None, "rc"), "5.0-rc"),
        ("5.0.0 Release Candidate 2", Version(5, 0, 0, "rc"), "5.0.0-rc"),
        ("4.5 (release candidate)", Version(4, 5, None, "rc"), "4.5-rc"),
        ("7.1-release-candidate", Version(7, 1, None, "rc"), "7.1-rc"),
        ("7.1 ReleaseCandidate", Version(7, 1, None, "rc"), "7.1-rc"),
        # Not a tag: a plain release, and words that merely contain the phrase.
        ("5.0 Release", Version(5, 0, None), "5.0"),
        ("5.0 release notes for candidates", Version(5, 0, None), "5.0"),
        ("5.0 prerelease candidates", Version(5, 0, None), "5.0"),
    ],
)
def test_release_candidate_spelled_out_is_rc(text: str, expected: Version, raw: str) -> None:
    parsed = parse_version(text)
    assert parsed == expected and parsed is not None and parsed.pre == expected.pre
    assert version_text(text) == raw


@pytest.mark.parametrize(
    ("raw", "channel"),
    [
        ("Release Candidate 5.0", Channel.RC),
        ("5.0 Release Candidate", Channel.RC),
        ("5.0 Beta, then Release Candidate", Channel.BETA),  # the earlier channel wins
        ("5.0 Release", Channel.STABLE),
        ("candidate 5.0", Channel.STABLE),
    ],
)
def test_release_candidate_names_the_rc_channel(raw: str, channel: Channel) -> None:
    assert detect_channel(raw) is channel
