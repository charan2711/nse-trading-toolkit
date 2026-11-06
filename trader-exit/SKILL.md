---
name: trader-exit
description: >-
  Exit Planner for NSE / BSE intraday trades. Sets your stop and your target
  before you enter, so you know when to get out. Reads the entry from
  trader-entry (or a price you give), puts the stop at the opening-range
  midpoint by default (or the range low, your price, a percent, or a multiple
  of the ATR), rounds it away from the entry to the NSE tick for that price
  band, and sets the target at 2R (or 1R, or your price). Refuses a stop at or
  above the entry, a target at or below it, or a stop too tight to survive the
  spread, and says why. Flat by 15:05 IST by default, ahead of Zerodha's MIS
  square-off. Writes trader/plan.json for the Time Machine, the Budget Manager
  and Price Alerts. Long side only. Use when the user says "where's my stop",
  "set my stop and target", "plan the exit", "when do I get out", "stop
  loss", "SL kahan lagau", "what's my R", "risk reward", or right after
  /trader-entry triggers, before /trader-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X]"
---

# trader-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap or a fast market can fill past it. Intraday trading loses most people
money (SEBI's own studies say about 7 in 10 intraday traders lose), and a
neat plan does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo, Kite or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --entry 1311.90 --or-high 1306.40 --or-low 1295.00
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

Other flags: `--flat-by 15:05`, `--tick 0.05` (override the NSE tick),
`--json`, `--now "2026-11-06 09:51"` (IST), `--entry PRICE` with a waiting
entry.json to plan ahead, `--setup level`, `--offline FILE` (a saved Yahoo
chart JSON or Kite candles from `get_historical_data`, for an ATR stop).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from trader-entry: symbol, exchange, entry price, time, bar, opening range |
| `trader/config.json` | `flat_by` (15:05) and `min_reward_risk` (1.0: a note here, trader-risk enforces it) |
| Yahoo chart API or Kite candles | only for an `atr:K` stop: the session's bars up to the entry bar |
| `trader/plan.json` | with `--write`: entry, stop and its rule, target and its R, risk a share, reward:risk, tick, exchange, flat by. trader-timemachine, trader-budget and trader-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **Ticks.** NSE prices move in steps that depend on the price: under ₹250
  it is 0.01, under ₹1,000 it is 0.05, under ₹5,000 it is 0.10, under
  ₹10,000 it is 0.50, under ₹20,000 it is 1, above that 5. Kite rejects a
  trigger price off the tick, so the stop rounds AWAY from the entry to the
  tick (down, for a long). Risk = entry minus stop. Target = entry + R x
  risk, rounded to the tick. `2R` by default; `1R`, `1.5R` or `price:X`
  also work.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under 0.05% of the price: too tight to survive the
  spread, normal wiggle would stop you out. An entry.json that has not
  triggered (unless you give `--entry`). A short: v2 plans longs only.
- **Flat by.** 15:05 IST by default (or `flat_by` in the config). Out by
  then, win or lose, no overnight hold. Zerodha squares off MIS positions
  itself at about 15:12 for stocks in the closing auction (F&O stocks) and
  about 15:25 for the rest, and charges ₹50 + GST an order for it. A flat-by
  after 15:10 gets a warning. On a special session (Muhurat trading) it
  moves inside that session. An entry at or after the flat-by time is
  refused.

## Steps

1. Run it right after `/trader-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/trader-timemachine` to replay the plan, then `/trader-budget`
   to size it to the max loss per trade. Offer `/trader-alerts` for the stop,
   the target and the flat-by time. Claude can keep `entry.py watch` running
   in the background or on a `/loop` until a setup triggers, then come here.

## Example: RELIANCE, Friday 2026-11-06

The entry is 1311.90 at 9:50 AM and the opening range (9:15 to 9:30) is
1295.00 to 1306.40. `exit.py plan --stop or-mid --target 1R --write`:

```
Exit plan: RELIANCE long, Fri 2026-11-06, opening-range breakout on 5-minute bars
  Entry         ₹1,311.90
  Stop          ₹1,300.70  opening-range midpoint: (₹1,306.40 + ₹1,295.00) / 2 = ₹1300.7
  Target        ₹1,323.10  1R (one times the risk)
  Risk a share  ₹11.20 (0.85% of the entry)
  Reward:risk   1.0
  Flat by       15:05 IST, win or lose: no overnight hold (intraday, MIS)
```

The tick at this price is 0.10. The default 2R target would be 1311.90 + 2 x
11.20 = 1334.30. trader-budget then sizes it: a ₹500 max loss per trade,
charges included, buys 39 shares.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk. It
  never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Long side only. v2 plans longs only.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite keys live in the MCP server's own config, never in the chat.
