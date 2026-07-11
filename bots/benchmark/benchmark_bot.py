"""
benchmark_bot.py — Passive Benchmark Bot (BUY & HOLD SPY) — PAPER TRADING
==========================================================================

The baseline for the entire experiment. This bot buys SPY once, at ~99% of
equity, and never trades again. Its weekly summary is the number every active
strategy in the fleet has to beat: if a strategy can't outperform this bot
over months, the strategy — not the market — is the problem.

Design guarantees:
  - NO SELL PATH EXISTS. This file contains no sell-order code of any kind.
    It is not a rule the bot follows; it is a capability the bot lacks.
  - One-time entry with fill confirmation, then hold forever.
  - Adoption guard: if the state file is lost but the account already holds
    the benchmark symbol, the bot ADOPTS the existing position rather than
    buying again. It can never double-buy.
  - Weekly summary identical to the rest of the fleet, so the tracker gets a
    benchmark row in the same format. Trades-closed will always read 0 —
    performance shows up purely in starting vs ending equity.

Memory-light: stock clients only, no indicators, no math. It mostly sleeps.

Hard safety guard: REFUSES TO START unless ALPACA_PAPER is "true".

Setup:  pip install alpaca-py requests pytz python-dotenv

Env vars (all optional except the ALPACA_/DISCORD_ ones):
    ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_PAPER=true
    DISCORD_WEBHOOK_URL
    BENCHMARK_SYMBOL        -> default "SPY"
    ALLOCATION_PCT          -> default "99"
    POLL_MINUTES            -> default "10"    (slow heartbeat; nothing to do)
    CLOSED_MARKET_SLEEP_SECONDS -> default "1800"
    ENTRY_FILL_TIMEOUT_SECONDS  -> default "180"
    POSITION_STATE_FILE     -> default "benchmark_position_state.json"
    WEEKLY_STATS_FILE       -> default "benchmark_weekly_stats.json"
    SUMMARY_TRIGGER_FILE    -> default "post_summary.flag"
    WEEKLY_SUMMARY_TIME     -> default "15:55"
    BOT_LABEL               -> default "Passive Benchmark"
    STRATEGY_LABEL          -> default "Buy & hold SPY"

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
from alpaca.data.requests import StockLatestQuoteRequest

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

load_dotenv()

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

BENCHMARK_SYMBOL = os.environ.get("BENCHMARK_SYMBOL", "SPY").strip().upper()
ALLOCATION_PCT = float(os.environ.get("ALLOCATION_PCT", "99"))

POLL_SECONDS = int(float(os.environ.get("POLL_MINUTES", "10")) * 60)
CLOSED_MARKET_SLEEP_SECONDS = int(os.environ.get("CLOSED_MARKET_SLEEP_SECONDS", "1800"))
ENTRY_FILL_TIMEOUT_SECONDS = int(os.environ.get("ENTRY_FILL_TIMEOUT_SECONDS", "180"))

POSITION_STATE_FILE = os.environ.get("POSITION_STATE_FILE", "benchmark_position_state.json")

EASTERN = pytz.timezone("US/Eastern")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("benchmark")

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
# State (atomic persistence)
# status: flat | pending_entry | holding
# --------------------------------------------------------------------------

def _default_state() -> dict:
    return {"status": "flat", "symbol": BENCHMARK_SYMBOL, "qty": 0,
            "entry_price": None, "entry_date": None,
            "entry_order_id": None, "entry_submitted_at": None}


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
        log.error("Failed to load %s (%s) — starting flat (adoption guard will check the account).",
                  POSITION_STATE_FILE, exc)
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

WEEKLY_STATS_FILE = os.environ.get("WEEKLY_STATS_FILE", "benchmark_weekly_stats.json")
SUMMARY_TRIGGER_FILE = os.environ.get("SUMMARY_TRIGGER_FILE", "post_summary.flag")
_wk_hh, _wk_mm = os.environ.get("WEEKLY_SUMMARY_TIME", "15:55").split(":")
WEEKLY_SUMMARY_TIME = dtime(int(_wk_hh), int(_wk_mm))
BOT_LABEL = os.environ.get("BOT_LABEL", "Passive Benchmark")
STRATEGY_LABEL = os.environ.get("STRATEGY_LABEL", "Buy & hold SPY")


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
    se = s.get("starting_equity")
    ee = s.get("ending_equity")
    try:
        ee = get_account_equity()
        s["ending_equity"] = ee
    except Exception:
        pass
    weekly_return = ((ee - se) / se) if (se and ee) else None
    net_pl = (ee - se) if (se is not None and ee is not None) else None
    holding_txt = (f"{_state['qty']} {_state['symbol']} @ ${_state['entry_price']:.2f} "
                   f"since {_state.get('entry_date') or '—'}") \
        if _state["status"] == "holding" else "not yet invested"

    def m(x): return f"${x:,.2f}" if x is not None else "—"
    def p(x): return f"{x:.1%}" if x is not None else "—"

    color = 0x2ecc71 if (net_pl is not None and net_pl >= 0) else 0xe74c3c
    embed = {
        "title": f"📊 Weekly Summary — {BOT_LABEL}",
        "description": (f"Week of **{s.get('week_start')}** · trigger: _{trigger}_\n"
                        f"THE BASELINE: every active strategy is judged against this row.\n"
                        f"Holding: **{holding_txt}**. This bot never sells."),
        "color": color,
        "fields": [
            {"name": "Bot Name", "value": BOT_LABEL, "inline": True},
            {"name": "Strategy Type", "value": STRATEGY_LABEL, "inline": True},
            {"name": "Week Start", "value": s.get("week_start", "—"), "inline": True},
            {"name": "Starting Equity ($)", "value": m(se), "inline": True},
            {"name": "Ending Equity ($)", "value": m(ee), "inline": True},
            {"name": "Max Drawdown (%)", "value": p(s.get("max_drawdown_pct")), "inline": True},
            {"name": "Net P/L ($)", "value": m(net_pl), "inline": True},
            {"name": "Weekly Return (%)", "value": p(weekly_return), "inline": True},
            {"name": "Trades", "value": "0 (buy & hold)", "inline": True},
        ],
        "footer": {"text": "Passive benchmark · Buy & hold"},
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
# Market gates + data
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


# --------------------------------------------------------------------------
# Adoption guard + the one and only entry
# (There is deliberately NO sell/close code anywhere in this file.)
# --------------------------------------------------------------------------

def adopt_existing_position_if_any() -> bool:
    """If the account already holds the benchmark symbol (state lost/fresh),
    adopt it instead of buying again. Returns True if adopted."""
    try:
        positions = trading_client.get_all_positions()
    except Exception as exc:
        log.warning("Could not list account positions (%s) — adoption check skipped.", exc)
        return False
    for p in positions:
        if p.symbol.upper() == BENCHMARK_SYMBOL:
            _state.update({"status": "holding", "symbol": BENCHMARK_SYMBOL,
                           "qty": int(float(p.qty)),
                           "entry_price": float(p.avg_entry_price),
                           "entry_date": _state.get("entry_date") or date.today().isoformat()})
            _save_state()
            log.info("ADOPTED existing %s position: %s sh @ %.2f (state file was fresh).",
                     BENCHMARK_SYMBOL, p.qty, float(p.avg_entry_price))
            return True
    return False


def buy_and_hold():
    try:
        equity = get_account_equity()
    except Exception as exc:
        log.warning("Could not fetch equity (%s) — entry deferred.", exc)
        return
    price = get_latest_price(BENCHMARK_SYMBOL)
    if not price or price <= 0:
        log.warning("No live price — entry deferred.")
        return
    qty = int((equity * (ALLOCATION_PCT / 100.0)) // price)
    if qty <= 0:
        log.warning("Sized to 0 shares — entry deferred.")
        return
    try:
        order = trading_client.submit_order(order_data=MarketOrderRequest(
            symbol=BENCHMARK_SYMBOL, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    except Exception as exc:
        log.exception("Benchmark entry failed: %s", exc)
        return
    _state.update({"status": "pending_entry", "qty": qty,
                   "entry_order_id": str(order.id), "entry_submitted_at": time.time()})
    _save_state()
    log.info("Benchmark BUY submitted: %d %s (~$%.2f). This is the only trade this bot will ever make.",
             qty, BENCHMARK_SYMBOL, qty * price)
    _discord_entry(qty, price, equity, order)


def _handle_pending_entry():
    try:
        order = trading_client.get_order_by_id(_state["entry_order_id"])
    except Exception as exc:
        log.warning("Entry status fetch failed (%s).", exc)
        return
    st = _order_status_name(order)
    filled = int(float(order.filled_qty or 0))
    if st == "filled":
        _state.update({"status": "holding", "entry_price": float(order.filled_avg_price),
                       "qty": filled or _state["qty"], "entry_date": date.today().isoformat()})
        _save_state()
        log.info("Benchmark position OPEN: %d %s @ %.2f. Holding forever.",
                 _state["qty"], BENCHMARK_SYMBOL, _state["entry_price"])
        return
    if st in ("canceled", "expired", "rejected"):
        if filled > 0:
            _state.update({"status": "holding", "entry_price": float(order.filled_avg_price),
                           "qty": filled, "entry_date": date.today().isoformat()})
            log.info("Partial fill %d before %s — holding the partial forever.", filled, st)
        else:
            log.warning("Entry %s, never filled — will retry next cycle.", st)
            _state.update({"status": "flat", "entry_order_id": None, "entry_submitted_at": None})
        _save_state()
        return
    if time.time() - (_state.get("entry_submitted_at") or 0) > ENTRY_FILL_TIMEOUT_SECONDS:
        log.warning("Entry unfilled after %ds (market order — unusual). Still waiting.",
                    ENTRY_FILL_TIMEOUT_SECONDS)


def _discord_entry(qty, price, equity, order):
    embed = {
        "title": f"📌 Benchmark Established — {BENCHMARK_SYMBOL} (Buy & Hold)",
        "description": (f"**Order ID:** `{order.id}` · **Status:** `{order.status}`\n"
                        f"This is the ONLY trade this bot will ever make. From here on it "
                        f"just holds and reports — the baseline every strategy must beat."),
        "color": 0x95a5a6,
        "fields": [
            {"name": "Symbol", "value": BENCHMARK_SYMBOL, "inline": True},
            {"name": "Shares", "value": str(qty), "inline": True},
            {"name": "Entry ~", "value": f"${price:.2f}", "inline": True},
            {"name": "Allocation", "value": f"{ALLOCATION_PCT:.0f}% of equity", "inline": True},
            {"name": "Account Equity", "value": f"${equity:,.2f}", "inline": True},
            {"name": "Exit Plan", "value": "None. Ever.", "inline": True},
        ],
        "footer": {"text": "Passive benchmark · buy & hold"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        log.error("Discord entry post failed: %s", exc)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main():
    log.info("Starting passive benchmark PAPER bot. Symbol %s, allocation %.0f%%. "
             "One buy, hold forever, weekly reporting only.",
             BENCHMARK_SYMBOL, ALLOCATION_PCT)
    if _state["status"] == "holding":
        log.info("Resuming: holding %d %s @ %.2f since %s.",
                 _state["qty"], _state["symbol"], _state["entry_price"] or 0.0,
                 _state.get("entry_date"))

    while True:
        market_open = False
        try:
            market_open = is_market_open()
            if market_open:
                _ensure_current_week()
                _sample_equity_for_drawdown()
                _check_summary_triggers()
                if _state["status"] == "pending_entry":
                    _handle_pending_entry()
                elif _state["status"] == "flat":
                    if not adopt_existing_position_if_any():
                        buy_and_hold()
            else:
                if os.path.exists(SUMMARY_TRIGGER_FILE):
                    _check_summary_triggers()
                log.info("Market closed — sleeping %ds.", CLOSED_MARKET_SLEEP_SECONDS)
        except Exception as exc:
            log.exception("Unhandled error in cycle: %s", exc)
        time.sleep(POLL_SECONDS if market_open else CLOSED_MARKET_SLEEP_SECONDS)


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# systemd unit — save as /etc/systemd/system/benchmark-bot.service
# NOTE: the filename below must match the file ON DISK exactly (benchmark_bot.py).
# ---------------------------------------------------------------------------
# [Unit]
# Description=Passive Benchmark Bot
# After=network-online.target
# Wants=network-online.target
#
# [Service]
# Type=simple
# User=ubuntu
# WorkingDirectory=/home/ubuntu/bot_benchmark
# ExecStart=/bin/bash -c '/usr/bin/python3 -u /home/ubuntu/bot_benchmark/benchmark_bot.py >> /home/ubuntu/bot_benchmark/benchmark.log 2>&1'
# Restart=always
# RestartSec=10
# MemoryMax=150M
#
# [Install]
# WantedBy=multi-user.target
