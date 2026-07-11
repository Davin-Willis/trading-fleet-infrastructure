"""
regime_calculator.py — Market-Internals Regime Calculator (STANDALONE)
=======================================================================

Does NOT trade. Does NOT touch any account. It computes a single market-regime
signal once per session and writes it to a shared file that any bot may read.

The regime is derived from three internals (all computable from Alpaca bars —
no VIX feed, no paid data):

  1. BREADTH   — % of a large-cap basket trading above its own 200-day SMA.
                 Broad participation = healthy tape. Thin = fragile.
  2. TREND     — SPY vs its own 200-day and 50-day SMA (price regime of the index).
  3. VOL PROXY — VXX 5-day change. Alpaca has no VIX, so VXX (short-term vol ETP)
                 is the proxy: rising VXX = fear rising = risk-off pressure.

Output state (written to REGIME_STATE_FILE as JSON):
    "risk_on"   — breadth healthy, SPY above both SMAs, vol not spiking
    "neutral"   — mixed signals
    "risk_off"  — breadth weak OR SPY below 200-SMA OR vol spiking

IMPORTANT: this file is ADVISORY. It only writes a state file and posts to
Discord. Whether any bot reads/acts on it is entirely up to that bot. A wrong
regime label here cannot by itself place a trade.

The state file shape:
{
  "state": "risk_on" | "neutral" | "risk_off",
  "score": <int -3..+3>,
  "asof": "<ISO timestamp>",
  "asof_date": "YYYY-MM-DD",
  "breadth_pct": <float>,
  "spy_above_200": <bool>,
  "spy_above_50": <bool>,
  "vxx_5d_change_pct": <float>,
  "detail": { ... raw components ... }
}

Setup:  pip install alpaca-py requests pytz python-dotenv

Env vars (all optional except ALPACA_ keys; DISCORD optional):
    ALPACA_API_KEY / ALPACA_SECRET_KEY   (read-only data use; no orders ever)
    ALPACA_PAPER=true
    DISCORD_WEBHOOK_URL      -> optional; posts a daily regime line if set
    REGIME_STATE_FILE        -> default "/home/ubuntu/shared/regime_state.json"
    BREADTH_BASKET           -> default a 30-name large-cap basket (comma list)
    CHECK_TIME               -> default "09:45" ET (after the open settles)
    CLOSED_MARKET_SLEEP_SECONDS -> default "1800"
    OPEN_MARKET_SLEEP_SECONDS   -> default "600"

NOTE: This process never submits orders. It only reads market data and writes
a file. ALPACA_PAPER is honored for client init but no order path exists here.
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, date, timedelta, time as dtime, timezone

import requests
import pytz
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

load_dotenv()

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

REGIME_STATE_FILE = os.environ.get("REGIME_STATE_FILE", "/home/ubuntu/shared/regime_state.json")

DEFAULT_BASKET = ("AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,JPM,V,UNH,HD,PG,JNJ,XOM,"
                  "CVX,BAC,WMT,KO,PEP,COST,MRK,ABBV,CRM,ADBE,NFLX,AMD,INTC,CSCO,"
                  "WFC,DIS")
BREADTH_BASKET = [s.strip().upper() for s in
                  os.environ.get("BREADTH_BASKET", DEFAULT_BASKET).split(",") if s.strip()]

_ct_hh, _ct_mm = os.environ.get("CHECK_TIME", "09:45").split(":")
CHECK_TIME = dtime(int(_ct_hh), int(_ct_mm))
CLOSED_MARKET_SLEEP_SECONDS = int(os.environ.get("CLOSED_MARKET_SLEEP_SECONDS", "1800"))
OPEN_MARKET_SLEEP_SECONDS = int(os.environ.get("OPEN_MARKET_SLEEP_SECONDS", "600"))

EASTERN = pytz.timezone("US/Eastern")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("regime")

if not API_KEY or not SECRET_KEY:
    log.error("ALPACA_API_KEY / ALPACA_SECRET_KEY not set. Exiting.")
    sys.exit(1)

# Data client only. TradingClient used solely for the market clock (no orders).
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def is_market_open() -> bool:
    try:
        return bool(trading_client.get_clock().is_open)
    except Exception as exc:
        log.warning("Could not fetch clock (%s); assuming closed.", exc)
        return False


def get_daily_closes(symbol: str, need: int) -> list:
    """Newest `need` completed daily closes. Generous window + high limit +
    slice-newest (never a small limit with a far-back start)."""
    start = datetime.now(timezone.utc) - timedelta(days=int(need * 1.7) + 20)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        limit=10000,
    )
    try:
        bars = stock_data_client.get_stock_bars(req).data.get(symbol, [])
    except Exception as exc:
        log.warning("%s: daily bar fetch failed (%s).", symbol, exc)
        return []
    if not bars:
        return []
    today_et = datetime.now(EASTERN).date()
    if bars[-1].timestamp.astimezone(EASTERN).date() >= today_et:
        bars = bars[:-1]  # drop today's forming bar
    closes = [b.close for b in bars]
    return closes[-need:] if len(closes) > need else closes


def sma(values: list, period: int):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def compute_breadth() -> tuple:
    """% of basket names above their own 200-day SMA. Returns (pct, counted)."""
    above = 0
    counted = 0
    for sym in BREADTH_BASKET:
        closes = get_daily_closes(sym, 205)
        s200 = sma(closes, 200)
        if s200 is None or not closes:
            continue
        counted += 1
        if closes[-1] > s200:
            above += 1
    pct = (100.0 * above / counted) if counted else None
    return pct, counted


def compute_spy_trend() -> tuple:
    closes = get_daily_closes("SPY", 205)
    s200 = sma(closes, 200)
    s50 = sma(closes, 50)
    if not closes or s200 is None or s50 is None:
        return None, None, None
    price = closes[-1]
    return price > s200, price > s50, price


def compute_vxx_change() -> float:
    """VXX 5-day % change as a fear proxy. Positive = vol rising = risk-off."""
    closes = get_daily_closes("VXX", 8)
    if len(closes) < 6:
        return None
    return 100.0 * (closes[-1] / closes[-6] - 1.0)


def compute_regime() -> dict:
    breadth_pct, breadth_n = compute_breadth()
    spy_above_200, spy_above_50, spy_price = compute_spy_trend()
    vxx_5d = compute_vxx_change()

    # Score each component: +1 healthy, -1 unhealthy, 0 unknown/neutral.
    score = 0
    components = {}

    # Breadth: >60% bullish, <40% bearish
    if breadth_pct is None:
        components["breadth"] = 0
    elif breadth_pct >= 60:
        components["breadth"] = 1; score += 1
    elif breadth_pct <= 40:
        components["breadth"] = -1; score -= 1
    else:
        components["breadth"] = 0

    # SPY trend: above 200 is the big one; 50 adds nuance
    if spy_above_200 is None:
        components["spy_trend"] = 0
    else:
        t = (1 if spy_above_200 else -1)
        if spy_above_200 and spy_above_50:
            t = 1
        elif (not spy_above_200) and (not spy_above_50):
            t = -1
        components["spy_trend"] = t
        score += t

    # Vol proxy: VXX up >15% over 5d = fear spike (risk-off); down = calm
    if vxx_5d is None:
        components["vol"] = 0
    elif vxx_5d >= 15:
        components["vol"] = -1; score -= 1
    elif vxx_5d <= -5:
        components["vol"] = 1; score += 1
    else:
        components["vol"] = 0

    if score >= 2:
        state = "risk_on"
    elif score <= -1:
        state = "risk_off"
    else:
        state = "neutral"

    return {
        "state": state,
        "score": score,
        "asof": datetime.now(timezone.utc).isoformat(),
        "asof_date": date.today().isoformat(),
        "breadth_pct": round(breadth_pct, 1) if breadth_pct is not None else None,
        "breadth_names_counted": breadth_n,
        "spy_above_200": spy_above_200,
        "spy_above_50": spy_above_50,
        "spy_price": round(spy_price, 2) if spy_price else None,
        "vxx_5d_change_pct": round(vxx_5d, 2) if vxx_5d is not None else None,
        "components": components,
    }


def write_state(state: dict):
    os.makedirs(os.path.dirname(REGIME_STATE_FILE), exist_ok=True)
    tmp = f"{REGIME_STATE_FILE}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, REGIME_STATE_FILE)
        log.info("Regime written: %s (score %d) -> %s",
                 state["state"], state["score"], REGIME_STATE_FILE)
    except Exception as exc:
        log.error("Failed to write regime state: %s", exc)


def post_discord(state: dict):
    if not DISCORD_WEBHOOK_URL:
        return
    color = {"risk_on": 0x2ecc71, "neutral": 0xf1c40f, "risk_off": 0xe74c3c}.get(state["state"], 0x95a5a6)
    emoji = {"risk_on": "🟢", "neutral": "🟡", "risk_off": "🔴"}.get(state["state"], "⚪")
    b = state.get("breadth_pct")
    embed = {
        "title": f"{emoji} Market Regime: {state['state'].upper().replace('_', '-')}",
        "description": f"Composite score **{state['score']:+d}** (−3 … +3). Advisory signal for the fleet.",
        "color": color,
        "fields": [
            {"name": "Breadth (>200SMA)", "value": f"{b:.0f}%" if b is not None else "—", "inline": True},
            {"name": "SPY > 200 / 50", "value": f"{state['spy_above_200']} / {state['spy_above_50']}", "inline": True},
            {"name": "VXX 5d", "value": f"{state['vxx_5d_change_pct']:+.1f}%" if state['vxx_5d_change_pct'] is not None else "—", "inline": True},
        ],
        "footer": {"text": "Regime calculator · advisory only, does not trade"},
        "timestamp": state["asof"],
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        log.error("Discord regime post failed: %s", exc)


def _due_today(last_date) -> bool:
    now_et = datetime.now(EASTERN)
    if now_et.time() < CHECK_TIME:
        return False
    return last_date != date.today().isoformat()


def main():
    log.info("Starting regime calculator (STANDALONE, no trading). Basket=%d names, "
             "check after %s ET, writing %s.",
             len(BREADTH_BASKET), CHECK_TIME.strftime("%H:%M"), REGIME_STATE_FILE)
    last_computed_date = None
    # On boot, if no state file exists yet, compute once immediately so readers
    # have something to read even before the first scheduled check.
    if not os.path.exists(REGIME_STATE_FILE):
        try:
            st = compute_regime()
            write_state(st)
            post_discord(st)
            last_computed_date = st["asof_date"]
        except Exception as exc:
            log.exception("Initial regime computation failed: %s", exc)

    while True:
        market_open = False
        try:
            market_open = is_market_open()
            if market_open and _due_today(last_computed_date):
                st = compute_regime()
                write_state(st)
                post_discord(st)
                last_computed_date = st["asof_date"]
        except Exception as exc:
            log.exception("Unhandled error in regime cycle: %s", exc)
        time.sleep(OPEN_MARKET_SLEEP_SECONDS if market_open else CLOSED_MARKET_SLEEP_SECONDS)


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# systemd unit — save as /etc/systemd/system/regime-calc.service
# (filename on disk must match: regime_calculator.py)
# ---------------------------------------------------------------------------
# [Unit]
# Description=Market Regime Calculator
# After=network-online.target
# Wants=network-online.target
#
# [Service]
# Type=simple
# User=ubuntu
# WorkingDirectory=/home/ubuntu/regime_calc
# ExecStart=/bin/bash -c '/usr/bin/python3 -u /home/ubuntu/regime_calc/regime_calculator.py >> /home/ubuntu/regime_calc/regime.log 2>&1'
# Restart=always
# RestartSec=10
# MemoryMax=200M
#
# [Install]
# WantedBy=multi-user.target
