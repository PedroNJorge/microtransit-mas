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
from common.graph import TransitNetwork
from harness.active import clear_active, write_active
from harness.ping import PingResult, format_results, ping_agents
from harness.scenario import DEFAULT_SCENARIO, AgentSpec, Scenario, ScenarioError

logger = logging.getLogger(__name__)


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Harness:
    def __init__(self, scenario: Scenario, start_server: bool = True):
        self.scenario = scenario
        self.start_server = start_server
        self.agents: Dict[str, BaseAgent] = {}          # jid -> agent, in start order
        self.network: Optional[TransitNetwork] = None
        self._server: Optional[asyncio.subprocess.Process] = None
        self._server_dir: Optional[str] = None

    # ---- XMPP server -----------------------------------------------------------------
    async def ensure_server(self, wait: float = 20.0) -> None:
        x = self.scenario.xmpp
        if port_open(x.host, x.port):
            return
        if not self.start_server:
            raise RuntimeError(f"No XMPP server on {x.host}:{x.port} (and --no-server is set)")
        logger.info("Starting XMPP server on %s:%d", x.host, x.port)
        # Accounts live in a temporary FILE database, not `--memory`: the in-memory SQLite
        # database can end up empty on another connection ("no such table: credentials") and
        # then every new agent hangs while connecting.
        self._server_dir = tempfile.mkdtemp(prefix="mas-xmpp-")
        self._server = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "spade.cli", "run", "--host", x.host,
            "--db", f"{self._server_dir}/server.db", "--purge",
            "--client_port", str(x.port), "--server_port", str(x.server_port),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
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
        if spec.role == "rider":
            kwargs.update(network=self.network, payment_jid=sc.jid("payment"),
                          reputation_jid=sc.jid("reputation"), hub_jids={x.name: x.jid for x in hubs})
        if spec.role == "reputation":
            kwargs.update(hub_jids=[x.jid for x in hubs])
        if spec.role == "driver":
            kwargs.update(network=self.network, payment_jid=sc.jid("payment"),
                          hub_jids=[s.jid for s in sc.roster() if s.role == "microhub"])
        if spec.role == "microhub":
            kwargs.update(
                network=self.network, payment_jid=sc.jid("payment"), reputation_jid=sc.jid("reputation"),
                driver_jids=[s.jid for s in sc.roster() if s.role == "driver"])
        return AGENT_CLASSES[spec.role](spec.jid, sc.xmpp.password, port=sc.xmpp.port, **kwargs)

    async def start(self) -> None:
        sc = self.scenario
        await self.ensure_server()
        self.network = TransitNetwork.from_file(str(sc.graph_path))
        clock.start(sc.speed)                           # t = 0
        try:
            for spec in sc.roster():
                agent = self.build_agent(spec)
                await agent.start(auto_register=True)
                self.agents[spec.jid] = agent
        except BaseException:
            await self.stop()
            raise
        logger.info("Scenario %r up: %d agents", sc.name, len(self.agents))

    async def stop(self) -> None:
        for jid in reversed(list(self.agents)):
            try:
                await self.agents[jid].stop()
            except Exception:                           # keep stopping the others
                logger.exception("error stopping %s", jid)
        self.agents.clear()
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


async def run(scenario: Scenario, check: bool, run_for: Optional[float], start_server: bool,
              timeout: float) -> int:
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
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:                 # Windows: fall back to KeyboardInterrupt
                pass
        print("\nRunning. Ping with:  python -m harness.ping --all     Stop with Ctrl-C.")
        try:
            await asyncio.wait_for(stop.wait(), run_for)
        except asyncio.TimeoutError:
            pass
        print("Stopping...")
        clear_active()
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
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
