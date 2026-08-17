"""Tests for the market-brief agent. No network, no model calls.

Covers the parts that decide whether we spend money (change-gating) and the
parts that decide what the page shows (fallback behaviour). The agent's prose
is not testable; its plumbing is.

Run:  ./venv/bin/python3 -m unittest test_arb_agent
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import arb_agent
from agent_kit import LLMUnavailable
from arb_engine import Leg, Opportunity
from test_arb_engine import make_contract


def make_opportunity(net_edge=0.05, market_id="m1", action="BUY YES"):
    contract = make_contract(market_id=market_id)
    return Opportunity(
        kind="ladder",
        description="test opportunity",
        legs=[Leg(contract=contract, action=action, price=0.4, fee=0.01)],
        gross_edge=net_edge + 0.01,
        fees=0.01,
        net_edge=net_edge,
        expires_at=None,
        expiring_soon=False,
    )


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FingerprintTest(unittest.TestCase):
    def test_stable_across_calls(self):
        opps = [make_opportunity()]
        self.assertEqual(arb_agent.opportunity_fingerprint(opps),
                         arb_agent.opportunity_fingerprint(opps))

    def test_ordering_does_not_matter(self):
        a = make_opportunity(market_id="m1")
        b = make_opportunity(market_id="m2")
        self.assertEqual(arb_agent.opportunity_fingerprint([a, b]),
                         arb_agent.opportunity_fingerprint([b, a]))

    def test_changes_when_edge_moves_materially(self):
        self.assertNotEqual(
            arb_agent.opportunity_fingerprint([make_opportunity(0.05)]),
            arb_agent.opportunity_fingerprint([make_opportunity(0.09)]))

    def test_ignores_sub_thousandth_noise(self):
        # Rounded to 3dp, so book jitter alone must not trigger a rewrite.
        self.assertEqual(
            arb_agent.opportunity_fingerprint([make_opportunity(0.050001)]),
            arb_agent.opportunity_fingerprint([make_opportunity(0.050002)]))

    def test_changes_when_a_leg_changes(self):
        self.assertNotEqual(
            arb_agent.opportunity_fingerprint([make_opportunity(action="BUY YES")]),
            arb_agent.opportunity_fingerprint([make_opportunity(action="BUY NO")]))

    def test_empty_set_is_a_valid_fingerprint(self):
        self.assertTrue(arb_agent.opportunity_fingerprint([]))


class StalenessTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
        self.fp = "abc123"

    def cached(self, age_hours=0, fingerprint=None):
        return {"fingerprint": fingerprint or self.fp,
                "generated_at": iso(self.now - timedelta(hours=age_hours))}

    def test_no_cache_is_stale(self):
        self.assertTrue(arb_agent.is_stale(None, self.fp, now=self.now))

    def test_fresh_and_unchanged_is_not_stale(self):
        # The money-saving case: 48 builds a day, one model call.
        self.assertFalse(arb_agent.is_stale(
            self.cached(age_hours=1), self.fp, now=self.now, max_age_hours=6))

    def test_changed_opportunities_is_stale(self):
        self.assertTrue(arb_agent.is_stale(
            self.cached(age_hours=1), "different", now=self.now,
            max_age_hours=6))

    def test_aged_out_is_stale(self):
        self.assertTrue(arb_agent.is_stale(
            self.cached(age_hours=7), self.fp, now=self.now, max_age_hours=6))

    def test_just_under_the_age_limit_is_fresh(self):
        self.assertFalse(arb_agent.is_stale(
            self.cached(age_hours=5.9), self.fp, now=self.now,
            max_age_hours=6))

    def test_corrupt_timestamp_is_stale(self):
        self.assertTrue(arb_agent.is_stale(
            {"fingerprint": self.fp, "generated_at": "not-a-date"},
            self.fp, now=self.now))

    def test_missing_timestamp_is_stale(self):
        self.assertTrue(arb_agent.is_stale(
            {"fingerprint": self.fp}, self.fp, now=self.now))


class BriefIOTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "brief.json"
            brief = {"headline": "h", "body": "b", "sources": [],
                     "generated_at": "2026-08-17T00:00:00Z",
                     "fingerprint": "x", "model": "m"}
            arb_agent.save_brief(brief, path)
            self.assertEqual(arb_agent.load_brief(path), brief)

    def test_missing_file_returns_none(self):
        self.assertIsNone(arb_agent.load_brief(Path("/nonexistent/brief.json")))

    def test_corrupt_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "brief.json"
            path.write_text("{ this is not json")
            self.assertIsNone(arb_agent.load_brief(path))


class GetBriefTest(unittest.TestCase):
    """get_brief must never raise -- a failed brief cannot fail the build."""

    def setUp(self):
        self._real = arb_agent.generate_brief
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "brief.json"
        self.calls = []

    def tearDown(self):
        arb_agent.generate_brief = self._real
        self.tmp.cleanup()

    def _stub(self, result=None, error=None):
        def fn(contracts, opportunities, fingerprint=None):
            self.calls.append(fingerprint)
            if error:
                raise error
            return dict(result, fingerprint=fingerprint)
        arb_agent.generate_brief = fn

    def test_generates_and_saves_when_no_cache(self):
        self._stub({"headline": "h", "body": "b", "sources": [],
                    "generated_at": iso(datetime.now(timezone.utc)),
                    "model": "m"})
        brief = arb_agent.get_brief([], [], path=self.path)
        self.assertEqual(brief["headline"], "h")
        self.assertTrue(self.path.exists())
        self.assertEqual(len(self.calls), 1)

    def test_cache_hit_makes_no_model_call(self):
        fp = arb_agent.opportunity_fingerprint([])
        arb_agent.save_brief({
            "headline": "cached", "body": "b", "sources": [],
            "generated_at": iso(datetime.now(timezone.utc)),
            "fingerprint": fp, "model": "m"}, self.path)
        self._stub({"headline": "fresh"})
        brief = arb_agent.get_brief([], [], path=self.path)
        self.assertEqual(brief["headline"], "cached")
        self.assertEqual(self.calls, [], "must not pay when nothing changed")

    def test_falls_back_to_stale_cache_on_failure(self):
        arb_agent.save_brief({
            "headline": "old", "body": "b", "sources": [],
            "generated_at": iso(datetime.now(timezone.utc)
                                - timedelta(hours=99)),
            "fingerprint": "stale", "model": "m"}, self.path)
        self._stub(error=LLMUnavailable("no key"))
        brief = arb_agent.get_brief([], [], path=self.path)
        self.assertEqual(brief["headline"], "old")
        self.assertTrue(brief["stale"], "served-stale must be marked")

    def test_returns_none_when_failing_with_no_cache(self):
        self._stub(error=LLMUnavailable("no key"))
        self.assertIsNone(arb_agent.get_brief([], [], path=self.path))

    def test_never_raises(self):
        self._stub(error=LLMUnavailable("boom"))
        try:
            arb_agent.get_brief([], [], path=self.path)
        except Exception as e:  # noqa: BLE001
            self.fail("get_brief raised %r -- it must never fail the build" % e)


class ToolPayloadTest(unittest.TestCase):
    """The tools are what the model actually sees. Shape matters."""

    def test_opportunities_tool_serializes_legs(self):
        tools = arb_agent.build_tools([], [make_opportunity()])
        payload = json.loads(tools[0].func())
        self.assertEqual(payload["count"], 1)
        leg = payload["opportunities"][0]["legs"][0]
        self.assertIn("action", leg)
        self.assertIn("platform", leg)
        self.assertIn("market", leg)

    def test_empty_opportunities_is_valid(self):
        tools = arb_agent.build_tools([], [])
        payload = json.loads(tools[0].func())
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["opportunities"], [])

    def test_prices_tool_skips_contracts_without_quotes(self):
        quoted = make_contract(market_id="q", yes_bid=0.4, yes_ask=0.45)
        unquoted = make_contract(market_id="u", yes_bid=0.0, yes_ask=1.0)
        tools = arb_agent.build_tools([quoted, unquoted], [])
        payload = json.loads(tools[1].func())
        ids = [r["title"] for r in payload["contracts"]]
        self.assertEqual(payload["count"], 1, ids)

    def test_prices_tool_caps_payload_size(self):
        many = [make_contract(market_id="m%d" % i, yes_bid=0.4, yes_ask=0.45)
                for i in range(arb_agent.MAX_CONTRACTS_IN_TOOL + 25)]
        tools = arb_agent.build_tools(many, [])
        payload = json.loads(tools[1].func())
        self.assertTrue(payload["truncated"])
        self.assertEqual(len(payload["contracts"]),
                         arb_agent.MAX_CONTRACTS_IN_TOOL)

    def test_web_search_is_a_server_tool(self):
        tools = arb_agent.build_tools([], [])
        self.assertEqual(tools[2]["type"], "web_search_20260209")


if __name__ == "__main__":
    unittest.main()
