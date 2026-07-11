# Architecture

## Design goals

1. **Isolation:** one strategy's crash, memory leak, or bad trade cannot affect another. Achieved with per-bot processes, per-bot brokerage accounts, per-bot logs and alert channels, and systemd `MemoryMax` caps.
2. **Comparability:** strategies are an experiment. Every variant differs from its control by one variable; every strategy is measured against a passive benchmark holding SPY.
3. **Unattended operation:** the fleet must run for weeks without SSH. systemd restarts crashes; logrotate bounds disk; Discord surfaces anything that needs a human.

## Data flows

**Market data / orders:** each bot talks directly to Alpaca (alpaca-py). No shared broker connection — an API issue in one bot stays in that bot.

**Shared regime state:** the Regime Calculator writes `regime_state.json` (market breadth, SPY trend, volatility proxy) once per trading morning. Regime-aware bots read it with a staleness check: if the file is older than a configurable threshold (sized to survive weekends), the bot fails safe.

**Risk oversight:** the Portfolio Allocator reads all 13 account equities nightly, appends to a history file (the fleet's canonical performance record), computes drawdowns against per-strategy kill-lines, and posts a fleet digest to Discord. Kill-lines are advisory-then-enforced: a strategy that breaches its maximum drawdown is flagged for shutdown.

**Analytics:** the Fleet Stats pipeline (cron, nightly) pulls every fill from every account via the Alpaca activities API, FIFO-matches buys to sells per symbol per account (options symbols normalized to their underlying), and rebuilds an Excel dashboard: leaderboard with kill-line headroom, per-session P/L heat map, per-ticker realized P/L per bot. FIFO results were validated to the penny against independently recorded trade notifications.

## Scheduling model

- **Equity bots:** internal loops with market-hours awareness; entry scans aligned to wall-clock boundaries (`:00/:15/:30/:45`) so A/B pairs are time-synchronized. Near the open, closed-market sleeps shorten to avoid oversleeping the first tradeable minutes (a real incident — see lessons-learned).
- **Options bots:** state-machine driven; positions transition only on confirmed fills.
- **Regime calc:** once per trading morning, after the open.
- **Allocator:** nightly, after the close.
- **Fleet stats:** nightly cron at 01:00 UTC.

## Failure modes and answers

| Failure | Answer |
|---|---|
| Bot crash / unhandled exception | systemd auto-restart; position state files are re-read on start |
| Memory leak | `MemoryMax` kills the unit; systemd restarts it clean |
| Stale regime file | Consumers fail safe (skip regime-gated entries) |
| Disk fill from logs | logrotate, compressed, all services enrolled |
| Silent infrastructure failure | routine `systemctl --failed` audits (added after logrotate failed silently for a week) |
| Webhook misconfiguration | mandatory HTTP-204-plus-eyes-on verification at deploy time |

## What I would change at 10x scale

Honest answer: this architecture is right-sized for 15 services on one box. At 100+ strategies I would want containerization, centralized structured logging instead of per-bot files, a real time-series store instead of JSON history files, and a message bus instead of a shared state file. Choosing *not* to build those now is deliberate — the complexity isn't earned yet.
