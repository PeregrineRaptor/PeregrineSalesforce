"""Claude reads each trade alert, searches the latest news on the coin and replies with a short verdict.

Runs in a background thread so alerts are never delayed; results are collected from `results`.
"""
import logging
import queue
import threading

log = logging.getLogger(__name__)

SYSTEM = """You are a crypto perpetual-futures analyst reviewing a trade alert for a trader who holds trades \
for about 11 hours. The alert comes from a rules engine; your job is to add judgement it can't.

Use web search to check news on the coin from the last 48 hours (listings, unlocks, hacks, partnerships, \
regulatory news, big wallet moves) and the current market mood. Then reply in exactly this format:

VERDICT: TAKE, CAUTION or AVOID
- Why it's moving (one line)
- The biggest risk to this trade (one line)
- What would invalidate it before the stop (one line)

Keep it under 80 words. Plain text, no markdown headings. If you find nothing relevant in the news, say so \
rather than guessing."""


def build_prompt(pos, btc_summary):
    return "\n".join([
        f"Trade alert: {pos['side'].upper()} {pos['coin']} perp on Hyperliquid, score {pos['score']}.",
        "Reasons from the rules engine:",
        *[f"- {r}" for r in pos["reasons"]],
        f"Plan: limit entry {pos['entry']}, stop {pos['stop']}, TP1 {pos['tp1']}, TP2 {pos['tp2']}, "
        f"{pos['leverage']}x leverage.",
        f"Levels method: {pos.get('method', '')}",
        f"Market: {btc_summary or 'BTC data unavailable'}",
    ])


class Analyst:
    def __init__(self, cfg):
        self.cfg = cfg["analyst"]
        self.results = queue.Queue()
        self.client = None
        if not self.cfg["enabled"]:
            return
        try:
            import anthropic
        except ImportError:
            log.warning("analyst disabled: run `pip install anthropic`")
            return
        if not self.cfg["api_key"]:
            log.info("analyst disabled: no Anthropic API key set")
            return
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=self.cfg["api_key"])

    @property
    def enabled(self):
        return self.client is not None

    def submit(self, pos, btc_summary):
        if self.enabled:
            threading.Thread(target=self._run, args=(dict(pos), btc_summary), daemon=True).start()

    def _run(self, pos, btc_summary):
        text = self.analyze(pos, btc_summary)
        if text:
            self.results.put((pos, text))

    def analyze(self, pos, btc_summary):
        anthropic = self._anthropic
        messages = [{"role": "user", "content": build_prompt(pos, btc_summary)}]
        try:
            for _ in range(4):  # a long web-search turn can pause; continue it a few times
                response = self.client.beta.messages.create(
                    model=self.cfg["model"],
                    max_tokens=8000,
                    system=SYSTEM,
                    messages=messages,
                    tools=[{"type": "web_search_20260209", "name": "web_search",
                            "max_uses": self.cfg["max_searches"]}],
                    output_config={"effort": self.cfg["effort"]},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",   # on a safety decline, the API retries on a fallback model
                )
                if response.stop_reason != "pause_turn":
                    break
                messages.append({"role": "assistant", "content": response.content})
        except anthropic.AuthenticationError:
            log.error("analyst: Anthropic API key rejected")
            return None
        except anthropic.RateLimitError:
            log.warning("analyst: rate limited, skipping this alert")
            return None
        except anthropic.APIStatusError as e:
            log.warning("analyst: API error %s: %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError:
            log.warning("analyst: could not reach the Anthropic API")
            return None
        if response.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text or None
