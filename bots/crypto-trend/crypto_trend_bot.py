#!/usr/bin/env python3
"""
crypto_trend_bot.py — daily dual-signal trend follower for one crypto pair.

One codebase serves both crypto slots. The ONLY difference between the BTC
bot and the ETH bot is the SYMBOL in each bot's .env (single-variable A/B,
same as the fleet's trend pair).

PRE-REGISTERED RULE (backtested 2021-2026 before deployment; see
tools/backtest_crypto_trend.py):
    Once per day, on the completed daily bar:
      LONG  when close > SMA(20)  AND  momentum(30) > 0
      FLAT  (cash) otherwise.
    Long-or-flat. No shorting, no leverage, no intraday exits — the rule the
    backtest tested is the rule that trades.

SIZING (Option B, decided from backtest drawdown calibration):
    Position notional = 50% of account equity. Historical full-allocation
    drawdowns ran 45-50%; half-sizing targets ~22-25% worst case, inside the
    fleet's 25% kill-line enforced by the allocator.

ARCHITECTURE NOTES:
    - 24/7 asset: no market-hours logic anywhere. One decision at
      DECISION_HOUR_UTC:05 daily; a light hourly heartbeat loop otherwise.
    - Stateless position tracking: the bot asks Alpaca what it holds each
      cycle instead of trusting a local state file (lesson: an order
      submitted is not a position opened; local state drifts).
    - Bars fetched with a wide window anchored to now(UTC), sliced newest
      (lesson: small limit + today anchor returns stale oldest-first bars).

.env (per bot):
    ALPACA_API_KEY / ALPACA_SECRET_KEY   (this bot's own paper account)
    DISCORD_WEBHOOK_URL                  (this bot's own channel)
    SYMBOL=BTC/USD                       (or ETH/USD)
    ALLOC_PCT=0.50
    SMA_N=20
    MOM_N=30
    DECISION_HOUR_UTC=0
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

# ---------------------------------------------------------------- config ---
load_dotenv()
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")
SYMBOL = os.environ.get("SYMBOL", "BTC/USD")
ALLOC_PCT = float(os.environ.get("ALLOC_PCT", "0.50"))
SMA_N = int(os.environ.get("SMA_N", "20"))
MOM_N = int(os.environ.get("MOM_N", "30"))
DECISION_HOUR = int(os.environ.get("DECISION_HOUR_UTC", "0"))

DATA_BARS_URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
POS_SYMBOL = SYMBOL.replace("/", "")  # Alpaca positions use BTCUSD form

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cryptotrend")

if not API_KEY or not SECRET_KEY:
    log.error("Missing Alpaca keys in .env")
    sys.exit(1)

trading = TradingClient(API_KEY, SECRET_KEY, paper=True)

# ------------------------------------------------------------- messaging ---
def notify(msg: str):
    log.info("DISCORD: %s", msg)
    if not WEBHOOK:
        return
    try:
        requests.post(WEBHOOK, json={"content": msg[:1900]}, timeout=15)
    except Exception as exc:
        log.warning("Discord notify failed: %s", exc)

# ------------------------------------------------------------------ data ---
def get_daily_closes(need: int):
    """Wide window, large limit, slice newest — the standard fleet pattern."""
    headers = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
    start = (datetime.now(timezone.utc) - timedelta(days=need * 2 + 30)).strftime("%Y-%m-%d")
    params = {"symbols": SYMBOL, "timeframe": "1Day", "start": start, "limit": 10000}
    r = requests.get(DATA_BARS_URL, headers=headers, params=params, timeout=60)
    r.raise_for_status()
    bars = r.json().get("bars", {}).get(SYMBOL, [])
    closes = [float(b["c"]) for b in bars]
    if len(closes) < need:
        raise RuntimeError(f"only {len(closes)} daily bars returned, need {need}")
    return closes[-need:]

# -------------------------------------------------------------- position ---
def current_position_qty() -> float:
    """Ask the broker, not a state file."""
    try:
        pos = trading.get_open_position(POS_SYMBOL)
        return float(pos.qty)
    except Exception:
        return 0.0

def account_equity() -> float:
    return float(trading.get_account().equity)

# ------------------------------------------------------------------ rule ---
def decide() -> str:
    closes = get_daily_closes(max(SMA_N, MOM_N) + 1)
    last = closes[-1]
    sma = sum(closes[-SMA_N:]) / SMA_N
    mom = last - closes[-1 - MOM_N]
    want = "LONG" if (last > sma and mom > 0) else "FLAT"
    log.info("decision inputs: close=%.2f sma%d=%.2f mom%d=%+.2f -> %s",
             last, SMA_N, sma, MOM_N, mom, want)
    return want

# ---------------------------------------------------------------- orders ---
def wait_for_fill(order_id: str, timeout_s: int = 180):
    """Confirm fills via order-by-id; never trust submission."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        o = trading.get_order_by_id(order_id)
        status = str(o.status).lower()
        if "filled" in status and o.filled_avg_price:
            return float(o.filled_avg_price), float(o.filled_qty)
        if any(s in status for s in ("canceled", "rejected", "expired")):
            raise RuntimeError(f"order {order_id} ended {status}")
        time.sleep(3)
    raise RuntimeError(f"order {order_id} not filled within {timeout_s}s")

def go_long():
    equity = account_equity()
    notional = round(equity * ALLOC_PCT, 2)
    req = MarketOrderRequest(symbol=POS_SYMBOL, notional=notional,
                             side=OrderSide.BUY, time_in_force=TimeInForce.GTC)
    o = trading.submit_order(req)
    price, qty = wait_for_fill(str(o.id))
    notify(f"🟢 {SYMBOL} ENTER LONG — {qty:.6f} @ ${price:,.2f} "
           f"(~${notional:,.0f}, {ALLOC_PCT:.0%} of ${equity:,.0f})")

def go_flat(qty: float):
    req = MarketOrderRequest(symbol=POS_SYMBOL, qty=qty,
                             side=OrderSide.SELL, time_in_force=TimeInForce.GTC)
    o = trading.submit_order(req)
    price, sold = wait_for_fill(str(o.id))
    notify(f"🔴 {SYMBOL} EXIT to CASH — {sold:.6f} @ ${price:,.2f}")

# ------------------------------------------------------------- main loop ---
def run_decision():
    want = decide()
    qty = current_position_qty()
    have = "LONG" if qty > 0 else "FLAT"
    if want == have:
        notify(f"ℹ️ {SYMBOL} daily decision: stay {want} "
               f"(equity ${account_equity():,.0f})")
        return
    if want == "LONG":
        go_long()
    else:
        go_flat(qty)

def seconds_until_next_decision() -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=DECISION_HOUR, minute=5, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

def main():
    notify(f"🚀 crypto trend bot online — {SYMBOL}, rule: close>SMA{SMA_N} & "
           f"mom{MOM_N}>0, {ALLOC_PCT:.0%} sizing, decision {DECISION_HOUR:02d}:05 UTC")
    while True:
        try:
            wait = seconds_until_next_decision()
            log.info("next decision in %.1f h", wait / 3600)
            # hourly heartbeat sleep so failures surface within an hour
            while wait > 0:
                chunk = min(wait, 3600)
                time.sleep(chunk)
                wait -= chunk
            run_decision()
            time.sleep(120)  # drift past the decision minute
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log.exception("cycle error")
            notify(f"⚠️ {SYMBOL} bot error: {exc} — retrying in 15 min")
            time.sleep(900)

if __name__ == "__main__":
    main()
