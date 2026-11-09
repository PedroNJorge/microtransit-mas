import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# src-layout: make `import common...` work even without `pythonpath = ["src"]` in pyproject
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

XMPP_PORT = 5233
SERVER_PORT = 5271


def _port_open(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def xmpp_server(tmp_path_factory):
    """Throw-away SPADE XMPP server on non-default ports. Agents must pass port=XMPP_PORT.

    The account database is a temporary FILE, not `--memory`: with --memory the server keeps
    accounts in an in-memory SQLite database that can end up empty on another connection
    ("no such table: credentials"), after which every new agent hangs while connecting."""
    db = tmp_path_factory.mktemp("xmpp") / "server.db"
    proc = subprocess.Popen(
        [sys.executable, "-m", "spade.cli", "run", "--host", "localhost", "--db", str(db),
         "--purge", "--client_port", str(XMPP_PORT), "--server_port", str(SERVER_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if _port_open(XMPP_PORT):
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("could not start the SPADE XMPP server")
    yield XMPP_PORT
    proc.terminate()
    proc.wait(timeout=10)
