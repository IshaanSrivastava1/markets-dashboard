#!/usr/bin/env python3
"""Gold arbitrage tracker (V3) - page builder.

Entry point for the 30-minute GitHub Actions workflow (arb.yml). Fetches all
live gold contracts from Polymarket + Kalshi (arb_sources), runs the detection
engine (arb_engine), and writes:
  - docs/arb.html        the rendered page (linked from the dashboard)
  - data/arb_current.csv snapshot of currently open opportunities
  - data/arb_log.csv     append-only history: first sighting of each opportunity

Run locally: venv/bin/python3 build_arb.py && open docs/arb.html
"""
import csv
import html
from datetime import datetime, timezone
from pathlib import Path

import arb_engine
import arb_sources

BASE_DIR = Path(__file__).resolve().parent
OUT_FILE = BASE_DIR / "docs" / "arb.html"
CURRENT_FILE = BASE_DIR / "data" / "arb_current.csv"
LOG_FILE = BASE_DIR / "data" / "arb_log.csv"

MARGINAL_EDGE = 0.01  # below 1c/contract: real but likely not worth the effort

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Gold Arbitrage Tracker - Polymarket vs Kalshi</title>
  <meta name="description" content="Auto-updating scan of Polymarket and Kalshi gold markets for cross-platform arbitrage and mispricings. Refreshed every 30 minutes via GitHub Actions."/>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; background: #0a0a0a; color: #e5e5e5;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      line-height: 1.5;
    }}
    header {{ padding: 32px 20px 8px; max-width: 1100px; margin: 0 auto; }}
    header h1 {{ margin: 0 0 4px; font-size: 1.8rem; }}
    header p {{ margin: 0; color: #888; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
    section {{
      background: #111; border: 1px solid #222; border-radius: 12px;
      padding: 20px; margin-bottom: 28px;
    }}
    section h2 {{ margin-top: 0; }}
    .tablewrap {{ overflow-x: auto; }}
    table {{ border-collapse: collapse; margin: 8px 0; font-size: 0.9rem; width: 100%; }}
    th, td {{ text-align: left; padding: 5px 12px 5px 0; vertical-align: top; }}
    th {{ color: #888; font-weight: 600; border-bottom: 1px solid #222; }}
    tr.divider td {{ border-top: 1px solid #222; }}
    .muted {{ color: #888; font-size: 0.9rem; }}
    .up {{ color: #34d399; }}
    .warn {{ color: #fbbf24; }}
    .down {{ color: #f87171; }}
    .pill {{
      display: inline-block; padding: 1px 8px; border-radius: 999px;
      font-size: 0.78rem; border: 1px solid #333; color: #aaa; white-space: nowrap;
    }}
    .edge {{ font-size: 1.05rem; font-weight: 700; }}
    a {{ color: #60a5fa; }}
    .empty {{ color: #888; padding: 12px 0; }}
    footer {{ max-width: 1100px; margin: 0 auto; padding: 8px 20px 40px; color: #666; font-size: 0.85rem; }}
    .explain {{ color: #aaa; font-size: 0.92rem; max-width: 760px; }}
  </style>
</head>
<body>
  <header>
    <h1>Gold Arbitrage Tracker <span class="muted">V3</span></h1>
    <p>Polymarket &times; Kalshi &middot; refreshed every 30 minutes via GitHub Actions &middot;
       last updated {updated} UTC &middot; <a href="index.html">back to dashboard</a></p>
  </header>
  <main>
    <section>
      <h2>Live opportunities</h2>
      <p class="explain">Each row is a set of trades that logically <em>cannot</em> pay out
      less than its guaranteed amount at resolution: e.g. gold hitting $12,000 requires it
      to hit $10,000 first, so "hit $10,000" can never be worth less than "hit $12,000".
      When the combined price of such a set drops below its guaranteed payout, that gap is
      the edge. Kalshi taker fees (0.07&times;P&times;(1&minus;P) per contract) are already
      subtracted; Polymarket charges no trading fee.</p>
      {opportunities}
    </section>
    <section>
      <h2>All live gold contracts ({n_contracts})</h2>
      <p class="explain">Every open gold-price market on both platforms, normalized to one
      schema. <strong>Settle</strong> = price at a specific timestamp;
      <strong>touch</strong> = trades through the level at any point in the window.
      Prices are YES bid/ask as probabilities.</p>
      <div class="tablewrap">{contracts_table}</div>
    </section>
  </main>
  <footer>
    Data: Polymarket gamma API and Kalshi trade-api v2 (both public market data).
    Edges are computed from top-of-book quotes only - order book depth, slippage beyond
    the touch, and Kalshi's per-order fee rounding are not modeled, so treat small edges
    as indicative. Personal/informational use, not financial advice.
  </footer>
</body>
</html>
"""


def _fmt_pct(value):
    return "-" if value is None else "%.1f%%" % (value * 100)


def _opportunity_rows(opportunities):
    if not opportunities:
        return ("<p class='empty'>No opportunities with a positive net edge right now. "
                "That is the normal state - real arbitrage is rare and small. The scan "
                "keeps running every 30 minutes.</p>")
    rows = []
    for o in opportunities:
        legs = "<br/>".join(
            "%s &middot; <a href='%s'>%s</a> @ %.3f <span class='pill'>%s</span>" % (
                html.escape(l.action), html.escape(l.contract.url),
                html.escape(l.contract.title), l.price, l.contract.platform)
            for l in o.legs)
        edge_class = "up" if o.net_edge >= MARGINAL_EDGE else "warn"
        marginal = "" if o.net_edge >= MARGINAL_EDGE else "<div class='muted'>marginal</div>"
        flags = []
        if o.expiring_soon:
            flags.append("<span class='down'>expires &lt;48h</span>")
        flags.extend("<div class='muted'>%s</div>" % html.escape(c) for c in o.caveats)
        rows.append(
            "<tr class='divider'><td><span class='pill'>%s</span></td><td>%s</td>"
            "<td class='edge %s'>$%.4f%s</td><td>%s<br/><span class='muted'>fees $%.4f"
            "</span></td><td>%s<br/>%s</td></tr>"
            % (o.kind, legs, edge_class, o.net_edge, marginal,
               "$%.4f" % o.gross_edge, o.fees,
               html.escape(o.expires_at or "-"), "".join(flags)))
    return ("<div class='tablewrap'><table><tr><th>Type</th><th>Trades (per $1 contract)"
            "</th><th>Net edge</th><th>Gross / fees</th><th>Resolves by</th></tr>%s"
            "</table></div>" % "".join(rows))


def _contracts_table(contracts):
    contracts = sorted(
        contracts,
        key=lambda c: (c.underlying, c.window_end or "", c.platform,
                       c.threshold_low or c.threshold_high or 0))
    rows = []
    prev_group = None
    for c in contracts:
        group = (c.underlying, c.window_end)
        divider = " class='divider'" if group != prev_group and prev_group else ""
        prev_group = group
        threshold = c.threshold_low if c.threshold_low is not None else c.threshold_high
        semantics = c.semantics.replace("_", " ").lower()
        stale = "" if c.has_quotes() else " <span class='down'>(no book)</span>"
        rows.append(
            "<tr%s><td><span class='pill'>%s</span></td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s / %s%s</td><td class='muted'>%s</td>"
            "<td><a href='%s'>open</a></td></tr>"
            % (divider, c.platform, c.underlying.replace("_", " "),
               html.escape(semantics),
               "-" if threshold is None else "$%s" % format(int(threshold), ","),
               _fmt_pct(c.yes_bid), _fmt_pct(c.yes_ask), stale,
               (c.window_end or "-")[:16].replace("T", " "),
               html.escape(c.url)))
    return ("<table><tr><th>Platform</th><th>Underlying</th><th>Type</th>"
            "<th>Level</th><th>YES bid/ask</th><th>Window ends</th><th></th></tr>%s"
            "</table>" % "".join(rows))


def _fingerprint(opportunity):
    return "|".join(sorted(
        "%s:%s:%s" % (l.contract.platform, l.contract.market_id, l.action)
        for l in opportunity.legs))


def _write_csvs(opportunities, now_iso):
    """Overwrite the current-opportunity snapshot; append first sightings to the log."""
    previous = set()
    if CURRENT_FILE.exists():
        with CURRENT_FILE.open() as f:
            previous = {row["fingerprint"] for row in csv.DictReader(f)}

    fields = ["seen_at", "fingerprint", "kind", "net_edge", "gross_edge", "fees",
              "expires_at", "description"]

    def row(o):
        return {"seen_at": now_iso, "fingerprint": _fingerprint(o), "kind": o.kind,
                "net_edge": o.net_edge, "gross_edge": o.gross_edge, "fees": o.fees,
                "expires_at": o.expires_at or "", "description": o.description}

    CURRENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CURRENT_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(row(o) for o in opportunities)

    new = [o for o in opportunities if _fingerprint(o) not in previous]
    if new:
        write_header = not LOG_FILE.exists()
        with LOG_FILE.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if write_header:
                writer.writeheader()
            writer.writerows(row(o) for o in new)
    return len(new)


def main():
    contracts = arb_sources.fetch_all_contracts()
    opportunities = arb_engine.find_opportunities(contracts)
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    html_out = PAGE_TEMPLATE.format(
        updated=now.strftime("%Y-%m-%d %H:%M"),
        opportunities=_opportunity_rows(opportunities),
        n_contracts=len(contracts),
        contracts_table=_contracts_table(contracts),
    )
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(html_out)

    new_count = _write_csvs(opportunities, now_iso)
    print("Wrote %s (%d contracts, %d opportunities, %d newly seen)"
          % (OUT_FILE, len(contracts), len(opportunities), new_count))


if __name__ == "__main__":
    main()
