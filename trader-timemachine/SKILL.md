---
name: trader-timemachine
description: >-
  Time Machine for NSE intraday trades. Replays every possible outcome of a
  trade before you risk a rupee: 10,000 simulated sessions plus every past
  time the setup fired. Takes the plan from trader-exit (entry, stop,
  target), rebuilds the rest of the day 10,000 times from the stock's own
  5-minute bars over the last 60 days (Yahoo .NS or saved Kite candles), and
  re-runs the same rule on every past session. Prints the odds of target
  first, stop first, or neither by the end (15:15 for F&O stocks that go into
  the closing auction, else 15:30, or your flat-by time), where the price
  sits then, the average R, the history, and one honest verdict, then hands
  off to /trader-budget. Use when the user says "run the time machine",
  "replay this trade", "what are the odds", "simulate this trade", "monte
  carlo", "backtest this setup", "how often does this hit the target", "is
  this a coin flip", or after /trader-exit writes a plan, before /trader-budget.
argument-hint: "[path to plan.json, or SYMBOL]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the stock's own last 60
days, how often does this plan hit the target first, the stop first, or
neither? It answers with 10,000 replays and with the real record of the same
rule on every past session. It is a simulation built from the stock's own
recent bars, not a prediction. SEBI found about 7 in 10 intraday traders
lost money in FY2023, and a good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:05      # end each replay at your flat-by time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-11-06 \
    --entry 1311.90 --stop 1300.70 --target 1323.10 --entry-time "9:50 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--offline FILE` (a saved Yahoo chart JSON or Kite candles, `.json` or
`.json.gz`), `--now "2026-11-06 09:51"` (IST) to pin the clock. For Kite,
save `get_historical_data` with interval `5minute` from 60 days back to today.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range |
| Yahoo chart API or Kite candles | 60 days of 5-minute bars (`SYMBOL.NS`, `range=60d&interval=5m`): Yahoo is free and unofficial and can lag; Kite is exchange data |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the close |

## How it works

1. **The pool.** It keeps only FULL sessions before the plan's date: all 78
   five-minute bars of the stock's normal session: 75 bars (9:15 to 15:30),
   or 72 for an F&O stock, whose continuous trading ends at 15:15 for the
   closing auction (from 2026-08-03). It takes whichever count most recent
   sessions have. Short sessions, gaps and today are left out. Every bar becomes four ratios to the bar before it:
   open, high, low and close over the previous close.
2. **One replay.** Start at the entry price. For each bar left in the day,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default the end of
   continuous trading: 3:15 PM for an F&O stock, 3:30 PM for the rest). Green, which means target
   first or above the entry at `--until`.
   R is +1 at a 1R target, -1 at the stop, and otherwise the distance from
   the entry at `--until` divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse.
4. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on every one of those past sessions. That is
   what really happened, not a simulation.
5. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them. So a 2R plan with target first
   24.1 % and stop first 50.5 % is close to break-even (it needs 33 % of
   the 74.6 % that hit a side, and got 32 %), not a loser.
6. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean. Those can only point the
   other way because of the replays that hit neither.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool (Kite keeps more, but
the tool stays on 5-minute bars). It refuses, in one
plain line, when there are fewer than 20 full sessions, when the entry bar is
not in the data (wrong date, not closed yet, a future day), or when the plan
does not make sense (a stop above a buy's entry, an entry off the 5-minute
grid). A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. Add `--until 15:05` when the plan has a
   flat-by time, so "neither" means still open at the time you would get out.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the close, the average R, the history line, then the
   verdict line and any line under it exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   Earnings, news and halts can do things those 60 days never did. Check
   `/trader-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

## Example: RELIANCE, Friday 2026-11-06

The 15-minute opening range is 1295.00 to 1306.40. The 9:45 bar closes above
it: entry 1311.90 at 9:50 AM (bar 6), stop 1300.70 (the range midpoint),
target 1323.10 (1R, risk 11.20 a share). RELIANCE is an F&O stock, so the
session ends at 3:15 PM.

```
10,000 replays of the rest of the session, 9:50 AM to 3:15 PM (65 five-minute bars), seed 19.
  target first   36.2 %
  stop first     33.1 %
  neither        30.7 %   still open at 3:15 PM
  green          51.9 %   target first, or above the entry at 3:15 PM
  Price at 3:15 PM: median 1312.94, 90 % of replays between 1293.41 and 1332.71.
  Average R: +0.034
History: the same rule ... on each of those 41 real sessions fired 26 times: 14 target, 11 stop, 1 neither. Average R +0.122.
Close to a coin flip: target first 36.2 % vs stop first 33.1 %.
```

(The test fixture behind these numbers is the real Kite day plus 41
synthetic sessions, so treat them as a demo of the output, not of RELIANCE.)
At 1R the target has to come first in half of the replays that hit one
side. It did in 52 %. That is close to a coin flip, and it is what the user
should hear before `/trader-budget` sizes 39 shares.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag; Kite candles are better. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite keys live in the MCP server's own config, never in the chat.
