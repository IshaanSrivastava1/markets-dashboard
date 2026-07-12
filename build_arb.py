#!/usr/bin/env python3
"""Gold arbitrage tracker (V3.5) - page builder.

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

import plotly.graph_objects as go
from plotly.subplots import make_subplots

import arb_engine
import arb_sources
from arb_engine import _parse_iso
from arb_sources import GC_FUTURES, SETTLE_ABOVE, TOUCH_HIGH, TOUCH_LOW

BASE_DIR = Path(__file__).resolve().parent
OUT_FILE = BASE_DIR / "docs" / "arb.html"
CURRENT_FILE = BASE_DIR / "data" / "arb_current.csv"
LOG_FILE = BASE_DIR / "data" / "arb_log.csv"

MARGINAL_EDGE = 0.01  # below 1c/contract: real but likely not worth the effort

# The dashboard's hue family, stepped darker for the dark surface and ordered
# to maximize adjacent color-vision-deficiency separation. Validated (dataviz
# six checks) on #0a0a0a: lightness band, chroma, CVD >= 12, contrast >= 3:1.
LADDER_COLORS = ["#3b82f6", "#ef4444", "#0891b2", "#d97706",
                 "#8b5cf6", "#059669", "#ec4899"]

# Newest first. A hand-maintained record of what shipped on this page - not
# derived from git history, so it can explain *why* in plain language.
CHANGELOG = [
    {
        "version": "v3.5", "date": "2026-07-12",
        "title": "Freshness & clarity",
        "tags": ["live freshness", "stale warning", "count fix"],
        "description": (
            "The header now shows how long ago the data was refreshed (\"updated "
            "12 minutes ago\") and raises a banner if the 30-minute auto-refresh "
            "appears to have stalled, so you can tell live numbers from stale "
            "ones at a glance. The \"opportunities now\" count also now matches the "
            "grouped list instead of the raw pre-grouping total."
        ),
    },
    {
        "version": "v3.4", "date": "2026-07-12",
        "title": "Cleaner opportunity list & contracts table",
        "tags": ["grouped variants", "event sections", "bug fix"],
        "description": (
            "Opportunities that reuse one leg against several strikes are now "
            "grouped — the best is shown with a \"not additive\" note (they draw "
            "on one shared order book) and the rest listed compactly beneath, "
            "instead of near-duplicate rows. The full contracts table is grouped "
            "under named per-event headers, and a bug that hid each variant's "
            "distinguishing strike was fixed."
        ),
    },
    {
        "version": "v3.3", "date": "2026-07-12",
        "title": "Order-book depth sizing",
        "tags": ["capturable profit", "order-book depth", "ranking"],
        "description": (
            "Each flagged opportunity is now sized against live order-book depth "
            "(Kalshi's orderbook endpoint, Polymarket's CLOB book) instead of just "
            "a top-of-book price. The page reports total capturable profit "
            "(edge x max executable contracts) and ranks opportunities by it, so a "
            "small edge on a deep book correctly outranks a big edge you could "
            "only fill once."
        ),
    },
    {
        "version": "v3.2", "date": "2026-07-05",
        "title": "Legibility pass, then a bug fix",
        "tags": ["price-ladder chart", "stat tiles", "relative times",
                 "Kalshi link fix", "opportunity history"],
        "description": (
            "Added a two-panel probability-vs-strike price-ladder chart (solid "
            "Polymarket / dashed Kalshi lines), an at-a-glance stat-tile summary, "
            "and human-relative timestamps. Then fixed the Kalshi “open” "
            "links, which pointed to a guessed URL that dead-ended, and added the "
            "“Recently spotted” section so visitors see a track record "
            "of past findings, not just the current instant."
        ),
    },
    {
        "version": "v3.0", "date": "2026-07-05",
        "title": "Launch",
        "tags": ["Polymarket", "Kalshi", "detection engine", "GitHub Actions"],
        "description": (
            "First version of the cross-platform gold arbitrage scanner: "
            "normalizes every open gold-price market on Polymarket and Kalshi "
            "into one schema, detects logically-guaranteed mispricings with a "
            "single implication rule (fee-adjusted), and refreshes automatically "
            "every 30 minutes via GitHub Actions."
        ),
    },
]

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
    .num-cell {{ font-variant-numeric: tabular-nums; }}
    a {{ color: #60a5fa; }}
    .empty {{ color: #888; padding: 12px 0; }}
    footer {{ max-width: 1100px; margin: 0 auto; padding: 8px 20px 40px; color: #666; font-size: 0.85rem; }}
    .explain {{ color: #aaa; font-size: 0.92rem; max-width: 760px; }}
    .tiles {{
      max-width: 1100px; margin: 16px auto 0; padding: 0 20px;
      display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
    }}
    .tile {{
      background: #111; border: 1px solid #222; border-radius: 12px;
      padding: 12px 16px;
    }}
    .tile .num {{ font-size: 1.5rem; font-weight: 700; }}
    .tile .lab {{ color: #888; font-size: 0.82rem; }}
    .changelog {{ display: flex; flex-direction: column; }}
    .change-entry {{ display: grid; grid-template-columns: 64px 1fr; gap: 18px; padding: 16px 0; }}
    .change-entry + .change-entry {{ border-top: 1px solid #222; }}
    .change-version {{ color: #60a5fa; font-weight: 700; font-size: 0.92rem; padding-top: 2px; }}
    .change-entry h3 {{ margin: 0 0 4px; font-size: 1rem; }}
    .change-date {{ color: #666; font-weight: 400; font-size: 0.85rem; }}
    .change-entry p {{ margin: 0 0 8px; color: #aaa; font-size: 0.92rem; max-width: 68ch; }}
    .change-tags {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .variant-row td {{ padding-top: 0; }}
    ul.variants {{ margin: 4px 0 2px; padding-left: 18px; color: #aaa; font-size: 0.88rem; }}
    ul.variants li {{ margin: 2px 0; }}
    tr.group-head td {{
      background: #161616; font-weight: 700; font-size: 0.85rem;
      letter-spacing: 0.02em; padding: 8px 12px; border-top: 1px solid #222;
    }}
    tr.group-head .gh-plat {{ color: #60a5fa; }}
    #freshness {{ white-space: nowrap; }}
    .stale-banner {{
      max-width: 1100px; margin: 16px auto 0; padding: 10px 16px;
      background: #3a2a00; border: 1px solid #7a5a00; border-radius: 10px;
      color: #fbbf24; font-size: 0.9rem;
    }}
    .stale-banner[hidden] {{ display: none; }}
  </style>
</head>
<body>
  <header>
    <h1>Gold Arbitrage Tracker <span class="muted">V3.5</span></h1>
    <p>Polymarket &times; Kalshi &middot; refreshed every 30 minutes via GitHub Actions &middot;
       last updated <span id="freshness" data-updated="{updated_iso}">{updated} UTC</span>
       &middot; <a href="index.html">back to dashboard</a></p>
  </header>
  <div id="stale-banner" class="stale-banner" hidden></div>
  <div class="tiles">{tiles}</div>
  <main>
    <section>
      <h2>Live opportunities</h2>
      <p class="explain">Each row is a set of trades that logically <em>cannot</em> pay out
      less than its guaranteed amount at resolution: e.g. gold hitting $12,000 requires it
      to hit $10,000 first, so "hit $10,000" can never be worth less than "hit $12,000".
      When the combined price of such a set drops below its guaranteed payout, that gap is
      the edge. Kalshi taker fees (0.07&times;P&times;(1&minus;P) per contract) are already
      subtracted; Polymarket charges no trading fee. <strong>Capturable</strong> is the edge
      times how many contracts are actually available at that price in the live order book
      (the thinner leg caps the position) &mdash; rows are ranked by it, so a small edge on a
      deep book can outrank a big edge you could only fill once. Opportunities that reuse the
      same leg against different strikes are grouped (best shown, the rest listed underneath)
      since they draw on one shared order book.</p>
      {opportunities}
    </section>
    <section>
      <h2>Recently spotted</h2>
      <p class="explain">First sighting of each opportunity the scanner has flagged
      (it checks every 30 minutes; most edges vanish within a few scans). Full history
      in <a href="https://github.com/IshaanSrivastava1/markets-dashboard/blob/main/data/arb_log.csv">arb_log.csv</a>.</p>
      {history}
    </section>
    <section>
      <h2>Price ladders</h2>
      <p class="explain">Each line is one market's ladder of price levels: how likely
      the platform thinks gold reaches each level (mid price). Consistent pricing
      slopes smoothly downward &mdash; a kink, flat step, or upward blip is a
      potential mispricing, and where a solid (Polymarket) and dashed (Kalshi) line
      cover the same levels you can compare the platforms directly. Hover any point
      for bid/ask.</p>
      {ladder_chart}
    </section>
    <section>
      <h2>All live gold contracts ({n_contracts})</h2>
      <p class="explain">Every open gold-price market on both platforms, normalized to one
      schema. <strong>Settle</strong> = price at a specific timestamp;
      <strong>touch</strong> = trades through the level at any point in the window.
      Prices are YES bid/ask as probabilities.</p>
      <div class="tablewrap">{contracts_table}</div>
    </section>
    <section>
      <h2>Changelog</h2>
      <p class="explain">What's changed on this tracker since launch.</p>
      {changelog}
    </section>
  </main>
  <footer>
    Data: Polymarket gamma API and Kalshi trade-api v2 (both public market data).
    Capturable profit reflects top-of-book order-book depth only - deeper book levels
    aren't walked, and Kalshi's per-order fee rounding isn't modeled - so treat marginal
    edges as indicative. Personal/informational use, not financial advice.
  </footer>
  <script>
  (function () {{
    var el = document.getElementById('freshness');
    var banner = document.getElementById('stale-banner');
    if (!el) return;
    var updated = new Date(el.getAttribute('data-updated'));
    var absolute = el.textContent.trim();
    function fmt(ms) {{
      var s = Math.max(0, Math.floor(ms / 1000));
      var m = Math.floor(s / 60);
      if (m < 1) return 'just now';
      if (m < 90) return m + ' minute' + (m === 1 ? '' : 's') + ' ago';
      var h = Math.floor(m / 60);
      if (h < 48) return h + ' hour' + (h === 1 ? '' : 's') + ' ago';
      var d = Math.floor(h / 24);
      return d + ' day' + (d === 1 ? '' : 's') + ' ago';
    }}
    function tick() {{
      var age = Date.now() - updated.getTime();
      el.textContent = absolute + ' \\u00b7 updated ' + fmt(age);
      el.title = absolute;
      if (age > 90 * 60 * 1000) {{
        banner.hidden = false;
        banner.textContent = '\\u26a0 Auto-refresh may have stalled \\u2014 this data was last ' +
          'refreshed ' + fmt(age) + ' (it normally updates every 30 minutes).';
      }} else {{
        banner.hidden = true;
      }}
    }}
    tick();
    setInterval(tick, 60000);
  }})();
  </script>
</body>
</html>
"""


def _fmt_pct(value):
    return "-" if value is None else "%.1f%%" % (value * 100)


def _fmt_size(value):
    """Contract count: '1,256' or '-' / '0' (thin book)."""
    if value is None:
        return "-"
    return "{:,.0f}".format(value)


def _fmt_money(value):
    if value is None:
        return "-"
    return "${:,.2f}".format(value)


def _relative(iso_str):
    """Human-relative time until an ISO timestamp: 'in 26 days', 'in 5 hours'."""
    dt = _parse_iso(iso_str)
    if dt is None:
        return "-"
    seconds = (dt - datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0:
        return "expired"
    if seconds >= 2 * 86400:
        return "in %d days" % int(seconds // 86400)
    if seconds >= 2 * 3600:
        return "in %d hours" % int(seconds // 3600)
    return "in %d min" % max(1, int(seconds // 60))


def _ago(iso_str):
    """Human-relative time since an ISO timestamp: '12 min ago', '3 days ago'."""
    dt = _parse_iso(iso_str)
    if dt is None:
        return "-"
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds >= 2 * 86400:
        return "%d days ago" % int(seconds // 86400)
    if seconds >= 2 * 3600:
        return "%d hours ago" % int(seconds // 3600)
    return "%d min ago" % max(1, int(seconds // 60))


def _history_rows(opportunities, limit=10):
    """Last `limit` first-sightings from the log, newest first, flagged if the
    same trade is still open right now."""
    if not LOG_FILE.exists():
        return "<p class='empty'>Nothing logged yet.</p>"
    with LOG_FILE.open() as f:
        entries = list(csv.DictReader(f))
    if not entries:
        return "<p class='empty'>Nothing logged yet.</p>"
    open_now = {_fingerprint(o) for o in opportunities}
    rows = []
    for e in reversed(entries[-limit:]):
        seen = e.get("seen_at", "")
        absolute = seen[:16].replace("T", " ") + " UTC"
        badge = (" <span class='pill up'>still open</span>"
                 if e.get("fingerprint") in open_now else "")
        cap = e.get("capturable", "")
        cap_cell = ("$%s" % cap) if cap not in ("", None) else "-"  # blank on pre-sizing rows
        rows.append(
            "<tr class='divider'><td><span title='%s'>%s</span></td>"
            "<td><span class='pill'>%s</span></td><td>%s</td><td>$%s%s</td>"
            "<td class='muted'>%s</td></tr>"
            % (html.escape(absolute), _ago(seen), html.escape(e.get("kind", "-")),
               html.escape(cap_cell), html.escape(e.get("net_edge", "-")), badge,
               html.escape(e.get("description", ""))))
    return ("<div class='tablewrap'><table><tr><th>First seen</th><th>Type</th>"
            "<th>Capturable</th><th>Net edge</th><th>What</th></tr>%s</table></div>"
            % "".join(rows))


def _when(iso_str):
    """Relative time with the absolute UTC timestamp available on hover."""
    if not iso_str:
        return "-"
    absolute = iso_str[:16].replace("T", " ") + " UTC"
    return "<span title='%s'>%s</span>" % (html.escape(absolute), _relative(iso_str))


def _tiles(contracts, opportunities):
    live = sum(1 for c in contracts if c.has_quotes())
    caps = [o.capturable for o in opportunities if o.capturable is not None]
    best = _fmt_money(max(caps)) if caps else "&mdash;"
    # The list groups shared-leg variants, so the headline count is the number
    # of distinct opportunities shown, not the raw detected total.
    distinct = len(_cluster_opportunities(opportunities))
    total = len(opportunities)
    opp_label = ("opportunities now &middot; %d with variants" % total
                 if total > distinct else "opportunities now")
    tiles = [
        ("%d" % len(contracts), "contracts scanned"),
        ("%d" % live, "with live books"),
        ("%d" % distinct, opp_label),
        (best, "best capturable profit"),
    ]
    return "".join(
        "<div class='tile'><div class='num'>%s</div><div class='lab'>%s</div></div>"
        % (num, lab) for num, lab in tiles)


def _ladder_groups(contracts):
    """Quoted strike-ladder contracts grouped by (event, direction), sorted by
    strike; only groups of >=3 form a drawable ladder."""
    groups = {}
    for c in contracts:
        threshold = c.threshold_low if c.threshold_low is not None else c.threshold_high
        if threshold is None or not c.has_quotes():
            continue
        direction = "above" if c.semantics in (TOUCH_HIGH, SETTLE_ABOVE) else "below"
        groups.setdefault((c.underlying, c.event_id, direction), []).append(c)
    return {
        key: sorted(members, key=lambda c: c.threshold_low
                    if c.threshold_low is not None else c.threshold_high)
        for key, members in groups.items() if len(members) >= 3
    }


def _trace_label(direction, sample):
    arrow = "↑" if direction == "above" else "↓"
    end = _parse_iso(sample.window_end)
    end_str = end.strftime("%b %d") if end else "?"
    if sample.semantics in (TOUCH_HIGH, TOUCH_LOW):
        phrase = "hit %s by %s" % (arrow, end_str)
    else:
        phrase = "settle %s %s" % (arrow, end_str)
    return "%s · %s" % (sample.platform.capitalize(), phrase)


def make_ladder_figure(contracts):
    """Probability-vs-strike lines, one per ladder; spot and GC futures strikes
    live in different price regions so each underlying gets its own panel."""
    groups = _ladder_groups(contracts)
    if not groups:
        return None
    underlyings = sorted({key[0] for key in groups}, key=lambda u: u == GC_FUTURES)
    col_of = {u: i + 1 for i, u in enumerate(underlyings)}
    titles = {"XAU_SPOT": "Spot gold (XAUUSD)", "GC_FUTURES": "GC futures (long shots)"}

    fig = make_subplots(
        rows=1, cols=len(underlyings), shared_yaxes=True,
        column_widths=[0.62, 0.38] if len(underlyings) == 2 else None,
        subplot_titles=[titles.get(u, u) for u in underlyings],
        horizontal_spacing=0.06)

    # Deterministic order -> stable color assignment while a ladder is alive.
    ordered = sorted(groups.items(),
                     key=lambda kv: (kv[0][0], kv[1][0].window_end or "",
                                     kv[1][0].platform, kv[0][2]))
    for i, ((underlying, _event, direction), members) in enumerate(ordered):
        strikes = [c.threshold_low if c.threshold_low is not None
                   else c.threshold_high for c in members]
        mids = [(c.yes_bid + c.yes_ask) / 2 * 100 for c in members]
        quotes = [[c.yes_bid * 100, c.yes_ask * 100] for c in members]
        sample = members[0]
        fig.add_trace(go.Scatter(
            x=strikes, y=mids, customdata=quotes,
            name=_trace_label(direction, sample),
            mode="lines+markers",
            line=dict(color=LADDER_COLORS[i % len(LADDER_COLORS)], width=2,
                      dash="dash" if sample.platform == "kalshi" else "solid"),
            marker=dict(size=8),
            hovertemplate=("%{fullData.name}<br>$%{x:,.0f}: mid %{y:.1f}%"
                           "<br>bid %{customdata[0]:.1f}% / ask %{customdata[1]:.1f}%"
                           "<extra></extra>"),
        ), row=1, col=col_of[underlying])

    fig.update_yaxes(title_text="Implied probability (%)", range=[0, 100],
                     row=1, col=1, gridcolor="#1f1f1f")
    fig.update_yaxes(gridcolor="#1f1f1f")
    fig.update_xaxes(tickprefix="$", tickformat=",.0f", gridcolor="#1f1f1f",
                     title_text="Gold price level")
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0a0a0a", plot_bgcolor="#0a0a0a",
        height=480,
        hovermode="closest",
        legend=dict(orientation="h", yanchor="top", y=-0.22),
        margin=dict(l=60, r=20, t=40, b=40),
    )
    return fig


def _leg_key(leg):
    return (leg.contract.platform, leg.contract.market_id, leg.action)


def _short_title(title):
    """Trim a market question to something legend-length."""
    return title.split(" — ")[0].strip()


def _cluster_opportunities(opportunities):
    """Group opportunities that share a leg (same platform+market+side). Those
    variants draw on one shared order book, so their capturable isn't additive.
    Returns (shared_leg_key_or_None, [opps]) clusters, best capturable first,
    variants within a cluster also sorted best first."""
    counts = {}
    for o in opportunities:
        for leg in o.legs:
            counts[_leg_key(leg)] = counts.get(_leg_key(leg), 0) + 1

    clusters = {}
    for i, o in enumerate(opportunities):
        shared = [k for k in (_leg_key(l) for l in o.legs) if counts[k] > 1]
        # Cluster on the most-shared leg; opps with no shared leg stand alone.
        key = max(shared, key=lambda k: counts[k]) if shared else ("__solo__", i)
        clusters.setdefault(key, []).append(o)

    def cap(o):
        return o.capturable if o.capturable is not None else -1

    out = []
    for key, opps in clusters.items():
        opps.sort(key=cap, reverse=True)
        shared_key = key if not (isinstance(key, tuple) and key[0] == "__solo__") else None
        out.append((shared_key, opps))
    out.sort(key=lambda c: cap(c[1][0]), reverse=True)
    return out


def _opp_row(o, extra_flags=""):
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
    if extra_flags:
        flags.append(extra_flags)

    thin = o.max_contracts is not None and o.max_contracts == 0
    size_cell = ("<span class='down'>thin book</span>" if thin
                 else "<span class='num-cell'>%s</span>" % _fmt_size(o.max_contracts))
    cap_class = "up" if (o.capturable or 0) > 0 else "warn"
    cap_cell = "<span class='edge %s'>%s</span>" % (cap_class, _fmt_money(o.capturable))

    return (
        "<tr class='divider'><td><span class='pill'>%s</span></td><td>%s</td>"
        "<td>%s</td><td>%s</td>"
        "<td class='edge %s'>$%.4f%s<br/><span class='muted'>gross $%.4f &middot; "
        "fees $%.4f</span></td><td>%s<br/>%s</td></tr>"
        % (o.kind, legs, cap_cell, size_cell,
           edge_class, o.net_edge, marginal, o.gross_edge, o.fees,
           _when(o.expires_at), "".join(flags)))


def _variant_note(primary, variants, shared_key):
    """Caveat + compact list of the other opportunities sharing primary's leg."""
    shared_leg = next(l for l in primary.legs if _leg_key(l) == shared_key)
    depth = None
    for leg, d in zip(primary.legs, primary.leg_depths or []):
        if _leg_key(leg) == shared_key:
            depth = d
    depth_str = _fmt_size(depth) if depth is not None else "one"
    caveat = (
        "<div class='muted'>Shares the %s %s leg with %d other variant(s) &mdash; one "
        "%s-contract book fills across all of them, so these capturables are "
        "<strong>not additive</strong>.</div>"
        % (html.escape(_short_title(shared_leg.contract.title)),
           html.escape(shared_leg.action), len(variants), depth_str))
    items = []
    for v in variants:
        # Show the *differing* leg in full - the strike is what distinguishes
        # these variants, and it lives after the "—" that _short_title trims.
        other = [l for l in v.legs if _leg_key(l) != shared_key] or v.legs
        desc = ", ".join(
            "%s %s @ %.3f" % (html.escape(l.action),
                              html.escape(l.contract.title), l.price)
            for l in other)
        items.append(
            "<li>%s &mdash; %s, net $%.4f</li>"
            % (desc, _fmt_money(v.capturable), v.net_edge))
    return caveat, "<ul class='variants'>%s</ul>" % "".join(items)


def _opportunity_rows(opportunities):
    if not opportunities:
        return ("<p class='empty'>No opportunities with a positive net edge right now. "
                "That is the normal state - real arbitrage is rare and small. The scan "
                "keeps running every 30 minutes.</p>")
    rows = []
    for shared_key, cluster in _cluster_opportunities(opportunities):
        primary = cluster[0]
        if shared_key is None or len(cluster) == 1:
            rows.append(_opp_row(primary))
            continue
        caveat, variant_list = _variant_note(primary, cluster[1:], shared_key)
        rows.append(_opp_row(primary, extra_flags=caveat))
        rows.append(
            "<tr class='variant-row'><td></td><td colspan='5'>"
            "<span class='muted'>Other variants on the same leg (shown above is best):"
            "</span>%s</td></tr>" % variant_list)
    return ("<div class='tablewrap'><table><tr><th>Type</th><th>Trades (per $1 contract)"
            "</th><th>Capturable</th><th>Max size</th><th>Net edge</th>"
            "<th>Resolves by</th></tr>%s</table></div>" % "".join(rows))


def _event_group_label(sample):
    """Named header for an event group: platform · event name · type · window."""
    kind = "settle" if sample.semantics.startswith("SETTLE") else (
        "touch" if sample.semantics.startswith("TOUCH") else "direction")
    name = sample.event_title or sample.event_id
    return ("<span class='gh-plat'>%s</span> &middot; %s &middot; %s &middot; ends %s" % (
        html.escape(sample.platform.capitalize()), html.escape(name),
        kind, _relative(sample.window_end)))


def _contracts_table(contracts):
    contracts = sorted(
        contracts,
        key=lambda c: (c.underlying, c.window_end or "", c.platform, c.event_id,
                       c.threshold_low or c.threshold_high or 0))
    rows = []
    prev_event = None
    for c in contracts:
        if c.event_id != prev_event:
            rows.append("<tr class='group-head'><td colspan='7'>%s</td></tr>"
                        % _event_group_label(c))
            prev_event = c.event_id
        threshold = c.threshold_low if c.threshold_low is not None else c.threshold_high
        semantics = c.semantics.replace("_", " ").lower()
        stale = "" if c.has_quotes() else " <span class='down'>(no book)</span>"
        rows.append(
            "<tr><td><span class='pill'>%s</span></td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s / %s%s</td><td class='muted'>%s</td>"
            "<td><a href='%s'>open</a></td></tr>"
            % (c.platform, c.underlying.replace("_", " "),
               html.escape(semantics),
               "-" if threshold is None else "$%s" % format(int(threshold), ","),
               _fmt_pct(c.yes_bid), _fmt_pct(c.yes_ask), stale,
               _when(c.window_end),
               html.escape(c.url)))
    return ("<table><tr><th>Platform</th><th>Underlying</th><th>Type</th>"
            "<th>Level</th><th>YES bid/ask</th><th>Window ends</th><th></th></tr>%s"
            "</table>" % "".join(rows))


def _changelog_rows():
    entries = []
    for entry in CHANGELOG:
        tags = "".join("<span class='pill'>%s</span>" % html.escape(t)
                       for t in entry["tags"])
        entries.append(
            "<div class='change-entry'><div class='change-version'>%s</div>"
            "<div><h3>%s <span class='change-date'>%s</span></h3>"
            "<p>%s</p><div class='change-tags'>%s</div></div></div>"
            % (html.escape(entry["version"]), html.escape(entry["title"]),
               entry["date"], html.escape(entry["description"]), tags))
    return "<div class='changelog'>%s</div>" % "".join(entries)


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
              "max_contracts", "capturable", "expires_at", "description"]

    def row(o):
        return {"seen_at": now_iso, "fingerprint": _fingerprint(o), "kind": o.kind,
                "net_edge": o.net_edge, "gross_edge": o.gross_edge, "fees": o.fees,
                "max_contracts": o.max_contracts if o.max_contracts is not None else "",
                "capturable": o.capturable if o.capturable is not None else "",
                "expires_at": o.expires_at or "", "description": o.description}

    CURRENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CURRENT_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(row(o) for o in opportunities)

    new = [o for o in opportunities if _fingerprint(o) not in previous]
    if new:
        _append_log(new, fields, row)
    return len(new)


def _append_log(new_opportunities, fields, row):
    """Append new sightings to the log. If the log predates a column (the
    schema has grown over time), rewrite it with the new header first so old
    and new rows stay aligned."""
    old_rows = []
    needs_migration = False
    if LOG_FILE.exists():
        with LOG_FILE.open(newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != fields:
                needs_migration = True
                old_rows = list(reader)

    if needs_migration:
        with LOG_FILE.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for r in old_rows:
                writer.writerow({k: r.get(k, "") for k in fields})
            writer.writerows(row(o) for o in new_opportunities)
        return

    with LOG_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not old_rows and LOG_FILE.stat().st_size == 0:
            writer.writeheader()
        writer.writerows(row(o) for o in new_opportunities)


def _cached_ladder_provider():
    """Order-book provider that fetches each distinct leg once (deduped by
    platform/market/side). Only the legs of flagged opportunities are ever
    passed in, so this stays a handful of calls per run."""
    cache = {}

    def provider(leg):
        key = (leg.contract.platform, leg.contract.market_id, leg.action)
        if key not in cache:
            cache[key] = arb_sources.fetch_ask_ladder(leg.contract, leg.action)
        return cache[key]

    provider.cache = cache
    return provider


def main():
    contracts = arb_sources.fetch_all_contracts()
    opportunities = arb_engine.find_opportunities(contracts)
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Size each opportunity against live order-book depth, then rank by total
    # capturable profit rather than per-contract edge.
    provider = _cached_ladder_provider()
    arb_engine.size_opportunities(opportunities, provider)
    opportunities = arb_engine.rank_by_capturable(opportunities)
    print("[book] fetched %d distinct leg book(s)" % len(provider.cache))

    # Log first so this run's new sightings appear in its own history section.
    new_count = _write_csvs(opportunities, now_iso)

    fig = make_ladder_figure(contracts)
    ladder_chart = (fig.to_html(full_html=False, include_plotlyjs="cdn") if fig
                    else "<p class='empty'>No ladders with live quotes right now.</p>")

    html_out = PAGE_TEMPLATE.format(
        updated=now.strftime("%Y-%m-%d %H:%M"),
        updated_iso=now_iso,
        tiles=_tiles(contracts, opportunities),
        opportunities=_opportunity_rows(opportunities),
        history=_history_rows(opportunities),
        ladder_chart=ladder_chart,
        n_contracts=len(contracts),
        contracts_table=_contracts_table(contracts),
        changelog=_changelog_rows(),
    )
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(html_out)

    print("Wrote %s (%d contracts, %d opportunities, %d newly seen)"
          % (OUT_FILE, len(contracts), len(opportunities), new_count))


if __name__ == "__main__":
    main()
