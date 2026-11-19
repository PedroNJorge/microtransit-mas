"""
src/harness/launcher.py
Start every agent of a scenario, keep them running, stop them cleanly.

    python -m harness.launcher                         # config/scenario.json, run until Ctrl-C
    python -m harness.launcher --scenario config/scenarios/full_8hubs.json
    python -m harness.launcher --check                 # start, ping everyone, print table, stop
    python -m harness.launcher --run-for 30            # stop by itself after 30 real seconds
    python -m harness.launcher --no-server             # use an XMPP server you started yourself

XMPP server: if nothing listens on xmpp.port the launcher starts SPADE's server with a temporary
database file (`python -m spade.cli run --db ...`) and stops it on exit. Pass --no-server to disable that.

Start order: payment, regulator, reputation, hubs, drivers, riders, environment (last, so its
subscribers exist when the first TrafficUpdate goes out). Stop order is the reverse.
The simulation clock is started once, right before the first agent (t = 0).
"""
import argparse
import asyncio
import logging
import signal
import shutil
import socket
import sys
import tempfile
from typing import Dict, List, Optional, Sequence

from agents.base_agent import BaseAgent
from agents.registry import AGENT_CLASSES
from common import clock
from common.settings import MAX_PICKUP_ETA, RESERVE_MARGIN, RETRY_DELAY, WINDOW_SLACK
from common.graph import TransitNetwork
from common.ws_server import start_ws_server
from harness.active import clear_active, write_active
from harness.ping import PingResult, format_results, ping_agents
from harness.live_metrics import MetricsCollector
from harness.runner import ScenarioRunner, report
from protocols.reposition import RepositionRules
from harness.scenario import DEFAULT_SCENARIO, AgentSpec, Scenario, ScenarioError

logger = logging.getLogger(__name__)


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Harness:
    def __init__(self, scenario: Scenario, start_server: bool = True, viewer: bool = True):
        self.scenario = scenario
        self.start_server = start_server
        self.viewer = viewer                            # False (experiments): no WebSocket server for the map
        self.agents: Dict[str, BaseAgent] = {}          # jid -> agent, in start order
        self.network: Optional[TransitNetwork] = None
        self._server: Optional[asyncio.subprocess.Process] = None
        self._server_dir: Optional[str] = None
        self._ws_server = None
        self.metrics: Optional[MetricsCollector] = None  # live metrics for the viewer (harness/live_metrics.py)
        self.runner: Optional[ScenarioRunner] = None    # plays the scenario's demand / events / forecasts

    # ---- XMPP server -----------------------------------------------------------------
    async def ensure_server(self, wait: float = 20.0) -> None:
        x = self.scenario.xmpp
        if port_open(x.host, x.port):
            logger.warning("An XMPP server is already listening on %s:%d: reusing it (this run did not start it). If an "
                           "earlier run was killed, its server may still be alive and stuck: stop it with "
                           "`pkill -f 'spade.cli run'` and start again.", x.host, x.port)
            return
        if not self.start_server:
            raise RuntimeError(f"No XMPP server on {x.host}:{x.port} (and --no-server is set)")
        logger.info("Starting XMPP server on %s:%d", x.host, x.port)
        # Accounts live in a temporary FILE database, not `--memory`: the in-memory SQLite
        # database can end up empty on another connection ("no such table: credentials") and
        # then every new agent hangs while connecting.
        self._server_dir = tempfile.mkdtemp(prefix="mas-xmpp-")
        self._server_log = f"{self._server_dir}/server.log"
        logger.info("XMPP server output goes to %s", self._server_log)
        self._server = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "spade.cli", "run", "--host", x.host,
            "--db", f"{self._server_dir}/server.db", "--purge",
            "--client_port", str(x.port), "--server_port", str(x.server_port),
            stdout=open(self._server_log, "ab"), stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while loop.time() < deadline:
            if port_open(x.host, x.port):
                return
            if self._server.returncode is not None:
                break
            await asyncio.sleep(0.1)
        await self._stop_server()
        raise RuntimeError(f"XMPP server did not come up on {x.host}:{x.port}")

    async def _stop_server(self) -> None:
        if self._server is not None and self._server.returncode is None:
            self._server.terminate()
            try:
                await asyncio.wait_for(self._server.wait(), 10)
            except asyncio.TimeoutError:
                self._server.kill()
                await self._server.wait()
        self._server = None
        if self._server_dir:
            shutil.rmtree(self._server_dir, ignore_errors=True)
            self._server_dir = None

    # ---- agents ---------------------------------------------------------------------
    def build_agent(self, spec: AgentSpec) -> BaseAgent:
        sc = self.scenario
        kwargs = dict(spec.kwargs)
        if spec.role == "environment":
            kwargs.update(
                network=self.network,
                subscribers=[s.jid for s in sc.roster() if s.role in ("microhub", "driver")],
                t_traffic=sc.t_traffic, full_every=sc.full_every)
        hubs = [x for x in sc.roster() if x.role == "microhub"]
        drivers = [x for x in sc.roster() if x.role == "driver"]
        if spec.role == "regulator":
            kwargs.update(hub_jids=[x.jid for x in hubs], driver_jids=[x.jid for x in drivers],
                          payment_jid=sc.jid("payment"))
        if spec.role == "payment":
            kwargs.update(reputation_jid=sc.jid("reputation"), regulator_jid=sc.jid("regulator"))
        if spec.role == "rider":
            kwargs.update(network=self.network, payment_jid=sc.jid("payment"),
                          reputation_jid=sc.jid("reputation"), hub_jids={x.name: x.jid for x in hubs})
            kwargs["detour_ratio"] = sc.detour_ratio
            if sc.patient:
                kwargs["retry_coverage"] = 6 * 3600.0     # a blocked road is no reason to give up
                kwargs["match_patience"] = 1e9           # waits for a driver as long as it takes (a dead hub still times out)
        if spec.role == "reputation":
            kwargs.update(hub_jids=[x.jid for x in hubs], payment_jid=sc.jid("payment"))
        if spec.role == "driver":
            kwargs.update(network=self.network, payment_jid=sc.jid("payment"),
                          regulator_jid=sc.jid("regulator"), reputation_jid=sc.jid("reputation"),
                          hub_jids=[s.jid for s in sc.roster() if s.role == "microhub"])
        if spec.role == "microhub" and sc.reposition is not None:     # "reposition": {...} switches the heuristic on
            opts = dict(sc.reposition)
            hub_opts = {k: opts.pop(k) for k in ("t_rebalance", "t_reposition") if k in opts}
            if "max_moves" in opts:
                opts["max_moves"] = int(opts["max_moves"])
            kwargs.update(reposition=RepositionRules(**opts), **hub_opts)
        if spec.role == "microhub" and sc.inter_hub is not None and len(hubs) > 1:
            opts = {k: (int(v) if k in ("overflow_after", "k_neighbours") else v) for k, v in sc.inter_hub.items()}
            kwargs.update(peer_jids={x.name: x.jid for x in hubs if x.jid != spec.jid}, **opts)
        if spec.role == "microhub":
            kwargs.update(
                t_pool=sc.t_pool, pool_incentive_per_kg=sc.pool_incentive_per_kg, late_join=sc.late_join, **market_settings(sc),
                network=self.network, payment_jid=sc.jid("payment"), reputation_jid=sc.jid("reputation"),
                regulator_jid=sc.jid("regulator"), driver_jids=[s.jid for s in sc.roster() if s.role == "driver"])
        if spec.role not in AGENT_CLASSES:
            raise RuntimeError(f"agent role {spec.role!r} is not available (the BDI brain needs `uv add spade_bdi`)")
        return AGENT_CLASSES[spec.role](spec.jid, sc.xmpp.password, port=sc.xmpp.port, **kwargs)

    async def start(self) -> None:
        sc = self.scenario
        await self.ensure_server()
        self.network = TransitNetwork.from_file(str(sc.graph_path))
        if self.viewer:
            self._ws_server = await start_ws_server()   # <-- Liga o WebSocket na porta 8765
        clock.start(sc.speed)
        try:
            for spec in sc.roster():
                agent = self.build_agent(spec)
                await agent.start(auto_register=True)
                self.agents[spec.jid] = agent
        except BaseException:
            await self.stop()
            raise
        if sc.demand or sc.events or sc.forecasts:
            self.runner = ScenarioRunner(self)
            self.runner.start()
        self.metrics = MetricsCollector(self, broadcast=self.viewer)
        self.metrics.start()
        self._health = asyncio.create_task(self._watch_health())
        logger.info("Scenario %r up: %d agents", sc.name, len(self.agents))

    async def _watch_health(self, period: float = 5.0, every: float = 30.0) -> None:
        """Tells a frozen run apart from a quiet one: warns when the event loop was blocked (a CPU-bound step starved every
        agent) and logs a heartbeat with the simulation time every `every` real seconds. If the heartbeat stops, the whole
        process is blocked; if it continues with the sim time moving but nothing happens, the problem is in the agents or in
        the XMPP server (see its log)."""
        loop = asyncio.get_running_loop()
        last_beat = loop.time()
        while True:
            t0 = loop.time()
            await asyncio.sleep(period)
            lag = loop.time() - t0 - period
            if lag > 1.0:
                logger.warning("event loop was blocked for %.1f s (sim time %.0f s): the process is too loaded or a step "
                               "is CPU-bound", lag, clock.now())
            if loop.time() - last_beat >= every:
                last_beat = loop.time()
                riders = Counter(str(getattr(a, "state", "?")).split(".")[-1] for a in self.agents.values()
                                 if a.role == "rider" and getattr(a, "trip", None) is not None)
                drivers = Counter(str(getattr(a, "state", "?")).split(".")[-1] for a in self.agents.values() if a.role == "driver")
                logger.info("heartbeat: sim time %.0f s, %d agents, loop lag %.2f s | riders with a trip: %s | drivers: %s",
                            clock.now(), len(self.agents), max(lag, 0.0),
                            dict(riders) or "none", dict(drivers))

    async def stop(self) -> None:
        health = getattr(self, "_health", None)
        if health is not None:
            health.cancel()
            self._health = None
        if self.metrics is not None:
            await self.metrics.stop()
        if self.runner is not None:
            await self.runner.stop()
        for jid in reversed(list(self.agents)):
            try:
                await self.agents[jid].stop()
            except Exception:
                logger.exception("error stopping %s", jid)
        self.agents.clear()
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None
        await self._stop_server()

    async def ping_all(self, timeout: float = 5.0) -> List[PingResult]:
        x = self.scenario.xmpp
        return await ping_agents(list(self.agents), x.domain, x.port, x.password, timeout)

    async def __aenter__(self) -> "Harness":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()


def roster_table(scenario: Scenario) -> str:
    rows = scenario.roster()
    w = max(len(s.jid) for s in rows)
    return "\n".join(f"  {s.role:<12} {s.jid:<{w}}  "
                     + " ".join(f"{k}={v}" for k, v in s.kwargs.items()) for s in rows)


def market_settings(sc: Scenario) -> dict:
    """Hub settings of the demo's 'market fixes': without them a hub never retries a failed auction, calls
    drivers of any distance and prices against the riders' cap. The windows are in sim seconds, so at clock
    x60 they are short in real time: 30 sim s would be 0.5 real s, too little for XMPP latency. The escrow
    window is generous because every hub waits on the one Payment agent; a slow machine hit it and dissolved
    pools with reason TIMEOUT."""
    if sc.legacy_market:
        return {}
    return dict(reserve_margin=RESERVE_MARGIN, max_pickup_eta=MAX_PICKUP_ETA, retry_delay=RETRY_DELAY,
                window_slack=WINDOW_SLACK, relax_expired_windows=sc.patient, max_attempts=1000 if sc.patient else 4,
                t_auction=60.0, t_award=40.0, t_escrow=300.0, t_pool_response=300.0)


class _DeadHubNoise(logging.Filter):
    """Hides spade's 'No behaviour matched' warning for messages addressed to a hub the scenario killed: drivers keep
    sending it their status, which is expected (the neighbours have taken over) and only buries the report."""

    def __init__(self, harness):
        super().__init__()
        self.h = harness

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        if not text.startswith("No behaviour matched"):
            return True
        killed = getattr(self.h.runner, "killed", ()) if self.h.runner is not None else ()
        return not any(f'to="{hub}@' in text for hub in killed)


async def _until_done(h, stop: asyncio.Event) -> None:
    """Wait for Ctrl-C or, when the scenario issues demand, until it has all been issued and every rider is
    home (no trip in progress). The caller's timeout (the scenario's duration) is the safety cap."""
    while not stop.is_set():
        r = h.runner
        if r is not None and r.sc.demand and r.finished():
            if all(a.trip is None for a in h.agents.values() if a.role == "rider"):
                print("Every rider is home.")
                return
        try:
            await asyncio.wait_for(stop.wait(), 1.0)
        except asyncio.TimeoutError:
            pass


async def run(scenario: Scenario, check: bool, run_for: Optional[float], start_server: bool,
              timeout: float) -> int:
    summary = None
    async with Harness(scenario, start_server=start_server) as h:
        if scenario.source is not None and not check:
            write_active(scenario.source)               # lets `harness.ping` find this scenario
        print(f"Scenario {scenario.name!r}: {len(h.agents)} agents registered "
              f"(clock x{scenario.speed:g})")
        print(roster_table(scenario))
        if check:
            results = await h.ping_all(timeout)
            print()
            print(format_results(results))
            return 0 if all(r.ok for r in results) else 1
        stop = asyncio.Event()
        logging.getLogger("spade.Agent").addFilter(_DeadHubNoise(h))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:                 # Windows: fall back to KeyboardInterrupt
                pass
        print("\nRunning. Ping with:  python -m harness.ping --all     Stop with Ctrl-C.")
        if run_for is None and scenario.duration:         # the scenario says how long it lasts (sim seconds)
            run_for = clock.to_real(scenario.duration)
        try:
            await asyncio.wait_for(_until_done(h, stop), run_for)
        except asyncio.TimeoutError:
            pass
        print("Stopping...")
        if h.runner is not None:
            summary = report(h)                         # computed now, printed after the agents have stopped
        # agents stopping one by one still get each other's status messages: harmless, but very noisy
        logging.getLogger("spade.Agent").setLevel(logging.ERROR)
        clear_active()
    if summary:
        print(summary)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness.launcher", description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenario", default=str(DEFAULT_SCENARIO))
    ap.add_argument("--check", action="store_true", help="start, ping every agent, stop")
    ap.add_argument("--run-for", type=float, default=None, help="stop after N real seconds")
    ap.add_argument("--no-server", action="store_true", help="never start an XMPP server")
    ap.add_argument("--timeout", type=float, default=10.0, help="ping timeout, real seconds")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)     # `| tee scenario.log` makes stdout block-buffered: the terminal
    except (AttributeError, ValueError):                # and the log would show an old state of a running scenario
        pass
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.setLevel(logging.INFO)                       # the launcher's own lines: server log path, heartbeat, "scenario up"
    try:
        scenario = Scenario.load(args.scenario)
    except ScenarioError as e:
        print(f"scenario error: {e}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(scenario, args.check, args.run_for,
                               not args.no_server, args.timeout))
    except KeyboardInterrupt:
        clear_active()
        return 0
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        clear_active()


if __name__ == "__main__":
    sys.exit(main())
