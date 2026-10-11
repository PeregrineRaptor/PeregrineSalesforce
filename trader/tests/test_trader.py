import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import alerts  # noqa: E402
from config import load_config  # noqa: E402
from hyperliquid import Candle, Market  # noqa: E402
from indicators import atr, breakout  # noqa: E402
from main import Bot  # noqa: E402
from news import Catalyst, parse_binance, parse_upbit  # noqa: E402
from paper import Paper  # noqa: E402
from planner import make_plan  # noqa: E402
from scanner import Signal, score_side  # noqa: E402
from state import State  # noqa: E402

CFG = load_config("/nonexistent")
HOUR = 3600
NOW = 1_760_000_000  # fixed "now"


def candles(closes, volumes=None, spread=0.01):
    volumes = volumes or [100.0] * len(closes)
    out = []
    for i, (c, v) in enumerate(zip(closes, volumes)):
        t = (NOW - (len(closes) - i) * HOUR) * 1000
        out.append(Candle(t, t + HOUR * 1000 - 1, c, c * (1 + spread), c * (1 - spread), c, v))
    return out


def market(coin="ENA", mark=0.62, prev_day=0.58, funding=0.00004, oi=1_000_000, max_lev=10):
    return Market(coin, mark, prev_day, funding, oi, 50_000_000, max_lev)


class IndicatorTests(unittest.TestCase):
    def test_atr(self):
        self.assertAlmostEqual(atr(candles([1.0] * 20), 14), 0.02)

    def test_breakout_needs_volume(self):
        closes = [1.0] * 25 + [1.05]
        self.assertIsNone(breakout(candles(closes)))
        brk = breakout(candles(closes, [100.0] * 25 + [300.0]))
        self.assertEqual(brk.side, "long")
        self.assertAlmostEqual(brk.level, 1.01)
        self.assertAlmostEqual(brk.volume_ratio, 3.0)

    def test_breakdown(self):
        brk = breakout(candles([1.0] * 25 + [0.95], [100.0] * 25 + [250.0]))
        self.assertEqual(brk.side, "short")


class ScoreTests(unittest.TestCase):
    def brk(self, side="long"):
        return breakout(candles([1.0] * 25 + ([1.05] if side == "long" else [0.95]), [100.0] * 25 + [300.0]))

    def test_full_long_setup_scores_100(self):
        cat = [Catalyst("ENA", "long", "Binance listing: ENA", NOW)]
        score, reasons, blocked = score_side("long", market(), self.brk(), 14.0, cat, True, CFG)
        self.assertIsNone(blocked)
        self.assertEqual(score, 100)
        self.assertEqual(len(reasons), 4)

    def test_breakout_alone_is_below_threshold(self):
        score, _, _ = score_side("long", market(), self.brk(), None, [], None, CFG)
        self.assertLess(score, CFG["scan"]["min_score"])

    def test_crowded_funding_blocks_long(self):
        _, _, blocked = score_side("long", market(funding=0.001), self.brk(), 20, [], True, CFG)
        self.assertIn("funding", blocked)

    def test_chasing_blocks_long(self):
        _, _, blocked = score_side("long", market(mark=1.4, prev_day=1.0), self.brk(), 20, [], True, CFG)
        self.assertIn("already moved", blocked)

    def test_short_oi_needs_crowded_longs(self):
        s1, _, _ = score_side("short", market(funding=0.00001), None, 20, [], False, CFG)
        s2, _, _ = score_side("short", market(funding=0.0004), None, 20, [], False, CFG)
        self.assertEqual(s2 - s1, CFG["weights"]["open_interest"])


class PlanTests(unittest.TestCase):
    def test_sizes_to_two_percent_risk(self):
        sig = Signal(market(mark=0.62), "long", 82, ["x"], atr=0.016)
        plan = make_plan(sig, 1000, 1000, CFG)
        # entry 0.62 - 0.008 = 0.612, stop 1.5 ATR = 0.024 below, 3.9% stop
        self.assertAlmostEqual(plan.entry, 0.612)
        self.assertAlmostEqual(plan.stop, 0.588)
        self.assertAlmostEqual(plan.tp1, 0.66)
        self.assertAlmostEqual(plan.tp2, 0.708)
        self.assertAlmostEqual(plan.risk_usd, 20, delta=0.05)
        self.assertEqual(plan.leverage, 5)
        self.assertAlmostEqual(plan.margin, plan.size_usd / 5, delta=0.01)

    def test_short_plan_is_mirrored(self):
        plan = make_plan(Signal(market(mark=100), "short", 80, [], atr=2), 1000, 1000, CFG)
        self.assertGreater(plan.stop, plan.entry)
        self.assertLess(plan.tp1, plan.entry)
        self.assertLess(plan.tp2, plan.tp1)

    def test_tight_margin_raises_leverage_but_caps_it(self):
        plan = make_plan(Signal(market(mark=100), "long", 80, [], atr=0.5), 1000, 100, CFG)
        self.assertLessEqual(plan.leverage, CFG["account"]["max_leverage"])
        self.assertLessEqual(plan.margin, 100.01)

    def test_too_volatile_is_skipped(self):
        self.assertIsNone(make_plan(Signal(market(mark=1), "long", 80, [], atr=0.2), 1000, 1000, CFG))


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self.tmp.name) / "journal.csv"
        self.paper = Paper({}, CFG, self.journal)
        sig = Signal(market(mark=0.62), "long", 82, ["x"], atr=0.016)
        self.pos = self.paper.add(make_plan(sig, 1000, 1000, CFG), NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def run_prices(self, *prices):
        events = []
        for i, p in enumerate(prices, 1):
            events += [e[0] for e in self.paper.update({"ENA": p}, NOW + i * 60)]
        return events

    def test_winner_hits_tp1_tp2_then_trails(self):
        events = self.run_prices(0.615, 0.611, 0.64, 0.661, 0.70, 0.709, 0.75, 0.73)
        self.assertEqual(events, ["filled", "tp1", "tp2", "closed"])
        self.assertEqual(self.pos["exit_reason"], "trailing stop")
        self.assertGreater(self.paper.balance, 1000)
        self.assertTrue(self.journal.exists())
        self.assertEqual(self.paper.d["stats"], {"trades": 1, "wins": 1, "pnl": self.pos["realized"]})

    def test_loser_loses_about_risk(self):
        events = self.run_prices(0.611, 0.60, 0.587)
        self.assertEqual(events, ["filled", "closed"])
        self.assertEqual(self.pos["exit_reason"], "stop loss")
        self.assertAlmostEqual(self.paper.balance, 1000 - 20, delta=1.5)

    def test_breakeven_after_tp1(self):
        events = self.run_prices(0.611, 0.661, 0.611)
        self.assertEqual(events, ["filled", "tp1", "closed"])
        self.assertEqual(self.pos["exit_reason"], "breakeven stop")
        self.assertGreater(self.paper.balance, 1000)

    def test_unfilled_entry_expires(self):
        events = [e[0] for e in self.paper.update({"ENA": 0.63}, NOW + 5 * HOUR)]
        self.assertEqual(events, ["expired"])
        self.assertEqual(self.paper.d["stats"]["trades"], 0)

    def test_time_stop(self):
        self.paper.update({"ENA": 0.61}, NOW + 60)
        events = [e[0] for e in self.paper.update({"ENA": 0.62}, NOW + 25 * HOUR)]
        self.assertEqual(events, ["closed"])
        self.assertIn("time stop", self.pos["exit_reason"])

    def test_daily_loss_limit(self):
        self.assertFalse(self.paper.halted(NOW))
        self.paper.d["balance"] -= 61
        self.assertTrue(self.paper.halted(NOW))


class NewsTests(unittest.TestCase):
    def test_binance(self):
        cats = parse_binance([
            {"title": "Binance Will List Ethena (ENA) with Seed Tag Applied", "releaseDate": NOW * 1000},
            {"title": "Binance Will Delist ABC (ABC), XYZ (XYZ)", "releaseDate": NOW * 1000},
            {"title": "Binance Futures Will Launch ENAUSDT Perpetual", "releaseDate": NOW * 1000},
        ])
        self.assertEqual([(c.coin, c.side) for c in cats], [("ENA", "long"), ("ABC", "short"), ("XYZ", "short")])

    def test_upbit(self):
        cats = parse_upbit([{"title": "에테나(ENA) 신규 거래지원 안내 (KRW, BTC, USDT 마켓)",
                             "listed_at": "2026-10-11T09:00:00+09:00"}])
        self.assertEqual([(c.coin, c.side) for c in cats], [("ENA", "long")])


class FakeHL:
    def __init__(self):
        self.m = market()

    def markets(self):
        return [self.m]

    def mids(self):
        return {self.m.coin: self.m.mark}

    def closed_candles(self, coin, interval, count, now=None):
        if coin == "BTC":
            return candles([100.0 + i for i in range(count)])
        return candles([0.6] * (count - 1) + [0.62], [100.0] * (count - 1) + [400.0])


class FakeNews:
    def catalysts(self, now):
        return {"ENA": [Catalyst("ENA", "long", "Upbit listing: ENA", now)]}


class FakeTG:
    chat_id = "42"
    enabled = True

    def __init__(self):
        self.sent, self.queue = [], []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))
        return len(self.sent)

    def clear_buttons(self, message_id):
        pass

    def answer(self, cid, text):
        self.sent.append((text, None))

    def updates(self, offset, timeout=0):
        q, self.queue = self.queue, []
        return q


class BotTests(unittest.TestCase):
    def test_end_to_end_alert_and_followups(self):
        with tempfile.TemporaryDirectory() as d:
            state, tg = State(Path(d) / "state.json"), FakeTG()
            bot = Bot(CFG, state, FakeHL(), FakeNews(), tg, Path(d) / "journal.csv")
            bot.scan(NOW)
            self.assertEqual(len(tg.sent), 1)
            text, buttons = tg.sent[0]
            self.assertIn("LONG ENA", text)
            self.assertIn("score 75/100", text)  # breakout + catalyst + BTC trend
            self.assertEqual(buttons[0][1], "took:1")

            bot.scan(NOW + 300)  # cooldown: no duplicate alert
            self.assertEqual(len(tg.sent), 1)

            tg.queue = [{"update_id": 1, "callback_query": {"id": "c", "data": "took:1",
                                                           "message": {"message_id": 1, "chat": {"id": 42}}}},
                        {"update_id": 2, "message": {"chat": {"id": 999}, "text": "/status"}}]  # stranger ignored
            bot.handle_updates()
            self.assertTrue(bot.paper.find(1)["taken"])
            self.assertEqual(state.data["tg_offset"], 3)

            pos = bot.paper.find(1)
            bot.check_prices(NOW + 400, {"ENA": pos["entry"]})
            bot.check_prices(NOW + 500, {"ENA": pos["tp1"]})
            msgs = [t for t, _ in tg.sent]
            self.assertTrue(any("Your limit should be filled" in m for m in msgs))
            self.assertTrue(any("TP1 hit" in m for m in msgs))

            tg.queue = [{"update_id": 3, "message": {"chat": {"id": 42}, "text": "/status"}}]
            bot.handle_updates()
            self.assertIn("Paper balance", tg.sent[-1][0])
            state.save()
            self.assertEqual(State(Path(d) / "state.json").data["paper"]["positions"][0]["coin"], "ENA")


class FormatTests(unittest.TestCase):
    def test_price(self):
        self.assertEqual(alerts.price(62345.12), "$62,345.1")
        self.assertEqual(alerts.price(0.612), "$0.61200")
        self.assertEqual(alerts.price(0.0000123456), "$0.000012346")


if __name__ == "__main__":
    unittest.main()
