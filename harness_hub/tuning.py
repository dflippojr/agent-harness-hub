"""The timing constants the Hub's routes read (#255): one frozen `Tuning` on `HubContext`, with today's values as the
defaults. A test passes its own `Tuning` to `create_app` (or replaces the context's), so no module is patched."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tuning:
    allowed_logins_seconds: float = 5.0  # how long the Hub reuses the daemon's allowed_logins answer
    # how long after the daemon last listed a tailnet tab's login an outage keeps the tab
    outage_grace_seconds: float = 120.0
    pairing_count_seconds: float = 5.0  # how long Overview waits for its optional pairing count, consent lock included
    spend_cap_reads: int = 8  # App session-cap reads in flight at once; every live App is read
    restart_grace_seconds: float = 1.0  # how long after a restart request the Hub ignores health that predates it
    known_recoveries: int = 16  # recoveries the Hub remembers from daemon replies, to name in a restart's 202 (#143)

DEFAULT_TUNING = Tuning()  # a default argument may not call a constructor (B008): share this one
