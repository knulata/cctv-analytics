"""WhatsApp alert notifications via Fonnte API or generic webhook."""
import os
import requests


FONNTE_TOKEN = os.environ.get("FONNTE_TOKEN", "")
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "")
WHATSAPP_WEBHOOK_URL = os.environ.get("WHATSAPP_WEBHOOK_URL", "")


def send_message(phone, text, image_url=""):
    """Low-level: send a WhatsApp message to a specific phone number."""
    if not phone:
        return False, "No phone number"

    if FONNTE_TOKEN:
        return _send_fonnte(phone, text, image_url)
    if WHATSAPP_WEBHOOK_URL:
        return _send_webhook(phone, text, image_url)
    return False, "No WhatsApp gateway configured"


def send_alert(camera_name, severity, category, title, description="", image_url="", alert_id=""):
    """Send a real-time alert to all configured alert recipients."""
    from _lib import db, digest as digest_mod

    base_url = os.environ.get("BASE_URL", "")
    text = digest_mod.compose_alert_message(
        camera_name, severity, category, title, description, alert_id, base_url
    )

    # Get recipients who want alerts
    recipients = db.get_recipients(alerts_only=True)

    # Fallback to legacy WHATSAPP_NUMBER if no recipients in DB
    if not recipients and WHATSAPP_NUMBER:
        return send_message(WHATSAPP_NUMBER, text, image_url)

    if not recipients:
        return False, "No alert recipients configured"

    sent = 0
    errors = []
    for r in recipients:
        ok, msg = send_message(r["phone"], text, image_url)
        if ok:
            sent += 1
        else:
            errors.append(f"{r['name']}: {msg}")
    return sent > 0, f"Sent to {sent}/{len(recipients)} recipients" + (f" — errors: {'; '.join(errors)}" if errors else "")


def send_digest():
    """Send the morning digest to all digest subscribers."""
    from _lib import db, digest as digest_mod

    stats = db.get_yesterday_stats()
    base_url = os.environ.get("BASE_URL", "")
    text = digest_mod.compose_digest(stats, base_url)

    recipients = db.get_recipients(digest_only=True)
    if not recipients and WHATSAPP_NUMBER:
        ok, msg = send_message(WHATSAPP_NUMBER, text)
        return {"sent": 1 if ok else 0, "total": 1, "error": msg if not ok else None}

    sent = 0
    errors = []
    for r in recipients:
        ok, msg = send_message(r["phone"], text)
        if ok:
            sent += 1
        else:
            errors.append(f"{r['name']}: {msg}")
    return {"sent": sent, "total": len(recipients), "errors": errors}


def _send_fonnte(phone, text, image_url=""):
    try:
        payload = {"target": phone, "message": text, "countryCode": "62"}
        if image_url:
            payload["url"] = image_url
        resp = requests.post(
            "https://api.fonnte.com/send",
            headers={"Authorization": FONNTE_TOKEN},
            data=payload,
            timeout=10,
        )
        result = resp.json()
        if result.get("status"):
            return True, "Sent via Fonnte"
        return False, result.get("reason", "Fonnte error")
    except Exception as e:
        return False, f"Fonnte error: {e}"


def _send_webhook(phone, text, image_url=""):
    try:
        payload = {"phone": phone, "message": text}
        if image_url:
            payload["image_url"] = image_url
        resp = requests.post(WHATSAPP_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code < 300:
            return True, "Sent via webhook"
        return False, f"Webhook returned {resp.status_code}"
    except Exception as e:
        return False, f"Webhook error: {e}"


def get_config_status():
    from _lib import db
    recipients = db.get_recipients()
    return {
        "number": WHATSAPP_NUMBER[:6] + "****" if WHATSAPP_NUMBER else "",
        "fonnte_configured": bool(FONNTE_TOKEN),
        "webhook_configured": bool(WHATSAPP_WEBHOOK_URL),
        "enabled": bool((WHATSAPP_NUMBER or recipients) and (FONNTE_TOKEN or WHATSAPP_WEBHOOK_URL)),
        "recipients_count": len(recipients),
    }


def parse_feedback_reply(text):
    """Parse a WhatsApp reply to detect feedback intent.
    Returns: 'false_positive', 'confirmed', 'investigating', or None."""
    if not text:
        return None
    t = text.strip().lower()
    # Indonesian + English keywords
    if any(k in t for k in ["false", "salah", "salah alarm", "bukan", "tidak", "❌"]):
        return "false_positive"
    if any(k in t for k in ["ok", "benar", "iya", "ya", "sudah", "ditangani", "✅"]):
        return "confirmed"
    if any(k in t for k in ["investigate", "selidiki", "cek", "periksa", "🔍"]):
        return "investigating"
    return None
