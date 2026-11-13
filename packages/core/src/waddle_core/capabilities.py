"""What Waddle can do right now, and what is off (ARCHITECTURE §8).

One frozen ``Capabilities`` value is worked out by the desktop from events. The penguin, the
tray, the answers and ``waddle doctor`` all read it, so the UI cannot claim an ability the
system does not have. ``degradations`` turns it into plain lines: what is off, why, and what
you can do about it.

Pure: no I/O, no clock. The codes in ``brain_reason``, ``capture_reason`` and
``sandbox_reason`` are short first-party identifiers, never text from the machine; an unknown
code gets a general line instead of being shown.

Rows that exist as of Phase 3: the brain, capture, a model that reads images, the sandbox and
the tools inside it, the app packs. Internet, VRAM, the microphone, accessibility and remote
providers arrive with their phases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, NamedTuple

BrainStatus = Literal["up", "connecting", "down"]

# capture_reason codes
CAPTURE_HELPER_MISSING: Final = "helper-missing"
CAPTURE_HELPER_FAILED: Final = "helper-failed"
CAPTURE_NO_KWIN: Final = "no-kwin"
CAPTURE_LOCK_UNKNOWN: Final = "lock-unknown"
CAPTURE_CONFIG_UNREADABLE: Final = "config-unreadable"

# sandbox_reason codes
SANDBOX_NO_BWRAP: Final = "no-bwrap"
SANDBOX_NO_PRLIMIT: Final = "no-prlimit"
SANDBOX_SETUP_FAILED: Final = "setup-failed"
SANDBOX_UNCHECKED: Final = "unchecked"


@dataclass(frozen=True)
class Capabilities:
    """A snapshot. The defaults describe a Waddle that has not checked anything yet."""

    brain: BrainStatus = "down"
    brain_reason: str = ""  # a ``LinkState.reason`` code, set when the brain is down
    vision: bool = False  # the brain has a model that can read a screenshot
    capture: bool = False  # the capture helper is there and can be used
    capture_reason: str = ""  # one of the CAPTURE_* codes, set when ``capture`` is False
    sandbox: bool = False  # observation commands can run (bwrap + prlimit)
    sandbox_reason: str = ""  # one of the SANDBOX_* codes, set when ``sandbox`` is False
    packs: int = 0  # app packs loaded
    pack_errors: int = 0  # pack files that were skipped (listed by ``waddle doctor``)
    man: bool = False  # ``man`` is installed
    tldr: bool = False  # a ``tldr`` client is installed

    @property
    def can_answer(self) -> bool:
        return self.brain == "up"

    @property
    def can_see_screen(self) -> bool:
        """A question can carry a screenshot that a model will read."""
        return self.brain == "up" and self.vision and self.capture


class Degradation(NamedTuple):
    """One thing that is off: what, why, and what you can do."""

    what: str
    why: str
    fix: str


_DOCTOR: Final = "Run: waddle doctor"
_BRAIN_UP: Final = "Start it with: waddle brain up"
_NOT_RUNNING: Final = ("Waddle's brain is not running.", _BRAIN_UP)
_MISMATCH: Final = (
    "The brain answered in a way Waddle does not understand.",
    "Update both, then: waddle brain down, waddle brain up",
)
_BRAIN: Final[dict[str, tuple[str, str]]] = {
    # "stopped" is the link before Waddle started it and after he stopped it: nothing is
    # known about the brain then, so nothing is claimed about it.
    "stopped": ("Waddle is not connected to his brain yet.", "Wait a moment."),
    "no_socket": _NOT_RUNNING,
    "connect_failed": _NOT_RUNNING,
    "lost": ("Waddle lost contact with his brain. He keeps trying.", "waddle brain status"),
    "handshake_timeout": ("The brain did not answer in time. Waddle keeps trying.", _DOCTOR),
    "unsafe_socket": (
        "The brain's socket does not belong to you, so Waddle will not use it.",
        _DOCTOR,
    ),
    "no_token": ("Waddle's key for the brain is missing or cannot be trusted.", _DOCTOR),
    "bad_token": (
        "The brain did not accept Waddle's key.",
        "Restart it: waddle brain down, then waddle brain up",
    ),
    "bad_version": (
        "The brain and the desktop app are different versions.",
        "Update both, then: waddle brain down, waddle brain up",
    ),
    "busy": ("Another Waddle is already connected to the brain.", "Close the other one."),
    "bad_handshake": _MISMATCH,
    "bad_hello": _MISMATCH,
    "protocol": _MISMATCH,
}
_BRAIN_OTHER: Final = ("Waddle cannot talk to his brain right now. He keeps trying.", _DOCTOR)

_CAPTURE: Final[dict[str, tuple[str, str]]] = {
    CAPTURE_HELPER_MISSING: (
        "The capture helper is not installed.",
        "Run ./install.sh again (it needs cargo to build the helper).",
    ),
    CAPTURE_HELPER_FAILED: ("The capture helper did not start.", _DOCTOR),
    CAPTURE_NO_KWIN: ("KWin did not answer the capture helper.", _DOCTOR),
    CAPTURE_LOCK_UNKNOWN: (
        "Waddle cannot tell whether the screen is locked, so he does not look.",
        _DOCTOR,
    ),
    CAPTURE_CONFIG_UNREADABLE: (
        "Your config file cannot be read, so the block list is unknown.",
        "Fix config.toml (waddle config shows where it is).",
    ),
}
_CAPTURE_OTHER: Final = ("Waddle cannot take a look at windows right now.", _DOCTOR)

_SANDBOX: Final[dict[str, tuple[str, str]]] = {
    SANDBOX_NO_BWRAP: ("bwrap is not installed.", "Install bubblewrap, then run: waddle doctor"),
    SANDBOX_NO_PRLIMIT: (
        "prlimit is not installed.",
        "Install util-linux, then run: waddle doctor",
    ),
    SANDBOX_SETUP_FAILED: ("The sandbox could not be set up.", _DOCTOR),
    SANDBOX_UNCHECKED: ("The sandbox has not been checked yet.", _DOCTOR),
}


def degradations(cap: Capabilities) -> list[Degradation]:
    """What is off, most important first. Empty when everything Phase 3 offers works.

    A cause is listed once: with the brain down nothing is said about its models, and with
    the sandbox off nothing is said about the tools that would run inside it.
    """
    out: list[Degradation] = []
    if cap.brain == "connecting":
        out.append(Degradation("Answers", "Waddle is still reaching his brain.", "Wait a moment."))
    elif cap.brain != "up":
        why, fix = _BRAIN.get(cap.brain_reason, _BRAIN_OTHER)
        out.append(Degradation("Answers", why, fix))
    if not cap.capture:
        why, fix = _CAPTURE.get(cap.capture_reason, _CAPTURE_OTHER)
        out.append(Degradation("Seeing your screen", why, fix))
    if cap.brain == "up" and not cap.vision:
        out.append(
            Degradation(
                "Reading screenshots",
                "No model that can read images is ready.",
                "Check the models: waddle models status",
            )
        )
    if not cap.sandbox:
        why, fix = _SANDBOX.get(cap.sandbox_reason, _SANDBOX[SANDBOX_SETUP_FAILED])
        out.append(Degradation("Version checks, man pages and tldr", why, fix))
    else:
        if not cap.man:
            out.append(Degradation("Man pages", "man is not installed.", "Install man-db."))
        if not cap.tldr:
            out.append(
                Degradation(
                    "tldr pages",
                    "No tldr client is installed.",
                    "Install one, for example tealdeer.",
                )
            )
    if cap.packs == 0:
        out.append(Degradation("App-specific knowledge", "No app packs could be loaded.", _DOCTOR))
    return out
