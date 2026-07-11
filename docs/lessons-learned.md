# Lessons Learned

Real incidents from operating the fleet, what caused them, and what changed. These are written up because the debugging was more educational than the building.

## 1. The indicator that froze only sometimes

**Symptom:** A bot's moving-average values stopped changing mid-session. Same code, other bots fine. Restart fixed it — until it didn't.

**Investigation:** The bars request used `start=date.today()` with a small `limit`. The data provider returns bars **oldest-first**, so on days with many bars, a small limit returned the *morning's* bars — and the "latest close" the bot computed was hours stale. The bug only manifested when the session had produced more bars than the limit, which is why it looked intermittent.

**Fix (now the standard pattern in every bot):**

```python
# WRONG: small limit + today anchor = oldest bars on busy days
bars = get_bars(symbol, start=date.today(), limit=50)

# RIGHT: wide window, large limit, slice the newest N locally
start = datetime.now(timezone.utc) - timedelta(days=5)
bars = get_bars(symbol, start=start, limit=10_000)
closes = [b.close for b in bars][-need:]
```

**Meta-lesson:** an API's sort order is part of its contract. Read it, don't assume it.

## 2. CRLF is a production hazard, not a style nit

**Symptom:** A freshly deployed bot's Discord webhook returned HTTP 400. The URL was verifiably correct — copy-pasted from a working bot.

**Investigation:** The `.env` had been edited on Windows and transferred via SFTP. Every line ended in `\r\n`. The invisible `\r` became part of the webhook URL value. Same class of failure later hit API keys (auth failures with "correct" keys) and even a Python file (syntax error on a line that looked fine).

**Fix:** a mandatory deployment step for anything that transits a Windows machine:

```bash
sed -i 's/\r$//' <file>
```

**Meta-lesson:** if a value is "definitely correct" but the system disagrees, look for what you can't see. `cat -A` shows line endings.

## 3. Logrotate failed nightly for a week and nothing looked wrong

**Symptom:** none. That's the point. A routine audit found `logrotate.service` in a failed state, and log files growing unrotated.

**Investigation:** an editor backup file (`tradingbots.save`) had been left in `/etc/logrotate.d/`. Logrotate parses *every* file in that directory; the `.save` file's duplicate stanzas made the whole run abort — every night, silently, for a week.

**Fix:** deleted the stray file; consolidated per-service stanzas into one shared block so future edits touch one place. Added "check `systemctl --failed`" to the routine.

**Meta-lesson:** infrastructure fails silently by default. If you don't audit, "no alerts" and "nothing is wrong" feel identical.

## 4. An order submitted is not a position opened

**Symptom:** an options bot's state file said a position was open; the account said otherwise. The order had been accepted and then not filled.

**Fix:** a proper position state machine — `pending_entry → open → pending_close → closed` — where transitions only happen on **confirmed fills** via `get_order_by_id()`, recording the actual `filled_avg_price`, never the intended price.

**Meta-lesson:** in any system that talks to an external execution venue, your state is a *hypothesis* until confirmed. Design the state machine around confirmations, not intentions.

## 5. Two "identical" strategies weren't comparable

**Symptom:** an A/B pair (a strategy and its regime-gated clone) diverged by amounts far too large for the single variable that separated them.

**Investigation:** each bot scanned for entries every N minutes *from whenever it happened to start*. The two bots' scan clocks were phase-offset, so they saw different prices and took different trades for reasons that had nothing to do with the experiment variable.

**Fix:** entry scans aligned to wall-clock boundaries (`:00/:15/:30/:45`) in both bots, deployed in the same minute. Post-fix, the pair moves in lockstep and the residual difference is attributable to the regime gate.

**Meta-lesson:** a controlled experiment is only as controlled as its most overlooked variable. Timing is a variable.

## 6. Categorize every inflow or your data lies

(From the companion finance-automation pipeline.) Transaction feeds mix spending, income, refunds, internal transfers, and card payments. Any inflow without an explicit category home ends up polluting either income or spending totals — the first version double-counted every credit card payment (once at purchase, once at payment), overstating spending by ~40%.

**Fix:** every transaction class gets an explicit category and the aggregation formulas exclude non-spending categories by name.

**Meta-lesson:** in data pipelines, "unclassified" is not a neutral state — it silently defaults into *some* bucket, and that bucket's totals are now wrong.

## 7. Deployment checklists exist because memory doesn't scale

Thirteen bots × (account, keys, webhook, service unit, memory cap, logrotate enrollment, allocator registration) is too many multiplied details to hold in a head. The one deployment that skipped the checklist produced a bot posting its alerts to another bot's Discord channel. The checklist is in [`deployment.md`](deployment.md); every line on it is there because of a specific incident.
