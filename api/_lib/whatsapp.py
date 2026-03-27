"""WhatsApp alert notifications via Fonnte API or generic webhook."""
import os
import requests


FONNTE_TOKEN = os.environ.get("FONNTE_TOKEN", "")
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "")
WHATSAPP_WEBHOOK_URL = os.environ.get("WHATSAPP_WEBHOOK_URL", "")


def send_alert(camera_name, severity, category, title, description="", image_url=""):
    """Send a WhatsApp alert notification. Returns (success, message)."""
    if not WHATSAPP_NUMBER:
        return False, "WHATSAPP_NUMBER not configured"

    emoji = {"critical": "🚨", "high": "⚠️", "medium": "📋", "low": "ℹ️"}.get(severity, "📋")
    text = f"""{emoji} *CCTV ALERT — {severity.upper()}*

📷 Camera: {camera_name}
🏷️ Category: {category}
📌 {title}
{f'📝 {description}' if description else ''}
{f'🖼️ {image_url}' if image_url else ''}

— CCTV Analytics"""

    # Try Fonnte API first (popular Indonesian WhatsApp gateway)
    if FONNTE_TOKEN:
        return _send_fonnte(text, image_url)

    # Try generic webhook
    if WHATSAPP_WEBHOOK_URL:
        return _send_webhook(text, image_url)

    return False, "No WhatsApp gateway configured (set FONNTE_TOKEN or WHATSAPP_WEBHOOK_URL)"


def _send_fonnte(text, image_url=""):
    """Send via Fonnte.com API."""
    try:
        payload = {
            "target": WHATSAPP_NUMBER,
            "message": text,
            "countryCode": "62",
        }
        if image_url:
            payload["url"] = image_url
        resp = requests.post(
            "https://api.fonnte.com/send",
            headers={"Authorization": FONNTE_TOKEN},
            json=payload,
            timeout=10,
        )
        result = resp.json()
        if result.get("status"):
            return True, "Sent via Fonnte"
        return False, result.get("reason", "Fonnte error")
    except Exception as e:
        return False, f"Fonnte error: {e}"


def _send_webhook(text, image_url=""):
    """Send via generic webhook (e.g. n8n, Make, Zapier)."""
    try:
        payload = {
            "phone": WHATSAPP_NUMBER,
            "message": text,
        }
        if image_url:
            payload["image_url"] = image_url
        resp = requests.post(WHATSAPP_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code < 300:
            return True, "Sent via webhook"
        return False, f"Webhook returned {resp.status_code}"
    except Exception as e:
        return False, f"Webhook error: {e}"


def get_config_status():
    """Return WhatsApp configuration status."""
    return {
        "number": WHATSAPP_NUMBER[:6] + "****" if WHATSAPP_NUMBER else "",
        "fonnte_configured": bool(FONNTE_TOKEN),
        "webhook_configured": bool(WHATSAPP_WEBHOOK_URL),
        "enabled": bool(WHATSAPP_NUMBER and (FONNTE_TOKEN or WHATSAPP_WEBHOOK_URL)),
    }
