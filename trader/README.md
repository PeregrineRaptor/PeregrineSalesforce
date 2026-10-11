# Trade alert bot

Scans every liquid perp on Hyperliquid, the exchange behind Phantom's perps, every 5 minutes. When a setup
scores 70 or more out of 100, it sends you a Telegram message with an exact plan: limit entry, stop loss,
two take-profits, position size and leverage. You place the trade yourself in Phantom.

Every alert is also **paper-traded** on a $1,000 account. Tap **✅ Took it** and the bot messages you
when the entry fills, when to take profit and move your stop, and when to get out.
`journal.csv` records every trade, wins and losses, so after about 50 trades you can see if the rules really work.

The bot never touches your wallet. It only reads public market data.

## Setup (about 10 minutes)

1. **Install Python 3.11 or newer** from python.org. On Windows, tick "Add Python to PATH".
2. **Install the one dependency.** In a terminal, inside this `trader` folder:
   ```
   pip install -r requirements.txt
   ```
3. **Create your Telegram bot**
   - In Telegram, message **@BotFather**, send `/newbot`, and pick a name.
   - It replies with a token like `123456:ABC-...`.
   - Copy `config.example.toml` to `config.toml` and paste the token into `bot_token`.
4. **Get your chat id**
   - Open your new bot in Telegram, press Start, and send it any message.
   - Run:
     ```
     python main.py --find-chat-id
     ```
   - Paste the number it prints into `chat_id` in `config.toml`.
5. **Run it:**
   ```
   python main.py
   ```
   You should get "🤖 Trade alert bot started" in Telegram.

`config.toml` holds your token and is never committed to git.

## Keeping it running

The bot has to stay on to catch setups.
- **Your computer:** leave the terminal open and stop the computer from sleeping.
- **A $5/month server (recommended):** for example DigitalOcean or Hetzner, Ubuntu. Run:
  ```
  nohup python3 main.py > bot.log 2>&1 &
  ```
  or set it up as a systemd service.

If the bot restarts it picks up where it left off, because everything is saved in `state.json`.

The open-interest signal needs about 4 hours of the bot running before it can score, because it compares
open interest now with 4 hours ago.

## Telegram commands

| Command | What it does |
|---|---|
| `/status` | Paper balance, today's P&L, open trades |
| `/stats` | Closed trades, win rate, total P&L |
| `/scan` | Scan right now |
| `/pause` / `/resume` | Stop or restart new alerts; open trades are still tracked |

## How a setup is scored

| Signal | Points |
|---|---|
| 1h candle closes above the 24h high (or below the low for shorts) on 2× or more average volume | 30 |
| Open interest up 10%+ in 4h while funding is at or below 0.01%/h (squeeze fuel). For shorts: funding at or above 0.03%/h | 25 |
| Catalyst in the last 2h: a Binance or Upbit listing (delisting for shorts), or a strongly voted CryptoPanic headline | 25 |
| BTC 4h trend in the same direction | 20 |

**Never alerts when:**
- Funding is above 0.05%/h for longs, or below −0.05%/h for shorts.
- The coin has already moved 30% or more in 24h.
- 24h volume is under $5M.
- The stop would need to be wider than 10%.

## How a trade is planned

| Item | Rule |
|---|---|
| Entry | Limit order 0.5 ATR better than the current price, or at the retest of the breakout level. Cancelled after 4h if not filled. |
| Stop | 1.5 ATR beyond the entry |
| Size | The stop loses exactly 2% of the account ($20 on $1,000) |
| Leverage | 5× by default, never more than 10×, and always low enough that liquidation is well beyond the stop |
| TP1 | 2× the stop distance: sell 50% and move the stop to entry |
| TP2 | 4× the stop distance: sell 30% |
| Remainder | Trails 1 ATR behind the best price |
| Time limits | Closed at 24h if TP1 isn't hit; 72h maximum hold |
| Account limits | Max 3 trades at once. After a 6% loss in a day, no new alerts until 00:00 UTC |

Every number above is in `config.toml` and can be changed there.

## Optional: more news

Get a free API key at cryptopanic.com/developers/api and put it in `cryptopanic_token`. That adds
headline catalysts on top of the exchange listing watchers.

## Tests

```
python -m unittest discover -s tests
```

## Limits to know about

- The paper account checks prices every 60 seconds and fills exits at the price it sees, so fast wicks
  can differ from what you get in real trading.
- The Binance and Upbit announcement feeds are unofficial. If one changes format the bot logs a warning
  and keeps running without it.
- Token unlocks aren't checked yet.
- This is a rules engine, not a guarantee. Run it on paper before trading real money on its alerts.
