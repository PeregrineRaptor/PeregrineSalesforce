"""Scores every liquid Hyperliquid perp for long and short setups."""
import logging
import time
from dataclasses import dataclass, field

from hyperliquid import Market
from indicators import atr, breakout, correlation, fib_swing, sma

log = logging.getLogger(__name__)


@dataclass
class Signal:
    market: Market
    side: str
    score: int
    reasons: list
    atr: float
    level: float = None   # broken range high/low, if this is a breakout
    catalysts: list = field(default_factory=list)
    fib: object = None    # indicators.Fib from the 15m chart, if there is a clean swing
    btc_corr: float = None


@dataclass
class BtcContext:
    price: float
    trend_up: bool        # 4h close above its moving average
    change_1h_pct: float
    change_4h_pct: float

    def summary(self):
        return (f"BTC {self.price:,.0f} · 4h trend {'up' if self.trend_up else 'down'} · "
                f"{self.change_1h_pct:+.1f}% 1h · {self.change_4h_pct:+.1f}% 4h")


def btc_gate(side, btc, cfg):
    """Reason BTC rules out this side right now, or None if BTC allows it."""
    b = cfg["btc"]
    if btc is None:
        return "BTC data unavailable" if b["require_trend"] else None
    long = side == "long"
    if b["require_trend"] and btc.trend_up != long:
        return f"BTC 4h trend is {'down' if long else 'up'}"
    s = 1 if long else -1
    if s * btc.change_1h_pct <= -b["max_against_1h_pct"]:
        return f"BTC {btc.change_1h_pct:+.1f}% in the last hour"
    if s * btc.change_4h_pct <= -b["max_against_4h_pct"]:
        return f"BTC {btc.change_4h_pct:+.1f}% in the last 4 hours"
    return None


def base_ticker(coin):
    """Hyperliquid lists some memecoins per 1000 units, e.g. kPEPE -> PEPE."""
    if coin.startswith("k") and coin[1:].isupper():
        return coin[1:]
    return coin


def score_side(side, market, brk, oi_change_pct, catalysts, btc_up, cfg):
    """Returns (score, reasons, block_reason). Pure function so the rules are easy to test."""
    sig, w = cfg["signals"], cfg["weights"]
    long = side == "long"
    move = market.change_24h_pct

    if long and market.funding > sig["block_long_funding"]:
        return 0, [], "funding too high, longs crowded"
    if not long and market.funding < sig["block_short_funding"]:
        return 0, [], "funding too negative, shorts crowded"
    if (move if long else -move) > sig["max_24h_move_pct"]:
        return 0, [], f"already moved {move:+.0f}% in 24h"

    score, reasons = 0, []
    if brk and brk.side == side:
        score += w["breakout"]
        edge = "above 24h high" if long else "below 24h low"
        reasons.append(f"1h close {edge} ({brk.level:.6g}) on {brk.volume_ratio:.1f}x volume")

    if oi_change_pct is not None and oi_change_pct >= sig["oi_rise_pct"]:
        funding_txt = f"funding {market.funding * 100:.4f}%/h"
        if long and market.funding <= sig["long_funding_max"]:
            score += w["open_interest"]
            reasons.append(f"Open interest +{oi_change_pct:.0f}% in {sig['oi_lookback_hours']}h, "
                           f"{funding_txt}: shorts piling in, squeeze fuel")
        elif not long and market.funding >= sig["short_funding_min"]:
            score += w["open_interest"]
            reasons.append(f"Open interest +{oi_change_pct:.0f}% in {sig['oi_lookback_hours']}h, "
                           f"{funding_txt}: longs crowded")

    matching = [c for c in catalysts if c.side == side]
    if matching:
        score += w["catalyst"]
        reasons.append(matching[0].text)

    if btc_up is not None and btc_up == long:
        score += w["btc_trend"]
        reasons.append(f"BTC 4h trend {'up' if long else 'down'}")

    return score, reasons, None


class Scanner:
    def __init__(self, cfg, hl, news, state):
        self.cfg = cfg
        self.hl = hl
        self.news = news
        self.oi_history = state.setdefault("oi_history", {})
        self._hourly_cache = {}
        self._m15_cache = {}
        self.btc = None

    def _record_oi(self, markets, now):
        keep = (self.cfg["signals"]["oi_lookback_hours"] + 2) * 3600
        for m in markets:
            hist = self.oi_history.setdefault(m.coin, [])
            hist.append([now, m.open_interest])
            self.oi_history[m.coin] = [p for p in hist if now - p[0] <= keep]

    def oi_change_pct(self, coin, now):
        cutoff = now - self.cfg["signals"]["oi_lookback_hours"] * 3600
        older = [oi for ts, oi in self.oi_history.get(coin, []) if ts <= cutoff]
        current = self.oi_history.get(coin, [])
        if not older or not current or older[-1] <= 0:
            return None
        return (current[-1][1] / older[-1] - 1) * 100

    def _hourly(self, coin, now):
        """1h candles only change once an hour, so fetch each coin at most once per hour."""
        hour = int(now // 3600)
        cached = self._hourly_cache.get(coin)
        if cached and cached[0] == hour:
            return cached[1]
        sig, plan = self.cfg["signals"], self.cfg["plan"]
        count = max(sig["breakout_lookback_hours"], sig["volume_period"], plan["atr_period"],
                    self.cfg["btc"]["correlation_hours"]) + 2
        candles = self.hl.closed_candles(coin, "1h", count, now)
        self._hourly_cache[coin] = (hour, candles)
        return candles

    def _quarter_hourly(self, coin, now):
        """15m candles, refetched at most once per 15 minutes."""
        slot = int(now // 900)
        cached = self._m15_cache.get(coin)
        if cached and cached[0] == slot:
            return cached[1]
        candles = self.hl.closed_candles(coin, "15m", self.cfg["fib"]["window_candles"], now)
        self._m15_cache[coin] = (slot, candles)
        return candles

    def btc_context(self, markets, now):
        try:
            btc = next(m for m in markets if m.coin == "BTC")
            period = self.cfg["btc"]["sma_period"]
            closes = [c.c for c in self.hl.closed_candles("BTC", "4h", period + 1, now)]
            avg = sma(closes, period)
            m15 = self.hl.closed_candles("BTC", "15m", 16, now)
            if avg is None or len(m15) < 16:
                return None
            return BtcContext(
                price=btc.mark,
                trend_up=closes[-1] > avg,
                change_1h_pct=(btc.mark / m15[-4].o - 1) * 100,
                change_4h_pct=(btc.mark / m15[-16].o - 1) * 100,
            )
        except Exception as e:
            log.warning("BTC context unavailable: %s", e)
            return None

    def _fib(self, coin, side, now):
        f = self.cfg["fib"]
        if not f["enabled"]:
            return None
        try:
            return fib_swing(self._quarter_hourly(coin, now), side, f["min_move_pct"], f["recent_candles"])
        except Exception as e:
            log.warning("15m candles for %s unavailable: %s", coin, e)
            return None

    def _btc_corr(self, coin_candles, now):
        if not coin_candles:
            return None
        try:
            return correlation(coin_candles, self._hourly("BTC", now), self.cfg["btc"]["correlation_hours"])
        except Exception:
            return None

    def run(self, now=None):
        """Returns (signals sorted best first, markets)."""
        now = now or time.time()
        sig, scan = self.cfg["signals"], self.cfg["scan"]
        markets = self.hl.markets()
        liquid = [m for m in markets if m.day_volume >= scan["min_day_volume_usd"]]
        self._record_oi(liquid, now)
        self.btc = btc = self.btc_context(markets, now)
        catalysts = self.news.catalysts(now)
        sides = []
        for side in (["long", "short"] if scan["allow_shorts"] else ["long"]):
            blocked = btc_gate(side, btc, self.cfg)
            if blocked:
                log.info("no %ss: %s", side, blocked)
            else:
                sides.append(side)
        if not sides:
            return [], markets

        signals = []
        for m in liquid:
            try:
                candles = self._hourly(m.coin, now)
            except Exception as e:
                log.warning("candles for %s unavailable: %s", m.coin, e)
                continue
            a = atr(candles, self.cfg["plan"]["atr_period"])
            if not a:
                continue
            brk = breakout(candles, sig["breakout_lookback_hours"], sig["volume_period"], sig["volume_multiple"])
            oi = self.oi_change_pct(m.coin, now)
            cats = catalysts.get(m.coin.upper(), []) + catalysts.get(base_ticker(m.coin), [])
            for side in sides:
                btc_up = btc.trend_up if btc else None
                score, reasons, blocked = score_side(side, m, brk, oi, cats, btc_up, self.cfg)
                if blocked or score < scan["min_score"]:
                    continue
                level = brk.level if brk and brk.side == side else None
                corr = self._btc_corr(candles, now) if m.coin != "BTC" else None
                if corr is not None:
                    reasons = reasons + [f"Tracks BTC with correlation {corr:.2f} over "
                                         f"{self.cfg['btc']['correlation_hours']}h"]
                signals.append(Signal(m, side, score, reasons, a, level, cats,
                                      fib=self._fib(m.coin, side, now), btc_corr=corr))
        signals.sort(key=lambda s: -s.score)
        log.info("scanned %d liquid markets, %d signals", len(liquid), len(signals))
        return signals, markets
