"""Paper-trades every alert against live prices so you can see whether the rules actually make money."""
import csv
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

JOURNAL_FIELDS = ["id", "coin", "side", "score", "reasons", "taken", "entry", "stop", "tp1", "tp2",
                  "size_usd", "leverage", "margin", "created_at", "filled_at", "closed_at",
                  "exit_reason", "pnl_usd", "pnl_pct_on_margin", "balance_after"]
EPS = 1e-9


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else ""


class Paper:
    def __init__(self, state, cfg, journal_path, keep_closed=False):
        self.cfg = cfg
        self.closed = [] if keep_closed else None   # the backtester keeps every closed trade for its report
        self.journal_path = Path(journal_path)
        start = cfg["account"]["starting_balance"]
        self.d = state.setdefault("paper", {
            "balance": start, "positions": [], "next_id": 1, "day": None, "day_start": start,
            "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        })

    # ---- account ---------------------------------------------------------------
    @property
    def balance(self):
        return self.d["balance"]

    def active(self):
        return [p for p in self.d["positions"] if p["status"] in ("pending", "open")]

    def find(self, pos_id):
        return next((p for p in self.d["positions"] if p["id"] == pos_id), None)

    def free_margin(self):
        used = sum(p["margin"] * p["remaining"] for p in self.active())
        return max(0.0, self.balance - used)

    def _roll_day(self, now):
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if self.d["day"] != day:
            self.d["day"], self.d["day_start"] = day, self.balance

    def day_pnl(self, now):
        self._roll_day(now)
        return self.balance - self.d["day_start"]

    def halted(self, now):
        """True once today's realized loss reaches the daily limit."""
        pnl = self.day_pnl(now)
        return pnl <= -self.d["day_start"] * self.cfg["account"]["daily_loss_limit_pct"] / 100

    # ---- lifecycle -------------------------------------------------------------
    def add(self, plan, now=None):
        pos = asdict(plan)
        pos.update(id=self.d["next_id"], status="pending", created_at=now or time.time(), filled_at=None,
                   closed_at=None, remaining=1.0, tp1_hit=False, tp2_hit=False, best=None,
                   initial_stop=plan.stop, realized=0.0, taken=None, exit_reason=None, message_id=None)
        self.d["next_id"] += 1
        self.d["positions"].append(pos)
        return pos

    def _close(self, pos, frac, price):
        qty = pos["size_usd"] / pos["entry"]
        s = 1 if pos["side"] == "long" else -1
        pnl = s * qty * frac * (price - pos["entry"]) - qty * frac * price * self.cfg["account"]["taker_fee_pct"] / 100
        self.d["balance"] += pnl
        pos["realized"] += pnl
        pos["remaining"] = max(0.0, pos["remaining"] - frac)
        return pnl

    def _finish(self, pos, reason, now, count_trade=True):
        pos.update(status="closed", closed_at=now, exit_reason=reason)
        if count_trade:
            st = self.d["stats"]
            st["trades"] += 1
            st["wins"] += pos["realized"] > 0
            st["pnl"] += pos["realized"]
        self._journal(pos)
        self.d["positions"].remove(pos)
        if self.closed is not None and count_trade:
            self.closed.append(pos)

    def update(self, prices, now=None):
        """Advance every pending/open position to the latest prices. Returns [(event, pos, info)]."""
        now = now or time.time()
        p_cfg, acct = self.cfg["plan"], self.cfg["account"]
        events = []
        for pos in self.active():
            price = prices.get(pos["coin"])
            if price is None:
                continue
            s = 1 if pos["side"] == "long" else -1

            if pos["status"] == "pending":
                if now - pos["created_at"] >= p_cfg["entry_expiry_hours"] * 3600:
                    self._finish(pos, "entry not filled", now, count_trade=False)
                    events.append(("expired", pos, {}))
                elif s * (pos["entry"] - price) >= 0:
                    fee = pos["size_usd"] * acct["maker_fee_pct"] / 100
                    self.d["balance"] -= fee
                    pos.update(status="open", filled_at=now, best=pos["entry"], realized=-fee)
                    events.append(("filled", pos, {}))
                continue

            if s * (price - pos["best"]) > 0:
                pos["best"] = price

            if s * (price - pos["stop"]) <= 0:
                reason = ("stop loss" if not pos["tp1_hit"]
                          else "breakeven stop" if not pos["tp2_hit"] else "trailing stop")
                self._close(pos, pos["remaining"], price)
                self._finish(pos, reason, now)
                events.append(("closed", pos, {"price": price}))
                continue

            if not pos["tp1_hit"] and s * (price - pos["tp1"]) >= 0:
                pnl = self._close(pos, min(pos["remaining"], p_cfg["tp1_close_pct"] / 100), pos["tp1"])
                pos.update(tp1_hit=True, stop=pos["entry"])
                events.append(("tp1", pos, {"pnl": pnl}))
            if pos["tp1_hit"] and not pos["tp2_hit"] and s * (price - pos["tp2"]) >= 0:
                pnl = self._close(pos, min(pos["remaining"], p_cfg["tp2_close_pct"] / 100), pos["tp2"])
                pos["tp2_hit"] = True
                events.append(("tp2", pos, {"pnl": pnl}))
            if pos["tp2_hit"]:
                trail = pos["best"] - s * p_cfg["trail_atr"] * pos["atr"]
                if s * (trail - pos["stop"]) > 0:
                    pos["stop"] = trail

            held_h = (now - pos["filled_at"]) / 3600
            reason = None
            if pos["remaining"] <= EPS:
                reason = "all targets hit"
            elif not pos["tp1_hit"] and held_h >= p_cfg["time_stop_hours"]:
                reason = f"time stop ({p_cfg['time_stop_hours']}h, TP1 not hit)"
            elif held_h >= p_cfg["max_hold_hours"]:
                reason = f"max hold ({p_cfg['max_hold_hours']}h)"
            if reason:
                if pos["remaining"] > EPS:
                    self._close(pos, pos["remaining"], price)
                self._finish(pos, reason, now)
                events.append(("closed", pos, {"price": price}))
        return events

    # ---- journal ---------------------------------------------------------------
    def _journal(self, pos):
        new = not self.journal_path.exists()
        with open(self.journal_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            if new:
                w.writeheader()
            w.writerow({
                **{k: pos[k] for k in ("id", "coin", "side", "score", "entry", "stop", "tp1", "tp2",
                                       "size_usd", "leverage", "margin", "exit_reason")},
                "stop": pos["initial_stop"],
                "reasons": " | ".join(pos["reasons"]),
                "taken": {True: "yes", False: "no"}.get(pos["taken"], "unanswered"),
                "created_at": utc(pos["created_at"]), "filled_at": utc(pos["filled_at"]),
                "closed_at": utc(pos["closed_at"]),
                "pnl_usd": round(pos["realized"], 2),
                "pnl_pct_on_margin": round(pos["realized"] / pos["margin"] * 100, 1) if pos["margin"] else 0,
                "balance_after": round(self.balance, 2),
            })
