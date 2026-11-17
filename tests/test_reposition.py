"""
tests/test_reposition.py   RepositionProtocol + the hub's rebalancing heuristic.

    cd <project root>
    pytest tests/test_reposition.py -v
    pytest tests/test_reposition.py -v -k "not xmpp"      # skip the tests that need an XMPP server

Three layers
  1. plan_repositions       the pure heuristic, no agents.
  2. MicrohubAgent helpers  idle drivers, supply, forecasts, cooldown: agents are built but never started.
  3. xmpp_*                 real agents over a throw-away SPADE XMPP server (started by a fixture on free
                            ports; the tests are skipped if it cannot start). The driver is the REAL
                            DriverAgent, the hubs are REAL MicrohubAgents; only a few "ScriptedPeer"
                            drivers play misbehaving participants (silent, failing, ...).

The simulation clock runs at 100x, so a 60 s road segment takes 0.6 s of real time.
"""
import asyncio
import math
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from spade.behaviour import CyclicBehaviour, OneShotBehaviour                      # noqa: E402

from agents.base_agent import BaseAgent                                             # noqa: E402
from agents.driver import DriverAgent                                               # noqa: E402
from agents.microhub import MicrohubAgent                                           # noqa: E402
from common import clock                                                            # noqa: E402
from common.graph import build_grid_network                                         # noqa: E402
from common.messages import make_inform, make_message                               # noqa: E402
from common.ontology import (                                                       # noqa: E402
    DemandForecast, DriverState, DriverStatus, Perf, Protocol, RepositionRequest, StagingPoint,
    parse_payload,
)
from protocols.payment_common import bare                                           # noqa: E402
from protocols.reposition import (                                                  # noqa: E402
    ARRIVED, AWAIT_ARRIVAL, END, FAILED, NO_ARRIVAL, REFUSED, REQUEST, TIMEOUT, RepositionContext,
    RepositionPlan, RepositionRules, plan_repositions,
)

SPEED = 100.0
NO_HEARTBEAT = 1e6          # t_status: no periodic DriverStatus, only changes of state


@pytest.fixture(autouse=True)
def sim_clock():
    clock.reset()
    clock.start(SPEED)
    yield
    clock.reset()


# ==========================================================================================
# 1. The pure heuristic
# ==========================================================================================
# Points on a line, 60 s apart: A=0 B=1 C=2 D=5.  "H" is a home node (no staging point).
POS = {"A": 0, "B": 1, "C": 2, "D": 5, "H": 3}


def seconds(a, b):
    return abs(POS[a] - POS[b]) * 60.0


def point(node, capacity=4):
    return StagingPoint(f"sp-{node}", node, capacity, [node])


POINTS = [point(n) for n in "ABCD"]
RULES = RepositionRules(demand_threshold=3.0, requests_per_driver=5.0, max_travel=600.0,
                        incentive_per_second=0.004, incentive_margin=1.0, max_moves=10)


def plan(idle, demand, supply=None, rules=RULES, points=POINTS, tt=seconds):
    return plan_repositions(idle, points, lambda sp: demand.get(sp.location, 0.0), tt,
                            supply or {}, rules)


def test_nothing_is_hot_nothing_is_planned():
    assert plan({"d1": "A"}, {"B": 2.9}) == []
    assert plan({"d1": "A"}, {"B": 3.0}) == []            # "exceeds": equal is not enough


def test_no_idle_drivers_nothing_is_planned():
    assert plan({}, {"B": 9.0}) == []


def test_idle_driver_goes_to_the_hot_point():
    (p,) = plan({"d1": "A"}, {"B": 4.0})
    assert (p.driver_jid, p.staging_point_id, p.from_node, p.to_node) == ("d1", "sp-B", "A", "B")
    assert p.travel_time == 60.0


def test_the_closest_idle_driver_is_chosen():
    (p,) = plan({"far": "D", "near": "H", "mid": "A"}, {"C": 4.0})     # to C: D 180 s, H 60 s, A 120 s
    assert p.driver_jid == "near" and p.from_node == "H" and p.travel_time == 60.0


def test_driver_already_on_the_hot_point_is_not_moved():
    assert plan({"d1": "B"}, {"B": 9.0}) == []


def test_a_driver_is_not_taken_from_a_point_that_needs_it_as_much():
    assert plan({"d1": "A"}, {"A": 5.0, "B": 5.0}) == []            # equally hot: stay
    assert plan({"d1": "A"}, {"A": 5.0, "B": 4.0}) == []            # the hot one is where it is
    (p,) = plan({"d1": "A"}, {"A": 4.0, "B": 5.0})                  # B is hotter: go
    assert p.staging_point_id == "sp-B"


def test_a_driver_standing_on_a_plain_node_is_free_to_go():
    (p,) = plan({"d1": "H"}, {"B": 4.0})
    assert p.from_node == "H"


def test_demand_asks_for_ceil_of_demand_over_requests_per_driver():
    idle = {"a": "A", "c": "C", "d": "D"}
    assert len(plan(idle, {"B": 3.5})) == 1                  # just over the threshold: one driver
    assert len(plan(idle, {"B": 5.0})) == 1                  # ceil(5/5)
    assert len(plan(idle, {"B": 5.1})) == 2                  # ceil(5.1/5)
    assert len(plan(idle, {"B": 10.1})) == 3
    assert len(plan(idle, {"B": 40.0})) == 3                 # only 3 idle drivers exist


def test_drivers_already_there_or_on_the_way_count_as_supply():
    idle = {"a": "A", "c": "C", "d": "D"}
    assert len(plan(idle, {"B": 6.0}, supply={"sp-B": 0})) == 2
    assert len(plan(idle, {"B": 6.0}, supply={"sp-B": 1})) == 1
    assert plan(idle, {"B": 6.0}, supply={"sp-B": 2}) == []
    assert plan(idle, {"B": 6.0}, supply={"sp-B": 5}) == []


def test_capacity_caps_the_number_of_drivers():
    pts = [point("A"), point("B", capacity=2), point("C"), point("D")]
    assert len(plan({"a": "A", "c": "C", "d": "D"}, {"B": 30.0}, points=pts)) == 2
    assert len(plan({"a": "A", "c": "C", "d": "D"}, {"B": 30.0}, supply={"sp-B": 2}, points=pts)) == 0


def test_a_driver_too_far_away_is_not_moved():
    rules = replace(RULES, max_travel=200.0)
    assert plan({"d": "D"}, {"A": 4.0}, rules=rules) == []                 # 300 s > 200 s
    assert len(plan({"d": "D"}, {"A": 4.0}, rules=replace(RULES, max_travel=300.0))) == 1


def test_an_unreachable_driver_is_not_moved():
    unreachable = lambda a, b: math.inf if "D" in (a, b) else seconds(a, b)
    assert plan({"d": "D"}, {"A": 4.0}, tt=unreachable) == []
    (p,) = plan({"d": "D", "c": "C"}, {"A": 4.0}, tt=unreachable)
    assert p.driver_jid == "c"


def test_the_hottest_point_is_served_first_and_a_driver_is_used_once():
    idle = {"d1": "A", "d2": "D"}
    plans = plan(idle, {"B": 9.0, "C": 4.0})            # B wants 3 drivers, C wants 2, only 2 exist
    assert {p.driver_jid for p in plans} == {"d1", "d2"}
    assert len(plans) == 2
    assert all(p.staging_point_id == "sp-B" for p in plans)


def test_max_moves_limits_one_round():
    plans = plan({"a": "A", "c": "C", "d": "D"}, {"B": 9.0}, rules=replace(RULES, max_moves=1))
    assert len(plans) == 1


def test_incentive_covers_the_assumed_driving_cost_rounded_up_to_the_cent():
    (p,) = plan({"d": "D"}, {"C": 4.0})                                      # 180 s * 0.004 = 0.72
    assert p.incentive == pytest.approx(0.72)
    (p,) = plan({"d": "D"}, {"C": 4.0}, rules=replace(RULES, incentive_margin=1.1))   # 0.792 -> 0.80
    assert p.incentive == pytest.approx(0.80)
    (p,) = plan({"d": "A"}, {"B": 4.0}, rules=replace(RULES, incentive_per_second=0.0))
    assert p.incentive == 0.0


def test_the_heuristic_is_deterministic():
    idle = {"x": "A", "y": "C", "z": "D"}
    assert plan(idle, {"B": 6.0}) == plan(dict(reversed(list(idle.items()))), {"B": 6.0})


def test_rules_are_validated():
    with pytest.raises(ValueError):
        RepositionRules(requests_per_driver=0)
    with pytest.raises(ValueError):
        RepositionRules(max_moves=-1)


# ==========================================================================================
# 2. MicrohubAgent helpers (built, never started: no XMPP)
# ==========================================================================================
D1, D2, D3 = "d1@localhost", "d2@localhost", "d3@localhost"


def make_hub(**kw):
    kw.setdefault("hub_id", "n1_1")
    kw.setdefault("network", build_grid_network())
    kw.setdefault("driver_jids", [D1, D2, D3])
    kw.setdefault("reposition", RepositionRules())
    return MicrohubAgent("hub@localhost", "pw", **kw)


def status(jid, location="n1_1", state=DriverState.IDLE, passengers=0, shift_end=1e12):
    return DriverStatus(jid, location, state, 4, passengers, shift_end, 2.0)


def hear(hub, st, at=None):
    hub.registry[st.driver_id] = (st, clock.now() if at is None else at)


def test_hub_only_moves_idle_empty_fresh_drivers_it_owns():
    hub, now = make_hub(driver_jids=[D1, D2, D3]), 1000.0
    hear(hub, status(D1), now)                                       # ok
    hear(hub, status(D2, state=DriverState.ON_TRIP), now)            # busy
    hear(hub, status(D3, passengers=1), now)                         # not empty
    hear(hub, status("stranger@localhost"), now)                     # not this hub's driver
    assert hub.idle_drivers(now) == {D1: "n1_1"}


def test_hub_ignores_a_stale_status_and_a_driver_whose_shift_is_ending():
    hub, now = make_hub(), 1000.0
    hear(hub, status(D1), now - 3 * hub.t_status - 1)                # older than 3 heartbeats
    hear(hub, status(D2, shift_end=now + 600.0), now)                # < min_shift_left (900 s)
    hear(hub, status(D3, shift_end=now + 5000.0), now)
    assert hub.idle_drivers(now) == {D3: "n1_1"}


def test_hub_does_not_move_a_driver_it_is_already_moving_or_on_cooldown():
    hub, now = make_hub(), 1000.0
    for jid in (D1, D2, D3):
        hear(hub, status(jid), now)
    hub.repositions["t1"] = RepositionContext(RepositionPlan(D1, "sp-n4_1", "n1_1", "n4_1", 180.0, 1.0), "t1")
    hub.reposition_cooldown[D2] = now + 50.0
    assert hub.idle_drivers(now) == {D3: "n1_1"}
    assert set(hub.idle_drivers(now + 51.0)) == {D2, D3}             # cooldown over


def test_supply_counts_idle_drivers_on_the_point_and_requests_in_flight():
    hub, now = make_hub(), 1000.0
    hear(hub, status(D1, "n4_1"), now)
    hear(hub, status(D2, "n4_1", state=DriverState.ON_TRIP), now)     # not idle: does not count
    hear(hub, status(D3, "n1_4"), now)
    hub.repositions["t1"] = RepositionContext(RepositionPlan("x@localhost", "sp-n4_1", "n1_1", "n4_1", 1.0, 1.0), "t1")
    supply = hub.reposition_supply(now)
    assert supply["sp-n4_1"] == 2 and supply["sp-n1_4"] == 1 and supply["sp-n1_1"] == 0


def test_predicted_demand_sums_the_unexpired_forecasts_of_the_hubs_a_point_serves():
    hub = make_hub()
    sp = StagingPoint("sp-x", "n4_1", 4, ["n4_1", "n1_4"])
    hub.record_forecast(DemandForecast("n4_1", 0.0, 100.0, 2.0))
    hub.record_forecast(DemandForecast("n1_4", 0.0, 100.0, 1.5))
    hub.record_forecast(DemandForecast("n4_4", 0.0, 100.0, 9.0))      # not served by this point
    assert hub.predicted_demand(sp, 50.0) == pytest.approx(3.5)
    assert hub.predicted_demand(sp, 100.0) == 0.0                     # expired
    assert hub.predicted_demand(StagingPoint("sp-y", "n1_1", 4, []), 50.0) == 0.0


def test_demand_fn_overrides_the_forecasts():
    hub = make_hub(demand_fn=lambda sp, now: 7.0 if sp.id == "sp-n1_4" else 0.0)
    hub.record_forecast(DemandForecast("n4_1", 0.0, 100.0, 99.0))
    assert hub.predicted_demand(hub.network.staging_points["sp-n1_4"], 1.0) == 7.0
    assert hub.predicted_demand(hub.network.staging_points["sp-n4_1"], 1.0) == 0.0


def test_forecasts_keep_the_newest_and_drop_nonsense():
    hub = make_hub()
    assert hub.record_forecast(DemandForecast("n4_1", 10.0, 100.0, 5.0))
    assert not hub.record_forecast(DemandForecast("n4_1", 0.0, 100.0, 9.0))      # older horizon
    assert not hub.record_forecast(DemandForecast("n4_1", 20.0, 100.0, -1.0))    # negative
    assert hub.demand_forecasts["n4_1"].expected_requests == 5.0
    assert hub.record_forecast(DemandForecast("n4_1", 20.0, 100.0, 6.0))
    assert hub.demand_forecasts["n4_1"].expected_requests == 6.0


def test_hub_plans_from_its_own_view_of_the_world():
    hub, now = make_hub(), 1000.0
    hear(hub, status(D1, "n1_1"), now)
    hub.record_forecast(DemandForecast("n4_1", 0.0, 1e9, 6.0))
    (p,) = hub.plan_repositions(now)                                  # wants 2, owns 1 idle driver
    assert (p.driver_jid, p.staging_point_id, p.to_node) == (D1, "sp-n4_1", "n4_1")
    assert p.travel_time == pytest.approx(180.0)
    assert p.incentive >= 0.004 * 180.0


def test_hub_without_rules_or_network_never_plans():
    now = 1000.0
    for hub in (make_hub(reposition=None), make_hub(network=None)):
        hear(hub, status(D1), now)
        hub.record_forecast(DemandForecast("n4_1", 0.0, 1e9, 6.0))
        assert hub.plan_repositions(now) == []


def test_hub_survives_a_driver_on_an_unknown_node():
    hub, now = make_hub(), 1000.0
    hear(hub, status(D1, "nowhere"), now)
    hub.record_forecast(DemandForecast("n4_1", 0.0, 1e9, 6.0))
    assert hub.plan_repositions(now) == []


@pytest.mark.parametrize("outcome,cooled", [(ARRIVED, False), (REFUSED, True), (TIMEOUT, True),
                                            (FAILED, True), (NO_ARRIVAL, True)])
def test_only_a_failed_reposition_puts_the_driver_on_cooldown(outcome, cooled):
    hub = make_hub()
    ctx = RepositionContext(RepositionPlan(D1, "sp-n4_1", "n1_1", "n4_1", 180.0, 1.0), "t1")
    ctx.outcome = outcome
    hub.repositions["t1"] = ctx
    hub.on_reposition_finished(ctx)
    assert hub.repositions == {} and hub.reposition_log == [ctx]
    assert (D1 in hub.reposition_cooldown) is cooled


# ==========================================================================================
# 3. Real agents over XMPP
# ==========================================================================================
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=0.3):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def xmpp_port(tmp_path_factory):
    """A throw-away SPADE XMPP server on free ports (the same command the harness launcher uses)."""
    port, server_port = _free_port(), _free_port()
    db = tmp_path_factory.mktemp("xmpp") / "server.db"
    proc = subprocess.Popen(
        [sys.executable, "-m", "spade.cli", "run", "--host", "localhost", "--db", str(db), "--purge",
         "--client_port", str(port), "--server_port", str(server_port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 30
        while not _port_open(port):
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.skip("could not start a local XMPP server (python -m spade.cli run)")
            time.sleep(0.2)
        yield port
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        proc.wait(timeout=10)


class World:
    """Starts agents and stops them again, whatever happens."""

    def __init__(self, port: int):
        self.port, self.agents, self._n = port, [], uuid.uuid4().hex[:6]

    def jid(self, name: str) -> str:
        return f"{name}_{self._n}@localhost"

    async def start(self, *agents):
        for a in agents:
            await a.start(auto_register=True)
            self.agents.append(a)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        for a in reversed(self.agents):
            try:
                await a.stop()
            except Exception:                                     # pragma: no cover
                pass


async def wait_until(predicate, what: str, timeout: float = 20.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for: {what}")
        await asyncio.sleep(0.05)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 90))


def echo(req):
    return req


def elsewhere(req):
    return replace(req, staging_point_id="sp-n1_4")


class _Scripted(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        peer = self.agent
        req = parse_payload(msg)
        peer.requests.append(req)
        for perf, payload in peer.script:
            body = payload(req) if callable(payload) else payload
            await self.send(make_message(bare(msg.sender), perf, Protocol.REPOSITION, body, msg.thread))


class _Post(OneShotBehaviour):
    def __init__(self, msg):
        super().__init__()
        self.msg = msg

    async def run(self):
        await self.send(self.msg)


class ScriptedPeer(BaseAgent):
    """Plays a driver by script: for every RepositionRequest it sends the steps [(performative,
    payload-or-function-of-the-request), ...] in order. An empty script is a silent driver.
    `post(message)` lets the test send an arbitrary message from this agent."""
    role = "driver"

    def __init__(self, jid, password, script=(), **kw):
        super().__init__(jid, password, **kw)
        self.script, self.requests = list(script), []

    async def setup(self):
        await super().setup()
        self.add_behaviour(_Scripted(), make_template_reposition())

    def post(self, msg):
        self.add_behaviour(_Post(msg))


def make_template_reposition():
    from common.messages import make_template
    return make_template(Protocol.REPOSITION, Perf.REQUEST)


def trace_events(ctx):
    return [(e["state"], e["event"]) for e in ctx.trace]


def test_xmpp_idle_driver_is_repositioned_and_every_hub_hears_about_it(xmpp_port):
    """Forecast -> heuristic -> request -> agree -> drive -> arrive -> inform + broadcast.

    The heartbeat is switched off (t_status = 1e6 sim s), so the ONLY way a hub can learn that the driver
    left and arrived is the DriverStatus the driver broadcasts on each change of state."""
    async def scenario():
        net = build_grid_network()
        async with World(xmpp_port) as w:
            jd, ja, jb, jf = w.jid("driver"), w.jid("hub_a"), w.jid("hub_b"), w.jid("forecaster")
            driver = DriverAgent(jd, "pw", port=xmpp_port, start_hub="n1_1", network=net, hub_jids=[ja, jb],
                                 t_status=NO_HEARTBEAT)
            hub_a = MicrohubAgent(ja, "pw", port=xmpp_port, hub_id="n1_1", network=net, driver_jids=[jd],
                                  reposition=RepositionRules(), t_rebalance=5.0, t_reposition=300.0,
                                  t_status=NO_HEARTBEAT)
            hub_b = MicrohubAgent(jb, "pw", port=xmpp_port, hub_id="n4_1", network=net,   # just listens
                                  t_status=NO_HEARTBEAT)
            forecaster = ScriptedPeer(jf, "pw", port=xmpp_port)
            await w.start(hub_a, hub_b, driver, forecaster)

            # the forecast reaches the hub as a message (DemandForecastProtocol), not by poking its fields
            forecaster.post(make_inform(ja, Protocol.DEMAND_FORECAST, DemandForecast("n4_1", 0.0, 1e9, 6.0)))
            await wait_until(lambda: hub_a.reposition_log, "the reposition to finish")

            ctx = hub_a.reposition_log[0]
            assert ctx.outcome == ARRIVED, ctx.trace
            assert (ctx.plan.driver_jid, ctx.plan.staging_point_id) == (jd, "sp-n4_1")
            assert ctx.request.hub_id == "n1_1" and ctx.request.driver_id == jd
            assert ctx.request.incentive >= 0.004 * 180.0            # covers the driver's cost
            assert [s for s, _ in trace_events(ctx)][0] == REQUEST and trace_events(ctx)[-1][0] == END
            assert ("AWAIT_REPLY", "agree") in trace_events(ctx)

            # the driver really moved, and is idle again at the new node
            assert driver.location == "n4_1" and driver.state == DriverState.IDLE
            states = [s.state for s in driver.status_log]
            assert DriverState.REPOSITIONING in states and states[-1] == DriverState.IDLE

            # the broadcast: BOTH hubs know where it is now, including the one that did not ask
            for hub in (hub_a, hub_b):
                await wait_until(lambda hub=hub: jd in hub.registry
                                 and hub.registry[jd][0].location == "n4_1"
                                 and hub.registry[jd][0].state == DriverState.IDLE,
                                 f"{hub.hub_id} to learn the driver arrived")

            # and the hub is content: no second request, nothing left in flight
            await asyncio.sleep(0.6)
            assert len(hub_a.reposition_log) == 1 and not hub_a.repositions
    run(scenario())


def test_xmpp_driver_refuses_an_incentive_below_its_cost_and_is_left_alone(xmpp_port):
    async def scenario():
        net = build_grid_network()
        async with World(xmpp_port) as w:
            jd, ja = w.jid("driver"), w.jid("hub_a")
            driver = DriverAgent(jd, "pw", port=xmpp_port, start_hub="n1_1", network=net, hub_jids=[ja])
            hub = MicrohubAgent(ja, "pw", port=xmpp_port, hub_id="n1_1", network=net, driver_jids=[jd],
                                reposition=RepositionRules(incentive_per_second=0.0, cooldown=1e9),
                                t_rebalance=5.0, t_reposition=300.0)
            await w.start(hub, driver)
            hub.record_forecast(DemandForecast("n4_1", 0.0, 1e9, 6.0))
            await wait_until(lambda: hub.reposition_log, "the refusal")

            ctx = hub.reposition_log[0]
            assert (ctx.outcome, ctx.reason) == (REFUSED, "incentive_too_low")
            assert driver.location == "n1_1" and driver.state == DriverState.IDLE
            assert jd in hub.reposition_cooldown                       # asked once, now left alone
            await asyncio.sleep(0.8)                                   # many rebalancing rounds
            assert len(hub.reposition_log) == 1 and not hub.repositions
    run(scenario())


def test_xmpp_a_driver_already_moving_for_one_hub_refuses_another_as_busy(xmpp_port):
    """Two hubs want the same driver; the driver itself is the arbiter (no coordinator needed)."""
    async def scenario():
        net = build_grid_network()
        async with World(xmpp_port) as w:
            jd, ja, jb = w.jid("driver"), w.jid("hub_a"), w.jid("hub_b")
            driver = DriverAgent(jd, "pw", port=xmpp_port, start_hub="n1_1", network=net, hub_jids=[ja, jb])
            hub_a = MicrohubAgent(ja, "pw", port=xmpp_port, hub_id="n1_1", network=net, driver_jids=[jd],
                                  reposition=RepositionRules(), t_rebalance=5.0, t_reposition=300.0)
            hub_b = MicrohubAgent(jb, "pw", port=xmpp_port, hub_id="n4_4", network=net, t_reposition=300.0)
            await w.start(hub_a, hub_b, driver)
            hub_a.record_forecast(DemandForecast("n4_1", 0.0, 1e9, 6.0))
            await wait_until(lambda: driver.activity == "repositioning", "the driver to set off")

            ctx_b = hub_b.start_reposition(RepositionPlan(jd, "sp-n4_4", "n1_1", "n4_4", 300.0, 5.0))
            await wait_until(lambda: hub_b.reposition_log, "hub_b's answer")
            assert (ctx_b.outcome, ctx_b.reason) == (REFUSED, "busy")
            assert jd in hub_b.reposition_cooldown

            await wait_until(lambda: hub_a.reposition_log, "hub_a's reposition to finish")
            assert hub_a.reposition_log[0].outcome == ARRIVED and driver.location == "n4_1"
    run(scenario())


SCRIPTS = {
    "silent": ([], TIMEOUT, ""),
    "refuses": ([(Perf.REFUSE, "unreachable")], REFUSED, "unreachable"),
    "agrees_then_fails": ([(Perf.AGREE, echo), (Perf.FAILURE, "unreachable")], FAILED, "unreachable"),
    "agrees_then_vanishes": ([(Perf.AGREE, echo)], NO_ARRIVAL, ""),
    "arrives_somewhere_else": ([(Perf.AGREE, echo), (Perf.INFORM, elsewhere)], NO_ARRIVAL, ""),
    "protocol_failure_at_once": ([(Perf.FAILURE, "malformed")], FAILED, "malformed"),
    "agrees_and_arrives": ([(Perf.AGREE, echo), (Perf.INFORM, echo)], ARRIVED, ""),
}


@pytest.mark.parametrize("name", list(SCRIPTS))
def test_xmpp_hub_handles_every_way_a_driver_can_answer(xmpp_port, name):
    script, outcome, reason = SCRIPTS[name]

    async def scenario():
        async with World(xmpp_port) as w:
            jp, jh = w.jid("peer"), w.jid("hub")
            peer = ScriptedPeer(jp, "pw", script=script, port=xmpp_port)
            hub = MicrohubAgent(jh, "pw", port=xmpp_port, hub_id="n1_1", network=build_grid_network(),
                                t_reposition=30.0)               # 0.3 s real at 100x
            await w.start(hub, peer)
            ctx = hub.start_reposition(RepositionPlan(jp, "sp-n4_1", "n1_1", "n4_1", 60.0, 1.0))
            assert hub.repositions == {ctx.thread: ctx}           # in flight until it finishes
            await wait_until(lambda: hub.reposition_log, f"the hub to settle ({name})")

            assert (ctx.outcome, ctx.reason) == (outcome, reason), ctx.trace
            assert not hub.repositions and hub.reposition_log == [ctx]
            assert len(peer.requests) == 1
            assert peer.requests[0] == RepositionRequest("n1_1", jp, "sp-n4_1", 1.0)
            assert (jp in hub.reposition_cooldown) is (outcome != ARRIVED)
            assert ctx.finished_at >= ctx.started_at > 0
            if outcome in (FAILED, NO_ARRIVAL, ARRIVED) and name != "protocol_failure_at_once":
                assert any(s == AWAIT_ARRIVAL for s, _ in trace_events(ctx))
    run(scenario())


def test_xmpp_a_stranger_cannot_report_an_arrival(xmpp_port):
    """Only the driver that was asked may answer; a message from anyone else on the thread is ignored."""
    async def scenario():
        async with World(xmpp_port) as w:
            jp, js, jh = w.jid("peer"), w.jid("stranger"), w.jid("hub")
            peer = ScriptedPeer(jp, "pw", script=[(Perf.AGREE, echo)], port=xmpp_port)   # then vanishes
            stranger = ScriptedPeer(js, "pw", port=xmpp_port)
            hub = MicrohubAgent(jh, "pw", port=xmpp_port, hub_id="n1_1", network=build_grid_network(),
                                t_reposition=30.0)
            await w.start(hub, peer, stranger)
            ctx = hub.start_reposition(RepositionPlan(jp, "sp-n4_1", "n1_1", "n4_1", 60.0, 1.0))
            await wait_until(lambda: ctx.agreed_at > 0, "the driver to agree")
            stranger.post(make_message(jh, Perf.INFORM, Protocol.REPOSITION,
                                       RepositionRequest("n1_1", js, "sp-n4_1", 1.0), ctx.thread))
            await wait_until(lambda: hub.reposition_log, "the hub to give up")
            assert ctx.outcome == NO_ARRIVAL
            assert any(e == "ignored: not the driver" for _, e in trace_events(ctx))
    run(scenario())


def test_xmpp_forecasts_from_unlisted_senders_and_nonsense_are_ignored(xmpp_port):
    async def scenario():
        async with World(xmpp_port) as w:
            jf, js, jh, jo = w.jid("forecaster"), w.jid("stranger"), w.jid("hub"), w.jid("open_hub")
            forecaster, stranger = ScriptedPeer(jf, "pw", port=xmpp_port), ScriptedPeer(js, "pw", port=xmpp_port)
            hub = MicrohubAgent(jh, "pw", port=xmpp_port, hub_id="n1_1", network=build_grid_network(),
                                reposition=RepositionRules(), forecast_jids=[jf])
            open_hub = MicrohubAgent(jo, "pw", port=xmpp_port, hub_id="n1_4", network=build_grid_network(),
                                     reposition=RepositionRules())            # accepts anyone
            await w.start(hub, open_hub, forecaster, stranger)

            fc = lambda h, start, n: make_inform(jh, Protocol.DEMAND_FORECAST, DemandForecast(h, start, 1e9, n))
            stranger.post(fc("n1_4", 0.0, 8.0))                               # not on the allow-list
            await asyncio.sleep(0.4)
            forecaster.post(fc("n4_1", 10.0, 5.0))                            # accepted
            forecaster.post(fc("n4_1", 0.0, 9.0))                             # older horizon: ignored
            forecaster.post(fc("n4_1", 20.0, -1.0))                           # negative: ignored
            forecaster.post(make_inform(jh, Protocol.DEMAND_FORECAST, DemandForecast("n4_4", 0.0, 1e9, 7.0)))
            await wait_until(lambda: "n4_4" in hub.demand_forecasts, "the marker forecast")
            assert hub.demand_forecasts["n4_1"].expected_requests == 5.0
            assert "n1_4" not in hub.demand_forecasts                         # only the stranger said so

            stranger.post(make_inform(jo, Protocol.DEMAND_FORECAST, DemandForecast("n4_1", 0.0, 1e9, 8.0)))
            await wait_until(lambda: "n4_1" in open_hub.demand_forecasts, "the open hub to take any sender")
    run(scenario())