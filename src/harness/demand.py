"""
src/harness/demand.py   DemandGenerator: where and when riders ask for trips.

Uniformly random homes and times (what the first demos used) make pooling nearly impossible: nobody
is on the way to anybody. Real demand is clustered in space (hotspots: hospitals, universities,
dense neighbourhoods) and in time (peaks). This generator draws

  * destinations around `hotspots` randomly chosen homes (the `spread` nearest homes by travel time),
    a `background` share uniformly over the covered homes;
  * request times from `peaks` Gaussian rush periods (70 %) plus a uniform background (30 %);
  * detour tolerance as a FRACTION of the direct ride (the rider's patience scales with the trip),
    drawn from `tol_frac`; `solo_share` of riders refuse pooling.

Seeded: the same seed gives the same demand.
"""
import random
from dataclasses import dataclass
from typing import List, Sequence, Tuple


@dataclass(frozen=True)
class Demand:
    at: float                  # sim seconds after the start
    rider: int                 # index of the rider agent
    destination: str
    tol_frac: float            # detour tolerance / direct ride time
    pooling: bool


class DemandGenerator:
    def __init__(self, network, homes: Sequence[str], seed: int = 1, hotspots: int = 6, spread: int = 120,
                 background: float = 0.2, peaks: Sequence[float] = (0.25, 0.7), peak_share: float = 0.7,
                 tol_frac: Tuple[float, float] = (0.2, 0.5), solo_share: float = 0.2, clustered: bool = True):
        self.rng = random.Random(seed)
        self.homes = sorted(homes)
        if not self.homes:
            raise ValueError("no homes to generate demand for")
        self.peaks, self.peak_share, self.tol_frac = list(peaks), peak_share, tol_frac
        self.solo_share, self.background, self.clustered = solo_share, background, clustered
        self.clusters: List[List[str]] = []
        if clustered:
            for center in self.rng.sample(self.homes, min(hotspots, len(self.homes))):
                near = sorted(self.homes, key=lambda h: (network.travel_time(center, h), h))
                self.clusters.append(near[:spread])

    def destination(self) -> str:
        if not self.clustered or self.rng.random() < self.background:
            return self.rng.choice(self.homes)
        return self.rng.choice(self.rng.choice(self.clusters))

    def time(self, span: float) -> float:
        if self.rng.random() < self.peak_share and self.peaks:
            c = self.rng.choice(self.peaks) * span
            return min(span, max(0.0, self.rng.gauss(c, span / 12)))
        return self.rng.uniform(0, span)

    def generate(self, n: int, span: float) -> List[Demand]:
        out = [Demand(self.time(span), i, self.destination(), self.rng.uniform(*self.tol_frac),
                      self.rng.random() >= self.solo_share) for i in range(n)]
        return sorted(out, key=lambda d: (d.at, d.rider))
