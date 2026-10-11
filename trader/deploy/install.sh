#!/usr/bin/env bash
# One-shot setup on an Ubuntu/Debian VM: Python venv, dependencies, config, and a systemd service
# that starts the bot on boot and restarts it if it crashes.
#   bash deploy/install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"

echo "==> Installing Python"
if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-venv python3-pip
fi
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
python3 -m venv --help >/dev/null 2>&1 || sudo apt-get install -y python3-venv

echo "==> Creating virtualenv and installing dependencies"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --quiet --upgrade pip
"$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

if [ ! -f "$DIR/config.toml" ]; then
  cp "$DIR/config.example.toml" "$DIR/config.toml"
  echo "==> Created config.toml"
fi

if ! grep -qE '^bot_token = "[^"]+"' "$DIR/config.toml"; then
  read -rp "Telegram bot token from @BotFather (leave empty to edit config.toml later): " TOKEN
  if [ -n "$TOKEN" ]; then
    sed -i "s|^bot_token = \"\"|bot_token = \"$TOKEN\"|" "$DIR/config.toml"
    echo "Now open your bot in Telegram and send it any message."
    read -rp "Press Enter once you've sent it a message... " _
    "$DIR/.venv/bin/python" "$DIR/main.py" --find-chat-id || true
    read -rp "Paste the chat_id number shown above: " CHAT
    [ -n "$CHAT" ] && sed -i "s|^chat_id = \"\"|chat_id = \"$CHAT\"|" "$DIR/config.toml"
  fi
fi

if ! grep -qE '^api_key = "[^"]+"' "$DIR/config.toml"; then
  read -rp "Anthropic API key for the AI analyst (optional, Enter to skip): " AKEY
  [ -n "$AKEY" ] && sed -i "s|^api_key = \"\"|api_key = \"$AKEY\"|" "$DIR/config.toml"
fi
chmod 600 "$DIR/config.toml"

echo "==> Installing systemd service 'trader'"
sudo tee /etc/systemd/system/trader.service >/dev/null <<UNIT
[Unit]
Description=Trade alert bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now trader

echo
echo "Done. The bot is running and starts automatically on boot."
echo "  Live logs:   journalctl -u trader -f"
echo "  Restart:     sudo systemctl restart trader   (after editing config.toml)"
echo "  Stop:        sudo systemctl stop trader"
echo "  Backtest:    $DIR/.venv/bin/python $DIR/backtest.py   (or /backtest in Telegram)"
