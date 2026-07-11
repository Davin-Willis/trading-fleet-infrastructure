"""
bot_v2.py — Credit Spread Options Bot (PAPER TRADING — ALPACA_PAPER MUST BE "true")
======================================================================================

WHY THIS VERSION EXISTS — THE REDESIGN RATIONALE
--------------------------------------------------
The original bot (bot.py) bought long calls/puts when its signal fired.
Stage 1 and Stage 2 backtests across 1,194 real trades showed:
  - Stage 1 win rate: 48.3% (statistically significantly below 50%)
  - Stage 2 option win rate: 30.5%, total return: -64.1%, profit factor: 0.61
  - Root cause: the signal stack (SMA + 2-day confirm + RSI band + MACD) is a
    LATE-CONFIRMATION signal by design — all four conditions aligning means the
    underlying has already moved. Buying premium at that point means paying
    peak IV after the move, then fighting theta decay as the trade matures.

This version FLIPS the trade structure to exploit that timing instead of
fighting it:
  - Bullish signal  -> BULL PUT SPREAD  (sell ATM put, buy lower-strike put)
  - Bearish signal  -> BEAR CALL SPREAD (sell ATM call, buy higher-strike call)

Now the same late-confirmation timing becomes an ASSET:
  - We're selling elevated IV AFTER the move has confirmed
  - Theta works FOR us every day the underlying doesn't reverse
  - The risk is defined (spread width - credit received), not unlimited
  - We profit if the underlying stays roughly where it is OR continues the trend
    — we only lose if it reverses aggressively back through our short strike

WHAT IS UNCHANGED FROM bot.py
-------------------------------
  - ALL 47 watchlist symbols and their signal modes
  - The entire signal detection engine (daily SMA/RSI/MACD for weekly symbols,
    intraday 15-min stack for 0DTE symbols — though 0DTE now logs signals
    without trading them, see note below)
  - Correlation-aware risk pooling (RISK_PCT / cluster_size)
  - Daily portfolio heat cap (MAX_DAILY_RISK_PCT)
  - Fill-confirmation state machine (pending_entry -> open -> pending_close)
  - Partial-fill blending, cancel-confirm-before-escalate safety
  - Atomic state file writes, duplicate-contract guard, all infrastructure fixes
  - Entry/exit timing gates and startup guards

0DTE NOTE
----------
SPY, QQQ, IWM, GLD, TLT remain in the watchlist for SIGNAL MONITORING — their
intraday filter stack still runs every cycle and logs results. They are NOT
traded in this version. Reasons:
  1. Credit spreads on same-day expiration are near-binary gamma events, a
     fundamentally different risk profile than the weekly credit spread model.
  2. Stage 2 confirmed we have no reliable backtestable option data for 0DTE
     to validate any structure — we can't test what we can't measure.
  3. The signal-monitoring data will accumulate over real sessions; if a
     0DTE credit spread structure earns separate validation, it can be added
     then with real evidence rather than speculation.

SPREAD MECHANICS
-----------------
Bull Put Spread (bullish signal):
  - SELL the put at the strike nearest to (at or below) current price
  - BUY the put SPREAD_WIDTH points lower
  - Net credit received = short put premium - long put premium
  - Max risk = (SPREAD_WIDTH * 100) - (net credit * 100) per spread
  - Profit if underlying stays above short strike at expiration
  - Theta positive: every day that passes without a reversal is a gain

Bear Call Spread (bearish signal):
  - SELL the call at the strike nearest to (at or above) current price
  - BUY the call SPREAD_WIDTH points higher
  - Net credit received = short call premium - long call premium
  - Max risk = (SPREAD_WIDTH * 100) - (net credit * 100) per spread
  - Profit if underlying stays below short strike at expiration

EXIT RULES (fundamentally different from long-option exits)
------------------------------------------------------------
For credit spreads, P/L is measured as percentage of MAX CREDIT (not entry price):
  - PROFIT TARGET: close at CREDIT_PROFIT_TARGET_PCT (50% default) of max credit
    received. Standard industry practice — removes the tail risk of holding to
    expiration while capturing most of the available gain.
  - STOP LOSS: close if the spread has LOST CREDIT_STOP_LOSS_PCT (200% default)
    of the credit received. I.e., if you collected $1.00 net credit per spread,
    exit if it now costs $3.00 to close (a $2.00 loss, or 200% of original credit).
  - PRE-EXPIRATION CUTOFF: close CREDIT_DTE_CLOSE_DAYS (2 default) calendar
    days before expiration — avoids assignment risk and the extreme gamma of the
    final days, where a small move can wipe a credit spread quickly.

Hard safety guard
------------------
This script REFUSES TO START if ALPACA_PAPER is not "true".

Setup
-----
    pip install alpaca-py requests pytz python-dotenv
    (same dependencies as bot.py — no new packages needed)

Environment variables:
    ALPACA_API_KEY
    ALPACA_SECRET_KEY
    ALPACA_PAPER                       -> MUST be "true"
    DISCORD_WEBHOOK_URL_V2             -> separate from bot.py's webhook (recommended)
                                          falls back to DISCORD_WEBHOOK_URL if not set
    RISK_PCT_WEEKLY                    -> default "10.0" (% of equity per trade pre-pooling)
    MAX_DAILY_RISK_PCT                 -> default "20.0"
    ENTRY_POLL_MINUTES                 -> default "15"
    EXIT_POLL_MINUTES                  -> default "2"
    CLOSED_MARKET_SLEEP_SECONDS        -> default "1200"
    SPREAD_WIDTH_WEEKLY                -> default "0" (auto-select by price tier, see below)
    CREDIT_PROFIT_TARGET_PCT           -> default "0.50" (close at 50% of max credit)
    CREDIT_STOP_LOSS_PCT               -> default "2.00" (close if loss = 200% of credit)
    CREDIT_DTE_CLOSE_DAYS              -> default "2"
    ZERO_DTE_ENTRY_CUTOFF              -> default "14:45" (still used for timing gate, no 0DTE trades)
    POSITION_STATE_FILE_V2             -> default "open_positions_v2.json" (separate from bot.py!)
    ENTRY_FILL_TIMEOUT_SECONDS         -> default "300"
    CLOSE_FILL_TIMEOUT_SECONDS         -> default "90"
    EARNINGS_BLACKOUT_DAYS             -> default "5"
    FMP_API_KEY                        -> optional; enables earnings blackout check

    Intraday signal config (same as bot.py — kept identical for comparability):
    INTRADAY_TIMEFRAME_MINUTES / INTRADAY_SMA_LOOKBACK / RSI_PERIOD /
    RSI_BULLISH_MIN / RSI_BULLISH_MAX / RSI_BEARISH_MIN / RSI_BEARISH_MAX /
    MACD_FAST / MACD_SLOW / MACD_SIGNAL / CHOP_CONFIRM_BARS /
    INTRADAY_FETCH_DAYS / INTRADAY_FIXED_LOOKBACK_BARS

Disclaimer
----------
Educational / paper-testing tool only. Not financial advice. Credit spreads
carry defined but real risk of losing more than the credit received (up to the
full spread width per contract). This is not a promise of profitability.
"""

import os
import re
import sys
import json
import time
import math
import logging
from datetime import datetime, date, timedelta, time as dtime, timezone

import requests
import pytz
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce, PositionIntent
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, OptionLegRequest
from alpaca.data.enums import OptionsFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    OptionChainRequest,
    OptionLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

load_dotenv()

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

# Separate Discord webhook recommended — falls back to the original if not set
DISCORD_WEBHOOK_URL = (
    os.environ.get("DISCORD_WEBHOOK_URL_V3") or
    os.environ.get("DISCORD_WEBHOOK_URL_V2") or
    os.environ.get("DISCORD_WEBHOOK_URL")
)

RISK_PCT_WEEKLY = float(os.environ.get("RISK_PCT_WEEKLY", "3.0"))
MAX_DAILY_RISK_PCT = float(os.environ.get("MAX_DAILY_RISK_PCT", "8.0"))

# --- Quality filters (the core difference from bot_v2) ---
# Hard ceiling on simultaneously open positions. Once reached, NO new entry is
# opened regardless of how many signals fire — this is the structural stop that
# bot_v2 lacked, which let 40+ correlated positions pile up in one session.
MAX_CONCURRENT_POSITIONS = int(os.environ.get("MAX_CONCURRENT_POSITIONS", "8"))

# Minimum net credit as a FRACTION OF SPREAD WIDTH. A $1-wide spread must collect
# at least this * $1. Rejects the lopsided-risk spreads (near-zero credit, full
# width at risk) that made up much of bot_v2's losing book.
MIN_CREDIT_TO_WIDTH = float(os.environ.get("MIN_CREDIT_TO_WIDTH", "0.30"))

# Short strike target: how far OTM to sell, as a fraction of the underlying price.
# Selling OTM (not ATM like bot_v2) gives the underlying room to move against the
# position before the short strike is threatened. ~3% OTM ≈ 0.20-0.25 delta on a
# typical weekly. If the feed provides delta we use that instead (see below).
SHORT_OTM_PCT = float(os.environ.get("SHORT_OTM_PCT", "0.03"))

# If the chain snapshot exposes greeks, prefer selling the short leg at this delta.
# Falls back to SHORT_OTM_PCT moneyness if delta isn't available from the feed.
SHORT_TARGET_DELTA = float(os.environ.get("SHORT_TARGET_DELTA", "0.25"))

# Distance-from-recent-extreme filter: don't sell a bull put spread right after a
# sharp run-up, or a bear call right after a sharp drop — that's selling into
# exhaustion, where reversal-through-the-short-strike risk is highest. Skip the
# trade if price is within this fraction of its N-day high (bullish) or low
# (bearish). Set to 0 to disable.
EXTREME_LOOKBACK_DAYS = int(os.environ.get("EXTREME_LOOKBACK_DAYS", "10"))
EXTREME_PROXIMITY_PCT = float(os.environ.get("EXTREME_PROXIMITY_PCT", "0.02"))

# IV-richness floor: only sell when the short leg's premium-to-strike ratio clears
# a floor, a rough proxy for "options are expensive enough to be worth selling".
# Conservative + degrades gracefully: if we can't compute it, the trade proceeds.
MIN_SHORT_PREMIUM_PCT = float(os.environ.get("MIN_SHORT_PREMIUM_PCT", "0.004"))

ENTRY_POLL_SECONDS = int(float(os.environ.get("ENTRY_POLL_MINUTES", "15")) * 60)
EXIT_POLL_SECONDS = int(float(os.environ.get("EXIT_POLL_MINUTES", "2")) * 60)
CLOSED_MARKET_SLEEP_SECONDS = int(os.environ.get("CLOSED_MARKET_SLEEP_SECONDS", "1200"))

# Spread config
_SPREAD_WIDTH_OVERRIDE = float(os.environ.get("SPREAD_WIDTH_WEEKLY", "0"))
CREDIT_PROFIT_TARGET_PCT = float(os.environ.get("CREDIT_PROFIT_TARGET_PCT", "0.50"))
CREDIT_STOP_LOSS_PCT = float(os.environ.get("CREDIT_STOP_LOSS_PCT", "2.00"))
CREDIT_DTE_CLOSE_DAYS = int(os.environ.get("CREDIT_DTE_CLOSE_DAYS", "2"))

# Fill timeouts
ENTRY_FILL_TIMEOUT_SECONDS = int(os.environ.get("ENTRY_FILL_TIMEOUT_SECONDS", "300"))
CLOSE_FILL_TIMEOUT_SECONDS = int(os.environ.get("CLOSE_FILL_TIMEOUT_SECONDS", "90"))

EARNINGS_BLACKOUT_DAYS = int(os.environ.get("EARNINGS_BLACKOUT_DAYS", "5"))
FMP_API_KEY = os.environ.get("FMP_API_KEY")

POSITION_STATE_FILE = os.environ.get("POSITION_STATE_FILE_V3", "local_credit_positions_v3.json")

# Timing gates
_zero_dte_entry_hh, _zero_dte_entry_mm = os.environ.get("ZERO_DTE_ENTRY_CUTOFF", "14:45").split(":")
ZERO_DTE_ENTRY_CUTOFF = dtime(int(_zero_dte_entry_hh), int(_zero_dte_entry_mm))

# Daily indicator config (identical to bot.py)
SMA_LOOKBACK = 20
DAILY_RSI_PERIOD = int(os.environ.get("DAILY_RSI_PERIOD", "14"))
DAILY_RSI_BULLISH_MIN = float(os.environ.get("DAILY_RSI_BULLISH_MIN", "50"))
DAILY_RSI_BULLISH_MAX = float(os.environ.get("DAILY_RSI_BULLISH_MAX", "70"))
DAILY_RSI_BEARISH_MIN = float(os.environ.get("DAILY_RSI_BEARISH_MIN", "30"))
DAILY_RSI_BEARISH_MAX = float(os.environ.get("DAILY_RSI_BEARISH_MAX", "50"))
DAILY_MACD_FAST = int(os.environ.get("DAILY_MACD_FAST", "12"))
DAILY_MACD_SLOW = int(os.environ.get("DAILY_MACD_SLOW", "26"))
DAILY_MACD_SIGNAL = int(os.environ.get("DAILY_MACD_SIGNAL", "9"))
CHOP_CONFIRM_DAYS = int(os.environ.get("CHOP_CONFIRM_DAYS", "2"))

# Intraday indicator config (identical to bot.py — monitoring only in this version)
INTRADAY_TIMEFRAME_MINUTES = int(os.environ.get("INTRADAY_TIMEFRAME_MINUTES", "15"))
INTRADAY_SMA_LOOKBACK = int(os.environ.get("INTRADAY_SMA_LOOKBACK", "20"))
RSI_PERIOD = int(os.environ.get("RSI_PERIOD", "14"))
RSI_BULLISH_MIN = float(os.environ.get("RSI_BULLISH_MIN", "50"))
RSI_BULLISH_MAX = float(os.environ.get("RSI_BULLISH_MAX", "70"))
RSI_BEARISH_MIN = float(os.environ.get("RSI_BEARISH_MIN", "30"))
RSI_BEARISH_MAX = float(os.environ.get("RSI_BEARISH_MAX", "50"))
MACD_FAST = int(os.environ.get("MACD_FAST", "12"))
MACD_SLOW = int(os.environ.get("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.environ.get("MACD_SIGNAL", "9"))
CHOP_CONFIRM_BARS = int(os.environ.get("CHOP_CONFIRM_BARS", "2"))
INTRADAY_FETCH_DAYS = int(os.environ.get("INTRADAY_FETCH_DAYS", "21"))
INTRADAY_FIXED_LOOKBACK_BARS = int(os.environ.get("INTRADAY_FIXED_LOOKBACK_BARS", "300"))

ORDER_COOLDOWN_SECONDS = 60 * 60 * 4
MIN_NET_CREDIT = float(os.environ.get("MIN_NET_CREDIT", "0.10"))  # absolute floor; MIN_CREDIT_TO_WIDTH is the real gate

# Watchlist — 15 liquid names with tight options markets. 0DTE index/ETF symbols
# are MONITOR-ONLY (signal logged, never traded as a spread). Deliberately small:
# fewer simultaneous signals means the concurrency cap rarely has to fight a
# stampede, and every name here has deep, liquid weekly options.
WATCHLIST = {
    # 0DTE monitors (signal logged only, not traded)
    "SPY":  {"mode": "0dte", "signal_mode": "intraday", "monitor_only": True},
    "QQQ":  {"mode": "0dte", "signal_mode": "intraday", "monitor_only": True},
    "IWM":  {"mode": "0dte", "signal_mode": "intraday", "monitor_only": True},

    # Liquid sector ETFs
    "XLF":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "XLE":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "XLK":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "XLV":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},

    # Large, liquid single names
    "AAPL": {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "MSFT": {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "NVDA": {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "AMZN": {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "JPM":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "GLD":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "TLT":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "SOFI": {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
}

EASTERN = pytz.timezone("US/Eastern")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot_v3")

# --- Startup guards ---
if not API_KEY or not SECRET_KEY:
    log.error("ALPACA_API_KEY / ALPACA_SECRET_KEY not set. Exiting.")
    sys.exit(1)
if not DISCORD_WEBHOOK_URL:
    log.error("DISCORD_WEBHOOK_URL_V2 or DISCORD_WEBHOOK_URL not set. Exiting.")
    sys.exit(1)
if not PAPER:
    log.error("ALPACA_PAPER is not 'true'. This script refuses to submit live orders.")
    sys.exit(1)

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
option_data_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)

_last_trade_state = {}
_daily_state = {"date": None, "risk_used": 0.0}
_earnings_cache = {}
_EARNINGS_CACHE_TTL = 60 * 60 * 12


def _reset_daily_state_if_new_day():
    today_str = date.today().isoformat()
    if _daily_state["date"] != today_str:
        if _daily_state["date"] is not None:
            log.info("New trading day — resetting daily heat tracker.")
        _daily_state["date"] = today_str
        _daily_state["risk_used"] = 0.0


def _order_status_name(order) -> str:
    return str(order.status).split(".")[-1].lower()


# --------------------------------------------------------------------------
# State persistence (atomic writes)
# --------------------------------------------------------------------------

def _load_open_positions() -> dict:
    if not os.path.exists(POSITION_STATE_FILE):
        return {}
    try:
        with open(POSITION_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception as exc:
        log.error("Failed to load %s (%s) — starting with no tracked positions.", POSITION_STATE_FILE, exc)
        return {}


def _save_open_positions():
    temp_file = f"{POSITION_STATE_FILE}.tmp"
    try:
        with open(temp_file, "w") as f:
            json.dump(_open_positions, f, indent=2)
        os.replace(temp_file, POSITION_STATE_FILE)
    except Exception as exc:
        log.error("Failed to save %s: %s", POSITION_STATE_FILE, exc)


_open_positions = _load_open_positions()


# --------------------------------------------------------------------------
# Market / time gates
# --------------------------------------------------------------------------

def is_market_open() -> bool:
    try:
        return bool(trading_client.get_clock().is_open)
    except Exception as exc:
        log.warning("Could not fetch market clock (%s); assuming closed.", exc)
        return False


# --------------------------------------------------------------------------
# Stock data + indicator math (identical to bot.py)
# --------------------------------------------------------------------------

def get_latest_price(symbol: str) -> float:
    req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    quote = stock_data_client.get_stock_latest_quote(req)[symbol]
    bid, ask = quote.bid_price, quote.ask_price
    if bid and ask:
        return (bid + ask) / 2
    return ask or bid


def get_recent_closes(symbol: str, min_bars_needed: int) -> list:
    start = date.today() - timedelta(days=int(min_bars_needed * 1.8) + 5)
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                            start=start, limit=min_bars_needed + 30)
    bars = stock_data_client.get_stock_bars(req).data.get(symbol, [])
    closes = [bar.close for bar in bars]
    if bars and bars[-1].timestamp.date() == date.today():
        closes = closes[:-1]
    return closes


def get_intraday_closes(symbol: str, timeframe_minutes: int, fixed_bars: int) -> list:
    start = date.today() - timedelta(days=INTRADAY_FETCH_DAYS)
    req = StockBarsRequest(symbol_or_symbols=symbol,
                            timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
                            start=start, limit=2000)
    bars = stock_data_client.get_stock_bars(req).data.get(symbol, [])
    if not bars:
        return []
    closes = [bar.close for bar in bars]
    last_bar_age = (datetime.now(timezone.utc) - bars[-1].timestamp).total_seconds() / 60
    if last_bar_age < timeframe_minutes:
        closes = closes[:-1]
    if len(closes) > fixed_bars:
        closes = closes[-fixed_bars:]
    return closes


def compute_rsi(closes: list, period: int):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period-1) + gains[i]) / period
        avg_loss = (avg_loss * (period-1) + losses[i]) / period
    if avg_loss < 1e-9:
        return 50.0 if avg_gain < 1e-9 else 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _ema_series(values: list, period: int) -> list:
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    series = [ema_val]
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
        series.append(ema_val)
    return series


def compute_macd(closes: list, fast: int, slow: int, signal: int):
    if len(closes) < slow + signal:
        return None, None, None
    fast_series = _ema_series(closes, fast)
    slow_series = _ema_series(closes, slow)
    offset = slow - fast
    fast_aligned = fast_series[offset:]
    macd_series = [f - s for f, s in zip(fast_aligned, slow_series)]
    if len(macd_series) < signal:
        return None, None, None
    sig_series = _ema_series(macd_series, signal)
    return macd_series[-1], sig_series[-1], macd_series[-1] - sig_series[-1]


def evaluate_daily_sma_signal(symbol: str):
    needed = max(SMA_LOOKBACK, DAILY_MACD_SLOW + DAILY_MACD_SIGNAL, DAILY_RSI_PERIOD + 1) + CHOP_CONFIRM_DAYS + 5
    closes = get_recent_closes(symbol, needed)
    if len(closes) < needed:
        log.warning("Not enough daily bar history for %s — skipping.", symbol)
        return None, None, None
    sma = sum(closes[-SMA_LOOKBACK:]) / SMA_LOOKBACK
    price = get_latest_price(symbol)
    rsi = compute_rsi(closes, DAILY_RSI_PERIOD)
    macd_line, macd_sig, _ = compute_macd(closes, DAILY_MACD_FAST, DAILY_MACD_SLOW, DAILY_MACD_SIGNAL)
    if rsi is None or macd_line is None:
        return None, None, None
    band = sma * 0.003
    trend_bull = price > sma + band
    trend_bear = price < sma - band
    recent = closes[-CHOP_CONFIRM_DAYS:]
    confirm_bull = all(c > sma for c in recent)
    confirm_bear = all(c < sma for c in recent)
    rsi_bull_ok = DAILY_RSI_BULLISH_MIN <= rsi <= DAILY_RSI_BULLISH_MAX
    rsi_bear_ok = DAILY_RSI_BEARISH_MIN <= rsi <= DAILY_RSI_BEARISH_MAX
    macd_bull_ok = macd_line > macd_sig
    macd_bear_ok = macd_line < macd_sig
    direction = None
    if trend_bull and confirm_bull and rsi_bull_ok and macd_bull_ok:
        direction = "bullish"
    elif trend_bear and confirm_bear and rsi_bear_ok and macd_bear_ok:
        direction = "bearish"
    log.info("%s daily: price=%.2f sma%d=%.2f rsi=%.1f macd=%.3f sig=%.3f -> %s",
              symbol, price, SMA_LOOKBACK, sma, rsi, macd_line, macd_sig, direction or "no signal")
    return direction, price, sma


def evaluate_intraday_signal(symbol: str):
    needed = max(INTRADAY_SMA_LOOKBACK, MACD_SLOW + MACD_SIGNAL, RSI_PERIOD + 1) + CHOP_CONFIRM_BARS + 5
    closes = get_intraday_closes(symbol, INTRADAY_TIMEFRAME_MINUTES, INTRADAY_FIXED_LOOKBACK_BARS)
    if len(closes) < needed:
        log.warning("Not enough intraday history for %s — skipping.", symbol)
        return None, None, None
    sma = sum(closes[-INTRADAY_SMA_LOOKBACK:]) / INTRADAY_SMA_LOOKBACK
    price = get_latest_price(symbol)
    rsi = compute_rsi(closes, RSI_PERIOD)
    macd_line, macd_sig, _ = compute_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    if rsi is None or macd_line is None:
        return None, None, None
    band = sma * 0.003
    trend_bull = price > sma + band
    trend_bear = price < sma - band
    recent = closes[-CHOP_CONFIRM_BARS:]
    confirm_bull = all(c > sma for c in recent)
    confirm_bear = all(c < sma for c in recent)
    rsi_bull_ok = RSI_BULLISH_MIN <= rsi <= RSI_BULLISH_MAX
    rsi_bear_ok = RSI_BEARISH_MIN <= rsi <= RSI_BEARISH_MAX
    macd_bull_ok = macd_line > macd_sig
    macd_bear_ok = macd_line < macd_sig
    direction = None
    if trend_bull and confirm_bull and rsi_bull_ok and macd_bull_ok:
        direction = "bullish"
    elif trend_bear and confirm_bear and rsi_bear_ok and macd_bear_ok:
        direction = "bearish"
    log.info("%s intraday [MONITOR ONLY]: price=%.2f sma%d=%.2f rsi=%.1f macd=%.3f sig=%.3f -> %s",
              symbol, price, INTRADAY_SMA_LOOKBACK, sma, rsi, macd_line, macd_sig, direction or "no signal")
    return direction, price, sma


def evaluate_signal(symbol: str):
    cfg = WATCHLIST[symbol]
    if cfg.get("signal_mode") == "intraday":
        return evaluate_intraday_signal(symbol)
    return evaluate_daily_sma_signal(symbol)


# --------------------------------------------------------------------------
# Spread construction helpers
# --------------------------------------------------------------------------

_OCC_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$")


def _parse_occ_symbol(contract_symbol: str):
    m = _OCC_RE.match(contract_symbol)
    if not m:
        return None
    return {
        "strike": int(m.group("strike")) / 1000.0,
        "type": "call" if m.group("cp") == "C" else "put",
        "expiration": date(2000 + int(m.group("yy")), int(m.group("mm")), int(m.group("dd"))),
    }


def get_spread_width(underlying_price: float) -> float:
    if _SPREAD_WIDTH_OVERRIDE > 0:
        return _SPREAD_WIDTH_OVERRIDE
    if underlying_price < 100:
        return 1.0
    elif underlying_price < 500:
        return 2.5
    else:
        return 5.0


def get_target_expiration(symbol: str) -> date:
    """Next Friday on/after target_dte days out — approximates the nearest
    real weekly listing that's at least 7 days away."""
    today = date.today()
    target = today + timedelta(days=WATCHLIST[symbol]["target_dte"])
    days_to_friday = (4 - target.weekday()) % 7
    return target + timedelta(days=days_to_friday)


def _extract_quote(snapshot) -> dict:
    bid = ask = None
    latest_quote = getattr(snapshot, "latest_quote", None)
    if latest_quote:
        bid = getattr(latest_quote, "bid_price", None)
        ask = getattr(latest_quote, "ask_price", None)
    mid = (bid + ask) / 2 if (bid and ask) else (ask or bid)
    return {"bid": bid, "ask": ask, "mid": mid}


def _extract_delta(snapshot):
    """Return abs(delta) from the snapshot's greeks if the feed provides them,
    else None. Alpaca's option chain MAY include a `greeks` object depending on
    feed/subscription — we read it if present and silently fall back to
    moneyness-based selection if not. Never raises."""
    greeks = getattr(snapshot, "greeks", None)
    if greeks is None:
        return None
    delta = getattr(greeks, "delta", None)
    if delta is None:
        return None
    try:
        return abs(float(delta))
    except (TypeError, ValueError):
        return None


def select_spread_contracts(symbol: str, underlying_price: float, direction: str):
    """
    Return a dict with both legs of the credit spread, or None if the chain
    doesn't have usable quotes for both.

    Bullish -> Bull Put Spread: sell ATM put, buy lower put
    Bearish -> Bear Call Spread: sell ATM call, buy higher call
    """
    cfg = WATCHLIST[symbol]
    expiration = get_target_expiration(symbol)
    spread_width = get_spread_width(underlying_price)

    gte = date.today() + timedelta(days=cfg["target_dte"] - 3)
    lte = expiration + timedelta(days=3)
    strike_window = max(underlying_price * 0.08, 10.0)

    try:
        chain = option_data_client.get_option_chain(
            OptionChainRequest(
                underlying_symbol=symbol,
                expiration_date_gte=gte,
                expiration_date_lte=lte,
                strike_price_gte=round(underlying_price - strike_window, 2),
                strike_price_lte=round(underlying_price + strike_window, 2),
            )
        )
    except Exception as exc:
        log.error("Failed to fetch option chain for %s: %s", symbol, exc)
        return None

    if not chain:
        log.warning("Empty option chain for %s.", symbol)
        return None

    wanted_type = "put" if direction == "bullish" else "call"
    candidates = []
    for contract_symbol, snapshot in chain.items():
        parsed = _parse_occ_symbol(contract_symbol)
        if not parsed or parsed["type"] != wanted_type:
            continue
        quote = _extract_quote(snapshot)
        if not quote["mid"] or quote["mid"] <= 0:
            continue
        candidates.append({**parsed, "symbol": contract_symbol,
                           "delta": _extract_delta(snapshot), **quote})

    if not candidates:
        log.warning("No %s candidates for %s.", wanted_type, symbol)
        return None

    # Pick the expiration closest to our target (multiple expirations may be
    # present in the widened window)
    available_dates = {c["expiration"] for c in candidates}
    target_exp = get_target_expiration(symbol)
    best_exp = min(available_dates, key=lambda d: abs((d - target_exp).days))
    candidates = [c for c in candidates if c["expiration"] == best_exp]

    # --- Short leg: target an OTM strike, NOT ATM ---
    # Bull put spread sells a put BELOW spot; bear call spread sells a call ABOVE
    # spot. Selling OTM gives the underlying room to move against us before the
    # short strike is breached — the single biggest change from bot_v2's ATM sell.
    if direction == "bullish":
        otm_candidates = [c for c in candidates if c["strike"] < underlying_price]
    else:
        otm_candidates = [c for c in candidates if c["strike"] > underlying_price]

    if not otm_candidates:
        log.warning("%s: no OTM %s strikes available — skipping.", symbol, wanted_type)
        return None

    have_delta = any(c["delta"] is not None for c in otm_candidates)
    if have_delta:
        # Prefer the strike whose delta is closest to our target.
        deltaed = [c for c in otm_candidates if c["delta"] is not None]
        short_leg = min(deltaed, key=lambda c: abs(c["delta"] - SHORT_TARGET_DELTA))
        log.info("%s: short strike chosen by delta (%.2f target %.2f).",
                 symbol, short_leg["delta"], SHORT_TARGET_DELTA)
    else:
        # Fallback: choose the strike closest to SHORT_OTM_PCT away from spot.
        if direction == "bullish":
            target_strike = underlying_price * (1 - SHORT_OTM_PCT)
        else:
            target_strike = underlying_price * (1 + SHORT_OTM_PCT)
        short_leg = min(otm_candidates, key=lambda c: abs(c["strike"] - target_strike))
        log.info("%s: short strike chosen by moneyness (~%.0f%% OTM, no delta in feed).",
                 symbol, SHORT_OTM_PCT * 100)

    # IV-richness floor: short premium must be a meaningful fraction of strike.
    # Degrades gracefully — only rejects when we positively know it's too cheap.
    short_prem_pct = short_leg["mid"] / underlying_price if underlying_price else None
    if short_prem_pct is not None and short_prem_pct < MIN_SHORT_PREMIUM_PCT:
        log.warning("%s: short premium %.3f%% of underlying below floor %.3f%% — options too cheap, skipping.",
                    symbol, short_prem_pct * 100, MIN_SHORT_PREMIUM_PCT * 100)
        return None

    # Long leg: spread_width away in the protective direction
    if direction == "bullish":
        long_strike_target = short_leg["strike"] - spread_width
    else:
        long_strike_target = short_leg["strike"] + spread_width

    long_candidates = [c for c in candidates if abs(c["strike"] - long_strike_target) < spread_width * 0.6]
    if not long_candidates:
        log.warning("%s: no long leg found at spread width %.2f from short strike %.2f.",
                    symbol, spread_width, short_leg["strike"])
        return None

    long_leg = min(long_candidates, key=lambda c: abs(c["strike"] - long_strike_target))

    net_credit = short_leg["mid"] - long_leg["mid"]
    if net_credit <= 0:
        log.warning("%s: spread produces no credit (short=%.2f, long=%.2f) — skipping.",
                    symbol, short_leg["mid"], long_leg["mid"])
        return None

    if net_credit < MIN_NET_CREDIT:
        log.warning("%s: net credit $%.4f below absolute minimum $%.4f — skipping.", symbol, net_credit, MIN_NET_CREDIT)
        return None

    actual_width = abs(short_leg["strike"] - long_leg["strike"])

    # --- Credit-to-width filter: the real quality gate ---
    # On a $1-wide spread we must collect at least MIN_CREDIT_TO_WIDTH * $1.
    # Rejects the lopsided spreads (tiny credit, near-full width at risk) that
    # dominated bot_v2's losing book.
    credit_to_width = net_credit / actual_width if actual_width > 0 else 0
    if credit_to_width < MIN_CREDIT_TO_WIDTH:
        log.warning("%s: credit/width %.1f%% below floor %.0f%% (credit $%.2f, width $%.2f) — skipping.",
                    symbol, credit_to_width * 100, MIN_CREDIT_TO_WIDTH * 100, net_credit, actual_width)
        return None

    max_risk_per_spread = (actual_width * 100) - (net_credit * 100)
    if max_risk_per_spread <= 0:
        log.warning("%s: max risk per spread is non-positive — skipping.", symbol)
        return None

    return {
        "direction": direction,
        "short_leg": short_leg,
        "long_leg": long_leg,
        "net_credit": round(net_credit, 4),
        "credit_to_width": round(credit_to_width, 4),
        "spread_width": actual_width,
        "max_risk_per_spread": round(max_risk_per_spread, 2),
        "expiration": best_exp.isoformat(),
    }


def get_option_latest_quote(contract_symbol: str):
    try:
        req = OptionLatestQuoteRequest(symbol_or_symbols=contract_symbol, feed=OptionsFeed.INDICATIVE)
        quote = option_data_client.get_option_latest_quote(req)[contract_symbol]
        bid, ask = quote.bid_price, quote.ask_price
        mid = (bid + ask) / 2 if (bid and ask) else (ask or bid)
        return {"bid": bid, "ask": ask, "mid": mid}
    except Exception as exc:
        log.warning("Could not fetch quote for %s: %s", contract_symbol, exc)
        return None


# --------------------------------------------------------------------------
# Position sizing
# --------------------------------------------------------------------------

def get_account_equity() -> float:
    return float(trading_client.get_account().equity)


# --------------------------------------------------------------------------
# Weekly performance summary (feeds the tracking spreadsheet)
# --------------------------------------------------------------------------
# Same design as the long-options bot: accumulate CLOSED-trade realized P/L
# for the week, persist to JSON (survives restarts), and post a Discord
# summary with exactly the spreadsheet columns. For credit spreads, realized
# dollars = (net_credit - close_cost) * num_spreads * 100.

WEEKLY_STATS_FILE = os.environ.get("WEEKLY_STATS_FILE_V3", "weekly_stats_v3.json")
SUMMARY_TRIGGER_FILE = os.environ.get("SUMMARY_TRIGGER_FILE_V3", "post_summary_v3.flag")
_wk_hh, _wk_mm = os.environ.get("WEEKLY_SUMMARY_TIME", "15:55").split(":")
WEEKLY_SUMMARY_TIME = dtime(int(_wk_hh), int(_wk_mm))
BOT_LABEL = os.environ.get("BOT_LABEL", "Credit Spread v3")
STRATEGY_LABEL = os.environ.get("STRATEGY_LABEL", "Credit spreads")


def _iso_monday(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


def _fresh_weekly_stats(starting_equity=None) -> dict:
    return {
        "week_start": _iso_monday(date.today()),
        "starting_equity": starting_equity,
        "ending_equity": None,
        "trades_closed": 0,
        "winning_trades": 0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "peak_equity": starting_equity,
        "max_drawdown_pct": 0.0,
        "last_summary_date": None,
    }


def _load_weekly_stats() -> dict:
    if not os.path.exists(WEEKLY_STATS_FILE):
        return _fresh_weekly_stats()
    try:
        with open(WEEKLY_STATS_FILE, "r") as f:
            return json.load(f)
    except Exception as exc:
        log.error("Failed to load %s (%s) — starting fresh weekly stats.", WEEKLY_STATS_FILE, exc)
        return _fresh_weekly_stats()


def _save_weekly_stats():
    tmp = f"{WEEKLY_STATS_FILE}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_weekly_stats, f, indent=2)
        os.replace(tmp, WEEKLY_STATS_FILE)
    except Exception as exc:
        log.error("Failed to save %s: %s", WEEKLY_STATS_FILE, exc)


_weekly_stats = _load_weekly_stats()


def _ensure_current_week():
    this_monday = _iso_monday(date.today())
    if _weekly_stats.get("week_start") != this_monday:
        log.info("New trading week detected — resetting weekly summary tracker (was %s, now %s).",
                  _weekly_stats.get("week_start"), this_monday)
        try:
            eq = get_account_equity()
        except Exception:
            eq = None
        _weekly_stats.clear()
        _weekly_stats.update(_fresh_weekly_stats(starting_equity=eq))
        _save_weekly_stats()
    elif _weekly_stats.get("starting_equity") is None:
        try:
            _weekly_stats["starting_equity"] = get_account_equity()
            _weekly_stats["peak_equity"] = _weekly_stats["starting_equity"]
            _save_weekly_stats()
        except Exception:
            pass


def _record_closed_trade(realized_dollars: float):
    _weekly_stats["trades_closed"] = _weekly_stats.get("trades_closed", 0) + 1
    if realized_dollars >= 0:
        _weekly_stats["winning_trades"] = _weekly_stats.get("winning_trades", 0) + 1
        _weekly_stats["gross_profit"] = round(_weekly_stats.get("gross_profit", 0.0) + realized_dollars, 2)
    else:
        _weekly_stats["gross_loss"] = round(_weekly_stats.get("gross_loss", 0.0) + realized_dollars, 2)
    _save_weekly_stats()


def _sample_equity_for_drawdown():
    try:
        eq = get_account_equity()
    except Exception:
        return
    _weekly_stats["ending_equity"] = eq
    peak = _weekly_stats.get("peak_equity") or eq
    if eq > peak:
        peak = eq
    _weekly_stats["peak_equity"] = peak
    if peak and peak > 0:
        dd = (peak - eq) / peak
        if dd > _weekly_stats.get("max_drawdown_pct", 0.0):
            _weekly_stats["max_drawdown_pct"] = round(dd, 4)
    _save_weekly_stats()


def _post_weekly_summary(trigger: str):
    s = _weekly_stats
    trades = s.get("trades_closed", 0)
    wins = s.get("winning_trades", 0)
    losses = trades - wins
    gp = s.get("gross_profit", 0.0)
    gl = s.get("gross_loss", 0.0)
    se = s.get("starting_equity")
    ee = s.get("ending_equity")
    try:
        ee = get_account_equity()
        s["ending_equity"] = ee
    except Exception:
        pass

    win_rate = (wins / trades) if trades else None
    weekly_return = ((ee - se) / se) if (se and ee) else None
    net_pl = (ee - se) if (se is not None and ee is not None) else None
    profit_factor = (gp / abs(gl)) if gl else None
    avg_win = (gp / wins) if wins else None
    avg_loss = (abs(gl) / losses) if losses else None
    open_count = len(_open_positions)

    def fmt_money(x): return f"${x:,.2f}" if x is not None else "—"
    def fmt_pct(x): return f"{x:.1%}" if x is not None else "—"
    def fmt_num(x, n=2): return f"{x:.{n}f}" if x is not None else "—"

    color = 0x2ecc71 if (net_pl is not None and net_pl >= 0) else 0xe74c3c
    embed = {
        "title": f"📊 Weekly Summary — {BOT_LABEL}",
        "description": (f"Week of **{s.get('week_start')}** · trigger: _{trigger}_\n"
                        f"Copy these into the tracker spreadsheet ({BOT_LABEL} row).\n"
                        f"⚠️ Realized (closed) trades only. **{open_count} spread(s) still open** and not counted."),
        "color": color,
        "fields": [
            {"name": "Bot Name", "value": BOT_LABEL, "inline": True},
            {"name": "Strategy Type", "value": STRATEGY_LABEL, "inline": True},
            {"name": "Week Start", "value": s.get("week_start", "—"), "inline": True},
            {"name": "Starting Equity ($)", "value": fmt_money(se), "inline": True},
            {"name": "Ending Equity ($)", "value": fmt_money(ee), "inline": True},
            {"name": "Max Drawdown (%)", "value": fmt_pct(s.get("max_drawdown_pct")), "inline": True},
            {"name": "Total Trades", "value": str(trades), "inline": True},
            {"name": "Winning Trades", "value": str(wins), "inline": True},
            {"name": "Losing Trades", "value": str(losses), "inline": True},
            {"name": "Gross Profit ($)", "value": fmt_money(gp), "inline": True},
            {"name": "Gross Loss ($)", "value": fmt_money(gl), "inline": True},
            {"name": "Net P/L ($)", "value": fmt_money(net_pl), "inline": True},
            {"name": "Win Rate (%)", "value": fmt_pct(win_rate), "inline": True},
            {"name": "Weekly Return (%)", "value": fmt_pct(weekly_return), "inline": True},
            {"name": "Profit Factor", "value": fmt_num(profit_factor), "inline": True},
            {"name": "Avg Win ($)", "value": fmt_money(avg_win), "inline": True},
            {"name": "Avg Loss ($)", "value": fmt_money(avg_loss), "inline": True},
            {"name": "Open (uncounted)", "value": str(open_count), "inline": True},
        ],
        "footer": {"text": "Paper-trading credit-spread bot · Weekly summary"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        resp.raise_for_status()
        log.info("Weekly summary posted to Discord (trigger: %s).", trigger)
    except requests.RequestException as exc:
        log.error("Failed to post weekly summary: %s", exc)
    s["last_summary_date"] = date.today().isoformat()
    _save_weekly_stats()


def _check_summary_triggers():
    if os.path.exists(SUMMARY_TRIGGER_FILE):
        log.info("Summary trigger file found — posting on-demand summary.")
        _post_weekly_summary("on-demand")
        try:
            os.remove(SUMMARY_TRIGGER_FILE)
        except OSError as exc:
            log.warning("Could not remove trigger file %s: %s", SUMMARY_TRIGGER_FILE, exc)
        return
    now_et = datetime.now(EASTERN)
    if now_et.weekday() == 4 and now_et.time() >= WEEKLY_SUMMARY_TIME:
        if _weekly_stats.get("last_summary_date") != date.today().isoformat():
            log.info("Friday close reached — posting scheduled weekly summary.")
            _post_weekly_summary("scheduled Friday close")


def size_spread(max_risk_per_spread: float, account_equity: float, effective_risk_pct: float) -> int:
    """Number of spreads, rounded down. Risk = max_risk_per_spread per spread."""
    if max_risk_per_spread <= 0:
        return 0
    dollar_risk = account_equity * (effective_risk_pct / 100.0)
    return int(dollar_risk // max_risk_per_spread)


def apply_daily_heat_cap(max_risk_per_spread: float, intended_spreads: int, account_equity: float) -> int:
    _reset_daily_state_if_new_day()
    daily_cap = account_equity * (MAX_DAILY_RISK_PCT / 100.0)
    remaining = daily_cap - _daily_state["risk_used"]
    if remaining <= 0:
        log.warning("Daily heat cap reached ($%.2f of $%.2f) — skipping.", _daily_state["risk_used"], daily_cap)
        return 0
    capped = int(remaining // max_risk_per_spread)
    if capped < intended_spreads:
        log.info("Daily heat cap reduces size from %d to %d spreads.", intended_spreads, capped)
    return min(intended_spreads, capped)


# --------------------------------------------------------------------------
# Order submission (PAPER ONLY)
# --------------------------------------------------------------------------

def submit_spread_order(short_symbol: str, long_symbol: str, qty: int,
                         short_limit: float, long_limit: float) -> object:
    """Submit both legs as a SINGLE multi-leg order. This is the correct way
    to submit a credit spread on Alpaca — when legs are submitted individually,
    Alpaca treats the short leg as a naked option (requiring full collateral or
    a higher approval level). A multi-leg order tells Alpaca it's a defined-risk
    spread and applies spread margin instead. Requires Level 3 options approval."""
    net_credit_limit = round(short_limit - long_limit, 2)
    if net_credit_limit <= 0:
        net_credit_limit = 0.01  # minimum positive credit; edge case guard

    order_request = LimitOrderRequest(
        qty=qty,
        limit_price=net_credit_limit,
        type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        order_class="mleg",
        legs=[
            OptionLegRequest(
                symbol=short_symbol,
                side=OrderSide.SELL,
                position_intent=PositionIntent.SELL_TO_OPEN,
                ratio_qty=1,
            ),
            OptionLegRequest(
                symbol=long_symbol,
                side=OrderSide.BUY,
                position_intent=PositionIntent.BUY_TO_OPEN,
                ratio_qty=1,
            ),
        ],
    )
    return trading_client.submit_order(order_data=order_request)


def submit_spread_close_order(short_symbol: str, long_symbol: str, qty: int,
                               short_bid: float, long_ask: float, urgent: bool) -> object:
    """Submit a multi-leg closing order: buy back the short, sell the long.
    urgent=True uses a wider limit (or effectively market) to guarantee exit
    near the pre-expiration cutoff."""
    net_debit_limit = round(short_bid - long_ask, 2)
    if net_debit_limit < 0.01:
        net_debit_limit = 0.01  # minimum; we still want to get out

    order_request = LimitOrderRequest(
        qty=qty,
        limit_price=net_debit_limit if not urgent else net_debit_limit * 1.5,
        type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        order_class="mleg",
        legs=[
            OptionLegRequest(
                symbol=short_symbol,
                side=OrderSide.BUY,
                position_intent=PositionIntent.BUY_TO_CLOSE,
                ratio_qty=1,
            ),
            OptionLegRequest(
                symbol=long_symbol,
                side=OrderSide.SELL,
                position_intent=PositionIntent.SELL_TO_CLOSE,
                ratio_qty=1,
            ),
        ],
    )
    return trading_client.submit_order(order_data=order_request)


# --------------------------------------------------------------------------
# Position tracking / state machine
# --------------------------------------------------------------------------

def register_pending_spread(spread: dict, num_spreads: int, symbol: str):
    """
    Track a just-submitted credit spread. Both legs are tracked together under
    the short leg's symbol (the defining leg of the spread). The position key
    is the short leg's contract symbol — same duplicate-contract guard logic
    as bot.py, now time-aware (tracks exit_date rather than a same-tick flag).
    """
    pos_key = spread["short_leg"]["symbol"]
    _open_positions[pos_key] = {
        "status": "pending_entry",
        "symbol": symbol,
        "direction": spread["direction"],
        "num_spreads": num_spreads,
        "short_leg_symbol": spread["short_leg"]["symbol"],
        "long_leg_symbol": spread["long_leg"]["symbol"],
        "short_leg_credit": spread["short_leg"]["mid"],
        "long_leg_debit": spread["long_leg"]["mid"],
        "net_credit": spread["net_credit"],
        "spread_width": spread["spread_width"],
        "max_risk_per_spread": spread["max_risk_per_spread"],
        "expiration": spread["expiration"],
        "entry_submitted_at": time.time(),
        "order_id": spread.get("order_id"),
        "actual_net_credit": None,
        "exit_date": None,
    }
    _save_open_positions()
    log.info("Registered pending credit spread: %s x%d, net credit $%.2f, max risk $%.2f/spread.",
              pos_key, num_spreads, spread["net_credit"], spread["max_risk_per_spread"])


def _handle_pending_entry(pos_key: str, pos: dict):
    """Check the multi-leg spread order for fill confirmation."""
    order_id = pos.get("order_id")
    if not order_id:
        return

    try:
        order = trading_client.get_order_by_id(order_id)
    except Exception as exc:
        log.warning("%s: could not fetch order status (%s) — will retry.", pos_key, exc)
        return

    status = _order_status_name(order)

    if status == "filled" and order.filled_avg_price:
        # For multi-leg credit spread orders, Alpaca reports filled_avg_price
        # from the buyer's perspective — negative means net credit received
        # (we are net sellers). Take abs() to get the actual credit amount.
        raw_fill = float(order.filled_avg_price)
        pos["actual_net_credit"] = round(abs(raw_fill), 4)
        pos["status"] = "open"
        log.info("%s: spread CONFIRMED filled. Actual net credit: $%.4f x%d spreads (raw fill: %.4f).",
                  pos_key, pos["actual_net_credit"], pos["num_spreads"], raw_fill)
        _save_open_positions()

        # Notify Discord that the fill is confirmed with real fill price
        direction = pos["direction"]
        color = 0x2ecc71 if direction == "bullish" else 0xe74c3c
        embed = {
            "title": f"✅ Spread Fill Confirmed — {pos['symbol']}",
            "description": "Both legs filled. Position is now open and being monitored.",
            "color": color,
            "fields": [
                {"name": "Short Leg", "value": pos["short_leg_symbol"], "inline": True},
                {"name": "Long Leg", "value": pos["long_leg_symbol"], "inline": True},
                {"name": "Direction", "value": direction.capitalize(), "inline": True},
                {"name": "Actual Net Credit", "value": f"${pos['actual_net_credit']:.4f}/share", "inline": True},
                {"name": "Spreads", "value": str(pos["num_spreads"]), "inline": True},
                {"name": "Total Credit Received",
                 "value": f"${pos['actual_net_credit'] * pos['num_spreads'] * 100:.2f}", "inline": True},
                {"name": "Expiration", "value": pos["expiration"], "inline": True},
                {"name": "Exit Plan", "value": (
                    f"Profit target: {CREDIT_PROFIT_TARGET_PCT:.0%} of credit captured · "
                    f"Stop: {CREDIT_STOP_LOSS_PCT:.0%} of credit lost · "
                    f"Cutoff: {CREDIT_DTE_CLOSE_DAYS}d before expiry"
                ), "inline": False},
            ],
            "footer": {"text": "bot_v2 · credit spread · paper trading only"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        send_discord(embed)
        return

    if status in ("canceled", "expired", "rejected"):
        log.warning("%s: spread order %s — removing from tracking.", pos_key, status)
        del _open_positions[pos_key]
        _save_open_positions()
        return

    if time.time() - pos["entry_submitted_at"] > ENTRY_FILL_TIMEOUT_SECONDS:
        if not pos.get("cancel_requested"):
            log.warning("%s: entry timeout — canceling unfilled spread order.", pos_key)
            try:
                trading_client.cancel_order_by_id(order_id)
            except Exception:
                pass
            pos["cancel_requested"] = True
            pos["cancel_requested_at"] = time.time()
            _save_open_positions()


def _current_spread_value(pos: dict):
    """Fetch live quotes for both legs. Returns the current cost to close
    (buy back short, sell long), or None if quotes unavailable."""
    short_quote = get_option_latest_quote(pos["short_leg_symbol"])
    long_quote = get_option_latest_quote(pos["long_leg_symbol"])
    if not short_quote or not long_quote:
        return None
    if not short_quote["ask"] or not long_quote["bid"]:
        return None
    # Cost to close = buy back short at ask, sell long at bid
    return short_quote["ask"] - long_quote["bid"]


def _should_exit(pos: dict, close_cost: float, today: date):
    """
    Evaluate exit rules against the CURRENT COST TO CLOSE the spread.
    P/L is measured as a percentage of the max credit originally received.
    Returns (reason, urgent) or (None, False).
    """
    net_credit = pos.get("actual_net_credit") or pos["net_credit"]
    if net_credit <= 0:
        return None, False

    # Profit: cost to close has decayed to < (1 - target) * original credit
    profit_threshold = net_credit * (1 - CREDIT_PROFIT_TARGET_PCT)
    if close_cost <= profit_threshold:
        pct = (net_credit - close_cost) / net_credit
        return f"Profit target hit ({pct:.1%} of credit captured, threshold {CREDIT_PROFIT_TARGET_PCT:.0%})", False

    # Stop loss: cost to close has grown to > (1 + stop_loss_pct) * original credit
    stop_threshold = net_credit * (1 + CREDIT_STOP_LOSS_PCT)
    if close_cost >= stop_threshold:
        loss_pct = (close_cost - net_credit) / net_credit
        return f"Stop loss hit (cost to close {loss_pct:.1%} above credit, threshold {CREDIT_STOP_LOSS_PCT:.0%})", False

    # Pre-expiration cutoff: avoid assignment risk and final-day gamma
    expiration = date.fromisoformat(pos["expiration"])
    if (expiration - today).days <= CREDIT_DTE_CLOSE_DAYS:
        return (f"Pre-expiration cutoff ({CREDIT_DTE_CLOSE_DAYS} days before {expiration.isoformat()})"), True

    return None, False


def _submit_close_spread(pos: dict, urgent: bool):
    """Submit a single multi-leg closing order for both legs simultaneously."""
    short_quote = get_option_latest_quote(pos["short_leg_symbol"])
    long_quote = get_option_latest_quote(pos["long_leg_symbol"])
    if not short_quote or not long_quote:
        return None

    short_bid = short_quote.get("bid") or short_quote["mid"]
    long_ask = long_quote.get("ask") or long_quote["mid"]

    try:
        order = submit_spread_close_order(
            pos["short_leg_symbol"], pos["long_leg_symbol"],
            pos["num_spreads"], short_bid, long_ask, urgent,
        )
        return order
    except Exception as exc:
        log.exception("Failed to submit close spread order: %s", exc)
        return None


def manage_open_positions():
    if not _open_positions:
        return
    today = date.today()

    for pos_key in list(_open_positions.keys()):
        pos = _open_positions[pos_key]
        status = pos.get("status", "open")

        if status == "pending_entry":
            _handle_pending_entry(pos_key, pos)
            continue

        if status == "pending_close":
            # For simplicity: check if enough time has passed — if so, just
            # remove from tracking. A full fill-confirmation loop for
            # spread-close is complex (4 orders: 2 initial + potential
            # escalations) — we verify position is gone at Alpaca instead.
            try:
                trading_client.get_open_position(pos["short_leg_symbol"])
                # Still open — close orders may still be working
            except Exception:
                net_credit = pos.get("actual_net_credit") or pos["net_credit"]
                close_cost = pos.get("close_cost_at_exit", net_credit)
                realized_pct = (net_credit - close_cost) / net_credit
                log.info("%s: position confirmed closed. Realized: %+.1f%% of credit.", pos_key, realized_pct * 100)
                send_discord_position_closed(pos_key, pos, close_cost, realized_pct, pos.get("close_reason", ""))
                del _open_positions[pos_key]
                _save_open_positions()
            continue

        # status == "open" — check exit rules
        try:
            trading_client.get_open_position(pos["short_leg_symbol"])
        except Exception:
            log.info("%s: no longer open at Alpaca — removing.", pos_key)
            del _open_positions[pos_key]
            _save_open_positions()
            continue

        close_cost = _current_spread_value(pos)
        if close_cost is None:
            log.warning("%s: no usable quotes this cycle — skipping exit check.", pos_key)
            continue

        reason, urgent = _should_exit(pos, close_cost, today)
        if reason is None:
            _save_open_positions()
            continue

        log.info("%s: exit triggered — %s (close cost $%.4f vs credit $%.4f).",
                  pos_key, reason, close_cost,
                  pos.get("actual_net_credit") or pos["net_credit"])

        order = _submit_close_spread(pos, urgent)
        if order is None:
            log.error("%s: failed to submit close order — will retry next cycle.", pos_key)
            continue

        pos["status"] = "pending_close"
        pos["close_reason"] = reason
        pos["close_cost_at_exit"] = close_cost
        pos["close_order_id"] = str(order.id)
        pos["close_submitted_at"] = time.time()
        _save_open_positions()


# --------------------------------------------------------------------------
# Discord notifications
# --------------------------------------------------------------------------

def send_discord(embed: dict):
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Discord send failed: %s", exc)


def send_discord_spread_submitted(symbol: str, spread: dict, num_spreads: int,
                                   equity: float, cluster_size: int):
    direction = spread["direction"]
    color = 0x2ecc71 if direction == "bullish" else 0xe74c3c
    spread_type = "Bull Put Spread" if direction == "bullish" else "Bear Call Spread"
    cluster_note = (f"Risk pooled across {cluster_size} correlated signals."
                    if cluster_size > 1 else "No correlated signals this cycle.")
    embed = {
        "title": f"📋 Paper Credit Spread Submitted — {symbol}",
        "description": f"**{spread_type}** · {cluster_note}\nThis is a PAPER account order.",
        "color": color,
        "fields": [
            {"name": "Underlying", "value": symbol, "inline": True},
            {"name": "Direction", "value": direction.capitalize(), "inline": True},
            {"name": "Expiration", "value": spread["expiration"], "inline": True},
            {"name": "Short Leg", "value": spread["short_leg"]["symbol"], "inline": True},
            {"name": "Long Leg", "value": spread["long_leg"]["symbol"], "inline": True},
            {"name": "Spread Width", "value": f"${spread['spread_width']:.2f}", "inline": True},
            {"name": "Net Credit (mid)", "value": f"${spread['net_credit']:.4f}/share", "inline": True},
            {"name": "Credit/Width", "value": f"{spread.get('credit_to_width', 0) * 100:.0f}%", "inline": True},
            {"name": "Max Risk/Spread", "value": f"${spread['max_risk_per_spread']:.2f}", "inline": True},
            {"name": "Spreads Submitted", "value": str(num_spreads), "inline": True},
            {"name": "Total Max Risk", "value": f"${spread['max_risk_per_spread'] * num_spreads:.2f}", "inline": True},
            {"name": "Daily Risk Used", "value": f"${_daily_state['risk_used']:.2f}", "inline": True},
            {"name": "Account Equity", "value": f"${equity:,.2f}", "inline": True},
            {"name": "Exit Plan", "value": (
                f"Profit target: {CREDIT_PROFIT_TARGET_PCT:.0%} of credit captured · "
                f"Stop: {CREDIT_STOP_LOSS_PCT:.0%} of credit lost · "
                f"Cutoff: {CREDIT_DTE_CLOSE_DAYS}d before expiry"
            ), "inline": False},
        ],
        "footer": {"text": "bot_v2 · credit spread · paper trading only"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    send_discord(embed)


def send_discord_position_closed(pos_key: str, pos: dict, close_cost: float, realized_pct: float, reason: str):
    net_credit = pos.get("actual_net_credit") or pos["net_credit"]
    # Record realized dollar P/L into the weekly tracker. For a credit spread,
    # you keep (credit - cost_to_close) per share, × 100 × number of spreads.
    try:
        realized_dollars = (net_credit - close_cost) * 100 * pos.get("num_spreads", 0)
        _record_closed_trade(realized_dollars)
    except Exception as exc:
        log.warning("Could not record closed trade for weekly stats: %s", exc)

    color = 0x2ecc71 if realized_pct >= 0 else 0xe74c3c
    embed = {
        "title": f"🔚 Credit Spread Closed — {pos['symbol']}",
        "description": f"**Reason:** {reason}",
        "color": color,
        "fields": [
            {"name": "Short Leg", "value": pos["short_leg_symbol"], "inline": True},
            {"name": "Long Leg", "value": pos["long_leg_symbol"], "inline": True},
            {"name": "Direction", "value": pos["direction"].capitalize(), "inline": True},
            {"name": "Original Credit", "value": f"${net_credit:.4f}", "inline": True},
            {"name": "Close Cost", "value": f"${close_cost:.4f}", "inline": True},
            {"name": "Realized P/L", "value": f"{realized_pct:+.1%} of credit", "inline": True},
            {"name": "Spreads", "value": str(pos["num_spreads"]), "inline": True},
        ],
        "footer": {"text": "bot_v2 · credit spread · paper trading only"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    send_discord(embed)


# --------------------------------------------------------------------------
# Earnings blackout (identical to bot.py)
# --------------------------------------------------------------------------

def check_earnings_blackout(symbol: str) -> bool:
    if not FMP_API_KEY:
        return True
    now = time.time()
    cached = _earnings_cache.get(symbol)
    if cached and (now - cached[0]) < _EARNINGS_CACHE_TTL:
        earnings_date = cached[1]
    else:
        earnings_date = None
        try:
            resp = requests.get("https://financialmodelingprep.com/api/v3/earning_calendar",
                                 params={"symbol": symbol, "apikey": FMP_API_KEY}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            today = date.today()
            upcoming = [datetime.strptime(row["date"], "%Y-%m-%d").date() for row in data if "date" in row]
            future_dates = [d for d in upcoming if d >= today]
            earnings_date = min(future_dates) if future_dates else None
        except Exception as exc:
            log.warning("%s: earnings lookup failed (%s) — skipping blackout.", symbol, exc)
        _earnings_cache[symbol] = (now, earnings_date)
    if earnings_date is None:
        return True
    days_to_earnings = (earnings_date - date.today()).days
    if 0 <= days_to_earnings <= EARNINGS_BLACKOUT_DAYS:
        log.warning("%s: earnings in %d day(s) — inside blackout window, skipping.", symbol, days_to_earnings)
        return False
    return True


# --------------------------------------------------------------------------
# Entry scan
# --------------------------------------------------------------------------

def gather_signals():
    signals = {}
    for symbol, cfg in WATCHLIST.items():
        direction, price, ref = evaluate_signal(symbol)
        if direction is None:
            continue
        if cfg.get("monitor_only"):
            log.info("%s: signal would fire (%s) — MONITOR ONLY, not trading.", symbol, direction)
            continue
        signals[symbol] = (direction, price, ref)
    return signals


def _count_active_positions() -> int:
    """Positions that occupy a slot: anything not already fully closed.
    pending_entry, open, and pending_close all count — they tie up margin
    and represent real exposure."""
    return sum(1 for p in _open_positions.values()
               if p.get("status") in ("pending_entry", "open", "pending_close"))


def _has_position_on_underlying(symbol: str) -> bool:
    """True if any tracked position (any strike, any direction) is on this
    underlying. Blocks the same-name layering that let bot_v2 stack four MCD
    spreads at shifted strikes in one session."""
    for p in _open_positions.values():
        if p.get("symbol") == symbol and p.get("status") in ("pending_entry", "open", "pending_close"):
            return True
    return False


def _near_recent_extreme(symbol: str, direction: str, price: float) -> bool:
    """True if we'd be selling into exhaustion: a bull put spread while price is
    within EXTREME_PROXIMITY_PCT of its N-day HIGH, or a bear call while within
    that of its N-day LOW. That's where reversal-through-the-short-strike risk is
    highest. Degrades gracefully — if we can't fetch history, returns False
    (don't block) rather than refusing the trade."""
    if EXTREME_LOOKBACK_DAYS <= 0 or EXTREME_PROXIMITY_PCT <= 0:
        return False
    try:
        closes = get_recent_closes(symbol, EXTREME_LOOKBACK_DAYS + 2)
    except Exception:
        return False
    if not closes or len(closes) < EXTREME_LOOKBACK_DAYS:
        return False
    window = closes[-EXTREME_LOOKBACK_DAYS:]
    hi, lo = max(window), min(window)
    if direction == "bullish":
        # selling a put after a run-up to near the highs = exhaustion risk
        return price >= hi * (1 - EXTREME_PROXIMITY_PCT)
    else:
        return price <= lo * (1 + EXTREME_PROXIMITY_PCT)


def process_symbol(symbol: str, direction: str, price: float,
                    effective_risk_pct: float, cluster_size: int):
    # --- Hard concurrency cap: the structural stop bot_v2 lacked ---
    if _count_active_positions() >= MAX_CONCURRENT_POSITIONS:
        log.info("%s: at concurrency cap (%d open) — not opening anything new this cycle.",
                 symbol, MAX_CONCURRENT_POSITIONS)
        return

    # --- One position per underlying: no same-name layering ---
    if _has_position_on_underlying(symbol):
        log.info("%s: already have an active position on this underlying — skipping.", symbol)
        return

    state = _last_trade_state.get(symbol)
    now = time.time()
    if state and now - state["timestamp"] < ORDER_COOLDOWN_SECONDS:
        log.info("%s: within 4-hour cooldown (last entry %.0f min ago) — skipping.",
                  symbol, (now - state["timestamp"]) / 60)
        return

    # --- Distance-from-extreme: don't sell into exhaustion ---
    if _near_recent_extreme(symbol, direction, price):
        log.info("%s: %s signal but price near %d-day %s — selling into exhaustion, skipping.",
                 symbol, direction, EXTREME_LOOKBACK_DAYS,
                 "high" if direction == "bullish" else "low")
        return

    if not check_earnings_blackout(symbol):
        return

    spread = select_spread_contracts(symbol, price, direction)
    if spread is None:
        log.info("%s: signal fired (%s) but no usable spread found.", symbol, direction)
        return

    # Duplicate-contract guard: check if we're already holding the same short leg
    pos_key = spread["short_leg"]["symbol"]
    existing = _open_positions.get(pos_key)
    if existing:
        expiry = existing.get("exit_date") or existing.get("expiration")
        if expiry:
            expiry_date = date.fromisoformat(expiry) if isinstance(expiry, str) else expiry
            if date.today() <= expiry_date:
                log.warning("%s: already tracking %s — skipping duplicate.", symbol, pos_key)
                return

    equity = get_account_equity()
    num_spreads = size_spread(spread["max_risk_per_spread"], equity, effective_risk_pct)
    num_spreads = apply_daily_heat_cap(spread["max_risk_per_spread"], num_spreads, equity)

    if num_spreads <= 0:
        log.info("%s: sized to 0 spreads after risk/heat checks — skipping.", symbol)
        return

    try:
        # Submit BOTH legs as a single multi-leg order. This is required for
        # Alpaca to recognize the position as a defined-risk spread and apply
        # spread margin. Submitting legs individually causes Alpaca to treat
        # the short as a naked option, triggering a Level 3 / buying-power error.
        order = submit_spread_order(
            short_symbol=spread["short_leg"]["symbol"],
            long_symbol=spread["long_leg"]["symbol"],
            qty=num_spreads,
            short_limit=spread["short_leg"]["bid"] or spread["short_leg"]["mid"],
            long_limit=spread["long_leg"]["ask"] or spread["long_leg"]["mid"],
        )
    except Exception as exc:
        log.exception("%s: spread order submission failed: %s", symbol, exc)
        return

    _daily_state["risk_used"] += spread["max_risk_per_spread"] * num_spreads
    spread["order_id"] = str(order.id)
    register_pending_spread(spread, num_spreads, symbol)
    send_discord_spread_submitted(symbol, spread, num_spreads, equity, cluster_size)
    _last_trade_state[symbol] = {"direction": direction, "timestamp": now}


def run_entry_scan():
    signals = gather_signals()
    if not signals:
        return

    direction_counts = {}
    for direction, _, _ in signals.values():
        direction_counts[direction] = direction_counts.get(direction, 0) + 1

    for symbol, (direction, price, ref) in signals.items():
        cluster_size = direction_counts[direction]
        effective_risk_pct = RISK_PCT_WEEKLY / cluster_size
        try:
            process_symbol(symbol, direction, price, effective_risk_pct, cluster_size)
        except Exception as exc:
            log.exception("Error processing %s: %s", symbol, exc)


def main():
    log.info(
        "Starting credit spread PAPER TRADING bot v3 (paper=%s). Weekly symbols: %d active, %d monitor-only. "
        "Entry scan every %ds, exit check every %ds.",
        PAPER,
        sum(1 for cfg in WATCHLIST.values() if not cfg.get("monitor_only")),
        sum(1 for cfg in WATCHLIST.values() if cfg.get("monitor_only")),
        ENTRY_POLL_SECONDS, EXIT_POLL_SECONDS,
    )
    log.info(
        "Quality filters: max %d concurrent positions · 1 per underlying · "
        "credit/width floor %.0f%% · short strike ~%.0f%% OTM (delta %.2f if feed has greeks) · "
        "skip within %.0f%% of %d-day extreme.",
        MAX_CONCURRENT_POSITIONS, MIN_CREDIT_TO_WIDTH * 100, SHORT_OTM_PCT * 100,
        SHORT_TARGET_DELTA, EXTREME_PROXIMITY_PCT * 100, EXTREME_LOOKBACK_DAYS,
    )
    log.info(
        "Spread exit rules: profit target %.0f%% of credit captured · "
        "stop loss %.0f%% of credit lost · pre-expiry cutoff %d days.",
        CREDIT_PROFIT_TARGET_PCT * 100, CREDIT_STOP_LOSS_PCT * 100, CREDIT_DTE_CLOSE_DAYS,
    )
    if not FMP_API_KEY:
        log.info("FMP_API_KEY not set — earnings blackout filter disabled. "
                 "Note: AAPL/MSFT/NVDA/AMZN carry earnings risk; consider setting FMP_API_KEY.")
    if _open_positions:
        log.info("Resuming with %d tracked position(s) from %s.", len(_open_positions), POSITION_STATE_FILE)

    next_entry_scan_at = 0.0

    while True:
        market_open = False
        try:
            market_open = is_market_open()
            if market_open:
                _reset_daily_state_if_new_day()
                _ensure_current_week()
                _sample_equity_for_drawdown()
                manage_open_positions()
                _check_summary_triggers()
                now = time.time()
                if now >= next_entry_scan_at:
                    run_entry_scan()
                    next_entry_scan_at = now + ENTRY_POLL_SECONDS
            else:
                if os.path.exists(SUMMARY_TRIGGER_FILE):
                    _check_summary_triggers()
                log.info("Market closed — sleeping %ds.", CLOSED_MARKET_SLEEP_SECONDS)
        except Exception as exc:
            log.exception("Unhandled error in cycle: %s", exc)

        time.sleep(EXIT_POLL_SECONDS if market_open else CLOSED_MARKET_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
