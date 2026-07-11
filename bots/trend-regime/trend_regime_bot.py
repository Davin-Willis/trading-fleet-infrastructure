"""
bot.py — Multi-Asset Options Scanner (PAPER TRADING — ALPACA_PAPER MUST BE "true")
=============================================================================

This version fixes a real bug found in a code review: the bot was treating
order SUBMISSION as order COMPLETION. Every order (entry buys and exit
sells) goes through alpaca-py's submit_order() and immediately returns an
order object whose status is typically "pending_new" — not "filled". The
previous version trusted that object immediately: entries were registered
as open positions and exits were deleted from tracking the instant the
order was *submitted*, not when it was actually *filled*. If a limit order
sat unfilled (very possible on a fast 0DTE move), the bot's internal state
said "closed"/"open" while Alpaca's real state disagreed — meaning an
exit could be silently abandoned, unmonitored, while still bleeding
premium.

Fix: every tracked position now goes through an explicit state machine:

    pending_entry -> open -> pending_close -> (removed, confirmed filled)

  - pending_entry: order submitted, not yet confirmed filled. Checked every
    fast-cadence tick via get_order_by_id(). On fill, entry_premium is set
    to the ACTUAL fill price (not the limit price submitted) and status
    moves to "open". If unfilled past ENTRY_FILL_TIMEOUT_SECONDS, the order
    is canceled and the position is dropped — never silently abandoned.
  - open: normal exit-rule monitoring (stop loss / trailing stop / time or
    calendar cutoffs), same as before.
  - pending_close: a close order has been submitted but not yet confirmed
    filled. Checked every tick. On fill, the "Position Closed" Discord
    message is sent using the ACTUAL fill price, and only then removed
    from tracking. If unfilled past CLOSE_FILL_TIMEOUT_SECONDS, the limit
    order is canceled and replaced with a MARKET order — once a stop/cutoff
    has triggered, getting out with certainty matters more than price.

Other fixes from the same review:
  - Option chain requests now pass strike_price_gte/lte around the current
    underlying price instead of pulling every strike in existence.
  - RSI returns 50 (neutral) rather than 100 in the genuinely-flat
    avg_gain == avg_loss == 0 case.
  - Intraday bars are fetched from a wider window (21 days) and then
    trimmed to a FIXED bar count before computing indicators, so EMA
    warm-up length — and therefore MACD/RSI values — don't drift based on
    how many weekends/holidays happened to fall in the fetch window.
  - The main loop sleeps much longer when the market is confirmed closed,
    instead of polling get_clock() every EXIT_POLL_SECONDS regardless.

Everything else (intraday filter stack for SPY/QQQ, daily SMA for SOFI,
correlation-aware sizing, daily heat cap, sanity filters, dual-cadence
polling) is unchanged from the prior version.

Hard safety guard
------------------
This script REFUSES TO START if ALPACA_PAPER is not "true".

Setup
-----
    pip install alpaca-py requests pytz python-dotenv

Environment variables (new ones for this version marked NEW):
    ALPACA_API_KEY
    ALPACA_SECRET_KEY
    ALPACA_PAPER                       -> MUST be "true"
    DISCORD_WEBHOOK_URL
    RISK_PCT_0DTE                      -> default "1.5" (0DTE has shown fast, large moves)
    RISK_PCT_WEEKLY                    -> default "5.0"
    MAX_DAILY_RISK_PCT                 -> default "4.0"
    MAX_PREMIUM_TO_PRICE_0DTE          -> default "0.015"
    MAX_PREMIUM_TO_PRICE_WEEKLY        -> default "0.04"
    MAX_BID_ASK_SPREAD_PCT             -> default "0.15"
    EARNINGS_BLACKOUT_DAYS             -> default "5"
    FMP_API_KEY                        -> optional
    ENTRY_POLL_MINUTES                 -> default "15"
    EXIT_POLL_MINUTES                  -> default "2"
    STOP_LOSS_PCT                      -> default "-0.50"
    PROFIT_ARM_PCT                     -> default "0.40"
    TRAILING_GIVEBACK_PCT              -> default "0.20"
    ZERO_DTE_ENTRY_CUTOFF              -> default "14:45" (ET, HH:MM, stop opening NEW 0DTE positions)
    ZERO_DTE_EXIT_TIME                 -> default "15:00"
    WEEKLY_EXIT_DAYS_BEFORE_EXPIRATION -> default "1"
    POSITION_STATE_FILE                -> default "open_positions_state.json"
    INTRADAY_TIMEFRAME_MINUTES         -> default "15"
    INTRADAY_SMA_LOOKBACK              -> default "20"
    RSI_PERIOD                         -> default "14"
    RSI_BULLISH_MIN / RSI_BULLISH_MAX  -> default "50" / "70"
    RSI_BEARISH_MIN / RSI_BEARISH_MAX  -> default "30" / "50"
    MACD_FAST / MACD_SLOW / MACD_SIGNAL-> default "12" / "26" / "9"
    CHOP_CONFIRM_BARS                  -> default "2"
    ENTRY_FILL_TIMEOUT_SECONDS         -> NEW, default "300" (5 min)
    CLOSE_FILL_TIMEOUT_SECONDS         -> NEW, default "90"
    STRIKE_WINDOW_PCT                  -> NEW, default "0.08" (8% around spot)
    INTRADAY_FETCH_DAYS                -> NEW, default "21"
    INTRADAY_FIXED_LOOKBACK_BARS       -> NEW, default "150"
    CLOSED_MARKET_SLEEP_SECONDS        -> NEW, default "1200" (20 min)

Disclaimer
----------
Educational / paper-testing tool only. Not financial advice.
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
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
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

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# --- Regime gate (CLONE-ONLY ADDITION) ---------------------------------
# Reads the shared regime state written daily by the Regime Calculator.
# Scales NEW ENTRY risk only; exit management is untouched.
REGIME_STATE_PATH = "/home/ubuntu/shared/regime_state.json"
REGIME_STALE_HOURS = 30.0
REGIME_MULTIPLIERS = {"risk_on": 1.0, "neutral": 0.5, "risk_off": 0.0}

RISK_PCT_0DTE = float(os.environ.get("RISK_PCT_0DTE", "1.5"))
RISK_PCT_WEEKLY = float(os.environ.get("RISK_PCT_WEEKLY", "5.0"))
MAX_DAILY_RISK_PCT = float(os.environ.get("MAX_DAILY_RISK_PCT", "4.0"))
MAX_PREMIUM_TO_PRICE_0DTE = float(os.environ.get("MAX_PREMIUM_TO_PRICE_0DTE", "0.015"))
MAX_PREMIUM_TO_PRICE_WEEKLY = float(os.environ.get("MAX_PREMIUM_TO_PRICE_WEEKLY", "0.04"))
MAX_BID_ASK_SPREAD_PCT = float(os.environ.get("MAX_BID_ASK_SPREAD_PCT", "0.15"))
EARNINGS_BLACKOUT_DAYS = int(os.environ.get("EARNINGS_BLACKOUT_DAYS", "5"))
FMP_API_KEY = os.environ.get("FMP_API_KEY")

ENTRY_POLL_SECONDS = int(float(os.environ.get("ENTRY_POLL_MINUTES", os.environ.get("POLL_MINUTES", "15"))) * 60)
EXIT_POLL_SECONDS = int(float(os.environ.get("EXIT_POLL_MINUTES", "2")) * 60)
if EXIT_POLL_SECONDS <= 0:
    EXIT_POLL_SECONDS = ENTRY_POLL_SECONDS
CLOSED_MARKET_SLEEP_SECONDS = int(os.environ.get("CLOSED_MARKET_SLEEP_SECONDS", "1200"))

STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "-0.50"))
PROFIT_ARM_PCT = float(os.environ.get("PROFIT_ARM_PCT", "0.40"))
PROFIT_ARM_PCT_0DTE = float(os.environ.get("PROFIT_ARM_PCT_0DTE", "0.25"))
TRAILING_GIVEBACK_PCT = float(os.environ.get("TRAILING_GIVEBACK_PCT", "0.20"))
_zero_dte_exit_hh, _zero_dte_exit_mm = os.environ.get("ZERO_DTE_EXIT_TIME", "15:00").split(":")
ZERO_DTE_EXIT_TIME = dtime(int(_zero_dte_exit_hh), int(_zero_dte_exit_mm))
WEEKLY_EXIT_DAYS_BEFORE_EXPIRATION = int(os.environ.get("WEEKLY_EXIT_DAYS_BEFORE_EXPIRATION", "1"))
POSITION_STATE_FILE = os.environ.get("POSITION_STATE_FILE", "open_positions_state.json")

ENTRY_FILL_TIMEOUT_SECONDS = int(os.environ.get("ENTRY_FILL_TIMEOUT_SECONDS", "300"))
CLOSE_FILL_TIMEOUT_SECONDS = int(os.environ.get("CLOSE_FILL_TIMEOUT_SECONDS", "90"))
STRIKE_WINDOW_PCT = float(os.environ.get("STRIKE_WINDOW_PCT", "0.08"))

SMA_LOOKBACK = 20

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

DAILY_RSI_PERIOD = int(os.environ.get("DAILY_RSI_PERIOD", "14"))
DAILY_RSI_BULLISH_MIN = float(os.environ.get("DAILY_RSI_BULLISH_MIN", "50"))
DAILY_RSI_BULLISH_MAX = float(os.environ.get("DAILY_RSI_BULLISH_MAX", "70"))
DAILY_RSI_BEARISH_MIN = float(os.environ.get("DAILY_RSI_BEARISH_MIN", "30"))
DAILY_RSI_BEARISH_MAX = float(os.environ.get("DAILY_RSI_BEARISH_MAX", "50"))
DAILY_MACD_FAST = int(os.environ.get("DAILY_MACD_FAST", "12"))
DAILY_MACD_SLOW = int(os.environ.get("DAILY_MACD_SLOW", "26"))
DAILY_MACD_SIGNAL = int(os.environ.get("DAILY_MACD_SIGNAL", "9"))
CHOP_CONFIRM_DAYS = int(os.environ.get("CHOP_CONFIRM_DAYS", "2"))

INTRADAY_FETCH_DAYS = int(os.environ.get("INTRADAY_FETCH_DAYS", "21"))
INTRADAY_FIXED_LOOKBACK_BARS = int(os.environ.get("INTRADAY_FIXED_LOOKBACK_BARS", "300"))

ORDER_COOLDOWN_SECONDS = 60 * 60 * 4

WATCHLIST = {
    "SOFI": {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "SPY":  {"mode": "0dte", "signal_mode": "intraday"},
    "QQQ":  {"mode": "0dte", "signal_mode": "intraday"},
    "IWM":  {"mode": "0dte", "signal_mode": "intraday"},
    "GLD":  {"mode": "0dte", "signal_mode": "intraday"},
    "TLT":  {"mode": "0dte", "signal_mode": "intraday"},
    "XLF":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "XLY":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "XLI":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "XLV":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "XLP":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "XLE":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "XLU":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "XLB":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "XLRE": {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "JPM":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "V":    {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "UNH":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "JNJ":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "PG":   {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "HD":   {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "CAT":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "HON":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "VZ":   {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "USO":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "DBA":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
    "EEM":  {"mode": "weekly", "target_dte": 7, "signal_mode": "daily_sma"},
}

EASTERN = pytz.timezone("US/Eastern")
_zero_dte_entry_hh, _zero_dte_entry_mm = os.environ.get("ZERO_DTE_ENTRY_CUTOFF", "14:45").split(":")
ZERO_DTE_ENTRY_CUTOFF = dtime(int(_zero_dte_entry_hh), int(_zero_dte_entry_mm))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

if not API_KEY or not SECRET_KEY:
    log.error("ALPACA_API_KEY / ALPACA_SECRET_KEY not set. Exiting.")
    sys.exit(1)
if not DISCORD_WEBHOOK_URL:
    log.error("DISCORD_WEBHOOK_URL not set. Exiting.")
    sys.exit(1)
if not PAPER:
    log.error("ALPACA_PAPER is not 'true'. This script refuses to submit live orders. Set ALPACA_PAPER=true.")
    sys.exit(1)
if ZERO_DTE_ENTRY_CUTOFF >= ZERO_DTE_EXIT_TIME:
    log.error(
        "ZERO_DTE_ENTRY_CUTOFF (%s) must be BEFORE ZERO_DTE_EXIT_TIME (%s).",
        ZERO_DTE_ENTRY_CUTOFF.strftime("%H:%M"), ZERO_DTE_EXIT_TIME.strftime("%H:%M"),
    )
    sys.exit(1)
if ZERO_DTE_EXIT_TIME > dtime(15, 5):
    log.error(
        "ZERO_DTE_EXIT_TIME (%s) is too late. Set ZERO_DTE_EXIT_TIME to 15:05 or earlier.",
        ZERO_DTE_EXIT_TIME.strftime("%H:%M"),
    )
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
            log.info("New trading day detected — resetting daily portfolio-heat tracker.")
        _daily_state["date"] = today_str
        _daily_state["risk_used"] = 0.0


def _order_status_name(order) -> str:
    return str(order.status).split(".")[-1].lower()


# --------------------------------------------------------------------------
# Open-position persistence
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
# Time / market-hours gates
# --------------------------------------------------------------------------

def is_market_open() -> bool:
    try:
        clock = trading_client.get_clock()
        return bool(clock.is_open)
    except Exception as exc:
        log.warning("Could not fetch market clock (%s); assuming closed.", exc)
        return False


def zero_dte_entry_cutoff_passed() -> bool:
    return datetime.now(EASTERN).time() >= ZERO_DTE_ENTRY_CUTOFF


def zero_dte_exit_cutoff_passed() -> bool:
    return datetime.now(EASTERN).time() >= ZERO_DTE_EXIT_TIME


# --------------------------------------------------------------------------
# Equity market data + indicators
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
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, limit=min_bars_needed + 30)
    bars = stock_data_client.get_stock_bars(req).data.get(symbol, [])
    closes = [bar.close for bar in bars]
    if bars and bars[-1].timestamp.date() == date.today():
        closes = closes[:-1]
    return closes


def evaluate_daily_sma_signal(symbol: str):
    needed = max(SMA_LOOKBACK, DAILY_MACD_SLOW + DAILY_MACD_SIGNAL, DAILY_RSI_PERIOD + 1) + CHOP_CONFIRM_DAYS + 5
    closes = get_recent_closes(symbol, needed)
    if len(closes) < needed:
        log.warning("Not enough daily bar history for %s (%d/%d days) — skipping.", symbol, len(closes), needed)
        return None, None, None
    sma = sum(closes[-SMA_LOOKBACK:]) / SMA_LOOKBACK
    price = get_latest_price(symbol)
    rsi = compute_rsi(closes, DAILY_RSI_PERIOD)
    macd_line, macd_signal_line, _ = compute_macd(closes, DAILY_MACD_FAST, DAILY_MACD_SLOW, DAILY_MACD_SIGNAL)
    if rsi is None or macd_line is None:
        log.warning("%s: daily indicators failed to compute — skipping.", symbol)
        return None, None, None
    band = sma * 0.003
    trend_bullish = price > sma + band
    trend_bearish = price < sma - band
    recent = closes[-CHOP_CONFIRM_DAYS:]
    confirm_bullish = all(c > sma for c in recent)
    confirm_bearish = all(c < sma for c in recent)
    rsi_bullish_ok = DAILY_RSI_BULLISH_MIN <= rsi <= DAILY_RSI_BULLISH_MAX
    rsi_bearish_ok = DAILY_RSI_BEARISH_MIN <= rsi <= DAILY_RSI_BEARISH_MAX
    macd_bullish_ok = macd_line > macd_signal_line
    macd_bearish_ok = macd_line < macd_signal_line
    direction = None
    if trend_bullish and confirm_bullish and rsi_bullish_ok and macd_bullish_ok:
        direction = "bullish"
    elif trend_bearish and confirm_bearish and rsi_bearish_ok and macd_bearish_ok:
        direction = "bearish"
    log.info("%s daily check: price=%.2f sma%d=%.2f rsi=%.1f macd=%.3f macd_sig=%.3f -> %s",
             symbol, price, SMA_LOOKBACK, sma, rsi, macd_line, macd_signal_line, direction or "no signal")
    return direction, price, sma


def get_intraday_closes(symbol: str, timeframe_minutes: int, fixed_bars: int) -> list:
    start = date.today() - timedelta(days=INTRADAY_FETCH_DAYS)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
        start=start,
        limit=2000,
    )
    bars = stock_data_client.get_stock_bars(req).data.get(symbol, [])
    if not bars:
        return []
    closes = [bar.close for bar in bars]
    last_bar_age_minutes = (datetime.now(timezone.utc) - bars[-1].timestamp).total_seconds() / 60
    if last_bar_age_minutes < timeframe_minutes:
        closes = closes[:-1]
    if len(closes) > fixed_bars:
        closes = closes[-fixed_bars:]
    return closes


def compute_rsi(closes: list, period: int):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss < 1e-9:
        if avg_gain < 1e-9:
            return 50.0
        return 100.0
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
    macd_line_series = [f - s for f, s in zip(fast_aligned, slow_series)]
    if len(macd_line_series) < signal:
        return None, None, None
    signal_series = _ema_series(macd_line_series, signal)
    return macd_line_series[-1], signal_series[-1], macd_line_series[-1] - signal_series[-1]


def evaluate_intraday_signal(symbol: str):
    needed = max(INTRADAY_SMA_LOOKBACK, MACD_SLOW + MACD_SIGNAL, RSI_PERIOD + 1) + CHOP_CONFIRM_BARS + 5
    closes = get_intraday_closes(symbol, INTRADAY_TIMEFRAME_MINUTES, INTRADAY_FIXED_LOOKBACK_BARS)
    if len(closes) < needed:
        log.warning("Not enough intraday bar history for %s (%d/%d %d-min bars) — skipping.",
                    symbol, len(closes), needed, INTRADAY_TIMEFRAME_MINUTES)
        return None, None, None
    intraday_sma = sum(closes[-INTRADAY_SMA_LOOKBACK:]) / INTRADAY_SMA_LOOKBACK
    price = get_latest_price(symbol)
    rsi = compute_rsi(closes, RSI_PERIOD)
    macd_line, macd_signal_line, _ = compute_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    if rsi is None or macd_line is None:
        log.warning("%s: intraday indicators failed to compute — skipping.", symbol)
        return None, None, None
    band = intraday_sma * 0.003
    trend_bullish = price > intraday_sma + band
    trend_bearish = price < intraday_sma - band
    recent = closes[-CHOP_CONFIRM_BARS:]
    confirm_bullish = all(c > intraday_sma for c in recent)
    confirm_bearish = all(c < intraday_sma for c in recent)
    rsi_bullish_ok = RSI_BULLISH_MIN <= rsi <= RSI_BULLISH_MAX
    rsi_bearish_ok = RSI_BEARISH_MIN <= rsi <= RSI_BEARISH_MAX
    macd_bullish_ok = macd_line > macd_signal_line
    macd_bearish_ok = macd_line < macd_signal_line
    direction = None
    if trend_bullish and confirm_bullish and rsi_bullish_ok and macd_bullish_ok:
        direction = "bullish"
    elif trend_bearish and confirm_bearish and rsi_bearish_ok and macd_bearish_ok:
        direction = "bearish"
    log.info("%s intraday check: price=%.2f sma%d=%.2f rsi=%.1f macd=%.3f macd_sig=%.3f -> %s",
             symbol, price, INTRADAY_SMA_LOOKBACK, intraday_sma, rsi, macd_line, macd_signal_line,
             direction or "no signal")
    return direction, price, intraday_sma


def evaluate_signal(symbol: str):
    cfg = WATCHLIST[symbol]
    if cfg.get("signal_mode") == "intraday":
        return evaluate_intraday_signal(symbol)
    return evaluate_daily_sma_signal(symbol)


# --------------------------------------------------------------------------
# Options chain selection
# --------------------------------------------------------------------------

_OCC_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$")


def _parse_occ_symbol(contract_symbol: str):
    m = _OCC_RE.match(contract_symbol)
    if not m:
        return None
    strike = int(m.group("strike")) / 1000.0
    opt_type = "call" if m.group("cp") == "C" else "put"
    expiration = date(2000 + int(m.group("yy")), int(m.group("mm")), int(m.group("dd")))
    return {"strike": strike, "type": opt_type, "expiration": expiration}


def _target_expiration_window(symbol: str):
    cfg = WATCHLIST[symbol]
    today = date.today()
    if cfg["mode"] == "0dte":
        return today, today
    if cfg["mode"] == "weekly":
        target = today + timedelta(days=cfg["target_dte"])
        return today + timedelta(days=cfg["target_dte"] - 3), target + timedelta(days=3)
    raise ValueError(f"Unknown mode for {symbol}: {cfg['mode']}")


def _pick_best_expiration(available_dates, symbol: str) -> date:
    cfg = WATCHLIST[symbol]
    today = date.today()
    target = today if cfg["mode"] == "0dte" else today + timedelta(days=cfg["target_dte"])
    return min(available_dates, key=lambda d: abs((d - target).days))


def _extract_contract_fields(contract_symbol: str, snapshot) -> dict:
    parsed_symbol = _parse_occ_symbol(contract_symbol)
    if parsed_symbol is None:
        return {"symbol": contract_symbol, "strike": None, "type": None,
                "expiration": None, "premium": None, "bid": None, "ask": None}
    bid = ask = premium = None
    latest_quote = getattr(snapshot, "latest_quote", None)
    if latest_quote is not None:
        bid = getattr(latest_quote, "bid_price", None)
        ask = getattr(latest_quote, "ask_price", None)
        if bid and ask:
            premium = (bid + ask) / 2
        else:
            premium = ask or bid
    if premium is None:
        latest_trade = getattr(snapshot, "latest_trade", None)
        if latest_trade is not None:
            premium = getattr(latest_trade, "price", None)
    return {
        "symbol": contract_symbol, "strike": parsed_symbol["strike"], "type": parsed_symbol["type"],
        "expiration": parsed_symbol["expiration"], "premium": premium, "bid": bid, "ask": ask,
    }


def select_atm_contract(symbol: str, underlying_price: float, direction: str):
    gte, lte = _target_expiration_window(symbol)
    strike_window = max(underlying_price * STRIKE_WINDOW_PCT, 5.0)
    strike_gte = round(underlying_price - strike_window, 2)
    strike_lte = round(underlying_price + strike_window, 2)
    try:
        chain = option_data_client.get_option_chain(
            OptionChainRequest(
                underlying_symbol=symbol,
                expiration_date_gte=gte,
                expiration_date_lte=lte,
                strike_price_gte=strike_gte,
                strike_price_lte=strike_lte,
            )
        )
    except Exception as exc:
        log.error("Failed to fetch option chain for %s: %s", symbol, exc)
        return None
    if not chain:
        log.warning("Empty option chain returned for %s in window %s..%s, strikes %.2f-%.2f.",
                    symbol, gte, lte, strike_gte, strike_lte)
        return None
    parsed = []
    for contract_symbol, snapshot in chain.items():
        fields = _extract_contract_fields(contract_symbol, snapshot)
        if fields["strike"] is None or fields["type"] is None:
            continue
        parsed.append(fields)
    wanted_type = "call" if direction == "bullish" else "put"
    candidates = [c for c in parsed if c["type"] == wanted_type]
    if not candidates:
        log.warning("No %s contracts found for %s in requested window.", wanted_type, symbol)
        return None
    cfg = WATCHLIST[symbol]
    if cfg["mode"] == "weekly":
        available_dates = {c["expiration"] for c in candidates}
        best_expiration = _pick_best_expiration(available_dates, symbol)
        candidates = [c for c in candidates if c["expiration"] == best_expiration]
    best = min(candidates, key=lambda c: abs(c["strike"] - underlying_price))
    if best["premium"] is None or best["premium"] <= 0:
        log.warning("Selected contract %s has no usable premium quote.", best["symbol"])
        return None
    return best


def get_option_latest_quote(contract_symbol: str):
    try:
        req = OptionLatestQuoteRequest(symbol_or_symbols=contract_symbol, feed=OptionsFeed.INDICATIVE)
        quote = option_data_client.get_option_latest_quote(req)[contract_symbol]
        bid, ask = quote.bid_price, quote.ask_price
        mid = (bid + ask) / 2 if (bid and ask) else (ask or bid)
        return {"bid": bid, "ask": ask, "mid": mid}
    except Exception as exc:
        log.warning("Could not fetch latest quote for %s: %s", contract_symbol, exc)
        return None


# --------------------------------------------------------------------------
# Sanity filters
# --------------------------------------------------------------------------

def check_spread_sanity(symbol: str, contract: dict) -> bool:
    bid, ask, premium = contract.get("bid"), contract.get("ask"), contract.get("premium")
    if not bid or not ask or not premium:
        log.warning("%s: contract %s missing bid/ask — cannot verify spread, skipping.", symbol, contract["symbol"])
        return False
    spread_pct = (ask - bid) / premium
    if spread_pct > MAX_BID_ASK_SPREAD_PCT:
        log.warning("%s: contract %s spread too wide (%.1f%% of mid, max %.1f%%) — skipping.",
                    symbol, contract["symbol"], spread_pct * 100, MAX_BID_ASK_SPREAD_PCT * 100)
        return False
    return True


def check_premium_richness(symbol: str, contract: dict, underlying_price: float) -> bool:
    cfg = WATCHLIST[symbol]
    threshold = MAX_PREMIUM_TO_PRICE_0DTE if cfg["mode"] == "0dte" else MAX_PREMIUM_TO_PRICE_WEEKLY
    ratio = contract["premium"] / underlying_price
    if ratio > threshold:
        log.warning("%s: contract %s premium looks rich (%.2f%% of underlying, max %.2f%%) — skipping.",
                    symbol, contract["symbol"], ratio * 100, threshold * 100)
        return False
    return True


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
            resp = requests.get(
                "https://financialmodelingprep.com/api/v3/earning_calendar",
                params={"symbol": symbol, "apikey": FMP_API_KEY}, timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            today = date.today()
            upcoming = [datetime.strptime(row["date"], "%Y-%m-%d").date() for row in data if "date" in row]
            future_dates = [d for d in upcoming if d >= today]
            earnings_date = min(future_dates) if future_dates else None
        except Exception as exc:
            log.warning("%s: earnings lookup failed (%s) — skipping blackout check this cycle.", symbol, exc)
        _earnings_cache[symbol] = (now, earnings_date)
    if earnings_date is None:
        return True
    days_to_earnings = (earnings_date - date.today()).days
    if 0 <= days_to_earnings <= EARNINGS_BLACKOUT_DAYS:
        log.warning("%s: earnings in %d day(s) — inside blackout window, skipping.", symbol, days_to_earnings)
        return False
    return True


# --------------------------------------------------------------------------
# Position sizing
# --------------------------------------------------------------------------

def get_account_equity() -> float:
    return float(trading_client.get_account().equity)


# --------------------------------------------------------------------------
# Weekly performance summary (feeds the tracking spreadsheet)
# --------------------------------------------------------------------------
# Accumulates CLOSED-trade stats for the current week and posts a single
# Discord summary listing exactly the columns the tracker spreadsheet needs.
# Persists to its own JSON file so a mid-week restart doesn't wipe the tally.
# Honest by design: it reports realized (closed) trades only, and separately
# notes how many positions are still open and unrealized.

WEEKLY_STATS_FILE = os.environ.get("WEEKLY_STATS_FILE", "weekly_stats.json")
# On-demand trigger: create this file (e.g. `touch summary.flag`) and the bot
# posts a summary on its next cycle, then deletes the file. Works on Windows
# and Linux alike (no OS signals, which aren't portable).
SUMMARY_TRIGGER_FILE = os.environ.get("SUMMARY_TRIGGER_FILE", "post_summary.flag")
# Scheduled auto-post: Friday at/after this ET time (once per day).
_wk_hh, _wk_mm = os.environ.get("WEEKLY_SUMMARY_TIME", "15:55").split(":")
WEEKLY_SUMMARY_TIME = dtime(int(_wk_hh), int(_wk_mm))
BOT_LABEL = os.environ.get("BOT_LABEL", "Long Options (AWS)")
STRATEGY_LABEL = os.environ.get("STRATEGY_LABEL", "Long calls/puts")


def _iso_monday(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


def _fresh_weekly_stats(starting_equity=None) -> dict:
    return {
        "week_start": _iso_monday(date.today()),
        "starting_equity": starting_equity,
        "ending_equity": None,
        "trades_closed": 0,
        "winning_trades": 0,
        "gross_profit": 0.0,   # sum of positive realized P/L ($)
        "gross_loss": 0.0,     # sum of negative realized P/L ($), stored negative
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
    """Roll the tracker over to a new week if the stored week_start is stale.
    Captures this week's starting equity on the first call of a new week."""
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
    """Hook called once per finalized close. Accumulates the realized P/L."""
    _weekly_stats["trades_closed"] = _weekly_stats.get("trades_closed", 0) + 1
    if realized_dollars >= 0:
        _weekly_stats["winning_trades"] = _weekly_stats.get("winning_trades", 0) + 1
        _weekly_stats["gross_profit"] = round(_weekly_stats.get("gross_profit", 0.0) + realized_dollars, 2)
    else:
        _weekly_stats["gross_loss"] = round(_weekly_stats.get("gross_loss", 0.0) + realized_dollars, 2)
    _save_weekly_stats()


def _sample_equity_for_drawdown():
    """Track running peak equity and the worst peak-to-trough drawdown seen
    this week. Called once per management cycle (one extra equity read)."""
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
    """Post the spreadsheet-ready weekly summary to Discord."""
    s = _weekly_stats
    trades = s.get("trades_closed", 0)
    wins = s.get("winning_trades", 0)
    losses = trades - wins
    gp = s.get("gross_profit", 0.0)
    gl = s.get("gross_loss", 0.0)
    se = s.get("starting_equity")
    ee = s.get("ending_equity")
    try:
        ee = get_account_equity()  # freshest possible
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
                        f"⚠️ Realized (closed) trades only. **{open_count} position(s) still open** and not counted."),
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
        "footer": {"text": "Paper-trading options scanner · Weekly summary"},
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
    """Called each cycle: handle on-demand sentinel file + scheduled Friday post."""
    # On-demand trigger file
    if os.path.exists(SUMMARY_TRIGGER_FILE):
        log.info("Summary trigger file found — posting on-demand summary.")
        _post_weekly_summary("on-demand")
        try:
            os.remove(SUMMARY_TRIGGER_FILE)
        except OSError as exc:
            log.warning("Could not remove trigger file %s: %s", SUMMARY_TRIGGER_FILE, exc)
        return
    # Scheduled Friday post (once per day, at/after the configured ET time)
    now_et = datetime.now(EASTERN)
    if now_et.weekday() == 4 and now_et.time() >= WEEKLY_SUMMARY_TIME:
        if _weekly_stats.get("last_summary_date") != date.today().isoformat():
            log.info("Friday close reached — posting scheduled weekly summary.")
            _post_weekly_summary("scheduled Friday close")


def fixed_fractional_options_size(premium: float, account_equity: float, risk_pct: float) -> dict:
    risk_per_contract = premium * 100
    if risk_per_contract <= 0:
        return {"contracts": 0, "dollar_risk": 0.0, "risk_per_contract": 0.0, "intended_dollar_risk": 0.0}
    dollar_risk = account_equity * (risk_pct / 100.0)
    contracts = int(dollar_risk // risk_per_contract)
    return {
        "contracts": contracts,
        "risk_per_contract": round(risk_per_contract, 2),
        "dollar_risk": round(contracts * risk_per_contract, 2),
        "intended_dollar_risk": round(dollar_risk, 2),
    }


def apply_daily_heat_cap(sizing: dict, account_equity: float) -> dict:
    _reset_daily_state_if_new_day()
    daily_cap_dollars = account_equity * (MAX_DAILY_RISK_PCT / 100.0)
    remaining_budget = daily_cap_dollars - _daily_state["risk_used"]
    if remaining_budget <= 0:
        log.warning("Daily portfolio-heat cap reached ($%.2f of $%.2f) — skipping further trades today.",
                    _daily_state["risk_used"], daily_cap_dollars)
        return {**sizing, "contracts": 0, "dollar_risk": 0.0}
    if sizing["dollar_risk"] <= remaining_budget:
        return sizing
    risk_per_contract = sizing["risk_per_contract"]
    if risk_per_contract <= 0:
        return {**sizing, "contracts": 0, "dollar_risk": 0.0}
    capped_contracts = int(remaining_budget // risk_per_contract)
    log.info("Daily heat cap reduces size: capping from %d to %d contracts.", sizing["contracts"], capped_contracts)
    return {**sizing, "contracts": capped_contracts, "dollar_risk": round(capped_contracts * risk_per_contract, 2)}


# --------------------------------------------------------------------------
# Order submission (PAPER ONLY)
# --------------------------------------------------------------------------

def submit_paper_option_order(contract: dict, contracts_qty: int):
    limit_price = contract.get("ask") or contract["premium"]
    order_request = LimitOrderRequest(
        symbol=contract["symbol"], qty=contracts_qty, side=OrderSide.BUY,
        type=OrderType.LIMIT, time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2),
    )
    return trading_client.submit_order(order_data=order_request)


def submit_paper_close_order(contract_symbol: str, qty: int, limit_price: float):
    order_request = LimitOrderRequest(
        symbol=contract_symbol, qty=qty, side=OrderSide.SELL,
        type=OrderType.LIMIT, time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2),
    )
    return trading_client.submit_order(order_data=order_request)


def submit_paper_market_close_order(contract_symbol: str, qty: int):
    order_request = MarketOrderRequest(symbol=contract_symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
    return trading_client.submit_order(order_data=order_request)


# --------------------------------------------------------------------------
# Position state machine: pending_entry -> open -> pending_close -> removed
# --------------------------------------------------------------------------

def register_pending_entry(order, contract: dict, sizing: dict, direction: str, underlying: str, mode: str):
    _open_positions[contract["symbol"]] = {
        "status": "pending_entry",
        "underlying": underlying,
        "mode": mode,
        "direction": direction,
        "qty": sizing["contracts"],
        "entry_order_id": str(order.id),
        "entry_submitted_at": time.time(),
        "entry_premium": None,
        "high_water_pct": 0.0,
        "armed": False,
        "expiration": contract["expiration"].isoformat(),
    }
    _save_open_positions()
    log.info("Registered pending entry: %s x%d, order %s — awaiting fill confirmation.",
              contract["symbol"], sizing["contracts"], order.id)


def _record_partial_close(pos: dict, qty: int, price: float):
    pos["closed_qty_so_far"] = pos.get("closed_qty_so_far", 0) + qty
    pos["closed_notional_so_far"] = pos.get("closed_notional_so_far", 0.0) + qty * price


def _blended_close_price(pos: dict):
    q = pos.get("closed_qty_so_far", 0)
    if q <= 0:
        return None
    return pos.get("closed_notional_so_far", 0.0) / q


def _handle_pending_entry(contract_symbol: str, pos: dict):
    try:
        order = trading_client.get_order_by_id(pos["entry_order_id"])
    except Exception as exc:
        log.warning("%s: could not fetch entry order status (%s) — will retry next tick.", contract_symbol, exc)
        return
    status_name = _order_status_name(order)
    filled_qty = int(float(order.filled_qty or 0))
    if status_name == "filled":
        pos["entry_premium"] = float(order.filled_avg_price)
        pos["qty"] = filled_qty or pos["qty"]
        pos["status"] = "open"
        log.info("%s: entry CONFIRMED filled @ $%.2f x%d.", contract_symbol, pos["entry_premium"], pos["qty"])
        _save_open_positions()
        return
    if status_name in ("canceled", "expired", "rejected"):
        if filled_qty > 0:
            pos["entry_premium"] = float(order.filled_avg_price)
            pos["qty"] = filled_qty
            pos["status"] = "open"
            log.info("%s: entry PARTIALLY filled (%d) before %s — tracking the filled portion as open.",
                      contract_symbol, filled_qty, status_name)
        else:
            log.warning("%s: entry order %s, never filled — removing from tracking.", contract_symbol, status_name)
            del _open_positions[contract_symbol]
        _save_open_positions()
        return
    if pos.get("cancel_requested"):
        return
    if time.time() - pos["entry_submitted_at"] > ENTRY_FILL_TIMEOUT_SECONDS:
        log.warning("%s: entry order unfilled after %ds (status=%s) — requesting cancellation.",
                    contract_symbol, ENTRY_FILL_TIMEOUT_SECONDS, status_name)
        try:
            trading_client.cancel_order_by_id(pos["entry_order_id"])
        except Exception as exc:
            log.warning("%s: cancel request failed (%s) — will retry next tick.", contract_symbol, exc)
            return
        pos["cancel_requested"] = True
        pos["cancel_requested_at"] = time.time()
        _save_open_positions()


def _exit_reason_for(pos: dict, current_pct: float, today: date):
    if current_pct <= STOP_LOSS_PCT:
        return f"Stop loss hit ({current_pct:+.1%} <= {STOP_LOSS_PCT:+.1%})", False
    if pos["armed"] and current_pct <= (pos["high_water_pct"] - TRAILING_GIVEBACK_PCT):
        return (f"Trailing stop hit (pulled back to {current_pct:+.1%} from "
                f"high-water {pos['high_water_pct']:+.1%}, giveback {TRAILING_GIVEBACK_PCT:.0%})"), False
    if pos["mode"] == "0dte" and zero_dte_exit_cutoff_passed():
        return f"0DTE hard time cutoff ({ZERO_DTE_EXIT_TIME.strftime('%H:%M')} ET reached)", True
    if pos["mode"] == "weekly":
        expiration = date.fromisoformat(pos["expiration"])
        if (expiration - today).days <= WEEKLY_EXIT_DAYS_BEFORE_EXPIRATION:
            return (f"Weekly pre-expiration cutoff ({WEEKLY_EXIT_DAYS_BEFORE_EXPIRATION} "
                     f"day(s) before {expiration.isoformat()})"), True
    return None, False


def send_discord_position_closed(contract_symbol: str, pos: dict, exit_price: float,
                                   realized_pct: float, order, reason: str):
    # Record the realized dollar P/L into the weekly tracker (single finalize
    # chokepoint — both the normal-fill and partial-fill-completion paths call
    # this exactly once per closed position).
    try:
        realized_dollars = (exit_price - pos["entry_premium"]) * 100 * pos.get("qty", 0)
        _record_closed_trade(realized_dollars)
    except Exception as exc:
        log.warning("Could not record closed trade for weekly stats: %s", exc)

    color = 0x2ecc71 if realized_pct >= 0 else 0xe74c3c
    embed = {
        "title": f"🔚 Position Closed (Confirmed Filled) — {pos['underlying']}",
        "description": f"**Order ID:** `{order.id}` · **Status:** `{order.status}`\n**Reason:** {reason}",
        "color": color,
        "fields": [
            {"name": "Contract", "value": contract_symbol, "inline": True},
            {"name": "Direction", "value": pos["direction"].capitalize(), "inline": True},
            {"name": "Mode", "value": pos["mode"].upper(), "inline": True},
            {"name": "Entry Premium", "value": f"${pos['entry_premium']:.2f}", "inline": True},
            {"name": "Exit Price (Filled)", "value": f"${exit_price:.2f}", "inline": True},
            {"name": "Realized P/L", "value": f"{realized_pct:+.1%}", "inline": True},
            {"name": "Contracts", "value": str(pos["qty"]), "inline": True},
            {"name": "High-Water Mark", "value": f"{pos['high_water_pct']:+.1%}", "inline": True},
            {"name": "Escalated to Market Order", "value": "Yes" if pos.get("close_escalated") else "No", "inline": True},
        ],
        "footer": {"text": "Paper-trading options scanner · Exit management"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Failed to send Discord close notification: %s", exc)


def _handle_pending_close(contract_symbol: str, pos: dict):
    try:
        order = trading_client.get_order_by_id(pos["close_order_id"])
    except Exception as exc:
        log.warning("%s: could not fetch close order status (%s) — will retry next tick.", contract_symbol, exc)
        return
    status_name = _order_status_name(order)
    leg_filled_qty = int(float(order.filled_qty or 0))
    if status_name == "filled":
        if leg_filled_qty > 0 and order.filled_avg_price:
            _record_partial_close(pos, leg_filled_qty, float(order.filled_avg_price))
        exit_price = _blended_close_price(pos)
        if exit_price is None:
            log.error("%s: order reported filled but no fill data recorded — cannot finalize, leaving tracked.",
                       contract_symbol)
            return
        realized_pct = (exit_price - pos["entry_premium"]) / pos["entry_premium"]
        log.info("%s: close CONFIRMED filled @ $%.2f blended (P/L %.1f%%).", contract_symbol, exit_price, realized_pct * 100)
        send_discord_position_closed(contract_symbol, pos, exit_price, realized_pct, order, pos["close_reason"])
        del _open_positions[contract_symbol]
        _save_open_positions()
        return
    if status_name in ("canceled", "expired", "rejected"):
        if leg_filled_qty > 0 and order.filled_avg_price:
            _record_partial_close(pos, leg_filled_qty, float(order.filled_avg_price))
            log.info("%s: this leg partially filled %d before %s.", contract_symbol, leg_filled_qty, status_name)
        for k in ("close_order_id", "cancel_requested", "cancel_requested_at"):
            pos.pop(k, None)
        remaining_qty = pos["qty"] - pos.get("closed_qty_so_far", 0)
        if remaining_qty <= 0:
            exit_price = _blended_close_price(pos)
            realized_pct = (exit_price - pos["entry_premium"]) / pos["entry_premium"]
            send_discord_position_closed(contract_symbol, pos, exit_price, realized_pct, order, pos["close_reason"])
            del _open_positions[contract_symbol]
            _save_open_positions()
            return
        if pos.get("close_escalated"):
            log.error("%s: market escalation order also %s — reverting to OPEN for re-evaluation.",
                       contract_symbol, status_name)
            pos["status"] = "open"
            for k in ("close_submitted_at", "close_reason", "close_escalated"):
                pos.pop(k, None)
            _save_open_positions()
            return
        log.warning("%s: cancellation confirmed, %d contract(s) remaining — escalating to MARKET order.",
                    contract_symbol, remaining_qty)
        try:
            market_order = submit_paper_market_close_order(contract_symbol, remaining_qty)
        except Exception as exc:
            log.exception("%s: market escalation order failed: %s — reverting to OPEN for retry.", contract_symbol, exc)
            pos["status"] = "open"
            for k in ("close_submitted_at", "close_reason", "close_escalated"):
                pos.pop(k, None)
            _save_open_positions()
            return
        pos["close_order_id"] = str(market_order.id)
        pos["close_submitted_at"] = time.time()
        pos["close_escalated"] = True
        _save_open_positions()
        return
    if pos.get("cancel_requested"):
        return
    if not pos.get("close_escalated") and time.time() - pos["close_submitted_at"] > CLOSE_FILL_TIMEOUT_SECONDS:
        log.warning("%s: close order unfilled after %ds (status=%s) — requesting cancellation before escalating.",
                    contract_symbol, CLOSE_FILL_TIMEOUT_SECONDS, status_name)
        try:
            trading_client.cancel_order_by_id(pos["close_order_id"])
        except Exception as exc:
            log.warning("%s: cancel of stuck limit failed (%s) — will retry next tick.", contract_symbol, exc)
            return
        pos["cancel_requested"] = True
        pos["cancel_requested_at"] = time.time()
        _save_open_positions()


def manage_open_positions():
    if not _open_positions:
        return
    today = date.today()
    for contract_symbol in list(_open_positions.keys()):
        pos = _open_positions[contract_symbol]
        status = pos.get("status", "open")
        if status == "pending_entry":
            _handle_pending_entry(contract_symbol, pos)
            continue
        if status == "pending_close":
            _handle_pending_close(contract_symbol, pos)
            continue
        try:
            alpaca_position = trading_client.get_open_position(contract_symbol)
            actual_qty = int(float(alpaca_position.qty))
        except Exception:
            log.info("%s: no longer open at Alpaca (closed/expired externally) — removing from tracking.",
                      contract_symbol)
            del _open_positions[contract_symbol]
            _save_open_positions()
            continue
        if actual_qty <= 0:
            del _open_positions[contract_symbol]
            _save_open_positions()
            continue
        pos["qty"] = actual_qty
        quote = get_option_latest_quote(contract_symbol)
        if quote is None or quote["mid"] is None:
            log.warning("%s: no usable quote this cycle — skipping exit check.", contract_symbol)
            continue
        current_pct = (quote["mid"] - pos["entry_premium"]) / pos["entry_premium"]
        if current_pct > pos["high_water_pct"]:
            pos["high_water_pct"] = current_pct
        arm_threshold = PROFIT_ARM_PCT_0DTE if pos["mode"] == "0dte" else PROFIT_ARM_PCT
        if not pos["armed"] and pos["high_water_pct"] >= arm_threshold:
            pos["armed"] = True
            log.info("%s: trailing stop armed at high-water %.1f%% (threshold %.0f%%).",
                      contract_symbol, pos["high_water_pct"] * 100, arm_threshold * 100)
        reason, urgent = _exit_reason_for(pos, current_pct, today)
        if reason is None:
            _save_open_positions()
            continue
        if urgent:
            try:
                order = submit_paper_market_close_order(contract_symbol, pos["qty"])
            except Exception as exc:
                log.exception("%s: urgent market close failed: %s", contract_symbol, exc)
                continue
            close_escalated = True
        else:
            exit_limit_price = quote["bid"] or quote["mid"]
            try:
                order = submit_paper_close_order(contract_symbol, pos["qty"], exit_limit_price)
            except Exception as exc:
                log.exception("%s: close order submission failed: %s", contract_symbol, exc)
                continue
            close_escalated = False
        pos["status"] = "pending_close"
        pos["close_order_id"] = str(order.id)
        pos["close_submitted_at"] = time.time()
        pos["close_reason"] = reason
        pos["close_escalated"] = close_escalated
        log.info("%s: close order SUBMITTED (%s, not yet confirmed filled) — %s (unrealized P/L %.1f%%).",
                  contract_symbol, "MARKET/urgent" if urgent else "LIMIT", reason, current_pct * 100)
        _save_open_positions()


# --------------------------------------------------------------------------
# Discord notification — entries
# --------------------------------------------------------------------------

def send_discord_trade_executed(symbol: str, mode: str, direction: str, underlying_price: float,
                                 contract: dict, sizing: dict, account_equity: float,
                                 expiration_label: str, order, cluster_size: int):
    color = 0x2ecc71 if direction == "bullish" else 0xe74c3c
    cluster_note = (f"Risk split across {cluster_size} correlated signals this cycle."
                    if cluster_size > 1 else "No other correlated signal this cycle.")
    embed = {
        "title": f"📤 Paper Order Submitted (Awaiting Fill) — {symbol}",
        "description": (f"**Order ID:** `{order.id}` · **Status:** `{order.status}`\n"
                        f"Fill will be confirmed before this is tracked as fully open. {cluster_note}"),
        "color": color,
        "fields": [
            {"name": "Underlying", "value": symbol, "inline": True},
            {"name": "Mode", "value": mode.upper(), "inline": True},
            {"name": "Expiration", "value": expiration_label, "inline": True},
            {"name": "Direction", "value": direction.capitalize(), "inline": True},
            {"name": "Underlying Price", "value": f"${underlying_price:.2f}", "inline": True},
            {"name": "Contract Type", "value": contract["type"].upper(), "inline": True},
            {"name": "Strike (ATM)", "value": f"${contract['strike']:.2f}", "inline": True},
            {"name": "Contract Symbol", "value": contract["symbol"], "inline": True},
            {"name": "Limit Price", "value": f"${(contract.get('ask') or contract['premium']):.2f}", "inline": True},
            {"name": "Max Risk / Contract", "value": f"${sizing['risk_per_contract']:.2f}", "inline": True},
            {"name": "Contracts Submitted", "value": str(sizing["contracts"]), "inline": True},
            {"name": "Total Risk (this trade)", "value": f"${sizing['dollar_risk']:.2f}", "inline": True},
            {"name": "Daily Risk Used (incl. this trade)", "value": f"${_daily_state['risk_used']:.2f}", "inline": True},
            {"name": "Account Equity", "value": f"${account_equity:,.2f}", "inline": True},
            {"name": "Signal", "value": ("Intraday (15m SMA+RSI+MACD)" if WATCHLIST[symbol].get("signal_mode") == "intraday"
                                          else "Daily SMA+RSI+MACD"), "inline": True},
            {"name": "Exit Plan", "value": (
                f"Stop {STOP_LOSS_PCT:+.0%} · Arm trail @ {PROFIT_ARM_PCT:+.0%} (giveback {TRAILING_GIVEBACK_PCT:.0%}) · "
                + (f"Hard close {ZERO_DTE_EXIT_TIME.strftime('%H:%M')} ET" if mode == "0dte"
                   else f"Close {WEEKLY_EXIT_DAYS_BEFORE_EXPIRATION}d before expiry")
            ), "inline": False},
        ],
        "footer": {"text": "Paper-trading options scanner · Fixed-fractional + portfolio-heat sizing"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        resp.raise_for_status()
        log.info("Discord submission notice sent for %s order %s.", symbol, order.id)
    except requests.RequestException as exc:
        log.error("Failed to send Discord confirmation: %s", exc)


# --------------------------------------------------------------------------
# Regime gate helpers (CLONE-ONLY ADDITION)
# --------------------------------------------------------------------------

_regime_warned_on = {"date": None}
_last_regime_logged = {"state": None}


def get_regime():
    """Return (state, size_multiplier) from the shared regime file.

    Fail-safe: on ANY problem (missing file, bad JSON, unknown state,
    stale timestamp) default to risk_on / full size so the clone degrades
    to exact control behavior instead of becoming a third strategy.
    Warns loudly (log + Discord) at most once per day when that happens.
    """
    try:
        with open(REGIME_STATE_PATH) as f:
            data = json.load(f)
        state = data.get("state")
        if state not in REGIME_MULTIPLIERS:
            raise ValueError(f"unknown regime state {state!r}")
        asof = data.get("asof")
        if asof:
            asof_dt = datetime.fromisoformat(asof)
            age_h = (datetime.now(timezone.utc) - asof_dt).total_seconds() / 3600.0
            if age_h > REGIME_STALE_HOURS:
                raise ValueError(f"regime file is stale ({age_h:.1f}h old)")
        return state, REGIME_MULTIPLIERS[state]
    except Exception as exc:
        today_str = date.today().isoformat()
        if _regime_warned_on["date"] != today_str:
            _regime_warned_on["date"] = today_str
            log.warning("Regime gate FAIL-SAFE: %s — defaulting to risk_on (full size).", exc)
            if DISCORD_WEBHOOK_URL:
                try:
                    requests.post(DISCORD_WEBHOOK_URL, json={
                        "content": f"⚠️ Regime gate fail-safe: {exc} — trading at FULL size (control behavior) until fixed."
                    }, timeout=10)
                except Exception:
                    pass
        return "risk_on(fallback)", 1.0


# --------------------------------------------------------------------------
# Entry-scan logic
# --------------------------------------------------------------------------

def gather_signals():
    signals = {}
    for symbol in WATCHLIST:
        cfg = WATCHLIST[symbol]
        if cfg["mode"] == "0dte" and zero_dte_entry_cutoff_passed():
            log.info("%s: past 3:15 PM ET entry cutoff — skipping 0DTE scan for today.", symbol)
            continue
        direction, price, ref = evaluate_signal(symbol)
        if direction is None:
            continue
        signals[symbol] = (direction, price, ref)
    return signals


def process_symbol(symbol: str, direction: str, price: float, effective_risk_pct: float, cluster_size: int):
    cfg = WATCHLIST[symbol]
    state = _last_trade_state.get(symbol)
    now = time.time()
    if state and state["direction"] == direction and now - state["timestamp"] < ORDER_COOLDOWN_SECONDS:
        log.info("%s: %s signal still active, within cooldown — skipping.", symbol, direction)
        return
    if cfg["mode"] == "weekly" and not check_earnings_blackout(symbol):
        return
    contract = select_atm_contract(symbol, price, direction)
    if contract is None:
        log.info("%s: signal fired (%s) but no usable ATM contract found.", symbol, direction)
        return
    if contract["symbol"] in _open_positions:
        log.warning("%s: already tracking %s (status=%s) — skipping duplicate entry.",
                    symbol, contract["symbol"], _open_positions[contract["symbol"]].get("status", "open"))
        return
    if not check_spread_sanity(symbol, contract):
        return
    if not check_premium_richness(symbol, contract, price):
        return
    equity = get_account_equity()
    sizing = fixed_fractional_options_size(contract["premium"], equity, effective_risk_pct)
    sizing = apply_daily_heat_cap(sizing, equity)
    if sizing["contracts"] <= 0:
        log.info("%s: sized to 0 contracts after risk/heat checks — skipping.", symbol)
        return
    try:
        order = submit_paper_option_order(contract, sizing["contracts"])
    except Exception as exc:
        log.exception("%s: order submission failed: %s", symbol, exc)
        return
    _daily_state["risk_used"] += sizing["dollar_risk"]
    register_pending_entry(order, contract, sizing, direction, symbol, cfg["mode"])
    expiration_label = "Today (0DTE)" if cfg["mode"] == "0dte" else f"~{cfg['target_dte']}-day weekly"
    send_discord_trade_executed(
        symbol=symbol, mode=cfg["mode"], direction=direction, underlying_price=price,
        contract=contract, sizing=sizing, account_equity=equity,
        expiration_label=expiration_label, order=order, cluster_size=cluster_size,
    )
    _last_trade_state[symbol] = {"direction": direction, "timestamp": now}


def run_entry_scan():
    regime_state, regime_mult = get_regime()
    if _last_regime_logged["state"] != regime_state:
        _last_regime_logged["state"] = regime_state
        log.info("Regime gate: state=%s -> entry size multiplier %.2f", regime_state, regime_mult)
    if regime_mult <= 0:
        log.info("Regime gate: %s — standing down, no new entries this scan (exits still managed).", regime_state)
        return
    signals = gather_signals()
    if not signals:
        return
    direction_counts = {}
    for direction, _, _ in signals.values():
        direction_counts[direction] = direction_counts.get(direction, 0) + 1
    for symbol, (direction, price, ref) in signals.items():
        cluster_size = direction_counts[direction]
        base_risk_pct = RISK_PCT_0DTE if WATCHLIST[symbol]["mode"] == "0dte" else RISK_PCT_WEEKLY
        effective_risk_pct = (base_risk_pct / cluster_size) * regime_mult
        try:
            process_symbol(symbol, direction, price, effective_risk_pct, cluster_size)
        except Exception as exc:
            log.exception("Error processing %s: %s", symbol, exc)


def _next_scan_boundary(interval_seconds):
    """Next wall-clock multiple of interval_seconds (15min -> :00/:15/:30/:45)."""
    now = time.time()
    return (int(now // interval_seconds) + 1) * interval_seconds


def main():
    log.info(
        "Starting multi-asset options PAPER TRADING bot (paper=%s). Watchlist: %s. "
        "Entry scan every %ds, exit check every %ds (market closed: sleep %ds).",
        PAPER, list(WATCHLIST.keys()), ENTRY_POLL_SECONDS, EXIT_POLL_SECONDS, CLOSED_MARKET_SLEEP_SECONDS,
    )
    log.info(
        "Exit rules: stop %.0f%%, arm trail at %.0f%% (giveback %.0f pts), 0DTE hard close %s ET, "
        "weekly close %dd before expiry. Entry fill timeout %ds, close fill timeout %ds (then market escalation).",
        STOP_LOSS_PCT * 100, PROFIT_ARM_PCT * 100, TRAILING_GIVEBACK_PCT * 100,
        ZERO_DTE_EXIT_TIME.strftime("%H:%M"), WEEKLY_EXIT_DAYS_BEFORE_EXPIRATION,
        ENTRY_FILL_TIMEOUT_SECONDS, CLOSE_FILL_TIMEOUT_SECONDS,
    )
    if not FMP_API_KEY:
        log.info("FMP_API_KEY not set — earnings blackout filter is disabled this run.")
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
                    next_entry_scan_at = _next_scan_boundary(ENTRY_POLL_SECONDS)
            else:
                # Still honor an on-demand summary request while market is closed
                # (e.g. you touch the trigger file over the weekend).
                if os.path.exists(SUMMARY_TRIGGER_FILE):
                    _check_summary_triggers()
                log.info("Market closed — sleeping %ds.", CLOSED_MARKET_SLEEP_SECONDS)
        except Exception as exc:
            log.exception("Unhandled error in cycle: %s", exc)
        time.sleep(EXIT_POLL_SECONDS if market_open else CLOSED_MARKET_SLEEP_SECONDS)


if __name__ == "__main__":
    main()