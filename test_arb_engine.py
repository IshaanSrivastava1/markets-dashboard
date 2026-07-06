#!/usr/bin/env python3
"""Unit tests for arb_engine (pure logic, no network)."""
import unittest
from datetime import datetime, timedelta, timezone

import arb_engine
from arb_engine import find_opportunities, find_implication_arbs, kalshi_fee
from arb_sources import (
    Contract,
    SETTLE_ABOVE, TOUCH_HIGH, TOUCH_LOW, DIRECTION,
)

NOW = "2026-07-05T12:00:00Z"
JUL_END = "2026-07-31T21:00:00Z"
AUG_1 = "2026-08-01T03:59:59Z"
JUL_10 = "2026-07-10T21:00:00Z"


def make_contract(market_id, platform="polymarket", semantics=TOUCH_HIGH,
                  threshold=4400.0, window_start=NOW, window_end=AUG_1,
                  yes_bid=0.3, yes_ask=0.32, event_id="ev", underlying="XAU_SPOT",
                  mutually_exclusive=False, source="src"):
    thr_low = threshold if semantics in (SETTLE_ABOVE, TOUCH_HIGH) else None
    thr_high = threshold if semantics not in (SETTLE_ABOVE, TOUCH_HIGH) else None
    return Contract(
        platform=platform, market_id=market_id, event_id=event_id,
        title=market_id, url="", underlying=underlying, settlement_source=source,
        semantics=semantics, threshold_low=thr_low, threshold_high=thr_high,
        window_start=window_start, window_end=window_end,
        yes_bid=yes_bid, yes_ask=yes_ask,
        no_bid=round(1 - yes_ask, 6), no_ask=round(1 - yes_bid, 6),
        last=None, volume_24h=None, mutually_exclusive=mutually_exclusive,
        fetched_at=NOW,
    )


class LadderMonotonicityTest(unittest.TestCase):
    def test_violating_touch_ladder_is_flagged(self):
        # hit $12k priced above hit $8k: bid(12k)=0.040 > ask(8k)=0.038
        low = make_contract("hit8k", threshold=8000, yes_bid=0.031, yes_ask=0.038)
        high = make_contract("hit12k", threshold=12000, yes_bid=0.040, yes_ask=0.041)
        opps = find_opportunities([low, high])
        self.assertEqual(len(opps), 1)
        opp = opps[0]
        self.assertEqual(opp.kind, "ladder")
        # buy low-strike YES at 0.038, buy high-strike NO at 1-0.040=0.960
        self.assertAlmostEqual(opp.gross_edge, 1 - (0.038 + 0.960), places=6)
        actions = {(l.contract.market_id, l.action) for l in opp.legs}
        self.assertEqual(actions, {("hit8k", "BUY YES"), ("hit12k", "BUY NO")})
        self.assertEqual(opp.fees, 0)  # both legs polymarket

    def test_consistent_ladder_is_clean(self):
        low = make_contract("hit8k", threshold=8000, yes_bid=0.30, yes_ask=0.32)
        high = make_contract("hit12k", threshold=12000, yes_bid=0.10, yes_ask=0.12)
        self.assertEqual(find_opportunities([low, high]), [])

    def test_settle_ladder_violation(self):
        low = make_contract("k4400", platform="kalshi", semantics=SETTLE_ABOVE,
                            threshold=4400, window_start=JUL_END, window_end=JUL_END,
                            yes_bid=0.20, yes_ask=0.22)
        high = make_contract("k4500", platform="kalshi", semantics=SETTLE_ABOVE,
                             threshold=4500, window_start=JUL_END, window_end=JUL_END,
                             yes_bid=0.30, yes_ask=0.32)
        opps = find_opportunities([low, high])
        self.assertEqual(len(opps), 1)
        self.assertGreater(opps[0].fees, 0)  # kalshi legs pay taker fees


class WindowNestingTest(unittest.TestCase):
    def test_weekly_touch_above_monthly_touch_is_flagged(self):
        weekly = make_contract("wk4300", threshold=4300, window_end=JUL_10,
                               yes_bid=0.50, yes_ask=0.52, event_id="weekly")
        monthly = make_contract("mo4300", threshold=4300, window_end=AUG_1,
                                yes_bid=0.40, yes_ask=0.45, event_id="monthly")
        opps = find_opportunities([weekly, monthly])
        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0].kind, "cross-event")
        actions = {(l.contract.market_id, l.action) for l in opps[0].legs}
        # weekly YES implies monthly YES -> buy monthly YES + weekly NO
        self.assertEqual(actions, {("mo4300", "BUY YES"), ("wk4300", "BUY NO")})
        # cost = 0.45 + (1-0.50) = 0.95
        self.assertAlmostEqual(opps[0].gross_edge, 0.05, places=6)

    def test_consistent_nesting_is_clean(self):
        weekly = make_contract("wk4300", threshold=4300, window_end=JUL_10,
                               yes_bid=0.30, yes_ask=0.32, event_id="weekly")
        monthly = make_contract("mo4300", threshold=4300, window_end=AUG_1,
                                yes_bid=0.50, yes_ask=0.52, event_id="monthly")
        self.assertEqual(find_opportunities([weekly, monthly]), [])


class SettleVsTouchTest(unittest.TestCase):
    def test_settle_priced_above_touch_is_flagged(self):
        # Settling above $4400 on Jul 31 implies touching $4400 during July.
        settle = make_contract("k4400", platform="kalshi", semantics=SETTLE_ABOVE,
                               threshold=4400, window_start=JUL_END,
                               window_end=JUL_END, yes_bid=0.45, yes_ask=0.47,
                               source="kalshi-ice")
        touch = make_contract("pm4400", threshold=4400, window_end=AUG_1,
                              yes_bid=0.36, yes_ask=0.37, source="pm-spot")
        opps = find_opportunities([settle, touch])
        self.assertEqual(len(opps), 1)
        opp = opps[0]
        self.assertEqual(opp.kind, "cross-platform")
        actions = {(l.contract.market_id, l.action) for l in opp.legs}
        self.assertEqual(actions, {("pm4400", "BUY YES"), ("k4400", "BUY NO")})
        # cost = 0.37 + (1-0.45) = 0.92; kalshi NO leg fee at price 0.55
        self.assertAlmostEqual(opp.gross_edge, 0.08, places=6)
        self.assertAlmostEqual(opp.fees, round(kalshi_fee(0.55), 4), places=6)
        self.assertTrue(any("basis" in c for c in opp.caveats))

    def test_touch_never_implies_settle(self):
        # Touch priced above settle is NOT an arb (touch can happen w/o settle).
        settle = make_contract("k4400", platform="kalshi", semantics=SETTLE_ABOVE,
                               threshold=4400, window_start=JUL_END,
                               window_end=JUL_END, yes_bid=0.10, yes_ask=0.12)
        touch = make_contract("pm4400", threshold=4400, window_end=AUG_1,
                              yes_bid=0.80, yes_ask=0.82)
        self.assertEqual(find_opportunities([settle, touch]), [])

    def test_settle_outside_touch_window_is_ignored(self):
        # Kalshi settles Jul 31; weekly touch window ends Jul 10 -> no relation.
        settle = make_contract("k4400", platform="kalshi", semantics=SETTLE_ABOVE,
                               threshold=4400, window_start=JUL_END,
                               window_end=JUL_END, yes_bid=0.45, yes_ask=0.47)
        touch = make_contract("wk4400", threshold=4400, window_end=JUL_10,
                              yes_bid=0.36, yes_ask=0.37)
        self.assertEqual(find_opportunities([settle, touch]), [])


class ComplementViewTest(unittest.TestCase):
    def test_touch_low_vs_settle_above_no_side(self):
        # Settling at/below $3872 (NO of 'above 3872') implies touching <= $3900.
        # Kalshi NO ask cheap + Polymarket touch-low... violation when
        # complement YES (= settle-below) bid exceeds touch-low ask:
        # buy touch-low YES + buy settle-below NO (= settle-above YES).
        settle = make_contract("k3872", platform="kalshi", semantics=SETTLE_ABOVE,
                               threshold=3872, window_start=JUL_END,
                               window_end=JUL_END, yes_bid=0.60, yes_ask=0.62)
        # settle-below view: yes_bid=1-0.62=0.38, yes_ask=1-0.60=0.40
        touch_low = make_contract("pm3900", semantics=TOUCH_LOW, threshold=3900,
                                  window_end=AUG_1, yes_bid=0.20, yes_ask=0.25)
        opps = find_opportunities([settle, touch_low])
        self.assertEqual(len(opps), 1)
        actions = {(l.contract.market_id, l.action) for l in opps[0].legs}
        self.assertEqual(actions, {("pm3900", "BUY YES"), ("k3872", "BUY YES")})
        # cost = touch ask 0.25 + settle-above YES ask 0.62 = 0.87
        self.assertAlmostEqual(opps[0].gross_edge, 0.13, places=6)

    def test_no_duplicate_from_complement_views(self):
        # A settle-ladder violation is reachable through base views and through
        # both complements - it must be reported once.
        low = make_contract("k4400", platform="kalshi", semantics=SETTLE_ABOVE,
                            threshold=4400, window_start=JUL_END, window_end=JUL_END,
                            yes_bid=0.20, yes_ask=0.22)
        high = make_contract("k4500", platform="kalshi", semantics=SETTLE_ABOVE,
                             threshold=4500, window_start=JUL_END, window_end=JUL_END,
                             yes_bid=0.30, yes_ask=0.32)
        opps = find_implication_arbs([low, high])
        self.assertEqual(len(opps), 1)


class EquivalentPairTest(unittest.TestCase):
    def test_cross_platform_equivalent_checked_both_ways(self):
        a = make_contract("k4400", platform="kalshi", semantics=SETTLE_ABOVE,
                          threshold=4400, window_start=JUL_END, window_end=JUL_END,
                          yes_bid=0.40, yes_ask=0.42)
        b = make_contract("pm4400", platform="polymarket", semantics=SETTLE_ABOVE,
                          threshold=4400, window_start=JUL_END, window_end=JUL_END,
                          yes_bid=0.50, yes_ask=0.52)
        opps = find_opportunities([a, b])
        # buy kalshi YES @0.42 + poly NO @(1-0.50)=0.50 -> cost 0.92
        self.assertEqual(len(opps), 1)
        self.assertAlmostEqual(opps[0].gross_edge, 0.08, places=6)

    def test_fair_equivalent_pair_is_clean(self):
        a = make_contract("k4400", platform="kalshi", semantics=SETTLE_ABOVE,
                          threshold=4400, window_start=JUL_END, window_end=JUL_END,
                          yes_bid=0.44, yes_ask=0.46)
        b = make_contract("pm4400", platform="polymarket", semantics=SETTLE_ABOVE,
                          threshold=4400, window_start=JUL_END, window_end=JUL_END,
                          yes_bid=0.45, yes_ask=0.47)
        self.assertEqual(find_opportunities([a, b]), [])


class BracketSumTest(unittest.TestCase):
    def _brackets(self, asks, spread=0.01):
        return [
            make_contract("b%d" % i, semantics=SETTLE_ABOVE, threshold=4000 + i,
                          window_start=JUL_END, window_end=JUL_END,
                          yes_bid=round(a - spread, 4), yes_ask=a,
                          event_id="brackets", mutually_exclusive=True)
            for i, a in enumerate(asks)
        ]

    def test_cheap_bracket_set_is_flagged(self):
        opps = find_opportunities(self._brackets([0.30, 0.30, 0.30]))
        buy_yes = [o for o in opps if o.kind == "bracket-sum"
                   and o.legs[0].action == "BUY YES"]
        self.assertEqual(len(buy_yes), 1)
        self.assertAlmostEqual(buy_yes[0].gross_edge, 0.10, places=6)

    def test_fair_bracket_set_is_clean(self):
        # Asks sum to 1.10 (> 1, no buy-YES arb) and bids sum to 0.80
        # (< 1, so NO asks sum to 2.20 > n-1, no buy-NO arb either).
        opps = find_opportunities(self._brackets([0.40, 0.35, 0.35], spread=0.10))
        self.assertEqual([o for o in opps if o.kind == "bracket-sum"], [])

    def test_non_exclusive_events_not_bracket_checked(self):
        contracts = self._brackets([0.30, 0.30, 0.30])
        for c in contracts:
            c.mutually_exclusive = False
        opps = find_opportunities(contracts)
        self.assertEqual([o for o in opps if o.kind == "bracket-sum"], [])


class GuardrailTest(unittest.TestCase):
    def test_different_underlyings_never_paired(self):
        gc = make_contract("gc4400", threshold=4400, underlying="GC_FUTURES",
                           yes_bid=0.80, yes_ask=0.82)
        xau = make_contract("xau4400", threshold=4400, underlying="XAU_SPOT",
                            yes_bid=0.10, yes_ask=0.12)
        self.assertEqual(find_opportunities([gc, xau]), [])

    def test_direction_markets_ignored(self):
        updown = make_contract("updown", semantics=DIRECTION, threshold=4400,
                               yes_bid=0.99, yes_ask=1.0)
        updown.threshold_low = updown.threshold_high = None
        touch = make_contract("pm4400", threshold=4400, yes_bid=0.10, yes_ask=0.12)
        self.assertEqual(find_opportunities([updown, touch]), [])

    def test_empty_books_ignored(self):
        low = make_contract("hit8k", threshold=8000, yes_bid=0.0, yes_ask=1.0)
        high = make_contract("hit12k", threshold=12000, yes_bid=0.040, yes_ask=0.041)
        self.assertEqual(find_opportunities([low, high]), [])

    def test_fees_can_kill_an_edge(self):
        # Gross edge 0.005 on two kalshi legs near 0.5 -> fees ~0.035 -> dropped.
        low = make_contract("k4400", platform="kalshi", semantics=SETTLE_ABOVE,
                            threshold=4400, window_start=JUL_END, window_end=JUL_END,
                            yes_bid=0.490, yes_ask=0.495)
        high = make_contract("k4500", platform="kalshi", semantics=SETTLE_ABOVE,
                             threshold=4500, window_start=JUL_END, window_end=JUL_END,
                             yes_bid=0.500, yes_ask=0.505)
        self.assertEqual(find_opportunities([low, high]), [])

    def test_min_net_edge_filter(self):
        low = make_contract("hit8k", threshold=8000, yes_bid=0.031, yes_ask=0.038)
        high = make_contract("hit12k", threshold=12000, yes_bid=0.040, yes_ask=0.041)
        self.assertEqual(find_opportunities([low, high], min_net_edge=0.01), [])


class FeeMathTest(unittest.TestCase):
    def test_kalshi_fee_formula(self):
        self.assertAlmostEqual(kalshi_fee(0.5), 0.0175, places=6)
        self.assertAlmostEqual(kalshi_fee(0.1), 0.07 * 0.1 * 0.9, places=6)
        self.assertEqual(kalshi_fee(1.0), 0.0)


if __name__ == "__main__":
    unittest.main()
