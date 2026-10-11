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
    method: str


def fib_levels(signal, cfg):
    """Entry on the pullback into the 15m swing, stop beyond the deeper level, targets at the swing
    extreme and its extension. None if the swing is already broken or the reward isn't worth the risk."""
    f, fib = cfg["fib"], signal.fib
    s = 1 if signal.side == "long" else -1
    mark = signal.market.mark
    stop = fib.level(f["stop_level"]) * (1 - s * f["stop_buffer_pct"] / 100)
    if s * (mark - stop) <= 0:
        return None  # price already retraced through the stop level: the swing has failed
    entry = fib.level(f["entry_level"])
    if s * (mark - entry) < 0:
        entry = mark  # already deeper than the entry level, so enter at the current price
    if abs(entry - stop) / entry * 100 < f["min_stop_pct"]:
        stop = entry * (1 - s * f["min_stop_pct"] / 100)
    tp1, tp2 = fib.level(f["tp1_level"]), fib.level(f["tp2_level"])
    if s * (tp1 - entry) < f["min_reward_risk"] * abs(entry - stop):
        return None
    method = (f"15m fib: swing {fmt(fib.start)} → {fmt(fib.end)}, entry at the {f['entry_level']} retracement, "
              f"stop beyond {f['stop_level']}, TP1 swing {'high' if s > 0 else 'low'}, "
              f"TP2 {1 - f['tp2_level']:g} extension")
    return entry, stop, tp1, tp2, method


def atr_levels(signal, cfg):
    """Fallback: limit entry on a small pullback (or the breakout retest), stop and targets in ATRs."""
    p = cfg["plan"]
    s = 1 if signal.side == "long" else -1
    mark, a = signal.market.mark, signal.atr
    entry = mark - s * p["entry_pullback_atr"] * a
    if signal.level is not None and s * (mark - signal.level) > 0:
        entry = max(entry, signal.level) if s > 0 else min(entry, signal.level)
    stop = entry - s * p["stop_atr"] * a
    r = abs(entry - stop)
    method = (f"1h ATR: entry {p['entry_pullback_atr']:g} ATR pullback, stop {p['stop_atr']:g} ATR "
              f"(no clean 15m swing)")
    return entry, stop, entry + s * p["tp1_r"] * r, entry + s * p["tp2_r"] * r, method


def fmt(x):
    return f"{x:.5g}"


def make_plan(signal, balance, free_margin, cfg):
    acct, p = cfg["account"], cfg["plan"]
    if signal.fib is not None:
        levels = fib_levels(signal, cfg)
    elif cfg["fib"]["enabled"] and not cfg["fib"]["fallback_to_atr"]:
        levels = None
    else:
        levels = atr_levels(signal, cfg)
    if levels is None:
        return None
    entry, stop, tp1, tp2, method = levels
    if min(entry, stop, tp1, tp2) <= 0:
        return None

    risk_per_unit = abs(entry - stop)
    stop_pct = risk_per_unit / entry
    if stop_pct * 100 > p["max_stop_pct"]:
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
        risk_usd=round(size * stop_pct, 2), atr=signal.atr, method=method,
    )
