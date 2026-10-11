import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import alerts  # noqa: E402
from config import load_config  # noqa: E402
from hyperliquid import Candle, Market  # noqa: E402
import backtest  # noqa: E402
from analyst import Analyst, build_prompt  # noqa: E402
from indicators import Fib, aggregate, atr, breakout, correlation, fib_swing  # noqa: E402
from main import Bot  # noqa: E402
from news import Catalyst, parse_binance, parse_upbit  # noqa: E402
from paper import Paper  # noqa: E402
from planner import make_plan  # noqa: E402
from scanner import BtcContext, Signal, btc_gate, evaluate, score_side  # noqa: E402
from smart_money import SmartMoney, diff_positions, select_traders  # noqa: E402
from state import State  # noqa: E402

CFG = load_config("/nonexistent")
HOUR = 3600
NOW = 1_760_000_000  # fixed "now"


def candles(closes, volumes=None, spread=0.01, step=HOUR, end=NOW):
    volumes = volumes or [100.0] * len(closes)
    end = end - end % step  # align like real exchange candles
    out = []
    for i, (c, v) in enumerate(zip(closes, volumes)):
        t = (end - (len(closes) - i) * step) * 1000
        out.append(Candle(t, t + step * 1000 - 1, c, c * (1 + spread), c * (1 - spread), c, v))
    return out


def m15(closes, volumes=None, spread=0.01, end=NOW):
    return candles(closes, volumes, spread, step=900, end=end)


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


def breakout_chart(n=200, base=0.55):
    """15m chart: flat, steady climb, then a high-volume breakout candle to 0.62 (also a clean fib swing)."""
    closes = [base] * (n - 31) + [base + 0.0017 * i for i in range(1, 31)] + [0.62]
    return m15(closes, [100.0] * (n - 1) + [400.0])


class FakeHL:
    def __init__(self, btc_closes=None):
        self.m = market()
        self.btc_closes = btc_closes
        self.trader_positions = {}
        self.board = []

    def markets(self):
        btc = self._btc(16, 900)[-1].c
        return [self.m, market("BTC", mark=btc, prev_day=btc, max_lev=40)]

    def mids(self):
        return {m.coin: m.mark for m in self.markets()}

    def _btc(self, count, step):
        closes = self.btc_closes or [100.0 + i * 0.01 for i in range(300)]
        return candles(closes[-count:], step=step)

    def closed_candles(self, coin, interval, count, now=None):
        step = {"15m": 900, "1h": 3600, "4h": 14400}[interval]
        if coin == "BTC":
            return self._btc(count, step)
        return breakout_chart(count)

    def leaderboard(self):
        return self.board

    def positions(self, address):
        return self.trader_positions.get(address, [])


class FakeNews:
    def catalysts(self, now):
        return {"ENA": [Catalyst("ENA", "long", "Upbit listing: ENA", now)]}


class NoNews:
    def catalysts(self, now):
        return {}


class FakeTG:
    chat_id = "42"
    enabled = True

    def __init__(self):
        self.sent, self.queue = [], []

    def send(self, text, buttons=None, reply_to=None):
        self.sent.append((text, buttons))
        return len(self.sent)

    def clear_buttons(self, message_id):
        pass

    def answer(self, cid, text):
        self.sent.append((text, None))

    def updates(self, offset, timeout=0):
        q, self.queue = self.queue, []
        return q


def make_bot(d, hl=None, news=None, cfg=CFG, analyst=None):
    tg = FakeTG()
    bot = Bot(cfg, State(Path(d) / "state.json"), hl or FakeHL(), news or FakeNews(), tg,
              Path(d) / "journal.csv", analyst, Path(d))
    return bot, tg


class BotTests(unittest.TestCase):
    def test_btc_downtrend_means_no_long_alerts(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {**CFG, "scan": {**CFG["scan"], "allow_shorts": False}}
            bot, tg = make_bot(d, FakeHL(btc_closes=[100.0 - i * 0.05 for i in range(300)]), cfg=cfg)
            bot.scan(NOW)
            self.assertEqual(tg.sent, [])

    def test_end_to_end_alert_and_followups(self):
        with tempfile.TemporaryDirectory() as d:
            bot, tg = make_bot(d)
            state = bot.state
            bot.scan(NOW)
            self.assertEqual(len(tg.sent), 1)
            text, buttons = tg.sent[0]
            self.assertIn("LONG ENA", text)
            self.assertIn("score 75/100", text)  # breakout + catalyst + BTC trend
            self.assertIn("15m close above 24h high", text)
            self.assertIn("15m fib", text)
            self.assertIn("4h trend up", text)
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
            bot.hl.btc_closes = [100.0] * 290 + [101.0] * 3 + [98.9]
            bot.scan(NOW + 1000)
            bot.scan(NOW + 1300)
            warnings = [t for t, _ in tg.sent if "against your" in t]
            self.assertEqual(len(warnings), 1)

            tg.queue = [{"update_id": 3, "message": {"chat": {"id": 42}, "text": "/status"}}]
            bot.handle_updates()
            self.assertIn("Paper balance", tg.sent[-1][0])
            state.save()
            self.assertEqual(State(Path(d) / "state.json").data["paper"]["positions"][0]["coin"], "ENA")


def leaderboard_row(addr, pnl, roi, vlm=1e6, account=500_000, all_time=1e6, name=None):
    return {"ethAddress": addr, "accountValue": str(account), "displayName": name,
            "windowPerformances": [["day", {"pnl": "0", "roi": "0", "vlm": "0"}],
                                   ["month", {"pnl": str(pnl), "roi": str(roi), "vlm": str(vlm)}],
                                   ["allTime", {"pnl": str(all_time), "roi": "1", "vlm": "1"}]]}


class SmartMoneyTests(unittest.TestCase):
    def test_select_filters_and_ranks(self):
        rows = [leaderboard_row("0xa", 200_000, 0.40),
                leaderboard_row("0xb", 900_000, 0.90, name="whale"),
                leaderboard_row("0xc", 300_000, 0.05),               # ROI too low
                leaderboard_row("0xd", 500_000, 0.50, vlm=1e9),      # market maker volume
                leaderboard_row("0xe", 400_000, 0.50, all_time=-1),  # lost money overall
                leaderboard_row("0xf", 20_000, 0.50)]                # too small
        picked = select_traders(rows, CFG["smart_money"])
        self.assertEqual([(t["address"], t["rank"]) for t in picked], [("0xb", 1), ("0xa", 2)])
        self.assertEqual(picked[0]["name"], "whale")
        self.assertAlmostEqual(picked[0]["month_roi_pct"], 90)

    def test_diff(self):
        t = {"address": "0xa", "rank": 1}
        before = {"ENA": [1000, 0.6, 600_000, 5, 0.4], "SOL": [-500, 150, 75_000, 3, 200], "DOGE": [10, 1, 10, 1, 0]}
        after = {"ENA": [2000, 0.6, 1_200_000, 5, 0.4], "SOL": [400, 150, 60_000, 3, 100],
                 "BTC": [2, 60000, 120_000, 10, 50000]}
        kinds = sorted((m.kind, m.coin, m.side) for m in diff_positions(t, before, after, 50_000))
        self.assertEqual(kinds, [("add", "ENA", "long"), ("close", "SOL", "short"), ("flip", "SOL", "long"),
                                 ("open", "BTC", "long")])

    def test_copy_alert_end_to_end(self):
        from hyperliquid import Position
        with tempfile.TemporaryDirectory() as d:
            hl = FakeHL()
            hl.board = [leaderboard_row("0xa", 900_000, 0.9, name="whale")]
            bot, tg = make_bot(d, hl, NoNews())
            bot.scan(NOW)                    # loads markets/BTC; no rule-based signal without news
            self.assertEqual(tg.sent, [])
            bot.follow_traders(NOW)          # first sighting only records
            hl.trader_positions["0xa"] = [Position("ENA", 1_000_000, 0.61, 610_000, 5, 0.45)]
            bot.follow_traders(NOW + 60)
            text, buttons = tg.sent[-1]
            self.assertIn("👀 COPY", text)
            self.assertIn("Top trader #1 (whale", text)
            self.assertIn("LONG worth $610,000", text)
            self.assertEqual(buttons[0][1], "took:1")
            self.assertEqual(bot.paper.find(1)["source"]["address"], "0xa")

            bot.paper.find(1)["taken"] = True
            hl.trader_positions["0xa"] = []
            bot.follow_traders(NOW + 120)
            self.assertIn("just <b>closed</b>", tg.sent[-1][0])

    def test_move_blocked_by_btc_is_info_only(self):
        from hyperliquid import Position
        with tempfile.TemporaryDirectory() as d:
            hl = FakeHL(btc_closes=[100.0 - i * 0.05 for i in range(300)])
            hl.board = [leaderboard_row("0xa", 900_000, 0.9)]
            bot, tg = make_bot(d, hl, NoNews())
            bot.scan(NOW)
            bot.follow_traders(NOW)
            hl.trader_positions["0xa"] = [Position("ENA", 1_000_000, 0.61, 610_000, 5, 0.45)]
            bot.follow_traders(NOW + 60)
            self.assertIn("Not copying: BTC 4h trend is down", tg.sent[-1][0])
            self.assertIsNone(tg.sent[-1][1])
            self.assertEqual(bot.paper.active(), [])

    def test_consensus_adds_score(self):
        sm = SmartMoney(CFG, FakeHL(), {})
        sm.d["positions"] = {"0xa": {"ENA": [1, 0.6, 100_000, 5, 0]}, "0xb": {"ENA": [2, 0.6, 200_000, 5, 0]}}
        cons = sm.consensus()["ENA"]["long"]
        self.assertEqual(cons, {"count": 2, "usd": 300_000})
        s1, _, _ = score_side("long", market(), None, None, [], True, CFG)
        s2, reasons, _ = score_side("long", market(), None, None, [], True, CFG, cons)
        self.assertEqual(s2 - s1, CFG["weights"]["smart_money"])
        self.assertIn("2 followed top traders are long", reasons[0])


class FifteenMinuteTests(unittest.TestCase):
    def test_aggregate(self):
        hourly = aggregate(m15([1, 2, 3, 4, 5, 6, 7, 8], end=NOW - NOW % 3600), 4)
        self.assertEqual(len(hourly), 2)
        self.assertEqual((hourly[0].o, hourly[0].c, hourly[0].v), (1, 4, 400))
        self.assertAlmostEqual(hourly[0].h, 4 * 1.01)

    def test_evaluate_finds_breakout_with_fib(self):
        btc = BtcContext(60000, True, 0.2, 0.5, hourly=aggregate(m15([100.0 + i for i in range(200)]), 4))
        sigs = evaluate(market(), breakout_chart(), btc, CFG, min_score=50)
        self.assertEqual([s.side for s in sigs], ["long"])
        self.assertEqual(sigs[0].score, 50)
        self.assertIsNotNone(sigs[0].fib)
        self.assertIsNotNone(sigs[0].btc_corr)


class BacktestTests(unittest.TestCase):
    def test_simulation_trades_a_breakout_and_reports(self):
        # 600 15m candles: flat, a breakout at candle 480, a pullback into the fib entry, then a rally through TP1 and TP2.
        n, b = 600, 480
        closes = [1.0] * (b - 30) + [1.0 + 0.002 * i for i in range(1, 30)] + [1.1] + [1.03] * 4
        closes += [1.1 + 0.01 * i for i in range(1, n - len(closes) + 1)]
        vols = [100.0] * (b - 1) + [500.0] + [100.0] * (n - b)
        history = {"BTC": m15([100.0 + 0.05 * i for i in range(n)]), "ENA": m15(closes, vols)}
        with tempfile.TemporaryDirectory() as d:
            paper = backtest.simulate(CFG, history, journal_path=Path(d) / "j.csv")
        self.assertGreaterEqual(len(paper.closed), 1)
        first = paper.closed[0]
        self.assertEqual((first["coin"], first["side"]), ("ENA", "long"))
        self.assertTrue(first["tp1_hit"])
        r = backtest.summarize(paper, 1000)
        self.assertGreater(r["pnl"], 0)
        report = backtest.format_report([("Current rules", r)], 4, 1)
        self.assertIn("win rate", report)

    def test_variants(self):
        names = [n for n, _ in backtest.variants(CFG)]
        self.assertEqual(names[0], "Current rules")
        self.assertFalse(dict(backtest.variants(CFG))["Without BTC gate"]["btc"]["require_trend"])
        self.assertTrue(CFG["btc"]["require_trend"])  # original untouched


class AnalystTests(unittest.TestCase):
    def test_disabled_without_key(self):
        cfg = {**CFG, "analyst": {**CFG["analyst"], "api_key": ""}}
        self.assertFalse(Analyst(cfg).enabled)

    def test_analyze_continues_paused_search_turn(self):
        from types import SimpleNamespace as NS
        calls = []

        class Messages:
            def create(self, **kw):
                calls.append({**kw, "messages": list(kw["messages"])})
                if len(calls) == 1:
                    return NS(stop_reason="pause_turn", content=[NS(type="server_tool_use")])
                return NS(stop_reason="end_turn", content=[NS(type="text", text="VERDICT: TAKE\n- listing")])

        class Err(Exception):
            pass
        a = Analyst({**CFG, "analyst": {**CFG["analyst"], "enabled": False}})
        a.client = NS(beta=NS(messages=Messages()))
        a._anthropic = NS(AuthenticationError=Err, RateLimitError=Err, APIStatusError=Err, APIConnectionError=Err)
        pos = {"id": 1, "coin": "ENA", "side": "long", "score": 80, "reasons": [], "entry": 0.6, "stop": 0.58,
               "tp1": 0.64, "tp2": 0.67, "leverage": 5}
        self.assertEqual(a.analyze(pos, None), "VERDICT: TAKE\n- listing")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["messages"][-1]["role"], "assistant")  # paused turn handed back to continue
        self.assertEqual(calls[0]["model"], "claude-opus-5-5")
        self.assertEqual(calls[0]["tools"][0]["type"], "web_search_20260209")
        self.assertEqual(calls[0]["fallbacks"], "default")

    def test_prompt_and_message(self):
        pos = {"id": 3, "coin": "ENA", "side": "long", "score": 80, "reasons": ["a", "b"], "entry": 0.6,
               "stop": 0.58, "tp1": 0.64, "tp2": 0.67, "leverage": 5, "method": "15m fib"}
        prompt = build_prompt(pos, "BTC 60,000 · 4h trend up")
        self.assertIn("LONG ENA", prompt)
        self.assertIn("- a", prompt)
        msg = alerts.analyst_message(pos, "VERDICT: AVOID\n- unlock tomorrow")
        self.assertIn("🔴", msg)
        self.assertIn("unlock tomorrow", msg)


class FormatTests(unittest.TestCase):
    def test_price(self):
        self.assertEqual(alerts.price(62345.12), "$62,345.1")
        self.assertEqual(alerts.price(0.612), "$0.61200")
        self.assertEqual(alerts.price(0.0000123456), "$0.000012346")


if __name__ == "__main__":
    unittest.main()
