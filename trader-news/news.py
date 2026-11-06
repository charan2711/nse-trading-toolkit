#!/usr/bin/env python3
"""News monitor for trader-news. Standard library only.

Watches headlines and NSE corporate announcements (the exchange filings:
results, board meetings, deals, management changes) for the stocks you watch
or hold and prints only what is new since the last check, newest first, with
how far each stock has moved today and a flag on anything that touches your
tickers.

    python3 news.py                                  # watchlist + plan + portfolio tickers
    python3 news.py --tickers RELIANCE TCS INFY
    python3 news.py --tickers RELIANCE --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-nse                         # headlines only
    python3 news.py --no-moves                       # skip the price line per ticker

Your tickers, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json.

Sources, one request each per ticker:
  - Yahoo Finance headline RSS for the NSE listing:
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=RELIANCE.NS&region=IN&lang=en-IN
  - NSE corporate announcements, the filings every listed company must make
    (SEBI's LODR rules): financial results, outcomes of board meetings,
    acquisitions, changes in directors or auditors, credit ratings, pledges.
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=RELIANCE
    NSE's site wants a browser: the tool opens www.nseindia.com once for the
    session cookies, then asks for each symbol. NSE blocks some networks and
    changes this API without notice; when it refuses, the tool says so and
    headlines still run. A BSE listing (BSE:X) has no NSE feed: check
    bseindia.com's corporate announcements for it.
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m

Times print in IST. State: trader/state/news.json remembers what you have seen
(30 days), so an item shows once. Offline: --offline DIR reads
yahoo-<TICKER>.xml, chart-<TICKER>.json and nse-<TICKER>.json from DIR. The
tests use it.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
NSE_HOME = "https://www.nseindia.com/"
NSE_ANN = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
NSE_PAGE = "https://www.nseindia.com/get-quotes/equity?symbol=%s"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30), "IST")

# NSE announcement subjects (the "desc" field) that move a stock, and the words
# inside any subject or text that do.
IMPORTANT_SUBJECTS = [
    ("earnings", r"financial result|outcome of board meeting|results?\b"),
    ("deal", r"acquisition|amalgamation|merger|scheme of arrangement|demerger|sale of (?:assets|undertaking)|"
             r"open offer|takeover|joint venture"),
    ("people", r"resignation|cessation|appointment of (?:managing director|ceo|cfo|chief)|change in (?:directors?|"
               r"management|auditors?|key managerial)"),
    ("risk", r"default|insolvency|nclt|fraud|forensic|qualified opinion|credit rating|rating (?:down|revis)|"
             r"pledge|invocation|search|raid|show cause|penalty"),
    ("capital", r"dividend|buy ?back|bonus|split|sub-division|rights issue|preferential|qip|"
                r"fund raising|record date"),
]
KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults\b|\bEPS\b|\bguidance\b|\bprofit\b|\brevenue\b|\bQ[1-4]\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|target price|\boutperform\b|\bunderperform\b|"
                r"\bbuy rating\b|\bsell rating\b"),
    ("legal", r"\blawsuit\b|\bprobe\b|\binvestigation\b|\bcourt\b|\bNCLT\b|\bED\b|\bCBI\b|\braid\b|"
              r"\bshow[- ]cause\b|\bpenalt"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bdeal\b|\bstake\b|\bdemerger\b"),
    ("capital", r"\bdividend\b|\bbuy ?back\b|\bbonus\b|\bstock split\b|\bQIP\b|\bOFS\b|\bblock deal\b|"
                r"\bbulk deal\b|\bpledge\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bMD\b|\bresign|\bsteps down\b|\bappoint"),
    ("risk", r"\bdefault\b|\binsolvency\b|\bdelist|\bhack|\bbreach\b|\boutage\b|\bfraud\b|\bdowngrade"),
    ("halt", r"\bcircuit\b|\bsuspen(?:d|sion)\b|\bhalt(?:s|ed)?\b|\bASM\b|\bGSM\b"),
    ("regulatory", r"\bSEBI\b|\bCCI\b|\bUSFDA\b|\bFDA\b|\bregulatory approval\b|\blicen[cs]e\b|\bTRAI\b|"
                   r"\bRBI (?:nod|approval|action|penalty)\b"),
    ("macro", r"\bRBI\b.*\b(?:rate|policy)\b|\brepo\b|\bMPC\b|\bCPI\b|\binflation\b|\bGDP\b|\bbudget\b|"
              r"\bFIIs?\b|\bFPIs?\b|\bcrude\b|\brupee\b|\bSensex\b|\bNifty\b"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Aware or naive UTC -> naive IST wall-clock time (UTC+5:30, no daylight saving)."""
    if utc.tzinfo is not None:
        utc = utc.astimezone(_dt.timezone.utc)
    return utc.replace(tzinfo=None) + _dt.timedelta(hours=5, minutes=30)


def plain_symbol(sym):
    s = str(sym or "").strip().upper()
    if s.startswith("NSE:"):
        s = s[4:]
    if s.endswith(".NS"):
        s = s[:-3]
    if s.endswith(".BO"):
        s = "BSE:" + s[:-3]
    return s


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{ticker: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[plain_symbol(sym)] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(plain_symbol(pl["symbol"]), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol") or p.get("tradingsymbol")
        if sym:
            out[plain_symbol(sym)] = p.get("name") or out.get(plain_symbol(sym))
    return out


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    """Polite GETs. NSE's API needs the cookies its home page sets, so NSE calls
    go through one cookie-keeping opener that visits www.nseindia.com first."""

    def __init__(self, offline=None, pause=0.35):
        self.offline = offline
        self.pause = pause
        self._last = 0.0
        self._jar = http.cookiejar.CookieJar()
        self._nse = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._nse_ready = False

    def get(self, url, name, nse=False, referer=None):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        headers = {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip", "Accept-Language": "en-IN,en;q=0.9",
                   "Accept": "application/json, text/plain, */*" if nse else "*/*"}
        if referer:
            headers["Referer"] = referer
        if nse and not self._nse_ready:
            try:
                home = urllib.request.Request(NSE_HOME, headers=dict(headers, Accept="text/html,*/*"))
                with self._nse.open(home, timeout=30) as resp:
                    resp.read()
                self._nse_ready = True
            except (urllib.error.URLError, OSError) as exc:
                raise NewsError("NSE's home page did not answer (%s): it blocks some networks"
                                % getattr(exc, "reason", exc))
        req = urllib.request.Request(url, headers=headers)
        opener = self._nse.open if nse else urllib.request.urlopen
        try:
            with opener(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))


def yahoo_symbol(ticker):
    """RELIANCE -> RELIANCE.NS, BSE:500325 -> 500325.BO, URL-escaped (M&M.NS -> M%26M.NS)."""
    s = plain_symbol(ticker)
    y = s if s.startswith("^") else (s[4:] + ".BO" if s.startswith("BSE:") else s + ".NS")
    return urllib.parse.quote(y, safe=".-^")


def _file_name(t):
    return plain_symbol(t).replace(":", "-")


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS), ISO 8601, or NSE's "06-Nov-2026 18:30:12" (IST) -> aware UTC
    datetime, or None. An ISO time without an offset is IST; so is NSE's."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%d%m%Y%H%M%S"):
        try:
            return _dt.datetime.strptime(value, fmt).replace(tzinfo=IST).astimezone(_dt.timezone.utc)
        except ValueError:
            pass
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=IST)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def _pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def parse_nse_announcements(data, ticker):
    """NSE corporate-announcements JSON -> items. The API answers a list of
    {symbol, sm_name, desc (the subject), attchmntText, attchmntFile (the PDF),
    an_dt or sort_date (IST), seq_id}; older and newer shapes use a few other
    names, which are tried too. {"data": [...]} works as well."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError as exc:
        raise NewsError("NSE announcements for %s are not JSON: %s" % (ticker, exc))
    if isinstance(doc, dict):
        doc = doc.get("data") or doc.get("announcements") or []
    if not isinstance(doc, list):
        raise NewsError("NSE announcements for %s are not a list" % ticker)
    out = []
    for a in doc:
        if not isinstance(a, dict):
            continue
        subject = _pick(a, "desc", "subject", "smIndustry")
        text = " ".join(_pick(a, "attchmntText", "sm_desc", "details").split())
        company = _pick(a, "sm_name", "companyName", "company") or ticker
        when = parse_time(_pick(a, "an_dt", "sort_date", "exchdisstime", "dt", "broadcastDateTime"))
        link = _pick(a, "attchmntFile", "attachment", "pdf") or (NSE_PAGE % urllib.parse.quote(ticker))
        sid = _pick(a, "seq_id", "seqId", "id") or hashlib.sha1(
            ("%s|%s|%s" % (subject, text[:80], when)).encode("utf-8")).hexdigest()[:12]
        hay = "%s %s" % (subject, text)
        cats = [label for label, rx in IMPORTANT_SUBJECTS if re.search(rx, hay, re.I)]
        title = "%s: %s" % (company, subject or "announcement")
        out.append({"id": "nse:%s:%s" % (ticker, sid), "source": "NSE filing", "kind": "filing",
                    "form": subject, "ticker": ticker, "title": title, "summary": text[:400],
                    "items": cats, "important": bool(cats), "link": link, "time": when})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_symbol(t)), "chart-%s.json" % _file_name(t))
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_symbol(t)), "chart-%s.json" % _file_name(t))
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        base = t.split(":", 1)[-1]
        alts = [r"\(%s\)" % re.escape(base), r"\b(?:NSE|BSE)\s*:\s*%s\b" % re.escape(base),
                r"\b%s\.(?:NS|BO)\b" % re.escape(base)]
        if name and len(name) >= 4:
            alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your tickers each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if it["kind"] == "filing":
            for cat in reversed(it.get("items", [])):
                if cat not in it["tags"]:
                    it["tags"].insert(0, cat)
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, nse=True, errors=None):
    errors = [] if errors is None else errors
    items = []
    for t in sorted(watch):
        try:
            items += parse_rss(fetcher.get(YAHOO % yahoo_symbol(t), "yahoo-%s.xml" % _file_name(t)), t)
        except NewsError as exc:
            errors.append("headlines for %s: %s" % (t, exc))
    if nse:
        for t in sorted(watch):
            if t.startswith("BSE:"):
                errors.append("%s is a BSE listing: check bseindia.com's corporate announcements for it" % t)
                continue
            try:
                data = fetcher.get(NSE_ANN % urllib.parse.quote(t), "nse-%s.json" % _file_name(t), nse=True,
                                   referer=NSE_PAGE % urllib.parse.quote(t))
                items += parse_nse_announcements(data, t)
            except NewsError as exc:
                if fetcher.offline and "no file" in str(exc):
                    continue
                errors.append("NSE announcements for %s: %s" % (t, exc))
                if "home page" in str(exc):
                    break
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-12s ₹%s %+.2f%% vs yesterday's close ₹%s%s  %s" % (
            m["ticker"], "{:,.2f}".format(m["price"]), m["pct"], "{:,.2f}".format(m["prev_close"]),
            when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-12s %-13s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % to_ist(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-nse", dest="no_nse", action="store_true",
                    help="headlines only, no NSE announcements")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.tickers:
        watch = {plain_symbol(t): None for t in args.tickers}
        known = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if not watch:
        print("news: no tickers. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, state, nse=not args.no_nse, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
