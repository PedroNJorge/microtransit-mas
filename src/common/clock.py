"""
src/common/clock.py
Scaled simulation clock on top of asyncio (no simpy).

    sim_time = sim_base + (monotonic_now - wall_base) * SIMULATION_SPEED

Rules for the whole project
- The harness (main.py) calls `start()` ONCE before any agent starts: that is t = 0.
  (If nobody does, the first call to now() starts it, so t = 0 becomes "first use".)
- Every wait is `await sim_sleep(...)`; every timestamp in the ontology is `now()`.
- Durations given to SPADE (e.g. PeriodicBehaviour period) go through `to_real()`.
- XMPP latency is REAL time. Keep sim-second timeouts comfortably above it
  (auction deadline * (1 / speed) must stay well above a CFP/bid round trip).

Changing the speed mid-run does not move time: the clock is re-based, so `now()` is
continuous. Periods already handed to SPADE behaviours are NOT recomputed.

Backwards compatible with the first version: `set_simulation_speed`, `reset_clock`,
`now`, `sim_sleep` and the `SIMULATION_SPEED` attribute all still exist. Prefer
`speed()` over reading `SIMULATION_SPEED`, which is a copy kept only for convenience
(`from common.clock import SIMULATION_SPEED` captures a stale value).
"""
import asyncio
import time
from typing import Optional

DEFAULT_SPEED: float = 10.0
SIMULATION_SPEED: float = DEFAULT_SPEED      # kept in sync by set_simulation_speed()

_sim_base: float = 0.0                       # sim time at the last (re)base
_wall_base: Optional[float] = None           # time.monotonic() at the last (re)base


def _check_speed(speed: float) -> float:
    if speed <= 0:
        raise ValueError("simulation speed must be > 0")
    return float(speed)


def start(speed: Optional[float] = None) -> None:
    """(Re)start the clock at sim time 0, optionally changing the speed."""
    global SIMULATION_SPEED, _sim_base, _wall_base
    if speed is not None:
        SIMULATION_SPEED = _check_speed(speed)
    _sim_base = 0.0
    _wall_base = time.monotonic()


def reset_clock() -> None:
    """Restart at t = 0 keeping the current speed (name kept from the first version)."""
    start()


def reset(speed: float = DEFAULT_SPEED) -> None:
    """Back to defaults and NOT started. Meant for tests."""
    global SIMULATION_SPEED, _sim_base, _wall_base
    SIMULATION_SPEED = _check_speed(speed)
    _sim_base, _wall_base = 0.0, None


def is_started() -> bool:
    return _wall_base is not None


def speed() -> float:
    return SIMULATION_SPEED


def set_simulation_speed(new_speed: float) -> None:
    """Change the speed without moving time (re-bases the clock first)."""
    global SIMULATION_SPEED, _sim_base, _wall_base
    new_speed = _check_speed(new_speed)
    _sim_base = now()                        # freeze elapsed sim time at the old speed
    _wall_base = time.monotonic()
    SIMULATION_SPEED = new_speed


def now() -> float:
    """Current simulation time in sim seconds. Starts the clock on first use."""
    if _wall_base is None:
        start()
    return _sim_base + (time.monotonic() - _wall_base) * SIMULATION_SPEED


def to_real(sim_seconds: float) -> float:
    """Sim duration -> real seconds at the current speed."""
    return sim_seconds / SIMULATION_SPEED


def to_sim(real_seconds: float) -> float:
    """Real duration -> sim seconds at the current speed."""
    return real_seconds * SIMULATION_SPEED


async def sim_sleep(sim_seconds: float) -> None:
    """asyncio.sleep for a duration given in simulation seconds."""
    if sim_seconds <= 0:
        return
    await asyncio.sleep(to_real(sim_seconds))
