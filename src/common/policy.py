"""
src/common/policy.py   PolicyBook: the policies an agent has acknowledged (RI-04). Pure Python.

A Policy has a monotonically increasing `version` and an `effective_from` time. The book
  * accepts a newer version (-> "agree"),
  * is idempotent for a repeat of the version it already holds (still "agree"),
  * refuses an older one (`stale_version`), a different policy under a version already used
    (`conflict`) and an invalid one (`invalid`),
  * answers "which policy is in force at time t": the highest version whose effective_from <= t.

The fare cap is `fare_caps["max"]`: the most a whole trip may cost (RI-01).
"""
import math
from typing import Dict, List, NamedTuple, Optional

from common.ontology import Policy

STALE, CONFLICT, INVALID = "stale_version", "conflict", "invalid"


class Ack(NamedTuple):
    ok: bool
    reason: str = ""


def validate(p: Policy) -> str:
    """'' if well formed, else why not."""
    for name, v in {**{f"fare_caps.{k}": x for k, x in p.fare_caps.items()},
                    **{f"subsidy.{k}": x for k, x in p.subsidy_rules.items()}}.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0:
            return f"{name} must be a finite number >= 0"
    rate = p.subsidy_rules.get("eligible_discount")
    if rate is not None and not 0 <= rate <= 1:
        return "subsidy.eligible_discount must be in [0, 1]"
    if "max" in p.fare_caps and p.fare_caps["max"] <= 0:
        return "fare_caps.max must be > 0"
    if p.version < 0:
        return "version must be >= 0"
    return ""


class PolicyBook:
    def __init__(self):
        self.by_version: Dict[int, Policy] = {}

    @property
    def latest(self) -> Optional[Policy]:
        return self.by_version[max(self.by_version)] if self.by_version else None

    def offer(self, p: Policy) -> Ack:
        bad = validate(p)
        if bad:
            return Ack(False, INVALID)
        known = self.by_version.get(p.version)
        if known is not None:
            return Ack(True) if known == p else Ack(False, CONFLICT)
        if self.by_version and p.version < max(self.by_version):
            return Ack(False, STALE)
        self.by_version[p.version] = p
        return Ack(True)

    def active(self, now: float) -> Optional[Policy]:
        live = [p for p in self.by_version.values() if p.effective_from <= now]
        return max(live, key=lambda p: p.version) if live else None

    def fare_cap(self, now: float) -> Optional[float]:
        p = self.active(now)
        return p.fare_caps.get("max") if p else None

    def cap_history(self) -> List[tuple]:
        """[(effective_from, cap or None)] sorted by time then version, for audits."""
        return sorted(((p.effective_from, p.fare_caps.get("max"), p.version) for p in self.by_version.values()),
                      key=lambda x: (x[0], x[2]))
