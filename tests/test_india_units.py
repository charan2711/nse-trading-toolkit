"""The India-specific building blocks: IST and the NSE calendar, ticks,
Zerodha charges, symbols, Kite candles and quotes, refusals."""

import datetime as dt
import gzip
import json
import os
import shutil
import tempfile
import unittest

from _load import INDIA, ROOT, load, run

entry = load("trader-entry", "entry")
exitp = load("trader-exit", "exit")
size = load("trader-budget", "size")
check = load("trader-risk", "check")
alerts = load("trader-alerts", "alerts")
tm = load("trader-timemachine", "timemachine")

CANDLES = os.path.join(INDIA, "RELIANCE-2026-11-06-5m.json")
QUOTES = os.path.join(INDIA, "kite-quotes-2026-11-06.json")


class Clock(unittest.TestCase):
    def test_ist_is_utc_plus_5_30_with_no_dst(self):
        for month in (1, 7):
            utc = dt.datetime(2026, month, 6, 3, 45)
            self.assertEqual(alerts.to_ist(utc).strftime("%H:%M"), "09:15")

    def test_nse_holidays_weekends_and_muhurat(self):
        self.assertEqual(alerts.trading_day(dt.date(2026, 11, 6)), (True, ""))
        self.assertFalse(alerts.trading_day(dt.date(2026, 11, 7))[0])          # Saturday
        ok, why = alerts.trading_day(dt.date(2026, 11, 10))                     # Diwali Balipratipada
        self.assertFalse(ok)
        self.assertIn("NSE holiday", why)
        self.assertTrue(alerts.trading_day(dt.date(2026, 11, 8))[0])            # Muhurat, a Sunday
        self.assertEqual(alerts.session(dt.date(2026, 11, 8)), (dt.time(14, 20), dt.time(15, 30)))
        self.assertEqual(alerts.session(dt.date(2026, 11, 6)), (dt.time(9, 15), dt.time(15, 30)))

    def test_calendars_agree_across_tools(self):
        for mod in (entry, exitp, check, alerts):
            self.assertEqual(mod.NSE_HOLIDAYS, alerts.NSE_HOLIDAYS, mod.__name__)

    def test_clock_command(self):
        code, out, _ = run(alerts, ["clock", "--now", "2026-11-06 10:53"])
        self.assertIn("NSE OPEN", out)
        code, out, _ = run(alerts, ["clock", "--now", "2026-11-10 10:53"])
        self.assertIn("CLOSED", out.upper())


class Ticks(unittest.TestCase):
    def test_nse_tick_bands(self):
        for price, tick in ((99, "0.01"), (500, "0.05"), (1311.9, "0.10"), (7000, "0.50"),
                            (15000, "1"), (30000, "5")):
            self.assertEqual(float(exitp.tick_for(price)), float(tick), price)
            self.assertEqual(float(check.tick_for(price)), float(tick), price)

    def test_stop_rounds_down_to_the_tick(self):
        self.assertEqual(str(exitp.floor_cents("1300.76", "0.10")), "1300.70")
        self.assertEqual(str(exitp.cents("1323.15", "0.10")), "1323.20")


class Charges(unittest.TestCase):
    def test_zerodha_intraday_round_trip(self):
        c = size.charges(39, 1311.90, 1300.70)
        self.assertEqual(c["brokerage"], 30.57)       # 0.03% of each side, under the ₹20 cap
        self.assertEqual(c["stt"], 13)                 # 0.025% of the sell, rounded to the rupee
        self.assertEqual(c["stamp"], 2)                # 0.003% of the buy, rounded
        self.assertEqual(c["total"], 54.77)

    def test_brokerage_caps_at_20_an_order(self):
        c = size.charges(1000, 1000, 1000)
        self.assertEqual(c["brokerage"], 40.0)

    def test_budget_and_risk_charges_match(self):
        for qty, b, s in ((39, 1311.90, 1300.70), (7, 245.35, 240.10), (500, 88.05, 87.0)):
            self.assertEqual(size.charges(qty, b, s)["total"], check.charges(qty, b, s))

    def test_bse_transaction_charge_is_higher(self):
        self.assertGreater(size.charges(39, 1311.9, 1300.7, "BSE")["transaction"],
                           size.charges(39, 1311.9, 1300.7, "NSE")["transaction"])

    def test_indian_grouping(self):
        self.assertEqual(size.money(5116410 / 100.0), "₹51,164.10")
        self.assertEqual(size.money(10000000), "₹1,00,00,000")


class Symbols(unittest.TestCase):
    def test_plain_and_yahoo(self):
        self.assertEqual(entry.plain_symbol("NSE:RELIANCE"), "RELIANCE")
        self.assertEqual(entry.plain_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(entry.yahoo_symbol("RELIANCE"), "RELIANCE.NS")
        self.assertEqual(entry.yahoo_symbol("BSE:RELIANCE"), "RELIANCE.BO")


class Kite(unittest.TestCase):
    def test_candles_in_every_shape(self):
        with open(CANDLES) as fh:
            doc = json.load(fh)
        got = entry.kite_candles(doc)
        self.assertIsNotNone(got)
        candles = got[0]
        self.assertEqual(len(candles), 72)    # an F&O stock: continuous trading ends at 15:15
        rest = {"data": {"candles": [list(c) for c in candles]}}
        self.assertEqual(len(entry.kite_candles(rest)[0]), 72)

    def test_quotes_carry_circuits(self):
        with open(QUOTES) as fh:
            quotes, errors = alerts.parse_kite_quotes(json.load(fh))
        q = quotes["RELIANCE"]
        self.assertEqual(q["price"], 1320)
        self.assertEqual(q["prev_close"], 1313.4)
        self.assertEqual((q.get("lower_circuit"), q.get("upper_circuit")), (1188, 1452))

    def test_timemachine_session_length_follows_the_stock(self):
        with gzip.open(os.path.join(INDIA, "RELIANCE-60d5m.json.gz"), "rt") as fh:
            doc = json.load(fh)
        code, out, _ = run(tm, ["run", "--symbol", "RELIANCE", "--date", "2026-11-06", "--entry", "1311.90",
                                "--stop", "1300.70", "--target", "1323.10", "--entry-time", "9:50 AM",
                                "--offline", os.path.join(INDIA, "RELIANCE-60d5m.json.gz"),
                                "--now", "2026-11-06 09:51", "--replays", "2000"])
        self.assertEqual(code, 0, out)
        self.assertIn("to 3:15 PM", out)
        self.assertIn("seed 19", out)
        self.assertTrue(doc)


class Refusals(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "trader"))
        shutil.copy(os.path.join(ROOT, "trader-budget", "config.example.json"),
                    os.path.join(self.d, "trader", "config.json"))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_stop_too_far_for_the_max_loss_is_refused(self):
        run(exitp, ["plan", "--symbol", "RELIANCE", "--entry", "1311.90", "--stop", "price:600",
                    "--target", "1R", "--now", "2026-11-06 09:51", "--write"], self.d)
        code, out, err = run(size, ["--now", "2026-11-06 09:51", "--write"], self.d)
        self.assertNotEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.d, "trader", "orders.json")))

    def test_entry_after_flat_by_is_refused(self):
        code, out, err = run(exitp, ["plan", "--symbol", "RELIANCE", "--entry", "1311.90", "--or-high", "1306.4",
                                     "--or-low", "1295", "--time", "3:10 PM", "--date", "2026-11-06",
                                     "--now", "2026-11-06 15:10"], self.d)
        self.assertNotEqual(code, 0)

    def test_entry_not_before_9_30(self):
        code, out, _ = run(entry, ["check", "RELIANCE", "--offline", CANDLES, "--now", "2026-11-06 09:20"])
        self.assertNotIn("TRIGGERED", out)


if __name__ == "__main__":
    unittest.main()
