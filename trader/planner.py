"""Turns a signal into an exact order plan: limit entry, stop, take-profits, size and leverage."""
import math
from dataclasses import dataclass


def round_sig(x, sig=5):
    """Hyperliquid accepts at most 5 significant figures on prices."""
    return float(f"{x:.{sig}g}")


@dataclass
class Plan:
    coin: str
    side: str
    score: int
    reasons: list
    entry: float
    stop: float
    tp1: float
    tp2: float
    size_usd: float
    leverage: int
    margin: float
    risk_usd: float
    atr: float


def make_plan(signal, balance, free_margin, cfg):
    acct, p = cfg["account"], cfg["plan"]
    s = 1 if signal.side == "long" else -1
    mark, a = signal.market.mark, signal.atr

    # Limit entry on a small pullback, or at the retest of the broken level if that's closer.
    entry = mark - s * p["entry_pullback_atr"] * a
    if signal.level is not None and s * (mark - signal.level) > 0:
        entry = max(entry, signal.level) if s > 0 else min(entry, signal.level)
    if entry <= 0:
        return None

    stop = entry - s * p["stop_atr"] * a
    risk_per_unit = abs(entry - stop)
    stop_pct = risk_per_unit / entry
    if stop <= 0 or stop_pct * 100 > p["max_stop_pct"]:
        return None

    tp1 = entry + s * p["tp1_r"] * risk_per_unit
    tp2 = entry + s * p["tp2_r"] * risk_per_unit
    if tp1 <= 0 or tp2 <= 0:
        return None

    # Size so that hitting the stop loses exactly risk_per_trade_pct of the account.
    risk_usd = balance * acct["risk_per_trade_pct"] / 100
    size = risk_usd / stop_pct

    # Leverage: default, raised only if margin is short, capped by config, exchange,
    # and keeping liquidation (~1/leverage away) well beyond the stop.
    lev_cap = max(1, min(acct["max_leverage"], signal.market.max_leverage, math.floor(0.8 / stop_pct)))
    needed = math.ceil(size / free_margin) if free_margin > 0 else lev_cap
    leverage = min(max(acct["default_leverage"], needed), lev_cap)
    if size / leverage > free_margin:
        size = free_margin * leverage
    if size < acct["min_order_usd"]:
        return None

    return Plan(
        coin=signal.market.coin, side=signal.side, score=signal.score, reasons=signal.reasons,
        entry=round_sig(entry), stop=round_sig(stop), tp1=round_sig(tp1), tp2=round_sig(tp2),
        size_usd=round(size, 2), leverage=leverage, margin=round(size / leverage, 2),
        risk_usd=round(size * stop_pct, 2), atr=a,
    )
