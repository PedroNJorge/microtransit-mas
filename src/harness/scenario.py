"""
src/harness/scenario.py
Parse and validate a scenario file (config/scenario.json) and derive the agent roster.

The scenario is the single source of truth for WHO exists and WHERE they are. Addresses are
scenario-driven (SPADE 4 has no Directory Facilitator): every agent's JID is
`<name>@<domain>`, with names fixed by this module:

    central   payment, regulator, reputation, environment
    hubs      the hub's node id            e.g. hub_trindade
    drivers   driver_01, driver_02, ...
    riders    rider_01, rider_02, ...

Schema (all sections but "graph" and "hubs" are optional):
{
  "name": "dev-4hubs",
  "xmpp":  {"host": "localhost", "port": 5222, "server_port": 5269,
            "domain": "localhost", "password": "dev-password"},
  "clock": {"speed": 60},                       # sim seconds per real second
  "graph": "config/porto_graph.json",           # relative paths resolve from the project root
  "hubs": ["hub_trindade", ...]  or  "all",     # ACTIVE hubs, a subset of the graph's hubs
  "drivers": {"count": 2, "start_hubs": ["hub_trindade"]},   # default: round-robin over hubs
  # optional driver intelligence (all off by default):
  #   "brains": true          every driver gets a BDI brain agent (brain_01, ...) that picks where it waits
  #   "brain_bids": true      the brain also sets the driver's bid markup (needs "brains")
  #   "return_policy": "nearest_hub" | "start_hub" | "none"   fixed rule, no BDI (ignored when "brains")
  "riders":  {"count": 2},
  "environment": {"t_traffic": 60, "full_every": 10},
  "payment": {"rider_balance": 100.0, "subsidy_discount": 0.5,
              "subsidy_budget": null, "hold_max": 7200},
  "seed": 42
}
payment: rider_balance = wallet each rider gets on their first escrow request (simulation
endowment); subsidy_discount = share of the fare rebated to subsidy-eligible riders;
subsidy_budget = size of the subsidy fund (null = unlimited); hold_max = sim seconds after
which an unsettled escrow is returned to the rider.
Unknown keys are rejected (a typo must not silently fall back to a default).

Pooling is the standard: hubs batch requests for "t_pool" seconds (default 180; 0 turns it off), riders and
demand streams are pooling-willing by default.

"patient" (default true): nobody gives up. A hub whose auction gets no bids tries again (with a wider pickup radius)
until a driver is free and a rider waits for as long as that takes. With false, a hub gives up after 4 attempts
and the rider ends 'unmatched'.
With a demand section the run ends by itself once every request has been issued and every rider is home;
"duration" is then only the safety cap (sim seconds).

Optional dynamic sections (what HAPPENS during the run; all times are sim seconds):
{
  "duration": 3600,                             # the launcher stops by itself after this sim time
  "drivers": {"count": 12, "placement": [{"hubs": {"centre": 2}, "count": 12}]},
  "demand": [                                   # request streams, trips go hub -> covered node
    {"id": "burst", "hubs": "hub_a", "start": 600, "end": 660, "count": 30,
     "pattern": "random",                       # "even" (default) or "random" times in [start, end)
     "max_fare": 20, "window": 3600, "detour_tolerance": 600,
     "pooling": 1.0, "subsidy": 0.0}            # probabilities per request (default 1.0: everyone pools)
  ],
  "events": [
    {"at": 900, "type": "block_road", "duration": 1200,
     "where": {"random_on_routes": 2, "among": "all"}      # or {"edges": ["a|b"]}
     "both_directions": true},
    {"at": 900, "type": "traffic", "duration": 1800, "where": "all",   # or {"near": sel, "radius": s}
     "severity": 2.0                            # or "profile": [[0, 1.5], [600, 3.0], [1200, 1.5]]
    },
    {"at": 1800, "type": "kill_hub", "hub": "hub_b"}
  ],
  "forecasts": [                                # the Environment tells hubs about a known upcoming surge
    {"at": 300, "hub": "hub_a", "expected": 20, "horizon": 900, "to": "hub_c"}
  ],
  "reposition": {"demand_threshold": 3, "requests_per_driver": 4, "max_travel": 600,
                 "cooldown": 120, "t_rebalance": 60, "t_reposition": 30}     # switches it on
}
Hub selectors ("hubs", "hub", "to", "among", "near"): "all" | "hub_id" | ["hub_id", ...] |
{"centre": k} (k hubs closest to the centroid of the active hubs) | {"periphery": k} (k farthest)
| {"role": "x"} | {"index": [0, 1]} (positions in the active hub list). Resolved when the
scenario is loaded, so a wrong id fails at once.
"""
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from common.settings import FULL_SNAPSHOT_EVERY, SUBSIDY_DISCOUNT, T_ESCROW_HOLD_MAX, T_POOL, T_TRAFFIC

PROJECT_ROOT = Path(__file__).resolve().parents[2]       # <root>/src/harness/scenario.py
DEFAULT_SCENARIO = PROJECT_ROOT / "config" / "scenario.json"

CENTRAL_ROLES = ("payment", "regulator", "reputation", "environment")
START_ORDER = ("payment", "regulator", "reputation", "microhub", "driver", "rider", "environment")


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class AgentSpec:
    role: str                                   # key in agents.registry.AGENT_CLASSES
    name: str                                   # local part of the JID
    jid: str
    kwargs: Dict[str, Any] = field(default_factory=dict)   # extra constructor arguments


@dataclass
class DemandSpec:
    id: str
    hubs: List[str]                             # requests are spread over these hubs
    start: float
    end: float
    count: int
    pattern: str = "even"                       # even | random
    max_fare: float = 20.0
    window: float = 3600.0
    detour_tolerance: float = 600.0
    pooling: float = 1.0
    subsidy: float = 0.0


@dataclass
class EventSpec:
    at: float
    type: str                                   # block_road | traffic | kill_hub
    id: str = ""
    params: Dict[str, Any] = field(default_factory=dict)    # validated, hub selectors resolved


@dataclass
class ForecastSpec:
    at: float
    hub: str                                    # the hub that expects the demand
    expected: float
    horizon: float
    to: List[str] = field(default_factory=list)


@dataclass
class XmppConfig:
    host: str = "localhost"
    port: int = 5222
    server_port: int = 5269
    domain: str = ""
    password: str = "dev-password"              # dev only: local in-memory server


@dataclass
class Scenario:
    name: str
    graph_path: Path
    hub_ids: List[str]
    xmpp: XmppConfig = field(default_factory=XmppConfig)
    speed: float = 10.0
    n_drivers: int = 2
    driver_start_hubs: List[str] = field(default_factory=list)
    n_riders: int = 2
    rider_auto_request_interval: Optional[float] = None
    rider_auto_request_max_fare: float = 100.0
    t_traffic: float = T_TRAFFIC
    full_every: int = FULL_SNAPSHOT_EVERY
    rider_balance: float = 100.0
    subsidy_discount: float = SUBSIDY_DISCOUNT
    subsidy_budget: Optional[float] = None
    escrow_hold_max: float = T_ESCROW_HOLD_MAX
    seed: int = 42
    duration: Optional[float] = None           # sim seconds; None = run until stopped
    demand: List[DemandSpec] = field(default_factory=list)
    events: List[EventSpec] = field(default_factory=list)
    forecasts: List[ForecastSpec] = field(default_factory=list)
    reposition: Optional[Dict[str, float]] = None   # None = hubs never move drivers
    t_pool: float = T_POOL                     # batch window at the hubs (s); 0 switches pooling off
    patient: bool = True                       # hubs keep retrying and riders keep waiting until a driver is free
    legacy_market: bool = False                # True: hubs without the market fixes (reserve margin, pickup radius, retries)
    brains: bool = False                       # one BDI brain per driver (agents/brain.py)
    brain_bids: bool = False                   # the brain also owns the driver's bid markup
    return_policy: str = "none"                # fixed baseline: none | start_hub | nearest_hub
    source: Optional[Path] = None              # file it was loaded from (None: from_dict)

    # ---- addresses -------------------------------------------------------------------
    def jid(self, name: str) -> str:
        return f"{name}@{self.xmpp.domain}"

    def roster(self) -> List[AgentSpec]:
        """Every agent of the scenario, in a sensible start order (the Environment last, so
        its recipients already exist when the first traffic update goes out)."""
        specs: List[AgentSpec] = []
        specs.append(AgentSpec("payment", "payment", self.jid("payment"), {
            "default_rider_balance": self.rider_balance, "subsidy_discount": self.subsidy_discount,
            "subsidy_budget": self.subsidy_budget, "hold_max": self.escrow_hold_max}))
        for role in ("regulator", "reputation"):
            specs.append(AgentSpec(role, role, self.jid(role)))
        for hid in self.hub_ids:
            hub_kw = {"hub_id": hid}
            if self.brains:
                hub_kw["broadcast_demand"] = True          # the brains read demand from the hubs' calls
            specs.append(AgentSpec("microhub", hid, self.jid(hid), hub_kw))
        width = max(2, len(str(max(self.n_drivers, 1))))
        if self.brains:                                    # brains first: their driver's percepts need a listener
            for i in range(self.n_drivers):
                specs.append(AgentSpec("driver_brain", f"brain_{i + 1:0{width}d}",
                                       self.jid(f"brain_{i + 1:0{width}d}"),
                                       {"driver_jid": self.jid(f"driver_{i + 1:0{width}d}"),
                                        "bid_policy": self.brain_bids}))
        for i in range(self.n_drivers):
            name = f"driver_{i + 1:0{width}d}"
            kw = {"start_hub": self.driver_start_hubs[i % len(self.driver_start_hubs)]}
            if self.brains:
                kw["brain_jid"] = self.jid(f"brain_{i + 1:0{width}d}")
            elif self.return_policy != "none":
                kw["return_policy"] = self.return_policy
            specs.append(AgentSpec("driver", name, self.jid(name), kw))
        width = max(2, len(str(max(self.n_riders, 1))))
        for i in range(self.n_riders):
            name = f"rider_{i + 1:0{width}d}"
            specs.append(AgentSpec("rider", name, self.jid(name), {
                "auto_request_interval": self.rider_auto_request_interval,
                "auto_request_max_fare": self.rider_auto_request_max_fare}))
        specs.append(AgentSpec("environment", "environment", self.jid("environment")))
        return specs

    def resolve(self, who: str) -> List[AgentSpec]:
        """Find agents by full JID, local name, or role (e.g. 'microhub' -> every hub)."""
        roster = self.roster()
        hits = [s for s in roster if who in (s.jid, s.name)]
        return hits or [s for s in roster if s.role == who]

    # ---- parsing -------------------------------------------------------------------------
    @classmethod
    def load(cls, path: Union[str, Path, None] = None) -> "Scenario":
        path = Path(path) if path else DEFAULT_SCENARIO
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ScenarioError(f"Scenario file not found: {path}") from None
        except json.JSONDecodeError as e:
            raise ScenarioError(f"{path}: invalid JSON ({e})") from None
        scenario = cls.from_dict(data, default_name=path.stem)
        scenario.source = path.resolve()
        return scenario

    @classmethod
    def from_dict(cls, data: dict, default_name: str = "scenario") -> "Scenario":
        _only_keys(data, {"name", "xmpp", "clock", "graph", "hubs", "drivers", "riders",
                          "environment", "payment", "seed", "duration", "demand", "events",
                          "forecasts", "reposition", "t_pool", "legacy_market", "patient"}, "scenario")
        for required in ("graph", "hubs"):
            if required not in data:
                raise ScenarioError(f"scenario: missing required key {required!r}")

        x = dict(data.get("xmpp", {}))
        _only_keys(x, {"host", "port", "server_port", "domain", "password"}, "xmpp")
        xmpp = XmppConfig(host=str(x.get("host", "localhost")),
                          port=_int(x.get("port", 5222), "xmpp.port", 1),
                          server_port=_int(x.get("server_port", 5269), "xmpp.server_port", 1),
                          password=str(x.get("password", "dev-password")))
        xmpp.domain = str(x.get("domain") or xmpp.host)

        clock_cfg = dict(data.get("clock", {}))
        _only_keys(clock_cfg, {"speed"}, "clock")
        speed = _num(clock_cfg.get("speed", 10.0), "clock.speed", positive=True)

        graph_path = Path(data["graph"])
        if not graph_path.is_absolute():
            graph_path = PROJECT_ROOT / graph_path
        graph = _graph_info(graph_path)
        graph_hubs = graph.hub_ids

        hubs = data["hubs"]
        if hubs == "all":
            hub_ids = list(graph_hubs)
        elif isinstance(hubs, list) and hubs and all(isinstance(h, str) for h in hubs):
            hub_ids = list(hubs)
        else:
            raise ScenarioError('scenario.hubs must be "all" or a non-empty list of hub ids')
        if len(set(hub_ids)) != len(hub_ids):
            raise ScenarioError("scenario.hubs contains duplicates")
        unknown = [h for h in hub_ids if h not in graph_hubs]
        if unknown:
            raise ScenarioError(f"hubs not in the graph: {unknown}. Graph hubs: {graph_hubs}")

        sel = _Selector(hub_ids, graph)
        d = dict(data.get("drivers", {}))
        _only_keys(d, {"count", "start_hubs", "placement", "brains", "brain_bids", "return_policy"}, "drivers")
        if "placement" in d:
            if "start_hubs" in d:
                raise ScenarioError("drivers: use either start_hubs or placement, not both")
            place = d["placement"]
            if not (isinstance(place, list) and place):
                raise ScenarioError("drivers.placement must be a non-empty list")
            start_hubs = []
            for k, item in enumerate(place):
                _only_keys(item, {"hubs", "count"}, f"drivers.placement[{k}]")
                hubs_k = sel.resolve(item.get("hubs"), f"drivers.placement[{k}].hubs")
                n_k = _int(item.get("count"), f"drivers.placement[{k}].count", 1)
                start_hubs += [hubs_k[i % len(hubs_k)] for i in range(n_k)]    # round-robin inside the group
            n_drivers = len(start_hubs)
            if "count" in d and d["count"] != n_drivers:
                raise ScenarioError(f"drivers.count ({d['count']}) != sum of placement counts ({n_drivers})")
        else:
            n_drivers = _int(d.get("count", 2), "drivers.count", 0)
            start_hubs = d.get("start_hubs") or hub_ids
            if not (isinstance(start_hubs, list) and all(h in hub_ids for h in start_hubs)):
                raise ScenarioError(f"drivers.start_hubs must be a list of ACTIVE hubs {hub_ids}")

        brains, brain_bids = d.get("brains", False), d.get("brain_bids", False)
        return_policy = d.get("return_policy", "none")
        if not isinstance(brains, bool) or not isinstance(brain_bids, bool):
            raise ScenarioError("drivers.brains and drivers.brain_bids must be true or false")
        if brain_bids and not brains:
            raise ScenarioError("drivers.brain_bids needs drivers.brains")
        if return_policy not in ("none", "start_hub", "nearest_hub"):
            raise ScenarioError("drivers.return_policy must be none, start_hub or nearest_hub")

        r = dict(data.get("riders", {}))
        _only_keys(r, {"count", "auto_request_interval", "max_fare"}, "riders")
        n_riders = _int(r.get("count", 2), "riders.count", 0)
        auto_request_interval = r.get("auto_request_interval")
        if auto_request_interval is not None:
            auto_request_interval = _num(auto_request_interval, "riders.auto_request_interval",
                                         positive=True)
        auto_request_max_fare = _num(r.get("max_fare", 100.0), "riders.max_fare", positive=True)

        e = dict(data.get("environment", {}))
        _only_keys(e, {"t_traffic", "full_every"}, "environment")

        p = dict(data.get("payment", {}))
        _only_keys(p, {"rider_balance", "subsidy_discount", "subsidy_budget", "hold_max"}, "payment")
        discount = _num(p.get("subsidy_discount", SUBSIDY_DISCOUNT), "payment.subsidy_discount")
        if not 0.0 <= discount <= 1.0:
            raise ScenarioError(f"payment.subsidy_discount must be in [0, 1] (got {discount})")
        budget = p.get("subsidy_budget")
        if budget is not None:
            budget = _num(budget, "payment.subsidy_budget")
            if budget < 0:
                raise ScenarioError("payment.subsidy_budget must be >= 0 or null")
        rider_balance = _num(p.get("rider_balance", 100.0), "payment.rider_balance")
        if rider_balance < 0:
            raise ScenarioError("payment.rider_balance must be >= 0")

        duration = data.get("duration")
        if duration is not None:
            duration = _num(duration, "duration", positive=True)
        demand = _parse_demand(data.get("demand", []), sel)
        events = _parse_events(data.get("events", []), sel)
        forecasts = _parse_forecasts(data.get("forecasts", []), sel)
        reposition = _parse_reposition(data["reposition"]) if "reposition" in data else None

        return cls(name=str(data.get("name", default_name)), graph_path=graph_path,
                   hub_ids=hub_ids, xmpp=xmpp, speed=speed, n_drivers=n_drivers,
                   driver_start_hubs=list(start_hubs), n_riders=n_riders,
                   rider_auto_request_interval=auto_request_interval,
                   rider_auto_request_max_fare=auto_request_max_fare,
                   t_traffic=_num(e.get("t_traffic", T_TRAFFIC), "environment.t_traffic",
                                  positive=True),
                   full_every=_int(e.get("full_every", FULL_SNAPSHOT_EVERY),
                                   "environment.full_every", 1),
                   rider_balance=rider_balance, subsidy_discount=discount,
                   subsidy_budget=budget,
                   escrow_hold_max=_num(p.get("hold_max", T_ESCROW_HOLD_MAX), "payment.hold_max",
                                        positive=True),
                   seed=_int(data.get("seed", 42), "seed", 0), duration=duration,
                   demand=demand, events=events, forecasts=forecasts, reposition=reposition,
                   t_pool=_num(data.get("t_pool", T_POOL), "t_pool"),
                   patient=bool(data.get("patient", True)),
                   legacy_market=bool(data.get("legacy_market", False)),
                   brains=brains, brain_bids=brain_bids, return_policy=return_policy)


# ---- helpers -------------------------------------------------------------------------------

def _only_keys(d: dict, allowed: set, where: str) -> None:
    if not isinstance(d, dict):
        raise ScenarioError(f"{where} must be an object")
    extra = set(d) - allowed
    if extra:
        raise ScenarioError(f"{where}: unknown keys {sorted(extra)} (allowed: {sorted(allowed)})")


def _int(v, where: str, minimum: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise ScenarioError(f"{where} must be an integer >= {minimum} (got {v!r})")
    return v


def _num(v, where: str, positive: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (positive and v <= 0):
        raise ScenarioError(f"{where} must be a {'positive ' if positive else ''}number (got {v!r})")
    return float(v)


@dataclass
class _GraphInfo:
    hub_ids: List[str]
    pos: Dict[str, Optional[tuple]]             # hub id -> (x, y) of its node
    role: Dict[str, str]


def _graph_info(path: Path) -> _GraphInfo:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ScenarioError(f"Graph file not found: {path} (build it with tools/build_graph.py)") from None
    except json.JSONDecodeError as e:
        raise ScenarioError(f"{path}: invalid JSON ({e})") from None
    hubs = [h["id"] for h in data.get("hubs", [])]
    if not hubs:
        raise ScenarioError(f"{path} defines no hubs")
    xy = {n["id"]: (float(n["x"]), float(n["y"])) for n in data.get("nodes", [])}
    return _GraphInfo(hubs, {h: xy.get(h) for h in hubs},
                      {h["id"]: str(h.get("role", "")) for h in data.get("hubs", [])})


def _graph_hub_ids(path: Path) -> List[str]:
    return _graph_info(path).hub_ids


class _Selector:
    """Turns a hub selector (see the module docstring) into a list of ACTIVE hub ids."""

    def __init__(self, active: List[str], graph: _GraphInfo):
        self.active, self.graph = list(active), graph

    def resolve(self, spec, where: str) -> List[str]:
        a = self.active
        if spec == "all":
            return list(a)
        if isinstance(spec, str):
            out = [spec]
        elif isinstance(spec, list) and spec and all(isinstance(h, str) for h in spec):
            out = list(spec)
        elif isinstance(spec, dict) and len(spec) == 1:
            (kind, arg), = spec.items()
            if kind in ("centre", "periphery"):
                k = _int(arg, f"{where}.{kind}", 1)
                pts = [(h, self.graph.pos.get(h)) for h in a]
                if any(p is None for _, p in pts):
                    raise ScenarioError(f"{where}: {kind!r} needs the hub nodes to be in the graph file")
                cx = sum(p[0] for _, p in pts) / len(pts)
                cy = sum(p[1] for _, p in pts) / len(pts)
                ranked = sorted(pts, key=lambda hp: (math.hypot(hp[1][0] - cx, hp[1][1] - cy), hp[0]))
                if kind == "periphery":
                    ranked.reverse()
                out = [h for h, _ in ranked[:k]]
            elif kind == "role":
                out = [h for h in a if self.graph.role.get(h) == arg]
                if not out:
                    raise ScenarioError(f"{where}: no active hub has role {arg!r}")
            elif kind == "index":
                if not (isinstance(arg, list) and arg and all(isinstance(i, int) and not isinstance(i, bool)
                                                              and 0 <= i < len(a) for i in arg)):
                    raise ScenarioError(f"{where}.index must be positions in 0..{len(a) - 1}")
                out = [a[i] for i in arg]
            else:
                raise ScenarioError(f"{where}: unknown selector {kind!r}")
        else:
            raise ScenarioError(f"{where}: not a hub selector ({spec!r})")
        bad = [h for h in out if h not in a]
        if bad:
            raise ScenarioError(f"{where}: not active hubs: {bad}. Active: {a}")
        return out


def _prob(v, where: str) -> float:
    v = _num(v, where)
    if not 0.0 <= v <= 1.0:
        raise ScenarioError(f"{where} must be in [0, 1] (got {v})")
    return v


def _parse_demand(items, sel: _Selector) -> List[DemandSpec]:
    if not isinstance(items, list):
        raise ScenarioError("demand must be a list")
    out, seen = [], set()
    for k, d in enumerate(items):
        w = f"demand[{k}]"
        _only_keys(d, {"id", "hubs", "start", "end", "count", "pattern", "max_fare", "window",
                       "detour_tolerance", "pooling", "subsidy"}, w)
        for req in ("hubs", "start", "end", "count"):
            if req not in d:
                raise ScenarioError(f"{w}: missing {req!r}")
        spec = DemandSpec(
            id=str(d.get("id", f"demand-{k + 1}")), hubs=sel.resolve(d["hubs"], f"{w}.hubs"),
            start=_num(d["start"], f"{w}.start"), end=_num(d["end"], f"{w}.end"),
            count=_int(d["count"], f"{w}.count", 1), pattern=str(d.get("pattern", "even")),
            max_fare=_num(d.get("max_fare", 20.0), f"{w}.max_fare"),
            window=_num(d.get("window", 3600.0), f"{w}.window", positive=True),
            detour_tolerance=_num(d.get("detour_tolerance", 600.0), f"{w}.detour_tolerance"),
            pooling=_prob(d.get("pooling", 1.0), f"{w}.pooling"),
            subsidy=_prob(d.get("subsidy", 0.0), f"{w}.subsidy"))
        if spec.start < 0 or spec.end <= spec.start:
            raise ScenarioError(f"{w}: need 0 <= start < end")
        if spec.pattern not in ("even", "random"):
            raise ScenarioError(f"{w}.pattern must be 'even' or 'random'")
        if spec.id in seen:
            raise ScenarioError(f"{w}: duplicate id {spec.id!r}")
        seen.add(spec.id)
        out.append(spec)
    return out


_EVENT_KEYS = {
    "block_road": {"duration", "where", "both_directions"},
    "traffic": {"duration", "where", "severity", "profile"},
    "kill_hub": {"hub"},
}


def _parse_where(where, sel: _Selector, w: str, kind: str):
    if kind == "block_road":
        if isinstance(where, dict) and set(where) == {"edges"}:
            e = where["edges"]
            if not (isinstance(e, list) and e and all(isinstance(x, str) and "|" in x for x in e)):
                raise ScenarioError(f"{w}.where.edges must be a list of 'a|b' edge ids")
            return {"edges": list(e)}
        if isinstance(where, dict) and set(where) <= {"random_on_routes", "among"} and "random_on_routes" in where:
            k = _int(where["random_on_routes"], f"{w}.where.random_on_routes", 1)
            return {"random_on_routes": k, "among": sel.resolve(where.get("among", "all"), f"{w}.where.among")}
        raise ScenarioError(f"{w}.where: expected {{'edges': [...]}} or {{'random_on_routes': k, 'among': sel}}")
    if where == "all":
        return "all"
    if isinstance(where, dict) and set(where) == {"near", "radius"}:
        return {"near": sel.resolve(where["near"], f"{w}.where.near"),
                "radius": _num(where["radius"], f"{w}.where.radius", positive=True)}
    raise ScenarioError(f"{w}.where must be 'all' or {{'near': sel, 'radius': seconds}}")


def _parse_events(items, sel: _Selector) -> List[EventSpec]:
    if not isinstance(items, list):
        raise ScenarioError("events must be a list")
    out = []
    for k, e in enumerate(items):
        w = f"events[{k}]"
        if not isinstance(e, dict) or e.get("type") not in _EVENT_KEYS:
            raise ScenarioError(f"{w}.type must be one of {sorted(_EVENT_KEYS)}")
        kind = e["type"]
        _only_keys(e, {"at", "type", "id"} | _EVENT_KEYS[kind], w)
        if "at" not in e:
            raise ScenarioError(f"{w}: missing 'at'")
        at = _num(e["at"], f"{w}.at")
        if at < 0:
            raise ScenarioError(f"{w}.at must be >= 0")
        p: Dict[str, Any] = {}
        if kind == "kill_hub":
            hubs = sel.resolve(e.get("hub"), f"{w}.hub")
            p["hubs"] = hubs
        else:
            if "where" not in e or "duration" not in e:
                raise ScenarioError(f"{w}: 'where' and 'duration' are required")
            p["where"] = _parse_where(e["where"], sel, w, kind)
            p["duration"] = _num(e["duration"], f"{w}.duration", positive=True)
            if kind == "block_road":
                p["both_directions"] = bool(e.get("both_directions", True))
            else:
                if ("severity" in e) == ("profile" in e):
                    raise ScenarioError(f"{w}: give exactly one of 'severity' or 'profile'")
                if "severity" in e:
                    sev = _num(e["severity"], f"{w}.severity")
                    if sev < 1.0:
                        raise ScenarioError(f"{w}.severity must be >= 1 (a multiplier on travel time)")
                    p["profile"] = [(0.0, sev)]
                else:
                    prof = e["profile"]
                    if not (isinstance(prof, list) and prof and all(isinstance(x, list) and len(x) == 2 for x in prof)):
                        raise ScenarioError(f"{w}.profile must be a list of [offset, severity] pairs")
                    prof = [(_num(o, f"{w}.profile offset"), _num(v, f"{w}.profile severity")) for o, v in prof]
                    if prof[0][0] != 0 or any(b[0] <= a[0] for a, b in zip(prof, prof[1:])) \
                            or prof[-1][0] >= p["duration"] or any(v < 1.0 for _, v in prof):
                        raise ScenarioError(f"{w}.profile: offsets must start at 0, increase, stay below "
                                            f"'duration', and severities must be >= 1")
                    p["profile"] = prof
        out.append(EventSpec(at, kind, str(e.get("id", f"{kind}-{k + 1}")), p))
    ids = [e.id for e in out]
    if len(set(ids)) != len(ids):
        raise ScenarioError("events: duplicate ids")
    return out


def _parse_forecasts(items, sel: _Selector) -> List[ForecastSpec]:
    if not isinstance(items, list):
        raise ScenarioError("forecasts must be a list")
    out = []
    for k, f in enumerate(items):
        w = f"forecasts[{k}]"
        _only_keys(f, {"at", "hub", "expected", "horizon", "to"}, w)
        for req in ("at", "hub", "expected", "horizon"):
            if req not in f:
                raise ScenarioError(f"{w}: missing {req!r}")
        hub = sel.resolve(f["hub"], f"{w}.hub")
        if len(hub) != 1:
            raise ScenarioError(f"{w}.hub must select exactly one hub (got {hub})")
        out.append(ForecastSpec(at=_num(f["at"], f"{w}.at"), hub=hub[0],
                                expected=_num(f["expected"], f"{w}.expected"),
                                horizon=_num(f["horizon"], f"{w}.horizon", positive=True),
                                to=sel.resolve(f.get("to", "all"), f"{w}.to")))
    return out


_REPO_KEYS = {"demand_threshold", "requests_per_driver", "max_travel", "incentive_per_second",
              "incentive_margin", "cooldown", "min_shift_left", "max_moves", "t_rebalance", "t_reposition"}


def _parse_reposition(r) -> Dict[str, float]:
    _only_keys(r, _REPO_KEYS, "reposition")
    return {k: _num(v, f"reposition.{k}") for k, v in r.items()}
