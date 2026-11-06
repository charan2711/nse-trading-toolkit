---
name: trader-news
description: >-
  News Monitor for NSE / BSE stocks. Tracks breaking news on every stock you
  watch and flags what moves it: Yahoo Finance headlines per symbol (India
  region), each company's NSE corporate announcements (financial results,
  board meeting outcomes, MD or CFO changes, mergers and demergers, credit
  rating actions, pledges, dividends, buybacks, bonus and splits) and how far
  each stock is from yesterday's close. Shows only what is new since the last
  check, newest first, in IST, tags each item (earnings, analyst, deal,
  capital, people, risk, halt, regulatory, macro) and never repeats one. Use
  when the user says "any news on RELIANCE", "why is this moving", "what's
  the news on my watchlist", "morning briefing", "pre-market news", "monitor
  the news", "did anything break", "check NSE announcements", "corporate
  filings", or after /trader-scanner finds setups.
argument-hint: "[symbols]"
---

# trader-news

A chart can look perfect and still be about to gap on a results date, a
circuit or a rating downgrade. Before a setup becomes a trade, this skill
reads the two feeds that carry those first, for every stock on the
watchlist, and tells you only what is new. It also prints how far each stock
has moved today, so a headline and a move sit side by side.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                              # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE TCS --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-nse --json              # headlines only
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (trader-scanner) |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (trader-broker records fills there) |
| Yahoo Finance headline RSS | `https://feeds.finance.yahoo.com/rss/2.0/headline?s=RELIANCE.NS&region=IN&lang=en-IN` |
| Yahoo Finance chart endpoint | the last price against yesterday's close (unofficial, can lag) |
| NSE corporate announcements | `nseindia.com/api/corporate-announcements?index=equities&symbol=RELIANCE`. NSE wants a browser user agent and the cookies from its home page first; the tool does that. If NSE blocks it, it says so and goes on with the headlines |
| `trader/state/news.json` | what has been shown (kept 30 days) |

`--offline DIR` reads saved files instead (`yahoo-SYMBOL.xml`,
`nse-SYMBOL.json`, `chart-SYMBOL.json`), for testing or when NSE is blocking.
The exact field names of NSE's API are not documented; the tool reads them
loosely (`desc`, `attchmntText`, `an_dt`, `attchmntFile`, and close
variants) and says when a field is missing.

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story or the filing and read it. Quote the source and
  link it. Never summarise a paywalled story you could not read: say so.
- **Filings beat headlines.** If an NSE announcement and a headline
  disagree, the announcement is what the company told the exchange (SEBI
  LODR Regulation 30 requires material events to be disclosed).
- **Say what it touches.** Every item lists the symbols it touches and its
  tags. Announcements marked `!` carry an important subject: financial
  results or a board meeting outcome, an acquisition, merger, demerger or
  scheme, a director, MD, CFO or auditor change, a default, insolvency
  (NCLT), fraud, credit rating action, pledge or invocation, search or
  penalty, or a dividend, buyback, bonus, split, rights issue, QIP or record
  date.
- **Halts.** Headlines about a circuit, a suspension, or the ASM / GSM
  surveillance lists are tagged `halt`. A stock in ASM can carry 100% margin,
  so no MIS.
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no headline is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: every `!` filing, anything tagged earnings,
   halt, legal, regulatory or risk, and every MOVING symbol first.
3. Write a short brief, per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "results
   are after market today: an overnight gap can jump past any stop, so this
   stays intraday").
4. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`: results inside the trade, a circuit, a block deal, a
   pledge invocation. Never act on a headline alone. `/trader-insider deals`
   shows today's bulk and block deals.

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before 9:00 IST (pre-open starts at 9:00).

## Example

```
RELIANCE     ₹1,320.00 +0.50% vs yesterday's close ₹1,313.40 at 16:08 IST

  2026-11-06 08:40 IST  RELIANCE     Yahoo Finance Reliance Industries shares rise as Jio tariff hike lifts outlook; ...
      tags: analyst  https://finance.yahoo.com/news/...
  2026-11-05 19:02 IST  RELIANCE     NSE filing    Reliance Industries Limited: Analysts/Institutional Investor Meet/Con. Call Updates
      https://nsearchives.nseindia.com/corporate/...
```

(From the sample fixtures in `tests/fixtures/india/news`; the headline is
made up.) The brief: "One analyst headline and a routine investor-meet
filing. Nothing that moves the date of a gap. Up 0.5% on the day." When a
symbol has no news, say so rather than padding the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per feed per symbol, a pause between
  calls. NSE blocks clients that hit it hard.

## Safety

- Education and tooling, not financial advice. This skill only reads and reports.
- Nothing places an order except `/trader-broker`, after `/trader-risk` passes and the user types an
  explicit yes for that order.
- Kite keys live in the MCP server's own config, never in the chat.
