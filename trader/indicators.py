from dataclasses import dataclass


def atr(candles, period=14):
    """Average true range over the last `period` candles."""
    if len(candles) < period + 1:
        return None
    trs = [max(cur.h - cur.l, abs(cur.h - prev.c), abs(cur.l - prev.c))
           for prev, cur in zip(candles, candles[1:])]
    return sum(trs[-period:]) / period


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


@dataclass
class Breakout:
    side: str       # "long" (closed above range high) or "short" (closed below range low)
    level: float    # the range high/low that was broken
    volume_ratio: float


def breakout(candles, lookback=24, volume_period=20, volume_multiple=2.0):
    """Did the last closed candle break the prior `lookback`-candle range on heavy volume?"""
    if len(candles) < max(lookback, volume_period) + 1:
        return None
    last = candles[-1]
    prior = candles[-1 - lookback:-1]
    avg_volume = sum(c.v for c in candles[-1 - volume_period:-1]) / volume_period
    ratio = last.v / avg_volume if avg_volume > 0 else 0.0
    if ratio < volume_multiple:
        return None
    high = max(c.h for c in prior)
    low = min(c.l for c in prior)
    if last.c > high:
        return Breakout("long", high, ratio)
    if last.c < low:
        return Breakout("short", low, ratio)
    return None
