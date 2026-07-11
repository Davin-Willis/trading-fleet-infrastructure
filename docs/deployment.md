# Bot Deployment Checklist

The real process used in production. Every step exists because skipping it caused an incident at least once.

## Per-bot provisioning

1. **Create a dedicated Alpaca paper account.** One account per strategy — shared accounts make per-strategy P/L unrecoverable.
2. **Create the bot's Discord channel and webhook.** One channel per bot.
3. **Create the bot folder** (`/home/ubuntu/bot_<name>/`) with its script and `.env`.
4. **Strip CRLF from anything that touched Windows:**
   ```bash
   sed -i 's/\r$//' /home/ubuntu/bot_<name>/.env
   ```
5. **Verify the webhook before first run** — curl must return HTTP 204 AND the message must be visually confirmed in the *correct* channel:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Content-Type: application/json" \
     -d '{"content":"<bot> webhook test"}' "$WEBHOOK"
   ```
6. **Lock down the env file:** `chmod 600 .env`

## Service supervision

7. **Install the systemd unit** (see `deploy/systemd/` for real examples): auto-restart, `MemoryMax` cap, log redirection to the bot's folder.
8. **Enable and start:** `sudo systemctl enable --now <bot>.service`
9. **Watch the first startup:** `journalctl -u <bot> -f` until the first full cycle completes cleanly.

## Fleet integration

10. **Enroll the log in logrotate** (`deploy/logrotate/tradingbots` shared block).
11. **Register the account in the allocator** (env prefix + kill-line thresholds).
12. **Confirm the nightly digest** lists the new bot's equity the next morning.

## Verification standard

A bot is *deployed* when all of the following are true:
- systemd shows `active (running)` and survives a `systemctl restart`
- the webhook test message appeared in the correct channel
- the first scheduled cycle logged cleanly
- logrotate `--debug` run includes the new log
- the allocator digest includes the new account

Anything less is *running*, not *deployed*.
