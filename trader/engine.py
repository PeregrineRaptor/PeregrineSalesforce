"""Turning signals into positions. Shared by the live bot and the backtester so both follow the same limits."""
from planner import make_plan


def open_positions(paper, signals, cooldowns, cfg, now):
    """Plan and paper-open the best signals that fit the account limits. Returns [(signal, position)]."""
    cooldown = cfg["scan"]["cooldown_hours"] * 3600
    for key in [k for k, ts in cooldowns.items() if now - ts > cooldown]:
        del cooldowns[key]
    active_coins = {p["coin"] for p in paper.active()}
    opened = []
    for sig in signals:
        if len(paper.active()) >= cfg["account"]["max_open_positions"]:
            break
        key = f"{sig.market.coin}:{sig.side}"
        if key in cooldowns or sig.market.coin in active_coins:
            continue
        plan = make_plan(sig, paper.balance, paper.free_margin(), cfg)
        if not plan:
            continue
        pos = paper.add(plan, now)
        if sig.source:
            pos["source"] = sig.source
        cooldowns[key] = now
        active_coins.add(plan.coin)
        opened.append((sig, pos))
    return opened
