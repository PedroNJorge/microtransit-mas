"""common/settings.py  Simulation parameters (all durations in SIMULATION seconds)."""

T_TRAFFIC = 60.0            # Environment broadcast interval (TrafficUpdateProtocol)
FULL_SNAPSHOT_EVERY = 10    # every Nth traffic update is a full snapshot (self-healing)

# ---- Payment (Milestone 4) -------------------------------------------------------------------
T_ESCROW_HOLD_MAX = 7200.0  # a HELD escrow older than this is returned to the rider (liveness net)
T_ESCROW_SWEEP = 60.0       # how often Payment looks for such escrows
SUBSIDY_DISCOUNT = 0.5      # initial share of the fare rebated to eligible riders, until a
                            # Policy from the Regulator says otherwise

# ---- Auction (Milestone 5) -------------------------------------------------------------------
T_AUCTION = 30.0            # bidding window after the CFP (Organizational Rules)
T_AWARD = 10.0              # time the winner has to confirm an accept-proposal (CC-05)
T_ESCROW = 15.0             # hub waits this long for escrow_confirmed (ID-09)
AUCTION_PRICING = "second_score"   # or "first_price"
MAX_AUCTION_ATTEMPTS = 2    # auctions per pool before it is declared unmatched (FE-01)
WIDEN_DETOUR = 300.0        # seconds of extra detour allowed on each retry

# Money value of the non-price attributes of a bid. These make the multi-attribute score
# QUASI-LINEAR (everything in EUR), which is what makes the second-score payment well defined
# and truthful. They are the rider-side valuation the hub acts on.
VALUE_OF_TIME = 0.004       # EUR per second until pickup (= 14.40 EUR per hour of waiting)
VALUE_OF_DETOUR = 0.004     # EUR per second of detour imposed on the riders
VALUE_OF_REPUTATION = 0.25  # EUR per reputation point below the maximum (5)
