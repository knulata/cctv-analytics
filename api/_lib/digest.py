"""Daily morning digest WhatsApp message composer (Indonesian)."""
from datetime import datetime, timezone, timedelta


SHIFT_NAMES = {
    "pagi": "Pagi",
    "sore": "Sore",
    "malam": "Malam",
}


def compose_digest(stats, base_url=""):
    """Compose Indonesian morning digest message from yesterday's stats."""
    if not stats or stats["total_analyses"] == 0:
        return f"""☕ Pagi! Laporan CCTV Kemarin

ℹ️ Belum ada data analisis kemarin.

Pastikan kamera aktif dan terhubung di:
{base_url or 'dashboard'}"""

    date_str = stats.get("date", "")
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        date_id = d.strftime("%d %b %Y")
    except (ValueError, TypeError):
        date_id = date_str

    # Service score with trend arrow
    svc = stats.get("avg_service", 0)
    svc_emoji = "🟢" if svc >= 7 else "🟡" if svc >= 5 else "🔴"

    # Risk score
    risk = stats.get("avg_theft_risk", 0)
    risk_emoji = "✅" if risk < 3 else "⚠️" if risk < 6 else "🚨"

    lines = [
        f"☕ *Pagi! Laporan CCTV {date_id}*",
        "",
        f"📊 Total analisis: *{stats['total_analyses']}*",
    ]

    crit = stats.get("critical_alerts", 0)
    high = stats.get("high_alerts", 0)
    if crit > 0:
        lines.append(f"🚨 Alert kritis: *{crit}* (perlu review!)")
    if high > 0:
        lines.append(f"⚠️ Alert tinggi: *{high}*")
    if crit == 0 and high == 0:
        lines.append("✅ Tidak ada alert serius")

    lines += [
        "",
        f"{svc_emoji} Skor layanan rata-rata: *{svc}/10*",
        f"{risk_emoji} Skor risiko: *{risk}/10*",
    ]

    # Shift performance
    shift_avgs = stats.get("shift_avgs", {})
    if shift_avgs:
        lines += ["", "*👥 Performa per shift:*"]
        for s, score in sorted(shift_avgs.items(), key=lambda x: x[1], reverse=True):
            name = SHIFT_NAMES.get(s, s)
            emoji = "🥇" if score == max(shift_avgs.values()) else "📊"
            lines.append(f"{emoji} Shift {name}: {score}/10")

    best = stats.get("best_shift")
    worst = stats.get("worst_shift")
    if best and worst and best != worst:
        lines += [
            "",
            f"🏆 Shift terbaik: *{SHIFT_NAMES[best]}*",
            f"📉 Perlu perhatian: *{SHIFT_NAMES[worst]}*",
        ]

    # Best/worst camera
    best_cam = stats.get("best_camera")
    worst_cam = stats.get("worst_camera")
    if best_cam:
        lines += ["", f"⭐ Kamera terbaik: *{best_cam['name']}* ({best_cam['avg_service']}/10)"]
    if worst_cam and worst_cam.get("name") != (best_cam or {}).get("name"):
        lines.append(f"📍 Perlu coaching: *{worst_cam['name']}* ({worst_cam['avg_service']}/10)")

    if base_url:
        lines += ["", f"📱 Dashboard: {base_url}"]

    lines += ["", "_— CCTV Analytics_"]
    return "\n".join(lines)


def compose_alert_message(camera_name, severity, category, title, description, alert_id="", base_url=""):
    """Compose a real-time WhatsApp alert message in Indonesian."""
    severity_emoji = {
        "critical": "🚨",
        "high": "⚠️",
        "medium": "📋",
        "low": "ℹ️",
    }.get(severity, "📋")

    severity_id = {
        "critical": "KRITIS",
        "high": "TINGGI",
        "medium": "SEDANG",
        "low": "RENDAH",
    }.get(severity, severity.upper())

    category_id = {
        "theft": "Pencurian",
        "fraud": "Penipuan",
        "service": "Layanan",
        "system": "Sistem",
    }.get(category, category)

    lines = [
        f"{severity_emoji} *ALERT {severity_id}*",
        "",
        f"📷 Kamera: *{camera_name}*",
        f"🏷️ Kategori: {category_id}",
        f"📌 {title}",
    ]
    if description:
        lines += ["", f"📝 {description}"]

    if alert_id and base_url:
        lines += ["", f"🔗 Detail: {base_url}/alerts"]

    if severity in ("critical", "high"):
        lines += [
            "",
            "_Balas pesan ini:_",
            "✅ *OK* — alert benar, sudah ditangani",
            "❌ *FALSE* — alarm palsu",
            "🔍 *INVESTIGATE* — sedang diselidiki",
        ]

    lines += ["", "_— CCTV Analytics_"]
    return "\n".join(lines)
