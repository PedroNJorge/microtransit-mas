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

# ---- Driver (Milestone 6) --------------------------------------------------------------------
T_PROVISIONAL = 60.0        # a bid's provisional lock expires after this (CC-02); must exceed
                            # T_AWARD + network delay (GC-17)
T_STATUS = 30.0             # DriverStatus heartbeat (DriverStatusProtocol)
T_NO_SHOW = 120.0           # driver waits this long for a rider at the pickup (FE-05)
T_REPOSITION = 30.0         # hub waits this long for an answer to a reposition request
DWELL_PICKUP = 15.0         # seconds spent boarding riders
DWELL_DROPOFF = 10.0        # seconds spent per drop-off
DRIVER_BASE_COST = 2.0      # EUR fixed cost of taking a job (the driver's true cost c_i, see
DRIVER_COST_PER_SECOND = 0.004   # docs/auction_mechanism.md) + EUR per second driven (empty or loaded)
DRIVER_CAPACITY = 4

# ---- Rider / pool formation / reputation (Milestone 7) ------------------------------------------
T_POOL_RESPONSE = 60.0      # a rider that does not answer a pool proposal within this is a reject
T_RIDER_PATIENCE = 900.0    # a rider gives up waiting for a pool proposal / a match after this
READY_DELAY = 10.0          # seconds a rider takes to walk to the car after "arriving"
REPUTATION_PRIOR = 3.0      # score of an unrated driver (the auction's default_reputation)
REPUTATION_PRIOR_WEIGHT = 2.0   # the prior counts as this many ratings (shrinks small samples)
MIN_RATINGS = 1             # ratings needed before a score is published (M8 may raise it)

# ---- Reputation gate / Regulator (Milestone 8) -----------------------------------------------
T_SETTLEMENT_MAX = 600.0    # ID-07: a rating is published unsettled (flagged) this long after the trip
T_REPUTATION_SWEEP = 30.0   # how often Reputation checks for ratings whose deadline passed
T_AUDIT = 3600.0            # Regulator audits every hub this often (RI-02)
T_AUDIT_RESPONSE = 60.0     # a hub that does not answer an audit request in time is non-cooperative (RI-05)
T_POLICY_ACK = 30.0         # the Regulator re-sends a policy to recipients that did not answer (RI-04)
PENALTY_NON_COOPERATION = 50.0   # EUR
PENALTY_FARE_VIOLATION = 10.0    # EUR per fare above the cap

# ---- Repositioning (hub rebalancing heuristic, protocols/reposition.py) ----------------------
# T_REPOSITION (above, Driver section) is how long the hub waits for agree / refuse.
T_REBALANCE = 60.0                  # the hub looks for drivers worth moving this often
REPOSITION_DEMAND_THRESHOLD = 3.0   # predicted requests (over the forecast horizon) above which a
                                    # staging point counts as "hot"
REPOSITION_REQUESTS_PER_DRIVER = 4.0    # predicted requests one waiting driver is expected to cover
                                        # (keep it >= the threshold: a point just over it wants ONE driver)
REPOSITION_MAX_TRAVEL = 600.0       # never move a driver further than this (seconds of driving)
REPOSITION_COOLDOWN = 120.0         # after a refusal / failure leave that driver alone this long
REPOSITION_MIN_SHIFT_LEFT = 900.0   # do not move a driver whose shift ends sooner than this
REPOSITION_INCENTIVE_MARGIN = 1.1   # the hub pays this x the driver's assumed driving cost
REPOSITION_MAX_MOVES = 3            # drivers moved per rebalancing round