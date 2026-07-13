#!/usr/bin/env python3
"""
signal_analysis_trend.py — signal-level analysis of the trend bots' DAILY
signal (the one driving the weekly-options names). NOT a P/L backtest: options
economics can't be honestly simulated without historical chains. This answers
one question only:

    When the four-way signal fired (SMA band + 2-day confirm + RSI window +
    MACD agreement), did the underlying actually move the signaled direction
    over the ~7-day option window?

That splits the live trend bots' losses into "bad signal" vs "good signal,
bleeding options execution."

Rule (exact from bot code):
  bullish: close > SMA20*1.003 AND last 2 closes > SMA20 AND 50<=RSI14<=70
           AND MACD(12,26,9) line > signal
  bearish: mirrored (close < SMA20*0.997, 2 closes < SMA, 30<=RSI14<=50,
           MACD line < signal)

Measured per signal: underlying return over the next 5 trading days (~7
calendar = the weekly DTE), in the signaled direction.
"""
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

BASE = Path(__file__).resolve().parent
YEARS = 5
HOLD_TDAYS = 5
WATCH = ["SOFI", "XLF", "XLY", "XLI", "XLV", "XLP", "XLE", "XLU", "XLB",
         "XLRE", "JPM", "V", "UNH", "JNJ", "PG", "HD", "CAT", "HON", "VZ",
         "USO", "DBA", "EEM"]


def load_env():
    env = {}
    for line in (BASE / ".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def fetch(env, symbols):
    headers = {"APCA-API-KEY-ID": env["ALPACA_API_KEY"],
               "APCA-API-SECRET-KEY": env.get("ALPACA_SECRET_KEY", env.get("ALPACA_API_SECRET", ""))}
    start = (datetime.now(timezone.utc) - timedelta(days=365 * YEARS + 120)).strftime("%Y-%m-%d")
    out = {s: [] for s in symbols}
    page = None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
                  "limit": 10000, "adjustment": "split", "feed": "iex"}
        if page:
            params["page_token"] = page
        r = requests.get("https://data.alpaca.markets/v2/stocks/bars",
                         headers=headers, params=params, timeout=90)
        r.raise_for_status()
        j = r.json()
        for s, bars in (j.get("bars") or {}).items():
            out[s] += [float(b["c"]) for b in bars]
        page = j.get("next_page_token")
        if not page:
            break
    return out


def rsi14(closes, i):
    n = 14
    if i < n:
        return 50.0
    gains = losses = 0.0
    for k in range(i - n + 1, i + 1):
        ch = closes[k] - closes[k - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def macd_state(closes, i):
    """EMA12-EMA26 line vs its EMA9 signal at index i."""
    if i < 35:
        return 0.0, 0.0
    def ema(vals, n):
        k = 2 / (n + 1)
        e = vals[0]
        out = [e]
        for v in vals[1:]:
            e = v * k + e * (1 - k)
            out.append(e)
        return out
    window = closes[max(0, i - 120):i + 1]
    e12, e26 = ema(window, 12), ema(window, 26)
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema(line, 9)
    return line[-1], sig[-1]


def main():
    env = load_env()
    print(f"Fetching {YEARS}y daily bars for {len(WATCH)} watchlist tickers ...")
    data = fetch(env, WATCH)
    total = {"bullish": [], "bearish": []}
    per_ticker = {}
    for sym, closes in data.items():
        if len(closes) < 60:
            continue
        rets = {"bullish": [], "bearish": []}
        for i in range(40, len(closes) - HOLD_TDAYS):
            sma = statistics.fmean(closes[i - 19:i + 1])
            band = sma * 0.003
            c = closes[i]
            conf_b = all(x > sma for x in closes[i - 1:i + 1])
            conf_s = all(x < sma for x in closes[i - 1:i + 1])
            r = rsi14(closes, i)
            ml, ms = macd_state(closes, i)
            fwd = closes[i + HOLD_TDAYS] / c - 1
            if c > sma + band and conf_b and 50 <= r <= 70 and ml > ms:
                rets["bullish"].append(fwd)
            elif c < sma - band and conf_s and 30 <= r <= 50 and ml < ms:
                rets["bearish"].append(-fwd)  # directional: positive = right
        per_ticker[sym] = rets
        for d in rets:
            total[d] += rets[d]

    print(f"\n=== SIGNAL ACCURACY: {HOLD_TDAYS}-trading-day forward move in signaled direction ===")
    print(f"{'':<10}{'signals':>9}{'win %':>8}{'avg move':>10}{'median':>9}")
    for d in ("bullish", "bearish"):
        v = total[d]
        if not v:
            continue
        wins = sum(1 for x in v if x > 0) / len(v) * 100
        print(f"{d:<10}{len(v):>9}{wins:>7.1f}%{statistics.fmean(v)*100:>9.2f}%"
              f"{statistics.median(v)*100:>8.2f}%")
    both = total["bullish"] + total["bearish"]
    if both:
        wins = sum(1 for x in both if x > 0) / len(both) * 100
        print(f"{'ALL':<10}{len(both):>9}{wins:>7.1f}%{statistics.fmean(both)*100:>9.2f}%"
              f"{statistics.median(both)*100:>8.2f}%")

    print("\n=== Worst tickers by avg directional move (min 20 signals) ===")
    rows = []
    for sym, rets in per_ticker.items():
        v = rets["bullish"] + rets["bearish"]
        if len(v) >= 20:
            rows.append((statistics.fmean(v), sym, len(v),
                         sum(1 for x in v if x > 0) / len(v) * 100))
    rows.sort()
    for avg, sym, n, w in rows[:6]:
        print(f"  {sym:<6} n={n:<5} win {w:5.1f}%  avg {avg*100:+6.2f}%")
    print("\nInterpretation: >50% win rate & positive avg = signal has directional")
    print("edge (losses are execution/options bleed). <=50% or negative = the")
    print("signal itself lacks edge over the option's holding window.")
    print("Informational only — the frozen bots stay frozen either way.")


if __name__ == "__main__":
    main()
