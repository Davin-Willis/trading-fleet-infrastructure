#!/usr/bin/env python3
"""
backtest_fleet_intraday.py — Tier 2: 15-minute-bar backtests of the fleet's
intraday strategies, using each bot's ACTUAL production parameters.

INFORMATIONAL ONLY per fleet policy.

Strategies (exact rules from bot code):
  Mean Reversion : SPY+QQQ 15-min. SMA(20) ± 2.0σ bands, RSI(3).
                   Long: close<lower & RSI<15. Short: close>upper & RSI>85.
                   TP at SMA, stop 2%, time stop 26 bars, 1% risk sizing
                   (=> ~50% equity notional per position), max 2 concurrent,
                   3% daily-loss halt.
  VWAP MeanRev   : identical, but band anchor = session VWAP (>=6 session
                   bars before trusted); band width = rolling 20-bar stddev.
  ORB Breakout   : QQQ 15-min opening range (09:30-09:45 ET). Skip if
                   |overnight gap|>1.5%. Break above high -> long 3x proxy;
                   below low -> short 3x proxy (TQQQ/SQQQ modeled as ±3x QQQ
                   intraday moves). Stop 3% / target 6% on the leveraged leg,
                   flatten 15:55, 1% risk sizing (=> ~33% notional), one
                   entry per day.

DATA CAVEATS (stated up front):
  - IEX feed: subset of consolidated volume; intraday prints can differ from
    tape. Fine for character, not penny precision.
  - Fills assumed at bar close crossing the trigger; stops/targets checked
    against bar high/low (if both hit in one bar, STOP is assumed first —
    pessimistic tie-break).
  - TQQQ/SQQQ as ±3x QQQ ignores leveraged-ETF financing/decay intraday
    (small over hours).
Costs: 5 bps/side (SPY/QQQ), 10 bps/side for the 3x legs.
"""
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).resolve().parent
YEARS = 5
ET = ZoneInfo("America/New_York")
COST = 0.0005
COST3X = 0.0010

# meanrev / vwap params (from bot code)
SMA_N, BAND_K = 20, 2.0
RSI_N, RSI_OS, RSI_OB = 3, 15.0, 85.0
STOP_PCT, MAX_HOLD, RISK_PCT = 0.02, 26, 1.0
DAILY_HALT = 3.0
NOTIONAL_MR = RISK_PCT / 100 / STOP_PCT      # 0.50 of equity
MIN_SESSION_BARS = 6
# orb params
ORB_GAP, ORB_STOP, ORB_TGT = 0.015, 0.03, 0.06
NOTIONAL_ORB = RISK_PCT / 100 / ORB_STOP     # 0.333 of equity


def load_env():
    env = {}
    for line in (BASE / ".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def fetch_15m(env, symbol):
    """5y of 15-min bars, RTH only, as list of dicts per session day."""
    headers = {"APCA-API-KEY-ID": env["ALPACA_API_KEY"],
               "APCA-API-SECRET-KEY": env.get("ALPACA_SECRET_KEY", env.get("ALPACA_API_SECRET", ""))}
    start = (datetime.now(timezone.utc) - timedelta(days=365 * YEARS)).strftime("%Y-%m-%d")
    rows, page = [], None
    while True:
        params = {"symbols": symbol, "timeframe": "15Min", "start": start,
                  "limit": 10000, "adjustment": "split", "feed": "iex"}
        if page:
            params["page_token"] = page
        r = requests.get("https://data.alpaca.markets/v2/stocks/bars",
                         headers=headers, params=params, timeout=90)
        r.raise_for_status()
        j = r.json()
        rows += j.get("bars", {}).get(symbol, [])
        page = j.get("next_page_token")
        if not page:
            break
    days = {}
    for b in rows:
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        if t.weekday() > 4:
            continue
        hm = t.hour * 60 + t.minute
        if hm < 9 * 60 + 30 or hm >= 16 * 60:
            continue
        d = t.date().isoformat()
        days.setdefault(d, []).append(
            {"hm": hm, "o": float(b["o"]), "h": float(b["h"]),
             "l": float(b["l"]), "c": float(b["c"]), "v": float(b["v"])})
    for d in days:
        days[d].sort(key=lambda x: x["hm"])
    return dict(sorted(days.items()))


def rsi(closes, n=RSI_N):
    if len(closes) < n + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(-n, 0):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def stats(day_rets, label, trades, note=""):
    eq = [1.0]
    for r in day_rets:
        eq.append(eq[-1] * (1 + r))
    yrs = len(day_rets) / 252
    cagr = eq[-1] ** (1 / yrs) - 1 if yrs > 0 else 0
    peak, maxdd = 1.0, 0.0
    for v in eq:
        peak = max(peak, v)
        maxdd = min(maxdd, v / peak - 1)
    print(f"  {label:<16} total {(eq[-1]-1)*100:+8.1f}%  CAGR {cagr*100:+6.1f}%  "
          f"maxDD {maxdd*100:6.1f}%  trades/yr {trades/yrs:4.0f}  {note}")


def run_meanrev(data_by_sym, anchor="sma"):
    """One pass serves both bots; anchor='sma' or 'vwap'."""
    all_days = sorted(set().union(*[set(d) for d in data_by_sym.values()]))
    day_rets, trades = [], 0
    hist = {s: [] for s in data_by_sym}          # rolling closes across days
    for day in all_days:
        pnl, halted = 0.0, False
        pos = {}                                  # sym -> dict
        sess = {s: {"pv": 0.0, "vv": 0.0, "n": 0} for s in data_by_sym}
        nbars = max(len(data_by_sym[s].get(day, [])) for s in data_by_sym)
        for i in range(nbars):
            for s, days in data_by_sym.items():
                bars = days.get(day, [])
                if i >= len(bars):
                    continue
                b = bars[i]
                hist[s].append(b["c"])
                if len(hist[s]) > 400:
                    hist[s] = hist[s][-400:]
                st = sess[s]
                tp = (b["h"] + b["l"] + b["c"]) / 3
                st["pv"] += tp * b["v"]; st["vv"] += b["v"]; st["n"] += 1
                # manage open position
                if s in pos:
                    p = pos[s]
                    p["bars"] += 1
                    stop = p["entry"] * (1 - STOP_PCT) if p["side"] > 0 else p["entry"] * (1 + STOP_PCT)
                    win = len(hist[s]) >= SMA_N
                    mean = (statistics.fmean(hist[s][-SMA_N:]) if anchor == "sma"
                            else (st["pv"] / st["vv"] if st["vv"] else b["c"]))
                    hit_stop = b["l"] <= stop if p["side"] > 0 else b["h"] >= stop
                    hit_tp = (b["h"] >= mean) if p["side"] > 0 else (b["l"] <= mean)
                    exit_p = None
                    if hit_stop:                       # pessimistic tie-break
                        exit_p = stop
                    elif hit_tp and win:
                        exit_p = mean
                    elif p["bars"] >= MAX_HOLD or i == nbars - 1:
                        exit_p = b["c"]
                    if exit_p is not None:
                        r = p["side"] * (exit_p / p["entry"] - 1) - 2 * COST
                        pnl += NOTIONAL_MR * r
                        trades += 1
                        del pos[s]
                        if pnl <= -DAILY_HALT / 100:
                            halted = True
                    continue
                # consider entry
                if halted or len(pos) >= 2 or len(hist[s]) < SMA_N + 1:
                    continue
                if anchor == "vwap" and st["n"] < MIN_SESSION_BARS:
                    continue
                window = hist[s][-SMA_N:]
                sd = statistics.pstdev(window)
                mean = (statistics.fmean(window) if anchor == "sma"
                        else st["pv"] / st["vv"] if st["vv"] else None)
                if not mean or not sd:
                    continue
                r3 = rsi(hist[s])
                if b["c"] < mean - BAND_K * sd and r3 < RSI_OS:
                    pos[s] = {"side": 1, "entry": b["c"], "bars": 0}
                elif b["c"] > mean + BAND_K * sd and r3 > RSI_OB:
                    pos[s] = {"side": -1, "entry": b["c"], "bars": 0}
        day_rets.append(pnl)
    return day_rets, trades


def run_orb(qqq):
    day_rets, trades = [], 0
    days = sorted(qqq.keys())
    prev_close = None
    for day in days:
        bars = qqq[day]
        pnl = 0.0
        if len(bars) >= 2 and prev_close:
            gap = abs(bars[0]["o"] / prev_close - 1)
            if gap <= ORB_GAP:
                rng_h, rng_l = bars[0]["h"], bars[0]["l"]
                side, entry = 0, None
                for b in bars[1:]:
                    if side == 0:
                        if b["c"] > rng_h:
                            side, entry = 1, b["c"]
                        elif b["c"] < rng_l:
                            side, entry = -1, b["c"]
                        continue
                    # manage 3x proxy position (moves = 3 * QQQ move)
                    move_h = 3 * side * (b["h"] / entry - 1) if side > 0 else 3 * side * (b["l"] / entry - 1)
                    move_l = 3 * side * (b["l"] / entry - 1) if side > 0 else 3 * side * (b["h"] / entry - 1)
                    if move_l <= -ORB_STOP:            # stop first, pessimistic
                        pnl = NOTIONAL_ORB * (-ORB_STOP - 2 * COST3X); trades += 1; side = 2; break
                    if move_h >= ORB_TGT:
                        pnl = NOTIONAL_ORB * (ORB_TGT - 2 * COST3X); trades += 1; side = 2; break
                if side in (1, -1):                    # flatten at close
                    last = bars[-1]["c"]
                    mv = 3 * side * (last / entry - 1)
                    pnl = NOTIONAL_ORB * (mv - 2 * COST3X); trades += 1
        if bars:
            prev_close = bars[-1]["c"]
        day_rets.append(pnl)
    return day_rets, trades


def main():
    env = load_env()
    print("Fetching 5y of 15-min bars (IEX) — SPY, QQQ ... (this takes a minute)")
    spy = fetch_15m(env, "SPY")
    qqq = fetch_15m(env, "QQQ")
    n = len(set(spy) & set(qqq))
    print(f"{n} sessions: {min(spy)} → {max(spy)}")
    print(f"Costs: {COST*1e4:.0f} bps/side (SPY/QQQ), {COST3X*1e4:.0f} bps/side (3x legs)\n")

    data = {"SPY": spy, "QQQ": qqq}
    dr, t = run_meanrev(data, anchor="sma")
    stats(dr, "Mean Reversion", t)
    dr, t = run_meanrev(data, anchor="vwap")
    stats(dr, "VWAP MeanRev", t)
    dr, t = run_orb(qqq)
    stats(dr, "ORB Breakout", t, note="[3x proxy for TQQQ/SQQQ]")

    print("\nCaveats: IEX subset data; bar-granularity fills; stop-first tie-break.")
    print("Informational only — the fleet trades on forward results.")


if __name__ == "__main__":
    main()
