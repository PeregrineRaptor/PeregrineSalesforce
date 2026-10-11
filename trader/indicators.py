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


@dataclass
class Fib:
    """A 15m impulse leg. `start` is where the move began, `end` its extreme (high for longs, low for shorts)."""
    side: str
    start: float
    end: float

    def level(self, ratio):
        """Retracement level: 0 = end of the move, 1 = start. Negative ratios are extensions past the end."""
        return self.end - ratio * (self.end - self.start)


def fib_swing(candles, side, min_move_pct=3.0, recent=24):
    """Most recent impulse leg in `candles` for `side`, or None if there isn't a clean one.

    Long: the highest high of the window must be within the last `recent` candles, and the leg runs from
    the lowest low before it up to it. Short is the mirror image.
    """
    if len(candles) < recent + 2:
        return None
    if side == "long":
        end_idx = max(range(len(candles)), key=lambda i: candles[i].h)
        if end_idx == 0:
            return None
        start_idx = min(range(end_idx), key=lambda i: candles[i].l)
        start, end = candles[start_idx].l, candles[end_idx].h
    else:
        end_idx = min(range(len(candles)), key=lambda i: candles[i].l)
        if end_idx == 0:
            return None
        start_idx = max(range(end_idx), key=lambda i: candles[i].h)
        start, end = candles[start_idx].h, candles[end_idx].l
    if end_idx < len(candles) - recent or start <= 0:
        return None
    if abs(end / start - 1) * 100 < min_move_pct:
        return None
    return Fib(side, start, end)


def correlation(a, b, periods=48):
    """Correlation of candle-to-candle returns of two series, matched by open time."""
    closes_b = {c.t: c.c for c in b}
    pairs = [(c.c, closes_b[c.t]) for c in a if c.t in closes_b][-(periods + 1):]
    if len(pairs) < 10:
        return None
    ra = [y[0] / x[0] - 1 for x, y in zip(pairs, pairs[1:])]
    rb = [y[1] / x[1] - 1 for x, y in zip(pairs, pairs[1:])]
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    return cov / (va * vb) ** 0.5 if va > 0 and vb > 0 else None
