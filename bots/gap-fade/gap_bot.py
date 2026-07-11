"""
gap_bot.py — Overnight Gap Fade/Continuation Bot (PAPER TRADING)
================================================================

Strategy (one decision per symbol per day, at the open):
  gap_pct = (today_open - prior_close) / prior_close * 100

  |gap| <  GAP_MIN_PCT   -> no trade (noise; spread/slippage dominate)
  GAP_MIN..GAP_RIDE      -> FADE: enter against the gap, target = prior close
                            (the classic gap-fill statistic)
  GAP_RIDE..GAP_MAX      -> RIDE: enter with the gap, target = one more
                            gap-distance in the gap direction
  |gap| >  GAP_MAX_PCT   -> stand down (event day: halts/news; a threshold
                            bot has no business here)

Execution rules:
  - Decision window: from ENTRY_ET (09:31) until ENTRY_WINDOW_END_ET (09:40).
    One attempt per symbol per day, entered as a market order.
  - Shares only (SPY/QQQ). Risk-based sizing off the stop distance.
  - Stop: STOP_PCT adverse move. Target as above. HARD FLATTEN at
    FLATTEN_ET (15:45) — nothing is ever held overnight.
  - Daily loss cap and max-concurrent inherited from the fleet pattern.

Plumbing (state machine, weekly summary, Discord embeds, sizing) is
inherited from the mean-reversion bot — battle-tested, unchanged.

Hard safety guard: refuses to start unless ALPACA_PAPER == "true".
"""

import os
import sys
import json
import time
import math
import logging
from datetime import datetime, date, timedelta, time as dtime, timezone

import requests
import pytz
from dotenv import load_dotenv

# NOTE: intentionally NOT importing any option data/chain clients — this bot
# trades shares only, and skipping those imports is a real memory saving.
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

load_dotenv()

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

SYMBOLS = [s.strip().upper() for s in os.environ.get("SYMBOLS", "SPY,QQQ").split(",") if s.strip()]
BAR_MINUTES = int(os.environ.get("BAR_MINUTES", "15"))
GAP_MIN_PCT = float(os.environ.get("GAP_MIN_PCT", "0.25"))
GAP_RIDE_PCT = float(os.environ.get("GAP_RIDE_PCT", "0.85"))
GAP_MAX_PCT = float(os.environ.get("GAP_MAX_PCT", "2.5"))
ENTRY_ET = os.environ.get("ENTRY_ET", "09:31")
ENTRY_WINDOW_END_ET = os.environ.get("ENTRY_WINDOW_END_ET", "09:40")
FLATTEN_ET = os.environ.get("FLATTEN_ET", "15:45")
ALLOW_SHORTS = os.environ.get("ALLOW_SHORTS", "true").lower() == "true"

RISK_PCT = float(os.environ.get("RISK_PCT", "1.0"))
STOP_PCT = float(os.environ.get("STOP_PCT", "0.02"))
MAX_HOLD_BARS = int(os.environ.get("MAX_HOLD_BARS", "26"))
MAX_DAILY_LOSS_PCT = float(os.environ.get("MAX_DAILY_LOSS_PCT", "3.0"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))

ENTRY_POLL_SECONDS = int(float(os.environ.get("ENTRY_POLL_MINUTES", "15")) * 60)
EXIT_POLL_SECONDS = int(float(os.environ.get("EXIT_POLL_MINUTES", "2")) * 60)
if EXIT_POLL_SECONDS <= 0:
    EXIT_POLL_SECONDS = ENTRY_POLL_SECONDS
CLOSED_MARKET_SLEEP_SECONDS = int(os.environ.get("CLOSED_MARKET_SLEEP_SECONDS", "1200"))
ENTRY_FILL_TIMEOUT_SECONDS = int(os.environ.get("ENTRY_FILL_TIMEOUT_SECONDS", "120"))
CLOSE_FILL_TIMEOUT_SECONDS = int(os.environ.get("CLOSE_FILL_TIMEOUT_SECONDS", "60"))

POSITION_STATE_FILE = os.environ.get("POSITION_STATE_FILE", "meanrev_positions_state.json")

EASTERN = pytz.timezone("US/Eastern")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("meanrev")

if not API_KEY or not SECRET_KEY:
    log.error("ALPACA_API_KEY / ALPACA_SECRET_KEY not set. Exiting.")
    sys.exit(1)
if not DISCORD_WEBHOOK_URL:
    log.error("DISCORD_WEBHOOK_URL not set. Exiting.")
    sys.exit(1)
if not PAPER:
    log.error("ALPACA_PAPER is not 'true'. Refusing to submit live orders. Set ALPACA_PAPER=true.")
    sys.exit(1)

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

_daily_state = {"date": None, "realized_pl": 0.0}


def _reset_daily_state_if_new_day():
    today_str = date.today().isoformat()
    if _daily_state["date"] != today_str:
        _daily_state["date"] = today_str
        _daily_state["realized_pl"] = 0.0


def _order_status_name(order) -> str:
    return str(order.status).split(".")[-1].lower()


# --------------------------------------------------------------------------
# State persistence (atomic)
# --------------------------------------------------------------------------

def _load_positions() -> dict:
    if not os.path.exists(POSITION_STATE_FILE):
        return {}
    try:
        with open(POSITION_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception as exc:
        log.error("Failed to load %s (%s) — starting with none.", POSITION_STATE_FILE, exc)
        return {}


def _save_positions():
    tmp = f"{POSITION_STATE_FILE}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_positions, f, indent=2)
        os.replace(tmp, POSITION_STATE_FILE)
    except Exception as exc:
        log.error("Failed to save %s: %s", POSITION_STATE_FILE, exc)


_positions = _load_positions()


# --------------------------------------------------------------------------
# Weekly performance summary (identical design to the other bots)
# --------------------------------------------------------------------------

WEEKLY_STATS_FILE = os.environ.get("WEEKLY_STATS_FILE", "meanrev_weekly_stats.json")
SUMMARY_TRIGGER_FILE = os.environ.get("SUMMARY_TRIGGER_FILE", "post_summary.flag")
_wk_hh, _wk_mm = os.environ.get("WEEKLY_SUMMARY_TIME", "15:55").split(":")
WEEKLY_SUMMARY_TIME = dtime(int(_wk_hh), int(_wk_mm))
BOT_LABEL = os.environ.get("BOT_LABEL", "Mean Reversion")
STRATEGY_LABEL = os.environ.get("STRATEGY_LABEL", "SPY/QQQ reversal (shares)")


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
        log.error("Failed to load %s (%s) — fresh weekly stats.", WEEKLY_STATS_FILE, exc)
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


def get_account_equity() -> float:
    return float(trading_client.get_account().equity)


def _ensure_current_week():
    this_monday = _iso_monday(date.today())
    if _weekly_stats.get("week_start") != this_monday:
        log.info("New trading week — resetting weekly tracker (was %s, now %s).",
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
    # also feed the daily-loss circuit breaker
    _daily_state["realized_pl"] = round(_daily_state["realized_pl"] + realized_dollars, 2)


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
    open_count = len(_positions)

    def m(x): return f"${x:,.2f}" if x is not None else "—"
    def p(x): return f"{x:.1%}" if x is not None else "—"
    def n(x): return f"{x:.2f}" if x is not None else "—"

    color = 0x2ecc71 if (net_pl is not None and net_pl >= 0) else 0xe74c3c
    embed = {
        "title": f"📊 Weekly Summary — {BOT_LABEL}",
        "description": (f"Week of **{s.get('week_start')}** · trigger: _{trigger}_\n"
                        f"Copy into the tracker ({BOT_LABEL} row).\n"
                        f"⚠️ Realized (closed) trades only. **{open_count} position(s) still open**, not counted."),
        "color": color,
        "fields": [
            {"name": "Bot Name", "value": BOT_LABEL, "inline": True},
            {"name": "Strategy Type", "value": STRATEGY_LABEL, "inline": True},
            {"name": "Week Start", "value": s.get("week_start", "—"), "inline": True},
            {"name": "Starting Equity ($)", "value": m(se), "inline": True},
            {"name": "Ending Equity ($)", "value": m(ee), "inline": True},
            {"name": "Max Drawdown (%)", "value": p(s.get("max_drawdown_pct")), "inline": True},
            {"name": "Total Trades", "value": str(trades), "inline": True},
            {"name": "Winning Trades", "value": str(wins), "inline": True},
            {"name": "Losing Trades", "value": str(losses), "inline": True},
            {"name": "Gross Profit ($)", "value": m(gp), "inline": True},
            {"name": "Gross Loss ($)", "value": m(gl), "inline": True},
            {"name": "Net P/L ($)", "value": m(net_pl), "inline": True},
            {"name": "Win Rate (%)", "value": p(win_rate), "inline": True},
            {"name": "Weekly Return (%)", "value": p(weekly_return), "inline": True},
            {"name": "Profit Factor", "value": n(profit_factor), "inline": True},
            {"name": "Avg Win ($)", "value": m(avg_win), "inline": True},
            {"name": "Avg Loss ($)", "value": m(avg_loss), "inline": True},
            {"name": "Open (uncounted)", "value": str(open_count), "inline": True},
        ],
        "footer": {"text": "Paper-trading mean-reversion bot · Weekly summary"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        resp.raise_for_status()
        log.info("Weekly summary posted (trigger: %s).", trigger)
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
            log.info("Friday close — posting scheduled weekly summary.")
            _post_weekly_summary("scheduled Friday close")


# --------------------------------------------------------------------------
# Market gates + data + indicators (pure Python, no numpy/pandas)
# --------------------------------------------------------------------------

def is_market_open() -> bool:
    try:
        return bool(trading_client.get_clock().is_open)
    except Exception as exc:
        log.warning("Could not fetch clock (%s); assuming closed.", exc)
        return False


def closed_market_sleep_seconds() -> int:
    """Coarse sleep when the open is far away, fine-grained when it is near.

    The entry window is only 09:31-09:40 ET; a 20-minute nap that starts at
    09:20 wakes at 09:40:28 and misses the whole day (this happened on
    2026-07-06). Within 30 minutes of the next open, sleep in 60s steps so
    the bot is polling within a minute of the bell.
    """
    try:
        clock = trading_client.get_clock()
        if clock.is_open:
            return EXIT_POLL_SECONDS
        secs_to_open = (clock.next_open - clock.timestamp).total_seconds()
        if secs_to_open <= 1800:
            return 60
    except Exception as exc:
        log.warning("Could not fetch clock for sleep sizing (%s).", exc)
    return CLOSED_MARKET_SLEEP_SECONDS


def get_latest_price(symbol: str):
    try:
        q = stock_data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
        bid, ask = q.bid_price, q.ask_price
        if bid and ask:
            return (bid + ask) / 2
        return ask or bid
    except Exception as exc:
        log.warning("%s: latest price fetch failed (%s).", symbol, exc)
        return None


def get_bars(symbol: str, minutes: int, need: int) -> list:
    """Fetch recent bars and return the NEWEST `need` closes.

    NOTE: Alpaca returns bars oldest-first. A `limit` combined with a far-back
    `start` returns the OLDEST bars in the window and stops — which froze the
    indicators on 10-day-old data. Instead we use a tight recent window, no
    restrictive limit, and slice the last `need` bars so the band/RSI advance
    with the live session."""
    start = (datetime.now(timezone.utc) - timedelta(days=5))
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
        start=start,
        limit=2000,
    )
    try:
        bars = stock_data_client.get_stock_bars(req).data.get(symbol, [])
    except Exception as exc:
        log.warning("%s: bar fetch failed (%s).", symbol, exc)
        return []
    if not bars:
        return []
    # drop a still-forming last bar
    age_min = (datetime.now(timezone.utc) - bars[-1].timestamp).total_seconds() / 60
    if age_min < minutes:
        bars = bars[:-1]
    closes = [b.close for b in bars]
    # take the NEWEST `need` closes (recent end of the window)
    if len(closes) > need:
        closes = closes[-need:]
    return closes


def _et_now():
    return datetime.now(pytz.timezone("US/Eastern"))


def _et_time_reached(hhmm: str) -> bool:
    h, m = (int(x) for x in hhmm.split(":"))
    now = _et_now()
    return (now.hour, now.minute) >= (h, m)


def get_prior_close(symbol: str):
    """Prior REGULAR-SESSION daily close, from daily bars (last completed day)."""
    start = datetime.now(timezone.utc) - timedelta(days=10)
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                           start=start, limit=10)
    try:
        bars = stock_data_client.get_stock_bars(req).data.get(symbol, [])
    except Exception as exc:
        log.warning("%s: daily bar fetch failed (%s).", symbol, exc)
        return None
    if not bars:
        return None
    et = pytz.timezone("US/Eastern")
    today_et = datetime.now(et).date()
    completed = [b for b in bars if b.timestamp.astimezone(et).date() < today_et]
    if not completed:
        return None
    return completed[-1].close


def classify_gap(symbol: str):
    """Return (mode, side, gap_pct, prior_close, price) where mode is
    'fade' | 'ride' | None. side is the trade direction ('long'/'short')."""
    prior_close = get_prior_close(symbol)
    price = get_latest_price(symbol)
    if prior_close is None or price is None:
        log.warning("%s: missing prior close or price — no gap decision.", symbol)
        return None, None, None, prior_close, price
    gap_pct = (price - prior_close) / prior_close * 100.0
    a = abs(gap_pct)
    if a < GAP_MIN_PCT:
        mode = None
        reason = f"below {GAP_MIN_PCT}% floor"
    elif a <= GAP_RIDE_PCT:
        mode = "fade"
        side = "short" if gap_pct > 0 else "long"     # against the gap
        reason = "fade zone"
    elif a <= GAP_MAX_PCT:
        mode = "ride"
        side = "long" if gap_pct > 0 else "short"     # with the gap
        reason = "ride zone"
    else:
        mode = None
        reason = f"above {GAP_MAX_PCT}% event cap — standing down"
    log.info("%s gap: prior_close=%.2f open_px=%.2f gap=%+.2f%% -> %s (%s)",
             symbol, prior_close, price, gap_pct, mode or "no trade", reason)
    if mode is None:
        return None, None, gap_pct, prior_close, price
    if side == "short" and not ALLOW_SHORTS:
        log.info("%s: %s wants short but shorts disabled — skipping.", symbol, mode)
        return None, None, gap_pct, prior_close, price
    return mode, side, gap_pct, prior_close, price


def gap_targets(mode: str, side: str, price: float, prior_close: float):
    """Target and stop for the trade. Fade targets the gap fill (prior close);
    ride targets one more gap-distance beyond the current price."""
    if mode == "fade":
        target = prior_close
    else:  # ride
        move = abs(price - prior_close)
        target = price + move if side == "long" else price - move
    stop = price * (1 - STOP_PCT) if side == "long" else price * (1 + STOP_PCT)
    return target, stop


# --------------------------------------------------------------------------
# Sizing (risk-based on the stop distance)
# --------------------------------------------------------------------------

def size_shares(entry: float, stop: float, equity: float) -> int:
    per_share_risk = abs(entry - stop)
    if per_share_risk <= 0:
        return 0
    dollar_risk = equity * (RISK_PCT / 100.0)
    return int(dollar_risk // per_share_risk)


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

def submit_market(symbol: str, qty: int, side: OrderSide):
    return trading_client.submit_order(order_data=MarketOrderRequest(
        symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY))


# --------------------------------------------------------------------------
# State machine: pending_entry -> open -> pending_close -> removed
# --------------------------------------------------------------------------

def _daily_loss_hit(equity: float) -> bool:
    cap = -abs(equity * (MAX_DAILY_LOSS_PCT / 100.0))
    if _daily_state["realized_pl"] <= cap:
        log.warning("Daily loss cap hit (realized %.2f <= cap %.2f) — no new entries today.",
                    _daily_state["realized_pl"], cap)
        return True
    return False


def _count_active() -> int:
    return sum(1 for p in _positions.values() if p.get("status") in ("pending_entry", "open", "pending_close"))


def _has_symbol(symbol: str) -> bool:
    return any(p.get("symbol") == symbol and p.get("status") in ("pending_entry", "open", "pending_close")
               for p in _positions.values())


def open_position(symbol: str, side: str, price: float, mean: float, stop: float, equity: float):
    qty = size_shares(price, stop, equity)
    if qty <= 0:
        log.info("%s: sized to 0 shares — skipping.", symbol)
        return
    order_side = OrderSide.BUY if side == "long" else OrderSide.SELL
    try:
        order = submit_market(symbol, qty, order_side)
    except Exception as exc:
        log.exception("%s: entry order failed: %s", symbol, exc)
        return
    key = f"{symbol}:{order.id}"
    _positions[key] = {
        "status": "pending_entry",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "entry_order_id": str(order.id),
        "entry_submitted_at": time.time(),
        "entry_price": None,
        "mean_target": mean,
        "stop_price": stop,
        "bars_held": 0,
    }
    _save_positions()
    log.info("%s: %s entry submitted x%d (target %.2f, stop %.2f).", symbol, side, qty, mean, stop)
    _discord_entry(symbol, side, qty, price, mean, stop, equity, order)


def _finalize_close(key: str, pos: dict, exit_price: float, reason: str):
    entry = pos["entry_price"]
    if pos["side"] == "long":
        realized = (exit_price - entry) * pos["qty"]
    else:
        realized = (entry - exit_price) * pos["qty"]
    _record_closed_trade(realized)
    realized_pct = (realized / (entry * pos["qty"])) if entry else 0.0
    log.info("%s: CLOSED %s @ %.2f (P/L $%.2f, %.1f%%) — %s",
             pos["symbol"], pos["side"], exit_price, realized, realized_pct * 100, reason)
    _discord_close(pos, exit_price, realized, realized_pct, reason)
    del _positions[key]
    _save_positions()


def _handle_pending_entry(key: str, pos: dict):
    try:
        order = trading_client.get_order_by_id(pos["entry_order_id"])
    except Exception as exc:
        log.warning("%s: entry status fetch failed (%s).", pos["symbol"], exc)
        return
    st = _order_status_name(order)
    filled = int(float(order.filled_qty or 0))
    if st == "filled":
        pos["entry_price"] = float(order.filled_avg_price)
        pos["qty"] = filled or pos["qty"]
        pos["status"] = "open"
        log.info("%s: entry CONFIRMED @ %.2f x%d.", pos["symbol"], pos["entry_price"], pos["qty"])
        _save_positions()
        return
    if st in ("canceled", "expired", "rejected"):
        if filled > 0:
            pos["entry_price"] = float(order.filled_avg_price)
            pos["qty"] = filled
            pos["status"] = "open"
            log.info("%s: entry partial %d before %s — tracking as open.", pos["symbol"], filled, st)
        else:
            log.warning("%s: entry %s, never filled — dropping.", pos["symbol"], st)
            del _positions[key]
        _save_positions()
        return
    if time.time() - pos["entry_submitted_at"] > ENTRY_FILL_TIMEOUT_SECONDS and not pos.get("cancel_requested"):
        try:
            trading_client.cancel_order_by_id(pos["entry_order_id"])
            pos["cancel_requested"] = True
            _save_positions()
        except Exception as exc:
            log.warning("%s: entry cancel failed (%s).", pos["symbol"], exc)


def _handle_pending_close(key: str, pos: dict):
    try:
        order = trading_client.get_order_by_id(pos["close_order_id"])
    except Exception as exc:
        log.warning("%s: close status fetch failed (%s).", pos["symbol"], exc)
        return
    st = _order_status_name(order)
    if st == "filled":
        _finalize_close(key, pos, float(order.filled_avg_price), pos.get("close_reason", "exit"))
        return
    if st in ("canceled", "expired", "rejected"):
        # market close should fill; if it didn't, revert to open and re-evaluate
        log.warning("%s: close order %s — reverting to open for retry.", pos["symbol"], st)
        pos["status"] = "open"
        pos.pop("close_order_id", None)
        pos.pop("close_submitted_at", None)
        _save_positions()


def close_position(key: str, pos: dict, reason: str):
    order_side = OrderSide.SELL if pos["side"] == "long" else OrderSide.BUY
    try:
        order = submit_market(pos["symbol"], pos["qty"], order_side)
    except Exception as exc:
        log.exception("%s: close order failed: %s", pos["symbol"], exc)
        return
    pos["status"] = "pending_close"
    pos["close_order_id"] = str(order.id)
    pos["close_submitted_at"] = time.time()
    pos["close_reason"] = reason
    _save_positions()
    log.info("%s: close submitted (%s).", pos["symbol"], reason)


def manage_positions():
    if not _positions:
        return
    for key in list(_positions.keys()):
        pos = _positions[key]
        status = pos.get("status", "open")
        if status == "pending_entry":
            _handle_pending_entry(key, pos)
            continue
        if status == "pending_close":
            _handle_pending_close(key, pos)
            continue
        # open: check exits
        price = get_latest_price(pos["symbol"])
        if price is None:
            continue
        pos["bars_held"] = pos.get("bars_held", 0)
        side = pos["side"]
        entry = pos["entry_price"]
        # take profit: target reached
        hit = (price >= pos["mean_target"]) if side == "long" else (price <= pos["mean_target"])
        # stop
        stopped = (price <= pos["stop_price"]) if side == "long" else (price >= pos["stop_price"])
        if _et_time_reached(FLATTEN_ET):
            close_position(key, pos, f"Hard flatten {FLATTEN_ET} ET @ {price:.2f}")
        elif stopped:
            close_position(key, pos, f"Stop hit @ {price:.2f}")
        elif hit:
            close_position(key, pos, f"Target {pos['mean_target']:.2f} reached")
        else:
            _save_positions()


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

_entry_signal_label = "gap"


def _discord_entry(symbol, side, qty, price, mean, stop, equity, order):
    color = 0x2ecc71 if side == "long" else 0xe74c3c
    embed = {
        "title": f"📤 Gap Entry (Awaiting Fill) — {symbol}",
        "description": f"**Order ID:** `{order.id}` · **Status:** `{order.status}`",
        "color": color,
        "fields": [
            {"name": "Symbol", "value": symbol, "inline": True},
            {"name": "Side", "value": side.capitalize(), "inline": True},
            {"name": "Shares", "value": str(qty), "inline": True},
            {"name": "Entry ~", "value": f"${price:.2f}", "inline": True},
            {"name": "Target", "value": f"${mean:.2f}", "inline": True},
            {"name": "Stop", "value": f"${stop:.2f}", "inline": True},
            {"name": "Account Equity", "value": f"${equity:,.2f}", "inline": True},
            {"name": "Signal", "value": _entry_signal_label, "inline": True},
        ],
        "footer": {"text": "Gap bot · fade/continuation · risk-based sizing"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        log.error("Discord entry post failed: %s", exc)


def _discord_close(pos, exit_price, realized, realized_pct, reason):
    color = 0x2ecc71 if realized >= 0 else 0xe74c3c
    embed = {
        "title": f"🔚 Gap Trade Closed — {pos['symbol']}",
        "description": f"**Reason:** {reason}",
        "color": color,
        "fields": [
            {"name": "Symbol", "value": pos["symbol"], "inline": True},
            {"name": "Side", "value": pos["side"].capitalize(), "inline": True},
            {"name": "Shares", "value": str(pos["qty"]), "inline": True},
            {"name": "Entry", "value": f"${pos['entry_price']:.2f}", "inline": True},
            {"name": "Exit", "value": f"${exit_price:.2f}", "inline": True},
            {"name": "Realized P/L", "value": f"${realized:,.2f} ({realized_pct:+.1%})", "inline": True},
        ],
        "footer": {"text": "Gap bot · exit management"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        log.error("Discord close post failed: %s", exc)


# --------------------------------------------------------------------------
# Entry scan
# --------------------------------------------------------------------------

_gap_traded_today = {"date": None, "symbols": set()}


def _reset_gap_day():
    today = date.today().isoformat()
    if _gap_traded_today["date"] != today:
        _gap_traded_today["date"] = today
        _gap_traded_today["symbols"] = set()


def in_entry_window() -> bool:
    return _et_time_reached(ENTRY_ET) and not _et_time_reached(ENTRY_WINDOW_END_ET)


def run_open_decision():
    """Once per symbol per day, inside the entry window: classify the gap and
    trade it (or log why not). A symbol is marked decided whether or not a
    trade resulted — one decision per day, no re-litigating at 10 AM."""
    _reset_gap_day()
    if not in_entry_window():
        return
    try:
        equity = get_account_equity()
    except Exception as exc:
        log.warning("Could not fetch equity (%s) — skipping open decision.", exc)
        return
    if _daily_loss_hit(equity):
        return
    global _entry_signal_label
    for symbol in SYMBOLS:
        if symbol in _gap_traded_today["symbols"]:
            continue
        if _count_active() >= MAX_CONCURRENT:
            log.info("At concurrency cap (%d) — no new entries.", MAX_CONCURRENT)
            break
        if _has_symbol(symbol):
            _gap_traded_today["symbols"].add(symbol)
            continue
        mode, side, gap_pct, prior_close, price = classify_gap(symbol)
        _gap_traded_today["symbols"].add(symbol)   # decided, trade or not
        if mode is None:
            continue
        target, stop = gap_targets(mode, side, price, prior_close)
        _entry_signal_label = f"{mode.upper()} gap {gap_pct:+.2f}%"
        open_position(symbol, side, price, target, stop, equity)


def main():
    log.info("Starting GAP fade/continuation PAPER bot. Symbols: %s. "
             "Zones: <%.2f%% none | %.2f-%.2f%% fade | %.2f-%.2f%% ride | >%.2f%% stand down. "
             "Entry window %s-%s ET, hard flatten %s ET.",
             SYMBOLS, GAP_MIN_PCT, GAP_MIN_PCT, GAP_RIDE_PCT, GAP_RIDE_PCT,
             GAP_MAX_PCT, GAP_MAX_PCT, ENTRY_ET, ENTRY_WINDOW_END_ET, FLATTEN_ET)
    log.info("Risk %.1f%%/trade, stop %.1f%%, daily loss cap %.1f%%, max concurrent %d, shorts=%s.",
             RISK_PCT, STOP_PCT * 100, MAX_DAILY_LOSS_PCT, MAX_CONCURRENT, ALLOW_SHORTS)
    if _positions:
        log.info("Resuming with %d tracked position(s).", len(_positions))

    while True:
        market_open = False
        try:
            market_open = is_market_open()
            if market_open:
                _reset_daily_state_if_new_day()
                _ensure_current_week()
                _sample_equity_for_drawdown()
                manage_positions()
                _check_summary_triggers()
                run_open_decision()
            else:
                if os.path.exists(SUMMARY_TRIGGER_FILE):
                    _check_summary_triggers()
                sleep_s = closed_market_sleep_seconds()
                if sleep_s >= CLOSED_MARKET_SLEEP_SECONDS:
                    log.info("Market closed — sleeping %ds.", sleep_s)
        except Exception as exc:
            log.exception("Unhandled error in cycle: %s", exc)
        time.sleep(EXIT_POLL_SECONDS if market_open else closed_market_sleep_seconds())


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# systemd unit (memory-capped) — save as /etc/systemd/system/gap-bot.service
# ---------------------------------------------------------------------------
# [Unit]
# Description=Gap Fade/Continuation Bot
# After=network-online.target
#
# [Service]
# Type=simple
# WorkingDirectory=/home/ubuntu/bot_gap
# ExecStart=/usr/bin/python3 /home/ubuntu/bot_gap/gap_bot.py
# Restart=always
# RestartSec=10
# # Hard memory cap: if this bot ever exceeds 180 MB it is restarted, so it can
# # NEVER OOM-kill a sibling bot on the shared t3.micro.
# MemoryMax=180M
#
# [Install]
# WantedBy=multi-user.target
