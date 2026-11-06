---
name: trader-scanner
description: >-
  Chart Scanner for NSE stocks. Scans hundreds or thousands of charts for
  setups: opening-range breakouts, flags, oversold bounces. Builds the list of
  NSE equities from NSE's own EQUITY_L.csv (series EQ by default) or an index
  list (Nifty 50, 100, 200, 500, Next 50, Midcap 150, Smallcap 250), runs a
  pre-market scan of daily closes (20-day closing-high breakout, bull flag,
  reclaimed the 20-day average, oversold, oversold bounce) and an intraday
  opening-range breakout scan on 5-minute bars (9:15 to 9:30 IST range), then
  writes trader/watchlist.json for trader-news and trader-entry. Free Yahoo
  .NS data or saved Kite candles, bar by bar. Use when the user says "scan
  the market", "scan Nifty 500", "find me setups", "what is breaking out",
  "opening range breakouts", "scan for bull flags", "oversold stocks",
  "pre-market scan", "build my watchlist", "what should I look at today",
  or at the start of a trading day before /trader-news.
argument-hint: "[universe | daily | orb] [--index nifty500] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade. It reads free charts and
lists the ones that match a rule. That is all it does. A scan is a list of
charts that match a rule, not a list of stocks to buy, and a setup on a chart
is not a prediction. SEBI found about 7 in 10 individual intraday traders
lost money in FY2023; the scanner does not change that. It only saves you
from flipping through 2,000 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                       # every NSE EQ stock -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe --index nifty500      # or just an index
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                          # before 9:15 -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,TCS,INFY  # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                            # after 9:35 AM IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                 # cut the clock at 10:15 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--min-price 50` (rupees) and `--min-volume` (daily 2,00,000 shares,
orb 0), `--or-minutes 15`, `--max-confirm 200` (orb stage 2), `--series
EQ,BE` (universe), `--now "2026-11-06 10:15"` (IST) to pin the clock,
`--offline PATH` to read saved Yahoo or Kite files (a folder of
`<SYMBOL>.json`), `--no-write`, `--show N`, `--json`. Exit code 0 when the
scan found something, 1 when it found nothing or could not run (the reason is
one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line, read by `daily` and `orb` |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. trader-news and trader-entry read `hits[].symbol` |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 14:45) |

## The universe

`universe` downloads `EQUITY_L.csv` (or `ind_<index>list.csv`) from
nsearchives.nseindia.com with a browser user agent, and keeps the rows whose
SERIES is in `--series` (EQ by default). BE (trade-for-trade, no intraday),
SM/ST (SME) and the rest are dropped and counted. Symbols stay plain NSE
names (`RELIANCE`, `M&M`, `BAJAJ-AUTO`); Yahoo gets them as `SYMBOL.NS`,
URL-escaped. NSE sometimes blocks scripted downloads: if it does, save the
CSV from the browser and pass `--offline FILE`.

Not every EQ stock is allowed for MIS on Zerodha, and that list changes.
trader-risk does not know it; Kite rejects an MIS order on a stock that is
not on it.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request, so the Nifty
500 is 25 requests and all of NSE EQ (about 2,000) is about 100. Liquidity
comes from the spark meta: last price at least `--min-price` and last
session's volume at least `--min-volume`. Run it before 9:15 or after 15:30:
during the session the volume is the day's so far, and today's daily bar is
left out until the close.

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Spark has no highs or lows, so these are closing-price rules. Hits are listed
most traded first (price times volume).

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 9:15,
9:20 and 9:25 five-minute bars. A hit is the first completed 5-minute bar to
CLOSE above the range high, at that bar's close time.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range (a necessary condition, so no
   breakout is lost).
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each, at most `--max-confirm` (200), the most traded first. It
   prints PARTIAL with the count it skipped. `--max-confirm 0` checks them all.

A chart is usable only when every 5-minute bar from 9:15 to the cutoff is
there; a missing bar means a halt, a circuit lock or a thin stock. A bar
counts only once it is complete. Before 9:35 AM, on a weekend or an NSE
holiday, after the close, or after `no_entries_after` it prints one plain
line and writes nothing.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Kite
  candles are exchange data: for a short list, fetch `get_historical_data`
  (5minute, today) per symbol into a folder and pass `--offline FOLDER`.
- Partial results are said as partial: "Fetched 480 of 500 charts (20 failed
  or had no data)". Never round a partial scan up to "the whole market".
- NSE holidays for 2026 are in the calendar, plus the Muhurat session on
  2026-11-08. Check NSE's circular each year and update `NSE_HOLIDAYS`.

## Steps

1. Once a week or so: `scan.py universe` (or `--index nifty500`).
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup.
3. After 9:35 AM IST: `scan.py orb`. Tell the user how many usable charts
   broke their range and when, and that a later hit is closer to the price now.
4. The user picks one chart. Hand it on: `/trader-news SYMBOL` (what is
   moving it), then `/trader-entry SYMBOL` (the entry bar by bar). Never pick
   for them.

## Example: RELIANCE, Friday 2026-11-06

```
python3 scan.py orb --symbols RELIANCE --offline charts/ --now "2026-11-06 10:15"
Opening-range breakout scan, Fri 2026-11-06 10:15 AM IST: 15-minute range (9:15 AM to 9:30 AM), 5-minute bars, saved charts.
Fetched 1 of 1 charts; 1 usable; 1 candidates, 1 confirmed with real highs and lows.
1 of 1 charts closed above their opening-range high by 10:15 AM:
   9:50 AM  RELIANCE        ₹1,311.90  range 1,295.00 to 1,306.40  +0.42% over
```

`charts/RELIANCE.json` is the Kite 5-minute candles for the day. The user
picks it and Claude hands it to `/trader-news RELIANCE`, then
`/trader-entry RELIANCE`.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls Yahoo data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite keys live in the MCP server's own config, never in the chat.
