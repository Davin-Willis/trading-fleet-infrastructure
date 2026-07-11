#!/usr/bin/env python3
"""
spread_audit.py — READ-ONLY crypto spread sampler for Alpaca.

Purpose: before building any crypto bot, measure the actual bid/ask spreads
on Alpaca's crypto pairs. Spread is the tax on every round trip; a strategy
is only viable if its expected edge exceeds ~2x the spread. This script
opens NO positions, needs NO account balance — it only reads live quotes.

Run it a few times across different hours (crypto is 24/7, spreads vary),
or leave it looping. It appends every sample to spread_log.csv and prints
a running summary ranking pairs from tightest to widest.

Usage:
    python3 spread_audit.py            # single snapshot of all pairs
    python3 spread_audit.py --loop 60  # sample every 60s until Ctrl+C
"""
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
ENV = BASE / ".env"
LOG = BASE / "spread_log.csv"

# Alpaca's tradeable crypto universe (USD pairs). We sample the liquid majors
# plus a few alts to prove the correlation/illiquidity thesis with data.
PAIRS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "LINK/USD",
    "AVAX/USD", "DOGE/USD", "BCH/USD", "UNI/USD", "AAVE/USD",
    "DOT/USD", "SHIB/USD", "XRP/USD", "MKR/USD",
]

DATA_URL = "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes"


def load_env():
    env = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def sample(env):
    """One snapshot of all pairs. Returns list of dicts."""
    headers = {
        "APCA-API-KEY-ID": env.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": env.get("ALPACA_SECRET_KEY", env.get("ALPACA_API_SECRET", "")),
    }
    params = {"symbols": ",".join(PAIRS)}
    try:
        r = requests.get(DATA_URL, headers=headers, params=params, timeout=30)
        r.raise_for_status()
    except Exception as exc:
        print(f"  quote fetch failed: {exc}")
        return []
    quotes = r.json().get("quotes", {})
    ts = datetime.now(timezone.utc).isoformat()
    rows = []
    for pair in PAIRS:
        q = quotes.get(pair)
        if not q:
            continue
        bid, ask = q.get("bp"), q.get("ap")
        if not bid or not ask or bid <= 0:
            continue
        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * 10000  # basis points
        rows.append({
            "ts": ts, "pair": pair, "bid": bid, "ask": ask,
            "mid": round(mid, 6), "spread_bps": round(spread_bps, 2),
        })
    return rows


def append_log(rows):
    new = not LOG.exists()
    with LOG.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "pair", "bid", "ask", "mid", "spread_bps"])
        if new:
            w.writeheader()
        w.writerows(rows)


def summarize():
    """Read the whole log and rank pairs by average spread."""
    if not LOG.exists():
        return
    from collections import defaultdict
    agg = defaultdict(list)
    with LOG.open() as f:
        for row in csv.DictReader(f):
            agg[row["pair"]].append(float(row["spread_bps"]))
    print("\n  === SPREAD SUMMARY (all samples so far) ===")
    print(f"  {'PAIR':<12} {'samples':>8} {'avg_bps':>9} {'min':>7} {'max':>7}   verdict")
    ranked = sorted(agg.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
    for pair, vals in ranked:
        avg = sum(vals) / len(vals)
        verdict = ("TIGHT — tradeable" if avg < 10 else
                   "OK — daily cadence fine" if avg < 30 else
                   "WIDE — costs bite" if avg < 75 else
                   "AVOID — untradeable")
        print(f"  {pair:<12} {len(vals):>8} {avg:>9.2f} {min(vals):>7.1f} {max(vals):>7.1f}   {verdict}")
    print("\n  Rule of thumb: a daily-cadence strategy needs the edge per trade to")
    print("  exceed ~2x spread. <30 bps is comfortable for daily trend-following.")


def main():
    env = load_env()
    if not env.get("ALPACA_API_KEY"):
        print("No ALPACA_API_KEY in .env — this script only reads quotes, but still needs keys.")
        sys.exit(1)

    loop = 0
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        loop = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) else 60

    while True:
        rows = sample(env)
        if rows:
            append_log(rows)
            print(f"\n[{datetime.now(timezone.utc):%H:%M:%S} UTC] sampled {len(rows)} pairs:")
            for r in sorted(rows, key=lambda x: x["spread_bps"]):
                print(f"    {r['pair']:<12} spread {r['spread_bps']:>7.2f} bps   (mid ${r['mid']:,.4f})")
        summarize()
        if not loop:
            break
        time.sleep(loop)


if __name__ == "__main__":
    main()
