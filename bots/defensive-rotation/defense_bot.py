"""
defense_bot.py — Defensive Dual-Momentum Bot (SPY / TLT / CASH) — PAPER TRADING
================================================================================

The DEFENSE counterpart to the offensive momentum-rotation bot. That bot ranks
SPY/QQQ/IWM/GLD weekly and chases the strongest. THIS bot never ranks anything.
It asks one question once a day: "is it safe to be in stocks?" and steps down a
ladder when the answer weakens:

    SPY trend positive                 -> hold SPY   (risk-on)
    SPY negative, TLT trend positive   -> hold TLT   (defense)
    both negative                      -> hold CASH  (full retreat; 2022-style
                                          regimes where stocks AND bonds fall)

Trend = total return over LOOKBACK_TDAYS completed daily closes (~6 months by
default). Checks once per trading day after CHECK_TIME ET; trades only when the
regime actually changes, so turnover is very low by design.

Deliberately simple and shares-only:
  - SPY/TLT shares. No options, no leverage, no intraday noise.
  - Memory-light: stock clients only, pure-Python math, small daily-bar fetch.
  - The value of this bot only becomes visible in drawdowns — in a bull market
    it will look nearly identical to buy-and-hold. That is expected. Judge it
    across a full cycle, not a green week.

Bar-fetch note (lesson from the mean-rev frozen-indicator bug): we fetch a
GENEROUS recent window with a high limit and slice the NEWEST closes. Never
pair a far-back start with a small limit — Alpaca returns oldest-first and you
end up computing on stale history.

Hard safety guard: REFUSES TO START unless ALPACA_PAPER is "true".

Setup:  pip install alpaca-py requests pytz python-dotenv

Env vars (all optional except the ALPACA_/DISCORD_ ones):
    ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_PAPER=true
    DISCORD_WEBHOOK_URL
    RISK_SYMBOL             -> default "SPY"
    DEFENSE_SYMBOL          -> default "TLT"
    LOOKBACK_TDAYS          -> default "126"   (~6 months of trading days)
    CHECK_TIME              -> default "10:00" (ET; daily regime check after this)
    ALLOCATION_PCT          -> default "95"    (% of equity deployed when invested)
    EXIT_POLL_MINUTES       -> default "2"     (pending-order progress checks)
    CLOSED_MARKET_SLEEP_SECONDS -> default "1200"
    ENTRY_FILL_TIMEOUT_SECONDS  -> default "120"
    CLOSE_FILL_TIMEOUT_SECONDS  -> default "90"
    POSITION_STATE_FILE     -> default "defense_position_state.json"
    WEEKLY_STATS_FILE       -> default "defense_weekly_stats.json"
    SUMMARY_TRIGGER_FILE    -> default "post_summary.flag"
    WEEKLY_SUMMARY_TIME     -> default "15:55"
    BOT_LABEL               -> default "Defensive Rotation"
    STRATEGY_LABEL          -> default "Dual momentum (SPY/TLT/cash)"

Educational / paper-testing tool only. Not financial advice.
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
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

load_dotenv()

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

RISK_SYMBOL = os.environ.get("RISK_SYMBOL", "SPY").strip().upper()
DEFENSE_SYMBOL = os.environ.get("DEFENSE_SYMBOL", "TLT").strip().upper()
CASH = "CASH"

LOOKBACK_TDAYS = int(os.environ.get("LOOKBACK_TDAYS", "126"))
_ck_hh, _ck_mm = os.environ.get("CHECK_TIME", "10:00").split(":")
CHECK_TIME = dtime(int(_ck_hh), int(_ck_mm))
ALLOCATION_PCT = float(os.environ.get("ALLOCATION_PCT", "95"))

EXIT_POLL_SECONDS = int(float(os.environ.get("EXIT_POLL_MINUTES", "2")) * 60)
CLOSED_MARKET_SLEEP_SECONDS = int(os.environ.get("CLOSED_MARKET_SLEEP_SECONDS", "1200"))
ENTRY_FILL_TIMEOUT_SECONDS = int(os.environ.get("ENTRY_FILL_TIMEOUT_SECONDS", "120"))
CLOSE_FILL_TIMEOUT_SECONDS = int(os.environ.get("CLOSE_FILL_TIMEOUT_SECONDS", "90"))

POSITION_STATE_FILE = os.environ.get("POSITION_STATE_FILE", "defense_position_state.json")

EASTERN = pytz.timezone("US/Eastern")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("defense")

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


def _order_status_name(order) -> str:
    return str(order.status).split(".")[-1].lower()


# --------------------------------------------------------------------------
# Single-slot position state (atomic persistence)
#
# Shape:
# {
#   "status": "flat" | "pending_entry" | "holding" | "pending_close",
#   "symbol": "SPY"/"TLT"/None,
#   "qty": int,
#   "entry_price": float|None,
#   "entry_order_id": str|None, "entry_submitted_at": float|None,
#   "close_order_id": str|None, "close_submitted_at": float|None,
#   "close_reason": str|None,
#   "pending_target": "SPY"/"TLT"/"CASH"/None,   # where to go after a close fills
#   "last_check_date": "YYYY-MM-DD"|None,
# }
# --------------------------------------------------------------------------

def _default_state() -> dict:
    return {"status": "flat", "symbol": None, "qty": 0, "entry_price": None,
            "entry_order_id": None, "entry_submitted_at": None,
            "close_order_id": None, "close_submitted_at": None,
            "close_reason": None, "pending_target": None,
            "last_check_date": None}


def _load_state() -> dict:
    if not os.path.exists(POSITION_STATE_FILE):
        return _default_state()
    try:
        with open(POSITION_STATE_FILE, "r") as f:
            data = json.load(f)
        base = _default_state()
        base.update(data)
        return base
    except Exception as exc:
        log.error("Failed to load %s (%s) — starting flat.", POSITION_STATE_FILE, exc)
        return _default_state()


def _save_state():
    tmp = f"{POSITION_STATE_FILE}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_state, f, indent=2)
        os.replace(tmp, POSITION_STATE_FILE)
    except Exception as exc:
        log.error("Failed to save %s: %s", POSITION_STATE_FILE, exc)


_state = _load_state()


# --------------------------------------------------------------------------
# Weekly performance summary (identical design to the other bots)
# --------------------------------------------------------------------------

WEEKLY_STATS_FILE = os.environ.get("WEEKLY_STATS_FILE", "defense_weekly_stats.json")
SUMMARY_TRIGGER_FILE = os.environ.get("SUMMARY_TRIGGER_FILE", "post_summary.flag")
_wk_hh, _wk_mm = os.environ.get("WEEKLY_SUMMARY_TIME", "15:55").split(":")
WEEKLY_SUMMARY_TIME = dtime(int(_wk_hh), int(_wk_mm))
BOT_LABEL = os.environ.get("BOT_LABEL", "Defensive Rotation")
STRATEGY_LABEL = os.environ.get("STRATEGY_LABEL", "Dual momentum (SPY/TLT/cash)")


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
    holding = _state["symbol"] if _state["status"] in ("holding", "pending_close") else CASH
    open_count = 1 if _state["status"] in ("pending_entry", "holding", "pending_close") else 0

    def m(x): return f"${x:,.2f}" if x is not None else "—"
    def p(x): return f"{x:.1%}" if x is not None else "—"
    def n(x): return f"{x:.2f}" if x is not None else "—"

    color = 0x2ecc71 if (net_pl is not None and net_pl >= 0) else 0xe74c3c
    embed = {
        "title": f"📊 Weekly Summary — {BOT_LABEL}",
        "description": (f"Week of **{s.get('week_start')}** · trigger: _{trigger}_\n"
                        f"Copy into the tracker ({BOT_LABEL} row).\n"
                        f"⚠️ Realized (closed) trades only. Currently in **{holding}** "
                        f"({open_count} open position not counted)."),
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
            {"name": "Current Holding", "value": holding, "inline": True},
        ],
        "footer": {"text": "Paper-trading defensive-rotation bot · Weekly summary"},
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
# Market gates + data (fixed-pattern bar fetch: newest bars, generous limit)
# --------------------------------------------------------------------------

def is_market_open() -> bool:
    try:
        return bool(trading_client.get_clock().is_open)
    except Exception as exc:
        log.warning("Could not fetch clock (%s); assuming closed.", exc)
        return False


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


def get_daily_closes(symbol: str, need: int) -> list:
    """Return the NEWEST `need` COMPLETED daily closes.

    Generous window + high limit + slice-the-newest (never a small limit with a
    far-back start — that's the frozen-indicator bug). Today's still-forming
    daily bar is dropped so momentum is computed on completed sessions only."""
    start = datetime.now(timezone.utc) - timedelta(days=int(need * 1.9) + 30)
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
    last_bar_date = bars[-1].timestamp.astimezone(EASTERN).date()
    if last_bar_date >= today_et:
        bars = bars[:-1]  # drop today's forming bar
    closes = [b.close for b in bars]
    if len(closes) > need:
        closes = closes[-need:]
    return closes


def compute_momentum(symbol: str):
    """Total return over LOOKBACK_TDAYS completed daily closes, or None."""
    closes = get_daily_closes(symbol, LOOKBACK_TDAYS + 1)
    if len(closes) < LOOKBACK_TDAYS + 1:
        log.info("%s: not enough daily bars (%d/%d) — cannot compute momentum.",
                 symbol, len(closes), LOOKBACK_TDAYS + 1)
        return None
    return closes[-1] / closes[0] - 1.0


def decide_target():
    """The regime ladder. Returns (target, risk_mom, def_mom) — target is
    RISK_SYMBOL, DEFENSE_SYMBOL, or CASH; None target means data unavailable."""
    risk_mom = compute_momentum(RISK_SYMBOL)
    def_mom = compute_momentum(DEFENSE_SYMBOL)
    if risk_mom is None:
        return None, risk_mom, def_mom
    if risk_mom > 0:
        return RISK_SYMBOL, risk_mom, def_mom
    if def_mom is not None and def_mom > 0:
        return DEFENSE_SYMBOL, risk_mom, def_mom
    return CASH, risk_mom, def_mom


# --------------------------------------------------------------------------
# Orders + single-slot state machine
# --------------------------------------------------------------------------

def submit_market(symbol: str, qty: int, side: OrderSide):
    return trading_client.submit_order(order_data=MarketOrderRequest(
        symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY))


def size_shares(symbol: str, equity: float):
    price = get_latest_price(symbol)
    if not price or price <= 0:
        return 0, None
    budget = equity * (ALLOCATION_PCT / 100.0)
    return int(budget // price), price


def enter_position(target: str):
    try:
        equity = get_account_equity()
    except Exception as exc:
        log.warning("Could not fetch equity (%s) — entry deferred.", exc)
        return
    qty, price = size_shares(target, equity)
    if qty <= 0:
        log.warning("%s: sized to 0 shares — entry skipped.", target)
        return
    try:
        order = submit_market(target, qty, OrderSide.BUY)
    except Exception as exc:
        log.exception("%s: entry order failed: %s", target, exc)
        return
    _state.update({"status": "pending_entry", "symbol": target, "qty": qty,
                   "entry_order_id": str(order.id), "entry_submitted_at": time.time(),
                   "entry_price": None, "pending_target": None})
    _save_state()
    log.info("%s: entry submitted x%d (~$%.2f).", target, qty, price or 0.0)
    _discord_entry(target, qty, price, equity, order)


def begin_rotation(target: str, reason: str):
    """Close the current holding; on close fill we enter `target` (or stay in
    cash if target == CASH)."""
    pos_symbol = _state["symbol"]
    try:
        order = submit_market(pos_symbol, _state["qty"], OrderSide.SELL)
    except Exception as exc:
        log.exception("%s: close order failed: %s", pos_symbol, exc)
        return
    _state.update({"status": "pending_close", "close_order_id": str(order.id),
                   "close_submitted_at": time.time(), "close_reason": reason,
                   "pending_target": target})
    _save_state()
    log.info("%s: close submitted — rotating to %s (%s).", pos_symbol, target, reason)


def _finalize_close(exit_price: float):
    entry = _state["entry_price"]
    qty = _state["qty"]
    symbol = _state["symbol"]
    realized = ((exit_price - entry) * qty) if entry else 0.0
    realized_pct = (realized / (entry * qty)) if (entry and qty) else 0.0
    _record_closed_trade(realized)
    reason = _state.get("close_reason") or "rotation"
    log.info("%s: CLOSED @ %.2f (P/L $%.2f, %.1f%%) — %s",
             symbol, exit_price, realized, realized_pct * 100, reason)
    _discord_close(symbol, qty, entry, exit_price, realized, realized_pct, reason)
    target = _state.get("pending_target")
    _state.update(_default_state())
    _state["last_check_date"] = date.today().isoformat()
    _save_state()
    if target and target != CASH:
        enter_position(target)
    else:
        log.info("Now in CASH.")


def _handle_pending_entry():
    try:
        order = trading_client.get_order_by_id(_state["entry_order_id"])
    except Exception as exc:
        log.warning("%s: entry status fetch failed (%s).", _state["symbol"], exc)
        return
    st = _order_status_name(order)
    filled = int(float(order.filled_qty or 0))
    if st == "filled":
        _state["entry_price"] = float(order.filled_avg_price)
        _state["qty"] = filled or _state["qty"]
        _state["status"] = "holding"
        _save_state()
        log.info("%s: entry CONFIRMED @ %.2f x%d.", _state["symbol"], _state["entry_price"], _state["qty"])
        return
    if st in ("canceled", "expired", "rejected"):
        if filled > 0:
            _state["entry_price"] = float(order.filled_avg_price)
            _state["qty"] = filled
            _state["status"] = "holding"
            log.info("%s: entry partial %d before %s — holding partial.", _state["symbol"], filled, st)
        else:
            log.warning("%s: entry %s, never filled — back to flat.", _state["symbol"], st)
            sym = _state["symbol"]
            _state.update(_default_state())
            _state["last_check_date"] = None  # allow a retry check today
        _save_state()
        return
    if time.time() - (_state.get("entry_submitted_at") or 0) > ENTRY_FILL_TIMEOUT_SECONDS \
            and not _state.get("entry_cancel_requested"):
        try:
            trading_client.cancel_order_by_id(_state["entry_order_id"])
            _state["entry_cancel_requested"] = True
            _save_state()
        except Exception as exc:
            log.warning("%s: entry cancel failed (%s).", _state["symbol"], exc)


def _handle_pending_close():
    try:
        order = trading_client.get_order_by_id(_state["close_order_id"])
    except Exception as exc:
        log.warning("%s: close status fetch failed (%s).", _state["symbol"], exc)
        return
    st = _order_status_name(order)
    if st == "filled":
        _finalize_close(float(order.filled_avg_price))
        return
    if st in ("canceled", "expired", "rejected"):
        log.warning("%s: close order %s — reverting to holding for retry.", _state["symbol"], st)
        _state["status"] = "holding"
        _state["close_order_id"] = None
        _state["close_submitted_at"] = None
        _state["pending_target"] = None
        _save_state()


def progress_state_machine():
    if _state["status"] == "pending_entry":
        _handle_pending_entry()
    elif _state["status"] == "pending_close":
        _handle_pending_close()


# --------------------------------------------------------------------------
# Daily regime check
# --------------------------------------------------------------------------

def run_daily_check():
    target, risk_mom, def_mom = decide_target()
    if target is None:
        log.warning("Regime check skipped — momentum data unavailable.")
        return
    fmt = lambda x: f"{x:+.2%}" if x is not None else "n/a"
    holding = _state["symbol"] if _state["status"] in ("holding", "pending_entry", "pending_close") else CASH
    log.info("Regime check: %s mom(%d)=%s, %s mom(%d)=%s -> target %s (holding %s)",
             RISK_SYMBOL, LOOKBACK_TDAYS, fmt(risk_mom),
             DEFENSE_SYMBOL, LOOKBACK_TDAYS, fmt(def_mom), target, holding)
    _state["last_check_date"] = date.today().isoformat()
    _save_state()
    if _state["status"] in ("pending_entry", "pending_close"):
        return  # let in-flight orders settle; regime re-checked tomorrow
    if _state["status"] == "holding":
        if target != _state["symbol"]:
            _discord_regime_change(_state["symbol"], target, risk_mom, def_mom)
            begin_rotation(target, f"Regime change -> {target}")
    else:  # flat / cash
        if target != CASH:
            _discord_regime_change(CASH, target, risk_mom, def_mom)
            enter_position(target)


def _daily_check_due() -> bool:
    now_et = datetime.now(EASTERN)
    if now_et.time() < CHECK_TIME:
        return False
    return _state.get("last_check_date") != date.today().isoformat()


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def _discord_regime_change(from_sym, to_sym, risk_mom, def_mom):
    fmt = lambda x: f"{x:+.2%}" if x is not None else "n/a"
    embed = {
        "title": f"🔀 Regime Change — {from_sym} → {to_sym}",
        "description": "Defensive ladder: risk-on when stocks trend up, bonds "
                       "when they don't, cash when nothing does.",
        "color": 0x3498db,
        "fields": [
            {"name": f"{RISK_SYMBOL} momentum ({LOOKBACK_TDAYS}d)", "value": fmt(risk_mom), "inline": True},
            {"name": f"{DEFENSE_SYMBOL} momentum ({LOOKBACK_TDAYS}d)", "value": fmt(def_mom), "inline": True},
            {"name": "New Target", "value": to_sym, "inline": True},
        ],
        "footer": {"text": "Defensive-rotation bot · regime ladder"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        log.error("Discord regime post failed: %s", exc)


def _discord_entry(symbol, qty, price, equity, order):
    embed = {
        "title": f"📤 Defensive Entry (Awaiting Fill) — {symbol}",
        "description": f"**Order ID:** `{order.id}` · **Status:** `{order.status}`",
        "color": 0x2ecc71,
        "fields": [
            {"name": "Symbol", "value": symbol, "inline": True},
            {"name": "Shares", "value": str(qty), "inline": True},
            {"name": "Entry ~", "value": f"${price:.2f}" if price else "—", "inline": True},
            {"name": "Allocation", "value": f"{ALLOCATION_PCT:.0f}% of equity", "inline": True},
            {"name": "Account Equity", "value": f"${equity:,.2f}", "inline": True},
            {"name": "Signal", "value": f"Dual momentum ({LOOKBACK_TDAYS}d)", "inline": True},
        ],
        "footer": {"text": "Defensive-rotation bot · SPY/TLT/cash"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        log.error("Discord entry post failed: %s", exc)


def _discord_close(symbol, qty, entry, exit_price, realized, realized_pct, reason):
    color = 0x2ecc71 if realized >= 0 else 0xe74c3c
    embed = {
        "title": f"🔚 Defensive Position Closed — {symbol}",
        "description": f"**Reason:** {reason}",
        "color": color,
        "fields": [
            {"name": "Symbol", "value": symbol, "inline": True},
            {"name": "Shares", "value": str(qty), "inline": True},
            {"name": "Entry", "value": f"${entry:.2f}" if entry else "—", "inline": True},
            {"name": "Exit", "value": f"${exit_price:.2f}", "inline": True},
            {"name": "Realized P/L", "value": f"${realized:,.2f} ({realized_pct:+.1%})", "inline": True},
        ],
        "footer": {"text": "Defensive-rotation bot · exit"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        log.error("Discord close post failed: %s", exc)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main():
    log.info("Starting defensive dual-momentum PAPER bot. Risk=%s Defense=%s Cash fallback. "
             "Lookback=%d trading days. Daily check after %s ET. Allocation %.0f%%.",
             RISK_SYMBOL, DEFENSE_SYMBOL, LOOKBACK_TDAYS, CHECK_TIME.strftime("%H:%M"),
             ALLOCATION_PCT)
    if _state["status"] != "flat":
        log.info("Resuming: status=%s symbol=%s qty=%s.",
                 _state["status"], _state["symbol"], _state["qty"])

    while True:
        market_open = False
        try:
            market_open = is_market_open()
            if market_open:
                _ensure_current_week()
                _sample_equity_for_drawdown()
                progress_state_machine()
                _check_summary_triggers()
                if _daily_check_due():
                    run_daily_check()
            else:
                if os.path.exists(SUMMARY_TRIGGER_FILE):
                    _check_summary_triggers()
                log.info("Market closed — sleeping %ds.", CLOSED_MARKET_SLEEP_SECONDS)
        except Exception as exc:
            log.exception("Unhandled error in cycle: %s", exc)
        time.sleep(EXIT_POLL_SECONDS if market_open else CLOSED_MARKET_SLEEP_SECONDS)


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# systemd unit — save as /etc/systemd/system/defense-bot.service
# NOTE: the filename below must match the file ON DISK exactly (defense_bot.py).
# ---------------------------------------------------------------------------
# [Unit]
# Description=Defensive Rotation Bot
# After=network-online.target
# Wants=network-online.target
#
# [Service]
# Type=simple
# User=ubuntu
# WorkingDirectory=/home/ubuntu/bot_defense
# ExecStart=/bin/bash -c '/usr/bin/python3 -u /home/ubuntu/bot_defense/defense_bot.py >> /home/ubuntu/bot_defense/defense.log 2>&1'
# Restart=always
# RestartSec=10
# MemoryMax=200M
#
# [Install]
# WantedBy=multi-user.target
