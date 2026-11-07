---
name: trader-risk
description: >-
  Risk Manager for NSE / BSE day trades. Checks every order against your
  limits and stops anything over them, before it can reach Kite. For a day
  trade: a stop is required (a cover order's trigger), the loss at the stop
  PLUS Zerodha's charges (brokerage, STT, exchange, SEBI, stamp duty, GST)
  must fit your max loss per trade in rupees (measured from the higher of the
  plan's entry and a fresh price), reward:risk must meet your minimum, the
  day's total risk must fit your daily loss limit, no more than your trades
  per day, and no new entries outside your IST entry window or when the NSE
  is closed (weekends, NSE holidays, Muhurat hours). It also checks the Kite
  order format (variety, product MIS / CNC, order type, validity, the NSE
  tick for the price band), blocks F&O / commodity exchanges, MTF margin and
  short sales unless allowed, blocked tickers, order size, position cap, cash
  buffer, duplicates and a daily order limit, and warns near the upper
  circuit or past Zerodha's square-off time. Getting out is never blocked by a
  day limit. Prints PASS or BLOCK with the reason and records the verdicts so
  trader-broker only shows an order that passed. Use when the user says
  "check my order", "is this safe", "risk check", "does this break my rules",
  "can I take this trade", "how much have I risked today", or before
  /trader-broker, always.
argument-hint: "[path to orders.json]"
---

# trader-risk

The Risk Manager is the gate between a proposal and Kite. Every order written
by trader-budget or trader-broker's flatten passes through it. A BLOCK is
final for that order: it does not go to the user for a yes, and it does not
go to the broker. Most people who day trade lose money. These limits keep
each loss and each day's losses small. They do not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-11-06 09:51"  # pin the clock (IST)
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget, or trader-broker's flatten), Kite place_order arguments |
| `trader/config.json` | the limits (trader-budget ships `config.example.json`) |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | a fresh price raises the entry it measures risk from; circuit limits from Kite quotes |
| `trader/state/broker.json` | what was placed today: duplicates, trades per day, daily loss |
| `trader/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

## Entries and exits

An **entry** opens or adds to a position: a buy, or a sell that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, asset and short sales
only. Every other rule (day limits, entry window, loss, reward:risk, blocked
list, size, duplicates) turns into a warning for an exit, never a BLOCK.
The flatten ticket that exits a cover order is an exit.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no tradingsymbol; transaction_type not BUY / SELL; quantity not a whole number above 0; an order_type, variety or validity Kite does not take (or the pack does not use: amo, iceberg, auction); a LIMIT without a price, an SL without price and trigger, an SL-M without a trigger; a cover order without trigger_price, not MIS, or not MARKET / LIMIT; a price or trigger off the NSE tick for its band |
| asset | an exchange other than NSE / BSE (NFO, BFO, MCX) while `allow_options` is false |
| margin | product MTF while `allow_margin` is false |
| short | a sell of more than you hold (`allow_short` false); an intraday MIS short is still a short |
| blocked | (entries) the ticker is on the `blocked` list |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy would leave less than the cash buffer, or need margin |
| stop | (entries) no stop while `require_stop` is true (a cover order's `trigger_price`, or the order's plan stop); a stop on the wrong side of the entry |
| loss | (entries) qty x (entry minus stop) plus the round trip's charges over `max_loss_per_trade_inr` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| daily loss | (entries) the max losses at the stops of today's entries, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` (09:30) or after `no_entries_after` (14:45), or the NSE is closed |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a limit order. For a
  market order, the HIGHER of the plan's entry and a fresh price from
  `trader/prices.json` (under 15 minutes old). A plan written at 9:51 cannot
  understate the risk at 10:30 after the price ran up. An older price is not
  used, and it says so.
- **Charges** are Zerodha's equity intraday schedule (the same sum trader-budget
  uses; override any part with `"charges"` in the config). On ₹50,000 of
  RELIANCE they come to about ₹55 for the round trip, and they count against
  your max loss, because they are lost win or lose.
- **Daily loss** counts the full max loss of every entry placed today, win or
  lose, as trader-broker logged it.
- **The window** is IST, worked out in the tool (UTC+5:30, no daylight
  saving), never from the computer's clock zone. Weekends and NSE holidays
  are closed; Muhurat trading (2026-11-08, 2:20 to 3:30 PM) is open, and its
  own hours apply. Set both `no_entries_*` to null to switch the window off.
- **No day-trade count rule.** SEBI's margin rules and Zerodha's MIS list decide what you can trade intraday.

Warnings do not block, but say them to the user: a LIVE account, a stop that
lives only in the plan and not at the exchange, a price too old to use, a
special session, a price within 1% of the upper circuit (a locked stock can
be impossible to sell), a flat_by after 3:10 PM (Zerodha's intraday square-off
starts at 3:12 PM in F&O stocks).

## Steps

1. If the price may have moved since the plan, refresh it first: Kite's
   `get_quotes` saved to a file, then `alerts.py quote --kite FILE --write`
   (trader-alerts), which also stores the circuit limits.
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop with charges, reward:risk, and how much of today's limits it
   uses. For a BLOCK, give the reason and the fix (fewer shares, a closer stop
   from trader-exit, wait for the window, or skip the trade). Never change a
   limit yourself.
4. If the order changes in any way, run the check again. trader-broker matches
   each order to its verdict by fingerprint, so an edited order is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example: RELIANCE, Friday 2026-11-06

The cover order from trader-budget at 9:51 AM, on a fresh ₹1,00,000 paper
account with a ₹500 max loss per trade:

```
PASS  #1 BUY RELIANCE 39 shares, MIS cover order, entry
          entry 1311.90 (the plan's entry), stop 1300.70, target 1323.10: risk 11.20 a share
          max loss ₹491.68 with ₹54.88 of charges (limit ₹500), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, ₹0 of ₹1,500 daily loss

1 passed, 0 blocked at 2026-11-06 09:51 IST. Cash after the passed orders: ₹48,835.90 (paper trading).
```

The same order at 2:50 PM is blocked by the window (no new entries after
14:45). The 3:00 PM exit of the cover order passes: it is an exit.

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices or the broker log.
- Yahoo prices are free, unofficial and can lag; Kite's are your broker's. The broker's price is the one
  that fills, and a stop can fill worse than its trigger in a fast market or a gap.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite keys live in the MCP server's own config, never in the chat.
