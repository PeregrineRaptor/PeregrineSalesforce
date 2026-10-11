import os
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent

ENV_OVERRIDES = {
    "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
    "TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
    "CRYPTOPANIC_TOKEN": ("news", "cryptopanic_token"),
}


def load_config(path=None):
    """Load the example config as defaults, then the user's config.toml on top, then env vars."""
    with open(HERE / "config.example.toml", "rb") as f:
        cfg = tomllib.load(f)

    path = Path(path) if path else HERE / "config.toml"
    if path.exists():
        with open(path, "rb") as f:
            for section, values in tomllib.load(f).items():
                cfg.setdefault(section, {}).update(values)

    for env, (section, key) in ENV_OVERRIDES.items():
        if os.environ.get(env):
            cfg[section][key] = os.environ[env]

    cfg["telegram"]["chat_id"] = str(cfg["telegram"]["chat_id"]).strip()
    return cfg
