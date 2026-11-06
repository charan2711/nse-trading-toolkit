---
name: trader-insider
description: >-
  Insider and Big-Money Tracker for NSE stocks. Reads two public NSE feeds: insider trades that promoters,
  directors, key managerial people and designated employees must disclose
  under SEBI's Prohibition of Insider Trading (PIT) Regulations within two
  trading days, and the daily bulk deals (one client over 0.5% of a company's
  shares) and block deals (single trades of Rs 10 crore or more). Shows who
  bought or sold, how many shares, the rupee value in lakh or crore, holding
  before and after, the mode (market purchase vs ESOP, gift, pledge), and
  flags rows in stocks you hold or watch. Alerts once per new row. Use when
  the user says "are promoters buying", "insider trades", "insider buying in
  RELIANCE", "who is buying this stock", "bulk deals today", "block deals",
  "big money", "smart money", "FII bought", or "run the insider skill".
argument-hint: "[pit | deals] [--symbol SYMBOL] [--mine]"
---

# trader-insider

Company insiders have to tell the exchange when they trade their own
company's shares, and NSE publishes every big trade of the day. This skill
reads both and tells you what is new.

It is fast. It is not early. A PIT disclosure can come two trading days
after the trade, and bulk and block deals are published after the close.
There is no inside information here: only public filings, read quickly.
Say that every time. Insiders sell for many reasons (tax, a house, a
pledge), and a promoter buying is not a promise the price goes up.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit                              # last 7 days, insiders only
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit --symbol RELIANCE --days 90  # one company, three months
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit --buys --market-only         # open-market buys only
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit --all-people                 # every disclosure, not just insiders
python3 "${CLAUDE_SKILL_DIR}/insider.py" deals                            # today's bulk and block deals
python3 "${CLAUDE_SKILL_DIR}/insider.py" deals --block --mine             # block deals in what you hold or watch
python3 "${CLAUDE_SKILL_DIR}/insider.py" pit --file pit.csv               # a CSV you downloaded from nseindia.com
```

Other flags: `--min-value 1000000` (rupees), `--show N`, `--json`,
`--dry-run` (do not remember what was shown), `--now 2026-11-06`,
`--offline DIR` (reads `pit.json` / `pit.csv`, `bulk.csv`, `block.csv`).

## What it reads and writes

| file / feed | what |
| --- | --- |
| NSE PIT API | `nseindia.com/api/corporates-pit?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY`, after opening the home page once for cookies |
| NSE bulk / block CSVs | `nsearchives.nseindia.com/content/equities/bulk.csv` and `block.csv`, published each evening |
| `trader/portfolio.json`, `trader/watchlist.json`, `trader/plan.json` | your symbols, for `--mine` and the `YOURS` flag |
| `trader/state/insider.json` | what has been shown, so each row says `NEW` once |

NSE blocks some networks and changes these endpoints without notice, and
their field names are not documented. The tool matches columns loosely
(and copes with NSE's own typos). If it is blocked, it says so in one line:
download the CSV from nseindia.com (Companies > Corporate filings > Insider
trading, or Market data > Bulk / Block deals) and pass `--file`.

## Reading a row

- **Who.** The person and their category: Promoter, Promoter Group,
  Director, KMP (key managerial personnel), Designated Person, Immediate
  relative. By default only these insiders are shown; `--all-people` shows
  the rest.
- **Mode.** Market Purchase / Market Sale is a trade with their own money on
  the exchange. ESOP, Gift, Pledge, Revocation of pledge, Inter-se transfer
  inside a promoter group are listed but say so: they are not a view on the
  price. `--market-only` hides them.
- **Size.** Shares in Indian grouping (70,00,000) and value in lakh or
  crore. A ₹50 lakh buy by a director of a ₹2 lakh crore company is small.
  Holding before and after shows whether it moved their stake.
- **Bulk vs block.** A bulk deal is any client crossing 0.5% of the
  company's shares in a day, through normal trading. A block deal is one
  negotiated trade of ₹10 crore or more in the special block window. Both
  name the client (often a fund, an FII or a broker's own book).
- **Flags.** `NEW` the first time a row is shown, `YOURS` when the symbol is
  in your portfolio, watchlist or plan.

## Steps

1. Run `insider.py pit` (and `deals` after about 6 PM IST). Exit code 1
   means nothing new.
2. Lead with `YOURS` rows, then open-market buys and sells by promoters and
   directors, then the biggest deals by value.
3. For each row worth mentioning: who, bought or sold, how much, when it
   traded, when it was disclosed, and that it is after the fact.
4. If a row touches a planned trade (a promoter selling, a big block sale in
   the stock you are about to buy), say so before `/trader-entry`. Never act
   on it alone.

## Example

```
Open-market trades by insiders: 2 buys, 0 sells.
  RELIANCE  SAMPLE DIRECTOR (Director) BOUGHT 5,000 shares at about ₹1,310.00, ₹65.50 lakh, ...
            traded 2026-11-04, mode Market Purchase, disclosed 2026-11-05  [NEW, YOURS]
NSE bulk and block deals: 3 read, 3 shown.
  HDFCBANK  BLOCK SAMPLE INSURANCE COMPANY BOUGHT 12,00,000 shares at ₹1,745.50 = ₹209.46 crore on 2026-11-06  [NEW]
```

(From the sample fixtures in `tests/fixtures/india/insider`; the SAMPLE
rows are made up.) The brief: "One director bought ₹65 lakh of RELIANCE on
the open market on Nov 4, disclosed a day later. Small next to the company's
size. A disclosure, not a tip."

## Rules

- Every row is public and after the fact. Say so with every brief.
- Never call a row "smart money says buy", and never rank stocks to buy from it.
- Never invent a name, a figure or a date. If a field was missing, say so.
- Never print a P&L or what a follower "would have made".

## What it never does

It never places, sizes or checks an order, never claims an edge, and never
hammers NSE: one request per feed per run.

## Safety

- Education and tooling, not financial advice. This skill only reads and reports.
- Nothing in this pack places an order except trader-broker, and only after trader-risk passes that
  exact order and the user types an explicit yes for it in the chat.
- Kite keys live in the MCP server's own config, never in the chat.
