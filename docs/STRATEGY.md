# Strategy: long-only Donchian breakout with trend filter and ATR stops

vigil implements exactly one strategy. The code is `vigil/strategy.py` (signal),
`vigil/costs.py` + `vigil/sizing.py` (whether a trade is worth taking and how big),
and `vigil/risk.py` (whether trading is allowed at all).

## What it is betting on

**Hypothesis:** when a liquid crypto asset closes above its recent trading range while
it is already in an uptrend, the move continues often enough, and far enough, that
riding it with a trailing stop beats the losses on the breakouts that fail. This is
time-series momentum (trend following). It has a long history in futures markets and
has been studied in crypto, but **vigil does not claim the hypothesis holds for any
particular asset or period.** The only evidence it accepts is a reproducible backtest
on real data that you run yourself (see "Evidence" below), and then paper trading.

**Intended conditions:** liquid spot pairs (e.g. BTCUSDT, ETHUSDT) on 1h–1d bars, during
trending regimes.

**Where it is expected to do badly:**
- Choppy, range-bound markets: frequent false breakouts, each costing about 1R plus fees.
- Sharp reversals and gaps: price can jump through the stop. The exit then fills at
  the next available price, and the loss can exceed 1R.
- Illiquid pairs or wide spreads: costs eat the edge. The cost gate (below) refuses many
  of these trades, which is intended.

Typical profile: win rate well under 50%, with occasional large winners paying for
many small losers. Long losing streaks are normal for this kind of strategy, and the
risk limits are sized with that in mind.

## Rules (evaluated once per closed bar)

Inputs are the last *closed* candles only. The in-progress candle is always dropped.

| Indicator | Definition |
|---|---|
| `entry_channel_high` | highest high of the previous `entry_lookback` bars (the current bar excluded) |
| `exit_channel_low` | lowest low of the previous `exit_lookback` bars |
| `ema` | EMA(close, `trend_ema_period`), seeded with an SMA. Needs about 3× the period of warm-up |
| `atr` | Wilder's ATR(`atr_period`) |

**Entry (flat):** `close > entry_channel_high` **and** `close > ema` **and** `atr > 0`.
Initial stop = `close − stop_atr_mult × atr`.

**Management (long):**
- `stop = max(previous stop, highest close since entry − trail_atr_mult × atr)`. The stop only ever rises.
- **Exit** if `close ≤ stop` (`trailing_stop`) or `close < exit_channel_low` (`channel_exit`).
- **Between bars**, on every loop (default 5 s): exit at once if the best bid is at or below
  the stop (`stop_breached`). This narrows, but does not remove, gap risk.

Long only, one position at a time, spot only (no leverage, no shorting). The signal is a
pure function: the same candles and position always give the same answer (tested).

## Missing, conflicting or unreliable data

The system never fills in, interpolates or guesses data. Every evaluation records exactly
what was wrong (`vigil/marketdata.py`).

| Situation | Treatment |
|---|---|
| Not enough history for the indicators | HOLD (`insufficient_history`) |
| Candle gap, out-of-order bars, non-finite / non-positive prices, high < low, etc. | data invalid → HOLD |
| Feed is missing the most recently closed bar | stale → HOLD |
| Order book missing, empty side, unsorted, crossed, zero size, older than `max_book_age_seconds` | invalid → HOLD; no stop checks with that book |
| Book mid differs from the last close by more than `max_book_vs_close_bps` | **conflict**: no new entries (`data_conflict`), but an open position is still managed, because a fast move is exactly when the stop must work |
| Data still invalid at bar close | retried each loop until `max_bar_lag_seconds` after the close, then the bar is consumed with no trade |
| Bar first evaluated more than `max_bar_lag_seconds` after its close (e.g. started mid-bar) | no entry (`late_bar_entry_blocked`); an open position is still managed. Late signals never become late trades |
| No usable order book for `max_invalid_data_seconds_in_position` while holding a position | **integrity halt**. A person must look; the system will not sell blind |
| Position record incomplete (no stop) | HOLD (`position_context_incomplete`), never guessed |
| Same bar evaluated again (restart, repeated loop) | the idempotency key `ENTER:<symbol>:<interval>:<bar_close>` prevents a second entry |

Signals cannot conflict with each other by construction: when flat, only the entry rule is
evaluated; when long, only the exit rules.

## Costs are part of the decision

For every candidate entry, `vigil/sizing.py` walks the live order book:

1. **Entry cost (bps of mid)** = impact (VWAP of walking the asks for the size, vs mid; this
   includes half the spread and depth) + `extra_slippage_bps` (latency / unseen liquidity) +
   `taker_fee_bps`.
2. **Exit cost (bps)**: the same, walking the bids. This assumes similar liquidity at exit,
   which is optimistic in a crash; `extra_slippage_bps` is the margin for that.
3. Both are estimated at the *largest size the caps allow*. Costs only grow with size, so
   the estimate is conservative for the final, smaller-or-equal size.
4. **Spread gate:** skip if spread > `max_spread_bps`.
5. **Cost hurdle:** `cost_to_risk = (entry cost + exit cost per unit) / (mid − stop)`. If
   above `max_cost_to_risk` (default 0.25), skip. The trade would need to earn that much
   R just to break even, and a breakout system with a sub-50% win rate cannot afford a
   large hurdle.

Orders are **marketable limit IOC**: the limit is `best ask × (1 + limit_price_buffer_bps)`
for buys (best bid × (1 − buffer) for sells), rounded away from the market to the tick.
The buffer is the worst fill price the system will accept. Anything that cannot fill
immediately within it expires; nothing rests on the book.

## Position sizing

```
worst_loss_per_unit = (limit_price − stop)          # entry could fill as badly as the limit
                    + mid  × entry_cost_bps / 1e4
                    + stop × exit_cost_bps  / 1e4
risk_budget         = min(equity × risk_per_trade_fraction, remaining daily loss allowance)
qty = floor_to_step( min( risk_budget / worst_loss_per_unit,
                          max_position_notional / limit_price,
                          equity × max_position_fraction / limit_price,
                          quote_free / (1 + fee) / limit_price,
                          depth fillable within the limit price ) )
```
The trade is rejected if `qty` is below the exchange's minimum quantity or notional.
Every intermediate number is written to the audit record's `sizing.details`.

Equity = quote balance + position quantity × best bid (marked at the price you could sell at).

## Limits and conditions that stop trading

| Limit | Default | Effect |
|---|---|---|
| `risk_per_trade_fraction` | 0.5% | worst-case loss at the stop, costs included |
| `max_position_notional` / `max_position_fraction` | 1000 / 25% | hard size caps |
| One position at a time, long only | always | |
| `max_daily_loss` (mark-to-market since the UTC day start) | 50 | blocks **entries** until the next UTC day; exits continue |
| `max_drawdown_fraction` from peak equity | 10% | **risk halt**: exits allowed, no entries, manual reset required |
| `max_orders_per_day` | 6 | an entry needs room for its exit too |
| `max_consecutive_errors` | 3 | **integrity halt** |
| `cooldown_bars_after_exit` | 1 | no immediate re-entry |
| Reconciliation failure, unresolved order, no data while holding, unexpected exception | always | **integrity halt**: nothing sent to the exchange, process stops |

## Evidence

vigil makes **no profitability claim**. `python -m vigil backtest` runs the same signal,
cost and sizing code on a CSV of real candles. Fills happen at the next bar's open, stops
are checked intrabar with gaps honored, and costs come from the same model. It records the
CSV's SHA-256 and the config fingerprint, so anyone can reproduce the numbers exactly.

The test suite runs the backtest on a *synthetic* random walk to check the machinery.
Those numbers are **not evidence of anything** about real markets.

Before relying on the strategy:
1. Fetch several years of real data (`fetch-candles`) for the pair and interval.
2. Backtest with costs at your real fee tier, plus pessimistic spread and slippage.
3. Check that results survive higher costs, nearby parameter values and sub-periods (a
   result that only works at one setting is overfit).
4. Paper trade, then testnet, before considering live (see OPERATIONS.md).
