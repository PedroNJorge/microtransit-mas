"""common/settings.py  Simulation parameters (all durations in SIMULATION seconds)."""

T_TRAFFIC = 60.0            # Environment broadcast interval (TrafficUpdateProtocol)
FULL_SNAPSHOT_EVERY = 10    # every Nth traffic update is a full snapshot (self-healing)

# ---- Payment (Milestone 4) -------------------------------------------------------------------
T_ESCROW_HOLD_MAX = 7200.0  # a HELD escrow older than this is returned to the rider (liveness net)
T_ESCROW_SWEEP = 60.0       # how often Payment looks for such escrows
SUBSIDY_DISCOUNT = 0.5      # initial share of the fare rebated to eligible riders, until a
                            # Policy from the Regulator says otherwise
