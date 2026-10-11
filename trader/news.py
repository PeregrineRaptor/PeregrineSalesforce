"""Catalysts: exchange listings/delistings and voted news headlines, keyed by ticker.

Each source is best effort: if one changes its format or goes down, it is logged and skipped.
"""
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import requests

log = logging.getLogger(__name__)

BINANCE_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
               "?type=1&catalogId=48&pageNo=1&pageSize=20")
UPBIT_URL = "https://api-manager.upbit.com/api/v1/announcements?os=web&page=1&per_page=20&category=trade"
TICKER_RE = re.compile(r"\(([A-Z0-9]{2,12})\)")
HEADERS = {"User-Agent": "Mozilla/5.0 (trader-alerts)"}


@dataclass
class Catalyst:
    coin: str
    side: str     # "long" or "short"
    text: str
    ts: float     # unix seconds


def parse_binance(articles):
    out = []
    for a in articles:
        title = a.get("title", "")
        if re.search(r"will delist", title, re.I):
            side, label = "short", "Binance delisting"
        elif re.search(r"will (list|add)", title, re.I):
            side, label = "long", "Binance listing"
        else:
            continue
        for ticker in TICKER_RE.findall(title):
            out.append(Catalyst(ticker, side, f"{label}: {title}", a["releaseDate"] / 1000))
    return out


def parse_upbit(notices):
    out = []
    for n in notices:
        title = n.get("title", "")
        if "거래지원 종료" in title:
            side, label = "short", "Upbit delisting"
        elif "신규 거래지원" in title or "디지털 자산 추가" in title:
            side, label = "long", "Upbit listing"
        else:
            continue
        ts = datetime.fromisoformat(n["listed_at"]).timestamp()
        for ticker in TICKER_RE.findall(title):
            out.append(Catalyst(ticker, side, f"{label}: {', '.join(TICKER_RE.findall(title))}", ts))
    return out


def parse_cryptopanic(posts, min_net_votes):
    out = []
    for p in posts:
        votes = p.get("votes") or {}
        pos, neg = votes.get("positive", 0) or 0, votes.get("negative", 0) or 0
        net = pos - neg
        if abs(net) < min_net_votes:
            continue
        side = "long" if net > 0 else "short"
        ts = datetime.fromisoformat(p["published_at"]).timestamp()
        codes = [c.get("code") for c in (p.get("currencies") or p.get("instruments") or [])]
        for code in filter(None, codes):
            out.append(Catalyst(code.upper(), side, f"News: {p.get('title', '')} (+{pos}/-{neg} votes)", ts))
    return out


class News:
    def __init__(self, cfg, session=None):
        self.cfg = cfg["news"]
        self.session = session or requests.Session()
        self._items = []
        self._fetched_at = 0.0

    def _get(self, url, **params):
        r = self.session.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()

    def _fetch_all(self):
        items = []
        if self.cfg["binance_listings"]:
            try:
                items += parse_binance(self._get(BINANCE_URL)["data"]["catalogs"][0]["articles"])
            except Exception as e:
                log.warning("Binance announcements unavailable: %s", e)
        if self.cfg["upbit_listings"]:
            try:
                items += parse_upbit(self._get(UPBIT_URL)["data"]["notices"])
            except Exception as e:
                log.warning("Upbit announcements unavailable: %s", e)
        if self.cfg["cryptopanic_token"]:
            try:
                data = self._get(self.cfg["cryptopanic_url"], auth_token=self.cfg["cryptopanic_token"],
                                 public="true", kind="news")
                items += parse_cryptopanic(data.get("results", []), self.cfg["cryptopanic_min_net_votes"])
            except Exception as e:
                log.warning("CryptoPanic unavailable: %s", e)
        return items

    def catalysts(self, now):
        """Recent catalysts as {TICKER: [Catalyst, ...]}."""
        if now - self._fetched_at >= self.cfg["refresh_seconds"]:
            self._items = self._fetch_all()
            self._fetched_at = now
        window = self.cfg["catalyst_window_hours"] * 3600
        out = defaultdict(list)
        for c in self._items:
            if 0 <= now - c.ts <= window:
                out[c.coin].append(c)
        return out
