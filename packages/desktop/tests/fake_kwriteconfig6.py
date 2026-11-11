#!/usr/bin/env python3
"""Test double for ``kwriteconfig6`` / ``kreadconfig6`` on a KConfig-style INI file.

Usage: ``fake_kwriteconfig6.py (kwriteconfig6|kreadconfig6) --file F --group G --key K ...``

It edits the file line by line, so every line it is not asked to change — other groups, other
keys, comments, blank lines — survives byte-for-byte and in order. Groups are created on demand
at the end of a non-empty file after one blank separator line of their own; ``--delete``
removes a key and drops a group that has no entries left, together with its separator. Values
are stored unescaped.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def _is_header(line: str) -> bool:
    text = line.strip()
    return text.startswith("[") and text.endswith("]")


def _group_span(lines: list[str], group: str) -> tuple[int, int] | None:
    """(index of the header, index one past the group's last line), or None."""
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == f"[{group}]")
    except StopIteration:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _is_header(lines[i])), len(lines))
    return start, end


def _key_index(lines: list[str], span: tuple[int, int], key: str) -> int | None:
    for i in range(span[0] + 1, span[1]):
        name, sep, _ = lines[i].partition("=")
        if sep and name.rstrip() == key and not name.startswith("#"):
            return i
    return None


def read(lines: list[str], group: str, key: str) -> str:
    span = _group_span(lines, group)
    index = _key_index(lines, span, key) if span else None
    return "" if index is None else lines[index].partition("=")[2].rstrip("\n")


def write(lines: list[str], group: str, key: str, value: str) -> None:
    entry = f"{key}={value}\n"
    span = _group_span(lines, group)
    if span is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines:
            lines.append("\n")  # always its own separator, so delete() can take it back
        lines += [f"[{group}]\n", entry]
        return
    index = _key_index(lines, span, key)
    if index is not None:
        lines[index] = entry
        return
    end = span[1]
    while end > span[0] + 1 and not lines[end - 1].strip():
        end -= 1  # insert before the blank lines that separate this group from the next
    lines.insert(end, entry)


def delete(lines: list[str], group: str, key: str) -> None:
    span = _group_span(lines, group)
    index = _key_index(lines, span, key) if span else None
    if span is None or index is None:
        return
    del lines[index]
    start, end = span[0], span[1] - 1
    if any(line.strip() for line in lines[start + 1 : end]):
        return
    # The group is empty: drop its header and its blank lines. If it had none of its own (it
    # was last in the file), drop the separator before it instead.
    had_own_blank = end > start + 1
    del lines[start:end]
    if not had_own_blank and start > 0 and not lines[start - 1].strip():
        del lines[start - 1]


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("kwriteconfig6", "kreadconfig6"):
        print("first argument must be kwriteconfig6 or kreadconfig6", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog=argv[1])
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--group", required=True)
    parser.add_argument("--key", required=True)
    if argv[1] == "kwriteconfig6":
        parser.add_argument("--type", choices=["string", "bool"])
        parser.add_argument("--delete", action="store_true")
        parser.add_argument("value", nargs="?")
    args = parser.parse_args(argv[2:])

    path: Path = args.file
    original = path.read_bytes().decode("utf-8") if path.exists() else None
    # Split on "\n" only: str.splitlines would also break at U+2028, form feed and the like.
    lines = [line for line in re.split(r"(?<=\n)", original or "") if line]

    if argv[1] == "kreadconfig6":
        print(read(lines, args.group, args.key))
        return 0
    if args.delete == (args.value is not None):
        print("give either a value or --delete", file=sys.stderr)
        return 2
    if args.delete:
        delete(lines, args.group, args.key)
    else:
        write(lines, args.group, args.key, args.value)
    text = "".join(lines)
    if text != original and not (original is None and args.delete):
        path.write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
