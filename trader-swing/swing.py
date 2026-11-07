#!/usr/bin/env python3
"""Swing trader for NSE stocks, held for days to a few weeks as delivery (CNC).
Standard library only.

    python3 swing.py scan --data kite-daily/                 # after 15:30: setups + stocks near a breakout
    python3 swing.py scan --symbols RELIANCE,TCS,INFY        # same, on free Yahoo .NS daily bars
    python3 swing.py plan TRENT --data kite-daily/ --write   # size it, write the CNC buy + GTT stop/target
    python3 swing.py backtest --data kite-daily/             # every setup, 5 years, with Zerodha costs
    python3 swing.py manage --data kite-daily/               # open swing positions: breakeven, time stop, exits
    python3 swing.py scan --data kite-daily/ --setups momentum --quotes quotes.json   # 3:15 PM, today's bursts
    python3 swing.py study --data kite-daily/ --entry close  # what holding a burst 1-10 days returned

Daily bars come from a folder of Kite get_historical_data files (one
SYMBOL.json per stock, interval "day", plus NIFTY_50.json for the market
filter), or from Yahoo's free chart endpoint (SYMBOL.NS, unofficial, can lag).

The rules (every number is in DEFAULTS and can be changed under "swing" in
trader/config.json):

  Market filter (Nifty 50)  above its 200-day average and the 50-day above the
                            200-day: full size. Above the 200-day only: half
                            size. Below the 200-day: no new swing trades.
  Stock filter              close > 50-day average > 200-day average, within
                            25% of its 52-week high, 50-day average traded
                            value at least Rs 10 crore, price at least Rs 50.
  pullback                  in that uptrend, a dip to the 20-day EMA on
                            falling volume over the last 5 days, then a day
                            that closes above the day before's high and above
                            the EMA. Stop: the dip's low.
  breakout                  a 30-day base no deeper than 15%; the day closes
                            above the base's high on 1.5x the 50-day average
                            volume, in the top 40% of its range. Stop: the
                            10-day low or 2 ATR under the close, the closer.
  rsi60                     monthly RSI(14) and weekly RSI(14) above 60, daily
                            RSI above 60, and the first close above the
                            10-day swing high. Stop: last week's low. Exit:
                            a daily close with RSI back under 60.
  momentum                  a momentum burst: up 5% or more on 2x the 50-day
                            volume, closing in the top 30% of the day's
                            range, the first such day (not the second day of a
                            run), not more than 60% above the 50-day average.
                            No trend filter. Stop: the burst day's low (at most
                            10% away). Exit: the close of day 5. Best bought in
                            the last minutes of the burst day (scan at 3:15 PM
                            with --quotes from Kite); the next open gaps up 0.5%
                            on average.
  near breakout (watch)     a tight 30-day base (12% deep at most), the close
                            within 3% under its high, volume drying up, daily
                            RSI 50 to 70. Not a trade: a level to watch, with
                            the trigger (base high) and a stop.

  Entry       the next day's open (backtest) or a limit order 0.5% above the
              signal close (live), so a gap up is not chased.
  Size        1% of the swing capital at risk per trade (half in a cautious
              market), at most 25% of capital in one stock, 5 positions,
              6% total open risk.
  Exits       stop and 2R target as a Kite GTT OCO (two-leg) order; the stop
              moves to the entry once the close is 1R up; time stop after 10
              trading days (40 for rsi60, which exits on its RSI rule).
  Costs       Zerodha equity delivery: no brokerage, STT 0.1% on buy and
              sell, NSE transaction 0.00307%, SEBI Rs 10 a crore, stamp
              0.015% on the buy, GST 18% on transaction + SEBI, DP Rs 15.34
              per scrip on each sell (zerodha.com/charges, read 2026-11-07).

The backtest enters at the next open, takes a gap through the stop at the
open price, assumes the stop comes first when one day touches both stop and
target, and splits the result into an earlier and a later period, so a rule
that only fits the past shows up. It is a test of rules, not a forecast.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

IST = datetime.timedelta(hours=5, minutes=30)
UA = "Mozilla/5.0 (compatible; nse-trading-toolkit/1.0; +https://github.com/charan2711/nse-trading-toolkit)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
INDEX_NAMES = ("NIFTY_50", "NIFTY 50", "NIFTY50", "^NSEI")

DEFAULTS = {
    "swing_capital": 200000,
    "risk_per_trade_pct": 1.0,
    "max_positions": 5,
    "max_open_risk_pct": 6.0,
    "max_position_pct": 25.0,
    "min_traded_value_cr": 10.0,
    "min_price": 50.0,
    "target_r": 2.0,
    "breakeven_at_r": 1.0,
    "time_stop_days": 10,
    "time_stop_days_rsi60": 40,
    "momentum_min_pct": 5.0,
    "momentum_vol_x": 2.0,
    "momentum_hold_days": 5,
    "momentum_max_risk_pct": 10.0,
    "min_risk_pct": 0.8,
    "max_risk_pct": 8.0,
    "entry_buffer_pct": 0.5,
    "setups": ["pullback", "breakout", "rsi60", "momentum"],
    "regime": True,
    "paper": True,
}
CHARGES = {
    "stt_pct": 0.1, "txn_pct_nse": 0.00307, "txn_pct_bse": 0.00375, "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.015, "gst_pct": 18.0, "dp_per_sell": 15.34,
}
TICK_BANDS = ((250, 0.01), (1000, 0.05), (5000, 0.10), (10000, 0.50), (20000, 1.0), (None, 5.0))
SETUPS = ("pullback", "breakout", "rsi60", "momentum")
NO_TARGET = ("rsi60", "momentum")      # these exit on a rule or a hold period, not a 2R target


class SwingError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def plain_symbol(sym):
    s = str(sym).strip().upper()
    for suffix in (".NS", ".BO"):
        if s.endswith(suffix):
            return ("BSE:" + s[:-3]) if suffix == ".BO" else s[:-3]
    if s.startswith("NSE:"):
        s = s[4:]
    return s


def yahoo_symbol(sym):
    s = plain_symbol(sym)
    if s.startswith("BSE:"):
        return s[4:] + ".BO"
    return s + ".NS"


def tick_for(price):
    for top, tick in TICK_BANDS:
        if top is None or price < top:
            return tick
    return TICK_BANDS[-1][1]


def floor_tick(x, tick):
    return round(math.floor(round(x / tick, 6)) * tick, 2)


def ceil_tick(x, tick):
    return round(math.ceil(round(x / tick, 6)) * tick, 2)


def money(x):
    """Indian grouping: 1234567.8 -> Rs 12,34,567.80 (with the rupee sign)."""
    neg = x < 0
    s = "%.2f" % abs(x)
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + "₹" + whole + "." + frac


def charges(qty, buy=None, sell=None, exchange="NSE", over=None):
    """Zerodha equity delivery charges for the legs given (buy, sell or both)."""
    k = dict(CHARGES)
    k.update(over or {})
    bv = qty * buy if buy else 0.0
    sv = qty * sell if sell else 0.0
    stt = round(bv * k["stt_pct"] / 100.0) + round(sv * k["stt_pct"] / 100.0)
    txn = (bv + sv) * k["txn_pct_bse" if exchange == "BSE" else "txn_pct_nse"] / 100.0
    sebi = (bv + sv) * k["sebi_per_crore"] / 1e7
    stamp = round(bv * k["stamp_buy_pct"] / 100.0)
    gst = (txn + sebi) * k["gst_pct"] / 100.0
    dp = k["dp_per_sell"] if sv > 0 else 0.0
    parts = {"stt": stt, "transaction": txn, "sebi": sebi, "stamp": stamp, "gst": gst, "dp": dp}
    parts = {n: round(v, 2) for n, v in parts.items()}
    parts["total"] = round(sum(parts.values()), 2)
    return parts


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        cfg.update(doc.get("swing") or {})
        if "paper" in doc and "paper" not in (doc.get("swing") or {}):
            cfg["paper"] = doc["paper"]
    return cfg


# ---------------------------------------------------------------- data

def _date_of(x):
    if isinstance(x, (int, float)):
        return (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=x) + IST).date()
    return datetime.date.fromisoformat(str(x)[:10])


def parse_candles(doc):
    """Kite daily candles (a list of dicts, {"candles": [...]}, or REST
    {"data": {"candles": [[...]]}}) or a Yahoo chart JSON -> bars, oldest first:
    [{"d": date, "o", "h", "l", "c", "v"}]."""
    if isinstance(doc, dict) and "chart" in doc:
        res = (doc["chart"].get("result") or [None])[0]
        if not res:
            raise SwingError("Yahoo sent no chart")
        q = res["indicators"]["quote"][0]
        out = []
        for t, o, h, l, c, v in zip(res.get("timestamp") or [], q["open"], q["high"], q["low"], q["close"],
                                    q["volume"]):
            if None in (o, h, l, c):
                continue
            out.append({"d": _date_of(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c),
                        "v": float(v or 0)})
        return _dedupe(out)
    if isinstance(doc, dict):
        doc = doc.get("candles") or (doc.get("data") or {}).get("candles") or []
    out = []
    for row in doc:
        if isinstance(row, dict):
            d, o, h, l, c, v = (row.get("date"), row.get("open"), row.get("high"), row.get("low"),
                                row.get("close"), row.get("volume", 0))
        else:
            d, o, h, l, c, v = (list(row) + [0])[:6]
        if None in (o, h, l, c):
            continue
        out.append({"d": _date_of(d), "o": float(o), "h": float(h), "l": float(l), "c": float(c),
                    "v": float(v or 0)})
    return _dedupe(out)


def _dedupe(bars):
    bars.sort(key=lambda b: b["d"])
    out = []
    for b in bars:
        if out and out[-1]["d"] == b["d"]:
            out[-1] = b
        else:
            out.append(b)
    return out


def _read_json(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def load_folder(folder):
    """A folder of SYMBOL.json(.gz) files -> ({SYM: bars}, index bars or None)."""
    if not os.path.isdir(folder):
        raise SwingError("no such folder: %s" % folder)
    data, index = {}, None
    for name in sorted(os.listdir(folder)):
        if not (name.endswith(".json") or name.endswith(".json.gz")):
            continue
        sym = name[:-8] if name.endswith(".json.gz") else name[:-5]
        bars = parse_candles(_read_json(os.path.join(folder, name)))
        if sym.upper() in INDEX_NAMES:
            index = bars
        elif bars:
            data[plain_symbol(sym)] = bars
    return data, index


def fetch_yahoo(sym, rng="2y"):
    path = "/v8/finance/chart/%s?range=%s&interval=1d" % (urllib.parse.quote(yahoo_symbol(sym), safe=".-^"), rng)
    last = None
    for host in YAHOO_HOSTS:
        try:
            req = urllib.request.Request(host + path, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return parse_candles(json.loads(r.read().decode("utf-8")))
        except (urllib.error.URLError, ValueError, SwingError) as e:
            last = e
    raise SwingError("Yahoo: no daily bars for %s (%s)" % (sym, last))


def load_yahoo(symbols, pause=0.3):
    data, failed = {}, {}
    for k, s in enumerate(symbols):
        if k and pause:
            time.sleep(pause)
        try:
            data[plain_symbol(s)] = fetch_yahoo(s)
        except SwingError as e:
            failed[s] = str(e)
    try:
        index = fetch_yahoo("^NSEI")
    except SwingError:
        index = None
    return data, index, failed


def merge_quotes(data, doc):
    """Kite get_quotes output -> today's (still forming) bar added to each stock,
    so a 3:15 PM scan sees today's move. Returns the symbols updated."""
    if isinstance(doc, dict) and isinstance(doc.get("data"), dict):
        doc = doc["data"]
    done = []
    for key, q in (doc or {}).items():
        if not isinstance(q, dict) or q.get("last_price") is None:
            continue
        sym = plain_symbol(key)
        if sym not in data:
            continue
        ohlc = q.get("ohlc") or {}
        when = q.get("last_trade_time") or q.get("timestamp")
        if not when:
            continue
        d = _date_of(when)
        bar = {"d": d, "o": float(ohlc.get("open") or q["last_price"]), "h": float(ohlc.get("high") or q["last_price"]),
               "l": float(ohlc.get("low") or q["last_price"]), "c": float(q["last_price"]),
               "v": float(q.get("volume") or q.get("volume_traded") or 0)}
        bars = data[sym]
        if bars and bars[-1]["d"] == d:
            bars[-1] = bar
        elif not bars or bars[-1]["d"] < d:
            bars.append(bar)
        done.append(sym)
    return done


# ---------------------------------------------------------------- indicators

def sma(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs, n):
    out, k, e = [None] * len(xs), 2.0 / (n + 1), None
    for i, x in enumerate(xs):
        if i == n - 1:
            e = sum(xs[:n]) / n
        elif i >= n:
            e = x * k + e * (1 - k)
        out[i] = e
    return out


def rsi(closes, n=14):
    """Wilder's RSI."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, n + 1)]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, n + 1)]
    ag, al = sum(gains) / n, sum(losses) / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(0.0, ch)) / n
        al = (al * (n - 1) + max(0.0, -ch)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def period_rsi(bars, key, n=14):
    """RSI(n) on weekly or monthly closes, as it reads on each day: the
    completed periods plus the current, still-forming period. No look-ahead."""
    out = [None] * len(bars)
    closes = []          # completed period closes
    ag = al = None
    cur_key = None
    for i, b in enumerate(bars):
        k = key(b["d"])
        if cur_key is not None and k != cur_key:          # the previous period closed yesterday
            closes.append(bars[i - 1]["c"])
            m = len(closes)
            if m == n + 1:
                ch = [closes[j] - closes[j - 1] for j in range(1, m)]
                ag = sum(max(0.0, c) for c in ch) / n
                al = sum(max(0.0, -c) for c in ch) / n
            elif m > n + 1:
                c = closes[-1] - closes[-2]
                ag = (ag * (n - 1) + max(0.0, c)) / n
                al = (al * (n - 1) + max(0.0, -c)) / n
        cur_key = k
        if ag is not None:
            c = b["c"] - closes[-1]
            g = (ag * (n - 1) + max(0.0, c)) / n
            l_ = (al * (n - 1) + max(0.0, -c)) / n
            out[i] = 100.0 if l_ == 0 else 100.0 - 100.0 / (1 + g / l_)
    return out


def week_key(d):
    y, w, _ = d.isocalendar()
    return (y, w)


def month_key(d):
    return (d.year, d.month)


def atr(bars, n=14):
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    out = [None] * len(bars)
    a = None
    for i, t in enumerate(trs):
        if i == n - 1:
            a = sum(trs[:n]) / n
        elif i >= n:
            a = (a * (n - 1) + t) / n
        out[i] = a
    return out


def indicators(bars):
    c = [b["c"] for b in bars]
    v = [b["v"] for b in bars]
    tv = [b["c"] * b["v"] for b in bars]
    ind = {"bars": bars, "c": c, "sma50": sma(c, 50), "sma200": sma(c, 200), "ema20": ema(c, 20),
           "rsi": rsi(c), "atr": atr(bars), "vol50": sma(v, 50), "tv50": sma(tv, 50),
           "wrsi": period_rsi(bars, week_key), "mrsi": period_rsi(bars, month_key),
           "idx": {b["d"]: i for i, b in enumerate(bars)}}
    hh = [None] * len(bars)
    for i in range(len(bars)):
        lo = max(0, i - 251)
        hh[i] = max(b["h"] for b in bars[lo:i + 1])
    ind["hh252"] = hh
    return ind


def last_week_low(bars, i):
    """The low of the last COMPLETED ISO week before bar i's week."""
    k = week_key(bars[i]["d"])
    j = i
    while j >= 0 and week_key(bars[j]["d"]) == k:
        j -= 1
    if j < 0:
        return None
    k2 = week_key(bars[j]["d"])
    lo = bars[j]["l"]
    while j >= 0 and week_key(bars[j]["d"]) == k2:
        lo = min(lo, bars[j]["l"])
        j -= 1
    return lo


# ---------------------------------------------------------------- rules

def regime_at(index_ind, d):
    """(size multiplier, label) for the market on date d. No index data: full size."""
    if not index_ind:
        return 1.0, "no index data (filter off)"
    i = index_ind["idx"].get(d)
    if i is None:
        # nearest earlier index bar
        days = [x for x in index_ind["idx"] if x <= d]
        if not days:
            return 1.0, "no index data (filter off)"
        i = index_ind["idx"][max(days)]
    c, s50, s200 = index_ind["c"][i], index_ind["sma50"][i], index_ind["sma200"][i]
    if s200 is None:
        return 1.0, "index history too short (filter off)"
    if c < s200:
        return 0.0, "BEAR: Nifty %.0f under its 200-day average %.0f: no new swing trades" % (c, s200)
    if s50 < s200 or c < s50:
        return 0.5, "CAUTION: Nifty %.0f above its 200-day %.0f but not a clean uptrend: half size" % (c, s200)
    return 1.0, "BULL: Nifty %.0f above its 50-day %.0f and 200-day %.0f" % (c, s50, s200)


def stock_ok(ind, i, cfg):
    c, s50, s200 = ind["c"][i], ind["sma50"][i], ind["sma200"][i]
    if s200 is None or ind["tv50"][i] is None:
        return False
    if c < cfg["min_price"] or ind["tv50"][i] < cfg["min_traded_value_cr"] * 1e7:
        return False
    return c > s50 > s200 and c >= 0.75 * ind["hh252"][i]


def _signal(sym, ind, i, setup, stop, why, cfg, extra=None):
    c = ind["c"][i]
    tick = tick_for(c)
    stop = floor_tick(stop - tick, tick)
    risk_pct = (c - stop) / c * 100.0
    top = cfg["momentum_max_risk_pct"] if setup == "momentum" else cfg["max_risk_pct"]
    if not (cfg["min_risk_pct"] <= risk_pct <= top):
        return None
    sig = {"symbol": sym, "setup": setup, "date": ind["bars"][i]["d"].isoformat(), "close": round(c, 2),
           "stop": stop, "risk_pct": round(risk_pct, 2), "why": why}
    sig.update(extra or {})
    return sig


def setup_pullback(sym, ind, i, cfg):
    if i < 210 or not stock_ok(ind, i, cfg):
        return None
    b, e20, s50 = ind["bars"], ind["ema20"], ind["sma50"]
    window = range(i - 5, i)
    if not any(b[j]["l"] <= e20[j] * 1.01 for j in window):
        return None
    if any(b[j]["c"] <= s50[j] for j in window):
        return None
    dip_vol = sum(b[j]["v"] for j in range(i - 3, i)) / 3.0
    if not ind["vol50"][i - 1] or dip_vol >= ind["vol50"][i - 1]:
        return None
    t = b[i]
    if not (t["c"] > b[i - 1]["h"] and t["c"] > e20[i] and t["c"] > t["o"]):
        return None
    low = min(b[j]["l"] for j in range(i - 5, i + 1))
    return _signal(sym, ind, i, "pullback", low,
                   "dipped to the 20-day EMA on light volume (%.1fx avg), closed above yesterday's high"
                   % (dip_vol / ind["vol50"][i - 1]), cfg)


def setup_breakout(sym, ind, i, cfg):
    if i < 210 or not stock_ok(ind, i, cfg):
        return None
    b = ind["bars"]
    hi = max(b[j]["h"] for j in range(i - 30, i))
    lo = min(b[j]["l"] for j in range(i - 30, i))
    depth = (hi - lo) / hi
    t = b[i]
    if depth > 0.15 or t["c"] <= hi:
        return None
    if b[i - 1]["c"] > max(b[j]["h"] for j in range(i - 31, i - 1)):
        return None                       # not the first close above the base: already broke out
    vx = t["v"] / ind["vol50"][i - 1] if ind["vol50"][i - 1] else 0
    rng = t["h"] - t["l"]
    if vx < 1.5 or rng <= 0 or (t["c"] - t["l"]) / rng < 0.6:
        return None
    stop = max(min(b[j]["l"] for j in range(i - 9, i + 1)), t["c"] - 2 * ind["atr"][i])
    return _signal(sym, ind, i, "breakout", stop,
                   "closed above its 30-day base (%.1f%% deep, high %.2f) on %.1fx volume" % (depth * 100, hi, vx),
                   cfg, {"base_high": round(hi, 2)})


def setup_rsi60(sym, ind, i, cfg):
    if i < 210 or not stock_ok(ind, i, cfg):
        return None
    m, w, d = ind["mrsi"][i], ind["wrsi"][i], ind["rsi"][i]
    if None in (m, w, d) or m <= 60 or w <= 60 or d <= 60:
        return None
    b = ind["bars"]
    swing_hi = max(b[j]["h"] for j in range(i - 10, i))
    prev_hi = max(b[j]["h"] for j in range(i - 11, i - 1))
    if not (b[i]["c"] > swing_hi and b[i - 1]["c"] <= prev_hi):
        return None
    wl = last_week_low(b, i)
    if wl is None:
        return None
    return _signal(sym, ind, i, "rsi60", wl,
                   "monthly RSI %.0f, weekly %.0f, daily %.0f; first close above the 10-day swing high %.2f"
                   % (m, w, d, swing_hi), cfg)


def liquid(ind, i, cfg):
    return (ind["tv50"][i] is not None and ind["c"][i] >= cfg["min_price"]
            and ind["tv50"][i] >= cfg["min_traded_value_cr"] * 1e7)


def setup_momentum(sym, ind, i, cfg):
    """A momentum burst: a big up day on unusual volume, closing near its high.
    No trend filter (the burst is the signal); liquid stocks only."""
    if i < 60 or not liquid(ind, i - 1, cfg):
        return None
    b = ind["bars"]
    t, y = b[i], b[i - 1]
    chg = (t["c"] / y["c"] - 1) * 100
    if chg < cfg["momentum_min_pct"]:
        return None
    if (y["c"] / b[i - 2]["c"] - 1) * 100 >= cfg["momentum_min_pct"]:
        return None                                    # the first burst, not the second day of one
    vx = t["v"] / ind["vol50"][i - 1] if ind["vol50"][i - 1] else 0
    rng = t["h"] - t["l"]
    if vx < cfg["momentum_vol_x"] or rng <= 0 or (t["c"] - t["l"]) / rng < 0.7:
        return None
    if ind["sma50"][i] and t["c"] > 1.6 * ind["sma50"][i]:
        return None                                    # already far extended: a blow-off, not a burst
    return _signal(sym, ind, i, "momentum", t["l"],
                   "up %.1f%% on %.1fx volume, closed %.1f%% under the day's high" % (
                       chg, vx, (t["h"] - t["c"]) / t["h"] * 100), cfg,
                   {"day_change_pct": round(chg, 2), "volume_x": round(vx, 1)})


def near_breakout(sym, ind, i, cfg, max_depth=0.12, within=0.03):
    """A stock coiling under its base high: a level to watch, not a trade."""
    if i < 210 or not stock_ok(ind, i, cfg):
        return None
    b = ind["bars"]
    hi = max(b[j]["h"] for j in range(i - 29, i + 1))
    lo = min(b[j]["l"] for j in range(i - 29, i + 1))
    c = b[i]["c"]
    depth = (hi - lo) / hi
    dist = (hi - c) / hi
    if depth > max_depth or not (0 < dist <= within):
        return None
    v10 = sum(b[j]["v"] for j in range(i - 9, i + 1)) / 10.0
    if not ind["vol50"][i] or v10 >= 0.9 * ind["vol50"][i]:
        return None
    r = ind["rsi"][i]
    if r is None or not (50 <= r <= 70):
        return None
    tick = tick_for(hi)
    trigger = ceil_tick(hi + tick, tick)
    stop = floor_tick(max(lo, trigger - 2 * ind["atr"][i]), tick)
    return {"symbol": sym, "date": b[i]["d"].isoformat(), "close": round(c, 2), "trigger": trigger,
            "stop": stop, "distance_pct": round(dist * 100, 2), "base_depth_pct": round(depth * 100, 2),
            "volume_dryup": round(v10 / ind["vol50"][i], 2), "rsi": round(r, 1),
            "risk_pct_at_trigger": round((trigger - stop) / trigger * 100, 2)}


SETUP_FUNCS = {"pullback": setup_pullback, "breakout": setup_breakout, "rsi60": setup_rsi60,
               "momentum": setup_momentum}


def hold_limit(setup, cfg):
    if setup == "rsi60":
        return cfg["time_stop_days_rsi60"]
    if setup == "momentum":
        return cfg["momentum_hold_days"]
    return cfg["time_stop_days"]


def rel_strength(ind, index_ind, i, n=63):
    if i < n:
        return 0.0
    r = ind["c"][i] / ind["c"][i - n] - 1
    if index_ind:
        d = ind["bars"][i]["d"]
        j = index_ind["idx"].get(d)
        if j is not None and j >= n:
            r -= index_ind["c"][j] / index_ind["c"][j - n] - 1
    return r


# ---------------------------------------------------------------- scan

def scan(data, index, cfg, asof=None, setups=None):
    setups = setups or cfg["setups"]
    index_ind = indicators(index) if index else None
    out = {"signals": [], "near_breakout": [], "skipped": []}
    dates = sorted({b["d"] for bars in data.values() for b in bars[-5:]})
    if not dates:
        raise SwingError("no bars")
    day = asof or dates[-1]
    out["date"] = day.isoformat()
    mult, label = regime_at(index_ind, day) if cfg.get("regime", True) else (1.0, "filter off")
    out["regime"] = {"size": mult, "label": label}
    for sym, bars in sorted(data.items()):
        ind = indicators(bars)
        i = ind["idx"].get(day)
        if i is None:
            out["skipped"].append("%s: no bar on %s" % (sym, day))
            continue
        rs = rel_strength(ind, index_ind, i)
        for name in setups:
            sig = SETUP_FUNCS[name](sym, ind, i, cfg)
            if sig:
                sig["rel_strength_pct"] = round(rs * 100, 1)
                out["signals"].append(sig)
        nb = near_breakout(sym, ind, i, cfg)
        if nb:
            nb["rel_strength_pct"] = round(rs * 100, 1)
            out["near_breakout"].append(nb)
    out["signals"].sort(key=lambda s: -s["rel_strength_pct"])
    out["near_breakout"].sort(key=lambda s: (s["distance_pct"], -s["rel_strength_pct"]))
    return out


def print_scan(res, n_symbols):
    print("Swing scan after the close of %s, %d stocks." % (res["date"], n_symbols))
    print("Market: %s" % res["regime"]["label"])
    if res["regime"]["size"] == 0:
        print("No new swing trades in this market. The lists below are for study only.")
    print()
    if res["signals"]:
        print("Setups that fired today (enter tomorrow, if you take them):")
        for s in res["signals"]:
            print("  %-12s %-9s close %9s  stop %9s (%.1f%% risk)  RS %+.1f%% vs Nifty" % (
                s["symbol"], s["setup"], "%.2f" % s["close"], "%.2f" % s["stop"], s["risk_pct"],
                s["rel_strength_pct"]))
            print("               %s" % s["why"])
    else:
        print("No setup fired today.")
    print()
    if res["near_breakout"]:
        print("On the verge of a breakout (watch, not a trade yet):")
        for s in res["near_breakout"]:
            print("  %-12s close %9s  %.1f%% under its 30-day high; trigger above %.2f, stop %.2f "
                  "(%.1f%% risk); base %.1f%% deep, volume %.2fx avg, RSI %.0f" % (
                      s["symbol"], "%.2f" % s["close"], s["distance_pct"], s["trigger"], s["stop"],
                      s["risk_pct_at_trigger"], s["base_depth_pct"], s["volume_dryup"], s["rsi"]))
    else:
        print("No stock is coiling near a breakout today.")
    print()
    print("A setup matched a rule; it is not a recommendation. Check /trader-news and results dates "
          "before any of them. Not financial advice.")


# ---------------------------------------------------------------- plan

def plan_trade(sig, cfg, open_positions=None, capital=None):
    """A signal -> a sized CNC limit buy plus the GTT OCO stop/target."""
    capital = capital or cfg["swing_capital"]
    open_positions = open_positions or []
    if len(open_positions) >= cfg["max_positions"]:
        raise SwingError("already %d swing positions (max %d)" % (len(open_positions), cfg["max_positions"]))
    c = sig["close"]
    tick = tick_for(c)
    limit = ceil_tick(c * (1 + cfg["entry_buffer_pct"] / 100.0), tick)
    stop = sig["stop"]
    risk = limit - stop
    if risk <= 0:
        raise SwingError("the stop %.2f is not below the entry %.2f" % (stop, limit))
    mult = sig.get("size_mult", 1.0)
    risk_inr = capital * cfg["risk_per_trade_pct"] / 100.0 * mult
    open_risk = sum(max(0.0, (p["entry"] - p["stop"]) * p["qty"]) for p in open_positions)
    room = capital * cfg["max_open_risk_pct"] / 100.0 - open_risk
    risk_inr = min(risk_inr, room)
    if risk_inr <= 0:
        raise SwingError("open risk is already at the %.0f%% limit" % cfg["max_open_risk_pct"])
    ch_stop = charges(1, limit, stop)
    qty = int(risk_inr // (risk + (ch_stop["total"] - CHARGES["dp_per_sell"]) + 1e-9))
    qty = min(qty, int(capital * cfg["max_position_pct"] / 100.0 // limit))
    while qty > 0 and qty * risk + charges(qty, limit, stop)["total"] > risk_inr:
        qty -= 1
    if qty < 1:
        raise SwingError("one share risks more than the %s allowed: skip it" % money(risk_inr))
    target = ceil_tick(limit + cfg["target_r"] * risk, tick) if sig["setup"] not in NO_TARGET else None
    at_stop = charges(qty, limit, stop)
    loss = qty * risk + at_stop["total"]
    gain = None
    if target:
        gain = qty * (target - limit) - charges(qty, limit, target)["total"]
    order = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sig["symbol"], "transaction_type": "BUY",
             "quantity": qty, "product": "CNC", "order_type": "LIMIT", "price": limit, "validity": "DAY",
             "tag": "swing"}
    sl_limit = floor_tick(stop * 0.99, tick_for(stop))
    gtt = {"exchange": "NSE", "tradingsymbol": sig["symbol"], "transaction_type": "SELL", "product": "CNC",
           "trigger_type": "two-leg" if target else "single"}
    if target:
        gtt.update({"lower_trigger_value": stop, "lower_limit_price": sl_limit, "lower_quantity": qty,
                    "upper_trigger_value": target, "upper_limit_price": target, "upper_quantity": qty})
    else:
        gtt.update({"trigger_value": stop, "limit_price": sl_limit, "quantity": qty})
    plan = {"setup": sig["setup"], "signal_date": sig["date"], "signal_close": c, "entry_limit": limit,
            "stop": stop, "target": target, "risk_per_share": round(risk, 2), "qty": qty,
            "value": round(qty * limit, 2), "loss_at_stop": round(loss, 2), "charges_at_stop": at_stop["total"],
            "gain_at_target": round(gain, 2) if gain is not None else None,
            "time_stop_days": hold_limit(sig["setup"], cfg),
            "breakeven_at_r": cfg["breakeven_at_r"], "why": sig.get("why", "")}
    order["gtt"] = gtt
    order["plan"] = plan
    order["why"] = "%s swing on %s: %d shares, %s at risk (%.1f%% of %s)" % (
        sig["setup"], sig["symbol"], qty, money(loss), loss / capital * 100, money(capital))
    return order


def print_plan(order, cfg):
    p, g = order["plan"], order["gtt"]
    print("%s swing on %s (signal %s, close %.2f)" % (p["setup"], order["tradingsymbol"], p["signal_date"],
                                                     p["signal_close"]))
    print("  %s" % p["why"])
    print("  Buy      %d shares, CNC delivery, LIMIT %.2f (0.5%% above the close, so a gap up is not chased)"
          % (p["qty"], p["entry_limit"]))
    print("  Value    %s" % money(p["value"]))
    print("  Stop     %.2f  (risk %.2f a share)" % (p["stop"], p["risk_per_share"]))
    if p["target"]:
        print("  Target   %.2f  (%.0fR)" % (p["target"], cfg["target_r"]))
    else:
        print("  Target   none: %s" % ("rsi60 exits on a daily close with RSI under 60" if p["setup"] == "rsi60"
                                         else "momentum sells at the close of day %d" % p["time_stop_days"]))
    print("  At stop  lose about %s including %s of charges (a gap down can lose more)"
          % (money(p["loss_at_stop"]), money(p["charges_at_stop"])))
    if p["gain_at_target"] is not None:
        print("  At target gain about %s after charges" % money(p["gain_at_target"]))
    print("  Then     move the stop to %.2f once a close is %.0fR up; exit at the close after %d trading days "
          "if nothing has hit" % (p["entry_limit"], p["breakeven_at_r"], p["time_stop_days"]))
    print()
    print("After the buy fills, the exits go to Kite as one GTT (%s) order:" % g["trigger_type"])
    if g["trigger_type"] == "two-leg":
        print("  stop leg   SELL %d when the price touches %.2f, limit %.2f" % (
            g["lower_quantity"], g["lower_trigger_value"], g["lower_limit_price"]))
        print("  target leg SELL %d when the price touches %.2f, limit %.2f" % (
            g["upper_quantity"], g["upper_trigger_value"], g["upper_limit_price"]))
        print("  Whichever triggers first cancels the other. A GTT places a LIMIT order: in a gap below the")
        print("  limit it may not fill. Check the position the next morning.")
    else:
        print("  SELL %d when the price touches %.2f, limit %.2f" % (g["quantity"], g["trigger_value"],
                                                                  g["limit_price"]))
    print()
    print("Nothing was placed. trader-risk checks it, then trader-broker asks you yes or no. Not financial advice.")


# ---------------------------------------------------------------- backtest

def backtest(data, index, cfg, start=None, end=None, setups=None, capital=None, entry="open"):
    """entry="close": momentum signals buy at the signal day's close (a 3:20 PM buy) instead of the next open."""
    setups = setups or cfg["setups"]
    capital = capital or cfg["swing_capital"]
    inds = {s: indicators(b) for s, b in data.items()}
    index_ind = indicators(index) if index else None
    all_dates = sorted({b["d"] for bars in data.values() for b in bars})
    if index:
        all_dates = sorted(set(all_dates) | {b["d"] for b in index})
    dates = [d for d in all_dates if (start is None or d >= start) and (end is None or d <= end)]
    if not dates:
        raise SwingError("no dates in the range")
    cash = float(capital)
    positions, pending, trades, curve = [], [], [], []
    near_flags, last_flag = [], {}

    def bar(sym, d):
        i = inds[sym]["idx"].get(d)
        return (i, inds[sym]["bars"][i]) if i is not None else (None, None)

    def close_pos(p, d, price, reason):
        nonlocal cash
        ch = charges(p["qty"], sell=price)
        proceeds = p["qty"] * price - ch["total"]
        cash += proceeds
        pnl = proceeds - p["cost"]
        trades.append({"symbol": p["symbol"], "setup": p["setup"], "entry_date": p["entry_date"].isoformat(),
                       "exit_date": d.isoformat(), "entry": round(p["entry"], 2), "exit": round(price, 2),
                       "qty": p["qty"], "pnl": round(pnl, 2), "r": round(pnl / p["risk_inr"], 3),
                       "days": p["days"], "reason": reason})

    def enter(sig, o, d, equity, held):
        nonlocal cash
        stop = sig["stop"]
        top = cfg["momentum_max_risk_pct"] if sig["setup"] == "momentum" else cfg["max_risk_pct"]
        if o <= stop or (o - stop) / o * 100 > top:
            return
        risk = o - stop
        risk_inr = equity * cfg["risk_per_trade_pct"] / 100.0 * sig["mult"]
        open_risk = sum(max(0.0, (p["entry"] - p["stop"]) * p["qty"]) for p in positions)
        risk_inr = min(risk_inr, equity * cfg["max_open_risk_pct"] / 100.0 - open_risk)
        if risk_inr <= 0:
            return
        qty = int(risk_inr // risk)
        qty = min(qty, int(equity * cfg["max_position_pct"] / 100.0 // o), int(cash // (o * 1.0015)))
        if qty < 1:
            return
        ch = charges(qty, buy=o)
        cost = qty * o + ch["total"]
        cash -= cost
        target = o + cfg["target_r"] * risk if sig["setup"] not in NO_TARGET else None
        positions.append({"symbol": sig["symbol"], "setup": sig["setup"], "entry": o, "stop": stop,
                          "stop0": stop, "target": target, "qty": qty, "cost": cost, "entry_date": d,
                          "risk_inr": qty * risk, "days": 0, "last": o, "at_close": sig.get("at_close", False)})
        held.add(sig["symbol"])

    for d in dates:
        equity = cash + sum(p["qty"] * p["last"] for p in positions)
        # 1. yesterday's signals enter at today's open, best relative strength first
        held = {p["symbol"] for p in positions}
        for sig in sorted(pending, key=lambda s: -s["rs"]):
            if len(positions) >= cfg["max_positions"] or sig["symbol"] in held:
                continue
            i, b = bar(sig["symbol"], d)
            if b is not None:
                enter(sig, b["o"], d, equity, held)
        pending = []
        # 2. stops and targets during the day; then the close
        still = []
        for p in positions:
            i, b = bar(p["symbol"], d)
            if b is None:
                still.append(p)
                continue
            if p.get("at_close") and d == p["entry_date"]:
                still.append(p)
                continue
            p["days"] += 1 if (d != p["entry_date"] or p.get("at_close")) else 0
            if b["o"] <= p["stop"] and (d != p["entry_date"]):
                close_pos(p, d, b["o"], "gap through stop")
                continue
            if b["l"] <= p["stop"]:
                close_pos(p, d, p["stop"], "stop" if p["stop"] < p["entry"] else "breakeven stop")
                continue
            if p["target"] and b["h"] >= p["target"]:
                close_pos(p, d, max(p["target"], b["o"]) if d != p["entry_date"] else p["target"], "target")
                continue
            ind = inds[p["symbol"]]
            r_now = (b["c"] - p["entry"]) / (p["entry"] - p["stop0"])
            if p["setup"] == "rsi60" and ind["rsi"][i] is not None and ind["rsi"][i] < 60:
                close_pos(p, d, b["c"], "RSI under 60")
                continue
            if p["days"] >= hold_limit(p["setup"], cfg):
                close_pos(p, d, b["c"], "hold period over" if p["setup"] == "momentum" else "time stop")
                continue
            if r_now >= cfg["breakeven_at_r"] and p["setup"] != "momentum":
                p["stop"] = max(p["stop"], p["entry"])
            p["last"] = b["c"]
            still.append(p)
        positions = still
        equity = cash + sum(p["qty"] * p["last"] for p in positions)
        curve.append((d, equity))
        # 3. today's signals, for tomorrow's open (or, for momentum with entry="close", bought now)
        closers = []
        mult, _ = regime_at(index_ind, d) if cfg.get("regime", True) else (1.0, "")
        for sym, ind in inds.items():
            i = ind["idx"].get(d)
            if i is None:
                continue
            if mult > 0:
                for name in setups:
                    sig = SETUP_FUNCS[name](sym, ind, i, cfg)
                    if not sig:
                        continue
                    rec = {"symbol": sym, "setup": name, "stop": sig["stop"], "mult": mult,
                           "rs": rel_strength(ind, index_ind, i)}
                    if entry == "close" and name == "momentum":
                        rec["at_close"] = True
                        closers.append((rec, ind["c"][i]))
                    else:
                        pending.append(rec)
            nb = near_breakout(sym, ind, i, cfg)
            if nb and (sym not in last_flag or (d - last_flag[sym]).days > 14):
                last_flag[sym] = d
                near_flags.append((sym, i, nb))
        if closers:
            held = {p["symbol"] for p in positions}
            for rec, px in sorted(closers, key=lambda x: -x[0]["rs"]):
                if len(positions) < cfg["max_positions"] and rec["symbol"] not in held:
                    enter(rec, px, d, equity, held)
            curve[-1] = (d, cash + sum(p["qty"] * p["last"] for p in positions))
    end_d = dates[-1]
    for p in positions:
        i, b = bar(p["symbol"], end_d)
        close_pos(p, end_d, p["last"], "still open at the end")
    # near-breakout follow-through: within 10 sessions, did it close above the trigger before the stop?
    nb_stats = {"flags": 0, "broke_out": 0, "failed": 0, "neither": 0}
    for sym, i, nb in near_flags:
        bars = inds[sym]["bars"]
        if i + 10 >= len(bars):
            continue
        nb_stats["flags"] += 1
        res = "neither"
        for j in range(i + 1, i + 11):
            if bars[j]["l"] <= nb["stop"]:
                res = "failed"
                break
            if bars[j]["c"] >= nb["trigger"]:
                res = "broke_out"
                break
        nb_stats[res] += 1
    bench = None
    if index:
        ib = [b for b in index if dates[0] <= b["d"] <= dates[-1]]
        if len(ib) > 1:
            bench = ib[-1]["c"] / ib[0]["c"] - 1
    return {"start": dates[0].isoformat(), "end": dates[-1].isoformat(), "capital": capital, "trades": trades,
            "curve": [(d.isoformat(), round(e, 2)) for d, e in curve], "near_breakout": nb_stats,
            "nifty_return": bench, "setups": list(setups), "symbols": len(data)}


def stats(trades, curve=None, capital=None):
    n = len(trades)
    out = {"trades": n}
    if not n:
        return out
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    out.update({"win_rate": round(len(wins) / n * 100, 1), "avg_r": round(sum(rs) / n, 3),
                "avg_win_r": round(sum(wins) / len(wins), 2) if wins else 0.0,
                "avg_loss_r": round(sum(losses) / len(losses), 2) if losses else 0.0,
                "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
                "avg_days": round(sum(t["days"] for t in trades) / n, 1),
                "pnl": round(sum(t["pnl"] for t in trades), 2)})
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    out["worst_losing_streak"] = worst
    if curve:
        peak, dd = curve[0][1], 0.0
        for _, e in curve:
            peak = max(peak, e)
            dd = max(dd, (peak - e) / peak)
        out["max_drawdown_pct"] = round(dd * 100, 1)
        start, end = curve[0][1], curve[-1][1]
        out["return_pct"] = round((end / start - 1) * 100, 1)
        days = (datetime.date.fromisoformat(curve[-1][0]) - datetime.date.fromisoformat(curve[0][0])).days
        if days > 180 and end > 0:
            out["cagr_pct"] = round(((end / start) ** (365.25 / days) - 1) * 100, 1)
    return out


def print_backtest(res, split):
    trades = res["trades"]
    print("Swing backtest, %s to %s, %d stocks, setups: %s. Start %s." % (
        res["start"], res["end"], res["symbols"], ", ".join(res["setups"]), money(res["capital"])))
    print("Entry at the next open, Zerodha delivery charges included, gaps through the stop taken at the open.")
    print()
    s = stats(trades, res["curve"])
    _print_stats("All trades", s)
    if res["nifty_return"] is not None:
        print("    Nifty 50 over the same dates: %+.1f%% (price only, no dividends)" % (res["nifty_return"] * 100))
    for name in res["setups"]:
        sub = [t for t in trades if t["setup"] == name]
        _print_stats("  %s" % name, stats(sub))
    early = [t for t in trades if t["entry_date"] < split]
    late = [t for t in trades if t["entry_date"] >= split]
    print()
    print("Split at %s: the later period is the check. A rule that only worked early is curve fitting." % split)
    _print_stats("  before %s" % split, stats(early))
    _print_stats("  from %s" % split, stats(late))
    nb = res["near_breakout"]
    if nb["flags"]:
        print()
        print("Near-breakout watch list: %d flags; within 10 sessions %d closed above the trigger first "
              "(%.0f%%), %d hit the stop level first, %d did neither." % (
                  nb["flags"], nb["broke_out"], nb["broke_out"] / nb["flags"] * 100, nb["failed"], nb["neither"]))
    print()
    print("These stocks are today's large caps, so the test leans optimistic (survivorship). Past results are")
    print("not a forecast. Not financial advice.")


def _print_stats(label, s):
    if not s.get("trades"):
        print("%-22s no trades" % label)
        return
    line = "%-22s %4d trades  win %5.1f%%  avg %+.2fR  (wins %+.2fR, losses %+.2fR)  PF %s  %.1f days" % (
        label, s["trades"], s["win_rate"], s["avg_r"], s["avg_win_r"], s["avg_loss_r"],
        "%.2f" % s["profit_factor"] if s.get("profit_factor") else "-", s["avg_days"])
    print(line)
    if "return_pct" in s:
        print("%-22s return %+.1f%%%s, max drawdown %.1f%%, worst losing streak %d, P&L %s" % (
            "", s["return_pct"], (" (%+.1f%% a year)" % s["cagr_pct"]) if "cagr_pct" in s else "",
            s["max_drawdown_pct"], s["worst_losing_streak"], money(s["pnl"])))


# ---------------------------------------------------------------- study: what holding N days returned

def forward_study(data, index, cfg, setups, horizons=(1, 2, 3, 5, 10), start=None, end=None, notional=100000.0,
                  entry="open"):
    """Every signal, no position limits: buy at the next open, sell at the close
    of day N (the entry day is day 1). Returns in percent after delivery charges
    on a notional position; "with stop" exits at the signal's stop (or the open,
    if it gaps below) when touched first. Also the same N-day return from every
    day in the same stocks, as the baseline to beat. entry="close" buys at the
    signal day's close instead (a buy in the last minutes, 3:20 to 3:30 PM);
    then day 1 is the next session."""
    index_ind = indicators(index) if index else None
    rows, base = [], {h: [] for h in horizons}
    for sym, bars in data.items():
        ind = indicators(bars)
        n = len(bars)
        for i in range(n - 1):
            d = bars[i]["d"]
            if (start and d < start) or (end and d > end):
                continue
            at_close = entry == "close"
            e = bars[i]["c"] if at_close else bars[i + 1]["o"]
            for h in horizons:
                j = i + h          # open entry: day 1 is the entry day; close entry: day 1 is the next session
                if j < n:
                    base[h].append((bars[j]["c"] / e - 1) * 100)
            mult = regime_at(index_ind, d)[0] if cfg.get("regime", True) else 1.0
            for name in setups:
                sig = SETUP_FUNCS[name](sym, ind, i, cfg)
                if not sig:
                    continue
                qty = max(1, int(notional // e))
                rec = {"symbol": sym, "setup": name, "date": d.isoformat(), "entry": e, "regime": mult,
                       "hold": {}, "stop": {}}
                stopped = None
                for k in range(1, max(horizons) + 1):
                    j = i + k
                    if j >= n:
                        break
                    bj = bars[j]
                    if stopped is None:
                        if (k > 1 or at_close) and bj["o"] <= sig["stop"]:
                            stopped = bj["o"]
                        elif bj["l"] <= sig["stop"]:
                            stopped = sig["stop"]
                    if k in horizons:
                        for key, px in (("hold", bj["c"]), ("stop", stopped if stopped is not None else bj["c"])):
                            ch = charges(qty, e, px)["total"]
                            rec[key][k] = round(((px - e) * qty - ch) / (e * qty) * 100, 3)
                rows.append(rec)
    return rows, base


def _pct_stats(xs):
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x <= 0]
    return {"n": n, "avg": round(sum(xs) / n, 2), "median": round(xs[n // 2], 2),
            "win_rate": round(len(wins) / n * 100, 1),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "best": round(xs[-1], 1), "worst": round(xs[0], 1)}


def print_study(rows, base, horizons, setups, n_symbols, split, entry="open"):
    if entry == "close":
        print("What holding N days returned: every signal, bought at the signal day's close (3:20 to 3:30 PM),")
        print("sold at the close N sessions later, after Zerodha delivery charges on a Rs 1 lakh position. %d stocks."
              % n_symbols)
    else:
        print("What holding N days returned: every signal, bought at the next open, sold at the close of day N")
        print("(the entry day is day 1), after Zerodha delivery charges on a Rs 1 lakh position. %d stocks."
              % n_symbols)
    for name in setups:
        rs = [r for r in rows if r["setup"] == name]
        print()
        print("%s: %d signals (%d when the market filter allowed new trades)" % (
            name, len(rs), sum(1 for r in rs if r["regime"] > 0)))
        if not rs:
            continue
        print("  %-6s %-20s %8s %8s %8s %9s %9s %8s" % ("days", "", "avg %", "median", "win %", "avg win", "avg loss",
                                                        "worst"))
        for h in horizons:
            for key, label in (("hold", "just hold"), ("stop", "with the stop")):
                st = _pct_stats([r[key][h] for r in rs if h in r[key]])
                if st:
                    print("  %-6d %-20s %+8.2f %+8.2f %7.1f%% %+9.2f %+9.2f %+8.1f" % (
                        h, label, st["avg"], st["median"], st["win_rate"], st["avg_win"], st["avg_loss"], st["worst"]))
            b = _pct_stats(base[h])
            if b:
                print("  %-6d %-20s %+8.2f %+8.2f %7.1f%%   (any day in these stocks, before charges)" % (
                    h, "baseline", b["avg"], b["median"], b["win_rate"]))
        early = [r for r in rs if r["date"] < split]
        late = [r for r in rs if r["date"] >= split]
        h = 5 if 5 in horizons else horizons[-1]
        for lab, part in (("before %s" % split, early), ("from %s" % split, late)):
            st = _pct_stats([r["stop"][h] for r in part if h in r["stop"]])
            if st:
                print("  %d days with the stop, %s: %d signals, avg %+.2f%%, win %.1f%%" % (
                    h, lab, st["n"], st["avg"], st["win_rate"]))
        on = [r for r in rs if r["regime"] > 0]
        off = [r for r in rs if r["regime"] == 0]
        for lab, part in (("market filter on (trades allowed)", on), ("market filter off (bear market)", off)):
            st = _pct_stats([r["stop"][h] for r in part if h in r["stop"]])
            if st:
                print("  %d days with the stop, %s: %d signals, avg %+.2f%%, win %.1f%%" % (
                    h, lab, st["n"], st["avg"], st["win_rate"]))
    print()
    print("Average % is per trade, before position sizing. These stocks are today's survivors, so the test leans")
    print("optimistic. Past results are not a forecast. Not financial advice.")


# ---------------------------------------------------------------- manage

def manage(positions, data, cfg, asof=None):
    """Open swing positions -> what to do after today's close."""
    out = []
    for p in positions:
        sym = plain_symbol(p["symbol"])
        bars = data.get(sym)
        if not bars:
            out.append({"symbol": sym, "action": "no data", "note": "no daily bars for it"})
            continue
        ind = indicators(bars)
        i = ind["idx"].get(asof) if asof else len(bars) - 1
        if i is None:
            out.append({"symbol": sym, "action": "no data", "note": "no bar on %s" % asof})
            continue
        b = bars[i]
        entry, stop, stop0 = float(p["entry"]), float(p["stop"]), float(p.get("initial_stop", p["stop"]))
        risk = entry - stop0
        r_now = (b["c"] - entry) / risk if risk > 0 else 0.0
        entry_d = datetime.date.fromisoformat(p["entry_date"])
        held = sum(1 for x in bars[:i + 1] if x["d"] > entry_d)
        setup = p.get("setup", "")
        limit_days = hold_limit(setup, cfg)
        row = {"symbol": sym, "close": b["c"], "entry": entry, "stop": stop, "r_now": round(r_now, 2),
               "days": held}
        if b["c"] <= stop:
            row.update(action="check fill", note="closed at or under the stop: the GTT should have sold; check Kite")
        elif setup == "rsi60" and ind["rsi"][i] is not None and ind["rsi"][i] < 60:
            row.update(action="exit", note="daily RSI %.0f closed under 60: the rsi60 exit rule" % ind["rsi"][i])
        elif held >= limit_days:
            row.update(action="exit", note="time stop: %d trading days without hitting stop or target" % held)
        elif r_now >= cfg["breakeven_at_r"] and stop < entry:
            row.update(action="move stop", new_stop=round(entry, 2),
                       note="closed %.1fR up: move the GTT stop to the entry %.2f" % (r_now, entry))
        else:
            row.update(action="hold", note="inside the plan")
        out.append(row)
    return out


# ---------------------------------------------------------------- CLI

def _symbols_arg(s):
    return [plain_symbol(x) for x in s.replace(",", " ").split() if x]


def _get_data(a):
    if a.data:
        data, index = load_folder(a.data)
        if a.symbols:
            want = set(_symbols_arg(a.symbols))
            data = {k: v for k, v in data.items() if k in want}
        if getattr(a, "quotes", None):
            with open(a.quotes, encoding="utf-8") as fh:
                merge_quotes(data, json.load(fh))
        return data, index, {}
    if a.symbols:
        return load_yahoo(_symbols_arg(a.symbols))
    raise SwingError("give --data FOLDER (Kite daily candles) or --symbols A,B,C (Yahoo)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Swing trading for NSE stocks: scan, plan, backtest, manage.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--data", metavar="FOLDER", help="Kite daily candles, one SYMBOL.json each, plus NIFTY_50.json")
        p.add_argument("--symbols", help="comma-separated NSE symbols (Yahoo, or a subset of --data)")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("scan", help="setups that fired today, and stocks near a breakout")
    common(p)
    p.add_argument("--asof", help="scan as of this date (YYYY-MM-DD)")
    p.add_argument("--setups", help="comma-separated: pullback, breakout, rsi60, momentum")
    p.add_argument("--quotes", metavar="FILE",
                   help="Kite get_quotes saved to a file: adds today's live bar (for a 3:15 PM momentum scan)")
    p.add_argument("--write", nargs="?", const="trader/swing-watchlist.json", metavar="PATH")

    p = sub.add_parser("plan", help="size one setup into a CNC buy + GTT stop/target")
    common(p)
    p.add_argument("symbol")
    p.add_argument("--setup", choices=SETUPS, help="which setup (default: the one that fired)")
    p.add_argument("--asof")
    p.add_argument("--capital", type=float, help="swing capital in rupees (default: config swing_capital)")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH")

    p = sub.add_parser("backtest", help="test the rules on history, with costs")
    common(p)
    p.add_argument("--from", dest="start")
    p.add_argument("--to", dest="end")
    p.add_argument("--split", help="date that starts the check period (default: 70%% of the way)")
    p.add_argument("--setups")
    p.add_argument("--capital", type=float)
    p.add_argument("--trades", metavar="CSV", help="write every trade to a CSV")
    p.add_argument("--entry", choices=["open", "close"], default="open",
                   help="momentum: buy at the signal day's close instead of the next open")

    p = sub.add_parser("study", help="what holding a signal 1, 2, 3, 5 or 10 days returned")
    common(p)
    p.add_argument("--setups", default="momentum")
    p.add_argument("--days", default="1,2,3,5,10")
    p.add_argument("--entry", choices=["open", "close"], default="open",
                   help="buy at the next open (default) or at the signal day's close")
    p.add_argument("--from", dest="start")
    p.add_argument("--to", dest="end")
    p.add_argument("--split")

    p = sub.add_parser("manage", help="open swing positions: breakeven, time stop, exits")
    common(p)
    p.add_argument("--positions", default="trader/swing-positions.json")
    p.add_argument("--asof")

    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        data, index, failed = _get_data(a)
        if not data:
            raise SwingError("no stock data")
        for s, e in sorted(failed.items()):
            print("skipped %s: %s" % (s, e), file=sys.stderr)
        asof = datetime.date.fromisoformat(a.asof) if getattr(a, "asof", None) else None
        if a.cmd == "scan":
            setups = a.setups.split(",") if a.setups else None
            res = scan(data, index, cfg, asof, setups)
            if a.write:
                os.makedirs(os.path.dirname(a.write) or ".", exist_ok=True)
                with open(a.write, "w", encoding="utf-8") as fh:
                    json.dump(res, fh, indent=2, ensure_ascii=False)
            if a.json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                print_scan(res, len(data))
                if a.write:
                    print("Wrote %s" % a.write)
            return 0 if (res["signals"] or res["near_breakout"]) else 1
        if a.cmd == "plan":
            sym = plain_symbol(a.symbol)
            if sym not in data:
                raise SwingError("no daily bars for %s" % sym)
            res = scan({sym: data[sym]}, index, cfg, asof, [a.setup] if a.setup else None)
            sigs = res["signals"]
            if not sigs:
                raise SwingError("no %s setup on %s as of %s: nothing to plan"
                                 % (a.setup or "swing", sym, res["date"]))
            sig = sigs[0]
            if res["regime"]["size"] == 0:
                raise SwingError(res["regime"]["label"])
            sig["size_mult"] = res["regime"]["size"]
            open_pos = []
            if os.path.exists(a.portfolio):
                with open(a.portfolio, encoding="utf-8") as fh:
                    pf = json.load(fh)
                open_pos = [x for x in pf.get("swing", []) if x.get("qty")]
            order = plan_trade(sig, cfg, open_pos, a.capital)
            order["paper"] = bool(cfg.get("paper", True))
            if a.json:
                print(json.dumps(order, indent=2, ensure_ascii=False))
            else:
                print("Market: %s" % res["regime"]["label"])
                print_plan(order, cfg)
            if a.write:
                doc = {"created_by": "trader-swing", "status": "proposed", "paper": order["paper"],
                       "note": "Swing order. Nothing is placed until trader-risk passes it and you say yes.",
                       "orders": [order]}
                os.makedirs(os.path.dirname(a.write) or ".", exist_ok=True)
                with open(a.write, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, indent=2, ensure_ascii=False)
                if not a.json:
                    print("Wrote %s" % a.write)
            return 0
        if a.cmd == "backtest":
            start = datetime.date.fromisoformat(a.start) if a.start else None
            end = datetime.date.fromisoformat(a.end) if a.end else None
            res = backtest(data, index, cfg, start, end, a.setups.split(",") if a.setups else None, a.capital,
                           entry=a.entry)
            if a.split:
                split = a.split
            else:
                ds = [d for d, _ in res["curve"]]
                split = ds[int(len(ds) * 0.7)]
            if a.trades:
                with open(a.trades, "w", encoding="utf-8") as fh:
                    cols = ["symbol", "setup", "entry_date", "exit_date", "entry", "exit", "qty", "pnl", "r", "days",
                            "reason"]
                    fh.write(",".join(cols) + "\n")
                    for t in res["trades"]:
                        fh.write(",".join(str(t[c]) for c in cols) + "\n")
            if a.json:
                res["stats"] = stats(res["trades"], res["curve"])
                res["split"] = split
                res["stats_before"] = stats([t for t in res["trades"] if t["entry_date"] < split])
                res["stats_after"] = stats([t for t in res["trades"] if t["entry_date"] >= split])
                res.pop("curve")
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                print_backtest(res, split)
            return 0
        if a.cmd == "study":
            horizons = tuple(int(x) for x in a.days.split(","))
            setups = a.setups.split(",")
            for name in setups:
                if name not in SETUP_FUNCS:
                    raise SwingError("unknown setup %r (%s)" % (name, ", ".join(SETUPS)))
            start = datetime.date.fromisoformat(a.start) if a.start else None
            end = datetime.date.fromisoformat(a.end) if a.end else None
            rows, base = forward_study(data, index, cfg, setups, horizons, start, end, entry=a.entry)
            dates = sorted(r["date"] for r in rows)
            split = a.split or (dates[int(len(dates) * 0.7)] if dates else "2025-01-01")
            if a.json:
                out = {"split": split, "setups": {}}
                for name in setups:
                    rs = [r for r in rows if r["setup"] == name]
                    out["setups"][name] = {str(h): {"hold": _pct_stats([r["hold"][h] for r in rs if h in r["hold"]]),
                                                    "with_stop": _pct_stats([r["stop"][h] for r in rs if h in r["stop"]]),
                                                    "baseline": _pct_stats(base[h])} for h in horizons}
                print(json.dumps(out, indent=2))
            else:
                print_study(rows, base, horizons, setups, len(data), split, a.entry)
            return 0
        if a.cmd == "manage":
            if not os.path.exists(a.positions):
                raise SwingError("no %s: list your swing positions there "
                                 "([{symbol, entry, stop, initial_stop, entry_date, setup, qty}])" % a.positions)
            with open(a.positions, encoding="utf-8") as fh:
                positions = json.load(fh)
            rows = manage(positions, data, cfg, asof)
            if a.json:
                print(json.dumps(rows, indent=2, ensure_ascii=False))
            else:
                for r in rows:
                    if "close" in r:
                        print("%-12s %-10s close %.2f  entry %.2f  stop %.2f  %+.2fR  day %d: %s" % (
                            r["symbol"], r["action"].upper(), r["close"], r["entry"], r["stop"], r["r_now"],
                            r["days"], r["note"]))
                    else:
                        print("%-12s %s: %s" % (r["symbol"], r["action"].upper(), r["note"]))
                print("Changing a GTT or selling goes through trader-broker and your yes. Not financial advice.")
            return 0
    except SwingError as e:
        print("swing.py: %s" % e, file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
