"""
notify.py
=========
Multi-channel alerting for the kiln monitor. Sends a message to every
channel you've configured, so you get redundancy (e.g. Telegram + SMS).

Configuration comes from environment variables (so tokens stay out of
git and out of the code). Set whichever channels you want; unset ones are
silently skipped. See notify.env.example.

Channels:
  - Telegram  (free, easy, supports a group for multiple people)
  - ntfy      (free, self-hostable, great for push to phones)
  - Twilio    (real SMS — most reliable "wake me up", costs per message)

All sends are best-effort and never raise into the caller: a failing
notification must not crash the monitor. Failures are logged and the
other channels still fire.

Quick test:
    python notify.py "test message from the kiln box"
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("notify")

HTTP_TIMEOUT = 10


def _post(url: str, data: bytes, headers: dict | None = None,
          auth_header: str | None = None) -> bool:
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if auth_header:
        req.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        log.warning(f"notify POST to {url.split('//')[-1].split('/')[0]} failed: {e}")
        return False


# --- channels --------------------------------------------------------------

def _send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    return _post(url, data,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})


def _send_ntfy(text: str, title: str, priority: str) -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    base = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    url = f"{base}/{topic}"
    headers = {"Title": title, "Priority": priority}
    token = os.environ.get("NTFY_TOKEN")  # for protected/self-hosted topics
    auth = f"Bearer {token}" if token else None
    return _post(url, text.encode("utf-8"), headers=headers, auth_header=auth)


def _send_twilio(text: str) -> bool:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_FROM")            # your Twilio number, +1...
    to = os.environ.get("TWILIO_TO")               # comma-separated recipients
    if not all((sid, token, frm, to)):
        return False
    import base64
    ok_all = True
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    cred = base64.b64encode(f"{sid}:{token}".encode()).decode()
    for recipient in [r.strip() for r in to.split(",") if r.strip()]:
        data = urllib.parse.urlencode({
            "From": frm, "To": recipient, "Body": text[:1500],
        }).encode()
        ok = _post(url, data,
                   headers={"Content-Type": "application/x-www-form-urlencoded"},
                   auth_header=f"Basic {cred}")
        ok_all = ok_all and ok
    return ok_all


# --- public API ------------------------------------------------------------

def send(text: str, *, title: str = "Kiln alert", priority: str = "high") -> dict:
    """Fire `text` to every configured channel. Returns {channel: bool}.

    priority is used by ntfy: one of min, low, default, high, urgent.
    """
    results = {}
    # Telegram
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        results["telegram"] = _send_telegram(f"{title}\n{text}")
    # ntfy
    if os.environ.get("NTFY_TOPIC"):
        results["ntfy"] = _send_ntfy(text, title, priority)
    # Twilio SMS
    if os.environ.get("TWILIO_ACCOUNT_SID"):
        results["sms"] = _send_twilio(f"{title}: {text}")

    if not results:
        log.warning("notify.send called but no channels configured")
    else:
        log.info(f"notify results: {results}")
    return results


def channels_configured() -> list[str]:
    out = []
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        out.append("telegram")
    if os.environ.get("NTFY_TOPIC"):
        out.append("ntfy")
    if os.environ.get("TWILIO_ACCOUNT_SID"):
        out.append("sms")
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    msg = " ".join(sys.argv[1:]) or "test message from the kiln box"
    configured = channels_configured()
    if not configured:
        print("No channels configured. Set env vars — see notify.env.example.")
        sys.exit(1)
    print(f"Sending to: {', '.join(configured)}")
    print(send(msg, title="Kiln test", priority="default"))
