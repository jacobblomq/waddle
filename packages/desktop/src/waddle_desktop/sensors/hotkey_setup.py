"""Decides which hotkeys to bind and binds them (ARCHITECTURE §4.5).

Only actions that already do something are bound: a global shortcut that swallows a key and
then does nothing would be worse than no shortcut. Before binding, each preferred key is
checked against the shortcuts you already have; a taken key is never requested, and the
action is offered to the portal without a preferred key so you choose one yourself.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from dbus_fast import BusType
from dbus_fast.aio import MessageBus

from waddle_core.config import HotkeysConfig
from waddle_desktop import log
from waddle_desktop.sensors.hotkeys import (
    BindingPlan,
    BindResult,
    KGlobalAccelUnavailableError,
    PortalHotkeys,
    find_conflicts,
    plan_bindings,
    wanted_from_config,
)
from waddle_desktop.sensors.keys import KeyCombo

# Bound today. push_to_talk (Phase 5), show_me_where and region_select (Phase 6) and
# focus_mode (Phase 7) join when the features behind them exist.
LIVE_ACTIONS: Final = ("ask", "pause", "quiet", "bring_back")


@dataclass(frozen=True)
class HotkeyPlan:
    plans: tuple[BindingPlan, ...]
    invalid: dict[str, str]  # action -> why its configured key is not usable
    conflicts_checked: bool  # False: the existing shortcuts could not be read

    def bindings(self) -> dict[str, KeyCombo | None]:
        wanted: dict[str, KeyCombo | None] = {plan.action: plan.combo for plan in self.plans}
        if not self.conflicts_checked:
            # Unknown is not "free": ask for no key rather than risk taking someone else's.
            wanted = dict.fromkeys(wanted)
        for action in self.invalid:
            wanted.setdefault(action, None)
        return wanted


async def _session_bus() -> MessageBus:
    return await MessageBus(bus_type=BusType.SESSION).connect()


async def make_plan(
    config: HotkeysConfig,
    bus_factory: Callable[[], Awaitable[MessageBus]] = _session_bus,
    actions: tuple[str, ...] = LIVE_ACTIONS,
) -> HotkeyPlan:
    """Read-only: parse the configured keys and look for clashes with existing shortcuts."""
    combos, invalid = wanted_from_config(config)
    wanted = {action: combo for action, combo in combos.items() if action in actions}
    invalid = {action: why for action, why in invalid.items() if action in actions}
    checked = True
    conflicts = {}
    try:
        bus = await bus_factory()
    except Exception:
        checked = False
    else:
        try:
            conflicts = await find_conflicts(bus, wanted)
        except KGlobalAccelUnavailableError:
            checked = False
        finally:
            with contextlib.suppress(Exception):
                bus.disconnect()
    return HotkeyPlan(tuple(plan_bindings(wanted, conflicts)), invalid, checked)


async def bind(plan: HotkeyPlan, portal: PortalHotkeys) -> BindResult | None:
    bindings = plan.bindings()
    if not bindings:
        return None
    result = await portal.start(bindings)
    log.event(
        "hotkeys.bound",
        status=result.status.value,
        bound=len(result.bound),
        missing=len(result.missing),
        conflicts=sum(1 for p in plan.plans if p.conflict is not None),
        checked=plan.conflicts_checked,
    )
    return result
