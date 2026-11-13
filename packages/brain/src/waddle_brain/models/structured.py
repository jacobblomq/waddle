"""Structured model calls: schema-constrained reply -> validated pydantic object.

The model's output is untrusted. It is size-capped, validated, and on failure the model is
asked again with a repair note written by this module. The note names only error kinds and
field names taken from *our* schema: nothing the model wrote is quoted back as text that
could read like an instruction, and the bad reply itself is not re-sent.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, ValidationError

from waddle_brain import log
from waddle_brain.contracts import ChatMessage, ModelProvider, ProviderError
from waddle_core.link import Role

MAX_STRUCTURED_CHARS: Final = 64_000
_MAX_PROBLEMS: Final = 5


async def structured_call[T: BaseModel](
    provider: ModelProvider,
    role: Role,
    messages: Sequence[ChatMessage],
    schema: type[T],
    *,
    retries: int = 2,
) -> T:
    """Ask for a reply matching ``schema``; retry up to ``retries`` times if it does not.

    Raises ``ProviderError("bad_model_output")`` when every attempt was invalid or too
    long. Other ``ProviderError``s (unreachable, missing, timeout) pass through at once.
    """
    attempt_messages = list(messages)
    for attempt in range(retries + 1):
        text = await _collect(provider, role, attempt_messages, schema)
        if text is None:
            problems = "the reply was too long"
        else:
            try:
                return schema.model_validate_json(text)
            except ValidationError as exc:
                problems = _summary(exc, schema)
        log.event("structured_retry", role=role, attempt=attempt)
        attempt_messages = [*messages, ChatMessage("user", _repair_note(problems))]
    raise ProviderError("bad_model_output")


async def _collect(
    provider: ModelProvider, role: Role, messages: Sequence[ChatMessage], schema: type[BaseModel]
) -> str | None:
    """The whole reply, or None if it passed the size cap (the stream is then closed)."""
    stream = provider.chat(role, messages, schema=schema)
    parts: list[str] = []
    size = 0
    try:
        async for piece in stream:
            size += len(piece)
            if size > MAX_STRUCTURED_CHARS:
                return None
            parts.append(piece)
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()  # stops the server generating when we leave early
    return "".join(parts)


def _repair_note(problems: str) -> str:
    return (
        "Your last reply did not fit the required JSON format. "
        f"Problems: {problems}. "
        "Reply again with only one JSON object in that format."
    )


def _summary(exc: ValidationError, schema: type[BaseModel]) -> str:
    """Error kinds and where they are, using only pydantic's fixed error-type names and
    field names that exist in our schema (a key invented by the model is not repeated)."""
    known = _field_names(schema.model_json_schema())
    items: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        where = ".".join(
            str(part) for part in error["loc"] if isinstance(part, int) or part in known
        )
        items.append(f"{error['type']} at {where}" if where else error["type"])
    shown = items[:_MAX_PROBLEMS]
    if len(items) > len(shown):
        shown.append(f"and {len(items) - len(shown)} more")
    return "; ".join(shown)


def _field_names(node: object) -> set[str]:
    """Every property name anywhere in a JSON schema."""
    names: set[str] = set()
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.update(str(key) for key in properties)
        for value in node.values():
            names |= _field_names(value)
    elif isinstance(node, list):
        for value in node:
            names |= _field_names(value)
    return names
