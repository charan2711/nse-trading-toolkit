#!/usr/bin/env python3
"""Builds RELIANCE-60d5m.json.gz: 41 synthetic NSE sessions of 5-minute Kite
candles (seeded random walks, 72 bars each, 9:15 to 3:15 PM IST like an F&O
stock under the closing auction) followed by the real 2026-11-06 session from
RELIANCE-2026-11-06-5m.json. The synthetic days are test data, not prices:
they exist so the Time Machine has a pool of sessions to draw from offline.

    python3 tests/fixtures/india/make_synthetic.py
"""
import datetime
import gzip
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
HOLIDAYS = {"2026-09-14", "2026-10-02", "2026-10-20"}
REAL = os.path.join(HERE, "RELIANCE-2026-11-06-5m.json")


def trading_days(end, n):
    out, d = [], end - datetime.timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5 and d.isoformat() not in HOLIDAYS:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return sorted(out)


def main():
    rng = random.Random(2026)
    real = json.load(open(REAL, encoding="utf-8"))
    days = trading_days(datetime.date(2026, 11, 6), 41)
    price = 1250.0
    candles = []
    for d in days:
        price *= 1 + rng.gauss(0, 0.008)                  # the overnight gap
        for i in range(72):
            t = datetime.datetime.combine(d, datetime.time(9, 15)) + datetime.timedelta(minutes=5 * i)
            vol = 0.0030 if i < 3 else 0.0016 if i < 12 else 0.0011
            o = price
            c = o * (1 + rng.gauss(0.00002, vol))
            h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
            v = int(rng.uniform(1.5, 3.0) * (900000 if i == 0 else 300000 if i < 12 else 180000))
            candles.append({"date": t.strftime("%Y-%m-%dT%H:%M:00+05:30"), "open": round(o, 1),
                            "high": round(h, 1), "low": round(lo, 1), "close": round(c, 1), "volume": v})
            price = c
    candles += real["candles"]
    doc = {"symbol": "RELIANCE", "exchange": "NSE", "interval": "5minute",
           "note": "41 SYNTHETIC sessions (seeded random walks, test data only) then the real 2026-11-06 session",
           "candles": candles}
    with gzip.open(os.path.join(HERE, "RELIANCE-60d5m.json.gz"), "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    print("wrote %d candles over %d sessions" % (len(candles), len(days) + 1))


if __name__ == "__main__":
    main()
