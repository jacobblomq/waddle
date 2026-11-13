"""Shared test plumbing for the brain: async tests without a plugin, and captured logs."""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
from collections.abc import Iterator

import pytest


def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Run ``async def`` tests on a fresh event loop (no plugin dependency)."""
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    names = pyfuncitem._fixtureinfo.argnames
    asyncio.run(pyfuncitem.obj(**{name: pyfuncitem.funcargs[name] for name in names}))
    return True


@pytest.fixture
def logs() -> Iterator[io.StringIO]:
    """Everything logged during the test, by the brain or by any library, at DEBUG."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root = logging.getLogger()
    brain = logging.getLogger("waddle_brain")
    client = logging.getLogger("websockets.client")  # the test's own client is not the brain
    saved = (root.level, brain.level, brain.propagate, brain.handlers[:], client.propagate)
    client.propagate = False
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    brain.handlers[:] = []
    brain.setLevel(logging.DEBUG)
    brain.propagate = True
    yield buffer
    root.removeHandler(handler)
    root.setLevel(saved[0])
    brain.setLevel(saved[1])
    brain.propagate = saved[2]
    brain.handlers[:] = saved[3]
    client.propagate = saved[4]
