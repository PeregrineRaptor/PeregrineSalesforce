import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import alerts  # noqa: E402
from config import load_config  # noqa: E402
from hyperliquid import Candle, Market  # noqa: E402
from indicators import Fib, atr, breakout, correlation, fib_swing  # noqa: E402
from main import Bot  # noqa: E402
from news import Catalyst, parse_binance, parse_upbit  # noqa: E402
from paper import Paper  # noqa: E402
from planner import make_plan  # noqa: E402
from scanner import BtcContext, Signal, btc_gate, score_side  # noqa: E402
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
        events = [e[0] for e in self.paper.update({"ENA": 0.63}, NOW + 7 * HOUR)]
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


def swing_up(n=96):
    """15m chart: flat at 0.55, rally to 0.64, pull back to 0.62."""
    closes = [0.55] * (n - 36) + [0.55 + 0.0045 * i for i in range(1, 21)] + [0.64 - 0.00125 * i for i in range(1, 17)]
    return candles(closes)


class FibTests(unittest.TestCase):
    def test_finds_recent_up_swing(self):
        fib = fib_swing(swing_up(), "long")
        self.assertAlmostEqual(fib.start, 0.55 * 0.99)
        self.assertAlmostEqual(fib.end, 0.64 * 1.01)
        self.assertAlmostEqual(fib.level(0.5), (fib.start + fib.end) / 2)
        self.assertGreater(fib.level(-0.272), fib.end)

    def test_ignores_old_or_small_swings(self):
        self.assertIsNone(fib_swing(candles([0.55] * 60 + [0.64] + [0.60] * 35), "long"))  # high 35 candles ago
        self.assertIsNone(fib_swing(candles([1.0] * 90 + [1.01] * 6, spread=0.001), "long"))  # 3% min move

    def test_short_swing_levels_mirror(self):
        fib = Fib("short", start=110, end=100)
        self.assertAlmostEqual(fib.level(0.618), 106.18)
        self.assertAlmostEqual(fib.level(-0.272), 97.28)

    def test_fib_plan(self):
        fib = fib_swing(swing_up(), "long")
        plan = make_plan(Signal(market(mark=0.62), "long", 80, [], atr=0.016, fib=fib), 1000, 1000, CFG)
        self.assertAlmostEqual(plan.entry, fib.level(0.618), places=4)
        self.assertAlmostEqual(plan.stop, fib.level(0.786) * 0.998, places=4)
        self.assertAlmostEqual(plan.tp1, fib.end, places=4)
        self.assertAlmostEqual(plan.tp2, fib.level(-0.272), places=4)
        self.assertAlmostEqual(plan.risk_usd, 20, delta=0.05)
        self.assertIn("15m fib", plan.method)

    def test_price_below_fib_stop_kills_setup(self):
        fib = fib_swing(swing_up(), "long")
        self.assertIsNone(make_plan(Signal(market(mark=0.55), "long", 80, [], atr=0.016, fib=fib), 1000, 1000, CFG))

    def test_deep_pullback_enters_at_market(self):
        fib = fib_swing(swing_up(), "long")
        plan = make_plan(Signal(market(mark=0.575), "long", 80, [], atr=0.016, fib=fib), 1000, 1000, CFG)
        self.assertAlmostEqual(plan.entry, 0.575)


class BtcTests(unittest.TestCase):
    def btc(self, up=True, h1=0.2, h4=0.5):
        return BtcContext(60000, up, h1, h4)

    def test_trend_gates_direction(self):
        self.assertIsNone(btc_gate("long", self.btc(up=True), CFG))
        self.assertIn("trend is down", btc_gate("long", self.btc(up=False), CFG))
        self.assertIsNone(btc_gate("short", self.btc(up=False, h1=-0.2, h4=-0.5), CFG))
        self.assertIn("trend is up", btc_gate("short", self.btc(up=True), CFG))

    def test_dump_blocks_longs_even_in_uptrend(self):
        self.assertIn("last hour", btc_gate("long", self.btc(h1=-1.8), CFG))
        self.assertIn("4 hours", btc_gate("long", self.btc(h4=-3.5), CFG))

    def test_no_btc_data_blocks_everything(self):
        self.assertIn("unavailable", btc_gate("long", None, CFG))

    def test_correlation(self):
        a = candles([1 + 0.01 * (i % 3) for i in range(50)])
        self.assertAlmostEqual(correlation(a, a), 1.0)


class FakeHL:
    def __init__(self, btc_closes=None):
        self.m = market()
        self.btc_closes = btc_closes

    def markets(self):
        btc = self._btc(16)[-1].c
        return [self.m, market("BTC", mark=btc, prev_day=btc, max_lev=40)]

    def mids(self):
        return {m.coin: m.mark for m in self.markets()}

    def _btc(self, count):
        return candles(self.btc_closes[-count:] if self.btc_closes else [100.0 + i * 0.01 for i in range(count)])

    def closed_candles(self, coin, interval, count, now=None):
        if coin == "BTC":
            return self._btc(count)
        if interval == "15m":
            return swing_up(count)
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
    def test_btc_downtrend_means_no_long_alerts(self):
        with tempfile.TemporaryDirectory() as d:
            tg = FakeTG()
            hl = FakeHL(btc_closes=[100.0 - i * 0.5 for i in range(30)])
            CFG_no_shorts = {**CFG, "scan": {**CFG["scan"], "allow_shorts": False}}
            bot = Bot(CFG_no_shorts, State(Path(d) / "s.json"), hl, FakeNews(), tg, Path(d) / "j.csv")
            bot.scan(NOW)
            self.assertEqual(tg.sent, [])

    def test_end_to_end_alert_and_followups(self):
        with tempfile.TemporaryDirectory() as d:
            state, tg = State(Path(d) / "state.json"), FakeTG()
            bot = Bot(CFG, state, FakeHL(), FakeNews(), tg, Path(d) / "journal.csv")
            bot.scan(NOW)
            self.assertEqual(len(tg.sent), 1)
            text, buttons = tg.sent[0]
            self.assertIn("LONG ENA", text)
            self.assertIn("score 75/100", text)  # breakout + catalyst + BTC trend
            self.assertIn("15m fib", text)
            self.assertIn("BTC 100 · 4h trend up", text)
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

            # BTC dumps 2% in the last hour while the trade is open: one warning, not repeated.
            bot.hl.btc_closes = [100.0] * 30 + [101.0] * 3 + [98.9]
            bot.scanner.hl = bot.hl
            bot.scan(NOW + 600)
            bot.scan(NOW + 900)
            warnings = [t for t, _ in tg.sent if "BTC" in t and "against your" in t]
            self.assertEqual(len(warnings), 1)

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
