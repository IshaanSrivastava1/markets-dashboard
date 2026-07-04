#!/usr/bin/env python3
"""Orchestrator: refresh both trackers' data and regenerate docs/index.html.

This is the entry point run by the GitHub Actions workflow. Each tracker's
update is wrapped in try/except so one failing data source degrades to a
notice rather than blanking the whole page (it falls back to stored CSV data).
"""
from datetime import datetime, timezone
from pathlib import Path

import gold_tracker
import mu_tracker

BASE_DIR = Path(__file__).resolve().parent
OUT_FILE = BASE_DIR / "docs" / "index.html"


def build_gold_section():
    note = ""
    try:
        rows, labels = gold_tracker.update_data()
    except Exception as exc:
        print(f"[gold] live update failed, using stored data: {exc}")
        note = "<p class='error'>Live update unavailable today, showing last stored data.</p>"
        rows, labels = gold_tracker.read_history()
    fig = gold_tracker.make_figure(rows, labels)
    summary = gold_tracker.make_summary(rows, labels)
    chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn")
    return chart_html, summary, note


def build_mu_section():
    note = ""
    try:
        rows = mu_tracker.update_data()
    except Exception as exc:
        print(f"[mu] live update failed, using stored data: {exc}")
        note = "<p class='error'>Live update unavailable today, showing last stored data.</p>"
        rows = mu_tracker.load_rows()
    emas = mu_tracker.compute_emas(rows)
    fig = mu_tracker.make_figure(rows, emas)
    summary = mu_tracker.compute_summary(rows, emas)
    # plotly.js already loaded by the gold chart's CDN include; don't duplicate.
    chart_html = fig.to_html(full_html=False, include_plotlyjs=False)
    return chart_html, summary, note


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Markets Dashboard - Gold Odds &amp; MU Trends</title>
  <meta name="description" content="Auto-updating dashboard: Polymarket gold settlement odds and Micron (MU) price with EMAs. Rebuilt daily via GitHub Actions."/>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; background: #0a0a0a; color: #e5e5e5;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      line-height: 1.5;
    }}
    header {{ padding: 32px 20px 8px; max-width: 1000px; margin: 0 auto; }}
    header h1 {{ margin: 0 0 4px; font-size: 1.8rem; }}
    header p {{ margin: 0; color: #888; }}
    main {{ max-width: 1000px; margin: 0 auto; padding: 20px; }}
    section {{
      background: #111; border: 1px solid #222; border-radius: 12px;
      padding: 20px; margin-bottom: 28px; scroll-margin-top: 20px;
    }}
    section h2 {{ margin-top: 0; }}
    h3 {{ margin: 18px 0 8px; font-size: 1.05rem; }}
    table {{ border-collapse: collapse; margin: 8px 0; font-size: 0.92rem; }}
    th, td {{ text-align: left; padding: 3px 14px 3px 0; }}
    th {{ color: #888; font-weight: 600; }}
    .muted {{ color: #888; font-size: 0.9rem; margin: 4px 0; }}
    .big {{ font-size: 1.6rem; font-weight: 700; margin: 4px 0; }}
    .up {{ color: #34d399; }}
    .down {{ color: #f87171; }}
    .signal {{ color: #fbbf24; font-weight: 600; margin: 6px 0; }}
    .error {{ color: #f87171; font-weight: 600; }}
    a {{ color: #60a5fa; }}
    .grid {{ display: grid; grid-template-columns: 1.6fr 1fr; gap: 20px; align-items: start; }}
    @media (max-width: 760px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    footer {{ max-width: 1000px; margin: 0 auto; padding: 8px 20px 40px; color: #666; font-size: 0.85rem; }}
    nav.tabs {{ max-width: 1000px; margin: 0 auto; padding: 8px 20px 0; display: flex; gap: 8px; flex-wrap: wrap; }}
    .tab-btn {{
      background: #111; color: #e5e5e5; border: 1px solid #222; border-radius: 8px;
      padding: 8px 16px; font-size: 0.95rem; cursor: pointer;
    }}
    .tab-btn:hover {{ border-color: #3b82f6; }}
    .tab-btn.active {{ background: #1e293b; border-color: #3b82f6; color: #fff; }}
    .tab-panel[hidden] {{ display: none; }}
    .description {{ color: #aaa; font-size: 0.92rem; margin: 12px 2px 0; max-width: 640px; }}
  </style>
</head>
<body>
  <header>
    <h1>Markets Dashboard</h1>
    <p>Auto-updated daily via GitHub Actions · Last updated {updated} UTC</p>
  </header>
  <nav class="tabs">
    <button class="tab-btn active" data-tab="archived" type="button">Archived Projects</button>
  </nav>
  <main>
    <div id="archived" class="tab-panel">
      <section id="gold">
        <h2>old - V1 Gold (GC) June 2026 Settlement Odds</h2>
        {gold_note}
        <div class="grid">
          <div>
            {gold_chart}
            <p class="description">The purpose of this tracker was to find correlations in the movements of prediction market odds to the movements of the underlying asset, and send a summary of these daily changes to myself for evaluation.</p>
          </div>
          <div>{gold_summary}</div>
        </div>
      </section>
      <section id="mu">
        <h2>old - V1 Micron (MU) EMA Trend Tracker</h2>
        {mu_note}
        <div class="grid">
          <div>
            {mu_chart}
            <p class="description">This tracker was created to send automated alerts to myself whenever MU broke below its 9-day or 21-day EMA, since I was using these metrics to trade this ticker at the time.</p>
          </div>
          <div>{mu_summary}</div>
        </div>
      </section>
    </div>
  </main>
  <footer>
    Data: Polymarket (gamma + CLOB APIs) and Yahoo Finance. For personal/informational use - not financial advice.
  </footer>
  {script}
</body>
</html>
"""


TAB_SCRIPT = """<script>
  (function () {
    var tabs = document.querySelectorAll('.tab-btn');
    var panels = document.querySelectorAll('.tab-panel');
    tabs.forEach(function (btn) {
      btn.addEventListener('click', function () {
        tabs.forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        panels.forEach(function (p) { p.hidden = (p.id !== btn.dataset.tab); });
      });
    });
  })();
</script>"""


def main():
    gold_chart, gold_summary, gold_note = build_gold_section()
    mu_chart, mu_summary, mu_note = build_mu_section()

    html = PAGE_TEMPLATE.format(
        updated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        gold_chart=gold_chart, gold_summary=gold_summary, gold_note=gold_note,
        mu_chart=mu_chart, mu_summary=mu_summary, mu_note=mu_note,
        script=TAB_SCRIPT,
    )

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(html)
    print(f"Wrote {OUT_FILE} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
