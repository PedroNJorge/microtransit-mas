"""
src/common/lock.py   The driver's provisional-lock state machine (CC-01 .. CC-09). Pure Python.

No SPADE, no clock, no messages: every method takes `now` (sim seconds) and returns a decision,
so each rule is unit-tested in isolation (tests/test_driver_fsm.py). The DriverAgent only
translates messages into calls and decisions into messages.

                 acquire                commit (accept-proposal)           finish / abort
        FREE ------------> PROVISIONAL -------------------------> FIRM ----------------> FREE
                              |  ^                                 |
          release (reject) /  |  | acquire again (same hub+pool,   +-- abort (hub cancel, failure) -> FREE
          expiry (CC-02)  ----+  |  higher attempt: replaces)
                              v  |
                             FREE

Rules
  CC-01  at most ONE lock (provisional or firm) at any time.
  CC-02  a provisional lock expires `t_provisional` after it was acquired.
  CC-03  commit turns it firm; while firm, every other CFP is refused.
  CC-04  a reject-proposal (or expiry) releases a PROVISIONAL lock. A firm lock is never released
         by a reject (only finish/abort), and only the hub that holds the lock can change it.
  CC-06  firm = the driver is assigned; no second trip can overlap (it holds until finish/abort).
  CC-07  every state change calls `on_change(old, new, lock, reason)` so the agent can broadcast.
  CC-09  acquire() while another lock is held -> Decision(False, "locked").

Idempotence: a repeated CFP (same hub, pool, attempt) returns the same decision without extending
the expiry; a repeated commit/release/finish is harmless. A CFP of a later attempt for the same
pool from the same hub REPLACES the old provisional lock (the hub's retry), an older one is refused.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, NamedTuple, Optional, Set, Tuple

from common.settings import T_PROVISIONAL


class LockState(str, Enum):
    FREE = "FREE"
    PROVISIONAL = "PROVISIONAL"
    FIRM = "FIRM"


class LockReason:
    LOCKED = "locked"                    # CC-09 refuse reason
    STALE_ATTEMPT = "stale_attempt"
    NO_LOCK = "no_lock"
    LOCK_EXPIRED = "lock_expired"
    NOT_HOLDER = "not_holder"            # right pool, wrong hub (or wrong pool)


@dataclass(frozen=True)
class Lock:
    pool_id: str
    hub: str
    bid_id: str
    attempt: int
    acquired_at: float
    expires_at: Optional[float]          # None once firm
    state: LockState


class Decision(NamedTuple):
    ok: bool
    reason: str = ""
    replay: bool = False                 # the request had already been applied


Listener = Callable[[LockState, LockState, Optional[Lock], str], None]


class DriverLock:
    def __init__(self, t_provisional: float = T_PROVISIONAL, on_change: Optional[Listener] = None):
        if not t_provisional > 0:
            raise ValueError("t_provisional must be > 0")
        self.t_provisional = t_provisional
        self.on_change = on_change
        self._lock: Optional[Lock] = None
        self._expired: Set[str] = set()                      # pools whose provisional lock expired
        self.history: List[Tuple[float, str, str, str]] = []     # (t, from, to, reason)

    # ---- inspection ---------------------------------------------------------------------
    @property
    def state(self) -> LockState:
        return self._lock.state if self._lock else LockState.FREE

    @property
    def holder(self) -> Optional[Lock]:
        return self._lock

    def holds(self, pool_id: str, hub: Optional[str] = None) -> bool:
        l = self._lock
        return l is not None and l.pool_id == pool_id and (hub is None or l.hub == hub)

    def blocks(self, pool_id: str, hub: str, attempt: int, now: float) -> bool:
        """True if a CFP for (hub, pool, attempt) would be refused as `locked` (CC-09)."""
        self.tick(now)
        l = self._lock
        if l is None:
            return False
        return not (l.state == LockState.PROVISIONAL and l.hub == hub and l.pool_id == pool_id
                    and attempt >= l.attempt)

    # ---- transitions --------------------------------------------------------------------
    def _set(self, new: Optional[Lock], now: float, reason: str) -> None:
        old_state, new_state = self.state, (new.state if new else LockState.FREE)
        self._lock = new
        self.history.append((now, old_state.value, new_state.value, reason))
        if self.on_change and (old_state != new_state or reason == "replaced"):
            self.on_change(old_state, new_state, new, reason)

    def tick(self, now: float) -> Optional[Lock]:
        """CC-02: drop an expired provisional lock. Returns it if one just expired."""
        l = self._lock
        if l and l.state == LockState.PROVISIONAL and now >= l.expires_at:
            self._expired.add(l.pool_id)
            self._set(None, now, "expired")
            return l
        return None

    def acquire(self, pool_id: str, hub: str, bid_id: str, attempt: int, now: float) -> Decision:
        self.tick(now)
        l = self._lock
        if l is None:
            self._expired.discard(pool_id)
            self._set(Lock(pool_id, hub, bid_id, attempt, now, now + self.t_provisional,
                           LockState.PROVISIONAL), now, "acquired")
            return Decision(True)
        if l.state == LockState.PROVISIONAL and l.hub == hub and l.pool_id == pool_id:
            if attempt == l.attempt:
                return Decision(True, replay=True)             # duplicate CFP
            if attempt > l.attempt:                            # the hub's retry replaces it
                self._set(Lock(pool_id, hub, bid_id, attempt, now, now + self.t_provisional,
                               LockState.PROVISIONAL), now, "replaced")
                return Decision(True)
            return Decision(False, LockReason.STALE_ATTEMPT)
        return Decision(False, LockReason.LOCKED)              # CC-01 / CC-09

    def commit(self, pool_id: str, hub: str, now: float) -> Decision:
        """accept-proposal (CC-03)."""
        self.tick(now)
        l = self._lock
        if l is None:
            return Decision(False, LockReason.LOCK_EXPIRED if pool_id in self._expired
                            else LockReason.NO_LOCK)
        if l.pool_id != pool_id or l.hub != hub:
            return Decision(False, LockReason.NOT_HOLDER)
        if l.state == LockState.FIRM:
            return Decision(True, replay=True)
        self._set(Lock(l.pool_id, l.hub, l.bid_id, l.attempt, l.acquired_at, None, LockState.FIRM),
                  now, "committed")
        return Decision(True)

    def release(self, pool_id: str, hub: str, now: float, reason: str = "rejected") -> bool:
        """reject-proposal (CC-04): frees a PROVISIONAL lock. Never touches a firm one."""
        self.tick(now)
        l = self._lock
        if l and l.state == LockState.PROVISIONAL and l.pool_id == pool_id and l.hub == hub:
            self._set(None, now, reason)
            return True
        return False

    def abort(self, pool_id: str, hub: str, now: float, reason: str = "aborted") -> bool:
        """Hub cancel or trip failure: frees the lock in either state (holder only)."""
        self.tick(now)
        l = self._lock
        if l and l.pool_id == pool_id and l.hub == hub:
            self._set(None, now, reason)
            return True
        return False

    def finish(self, pool_id: str, now: float) -> bool:
        """Trip over: FIRM -> FREE."""
        l = self._lock
        if l and l.state == LockState.FIRM and l.pool_id == pool_id:
            self._set(None, now, "finished")
            return True
        return False
