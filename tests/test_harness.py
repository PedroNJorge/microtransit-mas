"""Milestone 3 done-when: the launcher registers every agent and a ping reaches any of them."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness.launcher import Harness, port_open
from harness.ping import ping_agents
from harness.scenario import PROJECT_ROOT, Scenario

pytestmark = pytest.mark.integration

SRC = str(PROJECT_ROOT / "src")


@pytest.fixture(autouse=True)
def isolated_run_dir(tmp_path, monkeypatch):
    """Keep the active-scenario marker away from a launcher you may have running yourself."""
    monkeypatch.setenv("HARNESS_RUN_DIR", str(tmp_path / "run"))


def scenario_for(port, server_port, **over):
    d = {"name": "test", "graph": "config/porto_graph.json",
         "xmpp": {"port": port, "server_port": server_port},
         "clock": {"speed": 200},
         "hubs": ["hub_trindade", "hub_campanha"],
         "drivers": {"count": 2}, "riders": {"count": 2},
         "environment": {"t_traffic": 60, "full_every": 3}}
    d.update(over)
    return Scenario.from_dict(d)


@pytest.mark.asyncio
async def test_harness_starts_everyone_and_all_answer_pings(xmpp_server):
    sc = scenario_for(xmpp_server, 5271)
    async with Harness(sc, start_server=False) as h:
        assert list(h.agents) == [s.jid for s in sc.roster()]
        assert all(a.is_alive() for a in h.agents.values())
        results = await h.ping_all(timeout=10)
        assert len(results) == len(sc.roster()) and all(r.ok for r in results), results
        by_jid = {r.jid: r.role for r in results}
        assert by_jid["environment@localhost"] == "environment"
        assert by_jid["hub_trindade@localhost"] == "microhub"
        assert h.agents["driver_01@localhost"].start_hub == "hub_trindade"
        assert h.agents["hub_campanha@localhost"].hub_id == "hub_campanha"
        pay = h.agents["payment@localhost"]                           # configured from the scenario
        assert pay.ledger.default_rider_balance == 100.0 and pay.ledger.subsidy_discount == 0.5
        assert pay.ledger.unlimited_subsidy and pay.hold_max == 7200.0
    assert not h.agents                                               # stopped and cleared


@pytest.mark.asyncio
async def test_environment_feeds_hubs_and_drivers_but_not_riders(xmpp_server):
    sc = scenario_for(xmpp_server, 5271)
    async with Harness(sc, start_server=False) as h:
        env = h.agents["environment@localhost"]
        assert sorted(env.subscribers) == sorted(
            s.jid for s in sc.roster() if s.role in ("microhub", "driver"))
        await asyncio.sleep(1.5)                                      # t_traffic 60 s = 0.3 s real
        for jid in env.subscribers:
            a = h.agents[jid]
            assert a.traffic_updates_received >= 2, jid
            assert a.edge_weights == env.network.snapshot("x", 0).edge_weights
        assert h.agents["rider_01@localhost"].traffic_updates_received == 0


@pytest.mark.asyncio
async def test_no_server_flag_raises_when_nothing_listens():
    sc = scenario_for(5299, 5298)
    assert not port_open("localhost", 5299)
    with pytest.raises(RuntimeError, match="No XMPP server"):
        await Harness(sc, start_server=False).start()


@pytest.mark.asyncio
async def test_failed_start_cleans_up(xmpp_server, monkeypatch):
    sc = scenario_for(xmpp_server, 5271)
    h = Harness(sc, start_server=False)
    real = h.build_agent
    def boom(spec):
        if spec.role == "rider":
            raise RuntimeError("boom")
        return real(spec)
    monkeypatch.setattr(h, "build_agent", boom)
    with pytest.raises(RuntimeError, match="boom"):
        await h.start()
    assert not h.agents


# ---- real processes -------------------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _scenario_file(tmp_path, port, server_port):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({
        "name": "proc", "graph": "config/porto_graph.json",
        "xmpp": {"port": port, "server_port": server_port}, "clock": {"speed": 100},
        "hubs": ["hub_trindade", "hub_campanha"], "drivers": {"count": 1}, "riders": {"count": 1}}))
    return f


def _env():
    return {**os.environ, "PYTHONPATH": SRC}


def test_launcher_check_mode_starts_its_own_server(tmp_path):
    f = _scenario_file(tmp_path, _free_port(), _free_port())
    p = subprocess.run([sys.executable, "-m", "harness.launcher", "--scenario", str(f), "--check"],
                       capture_output=True, text=True, timeout=90, env=_env(), cwd=PROJECT_ROOT)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "8 agents registered" in p.stdout
    assert p.stdout.count(" ok ") == 8 and "FAIL" not in p.stdout


def test_launcher_rejects_bad_scenario(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"graph": "config/porto_graph.json", "hubs": ["hub_atlantis"]}))
    p = subprocess.run([sys.executable, "-m", "harness", "--scenario", str(f), "--check"],
                       capture_output=True, text=True, timeout=30, env=_env(), cwd=PROJECT_ROOT)
    assert p.returncode == 2 and "not in the graph" in p.stderr


def test_manual_ping_reaches_a_running_system(tmp_path):
    """The literal Milestone 3 criterion: launcher running, ping CLI from a second process."""
    port = _free_port()
    f = _scenario_file(tmp_path, port, _free_port())
    launcher = subprocess.Popen(
        [sys.executable, "-m", "harness.launcher", "--scenario", str(f), "--run-for", "60"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_env(), cwd=PROJECT_ROOT)
    try:
        deadline = time.time() + 60
        while time.time() < deadline and not port_open("localhost", port):
            time.sleep(0.2)
        time.sleep(4)                                             # let all agents register
        one = subprocess.run([sys.executable, "-m", "harness.ping", "hub_campanha",
                              "--scenario", str(f)], capture_output=True, text=True, timeout=60,
                             env=_env(), cwd=PROJECT_ROOT)
        assert one.returncode == 0, one.stdout + one.stderr
        assert "hub_campanha@localhost" in one.stdout and "microhub" in one.stdout
        every = subprocess.run([sys.executable, "-m", "harness.ping", "--all", "--scenario",
                                str(f)], capture_output=True, text=True, timeout=60,
                               env=_env(), cwd=PROJECT_ROOT)
        assert every.returncode == 0, every.stdout + every.stderr
        # the payment demo against the running system (scenario found through the marker)
        demo = subprocess.run([sys.executable, "-m", "harness.demo_payment", "--live"],
                              capture_output=True, text=True, timeout=90, env=_env(),
                              cwd=PROJECT_ROOT)
        assert demo.returncode == 0, demo.stdout + demo.stderr
        assert "ALL CHECKS PASSED" in demo.stdout and "      FAIL " not in demo.stdout
        # no --scenario: ping finds the running launcher's scenario through the marker
        implicit = subprocess.run([sys.executable, "-m", "harness.ping", "--all"],
                                  capture_output=True, text=True, timeout=60, env=_env(),
                                  cwd=PROJECT_ROOT)
        assert implicit.returncode == 0, implicit.stdout + implicit.stderr
        assert "scenario: proc" in implicit.stderr and implicit.stdout.count(" ok ") == 8
        by_role = subprocess.run([sys.executable, "-m", "harness.ping", "microhub", "--scenario",
                                  str(f)], capture_output=True, text=True, timeout=60,
                                 env=_env(), cwd=PROJECT_ROOT)
        assert by_role.stdout.count("microhub") >= 2 and by_role.returncode == 0
    finally:
        launcher.terminate()
        try:
            launcher.wait(timeout=20)
        except subprocess.TimeoutExpired:
            launcher.kill()
    assert not (tmp_path / "run" / "active_scenario.json").exists()       # cleaned on exit


def test_payment_demo_standalone_checks_the_balances(tmp_path):
    f = _scenario_file(tmp_path, _free_port(), _free_port())
    p = subprocess.run([sys.executable, "-m", "harness.demo_payment", "--scenario", str(f)],
                       capture_output=True, text=True, timeout=90, env=_env(), cwd=PROJECT_ROOT)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "ALL CHECKS PASSED" in p.stdout and "      FAIL " not in p.stdout
    assert "driver was paid exactly 21.00" in p.stdout and "books balance: True" in p.stdout


def test_payment_demo_live_needs_a_running_launcher(tmp_path):
    p = subprocess.run([sys.executable, "-m", "harness.demo_payment", "--live"],
                       capture_output=True, text=True, timeout=30, env=_env(), cwd=PROJECT_ROOT)
    assert p.returncode == 2 and "start `python -m harness.launcher` first" in p.stderr
