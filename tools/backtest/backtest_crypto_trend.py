#!/usr/bin/env python3
"""
backtest_crypto_trend.py — pre-deployment backtest for the BTC/ETH trend bots.

PRE-REGISTERED RULE (decided before this backtest was written, per fleet policy):
    Daily decision on the close:
      LONG the asset when  close > 20-day SMA  AND  30-day momentum > 0
      FLAT (cash) otherwise.
    One asset per bot (BTC bot, ETH bot). Long-or-flat, no leverage, no shorting.

PURPOSE: sanity gate + expectation calibration (drawdown depth for kill-line
sizing). Per fleet policy, backtest results are INFORMATIONAL ONLY and do not
drive changes to live strategies.

COST MODEL (stated explicitly, conservative):
    Alpaca crypto taker fee ~25 bps per side + ~5 bps half-spread (measured:
    BTC/ETH avg spread 9.5 bps) => 30 bps per side, 60 bps per round trip.

Usage (needs Alpaca keys in .env in same folder, same as spread_audit):
    python3 backtest_crypto_trend.py
"""
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
SYMBOLS = ["BTC/USD", "ETH/USD"]
YEARS = 5
SMA_N = 20
MOM_N = 30
COST_PER_SIDE = 0.0030  # 30 bps


def load_env():
    env = {}
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def fetch_daily_bars(env, symbol, years):
    headers = {
        "APCA-API-KEY-ID": env.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": env.get("ALPACA_SECRET_KEY", env.get("ALPACA_API_SECRET", "")),
    }
    start = (datetime.now(timezone.utc) - timedelta(days=365 * years + 60)).strftime("%Y-%m-%d")
    bars, page = [], None
    while True:
        params = {"symbols": symbol, "timeframe": "1Day", "start": start, "limit": 10000}
        if page:
            params["page_token"] = page
        r = requests.get("https://data.alpaca.markets/v1beta3/crypto/us/bars",
                         headers=headers, params=params, timeout=60)
        r.raise_for_status()
        j = r.json()
        bars += j.get("bars", {}).get(symbol, [])
        page = j.get("next_page_token")
        if not page:
            break
    return [(b["t"][:10], float(b["c"])) for b in bars]


def run_strategy(closes):
    """Returns (daily strategy returns list, trade count, position series)."""
    rets, pos_series = [], []
    position = 0
    trades = 0
    for i in range(len(closes)):
        if i >= MOM_N:
            sma = statistics.fmean(c for _, c in closes[i - SMA_N + 1:i + 1])
            mom = closes[i][1] - closes[i - MOM_N][1]
            want = 1 if (closes[i][1] > sma and mom > 0) else 0
        else:
            want = 0
        # today's return accrues to yesterday's position
        if i > 0:
            r = closes[i][1] / closes[i - 1][1] - 1
            rets.append(position * r)
        if want != position:
            trades += 1
            if rets:
                rets[-1] -= COST_PER_SIDE  # cost on the switch day
        position = want
        pos_series.append(position)
    return rets, trades, pos_series


def stats(rets, label):
    eq = [1.0]
    for r in rets:
        eq.append(eq[-1] * (1 + r))
    total = eq[-1] - 1
    years = len(rets) / 365
    cagr = (eq[-1]) ** (1 / years) - 1 if years > 0 else 0
    peak, maxdd, dds = eq[0], 0, []
    cur_dd = 0
    for v in eq:
        peak = max(peak, v)
        dd = v / peak - 1
        cur_dd = min(cur_dd, dd)
        maxdd = min(maxdd, dd)
        if dd == 0 and cur_dd < 0:
            dds.append(cur_dd)
            cur_dd = 0
    if cur_dd < 0:
        dds.append(cur_dd)
    exposure = None
    return {
        "label": label, "total": total, "cagr": cagr, "maxdd": maxdd,
        "dd_list": sorted(dds)[:5], "final": eq[-1],
    }


def main():
    env = load_env()
    if not env.get("ALPACA_API_KEY"):
        print("Need Alpaca keys in .env (copy from a bot folder).")
        return
    print(f"Pre-registered rule: long when close > {SMA_N}d SMA and {MOM_N}d momentum > 0; else cash.")
    print(f"Cost model: {COST_PER_SIDE*10000:.0f} bps per side.\n")
    for sym in SYMBOLS:
        closes = fetch_daily_bars(env, sym, YEARS)
        if len(closes) < 100:
            print(f"{sym}: insufficient data ({len(closes)} bars)")
            continue
        rets, trades, pos = run_strategy(closes)
        s = stats(rets, f"{sym} trend")
        bh = stats([closes[i][1] / closes[i-1][1] - 1 for i in range(1, len(closes))], f"{sym} buy&hold")
        exposure = sum(pos) / len(pos) * 100
        yrs = len(rets) / 365
        print(f"=== {sym}  ({closes[0][0]} → {closes[-1][0]}, {yrs:.1f} years, {len(closes)} bars) ===")
        print(f"  STRATEGY : total {s['total']*100:+8.1f}%   CAGR {s['cagr']*100:+6.1f}%   maxDD {s['maxdd']*100:6.1f}%")
        print(f"  BUY&HOLD : total {bh['total']*100:+8.1f}%   CAGR {bh['cagr']*100:+6.1f}%   maxDD {bh['maxdd']*100:6.1f}%")
        print(f"  trades: {trades} ({trades/yrs:.0f}/yr)   time in market: {exposure:.0f}%")
        print(f"  worst 5 drawdowns: {['%.1f%%' % (d*100) for d in s['dd_list']]}")
        print()
    print("Reminder: informational only. Live decisions come from forward results.")


if __name__ == "__main__":
    main()
