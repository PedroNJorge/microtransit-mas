"""Active-scenario marker used by `python -m harness.ping`."""
import json
import os
import subprocess
import sys

import pytest

from harness import active


@pytest.fixture(autouse=True)
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_RUN_DIR", str(tmp_path))
    return tmp_path


def test_roundtrip_and_clear(tmp_path):
    scn = tmp_path / "s.json"
    scn.write_text("{}")
    assert active.read_active() is None
    active.write_active(scn)
    assert active.read_active() == scn.resolve()
    active.clear_active()
    assert active.read_active() is None


def test_dead_pid_is_ignored(tmp_path):
    scn = tmp_path / "s.json"
    scn.write_text("{}")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    active.marker_path().write_text(json.dumps({"scenario": str(scn), "pid": proc.pid}))
    assert active.read_active() is None


def test_missing_scenario_file_or_garbage_is_ignored(tmp_path):
    active.marker_path().write_text(json.dumps({"scenario": str(tmp_path / "gone.json"),
                                                "pid": os.getpid()}))
    assert active.read_active() is None
    active.marker_path().write_text("not json")
    assert active.read_active() is None


def test_clear_only_removes_own_marker(tmp_path):
    scn = tmp_path / "s.json"
    scn.write_text("{}")
    active.marker_path().write_text(json.dumps({"scenario": str(scn), "pid": os.getpid() + 1}))
    active.clear_active()
    assert active.marker_path().exists()
