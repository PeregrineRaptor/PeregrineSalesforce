"""Read-only Hyperliquid market data (the exchange behind Phantom perps). No account or key needed."""
import time
from dataclasses import dataclass

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


@dataclass
class Market:
    coin: str
    mark: float
    prev_day: float
    funding: float          # hourly funding rate, e.g. 0.0001 = 0.01%/h
    open_interest: float    # in coins
    day_volume: float       # 24h notional, USD
    max_leverage: int

    @property
    def change_24h_pct(self):
        return (self.mark / self.prev_day - 1) * 100 if self.prev_day else 0.0


@dataclass
class Candle:
    t: int          # open time, ms
    close_time: int
    o: float
    h: float
    l: float
    c: float
    v: float


class Hyperliquid:
    def __init__(self, session=None, timeout=15):
        self.session = session or requests.Session()
        self.timeout = timeout

    def _post(self, body):
        r = self.session.post(INFO_URL, json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def markets(self):
        meta, ctxs = self._post({"type": "metaAndAssetCtxs"})
        out = []
        for asset, ctx in zip(meta["universe"], ctxs):
            if asset.get("isDelisted") or ctx.get("markPx") is None:
                continue
            out.append(Market(
                coin=asset["name"],
                mark=float(ctx["markPx"]),
                prev_day=float(ctx.get("prevDayPx") or 0),
                funding=float(ctx.get("funding") or 0),
                open_interest=float(ctx.get("openInterest") or 0),
                day_volume=float(ctx.get("dayNtlVlm") or 0),
                max_leverage=int(asset.get("maxLeverage", 10)),
            ))
        return out

    def mids(self):
        return {coin: float(px) for coin, px in self._post({"type": "allMids"}).items()}

    def closed_candles(self, coin, interval, count, now=None):
        """The last `count` fully closed candles, oldest first."""
        now_ms = int((now or time.time()) * 1000)
        start = now_ms - INTERVAL_MS[interval] * (count + 2)
        rows = self._post({"type": "candleSnapshot",
                           "req": {"coin": coin, "interval": interval, "startTime": start, "endTime": now_ms}})
        candles = [Candle(t=r["t"], close_time=r["T"], o=float(r["o"]), h=float(r["h"]),
                          l=float(r["l"]), c=float(r["c"]), v=float(r["v"])) for r in rows]
        return [c for c in candles if c.close_time < now_ms][-count:]
