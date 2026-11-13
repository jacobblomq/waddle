"""The link token file: private on creation, refused when anyone else could read it."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from waddle_desktop.link.token import TokenError, ensure_token, read_token

GOOD = "ab" * 32


def _write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text)
    path.chmod(mode)
    return path


def test_ensure_creates_a_private_token_in_a_private_directory(tmp_path: Path) -> None:
    path = tmp_path / "data" / "waddle" / "link-token"
    old = os.umask(0)  # even with the most permissive umask
    try:
        token = ensure_token(path)
    finally:
        os.umask(old)
    assert len(token) == 64 and set(token) <= set("0123456789abcdef")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["link-token"]  # no temp file left
    assert read_token(path) == token


def test_ensure_closes_an_existing_directory_before_creating_the_token(tmp_path: Path) -> None:
    parent = tmp_path / "waddle"
    parent.mkdir(mode=0o755)
    ensure_token(parent / "link-token")
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_ensure_returns_the_existing_token_and_tokens_differ(tmp_path: Path) -> None:
    first = ensure_token(tmp_path / "a" / "t")
    assert ensure_token(tmp_path / "a" / "t") == first
    assert ensure_token(tmp_path / "b" / "t") != first


def test_ensure_never_replaces_a_file_it_cannot_trust(tmp_path: Path) -> None:
    path = _write(tmp_path / "t", GOOD, 0o640)
    with pytest.raises(TokenError) as raised:
        ensure_token(path)
    assert raised.value.code == "bad_token_file"
    assert path.read_text() == GOOD
    assert GOOD not in str(raised.value)


def test_ensure_does_not_write_through_a_dangling_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    link = tmp_path / "t"
    link.symlink_to(target)
    with pytest.raises(TokenError):
        ensure_token(link)
    assert not target.exists()


def test_read_accepts_a_private_token_with_or_without_newline(tmp_path: Path) -> None:
    assert read_token(_write(tmp_path / "a", GOOD)) == GOOD
    assert read_token(_write(tmp_path / "b", GOOD + "\n")) == GOOD
    assert read_token(_write(tmp_path / "c", GOOD, 0o400)) == GOOD


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o602, 0o610, 0o644, 0o666])
def test_read_refuses_a_token_others_could_reach(tmp_path: Path, mode: int) -> None:
    assert read_token(_write(tmp_path / "t", GOOD, mode)) is None


@pytest.mark.parametrize(
    "text",
    ["", "ab" * 31, "ab" * 33, "AB" * 32, "zz" * 32, GOOD + "\n\n", " " + GOOD, GOOD + "\x00"],
)
def test_read_refuses_anything_but_64_hex(tmp_path: Path, text: str) -> None:
    assert read_token(_write(tmp_path / "t", text)) is None


def test_read_refuses_symlink_directory_fifo_and_missing(tmp_path: Path) -> None:
    real = _write(tmp_path / "real", GOOD)
    link = tmp_path / "link"
    link.symlink_to(real)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    assert read_token(link) is None
    assert read_token(tmp_path) is None
    assert read_token(fifo) is None  # and does not hang on it
    assert read_token(tmp_path / "missing") is None


def test_read_refuses_a_file_owned_by_someone_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "t", GOOD)
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)
    assert read_token(path) is None
