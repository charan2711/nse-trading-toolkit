"""NSE data: the equity list, the opening-range scan on Kite candles, news
with NSE announcements, insider trades and bulk / block deals."""

import os
import shutil
import tempfile
import unittest

from _load import INDIA, load, run

scan = load("trader-scanner", "scan")
news = load("trader-news", "news")
insider = load("trader-insider", "insider")


class Scanner(unittest.TestCase):
    def test_universe_keeps_series_eq(self):
        code, out, _ = run(scan, ["universe", "--offline", os.path.join(INDIA, "EQUITY_L.csv"), "--no-write"])
        self.assertEqual(code, 0, out)
        self.assertIn("Kept 7 NSE stocks (series EQ)", out)
        self.assertIn("1 series BE", out)

    def test_nse_csv_headers_with_spaces(self):
        rows = scan.parse_nse_csv("SYMBOL,NAME OF COMPANY, SERIES\nRELIANCE,Reliance Industries Limited,EQ\n")
        self.assertEqual(rows[0]["SERIES"], "EQ")

    def test_orb_scan_on_kite_candles(self):
        d = tempfile.mkdtemp()
        try:
            shutil.copy(os.path.join(INDIA, "RELIANCE-2026-11-06-5m.json"), os.path.join(d, "RELIANCE.json"))
            code, out, _ = run(scan, ["orb", "--symbols", "RELIANCE", "--offline", d,
                                      "--now", "2026-11-06 10:15", "--no-write"])
            self.assertEqual(code, 0, out)
            self.assertIn("9:50 AM  RELIANCE", out)
            self.assertIn("range 1,295.00 to 1,306.40", out)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_orb_scan_before_the_range_is_done(self):
        d = tempfile.mkdtemp()
        try:
            shutil.copy(os.path.join(INDIA, "RELIANCE-2026-11-06-5m.json"), os.path.join(d, "RELIANCE.json"))
            code, out, err = run(scan, ["orb", "--symbols", "RELIANCE", "--offline", d,
                                        "--now", "2026-11-06 09:25", "--no-write"])
            self.assertNotEqual(code, 0)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class News(unittest.TestCase):
    def test_headlines_and_nse_filings_in_ist(self):
        d = tempfile.mkdtemp()
        try:
            code, out, _ = run(news, ["--tickers", "RELIANCE", "--offline", os.path.join(INDIA, "news"),
                                      "--now", "2026-11-06T09:00:00+05:30", "--dry-run"], d)
            self.assertEqual(code, 0, out)
            self.assertIn("NSE filing", out)
            self.assertIn("IST", out)
            self.assertIn("vs yesterday's close ₹1,313.40", out)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_yahoo_symbol_and_region(self):
        self.assertEqual(news.yahoo_symbol("RELIANCE"), "RELIANCE.NS")

    def test_announcement_shape(self):
        import json
        items = news.parse_nse_announcements(json.dumps(
            [{"symbol": "RELIANCE", "desc": "Outcome of Board Meeting", "attchmntText": "Financial results",
              "an_dt": "06-Nov-2026 18:02:11", "attchmntFile": "https://nsearchives.nseindia.com/x.pdf"}]),
            "RELIANCE")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["items"], ["earnings"])
        self.assertTrue(items[0]["important"])
        self.assertEqual(items[0]["time"].strftime("%H:%M"), "12:32")   # 18:02 IST is 12:32 UTC


class Insider(unittest.TestCase):
    def test_market_buys_by_insiders(self):
        d = tempfile.mkdtemp()
        try:
            code, out, _ = run(insider, ["pit", "--offline", os.path.join(INDIA, "insider"), "--now", "2026-11-06",
                                         "--dry-run", "--market-only", "--buys"], d)
            self.assertEqual(code, 0, out)
            self.assertIn("BOUGHT 5,000 shares", out)
            self.assertIn("₹65.50 lakh", out)
            self.assertNotIn("SOLD", out)
            self.assertIn("Not financial advice", out)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_bulk_and_block_deals(self):
        d = tempfile.mkdtemp()
        try:
            code, out, _ = run(insider, ["deals", "--offline", os.path.join(INDIA, "insider"), "--now", "2026-11-06",
                                         "--dry-run"], d)
            self.assertEqual(code, 0, out)
            self.assertIn("BLOCK", out)
            self.assertIn("BULK", out)
            self.assertIn("70,00,000 shares", out)
            self.assertIn("crore", out)
            code, out, _ = run(insider, ["deals", "--block", "--offline", os.path.join(INDIA, "insider"),
                                         "--now", "2026-11-06", "--dry-run"], d)
            self.assertNotIn("BULK ", out)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_seen_rows_are_not_new_twice(self):
        d = tempfile.mkdtemp()
        try:
            args = ["deals", "--offline", os.path.join(INDIA, "insider"), "--now", "2026-11-06"]
            run(insider, args, d)
            code, out, _ = run(insider, args, d)
            self.assertNotIn("[NEW", out)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
