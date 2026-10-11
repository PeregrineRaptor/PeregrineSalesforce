"""Trade alert bot: scans Hyperliquid perps, sends trade plans to Telegram, paper-trades them.

    python main.py                 run forever
    python main.py --once          one scan, then exit
    python main.py --find-chat-id  print your Telegram chat id
"""
import argparse
import logging
import time
from pathlib import Path

import alerts
from config import HERE, load_config
from hyperliquid import Hyperliquid
from news import News
from paper import Paper
from planner import make_plan
from scanner import Scanner
from state import State
from telegram import Telegram

log = logging.getLogger("trader")


class Bot:
    def __init__(self, cfg, state, hl, news, tg, journal_path):
        self.cfg = cfg
        self.state = state
        self.hl = hl
        self.tg = tg
        self.scanner = Scanner(cfg, hl, news, state.data)
        self.paper = Paper(state.data, cfg, journal_path)
        self.prices = {}
        self.data = state.data
        self.data.setdefault("cooldowns", {})
        self.data.setdefault("paused", False)

    # ---- trading loop ----------------------------------------------------------
    def check_prices(self, now, prices=None):
        self.prices = prices or self.hl.mids()
        for kind, pos, info in self.paper.update(self.prices, now):
            log.info("#%s %s %s: %s", pos["id"], pos["coin"], pos["side"], kind)
            if pos["taken"]:
                msg = alerts.event_message(kind, pos, info, self.paper.balance)
                if msg:
                    self.tg.send(msg)
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
                self.tg.send(alerts.btc_warning(pos, btc))

    def scan(self, now):
        signals, markets = self.scanner.run(now)
        self.check_prices(now, {m.coin: m.mark for m in markets})
        self.warn_on_btc(now)

        if self.data["paused"]:
            return
        if self.paper.halted(now):
            if self.data.get("halt_notified") != self.paper.d["day"]:
                self.data["halt_notified"] = self.paper.d["day"]
                self.tg.send("⛔ Daily loss limit hit. No new alerts until 00:00 UTC. "
                             "Open trades are still tracked.")
            return

        cooldown = self.cfg["scan"]["cooldown_hours"] * 3600
        cds = self.data["cooldowns"]
        for key in [k for k, ts in cds.items() if now - ts > cooldown]:
            del cds[key]
        active_coins = {p["coin"] for p in self.paper.active()}

        for sig in signals:
            if len(self.paper.active()) >= self.cfg["account"]["max_open_positions"]:
                log.info("max open positions reached, skipping remaining signals")
                break
            key = f"{sig.market.coin}:{sig.side}"
            if key in cds or sig.market.coin in active_coins:
                continue
            plan = make_plan(sig, self.paper.balance, self.paper.free_margin(), self.cfg)
            if not plan:
                continue
            pos = self.paper.add(plan, now)
            if self.scanner.btc:
                pos["btc_note"] = self.scanner.btc.summary()
            cds[key] = now
            active_coins.add(plan.coin)
            pos["message_id"] = self.tg.send(alerts.trade_alert(pos, self.paper.balance, self.cfg),
                                             alerts.trade_buttons(pos))
            log.info("alert #%s %s %s score %s", pos["id"], plan.coin, plan.side, plan.score)

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
                self.tg.send(alerts.event_message("filled", pos, {}, self.paper.balance))
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
        scan_every = self.cfg["scan"]["interval_seconds"]
        price_every = self.cfg["scan"]["price_check_seconds"]
        last_scan = last_prices = 0.0
        self.tg.send("🤖 Trade alert bot started. /help for commands.")
        while True:
            now = time.time()
            for name, due, job in (("scan", now - last_scan >= scan_every, self.scan),
                                   ("price check", now - last_prices >= price_every, self.check_prices)):
                if not due:
                    continue
                try:
                    job(now)
                except Exception:
                    log.exception("%s failed, will retry", name)
                if name == "scan":
                    last_scan = last_prices = now
                else:
                    last_prices = now
                self.state.save()
            try:
                # Long-poll Telegram so button taps are handled within a few seconds.
                self.handle_updates(timeout=5 if self.tg.enabled else 0)
                self.state.save()
            except Exception:
                log.exception("telegram polling failed")
                time.sleep(5)
            if not self.tg.enabled:
                time.sleep(5)


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
    bot = Bot(cfg, state, Hyperliquid(), News(cfg), tg, data_dir / "journal.csv")
    try:
        if args.once:
            bot.scan(time.time())
        else:
            bot.run_forever()
    finally:
        state.save()


if __name__ == "__main__":
    main()
