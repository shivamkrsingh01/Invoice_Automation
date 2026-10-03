# """
# Small, reusable Telegram notifier.

# Used by monitor.py, but has no dependency on the invoice application and can
# be imported by anything:

#     from telegram_notifier import TelegramNotifier
#     TelegramNotifier(token, chat_id).send("hello")

# It can also be run directly for one-off setup tasks:

#     python stability_monitor/telegram_notifier.py find-chat-id   # discover your chat ID
#     python stability_monitor/telegram_notifier.py test           # send a test message

# Credentials come from stability_monitor/.env or real environment variables
# (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). Nothing is hardcoded.

# send() never raises - a monitoring tool must not crash because Telegram (or
# the internet) is briefly unavailable. It returns True/False instead and keeps
# the reason in .last_error, with the bot token scrubbed out of it.
# """
# import os
# import sys
# import time
# from pathlib import Path
# from typing import Optional

# import requests

# DEFAULT_API_BASE = "https://api.telegram.org"
# MAX_MESSAGE_LEN = 4000  # Telegram's hard limit is 4096 characters


# class TelegramNotifier:
#     def __init__(
#         self,
#         token: Optional[str],
#         chat_id: Optional[str],
#         api_base: str = DEFAULT_API_BASE,
#         timeout: int = 15,
#     ):
#         self.token = (token or "").strip()
#         self.chat_id = str(chat_id or "").strip()
#         self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
#         self.timeout = timeout
#         self.last_error: Optional[str] = None

#     @property
#     def configured(self) -> bool:
#         return bool(self.token and self.chat_id)

#     # ------------------------------------------------------------------
#     def _scrub(self, text: str) -> str:
#         """requests puts the full URL - which contains the bot token - into
#         its exception messages. Never let that reach a log or a screen."""
#         return text.replace(self.token, "<token>") if self.token else text

#     def _url(self, method: str) -> str:
#         return f"{self.api_base}/bot{self.token}/{method}"

#     def send(self, text: str, attempts: int = 3) -> bool:
#         """Send a plain-text message. True only if every part was delivered.
#         `attempts` bounds how long a call can block when Telegram is down."""
#         if not self.configured:
#             self.last_error = "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"
#             return False

#         chunks = [text[i:i + MAX_MESSAGE_LEN] for i in range(0, len(text), MAX_MESSAGE_LEN)] or [""]
#         for chunk in chunks:
#             if not self._send_one(chunk, attempts):
#                 return False
#         self.last_error = None
#         return True

#     def _send_one(self, text: str, attempts: int = 3) -> bool:
#         payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
#         for attempt in range(1, attempts + 1):
#             try:
#                 resp = requests.post(self._url("sendMessage"), json=payload, timeout=self.timeout)
#                 if resp.status_code == 200:
#                     return True
#                 if resp.status_code == 429:  # rate limited - Telegram says how long to wait
#                     try:
#                         wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
#                     except Exception:
#                         wait = 5
#                     self.last_error = f"Telegram rate limit, retry after {wait}s"
#                     time.sleep(min(wait, 30))
#                     continue
#                 try:
#                     detail = resp.json().get("description", resp.text[:200])
#                 except Exception:
#                     detail = resp.text[:200]
#                 self.last_error = self._scrub(f"HTTP {resp.status_code}: {detail}")
#                 if resp.status_code in (400, 401, 403, 404):
#                     return False  # wrong token/chat id/blocked bot - retrying will not help
#             except Exception as e:
#                 self.last_error = self._scrub(f"{type(e).__name__}: {e}")
#             if attempt < attempts:
#                 time.sleep(2 * attempt)
#         return False

#     def get_updates(self) -> dict:
#         resp = requests.get(self._url("getUpdates"), timeout=self.timeout)
#         return resp.json()


# # ----------------------------------------------------------------------
# # Config loading for the command-line helpers (monitor.py has its own).
# # ----------------------------------------------------------------------
# def _load_settings() -> dict:
#     from dotenv import dotenv_values

#     file_values = dotenv_values(Path(__file__).resolve().parent / ".env")

#     def get(key, default=None):
#         return os.environ.get(key) or file_values.get(key) or default

#     return {
#         "token": get("TELEGRAM_BOT_TOKEN"),
#         "chat_id": get("TELEGRAM_CHAT_ID"),
#         "api_base": get("TELEGRAM_API_BASE", DEFAULT_API_BASE),
#     }


# def _cli_find_chat_id(n: TelegramNotifier) -> int:
#     if not n.token:
#         print("TELEGRAM_BOT_TOKEN is not set (put it in stability_monitor/.env).")
#         return 1
#     try:
#         data = n.get_updates()
#     except Exception as e:
#         print("Could not reach Telegram:", n._scrub(str(e)))
#         return 1
#     if not data.get("ok"):
#         print("Telegram rejected the request:", data.get("description", data))
#         print("Check that TELEGRAM_BOT_TOKEN is correct.")
#         return 1

#     seen = {}
#     for upd in data.get("result", []):
#         msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
#         chat = msg.get("chat")
#         if chat:
#             seen[chat["id"]] = chat
#     if not seen:
#         print("No messages found for this bot yet.")
#         print("Open Telegram, send any message (e.g. 'hello') to your bot - or add the bot")
#         print("to a group and send a message there - then run this command again.")
#         return 1

#     print("Chats that have contacted this bot:\n")
#     for cid, chat in seen.items():
#         name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username", "")
#         print(f"  TELEGRAM_CHAT_ID={cid}    ({chat.get('type')}: {name})")
#     print("\nCopy the correct line into stability_monitor/.env")
#     return 0


# def _cli_test(n: TelegramNotifier) -> int:
#     if not n.configured:
#         print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set in stability_monitor/.env")
#         return 1
#     ok = n.send("✅ Telegram test message from the Invoice Automation stability monitor.\n\nIf you can read this, alerts will reach you.")
#     if ok:
#         print("Sent. Check Telegram - the message should arrive within a few seconds.")
#         return 0
#     print("FAILED to send:", n.last_error)
#     return 1


# if __name__ == "__main__":
#     commands = {"find-chat-id": _cli_find_chat_id, "test": _cli_test}
#     if len(sys.argv) != 2 or sys.argv[1] not in commands:
#         print("Usage: python stability_monitor/telegram_notifier.py [find-chat-id | test]")
#         sys.exit(2)
#     s = _load_settings()
#     sys.exit(commands[sys.argv[1]](TelegramNotifier(s["token"], s["chat_id"], s["api_base"])))






"""
Small, reusable Telegram notifier.

Used by monitor.py, but has no dependency on the invoice application and can
be imported by anything:

    from telegram_notifier import TelegramNotifier
    TelegramNotifier(token, chat_id).send("hello")

It can also be run directly for one-off setup tasks:

    python stability_monitor/telegram_notifier.py find-chat-id   # discover your chat ID
    python stability_monitor/telegram_notifier.py test           # send a test message

Credentials come from stability_monitor/.env or real environment variables
(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). Nothing is hardcoded.

send() never raises - a monitoring tool must not crash because Telegram (or
the internet) is briefly unavailable. It returns True/False instead and keeps
the reason in .last_error, with the bot token scrubbed out of it.
"""
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests

DEFAULT_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4000  # Telegram's hard limit is 4096 characters


class TelegramNotifier:
    def __init__(
        self,
        token: Optional[str],
        chat_id: Optional[str],
        api_base: str = DEFAULT_API_BASE,
        timeout: int = 15,
    ):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.timeout = timeout
        self.last_error: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    # ------------------------------------------------------------------
    def _scrub(self, text: str) -> str:
        """requests puts the full URL - which contains the bot token - into
        its exception messages. Never let that reach a log or a screen."""
        return text.replace(self.token, "<token>") if self.token else text

    def _url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    def send(self, text: str, attempts: int = 3) -> bool:
        """Send a plain-text message. True only if every part was delivered.
        `attempts` bounds how long a call can block when Telegram is down."""
        if not self.configured:
            self.last_error = "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"
            return False

        chunks = [text[i:i + MAX_MESSAGE_LEN] for i in range(0, len(text), MAX_MESSAGE_LEN)] or [""]
        for chunk in chunks:
            if not self._send_one(chunk, attempts):
                return False
        self.last_error = None
        return True

    def _send_one(self, text: str, attempts: int = 3) -> bool:
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        for attempt in range(1, attempts + 1):
            try:
                resp = requests.post(self._url("sendMessage"), json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    return True
                if resp.status_code == 429:  # rate limited - Telegram says how long to wait
                    try:
                        wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
                    except Exception:
                        wait = 5
                    self.last_error = f"Telegram rate limit, retry after {wait}s"
                    time.sleep(min(wait, 30))
                    continue
                try:
                    detail = resp.json().get("description", resp.text[:200])
                except Exception:
                    detail = resp.text[:200]
                self.last_error = self._scrub(f"HTTP {resp.status_code}: {detail}")
                if resp.status_code in (400, 401, 403, 404):
                    return False  # wrong token/chat id/blocked bot - retrying will not help
            except Exception as e:
                self.last_error = self._scrub(f"{type(e).__name__}: {e}")
            if attempt < attempts:
                time.sleep(2 * attempt)
        return False

    def get_updates(self) -> dict:
        resp = requests.get(self._url("getUpdates"), timeout=self.timeout)
        return resp.json()


# ----------------------------------------------------------------------
# Config loading for the command-line helpers (monitor.py has its own).
# ----------------------------------------------------------------------
def _load_settings() -> dict:
    from dotenv import dotenv_values

    file_values = dotenv_values(Path(__file__).resolve().parent / ".env")

    def get(key, default=None):
        return os.environ.get(key) or file_values.get(key) or default

    return {
        "token": get("TELEGRAM_BOT_TOKEN"),
        "chat_id": get("TELEGRAM_CHAT_ID"),
        "api_base": get("TELEGRAM_API_BASE", DEFAULT_API_BASE),
    }


def _cli_find_chat_id(n: TelegramNotifier) -> int:
    if not n.token:
        print("TELEGRAM_BOT_TOKEN is not set (put it in stability_monitor/.env).")
        return 1
    try:
        data = n.get_updates()
    except Exception as e:
        print("Could not reach Telegram:", n._scrub(str(e)))
        return 1
    if not data.get("ok"):
        print("Telegram rejected the request:", data.get("description", data))
        print("Check that TELEGRAM_BOT_TOKEN is correct.")
        return 1

    seen = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            seen[chat["id"]] = chat
    if not seen:
        print("No messages found for this bot yet.")
        print("Open Telegram, send any message (e.g. 'hello') to your bot - or add the bot")
        print("to a group and send a message there - then run this command again.")
        return 1

    print("Chats that have contacted this bot:\n")
    for cid, chat in seen.items():
        name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username", "")
        print(f"  TELEGRAM_CHAT_ID={cid}    ({chat.get('type')}: {name})")
    print("\nCopy the correct line into stability_monitor/.env")
    return 0


def _cli_test(n: TelegramNotifier) -> int:
    if not n.configured:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set in stability_monitor/.env")
        return 1
    ok = n.send("✅ Telegram test message from the Invoice Automation stability monitor.\n\nIf you can read this, alerts will reach you.")
    if ok:
        print("Sent. Check Telegram - the message should arrive within a few seconds.")
        return 0
    print("FAILED to send:", n.last_error)
    return 1


if __name__ == "__main__":
    commands = {"find-chat-id": _cli_find_chat_id, "test": _cli_test}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print("Usage: python stability_monitor/telegram_notifier.py [find-chat-id | test]")
        sys.exit(2)
    s = _load_settings()
    sys.exit(commands[sys.argv[1]](TelegramNotifier(s["token"], s["chat_id"], s["api_base"])))
