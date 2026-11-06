#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker, for Zerodha Kite.
Standard library only.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket plus the
            exact arguments Claude will send to the Kite connector's
            place_order tool. A cover order shows its stop-loss, the target
            the pack watches, and the max loss at the stop including charges.
            An order is READY only if trader-risk passed THIS exact order
            today (matched by fingerprint) and nobody has decided on it yet.
  approve   records your explicit yes for ONE order, with your reply word for
            word. It refuses "yes to all", "sure", "ok", a yes that names a
            different stock or amount, and any order that is not READY.
  decline   records a no.
  result    records what happened after the Allow once / Deny card:
            placed (with Kite's order id), denied, or failed. In paper mode
            ("paper": true, the default) nothing goes to Kite: result --paper
            records a simulated fill instead.
  fill      records a fill in trader/portfolio.json (cash and shares).
  flatten   builds the ticket that closes a symbol's intraday position by
            flat_by: a SELL MIS MARKET, or, when today's entry was a cover
            order, the exit of that cover order (cancel_order, variety co, on
            its stop-loss leg: Kite then sells at market). It still goes
            through trader-risk and your yes.
  log       prints today's decisions.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py result --index 1 --status placed --broker-id 261106000123456
    python3 ticket.py result --index 1 --status placed --paper --filled-qty 39 --filled-price 1311.90
    python3 ticket.py fill --symbol RELIANCE --side buy --qty 39 --price 1312.00
    python3 ticket.py flatten --symbol RELIANCE --qty 39
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). Dates are
IST dates; --now "2026-11-06 09:51" pins the clock.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

KITE_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
               "order_type", "price", "trigger_price", "validity", "validity_ttl", "disclosed_quantity",
               "iceberg_legs", "iceberg_quantity", "tag")
NUMBER_FIELDS = ("quantity", "price", "trigger_price", "validity_ttl", "disclosed_quantity",
                 "iceberg_legs", "iceberg_quantity")
VALIDITY_WORDS = {"DAY": "good for today", "IOC": "immediate or cancel", "TTL": "for a few minutes"}
PRODUCT_WORDS = {"MIS": "intraday (MIS)", "CNC": "delivery (CNC)", "NRML": "NRML", "MTF": "margin (MTF)"}
YES_WORDS = ("yes", "y", "haan", "ha")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each", "sab"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if", "nahi", "ruko"}
IST = datetime.timedelta(hours=5, minutes=30)   # India Standard Time: UTC+5:30, no daylight saving


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- time (IST, no tz files)

def to_ist(utc):
    """Naive UTC -> naive IST. India is UTC+5:30 all year (no daylight saving)."""
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    """Naive IST -> naive UTC."""
    return local - IST


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TicketError("--now wants a time like \"2026-11-06 09:51\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def ist_iso(now_utc):
    """naive UTC -> "2026-11-06T09:51:00+05:30" (IST wall clock with its offset)."""
    return "%s+05:30" % to_ist(now_utc).replace(microsecond=0).isoformat()


ny_iso = ist_iso   # the v2 name


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a decision belongs to: its "date" (the day it was made under), else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as trader-risk/check.py: identifies the exact order."""
    core = {k: order[k] for k in KITE_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _str_num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else repr(f)
    return str(v)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _num_out(v):
    f = _f(v)
    if f is None:
        return v
    return int(f) if f == int(f) else f


def tool_input(order):
    """The exact arguments for the Kite connector's place_order: Kite's own
    parameter names, numbers as numbers. "target_price", "why", "plan" and
    "exit_cover" are the pack's and are never sent."""
    out = {}
    for k in KITE_FIELDS:
        if k in order and order[k] is not None:
            out[k] = _num_out(order[k]) if k in NUMBER_FIELDS else order[k]
    return out


def order_symbol(order):
    """RELIANCE (NSE) or BSE:X, the pack's way of writing a ticker."""
    ts = str(order.get("tradingsymbol") or order.get("symbol") or "").strip().upper()
    ex = str(order.get("exchange") or "NSE").strip().upper()
    if not ts:
        return "?"
    return ts if ex == "NSE" else "%s:%s" % (ex, ts)


def order_side(order):
    return str(order.get("transaction_type") or order.get("side") or "").strip().lower()


def _money(v):
    f = float(v)
    s = "{:,.2f}".format(f)
    return "₹" + (s[:-3] if s.endswith(".00") else s)


def _p(v):
    return "%.2f" % float(v)


def is_cover(order):
    return str(order.get("variety") or "").lower() == "co"


is_bracket = is_cover   # the v2 name


def max_loss(order):
    """(rupees, stop) lost if the stop fills exactly at the stop, charges included
    when trader-budget worked them out, or (None, stop)."""
    plan = order.get("plan") or {}
    stop = (_f(order.get("trigger_price")) if is_cover(order) else None) or _f(plan.get("stop"))
    q = _f(order.get("quantity", order.get("qty")))
    if not stop or not q:
        return None, stop
    if str(order.get("order_type", "MARKET")).upper() == "LIMIT" and _f(order.get("price")):
        entry = _f(order["price"])
        if _f(plan.get("max_loss")) is not None and _f(plan.get("entry")) and abs(_f(plan["entry"]) - entry) < 0.005:
            return round(_f(plan["max_loss"]), 2), stop
    elif _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    risk = abs(entry - stop)
    return round(q * int(round(risk * 100)) / 100.0, 2), stop


def plain_parts(order):
    """The ticket in plain words, one part per line."""
    side = order_side(order).upper()
    sym = order_symbol(order)
    ex = str(order.get("exchange") or "NSE").upper()
    typ = str(order.get("order_type", "MARKET")).upper()
    q = _str_num(order.get("quantity", order.get("qty")))
    product = PRODUCT_WORDS.get(str(order.get("product") or "").upper(), order.get("product") or "?")
    val = VALIDITY_WORDS.get(str(order.get("validity") or "DAY").upper(), order.get("validity"))
    if order.get("exit_cover"):
        return ["EXIT the %s cover order: %s %s share%s of %s at market, %s" % (
                    sym, side, q, "" if q == "1" else "s", sym, product),
                "Kite exits a cover order when its stop-loss leg is cancelled: the stop goes away and the "
                "shares are sold at market"]
    if typ == "LIMIT":
        how = "limit %s" % _p(order.get("price"))
    elif typ == "SL":
        how = "stop-loss limit: trigger %s, limit %s" % (_p(order.get("trigger_price")), _p(order.get("price")))
    elif typ == "SL-M" and not is_cover(order):
        how = "stop-loss market: trigger %s" % _p(order.get("trigger_price"))
    else:
        how = "market"
    head = "%s %s %s on %s, %s, %s, %s" % (side, q, sym if ex == "NSE" else sym.split(":", 1)[-1], ex, how,
                                           product, val)
    if not is_cover(order):
        return [head]
    parts = [head + ", cover order"]
    if order.get("trigger_price") is not None:
        parts.append("stop-loss trigger %s (sent with the order, sits at the exchange)" % _p(order["trigger_price"]))
    if order.get("target_price") is not None:
        parts.append("target %s (watched by trader-alerts; Kite has no take-profit leg)" % _p(order["target_price"]))
    loss, stop = max_loss(order)
    if loss is not None:
        parts.append("max loss about %s if the stop fills at %s, charges included (it can fill worse in a "
                     "fast market)" % (_money(loss), _p(stop)))
    return parts


def plain(order):
    """BUY 39 RELIANCE on NSE, market, intraday (MIS), good for today, cover order / stop-loss trigger 1300.70 / ..."""
    return " / ".join(plain_parts(order))


def amount_numbers(order):
    nums = []
    for k in ("quantity", "qty", "price", "trigger_price", "target_price"):
        if order.get(k) is not None:
            v = _f(order[k])
            if v is not None:
                nums.append(v)
    loss, _ = max_loss(order)
    if loss is not None:
        nums.append(loss)
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = order_symbol(order).upper()
    tokens = set(re.findall(r"[A-Z][A-Z0-9&.\-]{0,19}", raw.upper()))
    tokens |= {t.split(":", 1)[-1] for t in re.findall(r"[A-Z]{3}:[A-Z0-9&.\-]+", raw.upper())}
    others = {str(s).upper() for s in batch_symbols} - {sym}
    others |= {s.split(":", 1)[-1] for s in others}
    others.discard(sym.split(":", 1)[-1])
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(_str_num(n) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for the Kite call (or the paper fill)" % rec.get("at"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    """entry or exit, as trader-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if order_side(order) == "buy" else "exit"


# ---------------------------------------------------------------- rendering

def call_for(order):
    """(tool, arguments) Claude calls for this ticket on the Kite connector."""
    if order.get("exit_cover"):
        return "cancel_order", {"variety": "co", "order_id": str(order["exit_cover"].get("order_id") or
                                                                 "<the stop-loss leg's order_id>")}
    return "place_order", tool_input(order)


def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    paper = cfg.get("paper", True)
    acct = "PAPER (nothing goes to Kite)" if paper else "LIVE, ZERODHA KITE, REAL MONEY"
    lines = ["ORDER %d of %d, %s" % (idx, total, acct)]
    for part in plain_parts(order):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    tool, args = call_for(order)
    if paper:
        lines.append("  Paper mode: Claude does NOT call Kite. After your yes it records a simulated fill at")
        lines.append("  the price Kite's get_ltp shows then (ticket.py result --paper). Set \"paper\": false in")
        lines.append("  trader/config.json to trade for real. The Kite call it would make:")
    else:
        lines.append("  Claude will call the Kite connector's %s with exactly:" % tool)
    for l in json.dumps(args, indent=2).splitlines():
        lines.append("    " + l)
    if order.get("exit_cover"):
        lines.append("  Find the stop-loss leg first: get_orders, the order whose parent_order_id is %s"
                     % (order["exit_cover"].get("parent_order_id") or "the entry's order id"))
        lines.append("  and status TRIGGER PENDING. Its order_id goes in the call above.")
    elif is_cover(order):
        lines.append("  A cover order buys and places the stop-loss (SL-M at the trigger) together. Kite only")
        lines.append("  takes a trigger inside its allowed range for the stock; outside it, Kite rejects the order.")
    if ready:
        lines.append("  Reply \"yes\" to %s this one order, or \"no\" to skip it." % ("paper-trade" if paper else "place"))
        if not paper:
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    loss, stop = max_loss(order)
    tool, args = call_for(order)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order),
            "fingerprint": fingerprint(order), "paper": bool(cfg.get("paper", True)),
            "broker": cfg.get("broker", "kite"), "tool": tool, "tool_input": args,
            "why": order.get("why", ""), "cover": is_cover(order), "target": order.get("target_price"),
            "max_loss": loss, "stop": stop}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date):
    """Record a filled order in trader/portfolio.json: cash and shares, with a
    cost-basis lot per buy (oldest lots are relieved first on a sell)."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    symbol = order_symbol({"tradingsymbol": symbol.split(":", 1)[-1],
                           "exchange": symbol.split(":", 1)[0] if ":" in symbol else "NSE"})
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    cost = round(qty * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker", "")).upper() == symbol.upper():
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0.0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        pf["cash"] = round(float(pf.get("cash", 0)) - cost, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (_str_num(qty), symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost, 2)
    write_json(portfolio_path, pf)
    return pf


def log_fill(state_path, now, today, symbol, side, qty, price):
    """A fill in the log, so trader-risk can count day trades (a buy and a sell the same day)."""
    state = load_state(state_path)
    state.setdefault("orders", []).append({
        "at": ny_iso(now), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
        "qty": _str_num(float(qty)), "price": float(price),
        "plain": "%s %s %s at %s" % (side.upper(), _str_num(float(qty)), symbol.upper(), _p(price))})
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-11-06")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [order_symbol(o) for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, stop = max_loss(order)
    paper = bool(cfg.get("paper", True))
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": order_symbol(order), "side": order_side(order), "variety": order.get("variety", "regular"),
           "product": order.get("product"), "qty": order.get("quantity", order.get("qty")),
           "kind": kind_of(order, rv), "max_loss": loss, "stop": stop, "target": order.get("target_price"),
           "plain": plain(order), "status": "approved", "reply": a.reply, "paper": paper, "broker": "kite"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order)))
    tool, args = call_for(order)
    if paper:
        print("PAPER: do not call Kite. Read the price now (Kite get_ltp %s:%s, or the plan's entry) and record "
              "the simulated fill:" % (order.get("exchange", "NSE"), order.get("tradingsymbol", "")))
        print("  ticket.py result --index %d --status placed --paper --filled-qty %s --filled-price PRICE"
              % (a.index, _str_num(rec["qty"])))
    else:
        print("Now call the Kite connector's %s with exactly:" % tool)
        print(json.dumps(args, indent=2))
        print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
              "placed|denied|failed --broker-id <Kite's order_id>" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": order_symbol(order), "side": order_side(order),
           "plain": plain(order), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    if a.paper and a.status == "placed" and not (a.filled_qty and a.filled_price):
        raise TicketError("a paper fill needs --filled-qty and --filled-price (the price Kite's get_ltp shows now)")
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status, "paper": bool(a.paper or rec.get("paper"))}
    for k in ("qty", "kind", "max_loss", "stop", "target", "variety", "product"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today)
        log_fill(a.state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price)
        print("Added the %sfill to %s." % ("paper " if new["paper"] else "", a.portfolio))
        if rec.get("kind") == "entry" and rec.get("target"):
            print("Watch the stop and the target: alerts.py add --from-plan (and alerts.py check every few minutes).")
    elif a.status == "placed":
        print("When it fills (Kite get_order_trades %s), record it: ticket.py fill --symbol %s --side %s --qty Q "
              "--price P" % (a.broker_id or "<order_id>", rec["symbol"], rec["side"]))
        if rec.get("kind") == "entry" and is_cover(order):
            print("The stop-loss leg sits at the exchange. When it fills, or you exit at the target, record that "
                  "sell the same way.")
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now = parse_now(a.now)
    date = a.date or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-11-06")
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date)
    log_fill(a.state, now, date, a.symbol, a.side, a.qty, a.price)
    print("Recorded %s %s %s at %s on %s in %s. Cash now %s." % (a.side, a.qty, a.symbol.upper(),
                                                                _money(a.price), date, a.portfolio,
                                                                _money(pf["cash"])))
    return 0


def _today_cover(state, sym, today):
    """The cover-order entry placed today for this symbol, if any: {order_id, qty}."""
    found = None
    for rec in (state or {}).get("orders", []):
        if (rec.get("status") == "placed" and rec_day(rec) == today and rec.get("kind") == "entry"
                and str(rec.get("variety") or "").lower() == "co" and str(rec.get("symbol", "")).upper() == sym):
            found = rec
    return found


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    raw = a.symbol.strip().upper()
    ex, ts = ("BSE", raw[4:]) if raw.startswith("BSE:") else ("NSE", raw[4:] if raw.startswith("NSE:") else raw)
    sym = ts if ex == "NSE" else "BSE:" + ts
    now, today = _clock(a)
    source = "Kite, get_positions"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0 or qty != int(qty):
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 39")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if str(p.get("ticker") or p.get("symbol") or "").upper() == sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check Kite (get_positions) and pass "
                              "--qty if you do hold some" % (sym, a.portfolio))
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == sym else None) \
        or cfg.get("flat_by") or "15:05"
    order = {"variety": "regular", "exchange": ex, "tradingsymbol": ts, "transaction_type": "SELL",
             "quantity": int(qty), "product": "MIS", "order_type": "MARKET", "validity": "DAY", "tag": "daytrader",
             "why": "flat by %s IST: closes the %s day trade before Zerodha's intraday square-off (qty from %s)"
             % (flat_by, sym, source)}
    state = load_state(a.state)
    co = _today_cover(state, sym, today)
    if co and not a.plain_sell:
        order["variety"] = "co"
        order["exit_cover"] = {"parent_order_id": co.get("broker_order_id"), "order_id": None}
        order["why"] += "; today's entry was a cover order (Kite id %s), so this exits it" % (
            co.get("broker_order_id") or "not recorded")
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": bool(cfg.get("paper", True)),
           "note": "Flatten ticket. Nothing is placed until trader-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s IST)." % (sym, plain(order), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against Kite's get_positions before you say yes." % source)
    print("Next:")
    print("  1. trader-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    if co and not a.plain_sell:
        print("  3. This position came from a cover order, so do NOT send a separate sell: its stop-loss leg is")
        print("     still at the exchange and a sell on top would leave you short when it fires. Exit the cover")
        print("     order instead: get_orders, find the leg with parent_order_id %s (status TRIGGER PENDING)," % (
            co.get("broker_order_id") or "<the entry's order id>"))
        print("     then cancel_order with variety \"co\" and that leg's order_id. Kite sells at market.")
    else:
        print("  3. If a stop-loss order for these shares is still open, cancel it first (get_orders, then")
        print("     cancel_order), or the sell plus the stop could leave you short. Then place_order with the ticket.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % _money(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-9s %s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes for Zerodha Kite (trader-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-06 09:51\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened after the Allow once / Deny card (or a paper fill)")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--broker-id", help="Kite's order_id")
    p.add_argument("--paper", action="store_true", help="a simulated fill: nothing went to Kite")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--date", help="YYYY-MM-DD (default: today in IST)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="a sell ticket that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from Kite's get_positions (default: trader/portfolio.json)")
    p.add_argument("--plain-sell", action="store_true", help="a SELL MIS MARKET even if today's entry was a cover order")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
