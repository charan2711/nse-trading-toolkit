# NSE Trading Toolkit

11 Claude skills for trading Indian stocks on NSE and BSE, intraday and swing: scan, track news and NSE filings, time every entry and exit, backtest, and replay every outcome before you risk a rupee.

Based on the Day Trader skill,
[github.com/Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT), rebuilt for Indian equities only: every tool runs on IST and the NSE
calendar, sizes in rupees with Zerodha's charges counted in every loss, reads
NSE data, and sends orders to Zerodha Kite through Kite's MCP connector.

Free, MIT, no signup. The tools are plain Python that read free public data
(Yahoo Finance .NS / .BO charts, NSE's equity lists, corporate announcements,
insider disclosures and bulk / block deals) or files you save from the Kite
connector (candles, quotes).

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default.

## Install

As a plugin, in Claude Code:

```
/plugin marketplace add charan2711/nse-trading-toolkit
/plugin install nse-trading-toolkit@nse-trading-toolkit
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/nse-trading-toolkit:trader-scanner` and so on. Copy the folders instead if you
want plain `/trader-scanner`:

```bash
git clone https://github.com/charan2711/nse-trading-toolkit.git
cp -r nse-trading-toolkit/trader-* ~/.claude/skills/
```

The tools need Python 3.8 or newer. Nothing to pip install.

### Connect Kite

1. Add Zerodha's Kite MCP server as a connector. The hosted server is
   `https://mcp.kite.trade/mcp` (see `trader-broker/claude_desktop_config.example.json`
   for Claude Desktop through `mcp-remote`). Log in through Kite's own page
   when it asks. Never paste a Kite password, TOTP, API key or access token
   into the chat.
2. The hosted server may not offer order tools. To place real orders you may
   need to self-host github.com/zerodha/kite-mcp-server with your own Kite
   Connect app. Until then, the pack runs in paper mode with Kite's prices.
3. In Claude, set `place_order`, `modify_order` and `cancel_order` to
   "Needs approval" (Customize, Connectors, tool permissions), never "Always
   allow".

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans NSE stocks (all EQ or an index like the Nifty 500) for setups: opening-range breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Yahoo headlines and NSE corporate announcements on every stock you watch, and today's move. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and your target on the NSE tick before you enter, flat by 15:05. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a rupee: 10,000 simulated sessions plus every past time the setup fired. |
| `/trader-risk` | Risk Manager | Checks every trade against your limits, charges included, and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade, after Zerodha's charges. |
| `/trader-broker` | Order Executor | Sends approved orders to Zerodha Kite as cover orders, paper trading by default, never without your yes. |
| `/trader-alerts` | Price Alerts | Rings when a price, a stop, the flat-by time or the 9:15 bell hits. |
| `/trader-insider` | Insider Tracker | SEBI PIT insider trades and NSE bulk / block deals, flagged when they touch your stocks. |
| `/trader-swing` | Swing Trader | After-close scan for pullbacks, breakouts, RSI-60 momentum and stocks near a breakout; CNC buy with a GTT stop and target; a costed backtest. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by 15:05                                           (while it is open)
news / insider                                                                (on a schedule)
swing: scan after 15:30 -> plan -> risk -> broker (CNC + GTT) -> manage each evening
```

An example: RELIANCE, Friday 2026-11-06, on Kite's real 5-minute candles.

1. **`/trader-scanner`** at 10:15 AM IST: RELIANCE closed above its
   15-minute opening range (1295.00 to 1306.40) on the 9:45 bar.
2. **`/trader-news`**: the morning's headlines and NSE filings for RELIANCE,
   in IST, with the day's move.
3. **`/trader-entry`** watched it bar by bar. At 9:47 it said waiting (the
   9:45 bar was still forming). At 9:51 it said triggered at 9:50 AM, 1311.90.
4. **`/trader-exit`** set the plan before the entry: stop at the range
   midpoint, 1300.70 (11.20 a share at risk), target at 1R, 1323.10, flat by
   15:05 either way.
5. **`/trader-timemachine`** replayed the rest of the day 10,000 times from
   the stock's own 5-minute bars (seed 19) and re-ran the rule on past
   sessions. On the test fixture the verdict is close to a coin flip. That is
   the point of looking before you risk a rupee.
6. **`/trader-budget`** with a ₹1,00,000 budget and a ₹500 max loss: 39 shares
   (₹51,164.10). At the stop: ₹436.80 plus ₹54.88 of charges = ₹491.68. At
   the target: ₹381.58 after charges. It writes one Kite cover order: buy 39
   RELIANCE, MIS, stop-loss trigger 1300.70.
7. **`/trader-risk`** checks it against your limits: max loss with charges,
   daily loss, trades per day, reward to risk, the entry window, position
   size, cash and circuit limits. PASS or BLOCK, with the reason.
8. **`/trader-broker`** shows you the ticket in plain words. You type yes for
   that exact order. In paper mode it records a simulated fill at Kite's last
   price. Live, Claude calls Kite's `place_order` and Claude's own "Allow
   once / Deny" card asks you one more time.
9. **`/trader-alerts`** rings on the stop, the target (Kite cover orders have
   no target leg) and at 15:05 to get out before Zerodha's MIS square-off.

## The tools

```bash
python3 trader-scanner/scan.py universe --index nifty500               # NSE index list
python3 trader-scanner/scan.py daily                                   # pre-market setups
python3 trader-scanner/scan.py orb --at 10:15                          # opening-range breakouts so far
python3 trader-news/news.py --tickers RELIANCE --hours 12              # headlines, NSE filings, today's move
python3 trader-entry/entry.py check RELIANCE --setup orb               # waiting / triggered / expired
python3 trader-entry/entry.py check RELIANCE --offline kite-5m.json    # on Kite candles
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat by
python3 trader-alerts/alerts.py check --kite quotes.json               # rings what hit, once
python3 trader-insider/insider.py pit --buys --market-only             # insiders buying
python3 trader-insider/insider.py deals                                # today's bulk and block deals
python3 trader-swing/swing.py scan --data kite-daily/                   # swing setups + near breakouts
python3 trader-swing/swing.py backtest --data kite-daily/               # the rules on 5 years, with costs
```

```bash
cd tests && python3 -m unittest -v
```

The tests run offline, on fixtures in `tests/fixtures/india`: Kite's real
5-minute candles and quote for RELIANCE on 2026-11-06, a synthetic 60-day
history built around that day, and sample NSE files (marked SAMPLE) for the
equity list, announcements, insider trades and deals. NSE's API field names
are not documented and were not checked live from here: the parsers read
them loosely, and the first live run is worth watching.

## Swing trading

`/trader-swing` holds stocks for days to weeks as delivery (CNC). After the
close it checks the Nifty's trend, scans for three setups (pullback to the
20-day EMA, volume breakout from a base, multi-timeframe RSI-60) and lists
stocks coiling just under a breakout, with the trigger and a stop. It sizes a
trade to 1% of your swing capital after Zerodha's delivery charges and writes
a CNC limit buy plus a GTT OCO order for the stop and the 2R target.

Its backtest runs the exact rules on Kite daily candles with costs. On 36
large caps from 2022 to 2026 the honest answer was: no edge after costs
(400 trades, 37% wins, -0.05R a trade; breakout and RSI-60 near break-even,
pullback negative) while the Nifty rose 40%. The tool is for testing ideas on
your own stocks before risking money, not a proof that any setup works.
See `trader-swing/SKILL.md`.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
SEBI's study of FY2023 found about 7 in 10 individual intraday traders in
equity cash lost money, and about 9 in 10 F&O traders lost money. You can
lose money. Decide for yourself, and talk to a SEBI-registered investment
adviser if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat. Set Kite's order tools to "Needs approval", never
"Always allow". No options, futures, MTF, short selling or MIS leverage
unless you turn them on.

**Paper first.** `"paper": true` is the default in `config.example.json`.
Kite has no paper account, so paper mode never calls an order tool: it
records a simulated fill at Kite's last price.

**Charges are an estimate.** Brokerage, STT, exchange, SEBI, stamp duty and
GST follow Zerodha's published intraday rates as read on 2026-11-06. Rates
change; set `"charges"` in the config to match. The contract note is the bill.

**Zerodha squares you off.** MIS positions still open at about 15:12 (F&O
stocks, closing auction) or 15:25 (the rest) are closed by Zerodha for ₹50 +
GST an order. The pack's flat-by is 15:05 for that reason.

**The Time Machine is a simulation, not a prediction.** Results, a circuit, a
block deal or a headline can do things the last 60 days never did.

**A stop is a plan, not a guarantee.** In a fast market, a gap or a circuit,
a stop can fill well past its price, or not at all.

**The data is free and unofficial.** Yahoo's chart endpoint can lag for .NS
stocks; Kite candles and quotes are better. NSE's site blocks some scripted
clients; save the file from the browser and pass `--file` or `--offline`.

**What you can trade intraday** is decided by SEBI's margin rules and
Zerodha's MIS list, which changes; Kite rejects an MIS order on a stock that
is not on it.

**Insider data is public and after the fact.** PIT disclosures can be two
trading days late; bulk and block deals are published after the close.

**Taxes.** Intraday equity gains are speculative business income in India,
taxed at your slab rate; swing (delivery) gains held under 12 months are
short-term capital gains. Nothing here is tax advice.

## Files

```
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-insider/     SKILL.md, insider.py
trader-swing/       SKILL.md, swing.py
tests/              unittest suite + offline fixtures
```

MIT. Original by [Jake Schincariol](https://opusjake.ai); Indian-market version by [Charan](https://github.com/charan2711).
