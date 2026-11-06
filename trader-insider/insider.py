#!/usr/bin/env python3
"""Insider Tracker for trader-insider: who inside a company, and which big
money, is buying or selling. Standard library only.

    python3 insider.py pit                              # insider trades disclosed to NSE in the last 7 days
    python3 insider.py pit --symbol RELIANCE --days 90  # one company, three months
    python3 insider.py pit --buys --market-only         # promoters' and directors' open-market buys
    python3 insider.py deals                            # today's NSE bulk and block deals
    python3 insider.py deals --block --mine             # block deals in what you hold or watch
    python3 insider.py pit --file pit.csv               # a CSV you downloaded from nseindia.com
    python3 insider.py pit --offline tests/fixtures/india/insider --now 2026-11-06

What it reads (all public, free):

  pit    SEBI's Prohibition of Insider Trading (PIT) Regulations make
         promoters, directors, key managerial people and designated employees
         disclose their trades in the company's shares within two trading days
         (Regulation 7(2)); the company passes them to the exchange. NSE
         publishes them at nseindia.com > Companies > Corporate filings >
         Insider trading, and as JSON:
           https://www.nseindia.com/api/corporates-pit?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
         Each row: the company, the person and their category (Promoter,
         Promoter Group, Director, KMP, Designated Person ...), buy or sell,
         shares and rupee value, holding before and after, the trade dates,
         the mode (Market Purchase, Market Sale, ESOP, Gift, Pledge, Inter-se
         transfer ...) and when it was disclosed.
  deals  Bulk deals (one client trading more than 0.5% of a company's shares
         in a day) and block deals (single trades of at least Rs 10 crore in
         the block window), published by NSE every evening:
           https://nsearchives.nseindia.com/content/equities/bulk.csv
           https://nsearchives.nseindia.com/content/equities/block.csv

Signal, not noise: a market purchase by a promoter or director with their own
money is the row worth reading; ESOPs, gifts, pledges and transfers inside a
promoter group are listed but marked as not open-market. Every row is a
disclosure after the fact (up to two trading days late for PIT), not a tip:
the price may have moved, and insiders sell for many reasons.

NSE's site wants a browser: the tool opens www.nseindia.com once for the
cookies, then asks the API. NSE blocks some networks and changes these
endpoints without notice; when it refuses, download the CSV from the page in
a browser and pass --file. Column names are matched loosely, so NSE's CSV,
its JSON and small changes to either all read.

New rows are remembered in trader/state/insider.json: each prints as NEW once.
--mine keeps rows for your tickers only (trader/portfolio.json positions,
trader/watchlist.json hits, trader/plan.json). Exit code 0 when there are
rows to show, 1 when there are none or it could not run.
"""

import argparse
import csv
import datetime
import gzip
import hashlib
import http.cookiejar
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
NSE_HOME = "https://www.nseindia.com/"
PIT_URL = "https://www.nseindia.com/api/corporates-pit?index=equities&from_date=%s&to_date=%s"
PIT_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"
DEALS_URL = "https://nsearchives.nseindia.com/content/equities/%s.csv"
IST = datetime.timedelta(hours=5, minutes=30)
KEEP_DAYS = 120
INSIDERS = ("promoter", "director", "kmp", "key managerial", "designated", "immediate relative", "employee")
MARKET_MODES = ("market purchase", "market sale", "open market", "market")

# Loose column names: the JSON key or the CSV header (upper-cased, spaces squeezed) that holds each field.
PIT_FIELDS = {
    "symbol": ("symbol",),
    "company": ("company", "companyname", "sm_name"),
    "person": ("acqname", "name of the acquirer", "name of the person", "personname", "acquirer"),
    "category": ("personcategory", "category of person", "category"),
    "shares": ("secacq", "no. of securities (acquired", "no. of security (acquired", "securities acquired",
               "secacqno", "quantity"),
    "value": ("secval", "value of security (acquired", "value of securit", "value"),
    "txn": ("tdptransactiontype", "transaction type", "acquisition/disposal transaction type", "buy/sell",
            "transactiontype"),
    "mode": ("acqmode", "mode of acquisition", "mode"),
    "before_pct": ("befacqsharesper", "% shareholding (prior", "shareholding prior"),
    "after_pct": ("afteracqsharesper", "% shareholding (post", "shareholding post"),
    "from": ("acqfromdt", "date of allotment/acquisition from", "acquisition from", "from date", "fromdate"),
    "to": ("acqtodt", "date of allotment/acquisition to", "acquisition to", "to date", "todate"),
    "disclosed": ("intimdt", "date of intimation", "date of initmation", "broadcaste date", "broadcast date",
                  "exchdisstime", "date"),
    "security": ("sectype", "type of security (acquired", "type of security", "type of instrument"),
}
DEAL_FIELDS = {
    "date": ("date",),
    "symbol": ("symbol",),
    "company": ("security name", "name"),
    "client": ("client name", "client"),
    "side": ("buy/sell", "buy / sell", "side"),
    "shares": ("quantity traded", "quantity", "qty"),
    "price": ("trade price", "wght. avg. price", "price"),
}


class InsiderError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def plain_symbol(sym):
    s = str(sym or "").strip().upper()
    if s.startswith("NSE:"):
        s = s[4:]
    if s.endswith(".NS"):
        s = s[:-3]
    return s


def _squeeze(k):
    return " ".join(str(k).replace("\n", " ").replace("﻿", "").split()).lower()


def pick(row, names):
    """The first non-empty value whose key starts with (or equals) one of names."""
    keys = {_squeeze(k): v for k, v in row.items()}
    for n in names:
        n = _squeeze(n)
        for k, v in keys.items():
            if (k == n or k.startswith(n)) and v not in (None, "", "-", "NA", "Nil"):
                return v
    return None


def num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "").replace("%", "").replace("₹", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_day(x):
    """'06-Nov-2026', '06-11-2026', '2026-11-06', '06-Nov-2026 18:30' -> date, or None."""
    s = str(x or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
                "%d-%m-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%B-%Y", "%d %b %Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def inr(x):
    """Rupees the Indian way: Rs 4.5 crore, Rs 12.3 lakh, Rs 85,000."""
    x = float(x or 0)
    if x >= 1e7:
        return "₹%.2f crore" % (x / 1e7)
    if x >= 1e5:
        return "₹%.2f lakh" % (x / 1e5)
    return "₹{:,.0f}".format(x)


def shares(x):
    """Indian grouping: 70,00,000."""
    s = str(int(round(float(x or 0))))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    return ",".join(([head] if head else []) + parts) + "," + tail


def today_ist(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return (now + IST).date()


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    """GETs with one cookie jar: NSE's API answers only after its home page set cookies."""

    def __init__(self, offline=None, pause=0.5):
        self.offline = offline
        self.pause = pause
        self._jar = http.cookiejar.CookieJar()
        self._open = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar)).open
        self._ready = False
        self._last = 0.0

    def get(self, url, name, nse=True, referer=None):
        if self.offline:
            for n in (name, name + ".gz"):
                p = os.path.join(self.offline, n)
                if os.path.isfile(p):
                    opener = gzip.open if n.endswith(".gz") else open
                    with opener(p, "rb") as fh:
                        return fh.read().decode("utf-8", "replace")
            raise InsiderError("offline: no %s in %s" % (name, self.offline))
        wait = self.pause - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        h = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/csv, */*",
             "Accept-Language": "en-IN,en;q=0.9", "Accept-Encoding": "gzip"}
        if referer:
            h["Referer"] = referer
        try:
            if nse and not self._ready:
                with self._open(urllib.request.Request(NSE_HOME, headers=dict(h, Accept="text/html,*/*")),
                                timeout=30) as r:
                    r.read()
                self._ready = True
            with self._open(urllib.request.Request(url, headers=h), timeout=45) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise InsiderError("NSE answered HTTP %d for %s: download the file from %s in a browser and pass "
                               "--file" % (e.code, url.split("?")[0], PIT_PAGE if "pit" in url else url))
        except (urllib.error.URLError, OSError) as e:
            raise InsiderError("could not reach NSE (%s): it blocks some networks; download the file in a "
                               "browser and pass --file" % getattr(e, "reason", e))


def rows_from_text(text):
    """CSV or JSON text -> a list of dict rows. JSON may be a list, or {"data": [...]}."""
    t = text.lstrip("﻿").strip()
    if not t:
        return []
    if t[0] in "[{":
        try:
            doc = json.loads(t)
        except ValueError as e:
            raise InsiderError("not valid JSON: %s" % e)
        if isinstance(doc, dict):
            doc = doc.get("data") or doc.get("rows") or []
        return [r for r in doc if isinstance(r, dict)]
    return [r for r in csv.DictReader(io.StringIO(t)) if any((v or "").strip() for v in r.values() if isinstance(v, str))]


# ---------------------------------------------------------------- PIT rows

def is_insider(cat):
    c = (cat or "").lower()
    return any(w in c for w in INSIDERS)


def is_market(mode):
    m = (mode or "").lower()
    return any(w in m for w in MARKET_MODES)


def side_of(txn, mode):
    t = ("%s %s" % (txn or "", mode or "")).lower()
    if "pledge" in t or "invocation" in t or "revoke" in t or "release" in t:
        return "pledge"
    if re.search(r"\bsell|\bsale|\bdispos", t):
        return "sell"
    if re.search(r"\bbuy|\bpurchase|\bacqui|\ballot", t):
        return "buy"
    return "other"


def pit_row(r):
    f = {k: pick(r, names) for k, names in PIT_FIELDS.items()}
    sym = plain_symbol(f["symbol"])
    if not sym:
        return None
    sh, val = num(f["shares"]), num(f["value"])
    side = side_of(f["txn"], f["mode"])
    out = {"type": "pit", "symbol": sym, "company": (f["company"] or "").strip(),
           "person": " ".join(str(f["person"] or "").split()), "category": (f["category"] or "").strip(),
           "side": side, "shares": sh, "value": val, "price": round(val / sh, 2) if sh and val else None,
           "mode": (f["mode"] or "").strip(), "security": (f["security"] or "Equity").strip(),
           "before_pct": num(f["before_pct"]), "after_pct": num(f["after_pct"]),
           "from": str(parse_day(f["from"]) or f["from"] or ""), "to": str(parse_day(f["to"]) or f["to"] or ""),
           "disclosed": str(parse_day(f["disclosed"]) or f["disclosed"] or "")}
    out["insider"] = is_insider(out["category"])
    out["market"] = is_market(out["mode"])
    out["signal"] = out["insider"] and out["market"] and side in ("buy", "sell")
    out["id"] = hashlib.sha1(("pit|%s|%s|%s|%s|%s|%s" % (sym, out["person"], side, sh, out["from"], out["to"]))
                             .encode("utf-8")).hexdigest()[:16]
    return out


def deal_row(r, kind):
    f = {k: pick(r, names) for k, names in DEAL_FIELDS.items()}
    sym = plain_symbol(f["symbol"])
    if not sym:
        return None
    sh, px = num(f["shares"]), num(f["price"])
    side = "buy" if str(f["side"] or "").strip().upper().startswith("B") else "sell"
    out = {"type": kind, "symbol": sym, "company": (f["company"] or "").strip(),
           "client": " ".join(str(f["client"] or "").split()), "side": side, "shares": sh, "price": px,
           "value": round(sh * px, 2) if sh and px else None, "date": str(parse_day(f["date"]) or f["date"] or "")}
    out["id"] = hashlib.sha1(("%s|%s|%s|%s|%s|%s" % (kind, sym, out["client"], side, sh, out["date"]))
                             .encode("utf-8")).hexdigest()[:16]
    return out


# ---------------------------------------------------------------- your tickers and memory

def _load(path):
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except ValueError:
            return None
    return None


def my_tickers(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json", plan="trader/plan.json"):
    out = set()
    for p in (_load(portfolio) or {}).get("positions", []) or []:
        out.add(plain_symbol(p.get("ticker") or p.get("symbol") or p.get("tradingsymbol")))
    for h in (_load(watchlist) or {}).get("hits", []) or []:
        out.add(plain_symbol(h.get("symbol")))
    pl = _load(plan) or {}
    if pl.get("symbol"):
        out.add(plain_symbol(pl["symbol"]))
    out.discard("")
    return out


def mark_new(rows, state, today):
    seen = state.setdefault("seen", {})
    for r in rows:
        r["new"] = r["id"] not in seen
    return rows


def remember(rows, state, today):
    seen = state.setdefault("seen", {})
    for r in rows:
        seen.setdefault(r["id"], today.isoformat())
    cut = (today - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cut}
    state["last_check"] = today.isoformat()


def save_state(path, state):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- words

def pit_line(r, mine):
    who = r["person"] or "someone"
    what = {"buy": "BOUGHT", "sell": "SOLD", "pledge": "PLEDGE", "other": "CHANGED"}[r["side"]]
    hold = ""
    if r["before_pct"] is not None and r["after_pct"] is not None:
        hold = ", holding %.2f%% -> %.2f%%" % (r["before_pct"], r["after_pct"])
    px = (" at about ₹%s" % "{:,.2f}".format(r["price"])) if r["price"] else ""
    when = r["from"] if r["from"] == r["to"] or not r["to"] else "%s to %s" % (r["from"], r["to"])
    flags = []
    if r["new"]:
        flags.append("NEW")
    if r["symbol"] in mine:
        flags.append("YOURS")
    if not r["market"]:
        flags.append("not open-market")
    return "%-12s %s (%s) %s %s shares%s, %s%s; traded %s, mode %s, disclosed %s%s" % (
        r["symbol"], who, r["category"] or "insider", what, shares(r["shares"]), px, inr(r["value"]),
        hold, when or "?", r["mode"] or "?", r["disclosed"] or "?", ("  [" + ", ".join(flags) + "]") if flags else "")


def deal_line(r, mine):
    flags = [x for x, ok in (("NEW", r["new"]), ("YOURS", r["symbol"] in mine)) if ok]
    return "%-12s %-5s %s %s %s shares at ₹%s = %s on %s%s" % (
        r["symbol"], r["type"].upper(), r["client"] or "?", "BOUGHT" if r["side"] == "buy" else "SOLD",
        shares(r["shares"]), "{:,.2f}".format(r["price"] or 0), inr(r["value"]), r["date"] or "?",
        ("  [" + ", ".join(flags) + "]") if flags else "")


# ---------------------------------------------------------------- commands

def cmd_pit(a):
    now = datetime.datetime.fromisoformat(a.now) if a.now else None
    today = now.date() if now else today_ist()
    start = today - datetime.timedelta(days=a.days)
    f = Fetcher(a.offline)
    if a.file:
        with open(a.file, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        src = a.file
    else:
        url = PIT_URL % (start.strftime("%d-%m-%Y"), today.strftime("%d-%m-%Y"))
        if a.symbol:
            url += "&symbol=" + urllib.parse.quote(plain_symbol(a.symbol))
        text = f.get(url, "pit.json", referer=PIT_PAGE)
        src = "NSE insider trading (PIT) %s to %s" % (start.isoformat(), today.isoformat())
    rows = [x for x in (pit_row(r) for r in rows_from_text(text)) if x]
    total = len(rows)
    if a.symbol:
        rows = [r for r in rows if r["symbol"] == plain_symbol(a.symbol)]
    rows = [r for r in rows if not r["disclosed"] or not parse_day(r["disclosed"]) or
            start <= parse_day(r["disclosed"]) <= today + datetime.timedelta(days=1)]
    if not a.all_people:
        rows = [r for r in rows if r["insider"]]
    if a.buys:
        rows = [r for r in rows if r["side"] == "buy"]
    if a.sells:
        rows = [r for r in rows if r["side"] == "sell"]
    if a.market_only:
        rows = [r for r in rows if r["market"]]
    if a.min_value:
        rows = [r for r in rows if (r["value"] or 0) >= a.min_value]
    mine = my_tickers(a.portfolio, a.watchlist, a.plan)
    if a.mine:
        rows = [r for r in rows if r["symbol"] in mine]
    state = _load(a.state) or {}
    mark_new(rows, state, today)
    rows.sort(key=lambda r: (not r["signal"], r["symbol"] not in mine, -(r["value"] or 0)))
    if a.json:
        print(json.dumps({"source": src, "read": total, "rows": rows}, indent=2, ensure_ascii=False))
    else:
        print("Insider trades: %s. %d row%s read, %d shown%s." % (
            src, total, "" if total == 1 else "s", len(rows),
            " (promoters, directors, KMP and designated persons)" if not a.all_people else ""))
        sig = [r for r in rows if r["signal"]]
        if sig:
            buys = sum(1 for r in sig if r["side"] == "buy")
            print("Open-market trades by insiders: %d buy%s, %d sell%s." % (
                buys, "" if buys == 1 else "s", len(sig) - buys, "" if len(sig) - buys == 1 else "s"))
        for r in rows[:a.show]:
            print("  " + pit_line(r, mine))
        if len(rows) > a.show:
            print("  ... and %d more (--show N, or --json)." % (len(rows) - a.show))
        if not rows:
            print("  Nothing matched.")
        print("A disclosure is after the fact (up to two trading days late), not a tip: insiders sell for "
              "many reasons, and the price may already have moved. Not financial advice.")
    if not a.dry_run:
        remember(rows, state, today)
        save_state(a.state, state)
    return 0 if rows else 1


def cmd_deals(a):
    now = datetime.datetime.fromisoformat(a.now) if a.now else None
    today = now.date() if now else today_ist()
    kinds = ["bulk"] if a.bulk and not a.block else ["block"] if a.block and not a.bulk else ["bulk", "block"]
    f = Fetcher(a.offline)
    rows, errors = [], []
    for kind in kinds:
        try:
            if a.file:
                with open(a.file, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            else:
                text = f.get(DEALS_URL % kind, "%s.csv" % kind, nse=False, referer="https://www.nseindia.com/")
            rows += [x for x in (deal_row(r, kind) for r in rows_from_text(text)) if x]
        except InsiderError as e:
            errors.append("%s deals: %s" % (kind, e))
        if a.file:
            break
    total = len(rows)
    if a.symbol:
        rows = [r for r in rows if r["symbol"] == plain_symbol(a.symbol)]
    if a.min_value:
        rows = [r for r in rows if (r["value"] or 0) >= a.min_value]
    mine = my_tickers(a.portfolio, a.watchlist, a.plan)
    if a.mine:
        rows = [r for r in rows if r["symbol"] in mine]
    state = _load(a.state) or {}
    mark_new(rows, state, today)
    rows.sort(key=lambda r: (r["symbol"] not in mine, -(r["value"] or 0)))
    if a.json:
        print(json.dumps({"read": total, "rows": rows, "errors": errors}, indent=2, ensure_ascii=False))
    else:
        print("NSE %s deals: %d read, %d shown." % (" and ".join(kinds), total, len(rows)))
        for r in rows[:a.show]:
            print("  " + deal_line(r, mine))
        if len(rows) > a.show:
            print("  ... and %d more (--show N, or --json)." % (len(rows) - a.show))
        if not rows and not errors:
            print("  Nothing matched.")
        for e in errors:
            print("note: " + e)
        print("A bulk deal is one client trading over 0.5% of a company's shares in a day; a block deal is a "
              "single trade of Rs 10 crore or more in the block window. Both are reported after the fact. "
              "Not financial advice.")
    if not a.dry_run:
        remember(rows, state, today)
        save_state(a.state, state)
    return 0 if rows else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Insider Tracker (trader-insider): SEBI insider-trading disclosures "
                                             "and NSE bulk / block deals.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--symbol", help="one NSE symbol")
        p.add_argument("--mine", action="store_true", help="only your portfolio, watchlist and plan tickers")
        p.add_argument("--min-value", type=float, help="only rows worth at least this many rupees")
        p.add_argument("--file", help="a CSV or JSON you downloaded from nseindia.com")
        p.add_argument("--offline", metavar="DIR", help="read pit.json / bulk.csv / block.csv from DIR")
        p.add_argument("--now", help="pretend today is this date (YYYY-MM-DD)")
        p.add_argument("--show", type=int, default=40)
        p.add_argument("--state", default="trader/state/insider.json")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--watchlist", default="trader/watchlist.json")
        p.add_argument("--plan", default="trader/plan.json")
        p.add_argument("--dry-run", action="store_true", help="do not remember what was shown")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("pit", help="insider trades disclosed under SEBI's PIT rules")
    common(p)
    p.add_argument("--days", type=int, default=7, help="look back this many days (default 7)")
    p.add_argument("--buys", action="store_true")
    p.add_argument("--sells", action="store_true")
    p.add_argument("--market-only", action="store_true", help="open-market trades only (no ESOP, gift, pledge)")
    p.add_argument("--all-people", action="store_true", help="every disclosure, not just insiders")
    p = sub.add_parser("deals", help="today's NSE bulk and block deals")
    common(p)
    p.add_argument("--bulk", action="store_true")
    p.add_argument("--block", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"pit": cmd_pit, "deals": cmd_deals}[a.cmd](a)
    except InsiderError as e:
        print("insider.py: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("insider.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
