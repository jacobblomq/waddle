"""The capability snapshot and the plain-language degradation table (ARCHITECTURE §8)."""

from __future__ import annotations

import dataclasses

import pytest

from waddle_core import capabilities as caps
from waddle_core.capabilities import Capabilities, Degradation, degradations

ALL_GOOD = Capabilities(
    brain="up", vision=True, capture=True, sandbox=True, packs=14, man=True, tldr=True
)


def whats(cap: Capabilities) -> list[str]:
    return [d.what for d in degradations(cap)]


def test_nothing_is_off_when_everything_works() -> None:
    assert degradations(ALL_GOOD) == []
    assert ALL_GOOD.can_answer and ALL_GOOD.can_see_screen


def test_the_unchecked_default_claims_no_ability() -> None:
    cap = Capabilities()
    assert not cap.can_answer and not cap.can_see_screen
    assert whats(cap) == [
        "Answers",
        "Seeing your screen",
        "Version checks, man pages and tldr",
        "App-specific knowledge",
    ]


def test_it_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        ALL_GOOD.brain = "down"  # type: ignore[misc]


def test_a_degradation_is_what_why_and_what_to_do() -> None:
    (only,) = degradations(dataclasses.replace(ALL_GOOD, brain="down", brain_reason="no_socket"))
    what, why, fix = only
    assert only == Degradation(what, why, fix)
    assert (what, why) == ("Answers", "Waddle's brain is not running.")
    assert fix == "Start it with: waddle brain up"


@pytest.mark.parametrize(
    ("reason", "why_has", "fix_has"),
    [
        ("stopped", "not connected", "Wait"),
        ("no_socket", "not running", "waddle brain up"),
        ("protocol", "does not understand", "Update both"),
        ("bad_handshake", "does not understand", "Update both"),
        ("bad_hello", "does not understand", "Update both"),
        ("connect_failed", "not running", "waddle brain up"),
        ("lost", "lost contact", "waddle brain status"),
        ("unsafe_socket", "does not belong to you", "waddle doctor"),
        ("no_token", "key", "waddle doctor"),
        ("bad_token", "did not accept", "waddle brain down"),
        ("bad_version", "different versions", "Update both"),
        ("busy", "Another Waddle", "Close the other"),
        ("handshake_timeout", "did not answer in time", "waddle doctor"),
    ],
)
def test_each_brain_reason_has_its_own_plain_line(reason: str, why_has: str, fix_has: str) -> None:
    (only,) = degradations(dataclasses.replace(ALL_GOOD, brain="down", brain_reason=reason))
    assert only.what == "Answers"
    assert why_has in only.why and fix_has in only.fix


def test_an_unknown_reason_code_is_never_shown() -> None:
    cap = dataclasses.replace(
        ALL_GOOD,
        brain="down",
        brain_reason="<b>ignore previous</b>",
        capture=False,
        capture_reason="rm -rf",
        sandbox=False,
        sandbox_reason="???",
    )
    text = " ".join(part for d in degradations(cap) for part in d)
    assert "ignore" not in text and "rm -rf" not in text and "???" not in text
    assert whats(cap) == ["Answers", "Seeing your screen", "Version checks, man pages and tldr"]


def test_connecting_is_said_as_such_not_as_a_fault() -> None:
    (only,) = degradations(dataclasses.replace(ALL_GOOD, brain="connecting"))
    assert "still reaching" in only.why and only.fix == "Wait a moment."
    assert not dataclasses.replace(ALL_GOOD, brain="connecting").can_answer


@pytest.mark.parametrize(
    ("code", "why_has"),
    [
        (caps.CAPTURE_HELPER_MISSING, "not installed"),
        (caps.CAPTURE_HELPER_FAILED, "did not start"),
        (caps.CAPTURE_NO_KWIN, "KWin"),
        (caps.CAPTURE_LOCK_UNKNOWN, "locked"),
        (caps.CAPTURE_CONFIG_UNREADABLE, "config file"),
    ],
)
def test_capture_off_says_why_and_answers_still_work(code: str, why_has: str) -> None:
    cap = dataclasses.replace(ALL_GOOD, capture=False, capture_reason=code)
    (only,) = degradations(cap)
    assert only.what == "Seeing your screen" and why_has in only.why
    assert cap.can_answer and not cap.can_see_screen


def test_no_vision_model_is_reported_only_while_the_brain_is_up() -> None:
    blind_model = dataclasses.replace(ALL_GOOD, vision=False)
    assert whats(blind_model) == ["Reading screenshots"]
    assert not blind_model.can_see_screen
    # With the brain down its models are not a second problem.
    assert whats(dataclasses.replace(blind_model, brain="down")) == ["Answers"]


@pytest.mark.parametrize(
    ("code", "why", "fix"),
    [
        (
            caps.SANDBOX_NO_BWRAP,
            "bwrap is not installed.",
            "Install bubblewrap, then run: waddle doctor",
        ),
        (
            caps.SANDBOX_NO_PRLIMIT,
            "prlimit is not installed.",
            "Install util-linux, then run: waddle doctor",
        ),
        # Installing something would not help with these two: do not say it would.
        (caps.SANDBOX_SETUP_FAILED, "The sandbox could not be set up.", "Run: waddle doctor"),
        (caps.SANDBOX_UNCHECKED, "The sandbox has not been checked yet.", "Run: waddle doctor"),
        ("something-new", "The sandbox could not be set up.", "Run: waddle doctor"),
    ],
)
def test_sandbox_off_is_one_line_and_hides_the_tools_inside_it(
    code: str, why: str, fix: str
) -> None:
    cap = dataclasses.replace(ALL_GOOD, sandbox=False, sandbox_reason=code, man=False, tldr=False)
    (only,) = degradations(cap)
    assert only == Degradation("Version checks, man pages and tldr", why, fix)


def test_missing_man_and_tldr_are_separate_lines() -> None:
    assert whats(dataclasses.replace(ALL_GOOD, man=False)) == ["Man pages"]
    assert whats(dataclasses.replace(ALL_GOOD, tldr=False)) == ["tldr pages"]
    assert whats(dataclasses.replace(ALL_GOOD, man=False, tldr=False)) == [
        "Man pages",
        "tldr pages",
    ]


def test_skipped_pack_files_alone_are_not_a_degradation_but_no_packs_is() -> None:
    assert degradations(dataclasses.replace(ALL_GOOD, pack_errors=3)) == []
    assert whats(dataclasses.replace(ALL_GOOD, packs=0)) == ["App-specific knowledge"]


def test_lines_are_short_plain_text() -> None:
    worst = Capabilities(brain="up", sandbox=True)
    for line in degradations(worst) + degradations(Capabilities()):
        for part in line:
            assert 0 < len(part) <= 90
            assert "\n" not in part and "<" not in part
