"""Telegram message text. HTML parse mode."""
import math
from html import escape


def price(p):
    """5 significant figures, never scientific notation: 62,345.0 / 3.1234 / 0.000012345."""
    if p == 0:
        return "$0"
    if abs(p) >= 1000:
        return f"${p:,.1f}"
    return f"${p:.{max(0, 4 - math.floor(math.log10(abs(p))))}f}"


def usd(x, sign=False):
    return f"{'+' if sign and x >= 0 else '-' if x < 0 else ''}${abs(x):,.2f}"


def pct(a, b):
    return (b / a - 1) * 100


def trade_alert(pos, balance, cfg):
    p = cfg["plan"]
    long = pos["side"] == "long"
    lev = pos["leverage"]
    head = f"{'🟢 LONG' if long else '🔴 SHORT'} {escape(pos['coin'])}"
    if pos.get("source"):
        head = f"👀 COPY {head}"
    stop_pct, tp1_pct, tp2_pct = (pct(pos["entry"], pos[k]) for k in ("stop", "tp1", "tp2"))
    lines = [
        f"<b>{head}</b> — score {min(pos['score'], 100)}/100",
        "",
        "<b>Why</b>",
        *[f"• {escape(r)}" for r in pos["reasons"]],
        "",
        f"<b>Levels</b> {escape(pos.get('method', ''))}",
        f"<b>Entry</b>  limit {price(pos['entry'])}",
        f"<b>Stop</b>   {price(pos['stop'])} ({stop_pct:+.1f}%)",
        f"<b>TP1</b>    {price(pos['tp1'])} ({tp1_pct:+.1f}%, {abs(tp1_pct) * lev:.0f}% on margin)"
        f" → sell {p['tp1_close_pct']}%, move stop to entry",
        f"<b>TP2</b>    {price(pos['tp2'])} ({tp2_pct:+.1f}%, {abs(tp2_pct) * lev:.0f}% on margin)"
        f" → sell {p['tp2_close_pct']}%",
        f"<b>Rest</b>   trail {price(p['trail_atr'] * pos['atr'])} behind the best price",
        f"<b>Size</b>   {usd(pos['size_usd'])} at {lev}× → {usd(pos['margin'])} margin, risking {usd(pos['risk_usd'])}",
        "",
        f"Cancel the entry if not filled in {p['entry_expiry_hours']}h. "
        f"Close at {p['time_stop_hours']}h if TP1 isn't hit.",
        *([f"<i>{escape(pos['btc_note'])}</i>"] if pos.get("btc_note") else []),
        f"<i>Trade #{pos['id']} · paper balance {usd(balance)}</i>",
    ]
    return "\n".join(lines)


def trade_buttons(pos):
    return [("✅ Took it", f"took:{pos['id']}"), ("❌ Skip", f"skip:{pos['id']}")]


def event_message(kind, pos, info, balance):
    """Follow-up instructions for a trade you said you took."""
    name = f"{escape(pos['coin'])} {pos['side']} (#{pos['id']})"
    if kind == "filled":
        return (f"✅ <b>{name}</b> entry {price(pos['entry'])} reached. Your limit should be filled.\n"
                f"Check your stop is at {price(pos['stop'])} and TP1 at {price(pos['tp1'])}.")
    if kind == "expired":
        return f"⌛ <b>{name}</b> entry {price(pos['entry'])} not filled in time. <b>Cancel your limit order.</b>"
    if kind == "tp1":
        return (f"🎯 <b>{name}</b> TP1 hit at {price(pos['tp1'])}. <b>Sell part of it now and move your stop "
                f"to entry ({price(pos['entry'])}).</b> This trade can no longer lose.\n"
                f"Locked in {usd(info['pnl'], sign=True)}.")
    if kind == "tp2":
        return (f"🎯🎯 <b>{name}</b> TP2 hit at {price(pos['tp2'])}. <b>Sell another part now.</b> "
                f"Let the rest run with a trailing stop at {price(pos['stop'])}, I'll tell you when it's hit.\n"
                f"Locked in {usd(info['pnl'], sign=True)}.")
    if kind == "closed":
        icon = "💰" if pos["realized"] > 0 else "🛑"
        on_margin = pos["realized"] / pos["margin"] * 100 if pos["margin"] else 0
        return (f"{icon} <b>{name}</b> closed: {escape(pos['exit_reason'])} at ~{price(info['price'])}. "
                f"<b>Close anything still open on this trade.</b>\n"
                f"Result {usd(pos['realized'], sign=True)} ({on_margin:+.0f}% on margin). "
                f"Paper balance {usd(balance)}.")
    return None


def btc_warning(pos, btc):
    name = f"{escape(pos['coin'])} {pos['side']} (#{pos['id']})"
    return (f"⚠️ <b>BTC {btc.change_1h_pct:+.1f}% in the last hour</b>, against your {name}.\n"
            f"Consider tightening your stop{' to entry' if pos['tp1_hit'] else ''} or taking profit early. "
            f"Current stop {price(pos['stop'])}.")


def trader_label(t):
    return (f"Top trader #{t['rank']} ({escape(t['name'])}, 30d {usd(t['month_pnl'], sign=True)[:-3]}, "
            f"{t['month_roi_pct']:+.0f}%)")


def move_reason(move):
    verb = {"open": "opened", "add": "added to", "flip": "flipped into"}[move.kind]
    liq = f", liq {price(move.liquidation)}" if move.liquidation else ""
    return (f"{trader_label(move.trader)} {verb} a {move.side.upper()} worth ${move.value_usd:,.0f} "
            f"at {move.leverage:g}x, entry {price(move.entry)}{liq}")


def move_info(move, why_not):
    """A followed trader moved, but the bot won't copy it."""
    return (f"👀 <b>{escape(move.coin)}</b>: {escape(move_reason(move))}\n"
            f"<i>Not copying: {escape(why_not)}.</i>")


def trader_closed(pos, move):
    return (f"👀 <b>{escape(move.coin)}</b>: {escape(trader_label(move.trader))} just <b>closed</b> their "
            f"{move.side}. You copied this (#{pos['id']}). <b>Consider exiting or tightening your stop</b> "
            f"(currently {price(pos['stop'])}).")


def analyst_message(pos, text):
    verdict = text.split("\n", 1)[0].upper()
    icon = "🟢" if "TAKE" in verdict else "🔴" if "AVOID" in verdict else "🟡"
    return f"🧠 {icon} <b>Analyst on {escape(pos['coin'])} {pos['side']} (#{pos['id']})</b>\n{escape(text)}"


def traders_message(smart):
    if not smart.traders:
        return "Not following anyone yet; the top-trader list loads within a minute of starting."
    cons = smart.consensus()
    lines = [f"<b>Following {len(smart.traders)} top Hyperliquid traders</b> (30-day P&amp;L)"]
    for t in smart.traders[:15]:
        lines.append(f"#{t['rank']} {escape(t['name'])}: {usd(t['month_pnl'], sign=True)[:-3]} "
                     f"({t['month_roi_pct']:+.0f}%)")
    crowded = sorted(((c, side, v) for c, sides in cons.items() for side, v in sides.items() if v["count"] >= 2),
                     key=lambda x: -x[2]["usd"])[:8]
    if crowded:
        lines += ["", "<b>Where they agree</b>"]
        lines += [f"{escape(c)} {side}: {v['count']} traders, ${v['usd'] / 1e6:.1f}M" for c, side, v in crowded]
    return "\n".join(lines)


def status_message(paper, prices, now, paused, btc=None):
    lines = [f"<b>Paper balance</b> {usd(paper.balance)} "
             f"(today {usd(paper.day_pnl(now), sign=True)})"]
    if btc:
        lines.append(escape(btc.summary()))
    if paused:
        lines.append("⏸ New alerts are paused. /resume to restart.")
    if paper.halted(now):
        lines.append("⛔ Daily loss limit hit. No new alerts until 00:00 UTC.")
    active = paper.active()
    if not active:
        lines.append("No open trades.")
    for pos in active:
        mark = prices.get(pos["coin"])
        s = 1 if pos["side"] == "long" else -1
        line = f"#{pos['id']} {escape(pos['coin'])} {pos['side']} {pos['status']} entry {price(pos['entry'])} stop {price(pos['stop'])}"
        if pos["status"] == "open" and mark:
            unreal = s * pos["size_usd"] / pos["entry"] * pos["remaining"] * (mark - pos["entry"])
            line += f" now {price(mark)} ({usd(unreal, sign=True)} open)"
        if pos["taken"]:
            line += " ✅"
        lines.append(line)
    return "\n".join(lines)


def stats_message(paper, start):
    st = paper.d["stats"]
    if not st["trades"]:
        return "No closed trades yet. Results build up in journal.csv as trades close."
    return (f"<b>{st['trades']} closed trades</b>\n"
            f"Win rate {st['wins'] / st['trades'] * 100:.0f}% ({st['wins']} wins)\n"
            f"Total P&L {usd(st['pnl'], sign=True)} ({(paper.balance / start - 1) * 100:+.1f}% on the account)\n"
            f"Average per trade {usd(st['pnl'] / st['trades'], sign=True)}")


HELP = """<b>Commands</b>
/status — paper balance, BTC and open trades
/stats — win rate and total P&amp;L
/backtest — test the rules on the last 50 days (takes a few minutes)
/traders — the top traders being followed and where they agree
/scan — run a market scan now
/pause — stop new alerts (open trades still tracked)
/resume — restart alerts

Tap <b>✅ Took it</b> on an alert and I'll message you when to take profit, move your stop, or get out \
— and if the top trader you copied closes, or BTC turns against you."""
