"""
src/harness/active.py
Marker file telling the CLI tools which scenario the launcher is running right now.

The launcher writes <project>/.run/active_scenario.json on start and removes it on exit;
`harness.ping` uses it as its default scenario. A marker whose process is gone (crash,
kill -9) is ignored. Set HARNESS_RUN_DIR to keep the marker somewhere else (tests do).
"""
import json
import os
from pathlib import Path
from typing import Optional

from harness.scenario import PROJECT_ROOT


def marker_path() -> Path:
    base = os.environ.get("HARNESS_RUN_DIR")
    return (Path(base) if base else PROJECT_ROOT / ".run") / "active_scenario.json"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def write_active(scenario_path: Path) -> None:
    p = marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"scenario": str(Path(scenario_path).resolve()), "pid": os.getpid()}))


def clear_active() -> None:
    """Remove the marker, but only if this process wrote it."""
    p = marker_path()
    try:
        if json.loads(p.read_text()).get("pid") == os.getpid():
            p.unlink()
    except (OSError, ValueError):
        pass


def read_active() -> Optional[Path]:
    """Scenario file of the running launcher, or None if none is running."""
    try:
        data = json.loads(marker_path().read_text())
        pid, path = int(data["pid"]), Path(data["scenario"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return path if _alive(pid) and path.exists() else None
