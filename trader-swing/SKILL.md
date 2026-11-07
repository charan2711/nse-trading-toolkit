---
name: trader-swing
description: >-
  Swing Trader for NSE stocks held for days to a few weeks as delivery (CNC).
  After the close it checks the market (Nifty 50 against its 50- and 200-day
  averages), scans for three setups (a pullback to the 20-day EMA in an
  uptrend, a volume breakout from a 30-day base, and the multi-timeframe
  RSI-60 momentum setup) plus a watch list of stocks coiling just under a
  breakout, sizes a trade to 1% of your swing capital with Zerodha delivery
  charges counted, and writes a CNC limit buy plus a Kite GTT OCO order for
  the stop and target. A momentum mode finds the day's bursts (up 5%+ on 2x
  volume, closing near the high) for a 5-day hold, live at 3:15 PM from Kite
  quotes. It also backtests the exact rules on years of Kite
  daily candles with costs and an out-of-sample split, and manages open
  positions (breakeven stop, time stop, RSI exit). Use when the user says
  "swing trade", "positional trade", "which stocks are breaking out", "stocks
  near breakout", "about to break out", "swing scan", "RSI 60 strategy",
  "momentum stocks", "stocks up 5% today", "momentum burst", "hold a few days",
  "backtest my swing setup", "GTT stop loss", "manage my swing positions".
argument-hint: "[scan | plan SYMBOL | backtest | study | manage]"
---

# trader-swing

Swing trading holds a stock for days or weeks instead of minutes. Fewer
decisions, no square-off clock, but every night is a gap risk: a stock can
open below your stop and the loss is then bigger than planned. This skill
finds setups after the close, plans the trade with the stop decided first,
and tests the rules on history before you trust them. A setup matching a rule
is not a prediction.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/swing.py" scan --data kite-daily/ --write      # after 15:30 -> trader/swing-watchlist.json
python3 "${CLAUDE_SKILL_DIR}/swing.py" scan --symbols RELIANCE,TCS,TITAN    # Yahoo .NS instead of saved Kite files
python3 "${CLAUDE_SKILL_DIR}/swing.py" plan TITAN --data kite-daily/ --write # CNC buy + GTT -> trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/swing.py" backtest --data kite-daily/          # all setups, with costs
python3 "${CLAUDE_SKILL_DIR}/swing.py" backtest --data kite-daily/ --setups breakout --trades trades.csv
python3 "${CLAUDE_SKILL_DIR}/swing.py" manage --data kite-daily/            # trader/swing-positions.json
python3 "${CLAUDE_SKILL_DIR}/swing.py" scan --data kite-daily/ --setups momentum --quotes quotes.json  # 3:15 PM
python3 "${CLAUDE_SKILL_DIR}/swing.py" study --data kite-daily/ --entry close   # what holding 1-10 days returned
python3 "${CLAUDE_SKILL_DIR}/swing.py" backtest --data kite-daily/ --setups momentum --entry close
```

Other flags: `--asof YYYY-MM-DD` (scan or plan as of a past close),
`--from` / `--to` / `--split` (backtest dates), `--capital`, `--json`.

## Data

The best source is Kite. For each stock, call the connector's
`get_historical_data` with interval `day` from about 5 years back to today
and save the result as `kite-daily/SYMBOL.json`; save the Nifty 50 index
(instrument token 256265) as `kite-daily/NIFTY_50.json`. Kite's daily
candles are adjusted for splits and bonuses. Saved files mean the scan and
the backtest need no login; Kite asks you to log in once a day, by exchange
rules, only for fresh data or orders. Without Kite, `--symbols` reads free
Yahoo daily bars (unofficial, can lag).

## The rules

| | rule |
| --- | --- |
| market | Nifty above its 200-day and the 50-day above the 200-day: full size. Above the 200-day only: half size. Below it: no new trades |
| stock | close > 50-day > 200-day average, within 25% of the 52-week high, 50-day average traded value at least ₹10 crore, price at least ₹50 |
| pullback | a dip to the 20-day EMA on falling volume in the last 5 days, then a close above the day before's high and the EMA. Stop: the dip's low |
| breakout | a 30-day base at most 15% deep; the first close above its high on 1.5x the 50-day volume, in the top 40% of the day. Stop: the 10-day low or 2 ATR, the closer |
| rsi60 | monthly and weekly RSI(14) above 60, daily above 60, the first close above the 10-day swing high. Stop: last week's low. Exit: a daily close with RSI under 60 |
| momentum | up 5%+ on 2x the 50-day volume, closing in the top 30% of the day, the first such day, not over 60% above the 50-day average; liquid (₹10 crore a day); no trend filter. Stop: the burst day's low, at most 10% away. Exit: the close of day 5 |
| near breakout | watch list only: a tight base (12% deep at most), the close within 3% under its high, volume drying up, RSI 50 to 70. Gives the trigger (base high) and a stop |
| entry | a LIMIT buy 0.5% above the signal close (a gap up is not chased); the backtest uses the next open |
| size | 1% of `swing_capital` at risk (charges included), at most 25% of capital in one stock, 5 positions, 6% open risk |
| exits | GTT OCO: stop and 2R target. Stop to the entry once a close is 1R up. Time stop: 10 trading days (40 for rsi60) |
| costs | Zerodha delivery: no brokerage, STT 0.1% each side, NSE 0.00307%, SEBI, stamp 0.015% on the buy, GST, DP ₹15.34 per sell (read 2026-11-07) |

Every number lives in DEFAULTS in `swing.py` and can be changed under
`"swing"` in `trader/config.json` (see `trader-budget/config.example.json`).

## What the backtest said (read this before trading it)

36 of today's large and mid caps, Kite daily candles 2021-11-01 to
2026-11-06 (trades start Sept 2022 after the 200-day warm-up), ₹2,00,000
start, all rules as above:

| | trades | win | avg R | profit factor |
| --- | --- | --- | --- | --- |
| all setups | 400 | 37.0% | -0.05R | 0.88 |
| pullback | 272 | 36.8% | -0.10R | 0.79 |
| breakout | 62 | 43.5% | +0.04R | 1.11 |
| rsi60 | 66 | 31.8% | +0.06R | 1.17 |

Overall it lost 16% while the Nifty rose 40%; the later period (from May
2025) was worse than the earlier one. Of 463 near-breakout flags, 42% closed
above the trigger within 10 sessions and 56% touched the stop level first.
The honest reading: on this data, none of these textbook rules has an edge
after costs; breakout and rsi60 are about break-even. Use the backtest to
test changes on your own list of stocks, keep it on paper, and treat any
claim of an "85% win rate" setup with suspicion. The stock list is today's
winners (survivorship), so real results would likely be worse.

## Momentum bursts: what holding them returned

75 stocks (the 36 large caps above plus 39 volatile mid caps: PSU, defence,
power, metals, brokers), Kite daily candles 2021-11 to 2026-11, 719 bursts.
Bought at the burst day's close, after Zerodha delivery charges:

| held | avg | median | win | baseline (any day, same stocks) |
| --- | --- | --- | --- | --- |
| 1 day | +0.09% | -0.16% | 48% | +0.09% |
| 3 days | +0.40% | -0.03% | 50% | +0.27% |
| 5 days | +1.01% | +0.26% | 52% | +0.44% |
| 10 days | +1.64% | +0.34% | 53% | +0.86% |

- "It continues the next day" is a coin flip: the next day averages the same
  as any day. The extra return shows up over about 5 days, and it is small:
  roughly +0.5% a trade over the baseline, with a median near zero (a few big
  winners carry it).
- Buying at the next open instead is worse: the open gaps up 0.5% on average,
  and 1 day held averages -0.40%. Buy in the last minutes of the burst day
  (scan at 3:15 PM with `--quotes`).
- As a full system (1% risk, 5 positions, stop at the burst low, sold on day
  5, bought at the close): +61% over 5 years with a 17% max drawdown, against
  +40% for the Nifty. But almost all of it came in 2023 and 2024 (the PSU and
  small-cap rally): 2022 was -0.04R a trade, 2025 -0.13R and 2026 -0.25R.
  From May 2025 it lost (84 trades, -0.14R, profit factor 0.64).
- With the market filter on, 5-day bursts averaged +1.1%; in a bear market
  +0.1%. The list is today's survivors (IRFC, BSE, Mazagon Dock), so the real
  edge is likely smaller.

A real case: Antelopus Selan Energy's +12% to +20% days in September and
October 2026. The first burst (2 Sep, 91x volume) gained 23% in 5 days; the
later ones, with the stock already 30% to 40% above its 50-day average,
lost 12% to 13% at the stop. This rule skips all of them: on 10% to 20%
days the day's low is more than 10% away, so the risk is too big.

## Steps

1. After 15:30 IST: refresh the Kite files (or use `--symbols`), then
   `swing.py scan --data kite-daily/ --write`. Tell the user the market
   regime, any setups (with the stop and risk %), and the near-breakout list
   with each trigger.
2. For any setup the user picks: `/trader-news SYMBOL` and the results date
   first. A results announcement inside the holding period is a gap risk.
3. `swing.py plan SYMBOL --data kite-daily/ --write`. Read back the buy, the
   stop, the target, the loss at the stop with charges, and the GTT legs.
4. `/trader-risk` checks it (the swing limit is `risk_per_trade_pct` of
   `swing_capital`, not the intraday ₹500), then `/trader-broker` asks the
   user yes or no for the buy.
5. After the buy fills, the GTT is a second order: show it and get a separate
   yes before calling `place_gtt_order`. In paper mode nothing is sent and
   `trader/swing-positions.json` tracks it.
6. Momentum: at 3:15 PM save Kite `get_quotes` for the watch list to
   `quotes.json` and run `scan --setups momentum --quotes quotes.json`. A
   burst can be bought before 3:30 PM as a CNC limit; the stop goes in as a
   GTT, and `manage` says sell on day 5.
7. Each evening: `swing.py manage --data kite-daily/`. It says hold, move the
   stop to the entry (modify the GTT), exit on the time stop or the rsi60
   rule, or check a fill. Every change goes through the user's yes.

## Rules

- Say "matched a rule", never "a buy" or "a winner".
- Always show the backtest numbers with a strategy, including the bad ones.
- A GTT places a LIMIT order when triggered; a gap through it may not fill.
  Say so, and check positions every morning.
- Never average down, never widen a stop, never skip the market filter.

## What it never does

It never places, modifies or cancels an order or a GTT on its own, never
promises a win rate, and never trades F&O, MTF (margin) or short.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tools are set to "Needs approval"
  (Customize > Connectors > tool permissions). The GTT is its own order and needs its own yes.
- Delivery gains held under 12 months are short-term capital gains; nothing here is tax advice.
- Kite keys live in the MCP server's own config, never in the chat.
