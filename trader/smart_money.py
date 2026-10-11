"""Follow the most profitable Hyperliquid accounts and report when they open, add to, flip or close positions.

Every Hyperliquid account's positions are public, so this is the same information copy-trading apps use.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Move:
    kind: str           # "open", "add", "flip" or "close"
    trader: dict
    coin: str
    side: str
    value_usd: float
    entry: float
    leverage: float
    liquidation: float


def _window(row, name):
    for window, perf in row.get("windowPerformances", []):
        if window == name:
            return perf
    return {}


def select_traders(rows, cfg):
    """Pick consistently profitable directional traders from the leaderboard, best 30-day P&L first."""
    picked = []
    for row in rows:
        try:
            month, all_time = _window(row, "month"), _window(row, "allTime")
            account = float(row.get("accountValue") or 0)
            pnl = float(month.get("pnl") or 0)
            roi_pct = float(month.get("roi") or 0) * 100   # API reports ROI as a fraction
            volume = float(month.get("vlm") or 0)
            all_time_pnl = float(all_time.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
        if (account < cfg["min_account_value"] or pnl < cfg["min_month_pnl"]
                or roi_pct < cfg["min_month_roi_pct"] or all_time_pnl <= 0
                or volume > account * cfg["max_month_volume_to_account"]):
            continue
        picked.append({"address": row["ethAddress"], "name": row.get("displayName") or row["ethAddress"][:8],
                       "month_pnl": pnl, "month_roi_pct": roi_pct, "account_value": account})
    picked.sort(key=lambda t: -t["month_pnl"])
    for rank, t in enumerate(picked[:cfg["top_n"]], 1):
        t["rank"] = rank
    return picked[:cfg["top_n"]]


def diff_positions(trader, before, after, min_usd):
    """Moves between two snapshots ({coin: [size, entry, value, leverage, liq]})."""
    moves = []
    for coin, (size, entry, value, lev, liq) in after.items():
        side = "long" if size > 0 else "short"
        prev = before.get(coin)
        if value < min_usd:
            continue
        if prev is None:
            moves.append(Move("open", trader, coin, side, value, entry, lev, liq))
        elif (prev[0] > 0) != (size > 0):
            moves.append(Move("flip", trader, coin, side, value, entry, lev, liq))
        elif abs(size) >= abs(prev[0]) * 1.5:
            moves.append(Move("add", trader, coin, side, value, entry, lev, liq))
    for coin, (size, entry, value, lev, liq) in before.items():
        flipped = coin in after and (after[coin][0] > 0) != (size > 0)
        if (coin not in after or flipped) and value >= min_usd:
            moves.append(Move("close", trader, coin, "long" if size > 0 else "short", value, entry, lev, liq))
    return moves


class SmartMoney:
    def __init__(self, cfg, hl, state):
        self.cfg = cfg["smart_money"]
        self.hl = hl
        self.d = state.setdefault("smart_money", {"traders": [], "picked_at": 0, "positions": {}})

    @property
    def traders(self):
        return self.d["traders"]

    def refresh(self, now):
        if now - self.d["picked_at"] < self.cfg["refresh_hours"] * 3600 and self.traders:
            return
        traders = select_traders(self.hl.leaderboard(), self.cfg)
        keep = {t["address"] for t in traders}
        self.d.update(traders=traders, picked_at=now,
                      positions={a: p for a, p in self.d["positions"].items() if a in keep})
        log.info("following %d top traders", len(traders))

    def _snapshot(self, trader):
        try:
            return trader, {p.coin: [p.size, p.entry, p.value_usd, p.leverage, p.liquidation]
                            for p in self.hl.positions(trader["address"])}
        except Exception as e:
            log.debug("positions for %s unavailable: %s", trader["address"], e)
            return trader, None

    def poll(self, now):
        """Refresh the trader list if due, fetch everyone's positions and return what changed."""
        self.refresh(now)
        moves = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            for trader, snap in pool.map(self._snapshot, self.traders):
                if snap is None:
                    continue
                before = self.d["positions"].get(trader["address"])
                if before is not None:  # first sighting just records, so a restart doesn't flood you
                    moves += diff_positions(trader, before, snap, self.cfg["min_position_usd"])
                self.d["positions"][trader["address"]] = snap
        return moves

    def consensus(self):
        """{coin: {"long": {"count", "usd"}, "short": {...}}} across followed traders."""
        out = {}
        for snap in self.d["positions"].values():
            for coin, (size, _, value, _, _) in snap.items():
                if value < self.cfg["min_position_usd"]:
                    continue
                side = out.setdefault(coin, {}).setdefault("long" if size > 0 else "short", {"count": 0, "usd": 0.0})
                side["count"] += 1
                side["usd"] += value
        return out

    def holders(self, coin, side):
        """Followed traders currently holding this coin on this side."""
        out = []
        for t in self.traders:
            p = self.d["positions"].get(t["address"], {}).get(coin)
            if p and (p[0] > 0) == (side == "long") and p[2] >= self.cfg["min_position_usd"]:
                out.append(t)
        return out
