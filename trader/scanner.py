"""Scores every liquid Hyperliquid perp for long and short setups on 15m candles.

`evaluate()` is pure (candles in, signals out), so the live bot and the backtester run identical rules.
"""
import logging
import time
from dataclasses import dataclass, field

from hyperliquid import Market
from indicators import aggregate, atr, breakout, correlation, fib_swing, sma

log = logging.getLogger(__name__)
M15_CANDLES = 200  # ~50h: 24h breakout range + 48h of hourly data for ATR and BTC correlation


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
    source: dict = None   # set when the signal comes from a followed trader's move


@dataclass
class BtcContext:
    price: float
    trend_up: bool        # 4h close above its moving average
    change_1h_pct: float
    change_4h_pct: float
    hourly: list = field(default_factory=list, repr=False)

    def summary(self):
        return (f"BTC {self.price:,.0f} · 4h trend {'up' if self.trend_up else 'down'} · "
                f"{self.change_1h_pct:+.1f}% 1h · {self.change_4h_pct:+.1f}% 4h")


def btc_context(btc_15m, btc_4h, price, cfg):
    avg = sma([c.c for c in btc_4h], cfg["btc"]["sma_period"])
    if avg is None or len(btc_15m) < 16:
        return None
    return BtcContext(price=price, trend_up=btc_4h[-1].c > avg,
                      change_1h_pct=(price / btc_15m[-4].o - 1) * 100,
                      change_4h_pct=(price / btc_15m[-16].o - 1) * 100,
                      hourly=aggregate(btc_15m, 4))


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


def score_side(side, market, brk, oi_change_pct, catalysts, btc_up, cfg, smart=None):
    """Returns (score, reasons, block_reason). `smart` is {"count": n, "usd": total} for this side, or None."""
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
        reasons.append(f"15m close {edge} ({brk.level:.6g}) on {brk.volume_ratio:.1f}x volume")

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

    if smart and smart["count"] >= cfg["smart_money"]["consensus_traders"]:
        score += w["smart_money"]
        reasons.append(f"{smart['count']} followed top traders are {side} (${smart['usd'] / 1e6:.1f}M)")

    if btc_up is not None and btc_up == long:
        score += w["btc_trend"]
        reasons.append(f"BTC 4h trend {'up' if long else 'down'}")

    return score, reasons, None


def evaluate(market, m15, btc, cfg, oi_change_pct=None, catalysts=(), smart=None, sides=("long", "short"),
             min_score=None):
    """All signals for one coin from its closed 15m candles (oldest first)."""
    sig, plan = cfg["signals"], cfg["plan"]
    hourly = aggregate(m15, 4)
    a = atr(hourly, plan["atr_period"])
    if not a:
        return []
    brk = breakout(m15, sig["breakout_lookback_candles"], sig["volume_period"], sig["volume_multiple"])
    min_score = cfg["scan"]["min_score"] if min_score is None else min_score
    out = []
    for side in sides:
        if btc_gate(side, btc, cfg):
            continue
        score, reasons, blocked = score_side(side, market, brk, oi_change_pct, list(catalysts),
                                             btc.trend_up if btc else None, cfg, (smart or {}).get(side))
        if blocked or score < min_score:
            continue
        out.append(enrich(Signal(market, side, score, reasons, a,
                                 level=brk.level if brk and brk.side == side else None,
                                 catalysts=list(catalysts)), m15, hourly, btc, cfg))
    return out


def enrich(signal, m15, hourly, btc, cfg):
    """Attach the 15m fib swing and BTC correlation to a signal."""
    f = cfg["fib"]
    if f["enabled"]:
        signal.fib = fib_swing(m15[-f["window_candles"]:], signal.side, f["min_move_pct"], f["recent_candles"])
    if btc and signal.market.coin != "BTC":
        signal.btc_corr = correlation(hourly, btc.hourly, cfg["btc"]["correlation_hours"])
        if signal.btc_corr is not None:
            signal.reasons = signal.reasons + [
                f"Tracks BTC with correlation {signal.btc_corr:.2f} over {cfg['btc']['correlation_hours']}h"]
    return signal


class Scanner:
    def __init__(self, cfg, hl, news, state, smart_money=None):
        self.cfg = cfg
        self.hl = hl
        self.news = news
        self.smart_money = smart_money
        self.oi_history = state.setdefault("oi_history", {})
        self._m15 = {}          # coin -> (slot, candles)
        self._btc4h = (None, [])
        self.btc = None
        self.markets = {}

    # ---- data ------------------------------------------------------------------
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

    def candles(self, coin, now, budget=None):
        """Closed 15m candles, refetched once per new 15m candle. `budget` is a one-item list counting fetches
        left this scan; once it runs out, cached (slightly stale) candles are used until the next scan."""
        slot = int(now // 900)
        cached = self._m15.get(coin)
        if cached and cached[0] == slot:
            return cached[1]
        if budget is not None:
            if budget[0] <= 0:
                return cached[1] if cached else []
            budget[0] -= 1
        candles = self.hl.closed_candles(coin, "15m", M15_CANDLES, now)
        self._m15[coin] = (slot, candles)
        return candles

    def _btc(self, now):
        try:
            slot = int(now // 900)
            if self._btc4h[0] != slot:
                self._btc4h = (slot, self.hl.closed_candles("BTC", "4h", self.cfg["btc"]["sma_period"] + 1, now))
            btc = self.markets.get("BTC")
            return btc_context(self.candles("BTC", now), self._btc4h[1], btc.mark, self.cfg) if btc else None
        except Exception as e:
            log.warning("BTC context unavailable: %s", e)
            return None

    # ---- scan ------------------------------------------------------------------
    def run(self, now=None):
        """Returns (signals sorted best first, markets)."""
        now = now or time.time()
        scan = self.cfg["scan"]
        markets = self.hl.markets()
        self.markets = {m.coin: m for m in markets}
        liquid = sorted((m for m in markets if m.day_volume >= scan["min_day_volume_usd"]),
                        key=lambda m: -m.day_volume)[:scan["max_coins"]]
        self._record_oi(liquid, now)
        self.btc = btc = self._btc(now)
        sides = [s for s in (["long", "short"] if scan["allow_shorts"] else ["long"])
                 if not btc_gate(s, btc, self.cfg)]
        if not sides:
            log.info("BTC blocks all trades: %s", btc_gate("long", btc, self.cfg))
            return [], markets

        catalysts = self.news.catalysts(now)
        smart = self.smart_money.consensus() if self.smart_money else {}
        budget = [scan["max_candle_fetches_per_scan"]]
        signals = []
        for m in liquid:
            try:
                m15 = self.candles(m.coin, now, budget)
            except Exception as e:
                log.warning("candles for %s unavailable: %s", m.coin, e)
                continue
            if not m15:
                continue
            cats = catalysts.get(m.coin.upper(), []) + catalysts.get(base_ticker(m.coin), [])
            signals += evaluate(m, m15, btc, self.cfg, self.oi_change_pct(m.coin, now), cats,
                                smart.get(m.coin), sides)
        signals.sort(key=lambda s: -s.score)
        log.info("scanned %d markets (%d candle fetches), %d signals", len(liquid),
                 scan["max_candle_fetches_per_scan"] - budget[0], len(signals))
        return signals, markets
