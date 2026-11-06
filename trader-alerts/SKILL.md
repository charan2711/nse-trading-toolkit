---
name: trader-alerts
description: >-
  Price Alerts for NSE / BSE trades. Rings when a price, a stop or the
  opening bell hits. Turns the trade plan into three alerts in one go (the
  stop, the target and "flat by" at your flat-by time, 15:05 IST by default,
  ahead of Zerodha's MIS square-off), plus above / below / stop / target
  levels, percent moves from the previous close, any time of day, and the
  NSE opening bell at 9:15, each ringing once so you are not spammed. Knows
  NSE hours with the 2026 holidays, the closing auction for F&O stocks and
  the Muhurat session, works out IST itself, reads Kite get_quotes output
  (with circuit limits) or fetches free delayed Yahoo .NS quotes, and writes
  trader/prices.json for sizing and the risk check. Use when the user says
  "alert me when", "tell me if RELIANCE hits", "watch my stop", "watch my
  trade", "remind me to close by 3", "is the market open", "when does the
  market open", "get me a price", or right after /trader-exit writes a plan.
argument-hint: "[symbol] [price]"
---

# trader-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. With a Kite cover order the stop-loss already
sits at the exchange and does that work. Kite cover orders have no
take-profit leg, so the target alert is the one that matters: when it rings,
the user decides whether to exit (trader-broker `flatten`, trader-risk, and
their yes). The flat-by alert tells them it is time to be out.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1300.70
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --target 1323.10
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:05 --label "flat by" --before-close
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TCS --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check --kite trader/kite-quotes.json   # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE --write         # Yahoo -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-11-06 15:05"` pins the clock (IST); `--offline DIR` reads saved
Yahoo quote files instead of the network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes, merged in, with circuit limits from Kite, for trader-budget and trader-risk |
| `trader/portfolio.json` | the symbols held, for `quote --holdings` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the IST clock passed `at` ("15:05") on a trading day | once per trading day |
| `opening_bell` | NSE is open | once per trading day |

A rule with `"date"` is watched on that IST date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today. A `time` rule
with `"before_close": true` moves with a special session's close (the
Muhurat session ends at 15:30 but starts at 14:20).

## Quotes

- **Kite (preferred).** Call the connector's `get_quotes` with
  `["NSE:RELIANCE", ...]`, save the result to a file, and pass `--kite FILE`
  to `check` or `quote`. It reads `last_price`, `ohlc.close` (the previous
  close), `last_trade_time`, and `lower_circuit_limit` /
  `upper_circuit_limit`, which go into `trader/prices.json` so trader-risk
  can warn when a stock is near its circuit. `get_ltp` output also works
  (price only).
- **Yahoo** (fallback): `query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS`,
  an unofficial free JSON endpoint, one request per symbol, half a second
  apart, `query1` on a 429 or 5xx. Delayed and can lag, more for .NS.
- Free quotes are fine for alerts and for checking a size, not for timing an
  order. The exchange's price is the one that fills. Each price in
  `trader/prices.json` keeps its quote time, and trader-risk only uses one
  under 15 minutes old.

## The NSE clock

Regular session 9:15 to 15:30 IST (pre-open 9:00 to 9:08). F&O stocks stop
continuous trading at 15:15 for the closing auction (from 2026-08-03).
Closed on weekends and on the NSE trading holidays for 2026: Jan 15, Jan 26,
Mar 3, Mar 26, Mar 31, Apr 3, Apr 14, May 1, May 28, Jun 26, Sep 14, Oct 2,
Oct 20, Nov 10, Nov 24, Dec 25. Muhurat trading on Sunday Nov 8, 14:20 to
15:30. IST has no daylight saving, so the tool needs no time zone files. For
any other year it says the holiday list is unknown: check NSE's circular and
update `NSE_HOLIDAYS` in the scripts.

## Steps

1. Right after trader-exit writes the plan, run `add --from-plan`. Confirm
   the three alerts back to the user: the stop, the target and the flat-by time.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`, with a fresh `get_quotes` saved for `--kite`.
   A check reads the latest quote; it is not a live feed, and a price can
   pass a level between two checks.
3. Tell the user what rang in one line each. A stop alert means the cover
   order's stop has probably filled: check `get_positions` / `get_orders`
   and record the fill with trader-broker. A target or flat-by alert means
   the position is probably still open: trader-broker `flatten` (it exits
   the cover order), then trader-risk, then the user's yes. Never act for
   them.

## Example: RELIANCE, Friday 2026-11-06

Entry 1311.90 at 9:50 AM, stop 1300.70, target 1323.10, flat by 15:05.
`add --from-plan` writes `reliance-plan-stop`, `reliance-plan-target` and
`reliance-flat-by` for that day. A `check --kite` reads 1320.00, between the
stop and the target:

```
NSE OPEN (2026-11-06 10:53 IST, regular session).
Nothing hit. 3 rule(s) watched today, RELIANCE ₹1320.00.
```

At 15:06:

```
ALERT Flat by: it is 15:06 IST (set for 15:05), the market closes at 15:30. If RELIANCE is still open,
close it: trader-broker flatten --symbol RELIANCE builds the exit ticket, then trader-risk and your yes,
before Zerodha's intraday square-off does it for a fee.
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --opening-bell`, the bell rings at 9:15.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a Yahoo quote as live. Say where a price came from and when.
- Confirm every new rule back to the user: symbol, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, and never predicts where a price is going.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite keys live in the MCP server's own config, never in the chat.
