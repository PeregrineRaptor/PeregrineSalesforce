"""Replay the bot's exact rules over the last ~50 days of Hyperliquid 15m candles.

    python backtest.py              baseline + variants (no BTC gate, ATR only, fib only)
    python backtest.py --quick      baseline only

Only price signals exist in history (no news, open interest or top-trader data), so the backtest uses
[backtest] min_score (breakout + BTC trend) instead of the live alert threshold.
"""
import argparse
import bisect
import copy
import json
import logging
import tempfile
import time
from pathlib import Path

from alerts import usd
from config import HERE, load_config
from engine import open_positions
from hyperliquid import Candle, Hyperliquid, Market
from indicators import aggregate, breakout
from paper import Paper
from scanner import M15_CANDLES, btc_context, evaluate

log = logging.getLogger("backtest")


# ---- data -------------------------------------------------------------------------
def load_history(hl, coins, days, cache_dir, now, pause=5.0):
    """{coin: [Candle]} of closed 15m candles, cached on disk for 6h. Pauses between downloads to respect
    Hyperliquid's rate limit (each big candle request is heavy)."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for i, coin in enumerate(coins):
        path = cache_dir / f"{coin}_15m.json"
        if path.exists() and now - path.stat().st_mtime < 6 * 3600:
            out[coin] = [Candle(*row) for row in json.loads(path.read_text())]
            continue
        if i:
            time.sleep(pause)
        log.info("downloading %s (%d/%d)", coin, i + 1, len(coins))
        try:
            candles = hl.closed_candles(coin, "15m", days * 96, now)
        except Exception as e:
            log.warning("skipping %s: %s", coin, e)
            continue
        path.write_text(json.dumps([[c.t, c.close_time, c.o, c.h, c.l, c.c, c.v] for c in candles]))
        out[coin] = candles
    return out


def candle_path(c):
    """Assumed intra-candle price path: green candles dip first then rally, red candles the reverse."""
    return (c.o, c.l, c.h, c.c) if c.c >= c.o else (c.o, c.h, c.l, c.c)


# ---- simulation -------------------------------------------------------------------
def simulate(cfg, history, max_leverage=None, journal_path=None):
    """Run the rules over `history` (must include BTC). Returns the Paper account with every closed trade."""
    max_leverage = max_leverage or {}
    btc15 = history["BTC"]
    btc4h = aggregate(btc15, 16)
    btc4h_close = [c.close_time for c in btc4h]
    index = {coin: {c.t: i for i, c in enumerate(cs)} for coin, cs in history.items()}
    tmp = None
    if journal_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        journal_path = tmp.name
    paper = Paper({}, cfg, journal_path, keep_closed=True)
    cooldowns = {}
    sig_cfg = cfg["signals"]
    sides = ["long", "short"] if cfg["scan"]["allow_shorts"] else ["long"]
    min_score = cfg["backtest"]["min_score"]
    last_prices = {}

    for i in range(M15_CANDLES, len(btc15)):
        t = btc15[i].t
        now = (btc15[i].close_time + 1) / 1000

        # 1) Positions react to this candle (orders placed earlier can fill on it).
        bars = {coin: history[coin][index[coin][t]] for coin in history if t in index[coin]}
        for step, frac in enumerate((0.0, 0.33, 0.66, 1.0)):
            prices = {coin: candle_path(c)[step] for coin, c in bars.items()}
            last_prices.update(prices)
            paper.update(prices, now - 900 * (1 - frac))

        # 2) New signals from the candle that just closed.
        if paper.halted(now):
            continue
        j4 = bisect.bisect_left(btc4h_close, btc15[i].close_time + 1)
        btc = btc_context(btc15[i - M15_CANDLES + 1:i + 1], btc4h[max(0, j4 - 21):j4], btc15[i].c, cfg)
        signals = []
        for coin, bar in bars.items():
            j = index[coin][t]
            if j < M15_CANDLES:
                continue
            m15 = history[coin][j - M15_CANDLES + 1:j + 1]
            # Every price-only setup needs a breakout, so skip the full evaluation when there isn't one.
            if not breakout(m15, sig_cfg["breakout_lookback_candles"], sig_cfg["volume_period"],
                            sig_cfg["volume_multiple"]):
                continue
            market = Market(coin, bar.c, m15[-97].c, 0.0, 0.0, 0.0, max_leverage.get(coin, 20))
            signals += evaluate(market, m15, btc, cfg, sides=sides, min_score=min_score)
        signals.sort(key=lambda s: -s.score)
        open_positions(paper, signals, cooldowns, cfg, now)

    # Close whatever is still open at the last price.
    end = (btc15[-1].close_time + 1) / 1000
    for pos in paper.active():
        if pos["status"] == "open":
            paper._close(pos, pos["remaining"], last_prices.get(pos["coin"], pos["entry"]))
            paper._finish(pos, "end of test", end)
        else:
            paper._finish(pos, "entry not filled", end, count_trade=False)
    if tmp:
        Path(tmp.name).unlink(missing_ok=True)
    return paper


# ---- report -----------------------------------------------------------------------
def summarize(paper, start_balance):
    trades = paper.closed
    if not trades:
        return {"trades": 0}
    wins = [p["realized"] for p in trades if p["realized"] > 0]
    losses = [p["realized"] for p in trades if p["realized"] <= 0]
    equity, peak, max_dd = start_balance, start_balance, 0.0
    for p in sorted(trades, key=lambda p: p["closed_at"]):
        equity += p["realized"]
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
    by_coin = {}
    for p in trades:
        by_coin[p["coin"]] = by_coin.get(p["coin"], 0.0) + p["realized"]
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "return_pct": (paper.balance / start_balance - 1) * 100,
        "pnl": paper.balance - start_balance,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float("inf"),
        "max_drawdown_pct": max_dd,
        "longs": sum(p["side"] == "long" for p in trades),
        "shorts": sum(p["side"] == "short" for p in trades),
        "best": max(by_coin.items(), key=lambda kv: kv[1]),
        "worst": min(by_coin.items(), key=lambda kv: kv[1]),
        "exits": _count(p["exit_reason"] for p in trades),
    }


def _count(items):
    out = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def variants(cfg):
    """Baseline plus the variants that show which rules matter."""
    no_btc = copy.deepcopy(cfg)
    no_btc["btc"].update(require_trend=False, max_against_1h_pct=1e9, max_against_4h_pct=1e9)
    no_btc["weights"]["btc_trend"] = 0
    no_btc["backtest"]["min_score"] = cfg["backtest"]["min_score"] - cfg["weights"]["btc_trend"]
    atr_only = copy.deepcopy(cfg)
    atr_only["fib"]["enabled"] = False
    fib_only = copy.deepcopy(cfg)
    fib_only["fib"]["fallback_to_atr"] = False
    return [("Current rules", cfg), ("Without BTC gate", no_btc), ("ATR levels only", atr_only),
            ("Fib setups only", fib_only)]


def format_report(results, days, coins):
    lines = [f"📊 <b>Backtest: last {days} days, {coins} most-traded coins</b>",
             "Price signals only (breakout + BTC gate + fib/ATR levels), fees included.", ""]
    for name, r in results:
        if not r["trades"]:
            lines += [f"<b>{name}</b>: no trades", ""]
            continue
        pf = "∞" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
        lines += [
            f"<b>{name}</b>",
            f"{r['trades']} trades ({r['longs']} long / {r['shorts']} short) · win rate {r['win_rate']:.0f}%",
            f"Return {r['return_pct']:+.1f}% ({usd(r['pnl'], sign=True)}) · max drawdown {r['max_drawdown_pct']:.1f}%",
            f"Avg win {usd(r['avg_win'], sign=True)} · avg loss {usd(r['avg_loss'], sign=True)} · profit factor {pf}",
            f"Best {r['best'][0]} {usd(r['best'][1], sign=True)} · worst {r['worst'][0]} {usd(r['worst'][1], sign=True)}",
            "Exits: " + ", ".join(f"{k} {v}" for k, v in r["exits"].items()),
            "",
        ]
    lines.append("Profit factor above 1.3 with a drawdown you can stomach = worth trading. Below 1 = the rules lose.")
    return "\n".join(lines)


def run(cfg, hl=None, quick=False, data_dir=HERE):
    hl = hl or Hyperliquid()
    bt = cfg["backtest"]
    markets = sorted(hl.markets(), key=lambda m: -m.day_volume)
    coins = [m.coin for m in markets if m.coin != "BTC"][:bt["coins"]]
    max_lev = {m.coin: m.max_leverage for m in markets}
    history = load_history(hl, ["BTC"] + coins, bt["days"], Path(data_dir) / bt["cache_dir"], time.time())
    if "BTC" not in history:
        raise RuntimeError("could not download BTC history")
    results = []
    for i, (name, vcfg) in enumerate(variants(cfg)[:1 if quick else None]):
        journal = Path(data_dir) / "backtest_trades.csv" if i == 0 else None
        if journal and journal.exists():
            journal.unlink()
        paper = simulate(vcfg, history, max_lev, journal)
        results.append((name, summarize(paper, cfg["account"]["starting_balance"])))
    return format_report(results, bt["days"], len(history) - 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--quick", action="store_true", help="baseline only")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    report = run(load_config(args.config), quick=args.quick)
    for tag in ("<b>", "</b>"):
        report = report.replace(tag, "")
    print("\n" + report)
    print("\nEvery trade of the current rules is in backtest_trades.csv")


if __name__ == "__main__":
    main()
