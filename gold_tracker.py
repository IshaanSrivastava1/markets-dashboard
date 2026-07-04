#!/usr/bin/env python3
"""Gold (GC) June 2026 settlement odds - data + chart module for the dashboard.

Fetches live odds from Polymarket's gamma API, appends to a CSV history store,
and can backfill the full market history from the CLOB API. Exposes:
  - update_data() -> (rows, labels)
  - make_figure(rows, labels) -> plotly Figure
  - make_summary(rows, labels) -> html string

No email / SMTP / matplotlib here - this is the hosted, website-only variant.
"""
import csv
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import plotly.graph_objects as go

EVENT_URL = "https://gamma-api.polymarket.com/events?slug=gc-settle-jun-2026"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = BASE_DIR / "data" / "gold_history.csv"
MARKET_URL = "https://polymarket.com/event/gc-settle-jun-2026"

LINE_COLORS = [
    "#60a5fa", "#f87171", "#34d399", "#fbbf24",
    "#a78bfa", "#f472b6", "#22d3ee", "#facc15",
]


def bracket_sort_key(label):
    nums = [float(n.replace(",", "")) for n in re.findall(r"[\d,]+(?:\.\d+)?", label)]
    if label.startswith("<"):
        return -1
    if label.startswith(">"):
        return 1_000_000
    return nums[0] if nums else 0


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.7.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def fetch_odds():
    data = _get_json(EVENT_URL)
    event = data[0]
    markets = sorted(event["markets"], key=lambda m: bracket_sort_key(m["groupItemTitle"]))
    odds = {}
    for m in markets:
        label = m["groupItemTitle"]
        yes_price = float(json.loads(m["outcomePrices"])[0])
        odds[label] = round(yes_price, 4)
    return odds, event.get("volume24hr")


def fetch_bracket_tokens():
    data = _get_json(EVENT_URL)
    markets = data[0]["markets"]
    return {m["groupItemTitle"]: json.loads(m["clobTokenIds"])[0] for m in markets}


def fetch_price_history(token_id):
    url = f"{CLOB_HISTORY_URL}?market={token_id}&interval=max&fidelity=1440"
    data = _get_json(url)
    by_date = {}
    for point in data["history"]:
        date_str = datetime.fromtimestamp(point["t"], tz=timezone.utc).strftime("%Y-%m-%d")
        by_date[date_str] = round(point["p"], 4)
    return by_date


def load_rows():
    if not HISTORY_FILE.exists():
        return [], None
    with HISTORY_FILE.open() as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames
    return rows, fieldnames


def last_saved_date():
    rows, _ = load_rows()
    return rows[-1]["date"] if rows else None


def backfill():
    """Fill history.csv from the CLOB API back to market creation. Never
    overwrites existing rows (real daily snapshots with volume win). Drops any
    row dated in the future relative to the current UTC date (UTC/CLOB boundary
    artifact). Safe to call every run - it only adds missing past dates."""
    tokens = fetch_bracket_tokens()
    labels = sorted(tokens.keys(), key=bracket_sort_key)

    history_by_label = {}
    all_dates = set()
    for label in labels:
        by_date = fetch_price_history(tokens[label])
        history_by_label[label] = by_date
        all_dates.update(by_date.keys())

    existing_rows, _ = load_rows()
    existing = {r["date"]: r for r in existing_rows}
    fieldnames = ["date"] + labels + ["volume"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    merged = dict(existing)
    for date_str in sorted(all_dates):
        if date_str in merged or date_str > today:
            continue
        row = {"date": date_str, "volume": ""}
        for label in labels:
            if date_str in history_by_label[label]:
                row[label] = history_by_label[label][date_str]
        merged[date_str] = row

    _write_rows(merged, fieldnames)


def append_history(today, odds, volume24hr):
    labels = sorted(odds.keys(), key=bracket_sort_key)
    existing_rows, fieldnames = load_rows()
    if not fieldnames:
        fieldnames = ["date"] + labels + ["volume"]
    if "volume" not in fieldnames:
        fieldnames = fieldnames + ["volume"]

    kept = {r["date"]: r for r in existing_rows if r["date"] != today}
    new_row = {"date": today, "volume": volume24hr if volume24hr is not None else ""}
    new_row.update({label: odds.get(label, "") for label in fieldnames if label not in ("date", "volume")})
    kept[today] = new_row
    _write_rows(kept, fieldnames)


def _write_rows(rows_by_date, fieldnames):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for date_str in sorted(rows_by_date.keys()):
            writer.writerow(rows_by_date[date_str])


def read_history():
    rows, fieldnames = load_rows()
    labels = [field for field in fieldnames if field not in ("date", "volume")]
    return rows, sorted(labels, key=bracket_sort_key)


def compute_deltas(rows, labels):
    """Latest row vs prior row → (changes sorted by magnitude, volume_delta)."""
    if len(rows) < 2:
        return [], None

    today_row, prev_row = rows[-1], rows[-2]
    changes = []
    for label in labels:
        today_val = float(today_row[label]) * 100 if today_row.get(label) else None
        prev_val = float(prev_row[label]) * 100 if prev_row.get(label) else None
        if today_val is None or prev_val is None:
            continue
        changes.append((label, today_val, today_val - prev_val))
    changes.sort(key=lambda c: abs(c[2]), reverse=True)

    volume_delta = None
    today_vol, prev_vol = today_row.get("volume"), prev_row.get("volume")
    if today_vol and prev_vol:
        try:
            volume_delta = float(today_vol) - float(prev_vol)
        except ValueError:
            pass
    return changes, volume_delta


def update_data():
    """Backfill any missing past days, then fetch + append today's odds."""
    try:
        backfill()
    except Exception as exc:  # backfill is best-effort; don't block the live fetch
        print(f"[gold] backfill skipped: {exc}")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    odds, volume24hr = fetch_odds()
    append_history(today, odds, volume24hr)
    return read_history()


def make_figure(rows, labels):
    dates = [r["date"] for r in rows]
    fig = go.Figure()
    for i, label in enumerate(labels):
        values = [float(r[label]) * 100 if r.get(label) else None for r in rows]
        fig.add_trace(go.Scatter(
            x=dates, y=values, name=label, mode="lines",
            connectgaps=False,
            line=dict(color=LINE_COLORS[i % len(LINE_COLORS)], width=2),
            hovertemplate=f"{label}<br>%{{x}}: %{{y:.2f}}%<extra></extra>",
        ))
    fig.update_layout(
        template="plotly_dark",
        title="Gold (GC) June 2026 Settlement Polymarket Odds",
        yaxis=dict(title="Probability (%)", range=[0, 100]),
        xaxis=dict(title="Date"),
        hovermode="x unified",
        paper_bgcolor="#0a0a0a",
        plot_bgcolor="#0a0a0a",
        margin=dict(l=60, r=20, t=60, b=50),
        height=500,
    )
    return fig


def make_summary(rows, labels):
    if not rows:
        return "<p>No data available.</p>"

    today_row = rows[-1]
    current = "".join(
        f"<tr><td>{label}</td><td>{float(today_row[label]) * 100:.1f}%</td></tr>"
        for label in labels if today_row.get(label)
    )

    changes, volume_delta = compute_deltas(rows, labels)
    changes_html = ""
    if changes:
        biggest_label, _, biggest_delta = changes[0]
        change_rows = "".join(
            f"<tr><td>{label}</td><td>{today_val:.1f}%</td>"
            f"<td class='{'up' if delta >= 0 else 'down'}'>"
            f"{'+' if delta >= 0 else ''}{delta:.1f}pp</td></tr>"
            for label, today_val, delta in changes
        )
        vol_html = ""
        if volume_delta is not None:
            cls = "up" if volume_delta >= 0 else "down"
            sign = "+" if volume_delta >= 0 else ""
            vol_html = f"<p class='{cls}'>24h volume change: {sign}${volume_delta:,.0f}</p>"
        changes_html = f"""
          <h3>Changes vs previous day</h3>
          <p class="muted">Biggest mover: {biggest_label}
            ({'+' if biggest_delta >= 0 else ''}{biggest_delta:.1f}pp)</p>
          <table class="delta"><tr><th>Bracket</th><th>Now</th><th>Δ</th></tr>{change_rows}</table>
          {vol_html}
        """

    vol_now = ""
    if today_row.get("volume"):
        try:
            vol_now = f"<p class='muted'>24h volume: ${float(today_row['volume']):,.0f}</p>"
        except ValueError:
            pass

    return f"""
      <h3>Current odds ({today_row['date']})</h3>
      <table class="current"><tr><th>Bracket</th><th>Probability</th></tr>{current}</table>
      {vol_now}
      {changes_html}
      <p class="muted">Source:
        <a href="{MARKET_URL}">polymarket.com/event/gc-settle-jun-2026</a></p>
    """
