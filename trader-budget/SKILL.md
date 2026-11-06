---
name: trader-budget
description: >-
  Budget Manager for NSE / BSE day trades in rupees. Sizes every position to
  your budget and your max loss per trade, with Zerodha's charges counted in
  the loss. Reads the trade plan from trader-exit (entry, stop, target), finds
  the whole shares that fit inside four limits (max loss including
  brokerage, STT, exchange, SEBI, stamp duty and GST; position cap; max
  order; cash after your buffer, with no MIS leverage unless you turn it on),
  says which limit decided it, shows what you make at the target after
  charges, and writes ONE Kite cover order (variety co, product MIS, the
  stop-loss in trigger_price) to trader/orders.json in the exact shape of the
  Kite connector's place_order. A stop too far away for your max loss is a
  plain refusal, not a smaller order; so is a trade whose target cannot pay
  for its charges. Nothing is placed. Use when the user says "how many
  shares", "size this trade", "how much should I buy", "I have 1 lakh", "I
  only want to risk 500 a trade", "set my rules", "keep 10% in cash", "what
  are the charges", or after /trader-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# trader-budget

The Budget Manager turns a plan ("buy RELIANCE at 1311.90, stop 1300.70")
into an exact order ("buy 39 shares"), inside rules the user sets once. The
stop decides the size: the further away the stop, the fewer shares. It writes
a proposal only. trader-risk checks it and trader-broker asks the user yes or
no. Intraday trading loses most people money. Sizing to a max loss keeps each
loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                         # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                 # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write         # a limit order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1311.90 --json  # a limit order at 1311.90
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights RELIANCE=40,TCS=60 --budget 200000 --config split.json
```

`--now "2026-11-06 09:51"` pins the clock (IST). A plan for another day is
refused: make a new one with trader-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, setup, flat-by time (trader-exit writes it) |
| `trader/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (trader-alerts writes it) |
| `trader/portfolio.json` | optional: its cash caps the size (trader-broker keeps it) |
| `trader/orders.json` | the one proposed cover order it writes with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each value in plain words:

- **budget**: how much money this is for, in rupees. Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills at the stop, charges
  included (default ₹500).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest position (75% of the
  budget), the biggest order (₹80,000) and the smallest order (₹500).
- The day limits trader-risk enforces: **max_daily_loss_inr** (₹1,500), **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:30" / "14:45") and **flat_by** ("15:05"), all IST.
- **use_mis_leverage** (false): Zerodha lends intraday (MIS) margin, often 5x. Leave it off: the cash
  limit is then the full value of the shares. Turned on, the cash limit uses `mis_margin_pct` (20) of
  the value; the max-loss limit does not change, so leverage never raises the loss at the stop.
- **blocked**: tickers they never want to trade.
- Keep **paper** true until they have watched it work for a while.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only (NSE has no fractions). The size is the smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | the most shares whose qty x (entry minus stop) plus the round trip's charges fits `max_loss_per_trade_inr` |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) / entry (or / the MIS margin a share, with leverage on), never more than the cash in `trader/portfolio.json` |

**Charges** (Zerodha equity intraday, read from zerodha.com/charges on
2026-11-06; override any of them with `"charges"` in the config): brokerage
0.03% or ₹20 per executed order, whichever is lower; STT 0.025% on the sell
(rounded to the rupee); NSE transaction charges 0.00297% (BSE 0.00375%); SEBI
₹10 a crore; stamp duty 0.003% on the buy; GST 18% on brokerage, transaction
and SEBI charges. They are an estimate: the contract note is the bill. If
Zerodha squares the position off for you it adds ₹50 + GST per order, which
is one more reason to be out by flat_by.

The entry it sizes at: the limit price with `--limit`. For a market order, the
plan's entry, or the price in `trader/prices.json` if it is fresh (under 15
minutes old) and higher. A price that ran up since the plan means a bigger
loss at the same stop, so fewer shares. A lower price never shrinks the risk.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk is under `min_reward_risk`, the target cannot
pay for the charges, the stop is not below the entry, the target is not above
it (the move already happened), the plan is for another day, the ticker is
blocked, or the plan is a short. Say the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees, the
max loss at the stop with the charges itemised, and what is left at the
target after charges. Say plainly that a stop is an order, not a promise: in
a fast market or a gap it can fill below the trigger. Then hand over to
`/trader-risk`.

## Example: RELIANCE, Friday 2026-11-06

Opening-range breakout: entry 1311.90 at 9:50 AM, stop 1300.70 (the
opening-range midpoint), target 1323.10 (1R). Risk 11.20 a share. A ₹1,00,000
budget, 10% cash buffer, ₹500 max loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | ₹500 / (11.20 + charges) | **39 (decides it)** |
| position cap | 75% of ₹1,00,000 = ₹75,000 / 1311.90 | 57 |
| max order | ₹80,000 / 1311.90 | 60 |
| cash | ₹1,00,000 minus ₹10,000 = ₹90,000 / 1311.90 | 68 |

39 shares = ₹51,164.10. At the stop: 39 x 11.20 = ₹436.80, plus ₹54.77 of
charges (brokerage ₹30.57, STT ₹13, exchange ₹3.03, SEBI ₹0.10, stamp ₹2, GST
₹6.07) = ₹491.57. At the target: ₹381.70 after charges. It writes:

```json
{"variety": "co", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
 "quantity": 39, "product": "MIS", "order_type": "MARKET", "validity": "DAY",
 "trigger_price": 1300.7, "tag": "daytrader", "target_price": 1323.1,
 "why": "opening-range breakout on RELIANCE: 39 shares, bound by your ₹500 max loss per trade ...",
 "plan": {"entry": 1311.9, "stop": 1300.7, "target": 1323.1, "risk_per_share": 11.2,
          "loss_at_stop": 436.8, "charges": 54.77, "max_loss": 491.57, "gain_at_target": 381.7, ...}}
```

`target_price`, `why` and `plan` are notes for trader-risk, trader-broker and
trader-alerts. They are never sent to Kite (Kite has no take-profit leg on a
cover order). With a ₹5,000 budget the position cap decides it instead: 2
shares, ₹25.45 at the stop.

## Weights mode (kept from v1)

`--weights RELIANCE=30,HDFCBANK=25` or `--targets FILE` (`targets.example.json`
here) splits a budget across a list by weight: the cash buffer comes off the
top, weights are renormalised to 100, any stock over `max_position_pct` is
pinned at the cap and its excess shared out, slices under the minimum order
are dropped. Whole shares only, so it needs prices (`--prices
trader/prices.json`, from trader-alerts or Kite quotes). It writes delivery
(CNC) market buys with no stop: trader-risk blocks them while `require_stop`
is true and counts each one against `max_trades_per_day`. Use a separate
config (`--config`) for a long-term split.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Yahoo prices are free, unofficial and can lag. The broker's price is the one that fills.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes options,
futures, margin (MTF) or short positions.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite keys live in the MCP server's own config, never in the chat.
