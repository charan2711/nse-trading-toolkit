"""The whole Indian trade flow on RELIANCE, Friday 2026-11-06, on Kite's real
5-minute candles: entry -> exit -> budget -> risk -> broker (paper) -> flatten."""

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
ticket = load("trader-broker", "ticket")

CANDLES = os.path.join(INDIA, "RELIANCE-2026-11-06-5m.json")
NOW = "2026-11-06 09:51"


def read(cwd, name):
    with open(os.path.join(cwd, "trader", name), encoding="utf-8") as fh:
        return json.load(fh)


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.d, "trader"))
        shutil.copy(os.path.join(ROOT, "trader-budget", "config.example.json"),
                    os.path.join(cls.d, "trader", "config.json"))
        cls.entry = run(entry, ["check", "RELIANCE", "--offline", CANDLES, "--now", NOW, "--write"], cls.d)
        cls.exit = run(exitp, ["plan", "--target", "1R", "--now", NOW, "--write"], cls.d)
        cls.budget = run(size, ["--now", NOW, "--write"], cls.d)
        cls.risk = run(check, ["--now", NOW], cls.d)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def test_entry_triggers_at_9_50_on_kite_bars(self):
        code, out, _ = self.entry
        self.assertEqual(code, 0, out)
        self.assertIn("TRIGGERED 9:50 AM at ₹1311.90", out)
        self.assertIn("(Kite)", out)
        e = read(self.d, "entry.json")
        self.assertEqual(e["symbol"], "RELIANCE")
        self.assertAlmostEqual(e["or_high"] if "or_high" in e else e["opening_range"]["high"], 1306.40)

    def test_exit_plan_midpoint_stop_and_1r_target(self):
        code, out, _ = self.exit
        self.assertEqual(code, 0, out)
        p = read(self.d, "plan.json")
        self.assertAlmostEqual(p["stop"], 1300.70)
        self.assertAlmostEqual(p["target"], 1323.10)
        self.assertAlmostEqual(p["risk_per_share"], 11.20)
        self.assertEqual(p["flat_by"], "15:05")
        self.assertIn("15:05 IST", out)

    def test_budget_sizes_39_shares_with_charges(self):
        code, out, _ = self.budget
        self.assertEqual(code, 0, out)
        self.assertIn("39 shares of RELIANCE = ₹51,164.10", out)
        self.assertIn("₹491.57", out)
        o = read(self.d, "orders.json")["orders"][0]
        self.assertEqual((o["variety"], o["product"], o["transaction_type"], o["quantity"]), ("co", "MIS", "BUY", 39))
        self.assertAlmostEqual(o["trigger_price"], 1300.7)
        self.assertAlmostEqual(o["plan"]["charges"], 54.77)
        self.assertAlmostEqual(o["plan"]["gain_at_target"], 381.70)

    def test_risk_passes_the_order(self):
        code, out, _ = self.risk
        self.assertEqual(code, 0, out)
        self.assertIn("PASS  #1 BUY RELIANCE 39 shares", out)
        self.assertIn("max loss ₹491.57 with ₹54.77 of charges (limit ₹500)", out)

    def test_paper_ticket_yes_fill_then_flatten_exits_the_cover_order(self):
        d = tempfile.mkdtemp()
        try:
            shutil.copytree(os.path.join(self.d, "trader"), os.path.join(d, "trader"))
            code, out, _ = run(ticket, ["show", "--now", NOW], d)
            self.assertIn("PAPER (nothing goes to Kite)", out)
            self.assertIn('"variety": "co"', out)
            self.assertNotIn("target_price", out.split("The Kite call it would make:")[1].split("}")[0])
            code, out, _ = run(ticket, ["approve", "--index", "1", "--reply", "haan", "--now", NOW], d)
            self.assertEqual(code, 0, out)
            code, out, _ = run(ticket, ["result", "--index", "1", "--status", "placed", "--paper",
                                        "--filled-qty", "39", "--filled-price", "1312.00", "--now", NOW], d)
            self.assertEqual(code, 0, out)
            code, out, _ = run(ticket, ["flatten", "--symbol", "RELIANCE", "--now", "2026-11-06 15:06"], d)
            o = read(d, "orders.json")["orders"][0]
            self.assertEqual((o["transaction_type"], o["quantity"], o["variety"]), ("SELL", 39, "co"))
            self.assertIn("exit_cover", o)
            code, out, _ = run(check, ["--now", "2026-11-06 15:06"], d)
            self.assertIn("PASS  #1 SELL RELIANCE 39 shares", out)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_vague_reply_is_not_a_yes(self):
        d = tempfile.mkdtemp()
        try:
            shutil.copytree(os.path.join(self.d, "trader"), os.path.join(d, "trader"))
            code, out, err = run(ticket, ["approve", "--index", "1", "--reply", "maybe later", "--now", NOW], d)
            self.assertNotEqual(code, 0)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_fingerprints_agree_between_risk_and_broker(self):
        o = read(self.d, "orders.json")["orders"][0]
        self.assertEqual(check.fingerprint(o), ticket.fingerprint(o))


if __name__ == "__main__":
    unittest.main()
