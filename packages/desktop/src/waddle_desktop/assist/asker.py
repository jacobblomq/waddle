"""One question from you to the brain, with what Waddle can see right now.

``AskController`` lives on the I/O loop. For each question it takes a single explicit look at
the focused window (you asked, so this is the on-demand watching level; nothing here watches
continuously, D-049), gathers the app and window context the privacy gate allows, and sends
the lot over the link. The answer comes back through plain callbacks.

Rules it keeps:

- One open question at a time: a new one ends the one before it, silently.
- Blind means blind: for a locked screen, a pause or a blocked window the question goes out
  with no picture, no window title and no app context, and says why Waddle cannot see.
- The picture is the helper's already-encoded bytes, passed from memory to the socket. It is
  never decoded, re-encoded, stored or written anywhere here.
- Model output is cleaned before it reaches a callback; the UI must show it as plain text.
- Logs carry codes and counts only: never the question, the answer, a title or the text of
  an exception (a validation error quotes the input it rejected).

Every callback is called on the I/O loop's thread; the UI layer forwards them as queued
signals.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from waddle_core.link import (
    MAX_DELTA_CHARS,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_ASK,
    MAX_QUESTION_CHARS,
    AnswerDone,
    AppContext,
    LinkError,
    Role,
    WindowContext,
)
from waddle_core.untrusted import clean
from waddle_core.watching import Trigger
from waddle_desktop import log
from waddle_desktop.assist import sight
from waddle_desktop.assist.identity_service import Current
from waddle_desktop.link.client import BRAIN_LOST, CANCELLED, BrainDownError, Image, LinkState
from waddle_desktop.sensors.capture_client import Frame
from waddle_desktop.sensors.senses import EyesState, Glimpse, Look

# on_error codes this controller adds to the brain's (``waddle_core.link.ErrorCode``) and the
# link's own ``brain_lost`` and ``cancelled``.
BRAIN_DOWN: Final = "brain_down"  # the question was not sent: there is no link right now
BAD_REQUEST: Final = "bad_request"  # nothing to ask, or it could not go on the wire
TOO_LARGE: Final = "too_large"  # the question, or the answer, is longer than allowed
MAX_ANSWER_CHARS: Final = 24_000  # the brain has its own, lower cap; this is the backstop
# The role that answers a question (waddle_brain/qa: smart_model, else fast_vlm).
ANSWER_ROLE: Final[Role] = "smart_model"
FALLBACK_ROLE: Final[Role] = "fast_vlm"

__all__ = [
    "ANSWER_ROLE",
    "BAD_REQUEST",
    "BRAIN_DOWN",
    "BRAIN_LOST",
    "CANCELLED",
    "MAX_ANSWER_CHARS",
    "TOO_LARGE",
    "AskController",
    "AskLink",
    "Sent",
    "frame_images",
]


class AskLink(Protocol):
    """The part of ``BrainLink`` a question needs."""

    @property
    def state(self) -> LinkState: ...
    def ask(
        self,
        text: str,
        *,
        app: AppContext | None,
        window: WindowContext | None,
        images: Sequence[Image] = (),
        can_see: bool,
        blind_reason: str | None,
        on_delta: Callable[[str], None],
        on_done: Callable[[AnswerDone], None],
        on_error: Callable[[str], None],
    ) -> str: ...
    def cancel(self, request_id: str) -> None: ...
    def prewarm(self, role: Role) -> None: ...


class Looker(Protocol):
    """The part of ``Senses`` a question needs."""

    @property
    def state(self) -> EyesState: ...
    async def look_once(self, trigger: Trigger = Trigger.USER_ASK) -> Glimpse: ...


@dataclass(frozen=True)
class Sent:
    """What went out with a question, for the "Waddle's eyes" panel. No text, no pixels."""

    can_see: bool
    blind_reason: str | None
    images: int
    image_bytes: int
    app: AppContext | None
    has_window: bool


def frame_images(frame: Frame) -> list[Image]:
    """The link's images for one captured frame: the overview, then the close-up.

    The capture helper already encodes both (a downscaled JPEG and a native-resolution PNG
    crop), which are the two types the link accepts, so nothing is decoded or re-encoded on
    the host. An image over the link's size limit is left out rather than cut.
    """
    images: list[Image] = []
    if 0 < len(frame.overview) <= MAX_IMAGE_BYTES:
        images.append(("overview", "image/jpeg", frame.overview))
    if 0 < len(frame.crop) <= MAX_IMAGE_BYTES:
        images.append(("crop", "image/png", frame.crop))
    return images[:MAX_IMAGES_PER_ASK]


class AskController:
    def __init__(
        self,
        *,
        link: AskLink,
        senses: Looker,
        identity: Callable[[], Current],
        on_delta: Callable[[str], None],
        on_done: Callable[[tuple[str, ...], str | None], None],
        on_error: Callable[[str], None],
        on_state: Callable[[LinkState], None] | None = None,
        on_sent: Callable[[Sent], None] | None = None,
    ):
        """``identity`` is ``IdentityService.current``.

        ``on_delta(text)``: the next piece of the answer, cleaned; newlines are kept.
        ``on_done(sources, role)``: the answer is complete. ``on_error(code)``: it is not.
        Each question ends with exactly one of the two, unless a newer question replaced it.
        ``on_state(LinkState)``: the brain went up or down, or a model is waking.
        ``on_sent(Sent)``: the question went out, with or without a picture.
        """
        self._link = link
        self._senses = senses
        self._identity = identity
        self._on_delta = on_delta
        self._on_done = on_done
        self._on_error = on_error
        self._on_state = on_state
        self._on_sent = on_sent
        self._turn = 0  # which question is the open one; callbacks of older ones are dropped
        self._open = False
        self._request_id: str | None = None
        self._chars = 0

    @property
    def busy(self) -> bool:
        """A question is open: looking, sent, or being answered."""
        return self._open

    # ---- from the UI (call on the loop) --------------------------------------------------

    async def ask(self, text: str) -> None:
        """Ask ``text``. Replaces any open question. Outcomes arrive through the callbacks."""
        turn = self._begin()
        text = text.strip()
        if not text:
            return self._fail(turn, BAD_REQUEST)
        if len(text) > MAX_QUESTION_CHARS:
            return self._fail(turn, TOO_LARGE)
        if self._link.state.status != "up":
            return self._fail(turn, BRAIN_DOWN)  # and no look: nobody would see the picture
        try:
            glimpse = await self._senses.look_once(Trigger.USER_ASK)
        except Exception as exc:  # CancelledError is not caught: it ends the question
            log.warning("ask_look_failed", kind=type(exc).__name__)
            glimpse = Glimpse(Look("error", "internal"))
        if turn != self._turn or not self._open:
            return None  # cancelled or replaced while looking; the frame is dropped here
        self._send(turn, text, glimpse)
        return None

    def cancel(self) -> None:
        """Stop the open question, if any. It ends with ``on_error("cancelled")``."""
        if not self._open:
            return
        turn = self._turn
        request_id = self._request_id
        if request_id is not None:
            self._link.cancel(request_id)  # ends it through the link's own on_error
        self._fail(turn, CANCELLED)  # nothing was sent yet (still looking), or as a backstop

    def prewarm(self) -> None:
        """The Ask window opened: have the answering model loaded. Nothing happens if down."""
        state = self._link.state
        if state.status != "up":
            return
        ready = {model.role for model in state.models if model.available}
        role = FALLBACK_ROLE if ANSWER_ROLE not in ready and FALLBACK_ROLE in ready else ANSWER_ROLE
        self._link.prewarm(role)
        log.event("ask_prewarm", role=role)

    def link_state(self, state: LinkState) -> None:
        """Give this to ``BrainLink`` as its ``on_state``."""
        if self._on_state is not None:
            self._on_state(state)

    # ---- one question --------------------------------------------------------------------

    def _begin(self) -> int:
        previous = self._request_id
        self._turn += 1
        self._open = True
        self._request_id = None
        self._chars = 0
        if previous is not None:
            self._link.cancel(previous)  # its callbacks belong to an older turn: dropped
        return self._turn

    def _send(self, turn: int, text: str, glimpse: Glimpse) -> None:
        state = self._senses.state
        window_now = state.window
        images: list[Image] = []
        frame = glimpse.frame
        if (
            frame is not None
            and state.blind is None
            and window_now is not None
            and window_now.uuid == glimpse.window_uuid
        ):
            images = frame_images(frame)
        else:
            frame = None
        reason = None if images else sight.no_frame_reason(glimpse.look, state)
        # A window the gate (here or in the helper) refuses is not described at all.
        allowed = state.blind is None and reason not in sight.HARD_REASONS
        window = sight.window_context(state, frame) if allowed else None
        app = self._identity().app if allowed else None
        try:
            request_id = self._link.ask(
                text,
                app=app,
                window=window,
                images=images,
                can_see=bool(images),
                blind_reason=reason,
                on_delta=lambda piece: self._delta(turn, piece),
                on_done=lambda done: self._done(turn, done),
                on_error=lambda code: self._fail(turn, code),
            )
        except BrainDownError:
            return self._fail(turn, BRAIN_DOWN)
        except LinkError as exc:
            # Checked before ValueError: it is one. Its code is a fixed word, safe to log.
            log.warning("ask_rejected", code=exc.code)
            return self._fail(turn, TOO_LARGE if exc.code == "too_large" else BAD_REQUEST)
        except ValueError as exc:
            # A pydantic ValidationError quotes its input: log only what kind it was.
            log.warning("ask_rejected", kind=type(exc).__name__)
            return self._fail(turn, BAD_REQUEST)
        self._request_id = request_id
        size = sum(len(data) for _kind, _mime, data in images)
        log.event(
            "ask_sent",
            can_see=bool(images),
            images=len(images),
            reason=reason or "",
            has_app=app is not None,
            has_window=window is not None,
        )
        if self._on_sent is not None:
            self._on_sent(Sent(bool(images), reason, len(images), size, app, window is not None))
        return None

    def _delta(self, turn: int, piece: str) -> None:
        if turn != self._turn or not self._open:
            return
        text = clean(piece, MAX_DELTA_CHARS)
        if not text:
            return
        self._chars += len(text)
        if self._chars > MAX_ANSWER_CHARS:
            # The brain is meant to stop long before this; do not let it fill the bubble.
            request_id = self._request_id
            log.warning("ask_answer_too_long")
            self._fail(turn, TOO_LARGE)
            if request_id is not None:
                self._link.cancel(request_id)
            return
        self._on_delta(text)

    def _done(self, turn: int, done: AnswerDone) -> None:
        if turn != self._turn or not self._open:
            return
        self._close()
        log.event("ask_done", sources=len(done.sources), chars=self._chars)
        self._on_done(tuple(done.sources), done.role)

    def _fail(self, turn: int, code: str) -> None:
        if turn != self._turn or not self._open:
            return
        self._close()
        log.event("ask_failed", code=code)
        self._on_error(code)

    def _close(self) -> None:
        self._open = False
        self._request_id = None
