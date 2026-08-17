"""
Agent B -- the gold market brief.

This is a real agent, unlike the job scorer. It gets three tools and decides
for itself which to call, in what order, and when it has enough to write:

  get_opportunities()   local  -- what the arb engine found this run
  get_market_prices()   local  -- the normalized contract table
  web_search            server -- runs on Anthropic's infrastructure, so
                                  there is no search API key to manage

The loop is: model picks a tool -> our code (or Anthropic's) runs it ->
result goes back -> repeat until the model stops asking. agent_kit.run_agent
prints each step so you can watch it happen.

Two calls, deliberately:
  1. run_agent(...) with tools -> the model researches and writes prose.
  2. ask_json(...) on that prose -> clean {headline, body, sources} for the
     renderer. Keeping the tool loop separate from the formatting keeps both
     simple.

Cost control: arb.yml runs every 30 minutes -- 48 builds a day. Regenerating
the brief every time would be wasteful and would mostly rewrite the same
paragraph. `get_brief` only calls the model when the opportunity set actually
changed or the cached brief has gone stale, which cuts it to a handful a day.

Run it standalone to watch the loop:
    ./venv/bin/python3 arb_agent.py
"""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent_kit
from agent_kit import LLMUnavailable, ask_json, run_agent, text_of

ROOT = Path(__file__).resolve().parent
BRIEF_FILE = ROOT / "data" / "arb_brief.json"

BRIEF_ENABLED = os.environ.get("BRIEF_ENABLED", "1") == "1"
# Regenerate at least this often even when nothing changed, so the brief never
# reads as though it were written days ago.
MAX_AGE_HOURS = float(os.environ.get("BRIEF_MAX_AGE_HOURS", "6"))

# Keep the tool payloads small -- these are billed as input tokens on every
# turn of the loop.
MAX_CONTRACTS_IN_TOOL = 60

SYSTEM = """\
You write a short daily brief for a public gold-markets dashboard. The page \
already shows the raw numbers; your job is the part a table cannot do -- say \
what is going on and why.

You have three tools. Use get_opportunities and get_market_prices first to see \
the current state. Use web_search only when the market data raises a question \
you cannot answer from the numbers alone -- for example a large move, an \
unusual spread, or an opportunity that looks too good and probably has a \
mundane explanation. Two or three searches is plenty.

Write 2-3 short paragraphs of plain prose. Rules:

  - Lead with what actually changed or what stands out today.
  - Be specific: name contracts, quote prices and dates.
  - If nothing is unusual, say so plainly and briefly. A quiet day is a \
legitimate finding and much better than manufactured drama.
  - Explain mechanics when they matter. Most readers do not know the \
difference between a settle market and a touch market.
  - Flag likely explanations for apparent edges (thin books, different \
underlyings, resolution-rule differences) rather than implying free money.
  - No financial advice, no price predictions, no recommendations to trade.
  - No headings, no bullet lists, no markdown. Plain paragraphs only.
  - Every claim that came from web search must carry its full URL in \
parentheses immediately after the claim, e.g. "gold set a record on Friday \
(https://example.com/article)". Naming the publication without the URL is \
not enough -- the page turns these into links, and a citation with no URL is \
silently dropped.
  - Keep the whole brief under 1,800 characters. Three paragraphs maximum.
"""

USER = """\
Write today's brief for the gold arbitrage tracker. Check the current \
opportunities and market prices first, then search for news only if something \
in the data needs explaining.
"""

FORMAT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "Under 90 characters. States the single most "
                           "important observation. No trailing period.",
        },
        "body": {
            "type": "string",
            "description": "The brief itself, as plain-text paragraphs "
                           "separated by blank lines. No markdown.",
        },
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["title", "url"],
                "additionalProperties": False,
            },
            "description": "Any web pages actually cited. Empty if the brief "
                           "was written from market data alone.",
        },
    },
    "required": ["headline", "body", "sources"],
    "additionalProperties": False,
}

FORMAT_SYSTEM = """\
You reformat a finished market brief into JSON. Do not rewrite, summarize, or \
add to the prose -- copy it across as-is, with three exceptions:

  1. Strip any markdown.
  2. Move every URL out of the body and into `sources`. The body must contain \
no bare URLs -- they are rendered as a separate Sources row underneath. Where \
a URL was cited inline, leave the surrounding sentence intact and simply \
remove the URL and its brackets or parentheses, keeping the publication name \
if one was given.
  3. Remove quotation marks around plain figures and dates (write $4,400 per \
troy ounce on Monday, not "$4,400 per troy ounce on Monday"). Keep quotation \
marks that mark a genuine term of art, like a "touch" contract.

Extract a headline from the brief's main point.
"""


# --- Tools ------------------------------------------------------------------

def build_tools(contracts, opportunities):
    """Build the tool list around data this run already fetched.

    The tools are closures rather than fresh fetches on purpose: build_arb has
    already paid for the HTTP calls (and Kalshi's 1s-per-request throttle), so
    re-fetching inside the tool would double the slowest part of the build.
    """
    from anthropic import beta_tool

    @beta_tool
    def get_opportunities() -> str:
        """Get the arbitrage opportunities the scanner found in this run.

        Returns JSON: each opportunity's kind, plain-English description, net
        edge per contract (after fees), how many contracts the live order book
        could actually fill, total capturable profit, expiry, and the
        individual legs to buy. An empty list is normal and common -- most
        scans find nothing.
        """
        out = []
        for o in opportunities:
            out.append({
                "kind": o.kind,
                "description": o.description,
                "net_edge_per_contract": round(o.net_edge, 4),
                "gross_edge": round(o.gross_edge, 4),
                "fees": round(o.fees, 4),
                "max_contracts": o.max_contracts,
                "capturable_dollars": (round(o.capturable, 2)
                                       if o.capturable is not None else None),
                "expires_at": o.expires_at,
                "expiring_soon": o.expiring_soon,
                "legs": [{
                    "action": l.action,
                    "platform": l.contract.platform,
                    "market": l.contract.title,
                    "price": round(l.price, 4),
                } for l in o.legs],
            })
        return json.dumps({"count": len(out), "opportunities": out})

    @beta_tool
    def get_market_prices(platform: str = "all") -> str:
        """Get current prices for live gold contracts on both platforms.

        Args:
            platform: "polymarket", "kalshi", or "all" (default).

        Returns JSON per contract: title, which platform, what it tracks
        (XAU_SPOT is spot gold, GC_FUTURES is the futures contract), its
        semantics (settle_above/settle_below/touch_high/touch_low -- "settle"
        means the price at a specific timestamp, "touch" means it trades
        through the level at any point), the strike thresholds, the window end
        date, and YES bid/ask as probabilities between 0 and 1. Only contracts
        with live quotes are included.
        """
        rows = []
        for c in contracts:
            if platform != "all" and c.platform != platform:
                continue
            if not c.has_quotes():
                continue
            rows.append({
                "platform": c.platform,
                "title": c.title,
                "underlying": c.underlying,
                "semantics": c.semantics,
                "threshold_low": c.threshold_low,
                "threshold_high": c.threshold_high,
                "window_end": c.window_end,
                "yes_bid": c.yes_bid,
                "yes_ask": c.yes_ask,
                "volume_24h": c.volume_24h,
            })
        rows.sort(key=lambda r: (r["platform"], r["threshold_low"] or 0))
        truncated = len(rows) > MAX_CONTRACTS_IN_TOOL
        return json.dumps({
            "count": len(rows),
            "truncated": truncated,
            "contracts": rows[:MAX_CONTRACTS_IN_TOOL],
        })

    web_search = {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 4,
    }

    return [get_opportunities, get_market_prices, web_search]


# --- Change gating ----------------------------------------------------------

def opportunity_fingerprint(opportunities):
    """A stable hash of the current opportunity set.

    Same legs at the same edge (to 3dp) => same fingerprint => no need to pay
    for a new brief. Sorted so ordering changes alone don't trigger a rewrite.
    """
    parts = sorted(
        "%s@%.3f" % (
            "|".join(sorted("%s:%s:%s" % (l.contract.platform,
                                          l.contract.market_id, l.action)
                            for l in o.legs)),
            o.net_edge,
        )
        for o in opportunities
    )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def load_brief(path=BRIEF_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (IOError, ValueError):
        return None


def save_brief(brief, path=BRIEF_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(brief, f, indent=2, sort_keys=True)
        f.write("\n")


def is_stale(cached, fingerprint, now=None, max_age_hours=None):
    """Should we spend a model call? True if the opportunity set changed or
    the cached brief has aged out."""
    if not cached:
        return True
    if cached.get("fingerprint") != fingerprint:
        return True
    max_age = MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    now = now or datetime.now(timezone.utc)
    try:
        generated = datetime.fromisoformat(
            cached["generated_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return True
    return now - generated > timedelta(hours=max_age)


# --- Generation -------------------------------------------------------------

def generate_brief(contracts, opportunities, fingerprint=None):
    """Run the agent, then format its prose. Raises LLMUnavailable on failure."""
    tools = build_tools(contracts, opportunities)

    final = run_agent(SYSTEM, USER, tools, max_tokens=8192, max_iterations=12)
    prose = text_of(final)
    if not prose:
        raise LLMUnavailable("agent produced no prose")

    formatted = ask_json(
        system=FORMAT_SYSTEM,
        user=prose,
        schema=FORMAT_SCHEMA,
        max_tokens=4096,
        effort="low",
    )

    return {
        "headline": formatted["headline"],
        "body": formatted["body"],
        "sources": formatted["sources"],
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "fingerprint": fingerprint or opportunity_fingerprint(opportunities),
        "model": agent_kit.MODEL,
    }


def get_brief(contracts, opportunities, path=BRIEF_FILE):
    """The entry point build_arb calls.

    Returns a brief dict, or None if there has never been one and the model is
    unavailable. Never raises -- a failed brief must not fail the page build.
    """
    cached = load_brief(path)
    if not BRIEF_ENABLED:
        print("[brief] disabled (BRIEF_ENABLED=0)")
        return cached

    fingerprint = opportunity_fingerprint(opportunities)
    if not is_stale(cached, fingerprint):
        print("[brief] cache hit (%s, unchanged since %s) -- no model call"
              % (fingerprint, cached.get("generated_at")))
        return cached

    print("[brief] regenerating (fingerprint %s)" % fingerprint)
    try:
        brief = generate_brief(contracts, opportunities, fingerprint)
    except LLMUnavailable as e:
        print("[brief] unavailable (%s)" % e)
        if cached:
            # Serve the old one, flagged, rather than dropping the section.
            cached = dict(cached, stale=True)
        return cached
    save_brief(brief, path)
    print("[brief] wrote %s" % path)
    return brief


if __name__ == "__main__":
    import arb_engine
    import arb_sources

    print("Fetching market data (Kalshi throttles at 1 req/s, ~8s)...")
    _contracts = arb_sources.fetch_all_contracts()
    _opps = arb_engine.find_opportunities(_contracts)
    print("%d contracts, %d opportunities\n" % (len(_contracts), len(_opps)))

    _brief = get_brief(_contracts, _opps)
    if not _brief:
        raise SystemExit("no brief produced")
    print("\n" + "=" * 70)
    print(_brief["headline"])
    print("=" * 70)
    print(_brief["body"])
    for s in _brief.get("sources", []):
        print("  - %s  %s" % (s["title"], s["url"]))
