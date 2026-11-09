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
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from common.settings import FULL_SNAPSHOT_EVERY, SUBSIDY_DISCOUNT, T_ESCROW_HOLD_MAX, T_TRAFFIC

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
    t_traffic: float = T_TRAFFIC
    full_every: int = FULL_SNAPSHOT_EVERY
    rider_balance: float = 100.0
    subsidy_discount: float = SUBSIDY_DISCOUNT
    subsidy_budget: Optional[float] = None
    escrow_hold_max: float = T_ESCROW_HOLD_MAX
    seed: int = 42
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
            specs.append(AgentSpec("microhub", hid, self.jid(hid), {"hub_id": hid}))
        width = max(2, len(str(max(self.n_drivers, 1))))
        for i in range(self.n_drivers):
            name = f"driver_{i + 1:0{width}d}"
            specs.append(AgentSpec("driver", name, self.jid(name),
                                   {"start_hub": self.driver_start_hubs[i % len(self.driver_start_hubs)]}))
        width = max(2, len(str(max(self.n_riders, 1))))
        for i in range(self.n_riders):
            name = f"rider_{i + 1:0{width}d}"
            specs.append(AgentSpec("rider", name, self.jid(name)))
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
                          "environment", "payment", "seed"}, "scenario")
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
        graph_hubs = _graph_hub_ids(graph_path)

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

        d = dict(data.get("drivers", {}))
        _only_keys(d, {"count", "start_hubs"}, "drivers")
        n_drivers = _int(d.get("count", 2), "drivers.count", 0)
        start_hubs = d.get("start_hubs") or hub_ids
        if not (isinstance(start_hubs, list) and all(h in hub_ids for h in start_hubs)):
            raise ScenarioError(f"drivers.start_hubs must be a list of ACTIVE hubs {hub_ids}")

        r = dict(data.get("riders", {}))
        _only_keys(r, {"count"}, "riders")
        n_riders = _int(r.get("count", 2), "riders.count", 0)

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

        return cls(name=str(data.get("name", default_name)), graph_path=graph_path,
                   hub_ids=hub_ids, xmpp=xmpp, speed=speed, n_drivers=n_drivers,
                   driver_start_hubs=list(start_hubs), n_riders=n_riders,
                   t_traffic=_num(e.get("t_traffic", T_TRAFFIC), "environment.t_traffic",
                                  positive=True),
                   full_every=_int(e.get("full_every", FULL_SNAPSHOT_EVERY),
                                   "environment.full_every", 1),
                   rider_balance=rider_balance, subsidy_discount=discount,
                   subsidy_budget=budget,
                   escrow_hold_max=_num(p.get("hold_max", T_ESCROW_HOLD_MAX), "payment.hold_max",
                                        positive=True),
                   seed=_int(data.get("seed", 42), "seed", 0))


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


def _graph_hub_ids(path: Path) -> List[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ScenarioError(f"Graph file not found: {path} (build it with tools/build_graph.py)") from None
    except json.JSONDecodeError as e:
        raise ScenarioError(f"{path}: invalid JSON ({e})") from None
    hubs = [h["id"] for h in data.get("hubs", [])]
    if not hubs:
        raise ScenarioError(f"{path} defines no hubs")
    return hubs
