# Trade alert bot

A 24/7 bot for Hyperliquid perps, the exchange behind Phantom perps and copy-trading apps. It sends trade plans to your Telegram:

- **Scans every minute.** Covers the 50 most-traded perps on 15m candles, so breakouts are caught within
  minutes of the candle closing.
- **Follows the top traders.** Tracks the 30 most profitable Hyperliquid accounts and alerts you within
  about a minute when one opens, adds to or flips a position. You get your own entry, stop and targets for
  the trade, plus a heads-up when that trader closes.
- **Checks the news with AI.** Claude searches the latest news on each coin and replies under the alert with
  TAKE / CAUTION / AVOID and the main risk.
- **Backtests the rules on demand.** `/backtest` replays the exact rules over the last 50 days, so you see
  win rate, return and drawdown in minutes instead of waiting weeks.
- **Respects BTC.** No longs while BTC's 4h trend is down or BTC is dumping, and you get warnings on open
  trades when BTC turns.
- **Plans every trade the same way.** Fibonacci entries and stops on 15m swings, a fixed $20 risk per trade
  on $1,000, two take-profits and a trailing stop.
- **Tracks trades you take.** Tap ✅ Took it and the bot tells you when the entry fills, when to take
  profit, when to move your stop, and when to get out.

The bot never touches your wallet. It only reads public market data. Every alert is also paper-traded,
so `/stats` and `journal.csv` show how the live alerts are really doing.

## Install on your VM (Ubuntu/Debian, about 5 minutes)

1. **Create the Telegram bot.** Message **@BotFather** in Telegram, send `/newbot`, and copy the token.
2. **Optional: get an Anthropic API key** at console.anthropic.com for the AI analyst. It costs roughly a
   few cents per alert.
3. **On the VM**, copy this `trader` folder over (or `git clone` the repo), then run:
   ```
   bash trader/deploy/install.sh
   ```
   It asks for the token, has you message the bot to find your chat id, optionally asks for the Anthropic
   key, then starts the bot as a service. The service restarts if it crashes and starts on boot.

You'll get "🤖 Trade alert bot started" in Telegram. Useful commands on the VM:

```
journalctl -u trader -f             # live logs
sudo systemctl restart trader       # after editing config.toml
.venv/bin/python backtest.py        # full backtest in the terminal (from the trader folder)
```

The VM only needs outbound internet access, with no open ports. Any small VM is enough: 1 vCPU and
1 GB RAM.

## Telegram commands

| Command | What it does |
|---|---|
| `/backtest` | Test the current rules on the last 50 days. About 3–5 minutes the first time, seconds after that. |
| `/traders` | The top traders being followed, and which coins several of them are positioned in |
| `/status` | Paper balance, the BTC picture, open trades |
| `/stats` | Win rate and P&L of the live alerts so far |
| `/scan` | Scan right now |
| `/pause` / `/resume` | Stop or restart new alerts; open trades are still tracked |

## The alerts you'll get

| Alert | When |
|---|---|
| 🟢 / 🔴 **LONG / SHORT** | A coin scores 70 or more (see scoring below) |
| 👀 **COPY** | A followed top trader opens, adds to or flips a $50k+ position, and BTC and the rules allow it. It comes with your own fib/ATR levels, not theirs. |
| 👀 *Not copying: …* | A top trader moved, but BTC or the rules say no. You see it anyway, with the reason. |
| 🧠 **Analyst** | Arrives as a reply under each trade alert about a minute later, with TAKE / CAUTION / AVOID |
| ✅ 🎯 💰 🛑 ⌛ | Follow-ups on trades you tapped "Took it" on |
| ⚠️ **BTC** | BTC moves 1.5%+ in an hour against a trade you took |
| 👀 **closed** | The top trader you copied just closed their position |

## How a setup is scored

| Signal | Points |
|---|---|
| A 15m candle closes above the 24h high (below the low for shorts) on 2× or more average volume | 30 |
| Open interest up 10%+ in 4h while funding is at or below 0.01%/h (squeeze fuel). For shorts: funding at or above 0.03%/h | 25 |
| Catalyst in the last 2h: a Binance/Upbit listing (delisting for shorts), or a strongly voted CryptoPanic headline | 25 |
| 2 or more followed top traders positioned the same way | 25 |
| BTC 4h trend in the same direction | 20 |

**BTC gate.** No longs unless BTC's 4h close is above its 20-period average, and no longs while BTC is
down 1.5% in the last hour or 3% over 4 hours. Shorts follow the mirror rule. Without BTC data, no alerts.

**Never alerts when:**
- Funding is above 0.05%/h for longs, or below −0.05%/h for shorts.
- The coin has already moved 30% or more in 24h.
- 24h volume is under $5M.
- The stop would need to be wider than 10%.

## How a trade is planned

**Fibonacci on the 15m chart** is used when there's a clean swing: the latest impulse leg of at least 3%,
with its high in the last 6h.

| Item | Rule |
|---|---|
| Entry | Limit at the **0.618** retracement. If price is already below it but above the stop, enter at the current price. |
| Stop | Just beyond **0.786** (0.2% buffer), never tighter than 1% |
| TP1 | Retest of the swing high: sell 50% and move the stop to entry |
| TP2 | **1.272** extension: sell 30% |
| Remainder | Trails 1 ATR (1h) behind the best price |
| Skipped when | The swing has already failed (price is below 0.786), or TP1 is less than 1.5× the risk away |

**Fallback.** With no clean swing, the bot uses an ATR plan:
- **Entry:** 0.5 ATR pullback.
- **Stop:** 1.5 ATR.
- **Targets:** TP1 at 2R, TP2 at 4R.

**Common to both:**
- **Risk:** 2% of the account per trade ($20 on $1,000).
- **Leverage:** 5× by default, 10× maximum, with liquidation always well beyond the stop.
- **Entries:** unfilled entries are cancelled after 6h.
- **Time limits:** a trade is closed at 24h if TP1 isn't hit, and held 72h at most.
- **Account limits:** max 3 trades at once. After a 6% loss in a day, no new alerts until 00:00 UTC.

Every number is in `config.toml`. After changing one, run `/backtest` to see its effect.

## Reading the backtest

`/backtest` runs the current rules plus three variants, so you can see which rules earn their keep:
- **without the BTC gate**
- **ATR levels only**
- **fib setups only**

It reports trades, win rate, return, max drawdown, average win and loss, profit factor, best and worst coin,
and how trades exited. Every trade of the current rules is saved to `backtest_trades.csv`.

**What the history covers.** Hyperliquid has no history for news, open interest or top-trader positions,
so the backtest only uses price signals: breakout plus BTC trend, at a 50-point threshold. It also assumes
green candles dip before rallying and red candles bounce before falling. Treat it as a test of the
entries, exits and BTC filter, not of every live signal.

**Rule of thumb:** a profit factor above 1.3 with a drawdown you can stomach is worth trading. Below 1.0,
the rules lose money. Change the config and test again.

## Running without the VM installer

```
pip install -r requirements.txt
cp config.example.toml config.toml   # add bot_token, then:
python main.py --find-chat-id        # after messaging your bot; paste chat_id into config.toml
python main.py
```

## Tests

```
python -m unittest discover -s tests
```

## Limits

- The top-trader list comes from Hyperliquid's public leaderboard, filtered to consistently profitable
  accounts that aren't market makers. Past P&L doesn't guarantee their next trade.
- Paper fills use 15-second price checks, so fast wicks can differ from real fills.
- The Binance/Upbit announcement feeds are unofficial. If one changes, the bot logs a warning and carries
  on without it.
- Token unlocks aren't checked.
