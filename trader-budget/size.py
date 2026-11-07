#!/usr/bin/env python3
"""Position sizing for trader-budget, NSE / BSE in rupees. Standard library only, no network.

Default mode: sizes ONE intraday trade from trader/plan.json (written by
trader-exit) to your budget and your max loss per trade, then writes one
order to trader/orders.json in the shape of the Kite connector's place_order:
a cover order (variety "co", product MIS) that buys and puts the stop-loss
at the exchange in one go. Kite has no take-profit leg, so the target rides
along for trader-alerts and trader-broker. It never places anything:
trader-risk checks the order and trader-broker asks you yes or no.

    python3 size.py                                   # trader/plan.json + trader/config.json
    python3 size.py --write                           # write trader/orders.json
    python3 size.py --limit                           # a limit order at the plan's entry
    python3 size.py --limit 1311.90 --write           # a limit order at 1311.90
    python3 size.py --now "2026-11-06 09:51" --json   # pin the clock (IST)

How many shares. Whole shares only (NSE has no fractional shares).

    shares = the smallest of
      max loss       the most shares whose loss at the stop PLUS the round
                     trip's charges stays within max_loss_per_trade_inr
      position cap   budget x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than the cash in trader/portfolio.json, if you have one)

The cash limit is the full value of the shares: no intraday leverage, even
though MIS would lend it, unless you set "use_mis_leverage": true (then the
cash limit is the margin, value x mis_margin_pct / 100, 20 by default; the
max-loss limit does not change, so leverage never raises what you can lose
at the stop).

Charges (Zerodha equity intraday, the defaults; override any of them with
"charges" in the config): brokerage 0.03% or Rs 20 per executed order,
whichever is lower; STT 0.025% on the sell; NSE transaction charges 0.00307%
(BSE 0.00375%); SEBI Rs 10 a crore; stamp duty 0.003% on the buy; GST 18% on
brokerage, transaction and SEBI charges. They are an estimate: the contract
note is the bill.

The entry it sizes at: a limit order's limit price; for a market order the
plan's entry, or the price in trader/prices.json if that is fresh (under 15
minutes old) and higher, because a price that ran up since the plan means a
bigger loss at the same stop. Zero shares (the stop is too far away for your
max loss) is a plain refusal: no order is written. So is a reward:risk under
min_reward_risk (the price ran toward the target since the plan).

Weights mode (kept from v1, no longer the headline): split a budget across a
list of stocks by weight, as delivery (CNC) market orders.

    python3 size.py --weights RELIANCE=30,HDFCBANK=25,INFY=20,TCS=15,ITC=10 --budget 200000
    python3 size.py --targets my-weights.json --budget 200000 --write
"""

import argparse
import datetime
import json
import math
import os
import sys

# The defaults: the same keys as config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 80000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 500,
    "max_daily_loss_inr": 1500,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:45",
    "flat_by": "15:05",
    "fractional": False,
    "broker": "kite",
    "exchange": "NSE",
    "product": "MIS",
    "paper": True,
    "use_mis_leverage": False,
    "mis_margin_pct": 20,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "allow_short": False,
    "charges": {},
}
CHARGES = {  # Zerodha equity intraday (zerodha.com/charges, read 2026-11-06)
    "brokerage_pct": 0.03, "brokerage_max": 20.0, "stt_sell_pct": 0.025,
    "txn_pct_nse": 0.00307, "txn_pct_bse": 0.00375, "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.003, "gst_pct": 18.0,
}
EPS = 1e-9
FRESH_MINUTES = 15
IST = datetime.timedelta(hours=5, minutes=30)   # India Standard Time: UTC+5:30, no daylight saving
SETUP_WORDS = {"orb": "opening-range breakout", "level": "close above a level", "flag": "flag",
               "bounce": "oversold bounce"}
TAG = "daytrader"


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- time (IST, no tz files)

def to_ist(utc):
    """Naive UTC -> naive IST. India is UTC+5:30 all year."""
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    return local - IST


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise SizeError("--now wants a time like \"2026-11-06 09:51\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-11-06T04:21:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def split_symbol(sym):
    """RELIANCE -> (NSE, RELIANCE); BSE:500325 -> (BSE, 500325); RELIANCE.NS -> (NSE, RELIANCE)."""
    s = str(sym or "").strip().upper()
    for pre, ex in (("NSE:", "NSE"), ("BSE:", "BSE")):
        if s.startswith(pre):
            return ex, s[4:]
    if s.endswith(".NS"):
        return "NSE", s[:-3]
    if s.endswith(".BO"):
        return "BSE", s[:-3]
    return "NSE", s


def plain_symbol(sym):
    ex, ts = split_symbol(sym)
    return ts if ex == "NSE" else "BSE:" + ts


def parse_weights(data):
    """Accept a weights file, a {ticker: weight} dict or a list of rows.

    Returns [{"ticker", "name", "weight"}] with weight as a float."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"ticker": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("ticker") or r.get("symbol") or r.get("tradingsymbol")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a ticker and a weight" % i)
            rows.append({"ticker": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["ticker"], r["weight"]))
        out.append({"ticker": plain_symbol(r["ticker"]), "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    """RELIANCE=30,HDFCBANK=25 -> {ticker: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants TICKER=WEIGHT pairs, got %r" % part)
        t, w = part.split("=", 1)
        out[t.strip().upper()] = w.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in prices.items():
        try:
            out[plain_symbol(t)] = float(p)
        except (TypeError, ValueError):
            continue
    return out


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, fetched_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


def cfg_inr(c, key):
    """A rupee limit from the config: key_inr."""
    return _f(c.get(key + "_inr"))


# ---------------------------------------------------------------- money helpers

def round_cent(x):
    """Round a price to the paisa (half up)."""
    return math.floor(float(x) * 100 + 0.5 + EPS) / 100.0


def shares_for(inr, per_share):
    """Whole shares that fit in inr at per_share, in exact integer paise."""
    u, p = int(round(float(inr) * 100)), int(round(float(per_share) * 100))
    if p <= 0 or u <= 0:
        return 0
    return u // p


def _price(x):
    """1300.7 -> "1300.70"."""
    return "%.2f" % round_cent(x)


def _num(x):
    """1250 -> "1250", 1118.13 -> "1118.13"."""
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def indian(n):
    """1234567 -> "12,34,567": lakh and crore grouping."""
    s = str(int(n))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def money(x):
    """Rupees with Indian grouping: ₹1,00,000 or ₹42,618.20."""
    x = float(x)
    neg, x = x < 0, abs(x)
    whole = int(math.floor(x + EPS))
    paise = int(round((x - whole) * 100))
    if paise == 100:
        whole, paise = whole + 1, 0
    out = "₹" + indian(whole) + ("" if paise == 0 else ".%02d" % paise)
    return "-" + out if neg else out


def charges(qty, buy, sell, exchange="NSE", over=None):
    """Zerodha equity intraday charges for a round trip (buy qty at buy, sell at sell)."""
    k = dict(CHARGES)
    k.update(over or {})
    bv, sv = qty * float(buy), qty * float(sell)
    brk = sum(min(k["brokerage_max"], v * k["brokerage_pct"] / 100.0) for v in (bv, sv) if v > 0)
    stt = round(sv * k["stt_sell_pct"] / 100.0)                  # STT is rounded to the rupee
    txn = (bv + sv) * k["txn_pct_bse" if exchange == "BSE" else "txn_pct_nse"] / 100.0
    sebi = (bv + sv) * k["sebi_per_crore"] / 1e7
    stamp = round(bv * k["stamp_buy_pct"] / 100.0)               # so is the stamp duty
    gst = (brk + txn + sebi) * k["gst_pct"] / 100.0
    parts = {"brokerage": brk, "stt": stt, "transaction": txn, "sebi": sebi, "stamp": stamp, "gst": gst}
    parts = {n: round(v, 2) for n, v in parts.items()}
    parts["total"] = round(sum(parts.values()), 2)
    return parts


# ---------------------------------------------------------------- plan mode (the default)

def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a limit order (None = market)
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in trader/portfolio.json, if there is one
    today  IST date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = plain_symbol(plan.get("symbol"))
    if not sym:
        raise SizeError("the plan has no symbol (run trader-exit)")
    exchange, tsym = split_symbol(sym)
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s (IST): make a new plan with trader-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit); a cover order needs entry, "
                            "stop and target" % name)
    blocked = {plain_symbol(b) for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    if limit is not None:
        ref, ref_from = round_cent(limit), "limit"
    elif price is not None and round_cent(price) > round_cent(entry_plan):
        ref, ref_from = round_cent(price), "price"
    else:
        ref, ref_from = round_cent(entry_plan), "plan"
    stop, target = round_cent(stop), round_cent(target)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (_price(target), _price(ref)))
    risk_c = int(round((ref - stop) * 100))
    reward_c = int(round((target - ref) * 100))
    risk = risk_c / 100.0
    over = c.get("charges") or {}

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget), money(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (money(cash), money(cash_buffer))
    available = max(round(available, 2), 0.0)
    lev = bool(c.get("use_mis_leverage")) and str(c.get("product") or "MIS").upper() == "MIS"
    margin_pct = float(c.get("mis_margin_pct") or 20) if lev else 100.0

    limits = []
    max_loss = cfg_inr(c, "max_loss_per_trade")
    if max_loss:
        q = shares_for(max_loss, risk)
        while q > 0 and q * risk + charges(q, ref, stop, exchange, over)["total"] > max_loss + EPS:
            q -= 1
        limits.append({"name": "max loss", "inr": max_loss, "per": risk, "shares": q,
                       "how": "%s max loss / (%s risk a share + charges)" % (money(max_loss), _price(risk))})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "inr": cap, "per": ref, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget), money(cap), _price(ref))})
    max_order = cfg_inr(c, "max_order")
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (money(max_order), _price(ref))})
    per = ref * margin_pct / 100.0
    limits.append({"name": "cash", "inr": available, "per": per, "shares": shares_for(available, per),
                   "how": "%s = %s / %s%s" % (cash_note, money(available), _price(per),
                                              (" (MIS margin %s%%)" % _num(margin_pct)) if lev else "")})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = round(qty * ref, 2)
    at_stop = round(qty * risk_c / 100.0, 2)
    ch_stop = charges(qty, ref, stop, exchange, over) if qty else charges(0, ref, stop, exchange, over)
    ch_target = charges(qty, ref, target, exchange, over) if qty else ch_stop
    loss = round(at_stop + ch_stop["total"], 2)
    gain = round(qty * reward_c / 100.0 - ch_target["total"], 2)
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            one = risk + charges(1, ref, stop, exchange, over)["total"]
            refusal = ("one share risks %s at the stop (entry %s minus stop %s) plus about %s of charges, "
                       "more than your %s max loss per trade. No order. Skip this trade, or plan a closer stop "
                       "with trader-exit." % (money(risk), _price(ref), _price(stop), money(one - risk),
                                              money(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and reward_c < _f(c["min_reward_risk"]) * risk_c - EPS:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so trader-risk "
                   "would block it. No order. Skip it, or make a new plan with trader-exit."
                   % (reward_c / float(risk_c), _price(target), _price(ref), _price(stop), _num(c["min_reward_risk"])))
    elif cost < float(cfg_inr(c, "min_order") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, money(cost), money(cfg_inr(c, "min_order"))))
    elif gain <= 0:
        refusal = ("at %d share%s the target makes %s and the round trip's charges are %s: the trade "
                   "cannot pay for itself. No order." % (qty, "" if qty == 1 else "s",
                                                        money(qty * reward_c / 100.0), money(ch_target["total"])))
    return {
        "symbol": sym, "exchange": exchange, "tradingsymbol": tsym, "side": "buy",
        "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": round_cent(entry_plan),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_risk": round(reward_c / float(risk_c), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "cost": cost, "loss_at_stop": at_stop, "charges_at_stop": ch_stop, "charges_at_target": ch_target,
        "max_loss": loss, "gain_at_target": gain,
        "margin": round(cost * margin_pct / 100.0, 2), "mis_leverage": lev,
        "flat_by": plan.get("flat_by") or c.get("flat_by"), "product": str(c.get("product") or "MIS").upper(),
        "order_type": "limit" if limit is not None else "market", "refusal": refusal,
    }


def cover_order(s):
    """The one proposed order: the Kite connector's place_order arguments for a
    cover order (buy + a stop-loss at the exchange), plus the pack's own keys
    (target, why, plan) that are never sent to Kite."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    o = {"variety": "co", "exchange": s["exchange"], "tradingsymbol": s["tradingsymbol"],
         "transaction_type": "BUY", "quantity": int(s["qty"]), "product": "MIS",
         "order_type": "LIMIT" if s["order_type"] == "limit" else "MARKET", "validity": "DAY",
         "trigger_price": round_cent(s["stop"]), "tag": TAG}
    if s["order_type"] == "limit":
        o["price"] = round_cent(s["entry"])
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    why = ("%s on %s: %d shares, bound by your %s (%d x %s = %s at the stop + %s charges = %s)"
           % (setup, s["symbol"], s["qty"], {"max loss": "%s max loss per trade" % money(s["max_loss_per_trade_inr"] or 0),
                                             "position cap": "position cap", "max order": "max order",
                                             "cash": "cash after the buffer"}[s["bound_by"]],
              s["qty"], _price(s["risk_per_share"]), money(s["loss_at_stop"]),
              money(s["charges_at_stop"]["total"]), money(s["max_loss"])))
    o.update({
        "target_price": round_cent(s["target"]),
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "loss_at_stop": s["loss_at_stop"],
                 "charges": s["charges_at_stop"]["total"], "max_loss": s["max_loss"],
                 "gain_at_target": s["gain_at_target"], "setup": s["setup"],
                 "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
    })
    return o


bracket_order = cover_order   # the v2 name


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, _price(s["entry_plan"]),
                                            (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f."
                 % (_price(s["entry"]), src, _price(s["risk_per_share"]), _price(s["entry"]), _price(s["stop"]),
                    s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-62s %5d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    ch = s["charges_at_stop"]
    lines.append("Size: %d shares of %s = %s at %s%s." % (
        s["qty"], s["symbol"], money(s["cost"]), _price(s["entry"]),
        (", margin %s (MIS)" % money(s["margin"])) if s["mis_leverage"] else ""))
    lines.append("Max loss at the stop: %d x %s = %s, plus about %s of charges = %s."
                 % (s["qty"], _price(s["risk_per_share"]), money(s["loss_at_stop"]), money(ch["total"]),
                    money(s["max_loss"])))
    lines.append("  charges if stopped: brokerage %s, STT %s, exchange %s, SEBI %s, stamp %s, GST %s"
                 % tuple(money(ch[k]) for k in ("brokerage", "stt", "transaction", "sebi", "stamp", "gst")))
    lines.append("At the target: %s after %s of charges." % (money(s["gain_at_target"]),
                                                           money(s["charges_at_target"]["total"])))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % _price(s["stop"]))
    lines.append("")
    lines.append("Proposed cover order (the Kite connector's place_order arguments; nothing has been placed):")
    how = "LIMIT %s" % _price(order["price"]) if order["order_type"] == "LIMIT" else "MARKET"
    lines.append("  BUY %d %s:%s, %s, MIS, cover order with a stop-loss trigger at %s"
                 % (order["quantity"], order["exchange"], order["tradingsymbol"], how, _price(order["trigger_price"])))
    lines.append("  Target %s is not an order (Kite has no take-profit leg): trader-alerts rings there and "
                 "trader-broker builds the exit." % _price(order["target_price"]))
    if s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets closed (trader-alerts rings, "
                     "trader-broker flatten), before Zerodha's intraday square-off does it for a fee."
                     % s["flat_by"])
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode (v1)

def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {str(b).upper() for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["ticker"])
        seen.add(r["ticker"])
        if r["ticker"] in blocked:
            dropped.append({"ticker": r["ticker"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"ticker": r["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"ticker": r["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact (unrounded) rupees per ticker with the cap enforced.

    Proportional to weight; any stock over the cap is pinned at the cap and
    the rest of the money is shared over the others, repeated until no stock
    is over. Returns ({ticker: rupees}, set of capped tickers)."""
    alloc, capped = {}, set()
    active = [r for r in rows]
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["ticker"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["ticker"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["ticker"]] = cap
            capped.add(r["ticker"])
            money_left -= cap
        active = [r for r in active if r["ticker"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["ticker"]] = 0.0
            break
    return alloc, capped


def round_to_unit(exact, capped, order, cap, unit):
    """Round to whole rupees (unit 1) or paise (unit 0.01) and keep the total.

    The total placed is the exact total rounded DOWN to the unit, so it never
    spends money you do not have. Each slice is rounded to the nearest unit,
    then the difference (usually one unit) goes to the biggest stock that is
    not capped, then the next, never pushing anyone over the cap."""
    def units(x):
        return int(math.floor(x / unit + EPS))

    cap_u = units(cap)
    total_u = units(sum(exact.values()))
    out = {}
    for t, v in exact.items():
        out[t] = cap_u if t in capped else int(math.floor(v / unit + 0.5 + EPS))
    diff = total_u - sum(out.values())
    takers = [t for t in order if t not in capped]
    guard = 0
    while diff != 0 and takers and guard < 100000:
        moved = False
        for t in takers:
            if diff == 0:
                break
            if diff > 0 and out[t] + 1 <= cap_u:
                out[t] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[t] > 0:
                out[t] -= 1
                diff += 1
                moved = True
        if not moved:
            break
        guard += 1
    return {t: round(u * unit, 2) for t, u in out.items()}


def whole_shares(rupees, prices, order, cap, cash):
    """Whole shares: floor each slice, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {}
    for t in order:
        shares[t] = int(math.floor(rupees[t] / prices[t] + EPS)) if rupees[t] > 0 else 0
    spent = sum(shares[t] * prices[t] for t in order)
    left = cash - spent
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = rupees[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, budget=None, prices=None, whole=None, cents=False, top=None):
    """Weights mode. Pure function: weights + rules -> the full sizing result (a dict)."""
    budget = float(budget if budget is not None else (cfg.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(cfg.get("cash_buffer_pct", 10))
    cap_pct = float(cfg.get("max_position_pct", 25))
    min_order = float(cfg_inr(cfg, "min_order") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    if whole is None:
        whole = not cfg.get("fractional", False)
    prices = prices or {}

    keep, dropped = normalise(rows, cfg.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100.0, 2)
    unit = 0.01 if (cents or whole) else 1.0

    if whole:
        missing = [r["ticker"] for r in keep if not prices.get(r["ticker"])]
        if missing:
            raise SizeError("whole shares need a price for: %s (add them to trader/prices.json, "
                            "e.g. with trader-alerts: alerts.py quote %s --write)"
                            % (", ".join(missing), " ".join(missing)))

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["ticker"] for r in sorted(live, key=lambda r: -exact[r["ticker"]])]
        rupees = round_to_unit(exact, capped, order, cap, unit)
        shares = None
        if whole:
            shares = whole_shares(rupees, prices, order, cap, sum(rupees.values()))
            rupees = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if rupees[r["ticker"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["ticker"]))
        if whole and shares is not None and shares[worst["ticker"]] == 0 and prices.get(worst["ticker"], 0) > 0:
            reason = ("one share costs ₹%s, more than its slice; it cannot be bought in whole shares"
                      % _num(prices[worst["ticker"]]))
        else:
            reason = "its slice (₹%s) is under your ₹%s minimum order" % (
                _num(rupees[worst["ticker"]]), _num(min_order))
        dropped.append({"ticker": worst["ticker"], "reason": reason})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")

    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["ticker"]
        p = {"ticker": t, "name": r["name"], "weight": round(r["weight"], 2),
             "source_weight": r["source_weight"], "rupees": rupees[t],
             "capped": t in capped}
        if prices.get(t):
            p["price"] = prices[t]
            p["shares"] = shares[t] if whole else round(rupees[t] / prices[t], 4)
        positions.append(p)
    invested = round(sum(p["rupees"] for p in positions), 2)
    return {
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "investable": investable, "max_position_pct": cap_pct, "cap": cap,
        "min_order_inr": min_order, "whole_shares": bool(whole),
        "positions": positions, "invested": invested,
        "idle": round(investable - invested, 2), "dropped": dropped,
    }


def orders_from(result, source=""):
    """Weights mode: proposed delivery (CNC) market buys, one per position, in the
    Kite connector's place_order shape. Whole shares only."""
    out = []
    for p in result["positions"]:
        if p["rupees"] <= 0:
            continue
        why = "%.2f%% of the split" % p["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_num(result["max_position_pct"]), money(result["budget"]))
        if not result["whole_shares"] or not p.get("shares"):
            continue
        ex, ts = split_symbol(p["ticker"])
        out.append({"variety": "regular", "exchange": ex, "tradingsymbol": ts, "transaction_type": "BUY",
                    "quantity": int(p["shares"]), "product": "CNC", "order_type": "MARKET", "validity": "DAY",
                    "tag": TAG, "why": why})
    return out


def report(result, orders, source=""):
    """Weights mode table."""
    r = result
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (money(r["budget"]), _num(r["cash_buffer_pct"]), money(r["cash_buffer"]),
                    money(r["investable"]), _num(r["max_position_pct"]), money(r["cap"])))
    lines.append("")
    has_price = any("price" in p for p in r["positions"])
    head = "%-12s %-22s %8s %13s" % ("ticker", "name", "weight", "rupees")
    if has_price:
        head += " %10s %10s" % ("price", "shares")
    lines.append(head + "  note")
    for p in r["positions"]:
        row = "%-12s %-22s %7.2f%% %13s" % (p["ticker"], (p["name"] or "")[:22], p["weight"], money(p["rupees"]))
        if has_price:
            row += " %10s %10s" % (money(p["price"]) if "price" in p else "", p.get("shares", ""))
        if p["capped"]:
            row += "  capped at %s%%" % _num(r["max_position_pct"])
        lines.append(row)
    lines.append("%-12s %-22s %8s %13s" % ("", "invested", "", money(r["invested"])))
    lines.append("%-12s %-22s %8s %13s" % ("", "cash buffer", "", money(r["cash_buffer"])))
    lines.append("%-12s %-22s %8s %13s" % ("", "left idle", "", money(r["idle"])))
    if r["idle"] >= 1:
        lines.append("")
        lines.append("Left idle %s: %s" % (money(r["idle"]), (
            "whole shares do not divide evenly" if r["whole_shares"]
            else "every stock is at its cap; add stocks or raise max_position_pct")))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["ticker"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (the Kite connector's place_order arguments, nothing has been placed):")
    for o in orders:
        lines.append("  BUY %d %s:%s, MARKET, CNC (delivery), DAY" % (o["quantity"], o["exchange"], o["tradingsymbol"]))
    lines.append("")
    lines.append("These are delivery buys with no stop. trader-risk blocks them while \"require_stop\" is true "
                 "and applies your day-trade limits to every buy.")
    lines.append("Next: trader-risk checks every order, then trader-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "note": ("Proposals only. Nothing is placed until trader-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = to_ist(now).date().isoformat()
    plan = read_json(a.plan, "plan (run trader-exit first, or use --weights for a split)")
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
        else:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 1311.90")
    sym = plain_symbol((plan or {}).get("symbol"))
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and limit is None and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        if p is not None and fresh:
            price = p
            if round_cent(p) > round_cent(_f(plan.get("entry")) or 0):
                notes.append("%s last %s (as of %s IST) is above the plan's entry %s, so the risk "
                             "is bigger: sized at %s." % (sym, _price(p), to_ist(t).strftime("%H:%M"),
                                                          _price(plan.get("entry")), _price(p)))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_ist(t).strftime("%Y-%m-%d %H:%M") + " IST")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today)
    order = bracket_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed cover order to %s" % a.write)
    return 0 if order else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    prices = load_prices(a.prices)
    if not prices and os.path.exists("trader/prices.json") and not a.prices:
        prices = load_prices("trader/prices.json")
    whole = True if a.whole_shares else None
    result = size(rows, cfg, a.budget, prices, whole, a.cents, a.top)
    orders = orders_from(result, source)
    if a.write:
        write_orders(a.write, orders, cfg, source, parse_now(a.now))
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size an NSE day trade to your budget and max loss, in rupees (trader-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a limit order instead of market, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-06 09:51\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    g = ap.add_argument_group("weights mode (split a budget across a list)")
    g.add_argument("--weights", help="inline weights: RELIANCE=30,HDFCBANK=25")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"ticker\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    g.add_argument("--whole-shares", action="store_true", help="whole shares (the default: NSE has no fractions)")
    g.add_argument("--cents", action="store_true", help="split to the paisa instead of whole rupees")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        if a.top or a.whole_shares or a.cents:
            raise SizeError("--top, --whole-shares and --cents work with --weights or --targets")
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
