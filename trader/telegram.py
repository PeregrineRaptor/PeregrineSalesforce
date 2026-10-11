"""Minimal Telegram Bot API client. Without a token it prints messages to the console instead."""
import logging

import requests

log = logging.getLogger(__name__)


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token, chat_id, session=None):
        self.token = token
        self.chat_id = str(chat_id)
        self.session = session or requests.Session()

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    def call(self, method, http_timeout=35, **params):
        r = self.session.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params,
                              timeout=http_timeout)
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    def send(self, text, buttons=None, reply_to=None):
        """Send an HTML message; `buttons` is [(label, callback_data), ...]. Returns the message id."""
        if not self.enabled:
            print("\n" + text + ("\n[" + " | ".join(b[0] for b in buttons) + "]" if buttons else "") + "\n")
            return None
        params = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
        return self.call("sendMessage", **params)["message_id"]

    def clear_buttons(self, message_id):
        if self.enabled and message_id:
            try:
                self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=message_id,
                          reply_markup={"inline_keyboard": []})
            except TelegramError as e:
                log.debug("could not clear buttons: %s", e)

    def answer(self, callback_id, text):
        if self.enabled:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def updates(self, offset, timeout=0):
        if not self.token:
            return []
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", http_timeout=timeout + 10, **params)
