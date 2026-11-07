#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-11-06 09:51"        # pin the clock (IST)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it.

Orders are the Kite connector's place_order arguments (what trader-budget
writes): variety, exchange, tradingsymbol, transaction_type, quantity,
product, order_type, price, trigger_price, validity, tag. A cover order
(variety "co") carries its stop-loss in trigger_price; the pack's own
target_price and plan ride along and are never sent.

What blocks an order (every order):

  format        no tradingsymbol, transaction_type not BUY / SELL, quantity not
                a whole number above 0, an order_type or variety or validity
                Kite does not take (or this pack does not use: amo, iceberg,
                auction), a LIMIT without a price, an SL without a price and a
                trigger, an SL-M without a trigger, a cover order without a
                trigger_price or with a product other than MIS, a price or
                trigger off the NSE tick for its price band (0.01 under 250,
                0.05 to 1,000, 0.10 to 5,000, 0.50 to 10,000, 1 to 20,000, 5 above)
  asset         an exchange other than NSE or BSE (NFO, BFO, MCX: futures,
                options, commodities) unless "allow_options": true
  margin        product MTF (margin trading: borrowed money) unless "allow_margin": true
  short         selling more than you hold, unless "allow_short": true (an
                intraday MIS short is still a short)

What blocks an entry (on top):

  blocked       a ticker on your "blocked" list
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (a cover order's trigger_price
                or the order's plan stop); a stop on the wrong side of the entry
  loss          qty x (entry - stop) plus the round trip's charges (Zerodha
                intraday: brokerage, STT, exchange, SEBI, stamp duty, GST) over
                max_loss_per_trade_inr. The entry is the limit price for a limit
                order, else the HIGHER of the plan's entry and a fresh price
                (under 15 minutes old), so a stale plan cannot understate the risk
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses at the stops of today's entries plus this one
                over max_daily_loss_inr (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before (09:30) or after no_entries_after
                (14:45) IST, or the market is closed (weekend, NSE holiday,
                outside 9:15 AM to 3:30 PM, or a special session such as
                Muhurat trading outside its hours)

Warnings, not blocks: a price within 1% of the day's upper circuit (when
trader/prices.json has the circuit limits from Kite's get_quotes: a stock
that locks at its circuit can be impossible to sell), a flat_by after 3:10 PM
(Zerodha squares off intraday F&O-stock positions from 3:12 PM, for a fee),
and a live account ("paper": false).

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so trader-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 80000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:05",
    "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
    "charges": {},
}
KITE_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
               "order_type", "price", "trigger_price", "validity", "validity_ttl", "disclosed_quantity",
               "iceberg_legs", "iceberg_quantity", "tag")
PRICE_FIELDS = ("price", "trigger_price")
TYPES = {"MARKET": "market", "LIMIT": "limit", "SL": "stop_limit", "SL-M": "stop"}
VARIETIES = {"regular", "co"}
VALIDITIES = {"DAY", "IOC", "TTL"}
PRODUCTS = {"MIS", "CNC", "NRML", "MTF"}
EXCHANGES = {"NSE", "BSE"}
TICKER = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
TICK_BANDS = ((250, 0.01), (1000, 0.05), (5000, 0.10), (10000, 0.50), (20000, 1.00), (None, 5.00))
CHARGES = {  # Zerodha equity intraday (zerodha.com/charges, read 2026-11-06); same as trader-budget
    "brokerage_pct": 0.03, "brokerage_max": 20.0, "stt_sell_pct": 0.025,
    "txn_pct_nse": 0.00307, "txn_pct_bse": 0.00375, "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.003, "gst_pct": 18.0,
}
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CENT = 0.005
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stop price": "the stop price", "stale price": "an old price"}

NSE_HOLIDAYS = {  # NSE equity and equity-derivatives trading holidays on weekdays (NSE circular, Dec 2025)
    "2026-01-15": "Municipal Corporation elections (Maharashtra)",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
NSE_SPECIAL = {  # special live sessions on a day that is otherwise closed: (open, close, name), IST
    "2026-11-08": ("14:20", "15:30", "Muhurat trading (Diwali Laxmi Pujan)"),
}
CALENDAR_YEARS = (2026,)        # NSE publishes next year's list in December: add it here
OPEN = datetime.time(9, 15)     # continuous trading starts (pre-open 9:00 to 9:08, matching to 9:15)
CLOSE = datetime.time(15, 30)   # continuous trading ends for stocks outside F&O
CAS_END = datetime.time(15, 15) # F&O stocks stop at 3:15 PM, then the closing auction runs to 3:35 PM
IST = datetime.timedelta(hours=5, minutes=30)   # India Standard Time: UTC+5:30, no daylight saving


class RiskError(Exception):
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
        raise RiskError("--now wants a time like \"2026-11-06 09:51\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def _clock_of(text):
    h, m = str(text).split(":")
    return datetime.time(int(h), int(m))


def special(d):
    """The name of a special session (Muhurat trading) on this date, or None."""
    sp = NSE_SPECIAL.get(d.isoformat())
    return sp[2] if sp else None


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NSE holiday list; a special
    session (Muhurat trading) opens a day that is otherwise closed."""
    if d.isoformat() in NSE_SPECIAL:
        return True, ""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def session(d):
    """(open_time, close_time) in IST, or None if closed all day. The close is
    3:30 PM; F&O stocks stop continuous trading at 3:15 PM (CAS_END)."""
    sp = NSE_SPECIAL.get(d.isoformat())
    if sp:
        return _clock_of(sp[0]), _clock_of(sp[1])
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, CLOSE


def hhmm(value, key):
    """"09:45" -> time(9, 45); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an IST time like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def window_check(now_et, c):
    """Reasons an entry cannot be placed now (IST). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now_et.date(), now_et.time().replace(second=0, microsecond=0)
    if d.year not in CALENDAR_YEARS:
        warns.append("the holiday list covers %s only: check nseindia.com for %s"
                     % (" and ".join(map(str, CALENDAR_YEARS)), d.isoformat()))
    ok, why = trading_day(d)
    stamp = now_et.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        return ["window: the market is closed today (%s, %s IST)" % (why, stamp)], warns
    opens, close = session(d)
    if t < opens or now_et.time() >= close:
        return ["window: the market is closed (%s IST; the session is %s to %s)"
                % (stamp, opens.strftime("%H:%M"), close.strftime("%H:%M"))], warns
    if special(d):
        warns.append("special session today: %s, %s to %s IST" % (special(d), opens.strftime("%H:%M"),
                                                                  close.strftime("%H:%M")))
        if before is not None and before < opens:
            before = None
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                     t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"),
                                                                    t.strftime("%H:%M"))], warns
    return [], warns



# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact order that was checked
    (the fields that go to Kite's place_order)."""
    core = {k: order[k] for k in KITE_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    return "₹" + "{:,.2f}".format(float(x)).replace(".00", "")


def _cent(x):
    """A price to the paisa, half up (1311.905 -> 1311.91)."""
    return int(float(x) * 100 + 0.5 + 1e-9) / 100.0


def cfg_inr(c, key):
    """A rupee limit from the config: key_inr."""
    return _f(c.get(key + "_inr"))


def tick_for(price):
    """The NSE equity tick for a price band (from 2025-04-15)."""
    for top, tick in TICK_BANDS:
        if top is None or price < top:
            return tick
    return TICK_BANDS[-1][1]


def on_tick(price, tick):
    return abs(round(price / tick) * tick - price) < 1e-6


def plain_symbol(sym):
    s = str(sym or "").strip().upper()
    if s.startswith("NSE:"):
        s = s[4:]
    if s.endswith(".NS"):
        s = s[:-3]
    if s.endswith(".BO"):
        s = "BSE:" + s[:-3]
    return s


def order_symbol(o):
    """The pack's ticker for an order: RELIANCE on NSE, BSE:X on BSE."""
    ts = str(o.get("tradingsymbol") or o.get("symbol") or "").strip().upper()
    ex = str(o.get("exchange") or "NSE").strip().upper()
    if not ts:
        return ""
    return ts if ex == "NSE" else "%s:%s" % (ex, ts)


def charges(qty, buy, sell, exchange="NSE", over=None):
    """Zerodha equity intraday charges for a round trip; the same sum as trader-budget."""
    k = dict(CHARGES)
    k.update(over or {})
    bv, sv = qty * float(buy), qty * float(sell)
    brk = sum(min(k["brokerage_max"], v * k["brokerage_pct"] / 100.0) for v in (bv, sv) if v > 0)
    stt = round(sv * k["stt_sell_pct"] / 100.0)
    txn = (bv + sv) * k["txn_pct_bse" if exchange == "BSE" else "txn_pct_nse"] / 100.0
    sebi = (bv + sv) * k["sebi_per_crore"] / 1e7
    stamp = round(bv * k["stamp_buy_pct"] / 100.0)
    gst = (brk + txn + sebi) * k["gst_pct"] / 100.0
    return round(sum(round(v, 2) for v in (brk, stt, txn, sebi, stamp, gst)), 2)


def delivery_charges(qty, buy, sell, exchange="NSE"):
    """Zerodha equity delivery round trip (zerodha.com/charges, read 2026-11-07): no brokerage, STT 0.1%
    each side, transaction charges, SEBI, stamp 0.015% on the buy, GST, DP Rs 15.34 on the sell."""
    bv, sv = qty * float(buy), qty * float(sell)
    stt = round(bv * 0.001) + round(sv * 0.001)
    txn = (bv + sv) * (0.00375 if exchange == "BSE" else 0.00307) / 100.0
    sebi = (bv + sv) * 10.0 / 1e7
    stamp = round(bv * 0.00015)
    gst = (txn + sebi) * 0.18
    return round(sum(round(v, 2) for v in (stt, txn, sebi, stamp, gst, 15.34)), 2)


def _p(x):
    return "%.2f" % float(x)


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = plain_symbol(p.get("ticker") or p.get("symbol") or p.get("tradingsymbol"))
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out



# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          circuits=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its IST date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh
    circuits     {SYM: (lower, upper)} circuit limits from Kite's get_quotes, if known"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    prices = {plain_symbol(k): float(v) for k, v in (prices or {}).items() if _f(v)}
    circuits = {plain_symbol(k): v for k, v in (circuits or {}).items()}
    now_utc = now or parse_now(None)
    now_et = to_ist(now_utc)
    today = today or now_et.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {plain_symbol(b) for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(plain_symbol(r.get("symbol", "")), str(r.get("side", "")).lower()) for r in done_today}
    win_reasons, win_warns = window_check(now_et, c)
    fb = hhmm(c.get("flat_by"), "flat_by")
    if fb and fb > datetime.time(15, 10):
        win_warns.append("flat_by %s is after 3:10 PM: Zerodha squares off intraday (MIS) positions in F&O "
                         "stocks from 3:12 PM and charges for each order it closes" % fb.strftime("%H:%M"))
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders go to Kite and use real money")
    top_warn.extend(win_warns)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = order_symbol(o)
        exch = str(o.get("exchange") or "NSE").strip().upper()
        side = str(o.get("transaction_type") or o.get("side") or "").strip().lower()
        otype = str(o.get("order_type") or "MARKET").strip().upper()
        typ = TYPES.get(otype, otype.lower())
        variety = str(o.get("variety") or "regular").strip().lower()
        product = str(o.get("product") or "").strip().upper()
        validity = str(o.get("validity") or "DAY").strip().upper()
        qty, notional = o.get("quantity", o.get("qty")), None
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        exit_cover = bool(o.get("exit_cover"))
        cover = variety == "co" and not exit_cover
        gtt = o.get("gtt") if isinstance(o.get("gtt"), dict) else None
        swing = product == "CNC" and gtt is not None and not exit_cover
        value = None
        if swing:
            for k in ("lower_trigger_value", "upper_trigger_value", "trigger_value", "lower_limit_price",
                      "upper_limit_price", "limit_price"):
                v = _f(gtt.get(k))
                if gtt.get(k) is not None and (v is None or v <= 0 or not on_tick(v, tick_for(v))):
                    reasons.append("format: the GTT's %s %r is not a price on the NSE tick" % (k, gtt.get(k)))
            gq = _f(gtt.get("lower_quantity", gtt.get("quantity")))
            if gq is not None and _f(qty) is not None and abs(gq - _f(qty)) > 1e-9:
                reasons.append("format: the GTT sells %s shares but the buy is for %s" % (_num(gq), qty))

        # format
        if not sym:
            reasons.append("format: no tradingsymbol")
        if side not in ("buy", "sell"):
            reasons.append("format: transaction_type must be BUY or SELL, got %r"
                           % (o.get("transaction_type") or o.get("side")))
        qf = _f(qty)
        if qf is None or qf <= 0 or abs(qf - round(qf)) > 1e-9:
            reasons.append("format: quantity must be a whole number of shares above 0, got %r" % qty)
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                elif not on_tick(v, tick_for(v)):
                    reasons.append("format: %s %s is not on the NSE tick of %s for that price"
                                   % (k, _p(v), ("%.2f" % tick_for(v)).rstrip("0").rstrip(".")))
        if otype not in TYPES:
            reasons.append("format: unknown order_type %r (MARKET, LIMIT, SL or SL-M)" % o.get("order_type"))
        if exit_cover and side != "sell":
            reasons.append("format: exiting a cover order is a SELL")
        if variety not in VARIETIES:
            reasons.append("format: variety %r is not used by this pack (regular or co)" % o.get("variety"))
        if validity not in VALIDITIES:
            reasons.append("format: unknown validity %r (DAY, IOC or TTL)" % o.get("validity"))
        if validity == "TTL" and not _f(o.get("validity_ttl")):
            reasons.append("format: a TTL order needs validity_ttl (minutes)")
        if product and product not in PRODUCTS:
            reasons.append("format: unknown product %r (MIS, CNC, NRML, MTF)" % o.get("product"))
        if not product:
            reasons.append("format: no product (MIS for intraday, CNC for delivery)")
        if otype in ("LIMIT", "SL") and o.get("price") is None:
            reasons.append("format: a %s order needs a price" % otype)
        if otype in ("SL", "SL-M") and o.get("trigger_price") is None:
            reasons.append("format: an %s order needs a trigger_price" % otype)
        if cover:
            if o.get("trigger_price") is None:
                reasons.append("format: a cover order needs its stop-loss in trigger_price")
            if product and product != "MIS":
                reasons.append("format: a cover order is intraday only: product MIS, got %s" % product)
            if otype not in ("MARKET", "LIMIT"):
                reasons.append("format: a cover order enters with MARKET or LIMIT, got %s" % otype)
        if product == "NRML" and exch in EXCHANGES:
            warns.append("product NRML is for futures and options; equity day trades use MIS (or CNC)")

        # asset
        if exch not in EXCHANGES:
            if not c.get("allow_options"):
                reasons.append("asset: %s is a derivatives or commodities exchange; this pack trades NSE / BSE "
                               "shares only (\"allow_options\": false)" % exch)
            else:
                warns.append("derivatives order on %s: options can lose all of what you pay, futures more" % exch)
        elif sym and not TICKER.match(str(o.get("tradingsymbol") or o.get("symbol") or "").upper()):
            reasons.append("asset: %s does not look like an NSE / BSE trading symbol" % sym)
        if product == "MTF" and not c.get("allow_margin"):
            reasons.append("margin: product MTF is margin trading (borrowed money); \"allow_margin\" is false")

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if typ in ("limit", "stop_limit"):
            ref, ref_from = _f(o.get("price")), "limit"
        elif typ == "stop" and not cover:
            ref, ref_from = _f(o.get("trigger_price")), "stop price"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = _cent(ref)
                if live and ref_from == "price" and plan_entry and live > plan_entry + CENT:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "market":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        q = _f(qty)
        if q and ref:
            value = q * ref

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9:
            if q is not None:
                exiting = q <= held + 1e-9
            elif value is not None:
                exiting = value <= held_value + CENT
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %s-share order; add it to "
                         "trader/prices.json (trader-alerts: alerts.py quote %s --write)" % (sym, qty, sym))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < float(cfg_inr(c, "min_order") or 0) - CENT:
                later.append("size: %s is under your %s minimum order"
                             % (money(value), money(cfg_inr(c, "min_order"))))
            if cfg_inr(c, "max_order") and value > cfg_inr(c, "max_order") + CENT:
                later.append("size: %s is over your %s max order"
                             % (money(value), money(cfg_inr(c, "max_order"))))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy" and kind == "entry":
            after_cash = cash - value
            if after_cash < -CENT and not c.get("allow_margin"):
                later.append("cash: needs %s but only %s cash; that would be margin (borrowed money)"
                             % (money(value), money(cash)))
            elif after_cash < buffer_inr - CENT:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), money(buffer_inr)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + CENT:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            if q is not None:
                over = q > held + 1e-9
            elif value is not None:
                over = value > held_value + CENT
            else:
                over = held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window
        max_loss = risk_ps = rr = stop = target = fees = loss_cap = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop = _f(o.get("trigger_price")) if cover else None
            if swing:
                leg_stop = _f(gtt.get("lower_trigger_value", gtt.get("trigger_value")))
            plan_stop = _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f(o.get("target_price")) or _f(plan.get("target"))
            if swing and _f(gtt.get("upper_trigger_value")):
                target = _f(gtt.get("upper_trigger_value"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > CENT:
                warns.append("the cover order's stop %s differs from the plan's stop %s: the order's is the one "
                             "the exchange uses" % (_p(leg_stop), _p(plan_stop)))
            if swing and leg_stop:
                info.append("swing (CNC delivery): the %s to Kite as a GTT order after the buy fills; "
                            "until then there is no stop at the exchange" % ("stop and target go" if target else "stop goes"))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not at the exchange: use a cover order "
                             "(trader-budget writes one), or set a stop alert (alerts.py add --from-plan)" % _p(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): a cover "
                                 "order's trigger_price, from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - CENT) or (not long and stop <= ref + CENT):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_c = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_c / 100.0
                    shares = q if q is not None else ((value or 0) / ref)
                    at_stop = round(shares * risk_ps, 2)
                    if swing:
                        fees = delivery_charges(shares, ref, stop, "BSE" if exch == "BSE" else "NSE")
                        sw = c.get("swing") or {}
                        cap = float(sw.get("swing_capital", 200000)) * float(sw.get("risk_per_trade_pct", 1.0)) / 100.0
                    else:
                        fees = charges(shares, ref if long else stop, stop if long else ref,
                                       "BSE" if exch == "BSE" else "NSE", c.get("charges"))
                        cap = cfg_inr(c, "max_loss_per_trade")
                    max_loss = round(at_stop + fees, 2)
                    loss_cap = cap
                    if cap and max_loss > cap + CENT:
                        later.append("loss: %s shares x %s risk a share = %s at the stop, plus %s of charges = %s, "
                                     "over your %s max loss per %s" % (_num(shares), _p(risk_ps), money(at_stop),
                                                                          money(fees), money(max_loss), money(cap),
                                                                          "swing trade (risk_per_trade_pct of "
                                                                          "swing_capital)" if swing else "trade"))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + CENT) or (not long and target >= ref - CENT):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(int(round(abs(target - ref) * 100)) / float(risk_c), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = None if swing else cfg_inr(c, "max_daily_loss")
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + CENT:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss), money(used + max_loss),
                                                       money(daily)))
            cap_t = 0 if swing else int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            if not swing:
                later.extend(win_reasons)
            lim = circuits.get(sym)
            if lim and ref and long and _f(lim[1]) and ref >= 0.99 * float(lim[1]):
                warns.append("%s is within 1%% of its upper circuit %s: if it locks there, there may be no "
                             "sellers to buy from, and at the lower circuit no buyers to sell to"
                             % (sym, _p(lim[1])))
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s with %s of charges%s, reward:risk %s%s"
                            % (money(max_loss), money(fees or 0), (" (limit %s)" % money(loss_cap)) if loss_cap else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "buy":
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if q is not None:
                    acct["shares"][sym] = held + q
                elif ref and value is not None:
                    acct["shares"][sym] = held + value / ref
            elif side == "sell":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                if q is not None:
                    acct["shares"][sym] = held - q
                elif ref and value is not None:
                    acct["shares"][sym] = held - value / ref
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "amount": "%s shares" % qty, "exchange": exch, "product": product, "variety": variety,
            "value_inr": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "charges": fees, "max_loss": max_loss,
            "reward_risk": rr, "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now_et.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s %s, %s" % (r["verdict"], r["index"], r["side"].upper(), r["symbol"],
                                                      r["amount"], r["product"] or "?",
                                                      "cover order" if r["variety"] == "co" else r["variety"],
                                                      r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s)."
                 % (res["pass"], res["block"], res["now_ist"], money(res["cash_after"]),
                    "paper trading" if res["paper"] else "LIVE, Kite"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}, {SYM: (lower, upper)}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of. Circuit limits come from
    detail.lower_circuit / detail.upper_circuit when trader-alerts read Kite quotes."""
    if not isinstance(pdoc, dict):
        return {}, {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}, {}
    out, times, circ = {}, {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        d = detail.get(s) or detail.get(str(s).upper()) or {}
        s = plain_symbol(s)
        out[s] = float(p)
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
        if _f(d.get("upper_circuit")):
            circ[s] = (_f(d.get("lower_circuit")), _f(d.get("upper_circuit")))
    return out, times, circ


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-06 09:51\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-11-06")
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times, circ = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, circ)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
