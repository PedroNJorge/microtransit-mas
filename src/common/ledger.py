"""
src/common/ledger.py
Money bookkeeping for the PaymentAgent. Pure Python: no SPADE, no clock dependency, so every
rule can be unit-tested without a network.

Model
- DOUBLE ENTRY, integer cents. Every movement is `transfer(src, dst, cents)`; the sum of all
  account balances is always 0 (`EXTERNAL` goes negative by whatever entered the system:
  rider endowments and subsidy funding). That is the Financial Conservation invariant of the
  Environmental Model: money is only transferred, except subsidies (an external inflow).
  `check_conservation()` verifies it.
- Accounts: "wallet:<jid>" (riders, drivers), "escrow:<escrow_id>", "subsidy_fund", "external".
- Escrow records are immutable facts (SEC-04); only their STATUS moves, and only
  HELD -> SETTLED or HELD -> REFUNDED, never back. Every movement is an append-only Entry.

Rules enforced here (the agent adds messaging on top)
- hold:    needs a wallet with enough funds; idempotent per request_id (a repeated request
           returns the same escrow instead of charging twice).
- settle:  only for the escrow's own hub (GC-03 / ID-06). The driver receives exactly the
           fares in TripCompleted (never below the auction price, which is the sum of the
           riders' shares); the unused part of each hold goes back to the rider; an eligible
           rider is then rebated `subsidy_discount * fare` from the subsidy fund. The subsidy
           is on top of the fare: it never reduces the driver's payout. All-or-nothing:
           any invalid rider in the request means nothing moves. Repeating a settlement
           moves no money and reports `replayed=True`.
- refund:  only for the escrow's own hub, only while HELD; full amount back to the rider.
           Refunding an already refunded escrow is a harmless replay; a settled one is refused.
- expired: HELD escrows older than `hold_max` can be swept back to their riders (liveness
           net for a hub that crashed before settling or refunding).

All account ids passed in must already be normalised (full JIDs); the agent does that.
"""
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from common.ontology import EscrowStatus

EXTERNAL = "external"
FUND = "subsidy_fund"


class Reason:
    """Machine-readable refusal reasons (they travel as the body of a `refuse`)."""
    INSUFFICIENT_FUNDS = "insufficient_funds"
    UNKNOWN_ACCOUNT = "unknown_account"
    INVALID_AMOUNT = "invalid_amount"
    DUPLICATE_REQUEST = "duplicate_request"
    UNKNOWN_ESCROW = "unknown_escrow"
    UNKNOWN_POOL = "unknown_pool"
    NOT_AUTHORISED = "not_authorised"
    ALREADY_SETTLED = "already_settled"
    NOT_HELD = "not_held"
    FARE_EXCEEDS_ESCROW = "fare_exceeds_escrow"
    INVALID_FARE = "invalid_fare"
    RIDER_WITHOUT_ESCROW = "rider_without_escrow"
    POOL_MISMATCH = "pool_mismatch"


class LedgerError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# ---- money helpers ----------------------------------------------------------------------

def cents(amount: float) -> int:
    """float money -> integer cents, rounding half up. Rejects NaN/inf/bool."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
        raise LedgerError(Reason.INVALID_AMOUNT, f"{amount!r}")
    return int((Decimal(str(amount)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def money(c: int) -> float:
    return c / 100


def wallet(account: str) -> str:
    return f"wallet:{account}"


# ---- records ------------------------------------------------------------------------------

@dataclass(frozen=True)
class EscrowRecord:
    escrow_id: str
    request_id: str
    rider: str
    pool_id: str
    amount_c: int
    subsidy_eligible: bool
    hub: str                            # "" = no hub bound (anyone may settle: tests only)
    created_at: float

    @property
    def amount(self) -> float:
        return money(self.amount_c)


@dataclass(frozen=True)
class Entry:
    seq: int
    time: float
    kind: str
    src: str
    dst: str
    cents: int
    ref: str


@dataclass(frozen=True)
class SettlementLine:
    escrow_id: str
    rider: str
    driver: str
    payout_c: int
    subsidy_c: int
    excess_c: int
    shortfall_c: int = 0                # subsidy wanted but not paid (fund exhausted)


@dataclass
class SettleOutcome:
    pool_id: str
    driver: str
    lines: List[SettlementLine]
    replayed: bool = False

    @property
    def total_paid(self) -> float:
        return money(sum(l.payout_c for l in self.lines))

    @property
    def total_subsidy(self) -> float:
        return money(sum(l.subsidy_c for l in self.lines))

    @property
    def total_returned(self) -> float:
        return money(sum(l.excess_c for l in self.lines))


@dataclass(frozen=True)
class RefundItem:
    escrow_id: str
    ok: bool
    rider: str = ""
    amount_c: int = 0
    reason: str = ""                    # why it was refused (when ok is False)
    replayed: bool = False

    @property
    def amount(self) -> float:
        return money(self.amount_c)


class Ledger:
    def __init__(self, default_rider_balance: Optional[float] = None,
                 subsidy_budget: Optional[float] = None, subsidy_discount: float = 0.0,
                 now: Callable[[], float] = lambda: 0.0):
        """default_rider_balance: wallet opened on a rider's first escrow request (simulation
        endowment, booked as an external inflow). None: unknown riders are refused.
        subsidy_budget: size of the subsidy fund; None = unlimited (funded on demand).
        subsidy_discount: share of the fare rebated to eligible riders, in [0, 1]."""
        self._now = now
        self.default_rider_balance = default_rider_balance
        self.unlimited_subsidy = subsidy_budget is None
        self.set_subsidy_discount(subsidy_discount)
        self._balances: Dict[str, int] = {EXTERNAL: 0, FUND: 0}
        self.entries: List[Entry] = []
        self._escrows: Dict[str, EscrowRecord] = {}
        self._status: Dict[str, EscrowStatus] = {}
        self._by_request: Dict[str, str] = {}
        self._by_pool: Dict[str, List[str]] = {}
        self._settled: Dict[str, SettlementLine] = {}
        self.subsidy_spent_c = 0
        self.subsidy_shortfall_c = 0
        if subsidy_budget is not None:
            if cents(subsidy_budget) < 0:
                raise ValueError("subsidy_budget must be >= 0")
            if cents(subsidy_budget):                   # 0 is a valid budget: "no subsidies"
                self.fund_subsidy(subsidy_budget)

    # ---- low level --------------------------------------------------------------------
    def _transfer(self, kind: str, src: str, dst: str, amount_c: int, ref: str = "") -> None:
        if amount_c < 0 or self._balances.get(src, 0) - amount_c < 0 and src != EXTERNAL:
            raise AssertionError(f"ledger bug: {kind} {src}->{dst} {amount_c} would overdraw")
        self._balances[src] = self._balances.get(src, 0) - amount_c
        self._balances[dst] = self._balances.get(dst, 0) + amount_c
        self.entries.append(Entry(len(self.entries) + 1, self._now(), kind, src, dst, amount_c, ref))

    def check_conservation(self) -> bool:
        """Sum of every balance (EXTERNAL included) is zero and nothing but EXTERNAL is negative."""
        return (sum(self._balances.values()) == 0
                and all(v >= 0 for k, v in self._balances.items() if k != EXTERNAL))

    # ---- accounts and funding ---------------------------------------------------------------
    def open_wallet(self, account: str, balance: float = 0.0) -> None:
        """Create a wallet (no-op if it exists, except the endowment is added)."""
        c = cents(balance)
        if c < 0:
            raise LedgerError(Reason.INVALID_AMOUNT, "negative balance")
        self._balances.setdefault(wallet(account), 0)
        if c:
            self._transfer("endowment", EXTERNAL, wallet(account), c, account)

    def has_wallet(self, account: str) -> bool:
        return wallet(account) in self._balances

    def balance(self, account: str) -> float:
        return money(self._balances.get(wallet(account), 0))

    def fund_subsidy(self, amount: float) -> None:
        c = cents(amount)
        if c <= 0:
            raise LedgerError(Reason.INVALID_AMOUNT, "subsidy funding must be > 0")
        self._transfer("subsidy_funding", EXTERNAL, FUND, c)

    def set_subsidy_discount(self, discount: float) -> None:
        if isinstance(discount, bool) or not isinstance(discount, (int, float)) \
                or not 0.0 <= discount <= 1.0:
            raise ValueError("subsidy_discount must be a number in [0, 1]")
        self.subsidy_discount = float(discount)

    @property
    def fund_balance(self) -> float:
        return money(self._balances[FUND])

    @property
    def subsidy_spent(self) -> float:
        return money(self.subsidy_spent_c)

    @property
    def subsidy_shortfall(self) -> float:
        return money(self.subsidy_shortfall_c)

    def escrow_balance(self, escrow_id: str) -> float:
        return money(self._balances.get(f"escrow:{escrow_id}", 0))

    # ---- queries --------------------------------------------------------------------------
    def get(self, escrow_id: str) -> Optional[EscrowRecord]:
        return self._escrows.get(escrow_id)

    def status(self, escrow_id: str) -> Optional[EscrowStatus]:
        return self._status.get(escrow_id)

    def pool_escrows(self, pool_id: str) -> List[EscrowRecord]:
        return [self._escrows[e] for e in self._by_pool.get(pool_id, [])]

    def held(self) -> List[EscrowRecord]:
        return [r for e, r in self._escrows.items() if self._status[e] == EscrowStatus.HELD]

    # ---- EscrowProtocol ------------------------------------------------------------------
    def hold(self, request_id: str, rider: str, pool_id: str, amount: float,
             subsidy_eligible: bool = False, hub: str = "") -> Tuple[EscrowRecord, bool]:
        """Lock `amount` of the rider's wallet. Returns (record, created); created is False
        when the same request was already held (idempotent)."""
        amount_c = cents(amount)
        if amount_c <= 0:
            raise LedgerError(Reason.INVALID_AMOUNT, "amount must be > 0")
        known = self._by_request.get(request_id)
        if known is not None:
            rec = self._escrows[known]
            if (rec.rider, rec.pool_id, rec.amount_c, rec.hub) != (rider, pool_id, amount_c, hub):
                raise LedgerError(Reason.DUPLICATE_REQUEST, request_id)
            return rec, False
        if not self.has_wallet(rider):
            if self.default_rider_balance is None:
                raise LedgerError(Reason.UNKNOWN_ACCOUNT, rider)
            self.open_wallet(rider, self.default_rider_balance)
        if self._balances[wallet(rider)] < amount_c:
            raise LedgerError(Reason.INSUFFICIENT_FUNDS, rider)
        escrow_id = f"esc-{len(self._escrows) + 1:06d}"
        rec = EscrowRecord(escrow_id, request_id, rider, pool_id, amount_c,
                           bool(subsidy_eligible), hub, self._now())
        self._escrows[escrow_id] = rec
        self._status[escrow_id] = EscrowStatus.HELD
        self._by_request[request_id] = escrow_id
        self._by_pool.setdefault(pool_id, []).append(escrow_id)
        self._transfer("hold", wallet(rider), f"escrow:{escrow_id}", amount_c, request_id)
        return rec, True

    # ---- SettlementProtocol --------------------------------------------------------------
    def settle(self, pool_id: str, driver: str, rider_ids: Iterable[str],
               rider_fares: Optional[Dict[str, float]] = None,
               requester: str = "") -> SettleOutcome:
        rider_ids = list(dict.fromkeys(rider_ids))
        if not self._by_pool.get(pool_id):
            raise LedgerError(Reason.UNKNOWN_POOL, pool_id)
        if not rider_ids:
            raise LedgerError(Reason.RIDER_WITHOUT_ESCROW, "no riders in trip_completed")

        # ---- validate everything first: all or nothing -----------------------------------
        targets: List[EscrowRecord] = []
        for rider in rider_ids:
            mine = [r for r in self.pool_escrows(pool_id) if r.rider == rider]
            if not mine:
                raise LedgerError(Reason.RIDER_WITHOUT_ESCROW, rider)
            targets += mine
        fares_c: Dict[str, int] = {}
        for rec in targets:
            if rec.hub and rec.hub != requester:
                raise LedgerError(Reason.NOT_AUTHORISED, rec.escrow_id)
            st = self._status[rec.escrow_id]
            if st == EscrowStatus.REFUNDED:
                raise LedgerError(Reason.NOT_HELD, rec.escrow_id)
            if st != EscrowStatus.HELD:
                continue
            if rider_fares:
                if rec.rider not in rider_fares:
                    raise LedgerError(Reason.INVALID_FARE, f"no fare for {rec.rider}")
                fare_c = cents(rider_fares[rec.rider])
            else:
                fare_c = rec.amount_c
            if fare_c < 0:
                raise LedgerError(Reason.INVALID_FARE, f"{rec.rider}: negative fare")
            if fare_c > rec.amount_c:
                raise LedgerError(Reason.FARE_EXCEEDS_ESCROW, rec.escrow_id)
            fares_c[rec.escrow_id] = fare_c

        if not fares_c:                                 # everything already settled: replay
            return SettleOutcome(pool_id, driver, [self._settled[r.escrow_id] for r in targets],
                                 replayed=True)

        # ---- apply ----------------------------------------------------------------------------
        self._balances.setdefault(wallet(driver), 0)
        lines: List[SettlementLine] = []
        for rec in targets:
            if rec.escrow_id not in fares_c:
                lines.append(self._settled[rec.escrow_id])
                continue
            fare_c = fares_c[rec.escrow_id]
            acct = f"escrow:{rec.escrow_id}"
            if fare_c:
                self._transfer("payout", acct, wallet(driver), fare_c, rec.escrow_id)
            excess_c = rec.amount_c - fare_c
            if excess_c:
                self._transfer("return", acct, wallet(rec.rider), excess_c, rec.escrow_id)
            subsidy_c = shortfall_c = 0
            if rec.subsidy_eligible and self.subsidy_discount > 0 and fare_c:
                wanted = int((Decimal(str(self.subsidy_discount)) * fare_c)
                             .quantize(Decimal(1), rounding=ROUND_HALF_UP))
                if self.unlimited_subsidy and self._balances[FUND] < wanted:
                    self._transfer("subsidy_funding", EXTERNAL, FUND, wanted - self._balances[FUND])
                subsidy_c = min(wanted, self._balances[FUND])
                shortfall_c = wanted - subsidy_c
                if subsidy_c:
                    self._transfer("subsidy", FUND, wallet(rec.rider), subsidy_c, rec.escrow_id)
                self.subsidy_spent_c += subsidy_c
                self.subsidy_shortfall_c += shortfall_c
            self._status[rec.escrow_id] = EscrowStatus.SETTLED
            line = SettlementLine(rec.escrow_id, rec.rider, driver, fare_c, subsidy_c,
                                  excess_c, shortfall_c)
            self._settled[rec.escrow_id] = line
            lines.append(line)
        return SettleOutcome(pool_id, driver, lines)

    # ---- RefundProtocol -------------------------------------------------------------------
    def refund(self, pool_id: str, escrow_ids: Iterable[str] = (),
               requester: str = "") -> List[RefundItem]:
        """Refund the listed escrows, or EVERY escrow of the pool if none are listed. Each is
        judged on its own and the result has one item per escrow: held -> refunded, already
        refunded -> ok + replayed, settled -> refused. (Never an empty answer for a known
        pool, so the requester always learns what happened.)"""
        ids = list(dict.fromkeys(escrow_ids))
        if not ids:
            ids = list(self._by_pool.get(pool_id, []))
            if not ids:
                raise LedgerError(Reason.UNKNOWN_POOL, pool_id)
        return [self._refund_one(pool_id, e, requester) for e in ids]

    def _refund_one(self, pool_id: str, escrow_id: str, requester: str,
                    force: bool = False) -> RefundItem:
        rec = self._escrows.get(escrow_id)
        if rec is None:
            return RefundItem(escrow_id, False, reason=Reason.UNKNOWN_ESCROW)
        if rec.pool_id != pool_id:
            return RefundItem(escrow_id, False, reason=Reason.POOL_MISMATCH)
        if not force and rec.hub and rec.hub != requester:
            return RefundItem(escrow_id, False, reason=Reason.NOT_AUTHORISED)
        st = self._status[escrow_id]
        if st == EscrowStatus.SETTLED:
            return RefundItem(escrow_id, False, reason=Reason.ALREADY_SETTLED)
        if st == EscrowStatus.REFUNDED:
            return RefundItem(escrow_id, True, rec.rider, rec.amount_c, replayed=True)
        self._transfer("refund", f"escrow:{escrow_id}", wallet(rec.rider), rec.amount_c, escrow_id)
        self._status[escrow_id] = EscrowStatus.REFUNDED
        return RefundItem(escrow_id, True, rec.rider, rec.amount_c)

    def expired(self, now: float, hold_max: float) -> List[EscrowRecord]:
        return [r for r in self.held() if now - r.created_at >= hold_max]

    def refund_expired(self, now: float, hold_max: float) -> List[Tuple[EscrowRecord, RefundItem]]:
        """Liveness net: return HELD escrows older than `hold_max` to their riders."""
        return [(r, self._refund_one(r.pool_id, r.escrow_id, "", force=True))
                for r in self.expired(now, hold_max)]
