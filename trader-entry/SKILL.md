---
name: trader-entry
description: >-
  Entry Timer for NSE / BSE stocks. Watches a setup bar by bar and tells you
  the moment to get in. Reads free Yahoo (.NS / .BO) 1-minute or 5-minute
  bars, or Kite candles from the Zerodha connector's get_historical_data,
  builds the opening range (9:15 to 9:30 IST by default) and calls the entry
  on the first completed bar that CLOSES above the range high, or above a
  level you give it (like a 20-day high from the scanner), with an optional
  volume check. Says waiting and how far, triggered with the time and the
  price, expired, or not open yet, and writes trader/entry.json for
  /trader-exit. Long side only. It never places an order. Use when the user
  says "tell me when to get in", "watch RELIANCE", "has it broken out", "did
  it break the opening range", "entry signal", "time my entry", "check my
  watchlist", or after /trader-scanner and /trader-news, before /trader-exit.
argument-hint: "[SYMBOL] [--setup orb|level] [--interval 5m|1m]"
---

# trader-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads bars, 1-minute or 5-minute, and waits for each one to finish. A price
that pokes above the high in the middle of a bar is not an entry. A bar that
CLOSES above it is. Then it says the time and the price, writes them down for
the Exit Planner, and stops. It never buys anything. Day trading loses most
people money (SEBI's study of FY2023 found about 7 in 10 intraday traders
lost), and a clean signal does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE              # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --write      # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --setup level --level 1320
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch RELIANCE --until 14:45 # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                   # every hit in trader/watchlist.json
```

Other flags: `--or-minutes N` (default 15), `--json`, `--offline FILE` (a
saved Yahoo chart JSON or Kite candles, `.json` or `.json.gz`, or a folder of
`<SYMBOL>.json`), `--now "2026-11-06 09:51"` (IST) to replay a day.
Symbols are plain NSE names (`RELIANCE`, `M&M`); `BSE:SYMBOL` or `X.BO` for BSE.
`watch` with `--now` or `--offline` runs on a simulated clock and replays the
day bar by bar without waiting.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`), for `watchlist` |
| `trader/config.json` | `no_entries_before` (09:30) and `no_entries_after` (14:45), IST. Optional |
| Yahoo chart API | today's bars for `SYMBOL.NS`, `range=1d&interval=5m` (or `1m`): free, unofficial, can lag |
| Kite candles (preferred) | `--offline FILE`: save the connector's `get_historical_data` (interval `5minute`, today) to a file. Exchange data, no lag beyond the bar |
| `trader/entry.json` | with `check --write`, or when `watch` triggers: symbol, setup, status, time, bar, price, level, opening range |

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes: the
  9:15, 9:20 and 9:25 five-minute bars, or fifteen 1-minute bars. The level
  to beat is the range high.
- **Level.** A price you give, like a 20-day high from `/trader-scanner`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time (the 9:45 bar closes at 9:50 AM).
  The price is its close.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Yahoo has caught up to that moment (it waits a
  minute at most). A live tick, like 10:49:45, is never a bar.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. The 9:15 bar follows the
  pre-open auction (9:00 to 9:08) and is heavy, so 1.0 is already strict.
- **Time limits.** No entries before 9:30 or after 14:45 (config), so there
  is time to manage the trade before Zerodha's MIS square-off (about 15:12
  for F&O stocks in the closing auction, 15:25 for the rest). On a special
  session (Muhurat trading, 2026-11-08 14:20 to 15:30) the cutoff moves inside it.
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before 9:15: not open yet. Before 9:30: the
  range is still forming. Weekends and NSE holidays: the market is closed.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/trader-scanner`. Check `/trader-news` for it first.
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 14:45` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade.
5. Hand off to `/trader-exit` to set the stop and the target BEFORE anything
   is bought.

## Example: RELIANCE, Friday 2026-11-06

5-minute Kite bars. The opening range is 1295.00 to 1306.40. At 9:47 the 9:45
bar is still open, so it waits:

```
WAITING  last close ₹1305.70 (9:40 bar), ₹0.70 under the range high ₹1306.40 (-0.05%).
The 9:45 bar is still forming (₹1311.90 so far, closes 9:50 AM); it does not count until it closes.
```

At 9:51 the bar has closed:

```
RELIANCE  Fri 2026-11-06 09:51 IST  opening-range breakout (15-minute range) on 5-minute bars (Kite)
Opening range 9:15-9:30: high ₹1306.40, low ₹1295.00 (3 of 3 bars).
TRIGGERED 9:50 AM at ₹1311.90: the 9:45 bar (bar 6) closed above the range high ₹1306.40 (+0.42%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /trader-exit (exit.py plan --from trader/entry.json).
```

With `--min-volume-x 1` the 9:45 bar is too thin and the trigger moves to
10:00 AM at 1309.80 (the 9:55 bar, 1.32x the range's volume).

## Rules

- Bar by bar, on Kite candles or free Yahoo 1-minute or 5-minute bars. Yahoo is
  unofficial and can lag; say so when a minute matters, and prefer Kite.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only. v2 plans longs only. If the user wants a short, say that.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/trader-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/trader-broker` from a background
watch: nobody is there to say yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite keys live in the MCP server's own config, never in the chat.
