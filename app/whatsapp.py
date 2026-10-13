import base64
import json
import re
import urllib.parse
import urllib.request

from app.audit import file_logger
from app.config import settings


def wa_log():
    return file_logger("whatsapp", settings.LOG_DIR / "whatsapp.log")


def normalize_phone(phone: str | None) -> str | None:
    """Digits with country code (e.g. 919876543210), or None if it does not look like a phone number."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = settings.DEFAULT_COUNTRY_CODE + digits
    return digits if 11 <= len(digits) <= 15 else None


def _post(url: str, data: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def _send_twilio(phone: str, message: str) -> None:
    sender = settings.TWILIO_WHATSAPP_FROM
    if not sender.startswith("whatsapp:"):
        sender = f"whatsapp:{sender}"
    auth = base64.b64encode(f"{settings.TWILIO_ACCOUNT_SID}:{settings.TWILIO_AUTH_TOKEN}".encode()).decode()
    _post(f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json",
          urllib.parse.urlencode({"From": sender, "To": f"whatsapp:+{phone}", "Body": message}).encode(),
          {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})


def _send_meta(phone: str, message: str) -> None:
    payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": message}}
    _post(f"https://graph.facebook.com/v21.0/{settings.META_WA_PHONE_ID}/messages", json.dumps(payload).encode(),
          {"Authorization": f"Bearer {settings.META_WA_TOKEN}", "Content-Type": "application/json"})


SENDERS = {"twilio": _send_twilio, "meta": _send_meta}


def send_whatsapp(phones: list[str | None], message: str) -> None:
    numbers = [n for n in dict.fromkeys(normalize_phone(p) for p in phones) if n]
    sender = SENDERS.get(settings.WHATSAPP_PROVIDER)
    for number in numbers:
        if not sender:
            wa_log().info(f"[not sent: WhatsApp not configured] to=+{number}\n{message}\n")
            continue
        try:
            sender(number, message)
            wa_log().info(f"[sent] to=+{number} | {message.splitlines()[0]}")
        except Exception as exc:
            wa_log().error(f"[failed] to=+{number} | {message.splitlines()[0]} | {exc}")
