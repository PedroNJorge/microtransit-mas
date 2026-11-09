"""
src/harness/ping.py
Ping agents over the HeartbeatProtocol and report who answered.

Library:  results = await ping_agents(jids, domain="localhost", port=5222)
CLI:      python -m harness.ping --all
          python -m harness.ping hub_trindade driver_01 rider_01@localhost
          python -m harness.ping microhub            # every agent of a role
Exit code 0 only if every target answered with a Pong from the expected JID.
RTT is measured in REAL milliseconds (it is a transport latency, not simulated time).
"""
import argparse
import asyncio
import sys
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional, Sequence

from spade.agent import Agent
from spade.behaviour import OneShotBehaviour

from common import clock
from common.messages import make_ping, make_template
from common.ontology import Perf, Ping, Pong, Protocol, parse_payload


@dataclass
class PingResult:
    jid: str
    ok: bool
    rtt_ms: Optional[float] = None
    role: str = ""
    error: str = ""


class _PingRound(OneShotBehaviour):
    """Send one Ping to every target, then collect replies until all arrived or time is up."""

    def __init__(self, jids: Sequence[str], timeout: float):
        super().__init__()
        self.jids = list(jids)
        self.timeout = timeout
        self.results = {}

    async def run(self):
        pending = {}                                    # ping_id -> (jid, real t0)
        for jid in self.jids:
            ping_id = f"ping-{uuid.uuid4().hex[:12]}"
            pending[ping_id] = (jid, time.monotonic())
            await self.send(make_ping(jid, Ping(ping_id=ping_id, sent_at=clock.now())))
        deadline = time.monotonic() + self.timeout
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            msg = await self.receive(timeout=remaining)
            if msg is None:
                break
            entry = pending.pop(msg.thread, None)
            if entry is None:
                continue                                # stray or duplicate reply
            jid, t0 = entry
            rtt = (time.monotonic() - t0) * 1000.0
            if msg.get_metadata("performative") == Perf.FAILURE:
                self.results[jid] = PingResult(jid, False, rtt, error=f"failure: {msg.body}")
                continue
            try:
                pong = parse_payload(msg, strict=True)
                if not isinstance(pong, Pong):
                    raise ValueError(f"expected a Pong, got {type(pong).__name__}")
            except (ValueError, TypeError) as e:
                self.results[jid] = PingResult(jid, False, rtt, error=f"bad reply: {e}")
                continue
            if pong.agent_id != jid:
                self.results[jid] = PingResult(jid, False, rtt, pong.role,
                                               f"answered as {pong.agent_id}")
            else:
                self.results[jid] = PingResult(jid, True, rtt, pong.role)
        for jid, _ in pending.values():
            self.results[jid] = PingResult(jid, False, error=f"no answer in {self.timeout:g}s")


class Pinger(Agent):
    """Throw-away agent used only to send the pings."""


async def ping_agents(jids: Sequence[str], domain: str = "localhost", port: int = 5222,
                      password: str = "dev-password", timeout: float = 5.0) -> List[PingResult]:
    jids = list(dict.fromkeys(jids))                    # de-duplicate, keep order
    if not jids:
        return []
    pinger = Pinger(f"pinger_{uuid.uuid4().hex[:8]}@{domain}", password, port=port)
    round_ = _PingRound(jids, timeout)
    await pinger.start(auto_register=True)
    try:
        pinger.add_behaviour(round_, make_template(protocol=Protocol.HEARTBEAT,
                                                   performative=None))
        await round_.join()
    finally:
        await pinger.stop()
    return [round_.results[j] for j in jids]


def format_results(results: Sequence[PingResult]) -> str:
    width = max([len(r.jid) for r in results] + [3])
    lines = [f"{'agent':<{width}}  {'role':<12} {'status':<6} {'rtt':>9}  note"]
    for r in results:
        rtt = f"{r.rtt_ms:.1f} ms" if r.rtt_ms is not None else "-"
        lines.append(f"{r.jid:<{width}}  {r.role or '-':<12} {'ok' if r.ok else 'FAIL':<6} "
                     f"{rtt:>9}  {r.error}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from harness.active import read_active
    from harness.scenario import DEFAULT_SCENARIO, Scenario, ScenarioError

    ap = argparse.ArgumentParser(prog="python -m harness.ping", description=__doc__.split("\n\n")[0])
    ap.add_argument("targets", nargs="*", help="agent name, full JID, or role")
    ap.add_argument("--all", action="store_true", help="ping every agent of the scenario")
    ap.add_argument("--scenario", default=None,
                    help="default: the scenario the launcher is running, else config/scenario.json")
    ap.add_argument("--timeout", type=float, default=5.0, help="real seconds (default 5)")
    args = ap.parse_args(argv)
    path = args.scenario or read_active() or DEFAULT_SCENARIO
    try:
        scenario = Scenario.load(path)
    except ScenarioError as e:
        print(f"scenario error: {e}", file=sys.stderr)
        return 2
    print(f"scenario: {scenario.name} ({path})", file=sys.stderr)
    if args.all:
        jids = [s.jid for s in scenario.roster()]
    elif args.targets:
        jids = []
        for t in args.targets:
            found = scenario.resolve(t)
            jids += [s.jid for s in found] if found else [t if "@" in t else scenario.jid(t)]
    else:
        ap.error("give at least one target or --all")
    results = asyncio.run(ping_agents(jids, scenario.xmpp.domain, scenario.xmpp.port,
                                      scenario.xmpp.password, args.timeout))
    print(format_results(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
