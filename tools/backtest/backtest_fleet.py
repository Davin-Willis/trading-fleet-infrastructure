#!/usr/bin/env python3
"""
backtest_fleet.py — daily-bar backtests of the fleet's share strategies,
using each bot's ACTUAL production parameters (read from the bot code).

INFORMATIONAL ONLY per fleet policy: results calibrate expectations and
sanity-check mechanisms; live decisions come from forward results.

Covered (exact rules):
  - Momentum Rotation : UNIVERSE=SPY,QQQ,IWM,GLD; 90-calendar-day trailing
                        return; rotate Mondays; cash if best < 0 (absolute
                        momentum); 95% allocation.
  - Defensive Rotation: SPY vs TLT vs cash; 126-trading-day total return;
                        daily check; hold winner if its return > 0 else cash.
  - Pairs Rel Value   : ratio SPY/QQQ; z-score vs 60d rolling mean/std;
                        enter |z|>2 (long cheap/short rich), exit |z|<0.5,
                        stop |z|>3.5; dollar-neutral.
  - Overnight Drift   : buy SPY+QQQ at close (50% each), sell at next open.
  - Passive Benchmark : buy SPY, hold.
Covered (APPROXIMATION, flagged):
  - Gap Fade/Ride     : zones from daily OHLC (<0.25% none, 0.25-0.85 fade to
                        prior close, 0.85-2.5 ride one gap-distance, >2.5
                        stand down). Intraday path unknown from daily bars:
                        if target inside day's high/low -> assume hit; else
                        exit at close. Optimistic on fills; read directionally.

COSTS: liquid ETFs, commission-free: 5 bps per side (spread+slippage).
"""
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
YEARS = 5
COST = 0.0005  # 5 bps per side

STOCK_BARS = "https://data.alpaca.markets/v2/stocks/bars"


def load_env():
    env = {}
    p = BASE / ".env"
    for line in p.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def fetch_daily(env, symbols):
    headers = {"APCA-API-KEY-ID": env["ALPACA_API_KEY"],
               "APCA-API-SECRET-KEY": env.get("ALPACA_SECRET_KEY", env.get("ALPACA_API_SECRET", ""))}
    start = (datetime.now(timezone.utc) - timedelta(days=365 * YEARS + 220)).strftime("%Y-%m-%d")
    out = {s: [] for s in symbols}
    page = None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
                  "limit": 10000, "adjustment": "split", "feed": "iex"}
        if page:
            params["page_token"] = page
        r = requests.get(STOCK_BARS, headers=headers, params=params, timeout=60)
        r.raise_for_status()
        j = r.json()
        for s, bars in (j.get("bars") or {}).items():
            out[s] += [(b["t"][:10], float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])) for b in bars]
        page = j.get("next_page_token")
        if not page:
            break
    # align on common dates
    common = set.intersection(*(set(d for d, *_ in v) for v in out.values() if v))
    for s in out:
        out[s] = sorted([r for r in out[s] if r[0] in common])
    return out


def stats(daily_rets, label, trades=None, exposure=None, note=""):
    eq = [1.0]
    for r in daily_rets:
        eq.append(eq[-1] * (1 + r))
    yrs = len(daily_rets) / 252
    cagr = eq[-1] ** (1 / yrs) - 1 if yrs > 0 else 0
    peak, maxdd = eq[0], 0.0
    dds, cur = [], 0.0
    for v in eq:
        peak = max(peak, v)
        dd = v / peak - 1
        cur = min(cur, dd)
        maxdd = min(maxdd, dd)
        if dd == 0 and cur < 0:
            dds.append(cur); cur = 0.0
    if cur < 0:
        dds.append(cur)
    line = (f"  {label:<22} total {(eq[-1]-1)*100:+8.1f}%  CAGR {cagr*100:+6.1f}%  "
            f"maxDD {maxdd*100:6.1f}%")
    if trades is not None:
        line += f"  trades/yr {trades/yrs:4.0f}"
    if exposure is not None:
        line += f"  in-mkt {exposure*100:3.0f}%"
    if note:
        line += f"  [{note}]"
    print(line)
    worst = sorted(dds)[:3]
    print(f"  {'':<22} worst DDs: {['%.1f%%' % (d*100) for d in worst]}")


# ------------------------------------------------------------ strategies ---
def bench(data):
    c = [r[4] for r in data["SPY"]]
    stats([c[i]/c[i-1]-1 for i in range(1, len(c))], "Passive Benchmark", trades=1, exposure=1.0)


def momentum(data):
    uni = ["SPY", "QQQ", "IWM", "GLD"]
    closes = {s: [r[4] for r in data[s]] for s in uni}
    dates = [r[0] for r in data["SPY"]]
    look = 63  # ~90 calendar days in trading days
    held, rets, trades = None, [], 0
    for i in range(1, len(dates)):
        if held:
            rets.append(closes[held][i]/closes[held][i-1] - 1)
        else:
            rets.append(0.0)
        wd = datetime.strptime(dates[i], "%Y-%m-%d").weekday()
        if wd == 0 and i >= look:  # Monday rotation
            perf = {s: closes[s][i]/closes[s][i-look] - 1 for s in uni}
            best = max(perf, key=perf.get)
            want = best if perf[best] > 0 else None
            if want != held:
                trades += 1 + (1 if held and want else 0)
                rets[-1] -= COST * (2 if held and want else 1)
                held = want
    exp = sum(1 for r in rets if r != 0) / len(rets)
    stats(rets, "Momentum Rotation", trades=trades, exposure=exp)


def defensive(data):
    look = 126
    cs, ct = [r[4] for r in data["SPY"]], [r[4] for r in data["TLT"]]
    held, rets, trades = None, [], 0
    series = {"SPY": cs, "TLT": ct}
    for i in range(1, len(cs)):
        rets.append(series[held][i]/series[held][i-1]-1 if held else 0.0)
        if i >= look:
            rs = cs[i]/cs[i-look] - 1
            rt = ct[i]/ct[i-look] - 1
            want = "SPY" if (rs >= rt and rs > 0) else ("TLT" if rt > 0 else None)
            if want != held:
                trades += 1 + (1 if held and want else 0)
                rets[-1] -= COST * (2 if held and want else 1)
                held = want
    exp = sum(1 for r in rets if r != 0)/len(rets)
    stats(rets, "Defensive Rotation", trades=trades, exposure=exp)


def pairs(data):
    W, ZE, ZX, ZS = 60, 2.0, 0.5, 3.5
    a = [r[4] for r in data["SPY"]]
    b = [r[4] for r in data["QQQ"]]
    ratio = [x/y for x, y in zip(a, b)]
    pos, rets, trades = 0, [], 0  # +1 long A short B, -1 reverse
    for i in range(1, len(ratio)):
        ra, rb = a[i]/a[i-1]-1, b[i]/b[i-1]-1
        rets.append(pos * 0.5 * (ra - rb))
        if i < W:
            continue
        win = ratio[i-W+1:i+1]
        mu, sd = statistics.fmean(win), statistics.pstdev(win)
        z = (ratio[i]-mu)/sd if sd else 0
        want = pos
        if pos == 0 and abs(z) > ZE:
            want = -1 if z > 0 else 1
        elif pos != 0 and (abs(z) < ZX or abs(z) > ZS):
            want = 0
        if want != pos:
            trades += 1
            rets[-1] -= COST * 2  # two legs
            pos = want
    exp = sum(1 for r in rets if r != 0)/len(rets)
    stats(rets, "Pairs Rel Value", trades=trades, exposure=exp)


def overnight(data):
    rets = []
    for s in ("SPY", "QQQ"):
        rows = data[s]
        leg = []
        for i in range(1, len(rows)):
            _, o, _, _, _ = rows[i]
            pc = rows[i-1][4]
            leg.append(o/pc - 1 - 2*COST)  # in at close, out at open, daily round trip
        rets.append(leg)
    combined = [0.5*(x+y) for x, y in zip(*rets)]
    stats(combined, "Overnight Drift", trades=len(combined)*2, exposure=1.0,
          note="every night, 50/50 SPY+QQQ")


def gap(data):
    MINP, RIDEP, MAXP = 0.0025, 0.0085, 0.025
    rets, trades = [], 0
    for s in ("SPY", "QQQ"):
        pass
    dates = range(1, len(data["SPY"]))
    daily = []
    for i in dates:
        day_ret = 0.0
        for s in ("SPY", "QQQ"):
            d, o, h, l, c = data[s][i]
            pc = data[s][i-1][4]
            g = o/pc - 1
            ag = abs(g)
            if ag < MINP or ag > MAXP:
                continue
            if ag < RIDEP:  # fade toward prior close
                direction = -1 if g > 0 else 1
                target = pc
            else:           # ride one gap-distance
                direction = 1 if g > 0 else -1
                target = o * (1 + direction * ag)
            hit = (l <= target <= h)
            exit_p = target if hit else c
            r = direction * (exit_p/o - 1) - 2*COST
            day_ret += 0.5 * r
            nonlocal_trades = 1
        daily.append(day_ret)
    trades = sum(1 for r in daily if r != 0)
    exp = trades/len(daily)
    stats(daily, "Gap Fade/Ride", trades=trades, exposure=exp,
          note="APPROX: daily OHLC, optimistic fills")


def main():
    env = load_env()
    syms = ["SPY", "QQQ", "IWM", "GLD", "TLT"]
    print(f"Fetching {YEARS}y daily bars (IEX feed) for {syms} ...")
    data = fetch_daily(env, syms)
    n = len(data["SPY"])
    print(f"{n} common trading days: {data['SPY'][0][0]} → {data['SPY'][-1][0]}")
    print(f"Cost model: {COST*10000:.0f} bps per side\n")
    bench(data)
    momentum(data)
    defensive(data)
    pairs(data)
    overnight(data)
    gap(data)
    print("\nReminder: informational only. The fleet trades on forward results.")


if __name__ == "__main__":
    main()
