"""Trade alert bot: scans Hyperliquid perps, follows top traders, sends trade plans to Telegram, paper-trades them.

    python main.py                 run forever
    python main.py --once          one scan, then exit
    python main.py --find-chat-id  print your Telegram chat id
"""
import argparse
import logging
import queue
import threading
import time
from pathlib import Path

import alerts
import backtest
from analyst import Analyst
from config import HERE, load_config
from engine import open_positions
from hyperliquid import Hyperliquid
from indicators import aggregate, atr
from news import News
from paper import Paper
from scanner import Scanner, Signal, btc_gate, enrich, score_side
from smart_money import SmartMoney
from state import State
from telegram import Telegram

log = logging.getLogger("trader")


class Bot:
    def __init__(self, cfg, state, hl, news, tg, journal_path, analyst=None, data_dir=HERE):
        self.cfg = cfg
        self.state = state
        self.hl = hl
        self.tg = tg
        self.data_dir = data_dir
        self.smart = SmartMoney(cfg, hl, state.data) if cfg["smart_money"]["enabled"] else None
        self.scanner = Scanner(cfg, hl, news, state.data, self.smart)
        self.paper = Paper(state.data, cfg, journal_path)
        self.analyst = analyst
        self.outbox = queue.Queue()   # messages produced by background threads (analyst, backtest)
        self.prices = {}
        self.data = state.data
        self.data.setdefault("cooldowns", {})
        self.data.setdefault("paused", False)
        self._backtest_running = False

    # ---- alerts ----------------------------------------------------------------
    def _alert(self, sig, pos):
        btc = self.scanner.btc
        if btc:
            pos["btc_note"] = btc.summary()
        pos["message_id"] = self.tg.send(alerts.trade_alert(pos, self.paper.balance, self.cfg),
                                         alerts.trade_buttons(pos))
        log.info("alert #%s %s %s score %s", pos["id"], pos["coin"], pos["side"], pos["score"])
        if self.analyst:
            self.analyst.submit(pos, btc.summary() if btc else None)

    def can_open(self, now):
        if self.data["paused"]:
            return False
        if self.paper.halted(now):
            if self.data.get("halt_notified") != self.paper.d["day"]:
                self.data["halt_notified"] = self.paper.d["day"]
                self.tg.send("⛔ Daily loss limit hit. No new alerts until 00:00 UTC. "
                             "Open trades are still tracked.")
            return False
        return True

    # ---- jobs ------------------------------------------------------------------
    def check_prices(self, now, prices=None):
        self.prices = prices or self.hl.mids()
        for kind, pos, info in self.paper.update(self.prices, now):
            log.info("#%s %s %s: %s", pos["id"], pos["coin"], pos["side"], kind)
            if pos["taken"]:
                msg = alerts.event_message(kind, pos, info, self.paper.balance)
                if msg:
                    self.tg.send(msg, reply_to=pos["message_id"])
            if kind == "expired":
                self.tg.clear_buttons(pos["message_id"])

    def warn_on_btc(self, now):
        """BTC moving hard against a trade you took: tell you, at most once an hour per trade."""
        btc = self.scanner.btc
        limit = self.cfg["btc"]["warn_open_trades_1h_pct"]
        if not btc:
            return
        for pos in self.paper.active():
            s = 1 if pos["side"] == "long" else -1
            if (pos["taken"] and pos["status"] == "open" and pos["coin"] != "BTC"
                    and s * btc.change_1h_pct <= -limit and now - pos.get("btc_warned_at", 0) >= 3600):
                pos["btc_warned_at"] = now
                self.tg.send(alerts.btc_warning(pos, btc), reply_to=pos["message_id"])

    def scan(self, now):
        signals, markets = self.scanner.run(now)
        self.check_prices(now, {m.coin: m.mark for m in markets})
        self.warn_on_btc(now)
        if self.can_open(now):
            for sig, pos in open_positions(self.paper, signals, self.data["cooldowns"], self.cfg, now):
                self._alert(sig, pos)

    def follow_traders(self, now):
        if not self.smart:
            return
        for move in self.smart.poll(now):
            log.info("top trader #%s %s %s %s $%.0f", move.trader["rank"], move.kind, move.coin, move.side,
                     move.value_usd)
            if move.kind == "close":
                self._trader_closed(move)
            elif self.cfg["smart_money"]["alert_on_open"]:
                self._copy(move, now)

    def _trader_closed(self, move):
        for pos in self.paper.active():
            src = pos.get("source") or {}
            if (pos["taken"] and pos["coin"] == move.coin and pos["side"] == move.side
                    and src.get("address") == move.trader["address"]):
                self.tg.send(alerts.trader_closed(pos, move), reply_to=pos["message_id"])

    def _info(self, move, why_not, now):
        """'Not copying' notices, at most one per coin and side per cooldown window so they never flood you."""
        key = f"info:{move.coin}:{move.side}"
        cds = self.data["cooldowns"]
        if now - cds.get(key, 0) < self.cfg["scan"]["cooldown_hours"] * 3600:
            return
        cds[key] = now
        self.tg.send(alerts.move_info(move, why_not))

    def _copy(self, move, now):
        """A followed trader opened/added/flipped: send a plan with our own levels if the rules allow it."""
        market = self.scanner.markets.get(move.coin)
        btc = self.scanner.btc
        why_not = btc_gate(move.side, btc, self.cfg)
        if not why_not and not market:
            why_not = "no market data yet"
        if not why_not and not self.can_open(now):
            why_not = "alerts paused or daily loss limit hit"
        if not why_not and len(self.paper.active()) >= self.cfg["account"]["max_open_positions"]:
            why_not = f"already {len(self.paper.active())} trades open"
        if why_not:
            self._info(move, why_not, now)
            return
        score, reasons, blocked = score_side(move.side, market, None, self.scanner.oi_change_pct(move.coin, now),
                                             [], btc.trend_up if btc else None, self.cfg)
        if blocked:
            self._info(move, blocked, now)
            return
        reasons = [alerts.move_reason(move)] + reasons
        holders = self.smart.holders(move.coin, move.side)
        if len(holders) > 1:
            reasons.insert(1, f"{len(holders)} followed top traders are now {move.side} {move.coin}")
        m15 = self.scanner.candles(move.coin, now)
        hourly = aggregate(m15, 4)
        a = atr(hourly, self.cfg["plan"]["atr_period"])
        if not a:
            self._info(move, "not enough price history", now)
            return
        sig = enrich(Signal(market, move.side, score + self.cfg["weights"]["smart_money"], reasons, a,
                            source={"address": move.trader["address"], "rank": move.trader["rank"]}),
                     m15, hourly, btc, self.cfg)
        opened = open_positions(self.paper, [sig], self.data["cooldowns"], self.cfg, now)
        if not opened:
            self._info(move, "already alerted on this coin recently, or no valid levels", now)
            return
        self._alert(*opened[0])

    def flush_outbox(self):
        if self.analyst:
            while not self.analyst.results.empty():
                pos, text = self.analyst.results.get()
                live = self.paper.find(pos["id"]) or pos
                self.tg.send(alerts.analyst_message(live, text), reply_to=live.get("message_id"))
        while not self.outbox.empty():
            self.tg.send(self.outbox.get())

    def start_backtest(self):
        if self._backtest_running:
            self.tg.send("A backtest is already running.")
            return
        self._backtest_running = True
        self.tg.send("📊 Backtest started: downloading 50 days of candles, about 3–5 minutes the first time.")

        def work():
            try:
                self.outbox.put(backtest.run(self.cfg, Hyperliquid(), data_dir=self.data_dir))
            except Exception as e:
                log.exception("backtest failed")
                self.outbox.put(f"Backtest failed: {e}")
            finally:
                self._backtest_running = False
        threading.Thread(target=work, daemon=True).start()

    # ---- telegram --------------------------------------------------------------
    def handle_updates(self, timeout=0):
        offset = self.data.get("tg_offset")
        for upd in self.tg.updates(offset, timeout):
            self.data["tg_offset"] = upd["update_id"] + 1
            if "callback_query" in upd:
                self._on_button(upd["callback_query"])
            elif "message" in upd:
                self._on_message(upd["message"])

    def _mine(self, chat):
        return str(chat.get("id")) == self.tg.chat_id

    def _on_button(self, cq):
        msg = cq.get("message", {})
        if not self._mine(msg.get("chat", {})):
            return
        action, _, pos_id = cq.get("data", "").partition(":")
        pos = self.paper.find(int(pos_id)) if pos_id.isdigit() else None
        self.tg.clear_buttons(msg.get("message_id"))
        if not pos:
            self.tg.answer(cq["id"], "That trade is already closed.")
            return
        pos["taken"] = action == "took"
        if pos["taken"]:
            self.tg.answer(cq["id"], "Logged. I'll tell you when to take profit or exit.")
            if pos["status"] == "open":
                self.tg.send(alerts.event_message("filled", pos, {}, self.paper.balance),
                             reply_to=pos["message_id"])
        else:
            self.tg.answer(cq["id"], "Skipped. Still paper-tracking it for your stats.")

    def _on_message(self, message):
        if not self._mine(message.get("chat", {})):
            return
        cmd = (message.get("text") or "").split()[0:1]
        cmd = cmd[0].split("@")[0].lower() if cmd else ""
        now = time.time()
        if cmd == "/status":
            self.tg.send(alerts.status_message(self.paper, self.prices, now, self.data["paused"],
                                               self.scanner.btc))
        elif cmd == "/stats":
            self.tg.send(alerts.stats_message(self.paper, self.cfg["account"]["starting_balance"]))
        elif cmd == "/backtest":
            self.start_backtest()
        elif cmd == "/traders":
            self.tg.send(alerts.traders_message(self.smart) if self.smart else "Top-trader following is off.")
        elif cmd == "/pause":
            self.data["paused"] = True
            self.tg.send("⏸ Alerts paused. Open trades are still tracked. /resume to restart.")
        elif cmd == "/resume":
            self.data["paused"] = False
            self.tg.send("▶️ Alerts resumed.")
        elif cmd == "/scan":
            self.tg.send("🔎 Scanning…")
            self.scan(now)
            self.tg.send("Scan done.")
        else:
            self.tg.send(alerts.HELP)

    # ---- main loop -------------------------------------------------------------
    def run_forever(self):
        sc = self.cfg["scan"]
        jobs = [("scan", sc["interval_seconds"], self.scan),
                ("price check", sc["price_check_seconds"], self.check_prices)]
        if self.smart:
            jobs.append(("top traders", self.cfg["smart_money"]["poll_seconds"], self.follow_traders))
        last = {name: 0.0 for name, _, _ in jobs}
        self.tg.send("🤖 Trade alert bot started. /help for commands."
                     + ("" if not self.analyst or self.analyst.enabled else
                        "\n(AI analyst off: add an Anthropic API key to turn it on.)"))
        while True:
            for name, every, job in jobs:
                now = time.time()
                if now - last[name] < every:
                    continue
                last[name] = now
                try:
                    job(now)
                except Exception:
                    log.exception("%s failed, will retry", name)
                if name == "scan":
                    last["price check"] = now
            try:
                self.flush_outbox()
                # Long-poll Telegram so button taps are handled within a few seconds.
                self.handle_updates(timeout=3 if self.tg.enabled else 0)
            except Exception:
                log.exception("telegram failed")
                time.sleep(5)
            self.state.save()
            if not self.tg.enabled:
                time.sleep(3)


def find_chat_id(cfg):
    tg = Telegram(cfg["telegram"]["bot_token"], "")
    if not tg.token:
        print("Set telegram.bot_token in config.toml first.")
        return
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name") or u["message"]["chat"].get("title")
             for u in tg.updates(None) if "message" in u}
    if not chats:
        print("No messages yet. Open your bot in Telegram, send it any message, then run this again.")
    for chat_id, name in chats.items():
        print(f"chat_id = \"{chat_id}\"   ({name})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.toml")
    ap.add_argument("--data-dir", default=str(HERE), help="where state.json and journal.csv are kept")
    ap.add_argument("--once", action="store_true", help="run one scan and exit")
    ap.add_argument("--find-chat-id", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.find_chat_id:
        return find_chat_id(cfg)

    data_dir = Path(args.data_dir)
    state = State(data_dir / "state.json")
    tg = Telegram(cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"])
    if not tg.enabled:
        log.warning("Telegram not configured; alerts will print here instead.")
    bot = Bot(cfg, state, Hyperliquid(), News(cfg), tg, data_dir / "journal.csv", Analyst(cfg), data_dir)
    try:
        if args.once:
            bot.follow_traders(time.time())
            bot.scan(time.time())
        else:
            bot.run_forever()
    finally:
        state.save()


if __name__ == "__main__":
    main()
