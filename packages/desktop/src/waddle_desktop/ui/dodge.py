"""When the penguin steps aside (ARCHITECTURE §4.2 "Dodging"). Pure state machine; no Qt.

A Wayland client only sees the pointer over its own surface, so the trigger is the pointer
resting on the penguin body. Rules:

* only when no bubble/card is open and no drag-and-drop is entering;
* only after a dwell with no press (a press means a click or the start of a drag);
* he comes back on a timer, not on pointer-leave, so he cannot oscillate under the cursor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class DodgeState(Enum):
    NORMAL = auto()
    DWELLING = auto()
    DODGED = auto()


@dataclass
class DodgeMachine:
    dwell_ms: int = 350
    return_ms: int = 4000
    enabled: bool = True

    state: DodgeState = DodgeState.NORMAL
    _deadline: int | None = None  # absolute ms at which the pending transition fires
    _blocked: bool = False  # bubble open or a drag-and-drop is over us

    @property
    def dodged(self) -> bool:
        return self.state is DodgeState.DODGED

    @property
    def deadline(self) -> int | None:
        """When ``tick`` next needs to be called, or None if nothing is pending."""
        return self._deadline

    def set_blocked(self, blocked: bool, now: int) -> None:
        self._blocked = blocked
        if blocked:
            self._reset()

    def hover_enter(self, now: int) -> None:
        if self.enabled and not self._blocked and self.state is DodgeState.NORMAL:
            self.state = DodgeState.DWELLING
            self._deadline = now + self.dwell_ms

    def hover_leave(self, now: int) -> None:
        if self.state is DodgeState.DWELLING:
            self._reset()

    def press(self, now: int) -> None:
        """A press during the dwell is an interaction, not a reason to move away."""
        if self.state is DodgeState.DWELLING:
            self._reset()

    def tick(self, now: int) -> None:
        if self._deadline is None or now < self._deadline:
            return
        if self.state is DodgeState.DWELLING:
            self.state = DodgeState.DODGED
            self._deadline = now + self.return_ms
        elif self.state is DodgeState.DODGED:
            self._reset()

    def _reset(self) -> None:
        self.state = DodgeState.NORMAL
        self._deadline = None
