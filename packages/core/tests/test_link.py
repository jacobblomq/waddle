"""The link protocol: what goes on the wire, and everything that must not get through."""

import contextlib
import json
import random
import struct
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from waddle_core import link
from waddle_core.link import LinkError

TOKEN = "0123456789abcdef" * 4
MODELS = (link.ModelStatus(role="fast_vlm", model="some-model", available=True, loaded=False),)

HOST_MESSAGES: list[BaseModel] = [
    link.Hello(id="h1", token=TOKEN, client_version="0.3.0"),
    link.Ask(id="a1", text="What does this button do?"),
    link.Ask(
        id="a2",
        text="x" * link.MAX_QUESTION_CHARS,
        app=link.AppContext(
            app_id="org.kde.krita",
            name="Krita",
            version="5.3.1",
            version_source="flatpak",
            packaging="flatpak",
            channel="stable",
            edition="plus",
            confidence=0.9,
        ),
        window=link.WindowContext(
            resource_class="krita",
            title="t" * link.MAX_TITLE_CHARS,
            width=1920,
            height=1080,
            scale=1.5,
            fullscreen=True,
            xwayland=False,
        ),
        images=(
            link.ImageRef(image_id="img-1", kind="overview"),
            link.ImageRef(image_id="img-2", kind="crop"),
        ),
        can_see=True,
    ),
    link.Ask(id="a3", text="hi", can_see=False, blind_reason="capture_denied"),
    link.Cancel(id="a1"),
    link.Prewarm(id="p1"),
    link.Prewarm(id="p2", role="fast_vlm"),
    link.ToolResult(id="a1", call_id="c1", ok=True, data={"cpu": [1, 2.5, "x", None, {"k": True}]}),
    link.ToolResult(id="a1", call_id="c2", ok=False, error="not_found"),
    link.Ping(id="k1"),
]
BRAIN_MESSAGES: list[BaseModel] = [
    link.Welcome(id="h1", server_version="0.3.0", models=MODELS),
    link.Welcome(id="h1", server_version="0.3.0"),
    *(link.Refused(id="h1", reason=r) for r in ("bad_token", "bad_version", "bad_hello", "busy")),
    link.AnswerDelta(id="a1", text="d" * link.MAX_DELTA_CHARS),
    link.AnswerDone(id="a1"),
    link.AnswerDone(
        id="a1",
        sources=("screen", "system", "app", "model"),
        role="smart_model",
        timings=link.Timings(first_delta_ms=120, total_ms=900, model_load_ms=0),
    ),
    link.AnswerError(id="a1", code="cancelled"),
    link.AnswerError(id="a1", code="model_timeout", waking=True),
    link.ToolCall(id="a1", call_id="c1", tool="man", args={"name": "ls", "section": "1"}),
    link.ToolCall(id="a1", call_id="c2", tool="get_app_state"),
    link.State(id="s1", models=MODELS, waking="embedder"),
    link.State(id="s2"),
    link.Pong(id="k1"),
]
HOST_TYPES = {"hello", "ask", "cancel", "prewarm", "tool.result", "ping"}
BRAIN_TYPES = {
    "welcome",
    "refused",
    "answer.delta",
    "answer.done",
    "answer.error",
    "tool.call",
    "state",
    "pong",
}


def wire(**fields: Any) -> str:
    return json.dumps({"v": 1, "id": "m1", **fields})


def ask(**fields: Any) -> str:
    return wire(type="ask", text="hi", **fields)


def rejected(decoder: Any, text: str | bytes, code: str = "bad_message") -> None:
    with pytest.raises(LinkError) as caught:
        decoder(text)
    assert caught.value.code == code
    assert str(caught.value) == code  # nothing of the message leaks into the error text


# ---- round trips and dispatch ------------------------------------------------------------


@pytest.mark.parametrize("message", HOST_MESSAGES, ids=lambda m: type(m).__name__)
def test_host_messages_round_trip(message: Any) -> None:
    text = link.encode(message)
    decoded = link.decode_host(text)
    assert type(decoded) is type(message)
    assert decoded == message
    assert link.decode_host(text.encode()) == message  # bytes work the same as str


@pytest.mark.parametrize("message", BRAIN_MESSAGES, ids=lambda m: type(m).__name__)
def test_brain_messages_round_trip(message: Any) -> None:
    decoded = link.decode_brain(link.encode(message))
    assert type(decoded) is type(message)
    assert decoded == message


def test_every_message_type_is_covered() -> None:
    assert {m.type for m in HOST_MESSAGES} == HOST_TYPES  # type: ignore[attr-defined]
    assert {m.type for m in BRAIN_MESSAGES} == BRAIN_TYPES  # type: ignore[attr-defined]
    assert not HOST_TYPES & BRAIN_TYPES


@pytest.mark.parametrize("message", HOST_MESSAGES, ids=lambda m: type(m).__name__)
def test_a_host_message_never_decodes_as_a_brain_message(message: Any) -> None:
    rejected(link.decode_brain, link.encode(message))


@pytest.mark.parametrize("message", BRAIN_MESSAGES, ids=lambda m: type(m).__name__)
def test_a_brain_message_never_decodes_as_a_host_message(message: Any) -> None:
    rejected(link.decode_host, link.encode(message))


def test_encoded_text_is_plain_json_without_nulls() -> None:
    sent = json.loads(link.encode(link.Ask(id="a1", text="hi")))
    assert sent == {"v": 1, "id": "a1", "type": "ask", "text": "hi", "images": [], "can_see": False}


def test_messages_are_frozen() -> None:
    message = link.Ask(id="a1", text="hi")
    with pytest.raises(ValidationError):
        message.text = "other"  # type: ignore[misc]


def test_constants_agree_with_each_other() -> None:
    assert link.PROTOCOL_VERSION == 1
    assert len(TOKEN) == link.TOKEN_BYTES * 2
    assert link.MAX_IMAGES_PER_ASK <= link.MAX_PENDING_IMAGES
    # The biggest legal question, in the widest characters, still fits a control message.
    assert link.MAX_QUESTION_CHARS * 4 < link.MAX_CONTROL_BYTES


# ---- envelope: version, id, type, unknown fields -----------------------------------------


@pytest.mark.parametrize("version", [0, 2, -1, "1", 1.0, 1.5, True, None, [1], {"v": 1}])
def test_wrong_version_is_rejected(version: Any) -> None:
    rejected(link.decode_host, json.dumps({"v": version, "id": "m1", "type": "ping"}))
    rejected(link.decode_brain, json.dumps({"v": version, "id": "m1", "type": "pong"}))


def test_version_may_be_left_out_and_means_one() -> None:
    assert link.decode_host('{"id":"m1","type":"ping"}') == link.Ping(id="m1")


@pytest.mark.parametrize("version", [True, 1.0, 2, "1"])
def test_wrong_version_is_rejected_when_built_in_code(version: Any) -> None:
    with pytest.raises(ValidationError):
        link.Ping(id="m1", v=version)


GOOD_IDS = ["a", "A-b_9", "0", "-", "_", "x" * 64]
BAD_IDS = [
    "",
    "x" * 65,
    "a b",
    "a\n",
    "\na",
    "a/b",
    "a.b",
    "../x",
    "a\x00",
    "a;b",
    "$(id)",
    "caf\u00e9",
    "\u0430bc",  # Cyrillic a
    "\uff41",  # full-width a
    "\u0661",  # Arabic-Indic digit
    1,
    1.5,
    None,
    True,
    ["a"],
    {"a": 1},
]


@pytest.mark.parametrize("good", GOOD_IDS)
def test_good_ids_are_accepted(good: str) -> None:
    assert link.decode_host(json.dumps({"id": good, "type": "ping"})).id == good


@pytest.mark.parametrize("bad", BAD_IDS, ids=repr)
def test_bad_ids_are_rejected_everywhere(bad: Any) -> None:
    rejected(link.decode_host, json.dumps({"v": 1, "id": bad, "type": "ping"}))
    rejected(link.decode_brain, json.dumps({"v": 1, "id": bad, "type": "pong"}))
    rejected(link.decode_host, wire(type="tool.result", call_id=bad, ok=True))
    rejected(link.decode_brain, wire(type="tool.call", call_id=bad, tool="man"))
    rejected(link.decode_host, ask(images=[{"image_id": bad, "kind": "crop"}]))


def test_missing_id_is_rejected() -> None:
    rejected(link.decode_host, '{"v":1,"type":"ping"}')


@pytest.mark.parametrize(
    "kind", ["", "nope", "PING", "ping ", "answer", "tool", "ask\n", None, 1, ["ping"], {}]
)
def test_unknown_type_is_rejected(kind: Any) -> None:
    rejected(link.decode_host, json.dumps({"v": 1, "id": "m1", "type": kind}))
    rejected(link.decode_brain, json.dumps({"v": 1, "id": "m1", "type": kind}))


def test_missing_type_is_rejected() -> None:
    rejected(link.decode_host, '{"v":1,"id":"m1"}')
    rejected(link.decode_brain, '{"v":1,"id":"m1"}')


@pytest.mark.parametrize("message", HOST_MESSAGES + BRAIN_MESSAGES, ids=lambda m: type(m).__name__)
def test_unknown_top_level_field_is_rejected(message: Any) -> None:
    decoder = link.decode_host if message.type in HOST_TYPES else link.decode_brain
    body = json.loads(link.encode(message))
    assert decoder(json.dumps(body)) == message
    rejected(decoder, json.dumps({**body, "extra": 1}))


@pytest.mark.parametrize(
    "fields",
    [
        {"app": {"nope": 1}},
        {"window": {"nope": 1}},
        {"images": [{"image_id": "i", "kind": "crop", "nope": 1}]},
    ],
)
def test_unknown_nested_field_is_rejected(fields: dict[str, Any]) -> None:
    rejected(link.decode_host, ask(**fields))


def test_unknown_nested_field_is_rejected_from_the_brain() -> None:
    status = {"role": "embedder", "model": "m", "available": True, "loaded": True}
    assert link.decode_brain(wire(type="state", models=[status]))
    rejected(link.decode_brain, wire(type="state", models=[{**status, "nope": 1}]))
    rejected(link.decode_brain, wire(type="answer.done", timings={"nope": 1}))


# ---- field rules -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "",
        TOKEN[:-1],
        TOKEN + "0",
        TOKEN.upper(),
        "g" + TOKEN[1:],
        " " + TOKEN[1:],
        TOKEN[:-1] + "\n",
        "\uff10" + TOKEN[1:],  # full-width zero
        None,
        12345,
    ],
    ids=repr,
)
def test_bad_token_is_rejected(token: Any) -> None:
    rejected(link.decode_host, wire(type="hello", token=token, client_version="1"))


def test_hello_needs_token_and_version() -> None:
    assert link.decode_host(wire(type="hello", token=TOKEN, client_version="1"))
    rejected(link.decode_host, wire(type="hello", client_version="1"))
    rejected(link.decode_host, wire(type="hello", token=TOKEN))
    rejected(link.decode_host, wire(type="hello", token=TOKEN, client_version="x" * 33))


def test_question_length_cap_is_exact() -> None:
    assert link.decode_host(wire(type="ask", text="q" * link.MAX_QUESTION_CHARS))
    rejected(link.decode_host, wire(type="ask", text="q" * (link.MAX_QUESTION_CHARS + 1)))
    rejected(link.decode_host, wire(type="ask", text=""))
    rejected(link.decode_host, wire(type="ask"))


def test_question_cap_counts_characters_not_bytes() -> None:
    text = "\U0001f427" * link.MAX_QUESTION_CHARS  # four bytes each in UTF-8
    message = link.decode_host(link.encode(link.Ask(id="a1", text=text)))
    assert isinstance(message, link.Ask) and message.text == text


def test_images_per_ask_cap_is_exact() -> None:
    refs = [{"image_id": f"i{n}", "kind": "crop"} for n in range(link.MAX_IMAGES_PER_ASK + 1)]
    assert link.decode_host(ask(images=refs[: link.MAX_IMAGES_PER_ASK]))
    rejected(link.decode_host, ask(images=refs))
    with pytest.raises(ValidationError):
        link.Ask(id="a1", text="hi", images=tuple(link.ImageRef(**r) for r in refs))


@pytest.mark.parametrize("kind", ["", "full", "CROP", None, 1])
def test_image_kind_is_a_closed_list(kind: Any) -> None:
    rejected(link.decode_host, ask(images=[{"image_id": "i", "kind": kind}]))


def test_delta_length_cap_is_exact() -> None:
    assert link.decode_brain(wire(type="answer.delta", text="d" * link.MAX_DELTA_CHARS))
    rejected(link.decode_brain, wire(type="answer.delta", text="d" * (link.MAX_DELTA_CHARS + 1)))
    rejected(link.decode_brain, wire(type="answer.delta", text=""))


def test_title_length_cap_is_exact() -> None:
    assert link.decode_host(ask(window={"title": "t" * link.MAX_TITLE_CHARS}))
    rejected(link.decode_host, ask(window={"title": "t" * (link.MAX_TITLE_CHARS + 1)}))


@pytest.mark.parametrize(
    "timings",
    [
        {"total_ms": -1},
        {"first_delta_ms": -1},
        {"model_load_ms": -1},
        {"total_ms": link.MAX_TIMING_MS + 1},
        {"first_delta_ms": link.MAX_TIMING_MS + 1},
        {"model_load_ms": 10**400},
    ],
)
def test_out_of_range_timings_are_rejected(timings: dict[str, int]) -> None:
    rejected(link.decode_brain, wire(type="answer.done", timings=timings))


def test_timings_at_the_cap_are_accepted() -> None:
    top = link.MAX_TIMING_MS
    done = link.decode_brain(wire(type="answer.done", timings={"total_ms": top}))
    assert isinstance(done, link.AnswerDone)
    assert done.timings.total_ms == top


@pytest.mark.parametrize(
    "fields",
    [
        {"app": {"app_id": "x" * 129}},
        {"app": {"name": "x" * 65}},
        {"app": {"version": "x" * 33}},
        {"app": {"confidence": 1.01}},
        {"app": {"confidence": -0.01}},
        {"window": {"width": -1}},
        {"window": {"height": 100_001}},
        {"window": {"scale": 0}},
        {"window": {"scale": 8.5}},
        {"blind_reason": "x" * 33},
    ],
)
def test_out_of_range_context_is_rejected(fields: dict[str, Any]) -> None:
    rejected(link.decode_host, ask(**fields))


@pytest.mark.parametrize(
    "fields",
    [
        {"can_see": 1},
        {"can_see": "true"},
        {"can_see": "yes"},
        {"text": 5},
        {"text": ["hi"]},
        {"window": {"width": "12"}},
        {"window": {"width": 1.5}},
        {"window": {"fullscreen": "on"}},
        {"window": {"title": 7}},
        {"app": {"confidence": "0.5"}},
        {"app": {"name": True}},
        {"images": "ab"},
        {"images": {"image_id": "i", "kind": "crop"}},
    ],
)
def test_decoding_does_not_coerce_types(fields: dict[str, Any]) -> None:
    rejected(
        link.decode_host, json.dumps({"v": 1, "id": "m1", "type": "ask", "text": "hi"} | fields)
    )


def test_decoding_does_not_coerce_brain_types() -> None:
    rejected(link.decode_brain, wire(type="answer.error", code="busy", waking="yes"))
    rejected(link.decode_brain, wire(type="answer.done", timings={"total_ms": "5"}))
    rejected(link.decode_brain, wire(type="answer.done", timings={"total_ms": -1}))
    rejected(link.decode_brain, wire(type="answer.delta", text=5))
    rejected(link.decode_host, wire(type="tool.result", call_id="c", ok="true"))
    rejected(link.decode_host, wire(type="tool.result", call_id="c", ok=1))


def test_whole_numbers_are_fine_where_a_fraction_is_expected() -> None:
    message = link.decode_host(ask(window={"scale": 2}, app={"confidence": 1}))
    assert isinstance(message, link.Ask) and message.window and message.app
    assert message.window.scale == 2.0 and message.app.confidence == 1.0


@pytest.mark.parametrize("role", ["", "main", "SMART_MODEL", None, 1])
def test_role_is_a_closed_list(role: Any) -> None:
    rejected(link.decode_host, wire(type="prewarm", role=role))
    status = {"role": role, "model": "m", "available": True, "loaded": True}
    rejected(link.decode_brain, wire(type="state", models=[status]))


def test_error_code_refusal_reason_and_sources_are_closed_lists() -> None:
    rejected(link.decode_brain, wire(type="answer.error", code="oops"))
    rejected(link.decode_brain, wire(type="answer.error"))
    rejected(link.decode_brain, wire(type="refused", reason="because"))
    rejected(link.decode_brain, wire(type="answer.done", sources=["web"]))


def test_model_list_and_names_are_capped() -> None:
    status = {"role": "embedder", "model": "m" * 128, "available": True, "loaded": False}
    assert link.decode_brain(wire(type="welcome", server_version="1", models=[status] * 8))
    rejected(link.decode_brain, wire(type="welcome", server_version="1", models=[status] * 9))
    rejected(link.decode_brain, wire(type="state", models=[status] * 9))
    rejected(link.decode_brain, wire(type="state", models=[{**status, "model": "m" * 129}]))


def test_tool_call_shape() -> None:
    call = link.decode_brain(wire(type="tool.call", call_id="c", tool="man", args={"name": "ls"}))
    assert isinstance(call, link.ToolCall) and call.args == {"name": "ls"}
    rejected(link.decode_brain, wire(type="tool.call", call_id="c", tool="t" * 33))
    rejected(link.decode_brain, wire(type="tool.call", call_id="c", tool="man", args=["ls"]))
    rejected(link.decode_brain, wire(type="tool.call", call_id="c", tool="man", args="ls"))
    rejected(link.decode_brain, wire(type="tool.call", tool="man"))


def test_tool_result_shape() -> None:
    rejected(link.decode_host, wire(type="tool.result", call_id="c"))
    rejected(link.decode_host, wire(type="tool.result", call_id="c", ok=True, data=[1]))
    rejected(link.decode_host, wire(type="tool.result", call_id="c", ok=False, error="e" * 65))


# ---- size caps ---------------------------------------------------------------------------


def padded_ping(total: int) -> bytes:
    body = b'{"v":1,"id":"m1","type":"ping"}'
    return body + b" " * (total - len(body))


def test_control_size_cap_is_exact_on_decode() -> None:
    assert link.decode_host(padded_ping(link.MAX_CONTROL_BYTES)) == link.Ping(id="m1")
    rejected(link.decode_host, padded_ping(link.MAX_CONTROL_BYTES + 1), "too_large")
    rejected(link.decode_brain, padded_ping(link.MAX_CONTROL_BYTES + 1), "too_large")


def test_control_size_cap_counts_bytes_not_characters() -> None:
    # Under the cap in characters, over it in UTF-8 bytes.
    text = wire(type="tool.result", call_id="c", ok=True, data={"x": "\u00e9" * 140_000})
    padded = json.loads(text)
    assert len(json.dumps(padded, ensure_ascii=False)) < link.MAX_CONTROL_BYTES
    rejected(link.decode_host, json.dumps(padded, ensure_ascii=False), "too_large")


def result_of_encoded_size(total: int) -> link.ToolResult:
    empty = len(link.encode(link.ToolResult(id="a1", call_id="c1", ok=True, data={"x": ""})))
    return link.ToolResult(id="a1", call_id="c1", ok=True, data={"x": "y" * (total - empty)})


def test_encode_refuses_oversized_messages() -> None:
    fits = link.encode(result_of_encoded_size(link.MAX_CONTROL_BYTES))
    assert len(fits) == link.MAX_CONTROL_BYTES
    assert link.decode_host(fits)
    with pytest.raises(LinkError) as caught:
        link.encode(result_of_encoded_size(link.MAX_CONTROL_BYTES + 1))
    assert caught.value.code == "too_large"


def test_encode_measures_bytes_not_characters() -> None:
    wide = link.ToolCall(id="a1", call_id="c1", tool="man", args={"x": "\U0001f427" * 70_000})
    with pytest.raises(LinkError) as caught:
        link.encode(wide)
    assert caught.value.code == "too_large"


def test_encode_refuses_text_that_is_not_valid_unicode() -> None:
    # Command output decoded with surrogateescape can carry lone surrogates.
    with pytest.raises(LinkError) as caught:
        link.encode(link.ToolResult(id="a1", call_id="c1", ok=True, data={"out": "\udcff"}))
    assert caught.value.code == "bad_message"


def test_encoded_numbers_are_always_strict_json() -> None:
    odd = {"a": float("nan"), "b": float("inf"), "c": -float("inf")}
    text = link.encode(link.ToolResult(id="a1", call_id="c1", ok=True, data=odd))

    def refuse(token: str) -> None:
        raise AssertionError(token)

    assert json.loads(text, parse_constant=refuse)["data"] == {"a": None, "b": None, "c": None}


# ---- hostile input -----------------------------------------------------------------------

GARBAGE: list[str | bytes] = [
    b"",
    "",
    b"\x00",
    b"\xff\xfe\xfd",
    b"\xef\xbb\xbf" + b'{"v":1,"id":"m1","type":"ping"}',  # BOM
    b'{"v":1,"id":"m1\xff","type":"ping"}',  # invalid UTF-8 inside a string
    b'{"v":1,"id":"m1","type":"ping"}\x00',
    b'{"v":1,"id":"\xed\xa0\x80","type":"ping"}',  # UTF-8 encoded surrogate
    '{"v":1,"id":"m1\ud800","type":"ping"}',  # lone surrogate in a str
    '{"v":1,"id":"\\ud800","type":"ping"}',  # escaped lone surrogate
    "null",
    "true",
    "1",
    '"ping"',
    "[]",
    "{}",
    '[{"v":1,"id":"m1","type":"ping"}]',
    '{"v":1,"id":"m1","type":"ping"}{"v":1,"id":"m1","type":"ping"}',
    '{"v":1,"id":"m1","type":"ping"',
    "{'v':1,'id':'m1','type':'ping'}",
    '{"v":1,"id":"m1","type":"ping",}',
    '{"v":NaN,"id":"m1","type":"ping"}',
    '{"v":Infinity,"id":"m1","type":"ping"}',
    '{"v":1e999,"id":"m1","type":"ping"}',
    '{"v":' + "9" * 5000 + ',"id":"m1","type":"ping"}',
    '{"v":1,"id":' + "9" * 5000 + ',"type":"ping"}',
    '{"v":1,"id":"m1","type":"ping","type":"nope"}',
    "[" * 100_000,
    "[" * 5000 + "]" * 5000,
    '{"a":' * 5000 + "1" + "}" * 5000,
    '{"v":1,"id":"m1","type":"ask","text":"hi","app":{"confidence":NaN}}',
    '{"v":1,"id":"m1","type":"ask","text":"hi","window":{"scale":NaN}}',
    '{"v":1,"id":"m1","type":"ask","text":"hi","window":{"scale":1e999}}',
    '{"v":1,"id":"m1","type":"ask","text":"hi","window":{"width":' + "9" * 400 + "}}",
    '{"v":1,"id":"m1","type":"answer.done","timings":{"total_ms":' + "9" * 6000 + "}}",
    '{"v":1,"id":"m1","type":"answer.done","timings":{"total_ms":NaN}}',
    '{"v":1,"id":"m1","type":"tool.result","call_id":"c","ok":true,"data":{"x":'
    + "[" * 5000
    + "]" * 5000
    + "}}",
    '{"v":1,"id":"m1","type":"tool.call","call_id":"c","tool":"man","args":'
    + '{"a":' * 5000
    + "1"
    + "}" * 5000
    + "}",
]


@pytest.mark.parametrize("junk", GARBAGE, ids=lambda j: repr(j[:24]))
def test_garbage_is_rejected_with_a_link_error(junk: str | bytes) -> None:
    for decoder in (link.decode_host, link.decode_brain):
        with pytest.raises(LinkError) as caught:
            decoder(junk)
        assert caught.value.code in {"bad_message", "too_large"}


def test_a_repeated_type_key_does_not_cross_the_direction() -> None:
    # Whichever duplicate a parser keeps, a brain message must not come out of decode_host.
    for text in (
        '{"v":1,"id":"m1","type":"pong","type":"ping"}',
        '{"v":1,"id":"m1","type":"ping","type":"pong"}',
    ):
        try:
            assert isinstance(link.decode_host(text), link.Ping)
        except LinkError as error:
            assert error.code == "bad_message"


def decodes_or_link_error(decoder: Any, data: str | bytes) -> None:
    with contextlib.suppress(LinkError):  # anything else propagates and fails the test
        decoder(data)


def mutate(rng: random.Random, seed: bytes) -> bytes:
    data = bytearray(seed)
    for _ in range(rng.randint(1, 6)):
        choice = rng.randrange(5)
        at = rng.randrange(len(data) + 1)
        if choice == 0 and data:
            data[at % len(data)] = rng.randrange(256)
        elif choice == 1 and data:
            del data[at % len(data)]
        elif choice == 2:
            data[at:at] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
        elif choice == 3:
            data[at:at] = rng.choice([b"NaN", b"1e999", b"[[[[", b'"\\ud800"', b"\x00", b"{", b"}"])
        else:
            del data[at:]
    return bytes(data)


def test_fuzz_decode_only_ever_raises_link_error() -> None:
    rng = random.Random(20261113)
    seeds = [link.encode(m).encode() for m in HOST_MESSAGES + BRAIN_MESSAGES if len(m.id) < 4]  # type: ignore[attr-defined]
    for _ in range(4000):
        junk = mutate(rng, rng.choice(seeds))
        decodes_or_link_error(link.decode_host, junk)
        decodes_or_link_error(link.decode_brain, junk)
        decodes_or_link_error(link.decode_host, junk.decode("utf-8", "surrogateescape"))
    for _ in range(500):
        noise = bytes(rng.randrange(256) for _ in range(rng.randint(0, 300)))
        decodes_or_link_error(link.decode_host, noise)
        decodes_or_link_error(link.decode_brain, noise)


def test_fuzz_whatever_decodes_can_be_sent_again_unchanged() -> None:
    rng = random.Random(7)
    seeds = [(link.decode_host, link.encode(m).encode()) for m in HOST_MESSAGES[:4]]  # type: ignore[arg-type]
    survivors = 0
    for _ in range(3000):
        decoder, seed = rng.choice(seeds)
        try:
            message = decoder(mutate(rng, seed))
        except LinkError:
            continue
        survivors += 1
        assert decoder(link.encode(message)) == message
    assert survivors > 20  # the mutations are mild enough that the loop tests something


# ---- binary image messages ---------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def frame(header: Any, data: bytes = PNG, size: int | None = None) -> bytes:
    raw = header if isinstance(header, bytes) else json.dumps(header).encode()
    return struct.pack(">I", len(raw) if size is None else size) + raw + data


def bad_image(message: bytes, code: str = "bad_image") -> None:
    with pytest.raises(LinkError) as caught:
        link.decode_image(message)
    assert caught.value.code == code


@pytest.mark.parametrize("mime", ["image/jpeg", "image/png"])
def test_image_round_trip(mime: str) -> None:
    message = link.encode_image("img-1", mime, PNG)
    assert isinstance(message, bytes)
    assert link.decode_image(message) == ("img-1", mime, PNG)


def test_image_round_trip_at_the_size_cap_and_longest_id() -> None:
    data = b"\xff" * link.MAX_IMAGE_BYTES
    image_id, mime, back = link.decode_image(link.encode_image("i" * 64, "image/jpeg", data))
    assert (image_id, mime) == ("i" * 64, "image/jpeg") and back == data


def test_decode_image_takes_any_bytes_like_message() -> None:
    message = link.encode_image("i", "image/png", PNG)
    for view in (bytearray(message), memoryview(message)):
        assert link.decode_image(view) == ("i", "image/png", PNG)  # type: ignore[arg-type]
    with pytest.raises(LinkError):
        link.decode_image(memoryview(b"\x00\x00\x00\x02{]x"))  # type: ignore[arg-type]


def test_image_one_byte_and_binary_safe() -> None:
    assert link.decode_image(link.encode_image("i", "image/png", b"\x00"))[2] == b"\x00"
    tricky = b'}{"image_id":"other","mime":"image/png"}'
    assert link.decode_image(link.encode_image("i", "image/png", tricky)) == (
        "i",
        "image/png",
        tricky,
    )


@pytest.mark.parametrize(
    "mime",
    [
        "",
        "image/gif",
        "image/svg+xml",
        "IMAGE/PNG",
        "image/png ",
        "image/png\n",
        "text/html",
        "png",
    ],
)
def test_encode_image_refuses_other_mime_types(mime: str) -> None:
    with pytest.raises(LinkError) as caught:
        link.encode_image("i", mime, PNG)
    assert caught.value.code == "bad_mime"
    bad_image(frame({"image_id": "i", "mime": mime}))


@pytest.mark.parametrize("mime", [None, 1, [], ["image/png"], {}, {"image/png": 1}, True], ids=repr)
def test_mime_that_is_not_text_is_refused(mime: Any) -> None:
    with pytest.raises(LinkError) as caught:
        link.encode_image("i", mime, PNG)
    assert caught.value.code == "bad_mime"
    bad_image(frame({"image_id": "i", "mime": mime}))


def test_encode_image_refuses_empty_and_oversized_data() -> None:
    for data in (b"", b"x" * (link.MAX_IMAGE_BYTES + 1)):
        with pytest.raises(LinkError) as caught:
            link.encode_image("i", "image/png", data)
        assert caught.value.code == "too_large"


@pytest.mark.parametrize("bad", BAD_IDS, ids=repr)
def test_image_id_must_match_the_id_pattern(bad: Any) -> None:
    with pytest.raises(LinkError) as caught:
        link.encode_image(bad, "image/png", PNG)
    assert caught.value.code == "bad_id"
    bad_image(frame({"image_id": bad, "mime": "image/png"}))


@pytest.mark.parametrize("message", [b"", b"\x00", b"\x00\x00\x00", b"\x00\x00\x00\x00"])
def test_truncated_image_header_is_rejected(message: bytes) -> None:
    bad_image(message)


def test_image_cut_anywhere_before_its_data_is_rejected() -> None:
    message = link.encode_image("img-1", "image/png", b"D")
    assert link.decode_image(message)[2] == b"D"
    for cut in range(len(message)):
        bad_image(message[:cut])


def test_image_with_no_data_is_rejected() -> None:
    bad_image(frame({"image_id": "i", "mime": "image/png"}, data=b""))


def test_image_header_length_lies_are_rejected() -> None:
    header = json.dumps({"image_id": "i", "mime": "image/png"}).encode()
    assert link.decode_image(frame(header)) == ("i", "image/png", PNG)
    bad_image(frame(header, size=0))
    bad_image(frame(header, size=len(header) - 1))  # the header's last byte lands in the data
    bad_image(frame(header, size=len(header) + 1))  # the header swallows a data byte
    bad_image(frame(header, size=len(header) + len(PNG)))  # the header claims everything
    bad_image(frame(header, size=len(header) + len(PNG) + 1))  # ... and more than there is
    bad_image(frame(header, size=0xFFFFFFFF))
    bad_image(frame(header, size=0x80000000))


def test_image_header_size_cap() -> None:
    def padded(size: int) -> bytes:
        header = json.dumps({"image_id": "i", "mime": "image/png"}).encode()
        return frame(header + b" " * (size - len(header)))

    assert link.decode_image(padded(512))[0] == "i"
    bad_image(padded(513))


@pytest.mark.parametrize(
    "header",
    [
        {"image_id": "i", "mime": "image/png", "extra": 1},
        {"image_id": "i", "mime": "image/png", "": ""},
        {"image_id": "i"},
        {"mime": "image/png"},
        {},
        [],
        ["image_id", "mime"],
        "image_id",
        "im",
        1,
        None,
        True,
        b"",
        b"{",
        b"\xff\xfe",
        b"NaN",
        b"[" * 500,
        b'{"a":' * 100,
        b'{"image_id":"i","mime":"image/png"}x',
        b'{"image_id":"i","mime":"image/png"}{}',
        b"9" * 500,
    ],
    ids=lambda h: repr(h)[:30],
)
def test_bad_image_headers_are_rejected(header: Any) -> None:
    bad_image(frame(header))


def test_decode_image_refuses_oversized_messages() -> None:
    header = {"image_id": "i", "mime": "image/png"}
    assert len(link.decode_image(frame(header, b"x" * link.MAX_IMAGE_BYTES))[2]) == (
        link.MAX_IMAGE_BYTES
    )
    # One byte over, still inside the room allowed for the largest header.
    bad_image(frame(header, b"x" * (link.MAX_IMAGE_BYTES + 1)), "too_large")
    bad_image(b"\x00" * (link.MAX_IMAGE_BYTES + 512 + 5), "too_large")


def test_fuzz_decode_image_only_ever_raises_link_error() -> None:
    rng = random.Random(42)
    seed = link.encode_image("img-1", "image/png", b"data")
    for _ in range(4000):
        junk = mutate(rng, seed)
        try:
            image_id, mime, data = link.decode_image(junk)
        except LinkError:
            continue
        assert mime in {"image/jpeg", "image/png"} and data
        assert link.decode_image(link.encode_image(image_id, mime, data)) == (image_id, mime, data)


def test_a_control_message_is_not_an_image_and_an_image_is_not_a_control_message() -> None:
    bad_image(link.encode(link.Ping(id="k1")).encode())
    image = link.encode_image("img-1", "image/png", PNG)
    rejected(link.decode_host, image)
    rejected(link.decode_brain, image)
