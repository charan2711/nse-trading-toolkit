"""trader-swing on constructed daily bars: indicators without look-ahead,
the setups, the near-breakout watch, sizing with delivery charges, the GTT,
the backtest engine, and the risk / broker hand-off."""

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

from _load import ROOT, load, run

swing = load("trader-swing", "swing")
check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")


def weekdays(n, start=dt.date(2024, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def bars_from(closes, vols=None, start=dt.date(2024, 1, 1)):
    days = weekdays(len(closes), start)
    out, prev = [], closes[0]
    for k, (d, c) in enumerate(zip(days, closes)):
        o = prev
        out.append({"d": d, "o": o, "h": max(o, c) * 1.004, "l": min(o, c) * 0.996, "c": c,
                    "v": (vols[k] if vols else 1e6)})
        prev = c
    return out


def uptrend_then_base():
    """260 rising days (100 -> ~191), then a 30-day base between ~186 and ~196."""
    closes = [100 * 1.0025 ** k for k in range(260)]
    top = closes[-1]
    for k in range(30):
        closes.append(top - (5 if k % 2 else 0) + (2 if k % 4 == 0 else 0))
    return closes


def with_breakout():
    closes = uptrend_then_base()
    bars = bars_from(closes)
    last = bars[-1]
    d = last["d"] + dt.timedelta(days=1 if last["d"].weekday() < 4 else 3)
    bars.append({"d": d, "o": closes[-1], "h": 200.2, "l": closes[-1] - 0.5, "c": 200.0, "v": 3e6})
    return bars


class Charges(unittest.TestCase):
    def test_delivery_round_trip(self):
        c = swing.charges(3, 5025, 4754.5)
        self.assertEqual(c["stt"], 29)            # 0.1% each side, each rounded to the rupee
        self.assertEqual(c["stamp"], 2)           # 0.015% of the buy
        self.assertEqual(c["dp"], 15.34)          # once per scrip on the sell
        self.assertEqual(c["total"], 47.44)

    def test_no_dp_without_a_sell(self):
        self.assertEqual(swing.charges(10, buy=100)["dp"], 0)

    def test_risk_check_uses_the_same_sum(self):
        self.assertEqual(check.delivery_charges(3, 5025, 4754.5), swing.charges(3, 5025, 4754.5)["total"])


class Indicators(unittest.TestCase):
    def test_weekly_rsi_matches_rsi_of_weekly_closes_at_each_week_end(self):
        closes = [100 + 10 * ((k * 7) % 13) / 13.0 + k * 0.1 for k in range(400)]
        bars = bars_from(closes)
        wr = swing.period_rsi(bars, swing.week_key)
        weekly, ends = [], []
        for i, b in enumerate(bars):
            if i + 1 == len(bars) or swing.week_key(bars[i + 1]["d"]) != swing.week_key(b["d"]):
                weekly.append(b["c"])
                ends.append(i)
        ref = swing.rsi(weekly)
        for w, i in enumerate(ends):
            if ref[w] is not None and wr[i] is not None:
                self.assertAlmostEqual(wr[i], ref[w], places=6)

    def test_no_look_ahead(self):
        bars = bars_from([100 + (k % 9) - (k % 5) + k * 0.2 for k in range(500)])
        full = swing.period_rsi(bars, swing.month_key)
        part = swing.period_rsi(bars[:300], swing.month_key)
        self.assertEqual(full[:300], part)


class Setups(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(swing.DEFAULTS)

    def test_breakout_fires_on_the_first_close_above_the_base(self):
        bars = with_breakout()
        ind = swing.indicators(bars)
        sig = swing.setup_breakout("DEMO", ind, len(bars) - 1, self.cfg)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["setup"], "breakout")
        self.assertLess(sig["stop"], sig["close"])
        self.assertTrue(0.8 <= sig["risk_pct"] <= 8)

    def test_breakout_does_not_fire_again_the_next_day(self):
        bars = with_breakout()
        d = bars[-1]["d"] + dt.timedelta(days=1 if bars[-1]["d"].weekday() < 4 else 3)
        bars.append({"d": d, "o": 200, "h": 203, "l": 199.5, "c": 202.8, "v": 3e6})
        ind = swing.indicators(bars)
        self.assertIsNone(swing.setup_breakout("DEMO", ind, len(bars) - 1, self.cfg))

    def test_thin_volume_is_not_a_breakout(self):
        bars = with_breakout()
        bars[-1]["v"] = 1.1e6
        ind = swing.indicators(bars)
        self.assertIsNone(swing.setup_breakout("DEMO", ind, len(bars) - 1, self.cfg))

    def test_illiquid_stock_is_skipped(self):
        bars = with_breakout()
        for b in bars:
            b["v"] = b["v"] / 100.0          # about Rs 0.2 crore a day
        ind = swing.indicators(bars)
        self.assertIsNone(swing.setup_breakout("DEMO", ind, len(bars) - 1, self.cfg))

    def test_near_breakout_flags_a_coiling_stock(self):
        closes = uptrend_then_base()
        vols = [1e6] * (len(closes) - 10) + [0.5e6] * 10
        closes[-3:] = [closes[-4] + 1.0, closes[-4] + 2.0, closes[-4] + 3.0]
        bars = bars_from(closes, vols)
        ind = swing.indicators(bars)
        nb = swing.near_breakout("DEMO", ind, len(bars) - 1, self.cfg)
        self.assertIsNotNone(nb)
        self.assertGreater(nb["trigger"], nb["close"])
        self.assertLess(nb["stop"], nb["close"])
        self.assertLessEqual(nb["distance_pct"], 3)

    def test_regime(self):
        up = swing.indicators(bars_from([100 * 1.001 ** k for k in range(300)]))
        down = swing.indicators(bars_from([100 * 0.999 ** k for k in range(300)]))
        d_up = up["bars"][-1]["d"]
        self.assertEqual(swing.regime_at(up, d_up)[0], 1.0)
        self.assertEqual(swing.regime_at(down, down["bars"][-1]["d"])[0], 0.0)
        self.assertEqual(swing.regime_at(None, d_up)[0], 1.0)


class Plan(unittest.TestCase):
    def test_sizing_and_gtt(self):
        cfg = dict(swing.DEFAULTS)
        sig = {"symbol": "TITAN", "setup": "breakout", "date": "2026-11-06", "close": 5000.0, "stop": 4754.5,
               "size_mult": 0.5, "why": "test"}
        o = swing.plan_trade(sig, cfg)
        p, g = o["plan"], o["gtt"]
        self.assertEqual((o["product"], o["order_type"], o["price"]), ("CNC", "LIMIT", 5025.0))
        self.assertEqual(p["qty"], 3)
        self.assertLessEqual(p["loss_at_stop"], 200000 * 0.01 * 0.5)
        self.assertEqual(p["loss_at_stop"], 858.94)
        self.assertEqual(g["trigger_type"], "two-leg")
        self.assertEqual(g["lower_trigger_value"], 4754.5)
        self.assertEqual(g["upper_trigger_value"], 5566.0)
        self.assertEqual(g["lower_quantity"], 3)
        for v in (g["lower_trigger_value"], g["lower_limit_price"], g["upper_trigger_value"]):
            self.assertAlmostEqual(round(v / 0.10) * 0.10, v, places=6)

    def test_rsi60_has_a_stop_only_gtt(self):
        sig = {"symbol": "KOTAKBANK", "setup": "rsi60", "date": "2026-11-05", "close": 467.0, "stop": 437.55}
        o = swing.plan_trade(sig, dict(swing.DEFAULTS))
        self.assertEqual(o["gtt"]["trigger_type"], "single")
        self.assertIsNone(o["plan"]["target"])

    def test_full_book_refuses(self):
        cfg = dict(swing.DEFAULTS)
        held = [{"entry": 100, "stop": 95, "qty": 10}] * 5
        sig = {"symbol": "X", "setup": "breakout", "date": "2026-11-06", "close": 100.0, "stop": 96.0}
        with self.assertRaises(swing.SwingError):
            swing.plan_trade(sig, cfg, held)


class Backtest(unittest.TestCase):
    def run_bt(self, after):
        bars = with_breakout()
        for o, h, l, c in after:
            last = bars[-1]["d"]
            d = last + dt.timedelta(days=1 if last.weekday() < 4 else 3)
            bars.append({"d": d, "o": o, "h": h, "l": l, "c": c, "v": 1e6})
        return swing.backtest({"DEMO": bars}, None, dict(swing.DEFAULTS), setups=["breakout"])

    def test_target_hit_is_about_plus_2r_after_costs(self):
        res = self.run_bt([(200.5, 202, 200, 201.5)] + [(201.5 + 4 * k, 205 + 4 * k, 201 + 4 * k, 204.5 + 4 * k)
                                                       for k in range(12)])
        t = [x for x in res["trades"] if x["setup"] == "breakout"]
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["reason"], "target")
        self.assertEqual(t[0]["entry"], 200.5)            # the next day's open, not the signal close
        self.assertTrue(1.8 < t[0]["r"] < 2.0)            # 2R less delivery charges

    def test_gap_through_the_stop_fills_at_the_open(self):
        res = self.run_bt([(200.5, 202, 200, 201.5), (170, 171, 169, 170.5)])
        t = res["trades"][0]
        self.assertEqual(t["reason"], "gap through stop")
        self.assertEqual(t["exit"], 170)
        self.assertLess(t["r"], -1)                       # a gap loses more than 1R

    def test_stats(self):
        s = swing.stats([{"r": 2, "pnl": 200, "days": 5}, {"r": -1, "pnl": -100, "days": 3},
                         {"r": -1, "pnl": -100, "days": 2}])
        self.assertEqual(s["trades"], 3)
        self.assertAlmostEqual(s["win_rate"], 33.3)
        self.assertEqual(s["profit_factor"], 1.0)
        self.assertEqual(s["worst_losing_streak"], 2)


class HandOff(unittest.TestCase):
    """trader-swing plan -> trader-risk -> trader-broker, on files."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "trader"))
        shutil.copy(os.path.join(ROOT, "trader-budget", "config.example.json"),
                    os.path.join(self.d, "trader", "config.json"))
        sig = {"symbol": "TITAN", "setup": "breakout", "date": "2026-11-06", "close": 5000.0, "stop": 4754.5,
               "size_mult": 0.5, "why": "test"}
        order = swing.plan_trade(sig, dict(swing.DEFAULTS))
        with open(os.path.join(self.d, "trader", "orders.json"), "w") as fh:
            json.dump({"created_by": "trader-swing", "status": "proposed", "paper": True, "orders": [order]}, fh)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_risk_passes_with_the_swing_limit_not_the_intraday_one(self):
        code, out, _ = run(check, ["--now", "2026-11-09 09:20"], self.d)
        self.assertEqual(code, 0, out)
        self.assertIn("PASS  #1 BUY TITAN 3 shares", out)
        self.assertIn("(limit ₹2,000)", out)
        self.assertIn("GTT", out)

    def test_risk_blocks_a_swing_loss_over_the_cap(self):
        cfg_path = os.path.join(self.d, "trader", "config.json")
        with open(cfg_path) as fh:
            cfg = json.load(fh)
        cfg["swing"] = {"swing_capital": 50000, "risk_per_trade_pct": 1.0}
        with open(cfg_path, "w") as fh:
            json.dump(cfg, fh)
        code, out, _ = run(check, ["--now", "2026-11-09 09:20"], self.d)
        self.assertIn("BLOCK", out)
        self.assertIn("swing trade", out)

    def test_broker_shows_the_gtt_and_records_the_swing_position(self):
        run(check, ["--now", "2026-11-09 09:20"], self.d)
        code, out, _ = run(ticket, ["show", "--now", "2026-11-09 09:20"], self.d)
        self.assertIn("place_gtt_order", out)
        self.assertIn('"trigger_type": "two-leg"', out)
        run(ticket, ["approve", "--index", "1", "--reply", "yes", "--now", "2026-11-09 09:20"], self.d)
        code, out, _ = run(ticket, ["result", "--index", "1", "--status", "placed", "--paper", "--filled-qty", "3",
                                    "--filled-price", "5020", "--now", "2026-11-09 09:21"], self.d)
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.d, "trader", "swing-positions.json")) as fh:
            rows = json.load(fh)
        self.assertEqual((rows[0]["symbol"], rows[0]["qty"], rows[0]["stop"]), ("TITAN", 3.0, 4754.5))


class Manage(unittest.TestCase):
    def test_breakeven_and_time_stop(self):
        bars = bars_from([100 + k * 0.5 for k in range(300)])
        data = {"A": bars, "B": bars}
        d_entry = bars[-3]["d"].isoformat()
        rows = swing.manage([
            {"symbol": "A", "entry": bars[-3]["c"], "stop": bars[-3]["c"] - 0.8, "initial_stop": bars[-3]["c"] - 0.8,
             "entry_date": d_entry, "setup": "breakout"},
            {"symbol": "B", "entry": 200, "stop": 150, "initial_stop": 150,
             "entry_date": bars[-20]["d"].isoformat(), "setup": "breakout"}], data, dict(swing.DEFAULTS))
        self.assertEqual(rows[0]["action"], "move stop")
        self.assertEqual(rows[1]["action"], "exit")


if __name__ == "__main__":
    unittest.main()


class Momentum(unittest.TestCase):
    def burst(self, chg=6.0, vx=3.0, close_frac=0.95):
        closes = [100 + (k % 7) * 0.3 + k * 0.05 for k in range(120)]
        bars = bars_from(closes)
        y = bars[-1]
        c = y["c"] * (1 + chg / 100.0)
        lo = y["c"] * 1.0
        hi = lo + (c - lo) / close_frac
        d = y["d"] + dt.timedelta(days=1 if y["d"].weekday() < 4 else 3)
        bars.append({"d": d, "o": lo + 0.1, "h": hi, "l": lo, "c": c, "v": 1e6 * vx})
        return bars

    def test_fires_on_a_5pct_day_on_volume_closing_near_the_high(self):
        bars = self.burst()
        ind = swing.indicators(bars)
        sig = swing.setup_momentum("X", ind, len(bars) - 1, dict(swing.DEFAULTS))
        self.assertIsNotNone(sig)
        tick = swing.tick_for(bars[-1]["c"])
        self.assertEqual(sig["stop"], swing.floor_tick(bars[-1]["l"] - tick, tick))
        self.assertGreaterEqual(sig["day_change_pct"], 5)

    def test_needs_volume_and_a_strong_close(self):
        cfg = dict(swing.DEFAULTS)
        for kw in ({"vx": 1.5}, {"close_frac": 0.5}, {"chg": 4.0}):
            bars = self.burst(**kw)
            ind = swing.indicators(bars)
            self.assertIsNone(swing.setup_momentum("X", ind, len(bars) - 1, cfg), kw)

    def test_a_stop_too_far_is_skipped(self):
        bars = self.burst(chg=14.0)            # a 14% day: the low is more than 10% under the close
        ind = swing.indicators(bars)
        self.assertIsNone(swing.setup_momentum("X", ind, len(bars) - 1, dict(swing.DEFAULTS)))

    def test_plan_has_a_stop_only_gtt_and_a_5_day_hold(self):
        bars = self.burst()
        ind = swing.indicators(bars)
        sig = swing.setup_momentum("X", ind, len(bars) - 1, dict(swing.DEFAULTS))
        o = swing.plan_trade(sig, dict(swing.DEFAULTS))
        self.assertEqual(o["gtt"]["trigger_type"], "single")
        self.assertEqual(o["plan"]["time_stop_days"], 5)

    def test_backtest_bought_at_the_close_sells_on_day_5(self):
        bars = self.burst()
        for k in range(7):
            last = bars[-1]
            d = last["d"] + dt.timedelta(days=1 if last["d"].weekday() < 4 else 3)
            c = last["c"] + 0.5
            bars.append({"d": d, "o": last["c"], "h": c + 0.3, "l": last["c"] - 0.2, "c": c, "v": 1e6})
        res = swing.backtest({"X": bars}, None, dict(swing.DEFAULTS), setups=["momentum"], entry="close")
        t = res["trades"][0]
        self.assertEqual(t["entry"], round(bars[120]["c"], 2))      # the burst day's close
        self.assertEqual(t["reason"], "hold period over")
        self.assertEqual(t["exit"], round(bars[125]["c"], 2))       # the close 5 sessions later

    def test_study_counts_days_from_the_entry(self):
        bars = self.burst()
        for k in range(12):
            last = bars[-1]
            d = last["d"] + dt.timedelta(days=1 if last["d"].weekday() < 4 else 3)
            bars.append({"d": d, "o": last["c"], "h": last["c"] + 1.2, "l": last["c"] - 0.1,
                         "c": last["c"] + 1, "v": 1e6})
        rows, base = swing.forward_study({"X": bars}, None, dict(swing.DEFAULTS), ["momentum"], (1, 5), entry="close")
        self.assertEqual(len(rows), 1)
        e = bars[120]["c"]
        expect = (bars[125]["c"] - e) / e * 100
        self.assertAlmostEqual(rows[0]["hold"][5], expect, delta=0.4)    # less delivery charges
        self.assertLess(rows[0]["hold"][5], expect)

    def test_live_quotes_add_todays_bar(self):
        bars = bars_from([100 + k * 0.1 for k in range(30)])
        data = {"RELIANCE": bars}
        last = bars[-1]["d"]
        nxt = last + dt.timedelta(days=1 if last.weekday() < 4 else 3)
        q = {"NSE:RELIANCE": {"last_price": 110.0, "volume": 5e6, "last_trade_time": nxt.isoformat() + " 15:14:58",
                              "ohlc": {"open": 103, "high": 110.5, "low": 102.8, "close": bars[-1]["c"]}}}
        self.assertEqual(swing.merge_quotes(data, q), ["RELIANCE"])
        self.assertEqual(data["RELIANCE"][-1]["c"], 110.0)
        self.assertEqual(data["RELIANCE"][-1]["d"], nxt)
        swing.merge_quotes(data, q)
        self.assertEqual(len(data["RELIANCE"]), 31)                    # same day again: replaced, not added
