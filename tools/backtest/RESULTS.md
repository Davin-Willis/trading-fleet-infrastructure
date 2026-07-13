# Backtest Results — and why they don't drive the live fleet

In July 2026 I backtested every strategy in the fleet that could be tested
honestly: 11 of 15. The results were humbling, which is what honest testing
looks like. Per fleet policy, these results are **informational only** — the
live fleet was not changed in response to them. This document explains the
numbers, the method, and the reasoning behind not acting on them.

## Method

- Each backtest reimplements the **exact production rules** read from the
  live bot code — same parameters, same thresholds, same universes.
- Rules were written down **before** running anything (pre-registration), so
  they couldn't be tuned to fit history afterward.
- Costs are modeled explicitly and conservatively: 5 bps/side for liquid
  ETFs, 10 bps/side for leveraged ETFs, 60 bps round trip for crypto
  (Alpaca taker fee + measured half-spread).
- Intraday fills use a **pessimistic tie-break**: if one bar's range contains
  both the stop and the target, the stop is assumed to fill first.
- Data: Alpaca IEX feed (a subset of consolidated volume — fine for strategy
  character, not penny precision). Scripts in this folder.

## Results (5–5.6 years ending July 2026)

| Strategy | Total | CAGR | MaxDD | Trades/yr |
|---|---|---|---|---|
| **SPY buy & hold (benchmark)** | **+104.1%** | **+13.7%** | −25.4% | 0 |
| Defensive Rotation | +34.6% | +5.5% | −25.2% | 12 |
| Momentum Rotation | +30.1% | +4.8% | −30.4% | 18 |
| Pairs Rel Value | −6.1% | −1.1% | −9.0% | 9 |
| ORB Breakout (15-min) | −22.3% | −4.9% | −24.4% | 232 |
| VWAP Mean Reversion (15-min) | −58.7% | −16.3% | −58.8% | 285 |
| Overnight Drift | −60.1% | −15.2% | −63.0% | 504 |
| Gap Fade/Ride (approx.) | −68.1% | −18.6% | −69.3% | 181 |
| Mean Reversion (15-min) | −82.1% | −29.3% | −82.2% | 525 |
| Crypto Trend BTC (daily) | +14.2% | +2.6% | −50.6% | 34 |
| Crypto Trend ETH (daily) | +90.8% | +13.3% | −45.3% | 28 |

Signal-level analysis of the trend-following daily signal (12,590 signals,
22 tickers, 5 years): 51.2% directional win rate, mean 5-day forward move
0.00%. The bullish half shows a small real edge (+0.13%/5d) — an order of
magnitude too small to pay for the weekly options the live bots buy.

The four options strategies (trend ×2, credit spreads, VRP) cannot be
backtested honestly without historical options chains, which are paid data.
Their only real test is forward.

## What was learned

1. **Nothing beat the benchmark.** Ten active strategies over five years;
   passive SPY won. This is the finding most published strategies avoid
   confronting, and it's why the benchmark bot exists.
2. **Trade frequency × cost is destiny.** Every strategy above ~180
   trades/yr is deeply negative; every survivor trades under 20 times/yr.
3. **The live leaderboard is weather, not climate.** The fleet's best live
   performer (Mean Reversion) has the worst 5-year history of everything
   tested. Weeks of live results are statistical noise; the backtests exist
   to read forward results against historical character.
4. **Crypto trend-following earned its slots on drawdown, not return.** BTC
   trend underperformed holding; ETH trend massively outperformed. Both cut
   historical max drawdown by ~30 points vs holding. That asymmetry (and
   the measured 45–50% full-allocation drawdowns) set the live bots'
   half-sizing and 25% kill-lines before deployment.

## Why the fleet didn't change

Acting on these results — killing the historically-bad strategies, promoting
the historically-good ones — would replace a controlled forward experiment
with curve-fitting to the one history that happened. The strategies most
likely to get killed are also the cheapest source of data on *when and how*
their families fail. Kill-lines bound the cost of that data. The
pre-commitment (results are informational; forward performance decides) was
stated before the first backtest ran, and it held.
