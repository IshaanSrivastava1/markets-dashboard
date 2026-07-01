#!/usr/bin/env python3
"""Micron (MU) daily price + EMAs — data + chart module for the dashboard.

Fetches ~1yr of daily OHLCV from Yahoo Finance's public chart endpoint,
maintains a CSV history, computes 9/21/48-day EMAs, and detects crossovers.
Exposes:
  - update_data() -> rows
  - compute_emas(rows) -> {period: [ema...]}
  - make_figure(rows, emas) -> plotly Figure
  - make_summary(rows, emas) -> html string

No email / SMTP / matplotlib — hosted, website-only variant.
"""
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import plotly.graph_objects as go
import requests

TICKER = "MU"
EMA_PERIODS = (9, 21, 48)
LOOKBACK_DAYS = 365

BASE_DIR = Path(__file__).resolve().parent
CSV_FILE = BASE_DIR / "data" / "mu_history.csv"
API_URL = f"https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}"

EMA_COLORS = {9: "#60a5fa", 21: "#fbbf24", 48: "#f87171"}


def fetch_daily_series():
    params = {"range": "1y", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(API_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    try:
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Unexpected API response: {data}")

    rows = []
    for i, ts in enumerate(timestamps):
        close = quote["close"][i]
        if close is None:
            continue
        rows.append({
            "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            "open": quote["open"][i],
            "high": quote["high"][i],
            "low": quote["low"][i],
            "close": close,
            "volume": quote["volume"][i],
        })
    rows.sort(key=lambda r: r["date"])
    return rows


def load_rows():
    if not CSV_FILE.exists():
        return []
    with CSV_FILE.open() as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("open", "high", "low", "close"):
            r[k] = float(r[k])
        r["volume"] = int(float(r["volume"]))
    return rows


def last_saved_date():
    rows = load_rows()
    return rows[-1]["date"] if rows else None


def trim_to_lookback(rows, days=LOOKBACK_DAYS):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    return [r for r in rows if r["date"] >= cutoff]


def save_csv(rows):
    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["date", "open", "high", "low", "close", "volume"]
    with CSV_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def compute_ema(closes, period):
    """Standard 2/(N+1) EMA, seeded with the SMA of the first `period` closes."""
    ema = [None] * len(closes)
    if len(closes) < period:
        return ema
    ema[period - 1] = sum(closes[:period]) / period
    multiplier = 2 / (period + 1)
    for i in range(period, len(closes)):
        ema[i] = (closes[i] - ema[i - 1]) * multiplier + ema[i - 1]
    return ema


def compute_emas(rows):
    closes = [r["close"] for r in rows]
    return {period: compute_ema(closes, period) for period in EMA_PERIODS}


def update_data():
    """Fetch fresh series; only rewrite CSV if there's a genuinely new latest
    trading day (keeps weekends/holidays from duplicating the last row)."""
    previous_last_date = last_saved_date()
    fetched = fetch_daily_series()
    if not fetched:
        return load_rows()  # fall back to stored data
    rows = trim_to_lookback(fetched)
    if rows and rows[-1]["date"] != previous_last_date:
        save_csv(rows)
        return rows
    return load_rows()


def compute_summary(rows, emas):
    if not rows:
        return "<p>No data available.</p>"

    today = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else None

    change_html = ""
    if prev is not None:
        change = today["close"] - prev["close"]
        pct = (change / prev["close"]) * 100 if prev["close"] else 0
        cls = "up" if change >= 0 else "down"
        sign = "+" if change >= 0 else ""
        change_html = f"<p class='{cls}'>Change: {sign}${change:.2f} ({sign}{pct:.2f}%)</p>"

    crossovers = []
    if len(rows) >= 2:
        for fast, slow in [(9, 21), (21, 48)]:
            pf, ps = emas[fast][-2], emas[slow][-2]
            tf, ts = emas[fast][-1], emas[slow][-1]
            if None in (pf, ps, tf, ts):
                continue
            if pf <= ps and tf > ts:
                crossovers.append(f"Bullish crossover: {fast}-day EMA crossed above {slow}-day EMA")
            elif pf >= ps and tf < ts:
                crossovers.append(f"Bearish crossover: {fast}-day EMA crossed below {slow}-day EMA")
    crossover_html = "".join(f"<p class='signal'>{c}</p>" for c in crossovers)

    ema_notes = []
    for p in EMA_PERIODS:
        val = emas[p][-1]
        if val is None:
            continue
        dist = (today["close"] - val) / val * 100
        side = "above" if dist >= 0 else "below"
        ema_notes.append(f"Close is {abs(dist):.1f}% {side} the {p}-day EMA")
    notes_html = "".join(f"<p class='muted'>{n}</p>" for n in ema_notes)

    return f"""
      <h3>Latest close ({today['date']})</h3>
      <p class="big">${today['close']:.2f}</p>
      {change_html}
      {crossover_html}
      {notes_html}
      <p class="muted">Volume: {today['volume']:,}</p>
    """


def make_figure(rows, emas):
    dates = [r["date"] for r in rows]
    closes = [r["close"] for r in rows]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates, y=closes, name="Close", mode="lines",
        line=dict(color="#e5e5e5", width=1.5),
        hovertemplate="Close<br>%{x}: $%{y:.2f}<extra></extra>",
    ))
    for period in EMA_PERIODS:
        fig.add_trace(go.Scatter(
            x=dates, y=emas[period], name=f"EMA {period}", mode="lines",
            connectgaps=False,
            line=dict(color=EMA_COLORS[period], width=1.5),
            hovertemplate=f"EMA {period}<br>%{{x}}: $%{{y:.2f}}<extra></extra>",
        ))
    fig.update_layout(
        template="plotly_dark",
        title=f"{TICKER} — Close with 9/21/48-day EMAs",
        yaxis=dict(title="Price (USD)"),
        xaxis=dict(title="Date"),
        hovermode="x unified",
        paper_bgcolor="#0a0a0a",
        plot_bgcolor="#0a0a0a",
        margin=dict(l=60, r=20, t=60, b=50),
        height=500,
    )
    return fig
