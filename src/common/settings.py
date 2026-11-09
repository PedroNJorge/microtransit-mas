"""common/settings.py  Simulation parameters (all durations in SIMULATION seconds)."""

T_TRAFFIC = 60.0            # Environment broadcast interval (TrafficUpdateProtocol)
FULL_SNAPSHOT_EVERY = 10    # every Nth traffic update is a full snapshot (self-healing)
