"""Restart and recovery tracking (#255): the restart the Hub asked for, until health shows it done, and the recoveries
a daemon reply reported, which name a restart's 202 (#143). Overview and the Settings health poll both end a restart,
so neither page has to stay open."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .guard import leaks

if TYPE_CHECKING:
    from .routes.common import HubContext


@dataclass
class RestartState:
    """A restart the Hub asked for, until health shows it done. `generation` counts the restarts asked for, so a health
    request begun before the newest one cannot end it; `not_before` is the monotonic time before which health is
    ignored; `target` is the settings revision the restart must reach, or None for any."""

    active: bool = False
    target: int | None = None
    generation: int = 0
    not_before: float = 0.0


def observe_recovery(ctx: HubContext, recovery: dict | None) -> None:
    """Remember a recovery a daemon reply reported. Any reply the Hub has before it asks for a restart was sampled
    before that restart, so these are the recoveries the shell must not take for one of it (#143). One the response
    guard would withhold is not kept: the page never had it, and naming it would withhold every restart reply."""
    if recovery and recovery not in ctx.recoveries and not leaks(json.dumps(recovery).encode(), ctx.store.secrets()):
        ctx.recoveries.append(recovery)
        del ctx.recoveries[:-ctx.tuning.known_recoveries]


def observe_restart(ctx: HubContext, shown: dict | None, health_generation: int) -> None:
    """End a restart the Hub started once the daemon answers healthy with confirmed settings, at the target revision
    or a later one (a save made during the restart, which the restarted daemon reports), or after a recovery. Only
    a health request begun since this restart can end it. Overview and the Settings health poll both look, so
    neither page has to stay open."""
    restart = ctx.restart
    revision = shown["config_revision"] if shown else None
    if (health_generation == restart.generation and time.monotonic() >= restart.not_before
            and shown and shown["ok"] and shown["config_confirmed"]) and (
            restart.target is None or isinstance(revision, int) and revision >= restart.target
            or shown["recovery"]):
        restart.active = False
