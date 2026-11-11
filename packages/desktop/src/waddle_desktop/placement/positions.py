"""Where Waddle sits on each monitor. Stored in Waddle's own SQLite file (ARCHITECTURE §7)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


def default_position(output: Rect, window: tuple[int, int], margin: int) -> tuple[int, int]:
    """Bottom-right corner of the output, inset by ``margin`` (global logical coordinates)."""
    width, height = window
    return (
        max(output.x, output.right - width - margin),
        max(output.y, output.bottom - height - margin),
    )


def clamp_to_output(pos: tuple[int, int], output: Rect, window: tuple[int, int]) -> tuple[int, int]:
    """Keep the whole window on the output (a saved position may predate a resolution change)."""
    width, height = window
    x = min(max(pos[0], output.x), max(output.x, output.right - width))
    y = min(max(pos[1], output.y), max(output.y, output.bottom - height))
    return x, y


class PositionStore:
    """Per-output position, saved as an offset from the output's origin."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(db_path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS positions ("
            " output TEXT PRIMARY KEY, dx INTEGER NOT NULL, dy INTEGER NOT NULL)"
        )
        self._db.commit()
        db_path.chmod(0o600)

    def save(self, output: Rect, name: str, pos: tuple[int, int]) -> None:
        self._db.execute(
            "INSERT INTO positions(output, dx, dy) VALUES(?,?,?)"
            " ON CONFLICT(output) DO UPDATE SET dx=excluded.dx, dy=excluded.dy",
            (name, pos[0] - output.x, pos[1] - output.y),
        )
        self._db.commit()

    def load(self, output: Rect, name: str) -> tuple[int, int] | None:
        row = self._db.execute(
            "SELECT dx, dy FROM positions WHERE output = ?", (name,)
        ).fetchone()
        return None if row is None else (output.x + int(row[0]), output.y + int(row[1]))

    def forget(self) -> None:
        self._db.execute("DELETE FROM positions")
        self._db.commit()

    def close(self) -> None:
        self._db.close()
