"""
portfolio_allocator.py — Fleet Risk Monitor & Allocator (STANDALONE)
=====================================================================

Does NOT trade. Does NOT place or modify a single order. It reads each bot's
account equity, maintains an equity-history file, and reports fleet-level risk
that no individual bot can see:

  1. PER-BOT PERFORMANCE — equity, return since first-seen, drawdown from peak.
  2. PRE-SET KILL-LINES  — thresholds fixed IN ADVANCE (max drawdown, return
     floor). Breach => ALERT. The allocator does NOT stop the bot (it can't;
     it's read-only) — it tells YOU so the decision is yours, and the line was
     set cold so a degrading bot can't be rationalized as "normal" months later.
  3. CORRELATION / CONCENTRATION — how correlated the bots' daily returns are.
     Flags when "N independent bots" are really one big beta bet wearing N hats.
  4. DISCORD DIGEST — one consolidated fleet report card.

Because returns/drawdown/correlation need HISTORY, the allocator appends every
bot's equity to allocator_history.json each run. Correlation is meaningful only
after ~2+ weeks of data; until then it honestly reports "insufficient history."

------------------------------------------------------------------------------
CONFIG: fill in each bot's read-only API keys and its kill-line thresholds.
Keys are used ONLY to read equity — this process has no order code at all.
------------------------------------------------------------------------------

Setup:  pip install alpaca-py requests pytz python-dotenv numpy
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

load_dotenv()

# --------------------------------------------------------------------------
# FLEET CONFIG
# Each bot: display name, its API key/secret (read-only use here), and its
# pre-set kill-lines. Fill keys from each bot's own .env. thresholds are set
# ONCE, in advance, and should not be loosened reactively.
#
# kill-lines:
#   max_drawdown_pct : alert if drawdown from the bot's peak equity exceeds this
#   return_floor_pct : alert if total return since first-seen falls below this
#                      (e.g. -15.0 means "alert if it's down more than 15%")
# --------------------------------------------------------------------------

BOTS = [
    # name,                key_env,                 secret_env,                 max_dd_pct, return_floor_pct
    {"name": "Trend Confirmation", "key": os.environ.get("TREND_KEY"),     "secret": os.environ.get("TREND_SECRET"),     "max_dd_pct": 25.0, "return_floor_pct": -20.0},
    {"name": "Mean Reversion",     "key": os.environ.get("MEANREV_KEY"),   "secret": os.environ.get("MEANREV_SECRET"),   "max_dd_pct": 15.0, "return_floor_pct": -12.0},
    {"name": "ORB Breakout",       "key": os.environ.get("ORB_KEY"),       "secret": os.environ.get("ORB_SECRET"),       "max_dd_pct": 20.0, "return_floor_pct": -15.0},
    {"name": "Overnight Drift",    "key": os.environ.get("OVERNIGHT_KEY"), "secret": os.environ.get("OVERNIGHT_SECRET"), "max_dd_pct": 15.0, "return_floor_pct": -12.0},
    {"name": "Momentum Rotation",  "key": os.environ.get("MOMENTUM_KEY"),  "secret": os.environ.get("MOMENTUM_SECRET"),  "max_dd_pct": 18.0, "return_floor_pct": -15.0},
    {"name": "Credit Spreads",     "key": os.environ.get("CREDIT_KEY"),    "secret": os.environ.get("CREDIT_SECRET"),    "max_dd_pct": 15.0, "return_floor_pct": -12.0},
    {"name": "Defensive Rotation", "key": os.environ.get("DEFENSE_KEY"),   "secret": os.environ.get("DEFENSE_SECRET"),   "max_dd_pct": 15.0, "return_floor_pct": -12.0},
    {"name": "Pairs Rel Value",    "key": os.environ.get("PAIRS_KEY"),     "secret": os.environ.get("PAIRS_SECRET"),     "max_dd_pct": 12.0, "return_floor_pct": -10.0},
    {"name": "Passive Benchmark",  "key": os.environ.get("BENCHMARK_KEY"), "secret": os.environ.get("BENCHMARK_SECRET"), "max_dd_pct": 40.0, "return_floor_pct": -40.0},
    # --- Expansion bots (added with fleet growth to 13) ---
    {"name": "Trend Regime",       "key": os.environ.get("TRENDREGIME_KEY"), "secret": os.environ.get("TRENDREGIME_SECRET"), "max_dd_pct": 25.0, "return_floor_pct": -20.0},
    {"name": "VWAP MeanRev",       "key": os.environ.get("VWAP_KEY"),        "secret": os.environ.get("VWAP_SECRET"),        "max_dd_pct": 15.0, "return_floor_pct": -12.0},
    {"name": "Gap Bot",            "key": os.environ.get("GAP_KEY"),         "secret": os.environ.get("GAP_SECRET"),         "max_dd_pct": 15.0, "return_floor_pct": -12.0},
    {"name": "VRP Bot",            "key": os.environ.get("VRP_KEY"),         "secret": os.environ.get("VRP_SECRET"),         "max_dd_pct": 15.0, "return_floor_pct": -12.0},
]

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
HISTORY_FILE = os.environ.get("ALLOCATOR_HISTORY_FILE", "/home/ubuntu/shared/allocator_history.json")

_ct_hh, _ct_mm = os.environ.get("CHECK_TIME", "16:15").split(":")
CHECK_TIME = dtime(int(_ct_hh), int(_ct_mm))  # after the close by default
CLOSED_MARKET_SLEEP_SECONDS = int(os.environ.get("CLOSED_MARKET_SLEEP_SECONDS", "1800"))
OPEN_MARKET_SLEEP_SECONDS = int(os.environ.get("OPEN_MARKET_SLEEP_SECONDS", "900"))
CORRELATION_MIN_DAYS = int(os.environ.get("CORRELATION_MIN_DAYS", "10"))

EASTERN = pytz.timezone("US/Eastern")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("allocator")

# A clock/data client using the first configured bot's keys (any works).
_clock_key = next((b["key"] for b in BOTS if b["key"]), None)
_clock_secret = next((b["secret"] for b in BOTS if b["secret"]), None)
if not _clock_key:
    log.error("No bot API keys configured. Fill the *_KEY / *_SECRET env vars. Exiting.")
    sys.exit(1)
clock_client = TradingClient(_clock_key, _clock_secret, paper=True)


def is_market_open() -> bool:
    try:
        return bool(clock_client.get_clock().is_open)
    except Exception as exc:
        log.warning("Could not fetch clock (%s); assuming closed.", exc)
        return False


def get_equity(key: str, secret: str):
    """Read one account's equity. Read-only; no order path exists."""
    if not key or not secret:
        return None
    try:
        acct = TradingClient(key, secret, paper=True).get_account()
        return float(acct.equity)
    except Exception as exc:
        log.warning("Equity fetch failed (%s).", exc)
        return None


# --------------------------------------------------------------------------
# History persistence: { "YYYY-MM-DD": { "Bot Name": equity, ... }, ... }
# --------------------------------------------------------------------------

def load_history() -> dict:
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except Exception as exc:
        log.error("Failed to load history (%s) — starting fresh.", exc)
        return {}


def save_history(hist: dict):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp = f"{HISTORY_FILE}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(hist, f, indent=2)
        os.replace(tmp, HISTORY_FILE)
    except Exception as exc:
        log.error("Failed to save history: %s", exc)


def _series_for(hist: dict, name: str) -> list:
    """Ordered (date, equity) for one bot across history."""
    out = []
    for d in sorted(hist.keys()):
        v = hist[d].get(name)
        if v is not None:
            out.append((d, float(v)))
    return out


def _daily_returns(series: list) -> list:
    rets = []
    for i in range(1, len(series)):
        prev = series[i - 1][1]
        cur = series[i][1]
        if prev:
            rets.append(cur / prev - 1.0)
    return rets


def _pearson(a: list, b: list):
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / (va ** 0.5 * vb ** 0.5)


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def analyze(hist: dict) -> dict:
    """Per-bot stats + kill-line checks + fleet correlation summary."""
    results = []
    alerts = []
    for bot in BOTS:
        name = bot["name"]
        series = _series_for(hist, name)
        if not series:
            results.append({"name": name, "status": "no data"})
            continue
        first_eq = series[0][1]
        cur_eq = series[-1][1]
        peak = max(v for _, v in series)
        total_ret = (cur_eq / first_eq - 1.0) * 100 if first_eq else 0.0
        drawdown = (peak - cur_eq) / peak * 100 if peak else 0.0

        # Kill-line checks (pre-set, fixed in advance)
        bot_alerts = []
        if drawdown > bot["max_dd_pct"]:
            bot_alerts.append(f"drawdown {drawdown:.1f}% > kill-line {bot['max_dd_pct']:.0f}%")
        if total_ret < bot["return_floor_pct"]:
            bot_alerts.append(f"return {total_ret:+.1f}% < floor {bot['return_floor_pct']:+.0f}%")
        if bot_alerts:
            alerts.append({"name": name, "issues": bot_alerts})

        results.append({
            "name": name, "status": "ok",
            "equity": cur_eq, "total_return_pct": round(total_ret, 2),
            "drawdown_pct": round(drawdown, 2), "peak": peak,
            "days": len(series),
            "breached": bool(bot_alerts),
        })

    # Correlation: average pairwise correlation of active bots' daily returns.
    ret_map = {}
    for bot in BOTS:
        s = _series_for(hist, bot["name"])
        r = _daily_returns(s)
        if len(r) >= CORRELATION_MIN_DAYS:
            ret_map[bot["name"]] = r
    corr_summary = {"pairs": 0, "avg_corr": None, "high_pairs": [], "note": ""}
    names = list(ret_map.keys())
    if len(names) < 2:
        corr_summary["note"] = f"insufficient history (<{CORRELATION_MIN_DAYS} days on 2+ bots)"
    else:
        corrs = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                c = _pearson(ret_map[names[i]], ret_map[names[j]])
                if c is not None:
                    corrs.append(c)
                    if c >= 0.7:
                        corr_summary["high_pairs"].append(
                            {"a": names[i], "b": names[j], "corr": round(c, 2)})
        if corrs:
            corr_summary["pairs"] = len(corrs)
            corr_summary["avg_corr"] = round(sum(corrs) / len(corrs), 2)
            if corr_summary["avg_corr"] >= 0.6:
                corr_summary["note"] = ("HIGH fleet correlation — bots are moving together; "
                                        "diversification is weaker than the bot count implies.")
            else:
                corr_summary["note"] = "correlation within normal range."

    total_equity = sum(r["equity"] for r in results if r.get("status") == "ok")
    return {"results": results, "alerts": alerts, "correlation": corr_summary,
            "total_equity": total_equity, "asof": datetime.now(timezone.utc).isoformat()}


def post_digest(analysis: dict):
    if not DISCORD_WEBHOOK_URL:
        log.info("No Discord webhook — digest logged only.")
        return
    results = analysis["results"]
    alerts = analysis["alerts"]
    corr = analysis["correlation"]

    lines = []
    for r in results:
        if r.get("status") != "ok":
            lines.append(f"• {r['name']}: _no data yet_")
            continue
        flag = " ⚠️" if r.get("breached") else ""
        lines.append(f"• **{r['name']}**: {r['total_return_pct']:+.1f}% "
                     f"(DD {r['drawdown_pct']:.1f}%){flag}")
    perf_block = "\n".join(lines) if lines else "_no data_"

    alert_block = ""
    if alerts:
        alert_block = "\n".join(
            f"🚨 **{a['name']}** — " + "; ".join(a["issues"]) for a in alerts)
    else:
        alert_block = "✅ No kill-lines breached."

    if corr["avg_corr"] is None:
        corr_block = f"_{corr['note']}_"
    else:
        corr_block = (f"Avg pairwise correlation: **{corr['avg_corr']}** "
                      f"across {corr['pairs']} pairs. {corr['note']}")
        if corr["high_pairs"]:
            hp = ", ".join(f"{p['a']}~{p['b']} ({p['corr']})" for p in corr["high_pairs"][:5])
            corr_block += f"\nHighly correlated: {hp}"

    color = 0xe74c3c if alerts else (0xf1c40f if (corr["avg_corr"] or 0) >= 0.6 else 0x2ecc71)
    embed = {
        "title": "🗂️ Fleet Risk Digest — Portfolio Allocator",
        "description": f"Total fleet equity: **${analysis['total_equity']:,.0f}**\n"
                       f"_Read-only monitor · does not trade · kill-lines set in advance_",
        "color": color,
        "fields": [
            {"name": "Performance (return / drawdown)", "value": perf_block[:1024], "inline": False},
            {"name": "Kill-Line Status", "value": alert_block[:1024], "inline": False},
            {"name": "Correlation / Concentration", "value": corr_block[:1024], "inline": False},
        ],
        "footer": {"text": "Portfolio allocator · advisory risk monitor"},
        "timestamp": analysis["asof"],
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10).raise_for_status()
        log.info("Fleet digest posted.")
    except requests.RequestException as exc:
        log.error("Discord digest post failed: %s", exc)


def run_cycle():
    hist = load_history()
    today = date.today().isoformat()
    snapshot = hist.get(today, {})
    for bot in BOTS:
        eq = get_equity(bot["key"], bot["secret"])
        if eq is not None:
            snapshot[bot["name"]] = eq
    hist[today] = snapshot
    save_history(hist)
    log.info("Recorded equities for %d bots on %s.", len(snapshot), today)

    analysis = analyze(hist)
    for a in analysis["alerts"]:
        log.warning("KILL-LINE: %s — %s", a["name"], "; ".join(a["issues"]))
    if analysis["correlation"]["avg_corr"] is not None:
        log.info("Fleet avg correlation: %s (%s)",
                 analysis["correlation"]["avg_corr"], analysis["correlation"]["note"])
    post_digest(analysis)


def _due_today(last_date) -> bool:
    now_et = datetime.now(EASTERN)
    if now_et.time() < CHECK_TIME:
        return False
    return last_date != date.today().isoformat()


def main():
    log.info("Starting portfolio allocator (STANDALONE, read-only, no trading). "
             "%d bots configured, digest after %s ET, history at %s.",
             len(BOTS), CHECK_TIME.strftime("%H:%M"), HISTORY_FILE)
    last_run_date = None
    while True:
        market_open = False
        try:
            market_open = is_market_open()
            # Run once per day after CHECK_TIME; also run once on first boot.
            if last_run_date is None or _due_today(last_run_date):
                run_cycle()
                last_run_date = date.today().isoformat()
        except Exception as exc:
            log.exception("Unhandled error in allocator cycle: %s", exc)
        time.sleep(OPEN_MARKET_SLEEP_SECONDS if market_open else CLOSED_MARKET_SLEEP_SECONDS)


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# systemd unit — save as /etc/systemd/system/allocator.service
# (filename on disk must match: portfolio_allocator.py)
# ---------------------------------------------------------------------------
# [Unit]
# Description=Portfolio Allocator (Fleet Risk Monitor)
# After=network-online.target
# Wants=network-online.target
#
# [Service]
# Type=simple
# User=ubuntu
# WorkingDirectory=/home/ubuntu/allocator
# ExecStart=/bin/bash -c '/usr/bin/python3 -u /home/ubuntu/allocator/portfolio_allocator.py >> /home/ubuntu/allocator/allocator.log 2>&1'
# Restart=always
# RestartSec=10
# MemoryMax=200M
#
# [Install]
# WantedBy=multi-user.target
