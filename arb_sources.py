#!/usr/bin/env python3
"""Gold arbitrage tracker (V3) - data layer.

Fetches every open gold-price market from Polymarket (gamma API) and Kalshi
(trade-api v2) and normalizes them into a single Contract shape so the
detection engine can compare listings across platforms. Exposes:
  - fetch_polymarket_contracts() -> list of Contract
  - fetch_kalshi_contracts()     -> list of Contract
  - fetch_all_contracts()        -> list of Contract

Both APIs are public for market data (no keys). Kalshi 429s under fast
pagination, so its calls are throttled to ~1 req/sec. Run this file directly
to print the normalized table.
"""
import json
import re
import time
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Optional

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events?tag_slug=gold&closed=false&limit=100"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
# Gold price series verified live on Kalshi 2026-07-05. New gold series must be
# added here (the events API has no text search worth trusting).
KALSHI_GOLD_SERIES = ["KXGOLDMON", "KXGOLDDIRY"]
KALSHI_THROTTLE_S = 1.0

# semantics values
SETTLE_ABOVE = "SETTLE_ABOVE"    # resolves YES if price > threshold_low at window_end
SETTLE_BELOW = "SETTLE_BELOW"    # resolves YES if price < threshold_high at window_end
SETTLE_RANGE = "SETTLE_RANGE"    # resolves YES if threshold_low <= price <= threshold_high
TOUCH_HIGH = "TOUCH_HIGH"        # resolves YES if price trades >= threshold_low any time in window
TOUCH_LOW = "TOUCH_LOW"          # resolves YES if price trades <= threshold_high any time in window
DIRECTION = "DIRECTION"          # up/down vs prior close; no threshold

# underlying values
XAU_SPOT = "XAU_SPOT"
GC_FUTURES = "GC_FUTURES"


@dataclass
class Contract:
    platform: str                     # "polymarket" | "kalshi"
    market_id: str
    event_id: str
    title: str
    url: str
    underlying: str                   # XAU_SPOT | GC_FUTURES
    settlement_source: str
    semantics: str
    threshold_low: Optional[float]
    threshold_high: Optional[float]
    window_start: Optional[str]       # ISO UTC; for settlement markets == window_end
    window_end: Optional[str]
    yes_bid: Optional[float]          # executable prices as probabilities 0-1
    yes_ask: Optional[float]
    no_bid: Optional[float]
    no_ask: Optional[float]
    last: Optional[float]
    volume_24h: Optional[float]
    mutually_exclusive: bool          # true if the event's markets form exclusive brackets
    fetched_at: str
    token_ids: Optional[list] = None  # Polymarket [yesToken, noToken]; None for Kalshi
    event_title: Optional[str] = None # human name of the parent event (for grouping)

    def has_quotes(self):
        """True if there is a real two-sided book (not an empty 0-bid/1-ask shell)."""
        return (
            self.yes_bid is not None and self.yes_ask is not None
            and self.yes_bid > 0 and self.yes_ask < 1
        )


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.7.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- Polymarket

_PM_TOUCH_RE = re.compile(r"hit \((HIGH|LOW)\) \$([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_PM_UPDOWN_RE = re.compile(r"up or down", re.IGNORECASE)


def _pm_underlying(text):
    if "(GC)" in text or "GC)" in text:
        return GC_FUTURES
    return XAU_SPOT  # XAUUSD and unlabeled gold questions are spot


def _pm_classify(question):
    """Map a Polymarket question to (semantics, threshold_low, threshold_high).
    Returns None for questions that aren't price markets we can compare."""
    m = _PM_TOUCH_RE.search(question)
    if m:
        threshold = float(m.group(2).replace(",", ""))
        if m.group(1).upper() == "HIGH":
            return TOUCH_HIGH, threshold, None
        return TOUCH_LOW, None, threshold
    if _PM_UPDOWN_RE.search(question):
        return DIRECTION, None, None
    return None


def fetch_polymarket_contracts():
    events = _get_json(GAMMA_EVENTS_URL)
    fetched_at = _now_iso()
    contracts = []
    skipped = []
    for event in events:
        slug = event.get("slug", "")
        url = "https://polymarket.com/event/" + slug
        event_title = event.get("title") or slug
        mutually_exclusive = bool(event.get("negRisk"))
        for m in event.get("markets", []):
            if m.get("closed"):
                continue
            question = m.get("question") or ""
            classified = _pm_classify(question)
            if classified is None:
                skipped.append(question)
                continue
            semantics, thr_low, thr_high = classified
            yes_bid = _to_float(m.get("bestBid"))
            yes_ask = _to_float(m.get("bestAsk"))
            last = None
            try:
                last = float(json.loads(m.get("outcomePrices") or "[]")[0])
            except (ValueError, IndexError, TypeError):
                pass
            end_date = m.get("endDate") or event.get("endDate")
            try:
                token_ids = json.loads(m.get("clobTokenIds") or "null")
            except (ValueError, TypeError):
                token_ids = None
            contracts.append(Contract(
                platform="polymarket",
                market_id=str(m.get("id")),
                event_id=slug,
                title=question,
                url=url,
                underlying=_pm_underlying(question),
                settlement_source="Polymarket resolution (see market rules)",
                semantics=semantics,
                threshold_low=thr_low,
                threshold_high=thr_high,
                # Touch markets: what matters for comparisons is the *remaining*
                # window, i.e. from now until the market's end date.
                window_start=fetched_at if semantics in (TOUCH_HIGH, TOUCH_LOW) else end_date,
                window_end=end_date,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=(1 - yes_ask) if yes_ask is not None else None,
                no_ask=(1 - yes_bid) if yes_bid is not None else None,
                last=last,
                volume_24h=_to_float(m.get("volume24hr")),
                mutually_exclusive=mutually_exclusive,
                fetched_at=fetched_at,
                token_ids=token_ids,
                event_title=event_title,
            ))
    if skipped:
        print("[polymarket] skipped %d non-price market(s): %s"
              % (len(skipped), "; ".join(skipped[:5])))
    return contracts


# -------------------------------------------------------------------- Kalshi


def _kalshi_get(path):
    time.sleep(KALSHI_THROTTLE_S)
    return _get_json(KALSHI_BASE + path)


def _slugify(title):
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]", "-", title.lower())).strip("-")


def _kalshi_series_info(series_ticker):
    """(settlement_source, web-url title slug) from the series endpoint."""
    source, slug = "Kalshi settlement", None
    try:
        series = _kalshi_get("/series/" + series_ticker).get("series", {})
        sources = [s.get("name", "") for s in series.get("settlement_sources", [])]
        if sources:
            source = "Kalshi via " + ", ".join(filter(None, sources))
        if series.get("title"):
            slug = _slugify(series["title"])
    except Exception as exc:
        print("[kalshi] series %s lookup failed: %s" % (series_ticker, exc))
    return source, slug


def _kalshi_url(series_ticker, series_slug, event_ticker):
    """Deep link to the event page: /markets/{series}/{title-slug}/{event}
    (format confirmed against indexed kalshi.com pages)."""
    if not series_slug:
        return "https://kalshi.com/markets/%s" % series_ticker.lower()
    return "https://kalshi.com/markets/%s/%s/%s" % (
        series_ticker.lower(), series_slug, event_ticker.lower())


def _kalshi_classify(market):
    strike_type = market.get("strike_type")
    floor = _to_float(market.get("floor_strike"))
    cap = _to_float(market.get("cap_strike"))
    if strike_type == "greater":
        return SETTLE_ABOVE, floor, None
    if strike_type == "less":
        return SETTLE_BELOW, None, cap
    if strike_type == "between":
        return SETTLE_RANGE, floor, cap
    return None


def fetch_kalshi_contracts():
    fetched_at = _now_iso()
    contracts = []
    for series_ticker in KALSHI_GOLD_SERIES:
        source, series_slug = _kalshi_series_info(series_ticker)
        try:
            events = _kalshi_get(
                "/events?series_ticker=%s&status=open&limit=200" % series_ticker
            ).get("events", [])
        except Exception as exc:
            print("[kalshi] event list for %s failed: %s" % (series_ticker, exc))
            continue
        for event in events:
            event_ticker = event["event_ticker"]
            event_title = event.get("title") or event_ticker
            markets = []
            cursor = ""
            while True:
                path = "/markets?event_ticker=%s&limit=1000" % event_ticker
                if cursor:
                    path += "&cursor=" + cursor
                data = _kalshi_get(path)
                markets.extend(data.get("markets", []))
                cursor = data.get("cursor") or ""
                if not cursor:
                    break
            for m in markets:
                if m.get("status") != "active":
                    continue
                classified = _kalshi_classify(m)
                if classified is None:
                    print("[kalshi] skipped %s (strike_type=%s)"
                          % (m.get("ticker"), m.get("strike_type")))
                    continue
                semantics, thr_low, thr_high = classified
                close_time = m.get("close_time")
                contracts.append(Contract(
                    platform="kalshi",
                    market_id=m["ticker"],
                    event_id=event_ticker,
                    title="%s — %s" % (event.get("title", ""), m.get("yes_sub_title", "")),
                    url=_kalshi_url(series_ticker, series_slug, event_ticker),
                    underlying=XAU_SPOT,  # Kalshi gold series settle on spot gold candles
                    settlement_source=source,
                    semantics=semantics,
                    threshold_low=thr_low,
                    threshold_high=thr_high,
                    window_start=close_time,
                    window_end=close_time,
                    yes_bid=_to_float(m.get("yes_bid_dollars")),
                    yes_ask=_to_float(m.get("yes_ask_dollars")),
                    no_bid=_to_float(m.get("no_bid_dollars")),
                    no_ask=_to_float(m.get("no_ask_dollars")),
                    last=_to_float(m.get("last_price_dollars")),
                    volume_24h=_to_float(m.get("volume_24h_fp") or m.get("volume_24h")),
                    mutually_exclusive=bool(event.get("mutually_exclusive")),
                    fetched_at=fetched_at,
                    event_title=event_title,
                ))
    return contracts


# ---------------------------------------------------------------------- main


def fetch_all_contracts():
    return fetch_polymarket_contracts() + fetch_kalshi_contracts()


# ------------------------------------------------------- order-book depth

CLOB_BOOK_URL = "https://clob.polymarket.com/book"


def _kalshi_ask_ladder(ticker, action):
    """Ask ladder (best price first) for one side of a Kalshi market. Kalshi's
    orderbook lists resting *bids*; the ask you take is the complement of the
    opposite side's bids (a NO bid at p offers YES at 1-p, and vice-versa)."""
    book = _kalshi_get("/markets/%s/orderbook" % ticker).get("orderbook_fp") or {}
    opposite = book.get("no_dollars" if action == "BUY YES" else "yes_dollars") or []
    # opposite is ascending by bid price; highest bid = cheapest ask for us.
    ladder = [(round(1 - float(price), 4), float(size))
              for price, size in opposite]
    ladder.sort(key=lambda level: level[0])
    return ladder


def _polymarket_ask_ladder(token_ids, action):
    """Ask ladder (best price first) for one side of a Polymarket market:
    the asks of the YES token (BUY YES) or the NO token (BUY NO)."""
    if not token_ids or len(token_ids) < 2:
        return []
    token = token_ids[0] if action == "BUY YES" else token_ids[1]
    data = _get_json("%s?token_id=%s" % (CLOB_BOOK_URL, token))
    ladder = [(float(level["price"]), float(level["size"]))
              for level in (data.get("asks") or [])]
    ladder.sort(key=lambda level: level[0])
    return ladder


def fetch_ask_ladder(contract, action):
    """Executable ask ladder (list of (price, size), cheapest first) for buying
    `action` ("BUY YES" | "BUY NO") of `contract`. Prices are probability
    dollars. Returns [] on any failure or empty book (best-effort)."""
    try:
        if contract.platform == "kalshi":
            return _kalshi_ask_ladder(contract.market_id, action)
        return _polymarket_ask_ladder(contract.token_ids, action)
    except Exception as exc:
        print("[book] %s %s failed: %s" % (contract.platform, contract.market_id, exc))
        return []


def _fmt(value):
    return "-" if value is None else ("%.3f" % value)


def _print_table(contracts):
    contracts = sorted(
        contracts,
        key=lambda c: (c.underlying, c.window_end or "", c.platform,
                       c.threshold_low or c.threshold_high or 0),
    )
    header = ("%-10s %-12s %-13s %9s %9s | %7s %7s %7s %7s | %-16s %s"
              % ("platform", "underlying", "semantics", "thr_low", "thr_high",
                 "yes_bid", "yes_ask", "no_bid", "no_ask", "window_end", "event"))
    print(header)
    print("-" * len(header))
    for c in contracts:
        print("%-10s %-12s %-13s %9s %9s | %7s %7s %7s %7s | %-16s %s"
              % (c.platform, c.underlying, c.semantics,
                 "-" if c.threshold_low is None else "%.0f" % c.threshold_low,
                 "-" if c.threshold_high is None else "%.0f" % c.threshold_high,
                 _fmt(c.yes_bid), _fmt(c.yes_ask), _fmt(c.no_bid), _fmt(c.no_ask),
                 (c.window_end or "-")[:16], c.event_id))
    quoted = sum(1 for c in contracts if c.has_quotes())
    print("\n%d contracts (%d with a live two-sided book)" % (len(contracts), quoted))


if __name__ == "__main__":
    _print_table(fetch_all_contracts())
