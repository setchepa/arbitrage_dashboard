"""
Telegram notifications.

IMPORTANT — how Telegram actually works: the Bot API cannot send to a phone
number. It sends to a `chat_id`, and the recipient must have messaged the bot
first (anti-spam by design). Telegram's Gateway API *does* target phone numbers
but is restricted to verification codes, so it can't be used for alerts.

Setup (one time):
  1. Message @BotFather -> /newbot -> copy the token
  2. Message your new bot anything (e.g. /start) so it may reply to you
  3. Discover your chat id:   TELEGRAM_BOT_TOKEN=... python notify.py --chat-id
  4. Set both env vars on the service:
       TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Send a test:  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python notify.py --test

Uses only the standard library so it adds no dependency.
"""

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 20


def _token():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id():
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def enabled():
    """True when both secrets are present, so callers can no-op cleanly."""
    return bool(_token() and _chat_id())


def _call(method, payload):
    url = API.format(token=_token(), method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Telegram {method} failed: HTTP {e.code} {body}") from e


def send_message(text, parse_mode="HTML", silent=False):
    """Send `text` to TELEGRAM_CHAT_ID. Returns the API response dict."""
    if not enabled():
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set — cannot send."
        )
    return _call("sendMessage", {
        "chat_id": _chat_id(),
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
        "disable_notification": silent,
    })


def discover_chat_ids():
    """
    Print every chat that has messaged this bot, so you can grab your chat_id.
    Requires TELEGRAM_BOT_TOKEN and that you've already messaged the bot.
    """
    url = API.format(token=_token(), method="getUpdates")
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {data}")
    seen = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            name = chat.get("username") or chat.get("first_name") or chat.get("title")
            seen[chat["id"]] = f"{name} ({chat.get('type')})"
    return seen


if __name__ == "__main__":
    if not _token():
        print("TELEGRAM_BOT_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    if "--chat-id" in sys.argv:
        chats = discover_chat_ids()
        if not chats:
            print("No chats found. Message your bot (e.g. /start), then retry.")
            sys.exit(1)
        print("Chats that have messaged this bot:")
        for cid, who in chats.items():
            print(f"  chat_id={cid}   {who}")
    elif "--test" in sys.argv:
        r = send_message("✅ Arbitrage dashboard: Telegram alerts are wired up.")
        print("sent:", r.get("ok"), "message_id:", r.get("result", {}).get("message_id"))
    else:
        print(__doc__)
