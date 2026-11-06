---
name: trader-broker
description: >-
  Order Executor for Zerodha Kite. Sends approved orders to Kite through the
  Kite MCP connector, paper trading by default, never without your yes. Shows
  each order as a plain ticket (a cover order shows its stop-loss trigger, the
  target the pack watches and the max loss at the stop with charges) plus the
  exact arguments for Kite's place_order, records your yes word for word,
  then Claude's own Allow once / Deny card asks again. In paper mode it
  records a simulated fill at the live Kite price instead. Builds the exit
  that closes a day trade by your flat-by time (exiting a cover order through
  cancel_order on its stop-loss leg), records fills, and logs each trade's
  max loss so trader-risk can hold the daily limit. Covers connecting Kite's
  MCP server in Claude and setting order tools to "Needs approval". Use when
  the user says "place the order", "buy it", "execute", "send it to Kite",
  "close my position", "square off", "flatten", "get me out", "connect Kite",
  "set up paper trading", or after /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches NSE only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the Kite order tools are set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id 261106000123456
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --paper --filled-qty 39 --filled-price 1312.10
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --qty 39 --price 1323.10
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE --qty 39
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to Kite. It renders tickets, checks that a reply is a
real yes for that order, builds the flatten ticket, and keeps the log in
`trader/state/broker.json`. `--now "2026-11-06 09:51"` pins the clock (IST).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, failed and filled, with each entry's max loss and Kite order id |
| `trader/portfolio.json` | cash and shares, updated by `fill` and by `result --filled-qty` |
| `trader/plan.json` | the flat-by time for `flatten` |

## Paper or live

`"paper": true` is the default in `trader/config.json`. Zerodha has no paper
account, so paper mode never calls Kite to trade: after the user's yes,
Claude reads the live price with Kite's `get_ltp` (read only) and records a
simulated fill with `ticket.py result --paper --filled-qty Q --filled-price P`.
The portfolio, the log and the daily limits then work exactly as they would
live. Run paper for a while before thinking about live. Going live is the
user's own decision: they set `"paper": false` themselves. Claude never does.

## Setup: Zerodha's Kite MCP server

Zerodha publishes the Kite MCP server (github.com/zerodha/kite-mcp-server).

1. **Hosted** (no install, no API key): add `https://mcp.kite.trade/mcp` as a
   connector in Claude (Customize > Connectors > Add custom connector), or in
   Claude Desktop's `claude_desktop_config.json` (the example file here):
   `npx mcp-remote https://mcp.kite.trade/mcp`. Zerodha's docs say the hosted
   server has left out the tools that place, change or cancel orders; if
   `place_order` is not in the tool list, the hosted server is read only for
   orders: see "No order tools" below.
2. **Self-hosted** (order tools included): a Kite Connect developer app
   (developers.kite.trade, a paid subscription) gives an API key and secret.
   They go in the server's `.env` or the Claude Desktop config `env` block,
   typed by the user into that file. **Keys never go in the chat.** Never ask
   for them, never read the file back into the conversation, and if the user
   pastes a key or secret anyway, tell them to delete the message and
   regenerate it at developers.kite.trade.
3. **Log in**: the `login` tool returns a Kite login link. The user opens it
   and signs in on Zerodha's own page (with their TOTP). Never ask for the
   Kite password, PIN or TOTP in the chat. A Kite session ends every day
   around 6 AM, so log in again each trading day.
4. **Set the tool permissions** (Customize > Connectors > kite, tool permissions):
   - `place_order`, `modify_order`, `cancel_order`, `place_gtt_order`,
     `modify_gtt_order`, `delete_gtt_order`: **Needs approval**. Never Always allow.
   - read-only tools (`get_profile`, `get_margins`, `get_holdings`,
     `get_positions`, `get_orders`, `get_order_history`, `get_order_trades`,
     `get_trades`, `get_quotes`, `get_ltp`, `get_ohlc`, `get_historical_data`,
     `search_instruments`, `get_gtts`): Always allow is fine.
5. Check it: `get_profile` (read only) should show the user's own Zerodha ID.

In Claude Code, keep the order tools on ask in `.claude/settings.json`:
`"permissions": {"ask": ["mcp__kite__place_order", "mcp__kite__modify_order", "mcp__kite__cancel_order"]}`,
and never put an order tool in `allow`. (The prefix is whatever name the
server has in your MCP config.)

**No order tools** (the hosted server): the ticket still shows the exact
order. After the user's yes, they place it themselves in the Kite app or
kite.zerodha.com, then Claude records the result with `ticket.py result`.

## Cover orders on Kite

trader-budget writes a **cover order** (`variety: "co"`, product MIS): the
buy and an SL-M stop-loss at `trigger_price` go in together, so the stop is at
the exchange from the first second. Kite only takes a trigger inside a range
it sets for each stock (a few percent from the price); outside it Kite
rejects the order with a message, and the plan needs a closer stop. Kite has
no take-profit leg, so `target_price` is the pack's own field: trader-alerts
rings there and the exit is a new ticket with a new yes. A cover order is
intraday: Zerodha squares it off itself from 3:12 PM (F&O stocks, closing
auction) or 3:25 PM (the rest), for a fee, if it is still open.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order per
   question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "yes to all", or a yes that names a different
   stock or quantity). Ask again. Never turn a reply into a yes yourself.
4. Only after "Recorded your yes":
   - paper: call `get_ltp` for the symbol, then `ticket.py result --index N
     --status placed --paper --filled-qty Q --filled-price <that price>`.
   - live: call `place_order` with exactly the arguments the ticket printed.
     Claude's card appears: Allow once or Deny. Then `ticket.py result --index N
     --status placed --broker-id <Kite's order_id>` (or `--status denied`, or
     `--status failed --note "<Kite's message>"`). Check the fill with
     `get_order_trades` and record it with `ticket.py fill`. A failed order is
     not retried without a new check and a new yes.
5. Set the alerts: `/trader-alerts` (`alerts.py add --from-plan`).

## Steps: while it is open, and out by flat-by

- trader-alerts watches the stop, the target and the flat-by time. The cover
  order's stop does the stopping at the exchange.
- When the stop-loss fills, record it: `ticket.py fill --symbol RELIANCE
  --side sell --qty 39 --price 1300.70` (the price from `get_order_trades`).
- At the target, or by `flat_by` (15:05 IST by default), get out:
  1. Check what is held: `get_positions` (read only).
  2. `ticket.py flatten --symbol RELIANCE --qty 39`. When today's entry was a
     cover order, the ticket is the EXIT of that cover order, not a new sell:
     a separate sell while the cover order's stop is still live could leave
     the user short when the stop fires.
  3. `/trader-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one ticket, as above.
  5. Exit a cover order: `get_orders`, find the stop-loss leg (its
     `parent_order_id` is the entry's order id, status TRIGGER PENDING), then
     `cancel_order` with `variety: "co"` and that leg's `order_id`. Kite cancels
     the stop and sells at market. For a plain MIS position, `place_order` with
     the SELL ticket. Then `ticket.py result` and `ticket.py fill`.

## Example: RELIANCE, Friday 2026-11-06

```
ORDER 1 of 1, PAPER (nothing goes to Kite)
  BUY 39 RELIANCE on NSE, market, intraday (MIS), good for today, cover order
  stop-loss trigger 1300.70 (sent with the order, sits at the exchange)
  target 1323.10 (watched by trader-alerts; Kite has no take-profit leg)
  max loss about ₹491.57 if the stop fills at 1300.70, charges included
  READY: trader-risk PASS, waiting for your yes
  The Kite call it would make:
    {"variety": "co", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
     "quantity": 39, "product": "MIS", "order_type": "MARKET", "trigger_price": 1300.7,
     "validity": "DAY", "tag": "daytrader"}
```

The user replies "yes". Paper: Claude reads `get_ltp NSE:RELIANCE` and
records the simulated fill. Live: Claude calls `place_order` with that JSON,
the card shows Allow once / Deny, and Allow once sends it to Kite. The `why`,
`target_price` and `plan` stay in the file; they are never sent.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys, secrets, passwords, PINs or TOTPs.
- Never switches `"paper"` to false. The user does that themselves.
- Never places F&O, commodity, MTF or short orders on its own, and never uses GTT for an intraday trade.
- Never promises a fill price: a market order and a stop fill at whatever the market gives.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation. Most
  people who trade intraday or F&O in India lose money (SEBI's own studies say so, year after year).
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, futures, margin (MTF), short selling or leverage unless the config allows it and the user confirms.
- Kite keys live in the MCP server's own config, never in the chat. Logins happen on Zerodha's page.
