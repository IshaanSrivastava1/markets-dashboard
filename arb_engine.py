#!/usr/bin/env python3
"""Gold arbitrage tracker (V3) - detection engine.

Pure functions over normalized Contracts (see arb_sources.py) - no network,
no I/O - so the logic is unit-testable and reusable outside the dashboard.

Core idea: nearly every mispricing class reduces to one implication rule.
If contract A resolving YES logically forces contract B to resolve YES
(same underlying), then P(A) <= P(B) must hold. The executable, buy-only
version: buying B-YES at its ask plus A-NO at its ask guarantees a >= $1
payout per pair (A yes -> B yes pays the YES leg; A no pays the NO leg),
so whenever  B.yes_ask + A.no_ask < 1  there is a locked-in edge. This one
rule covers:
  - strike-ladder monotonicity (same event, higher strike priced >= lower)
  - nested touch windows (weekly "hit $X" priced above monthly "hit $X")
  - settle-vs-touch bounds (settling above $X implies having touched $X)
  - cross-platform equivalents (mutual implication -> checked both ways)
NO-side comparisons (e.g. Polymarket "touch low" vs the NO side of a Kalshi
"settle above") are handled by synthesizing complement views of settlement
contracts (YES of "settle below X" == NO of "settle above X").

Separately, mutually-exclusive bracket events are checked for
buy-all-YES (asks sum < $1) and buy-all-NO (asks sum < $n-1).

Fees: Kalshi taker fee = 0.07 * P * (1-P) per contract (per-order round-up
to the cent is ignored; add ~1c per order when acting on tiny edges).
Polymarket charges no trading fee. Slippage beyond top-of-book is not
modeled - flagged sizes must be checked against book depth before trading.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from arb_sources import (
    Contract,
    DIRECTION, SETTLE_ABOVE, SETTLE_BELOW, SETTLE_RANGE, TOUCH_HIGH, TOUCH_LOW,
)

TIME_TOLERANCE = timedelta(minutes=5)
EXPIRING_SOON = timedelta(hours=48)
KALSHI_TAKER_FEE_RATE = 0.07


def kalshi_fee(price):
    """Kalshi taker fee per contract at execution price `price` (dollars)."""
    return KALSHI_TAKER_FEE_RATE * price * (1 - price)


def leg_fee(platform, price):
    return kalshi_fee(price) if platform == "kalshi" else 0.0


@dataclass
class Leg:
    contract: Contract
    action: str        # "BUY YES" | "BUY NO"
    price: float       # executable ask for that side, in probability dollars
    fee: float


@dataclass
class Opportunity:
    kind: str          # "ladder" | "cross-event" | "cross-platform" | "bracket-sum"
    description: str
    legs: List[Leg]
    gross_edge: float  # guaranteed profit per $1 contract pair/set, before fees
    fees: float
    net_edge: float
    expires_at: Optional[str]
    expiring_soon: bool
    caveats: List[str] = field(default_factory=list)


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class _View:
    """The YES side of a contract, or (for settlement markets) the synthesized
    complement: YES of 'settle below X' == NO of 'settle above X'."""

    def __init__(self, contract, complement=False):
        self.contract = contract
        self.complement = complement
        semantics = contract.semantics
        if complement:
            assert semantics in (SETTLE_ABOVE, SETTLE_BELOW)
            semantics = SETTLE_BELOW if semantics == SETTLE_ABOVE else SETTLE_ABOVE
        self.kind = "settle" if semantics in (SETTLE_ABOVE, SETTLE_BELOW) else "touch"
        self.direction = "above" if semantics in (SETTLE_ABOVE, TOUCH_HIGH) else "below"
        self.threshold = (
            contract.threshold_low if contract.threshold_low is not None
            else contract.threshold_high
        )
        self.window_start = _parse_iso(contract.window_start)
        self.window_end = _parse_iso(contract.window_end)
        if complement:
            self.yes_bid, self.yes_ask = contract.no_bid, contract.no_ask
            self.no_bid, self.no_ask = contract.yes_bid, contract.yes_ask
        else:
            self.yes_bid, self.yes_ask = contract.yes_bid, contract.yes_ask
            self.no_bid, self.no_ask = contract.no_bid, contract.no_ask

    def buy_yes_leg(self):
        action = "BUY NO" if self.complement else "BUY YES"
        return Leg(self.contract, action, self.yes_ask,
                   leg_fee(self.contract.platform, self.yes_ask))

    def buy_no_leg(self):
        action = "BUY YES" if self.complement else "BUY NO"
        return Leg(self.contract, action, self.no_ask,
                   leg_fee(self.contract.platform, self.no_ask))


def _make_views(contracts):
    views = []
    for c in contracts:
        if c.semantics in (DIRECTION, SETTLE_RANGE):
            continue  # no threshold semantics to compare (none live for gold)
        if c.threshold_low is None and c.threshold_high is None:
            continue
        views.append(_View(c))
        if c.semantics in (SETTLE_ABOVE, SETTLE_BELOW):
            views.append(_View(c, complement=True))
    return views


def _same_time(t1, t2):
    return t1 is not None and t2 is not None and abs(t1 - t2) <= TIME_TOLERANCE


def _within_window(t, view):
    return (
        t is not None and view.window_start is not None and view.window_end is not None
        and view.window_start - TIME_TOLERANCE <= t <= view.window_end + TIME_TOLERANCE
    )


def _window_subset(a, b):
    """a's observation window fits inside b's."""
    return (
        a.window_start is not None and b.window_start is not None
        and a.window_end is not None and b.window_end is not None
        and b.window_start - TIME_TOLERANCE <= a.window_start
        and a.window_end <= b.window_end + TIME_TOLERANCE
    )


def implies(a, b):
    """True if view `a` resolving YES logically forces view `b` to resolve YES."""
    if a.contract.underlying != b.contract.underlying:
        return False
    if a.direction != b.direction:
        return False
    if a.threshold is None or b.threshold is None:
        return False
    if a.direction == "above":
        threshold_ok = a.threshold >= b.threshold
    else:
        threshold_ok = a.threshold <= b.threshold

    if not threshold_ok:
        return False
    if a.kind == "settle" and b.kind == "settle":
        return _same_time(a.window_end, b.window_end)
    if a.kind == "settle" and b.kind == "touch":
        return _within_window(a.window_end, b)
    if a.kind == "touch" and b.kind == "touch":
        return _window_subset(a, b)
    return False  # touch never implies settle


def _pair_caveats(a, b):
    caveats = []
    if a.contract.settlement_source != b.contract.settlement_source:
        caveats.append(
            "Legs settle on different sources (%s vs %s) - small basis risk."
            % (a.contract.settlement_source, b.contract.settlement_source))
    if a.threshold == b.threshold and (a.complement or b.complement or a.kind != b.kind):
        caveats.append(
            "Equal thresholds with different boundary semantics (> vs >=) - "
            "a settle exactly on the strike can break the pairing.")
    return caveats


def _kind_for_pair(a, b):
    if a.contract.platform != b.contract.platform:
        return "cross-platform"
    if a.contract.event_id == b.contract.event_id:
        return "ladder"
    return "cross-event"


def _describe_leg(leg):
    return "%s '%s' @ %.3f on %s" % (
        leg.action, leg.contract.title, leg.price, leg.contract.platform)


def find_implication_arbs(contracts):
    """All executable implication violations: buy B-YES + A-NO for < $1
    where A-YES forces B-YES."""
    views = _make_views(contracts)
    opportunities = []
    seen = set()
    for a in views:
        if a.no_ask is None or not (0 < a.no_ask < 1):
            continue
        for b in views:
            if a.contract.market_id == b.contract.market_id:
                continue
            if b.yes_ask is None or not (0 < b.yes_ask < 1):
                continue
            if not implies(a, b):
                continue
            cost = b.yes_ask + a.no_ask
            if cost >= 1:
                continue
            legs = [b.buy_yes_leg(), a.buy_no_leg()]
            key = frozenset((l.contract.market_id, l.action) for l in legs)
            if key in seen:  # same trade reachable via complement views
                continue
            seen.add(key)
            gross = 1 - cost
            fees = sum(l.fee for l in legs)
            net = gross - fees
            if net <= 0:
                continue
            expires = min(filter(None, [a.window_end, b.window_end]), default=None)
            opportunities.append(Opportunity(
                kind=_kind_for_pair(a, b),
                description=(
                    "%s + %s: costs $%.3f for a guaranteed $1 payout "
                    "(YES on the first leg is forced whenever NO on the second loses)."
                    % (_describe_leg(legs[0]), _describe_leg(legs[1]), cost)),
                legs=legs,
                gross_edge=round(gross, 4),
                fees=round(fees, 4),
                net_edge=round(net, 4),
                expires_at=expires.strftime("%Y-%m-%dT%H:%M:%SZ") if expires else None,
                expiring_soon=(
                    expires is not None
                    and expires - datetime.now(timezone.utc) <= EXPIRING_SOON),
                caveats=_pair_caveats(a, b),
            ))
    return opportunities


def find_bracket_arbs(contracts):
    """Mutually-exclusive bracket events: buy every YES for < $1 total, or
    every NO for < $(n-1) total (exactly one bracket pays)."""
    groups = {}
    for c in contracts:
        if c.mutually_exclusive:
            groups.setdefault((c.platform, c.event_id), []).append(c)

    opportunities = []
    now = datetime.now(timezone.utc)
    for (platform, event_id), group in groups.items():
        if len(group) < 2:
            continue
        yes_asks = [c.yes_ask for c in group]
        no_asks = [c.no_ask for c in group]
        expires = min(filter(None, (_parse_iso(c.window_end) for c in group)),
                      default=None)
        caveat = ("Assumes the event's brackets are exhaustive "
                  "(exactly one resolves YES) and all were fetched.")
        checks = []
        if all(p is not None and 0 < p < 1 for p in yes_asks):
            checks.append(("BUY YES", yes_asks, 1.0))
        if all(p is not None and 0 < p < 1 for p in no_asks):
            checks.append(("BUY NO", no_asks, float(len(group) - 1)))
        for action, prices, payout in checks:
            cost = sum(prices)
            if cost >= payout:
                continue
            legs = [Leg(c, action, p, leg_fee(platform, p))
                    for c, p in zip(group, prices)]
            gross = payout - cost
            fees = sum(l.fee for l in legs)
            net = gross - fees
            if net <= 0:
                continue
            opportunities.append(Opportunity(
                kind="bracket-sum",
                description=(
                    "%s all %d brackets of %s/%s: total cost $%.3f vs "
                    "guaranteed $%.0f payout." % (
                        action, len(group), platform, event_id, cost, payout)),
                legs=legs,
                gross_edge=round(gross, 4),
                fees=round(fees, 4),
                net_edge=round(net, 4),
                expires_at=expires.strftime("%Y-%m-%dT%H:%M:%SZ") if expires else None,
                expiring_soon=(expires is not None and expires - now <= EXPIRING_SOON),
                caveats=[caveat],
            ))
    return opportunities


def find_opportunities(contracts, min_net_edge=0.0):
    """All detected opportunities, ranked by net edge (largest first)."""
    opportunities = find_implication_arbs(contracts) + find_bracket_arbs(contracts)
    opportunities = [o for o in opportunities if o.net_edge > min_net_edge]
    opportunities.sort(key=lambda o: o.net_edge, reverse=True)
    return opportunities


if __name__ == "__main__":
    from arb_sources import fetch_all_contracts

    found = find_opportunities(fetch_all_contracts())
    if not found:
        print("No opportunities with positive net edge right now.")
    for opp in found:
        print("[%s] net edge $%.4f/contract (gross %.4f, fees %.4f)%s"
              % (opp.kind, opp.net_edge, opp.gross_edge, opp.fees,
                 "  EXPIRING <48h" if opp.expiring_soon else ""))
        print("   " + opp.description)
        for caveat in opp.caveats:
            print("   caveat: " + caveat)
        print()
