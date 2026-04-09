"""Vercel serverless Flask API for CCTV Analytics."""
import os
import sys
import json
import base64
from flask import Flask, request, jsonify

# Add api directory to path for _lib imports
sys.path.insert(0, os.path.dirname(__file__))

from _lib import db, analyzer, whatsapp

app = Flask(__name__)


# ─── Cameras ────────────────────────────────────────────────

@app.route("/api/cameras", methods=["GET"])
def list_cameras():
    active = request.args.get("active", "false").lower() == "true"
    return jsonify(db.get_cameras(active_only=active))


@app.route("/api/cameras", methods=["POST"])
def add_camera():
    data = request.json or {}
    name = data.get("name", "").strip()
    snapshot_url = data.get("snapshot_url", "").strip()
    business_type = data.get("business_type", "retail")
    location = data.get("location", "").strip()
    wa_alert = data.get("whatsapp_alert", True)
    zone_type = data.get("zone_type", "general")
    hours_open = data.get("hours_open", "09:00")
    hours_close = data.get("hours_close", "22:00")
    after_hours_mode = data.get("after_hours_mode", "critical")

    if not name:
        return jsonify({"error": "Camera name is required"}), 400
    if business_type not in ("retail", "wholesale", "restaurant"):
        return jsonify({"error": "Invalid business type"}), 400

    if snapshot_url:
        b64, err = analyzer.fetch_snapshot(snapshot_url)
        if err:
            return jsonify({"error": f"Cannot fetch snapshot: {err}", "connectable": False}), 400

    cid = db.add_camera(
        name, snapshot_url, business_type, location, wa_alert,
        zone_type=zone_type, hours_open=hours_open, hours_close=hours_close,
        after_hours_mode=after_hours_mode,
    )
    return jsonify({"id": cid, "message": "Camera added"}), 201


@app.route("/api/cameras/<camera_id>", methods=["PUT"])
def update_camera(camera_id):
    data = request.json or {}
    allowed = {"name", "snapshot_url", "business_type", "location", "is_active",
               "whatsapp_alert", "zone_type", "hours_open", "hours_close", "after_hours_mode"}
    updates = {}
    for k, v in data.items():
        if k in allowed:
            if k in ("is_active", "whatsapp_alert"):
                updates[k] = "1" if v else "0"
            else:
                updates[k] = str(v)
    if updates:
        db.update_camera(camera_id, **updates)
    return jsonify({"message": "Updated"})


@app.route("/api/cameras/<camera_id>", methods=["DELETE"])
def delete_camera(camera_id):
    db.deactivate_camera(camera_id)
    return jsonify({"message": "Camera deactivated"})


@app.route("/api/cameras/<camera_id>/test", methods=["POST"])
def test_camera(camera_id):
    cam = db.get_camera(camera_id)
    if not cam:
        return jsonify({"error": "Camera not found"}), 404
    if not cam["snapshot_url"]:
        return jsonify({"online": False, "message": "No snapshot URL configured"})
    b64, err = analyzer.fetch_snapshot(cam["snapshot_url"])
    return jsonify({"online": b64 is not None, "message": err or "OK"})


# ─── Analysis ───────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST"])
def analyze_upload():
    """Analyze an uploaded image or image URL."""
    camera_id = None
    business_type = "retail"
    image_url = ""
    b64_image = None

    if request.content_type and "multipart" in request.content_type:
        file = request.files.get("image")
        camera_id = request.form.get("camera_id")
        business_type = request.form.get("business_type", "retail")
        if file:
            b64_image = base64.b64encode(file.read()).decode("utf-8")
    else:
        data = request.json or {}
        camera_id = data.get("camera_id")
        business_type = data.get("business_type", "retail")
        image_url = data.get("image_url", "")
        b64_image = data.get("image_base64")

    if camera_id:
        cam = db.get_camera(camera_id)
        if cam:
            business_type = cam["business_type"]

    # Get image
    if b64_image:
        result = analyzer.analyze_image_base64(b64_image, business_type)
    elif image_url:
        result = analyzer.analyze_image_url(image_url, business_type)
    else:
        return jsonify({"error": "Provide image file, image_url, or image_base64"}), 400

    if "error" in result and result.get("customer_service_score", 0) == 0:
        return jsonify({"error": result["error"]}), 500

    # Save analysis
    analysis_id = None
    if camera_id:
        analysis_id = db.add_analysis(camera_id, result, image_url)
        _create_alerts(result, analysis_id, camera_id, image_url)

    result["analysis_id"] = analysis_id
    return jsonify(result)


@app.route("/api/analyze/camera/<camera_id>", methods=["POST"])
def analyze_camera(camera_id):
    """Fetch snapshot from camera and analyze it."""
    cam = db.get_camera(camera_id)
    if not cam:
        return jsonify({"error": "Camera not found"}), 404
    if not cam["snapshot_url"]:
        return jsonify({"error": "No snapshot URL configured"}), 400

    force = request.args.get("force", "false").lower() == "true"
    b64, err = analyzer.fetch_snapshot(cam["snapshot_url"])
    if not b64:
        return jsonify({"error": f"Cannot fetch snapshot: {err}"}), 502

    if not force and not analyzer.has_scene_changed_redis(camera_id, b64):
        return jsonify({"skipped": True, "reason": "Scene unchanged"})

    after_hours = not analyzer.is_in_business_hours(cam)
    result = analyzer.analyze_image_base64(
        b64, cam["business_type"],
        zone_type=cam.get("zone_type", "general"),
        after_hours=after_hours,
    )
    analysis_id = db.add_analysis(camera_id, result, cam["snapshot_url"])
    _create_alerts(result, analysis_id, camera_id, cam["snapshot_url"], after_hours=after_hours)

    result["analysis_id"] = analysis_id
    result["after_hours"] = after_hours
    return jsonify(result)


# ─── Cron (Vercel scheduled) ───────────────────────────────

@app.route("/api/cron/analyze", methods=["GET"])
def cron_analyze():
    """Vercel cron: analyze all active cameras with smart skipping.

    Skips cameras when:
    - Outside business hours AND after_hours_mode is 'off'
    - Scene unchanged (motion detection)

    After-hours mode is stricter: any motion = critical alert.
    """
    cameras = db.get_cameras(active_only=True)
    results = []
    skipped_unchanged = 0
    skipped_closed = 0
    for cam in cameras:
        if not cam["snapshot_url"]:
            continue
        try:
            after_hours = not analyzer.is_in_business_hours(cam)
            mode = cam.get("after_hours_mode", "critical")
            if after_hours and mode == "off":
                skipped_closed += 1
                results.append({"camera": cam["name"], "skipped": True, "reason": "closed"})
                continue

            b64, err = analyzer.fetch_snapshot(cam["snapshot_url"])
            if not b64:
                results.append({"camera": cam["name"], "error": err})
                continue

            if not analyzer.has_scene_changed_redis(cam["id"], b64):
                skipped_unchanged += 1
                results.append({"camera": cam["name"], "skipped": True, "reason": "no_change"})
                continue

            result = analyzer.analyze_image_base64(
                b64, cam["business_type"],
                zone_type=cam.get("zone_type", "general"),
                after_hours=after_hours,
            )
            analysis_id = db.add_analysis(cam["id"], result, cam["snapshot_url"])
            alerts = _create_alerts(result, analysis_id, cam["id"], cam["snapshot_url"], after_hours=after_hours)
            results.append({
                "camera": cam["name"],
                "service_score": result.get("customer_service_score"),
                "theft_risk": result.get("theft_risk_score"),
                "alerts_created": len(alerts),
                "model": result.get("model_used", "unknown"),
                "escalated": result.get("escalated", False),
                "after_hours": after_hours,
            })
        except Exception as e:
            results.append({"camera": cam["name"], "error": str(e)})
    return jsonify({
        "analyzed": len(results) - skipped_unchanged - skipped_closed,
        "skipped_unchanged": skipped_unchanged,
        "skipped_closed": skipped_closed,
        "total_cameras": len(results),
        "results": results,
    })


@app.route("/api/cron/digest", methods=["GET"])
def cron_digest():
    """Vercel cron: send daily morning digest at 8am Jakarta."""
    result = whatsapp.send_digest()
    return jsonify(result)


# ─── Alerts ─────────────────────────────────────────────────

@app.route("/api/alerts", methods=["GET"])
def list_alerts():
    severity = request.args.get("severity")
    camera_id = request.args.get("camera_id")
    category = request.args.get("category")
    unread = request.args.get("unread", "false").lower() == "true"
    limit = request.args.get("limit", 100, type=int)
    return jsonify(db.get_alerts(severity, camera_id, category, unread, limit))


@app.route("/api/alerts/<alert_id>/read", methods=["PUT"])
def read_alert(alert_id):
    db.mark_alert_read(alert_id)
    return jsonify({"message": "Marked as read"})


# ─── Analytics ──────────────────────────────────────────────

@app.route("/api/analytics/summary", methods=["GET"])
def dashboard_summary():
    return jsonify(db.get_dashboard_stats())


@app.route("/api/analytics/scores", methods=["GET"])
def score_trends():
    camera_id = request.args.get("camera_id")
    business_type = request.args.get("business_type")
    limit = request.args.get("limit", 200, type=int)
    return jsonify(db.get_score_trends(camera_id, business_type, limit))


@app.route("/api/analytics/recent", methods=["GET"])
def recent_analyses():
    camera_id = request.args.get("camera_id")
    limit = request.args.get("limit", 20, type=int)
    return jsonify(db.get_recent_analyses(camera_id, limit))


# ─── Cost Estimate ─────────────────────────────────────────

@app.route("/api/analytics/cost", methods=["GET"])
def cost_estimate():
    """Estimate API cost based on recent usage patterns."""
    stats = db.get_dashboard_stats()
    total = stats.get("total_analyses", 0)
    # Count escalated analyses from recent data
    recent = db.get_recent_analyses(limit=50)
    escalated = sum(1 for a in recent if a.get("escalated"))
    mini_count = len(recent) - escalated

    # Cost per analysis (approximate)
    cost_mini = 0.0013   # gpt-4o-mini with low detail
    cost_full = 0.022    # gpt-4o with high detail

    est_per_analysis = cost_mini  # most are mini
    if len(recent) > 0:
        escalation_rate = escalated / len(recent)
        est_per_analysis = cost_mini * (1 - escalation_rate) + cost_full * escalation_rate
    else:
        escalation_rate = 0.05  # default 5% assumption

    return jsonify({
        "total_analyses": total,
        "recent_sample": len(recent),
        "escalation_rate": round(escalation_rate * 100, 1),
        "cost_per_analysis": round(est_per_analysis, 4),
        "estimated_monthly_30day": round(est_per_analysis * total, 2) if total > 0 else 0,
        "pricing": {
            "gpt4o_mini_per_call": cost_mini,
            "gpt4o_per_call": cost_full,
            "savings_vs_gpt4o_only": "94%",
            "motion_filter_savings": "~80% fewer API calls",
        },
    })


# ─── WhatsApp ──────────────────────────────────────────────

@app.route("/api/whatsapp/status", methods=["GET"])
def whatsapp_status():
    return jsonify(whatsapp.get_config_status())


@app.route("/api/whatsapp/test", methods=["POST"])
def whatsapp_test():
    ok, msg = whatsapp.send_alert(
        "Test Camera", "low", "system",
        "Test notification from CCTV Analytics",
        "If you receive this, WhatsApp alerts are working correctly."
    )
    return jsonify({"success": ok, "message": msg})


# ─── Recipients (WhatsApp targets) ─────────────────────────

@app.route("/api/recipients", methods=["GET"])
def list_recipients():
    return jsonify(db.get_recipients())


@app.route("/api/recipients", methods=["POST"])
def add_recipient():
    data = request.json or {}
    phone = data.get("phone", "").strip()
    name = data.get("name", "").strip()
    role = data.get("role", "manager")
    digest = data.get("digest", True)
    alerts = data.get("alerts", True)
    if not phone or not name:
        return jsonify({"error": "Phone and name required"}), 400
    rid = db.add_recipient(phone, name, role, digest, alerts)
    return jsonify({"id": rid, "message": "Recipient added"}), 201


@app.route("/api/recipients/<rid>", methods=["DELETE"])
def delete_recipient(rid):
    db.delete_recipient(rid)
    return jsonify({"message": "Removed"})


# ─── Shifts (leaderboard) ──────────────────────────────────

@app.route("/api/analytics/shifts", methods=["GET"])
def shift_leaderboard():
    days = request.args.get("days", 7, type=int)
    return jsonify(db.get_shift_leaderboard(days))


# ─── Digest (preview & manual send) ────────────────────────

@app.route("/api/digest/preview", methods=["GET"])
def digest_preview():
    from _lib import digest as digest_mod
    stats = db.get_yesterday_stats()
    base_url = os.environ.get("BASE_URL", "")
    text = digest_mod.compose_digest(stats, base_url)
    return jsonify({"stats": stats, "message": text})


@app.route("/api/digest/send", methods=["POST"])
def digest_send():
    return jsonify(whatsapp.send_digest())


# ─── Alert feedback (manual + WhatsApp webhook) ────────────

@app.route("/api/alerts/<alert_id>/feedback", methods=["PUT"])
def alert_feedback(alert_id):
    data = request.json or {}
    feedback = data.get("feedback", "").strip().lower()
    if feedback not in ("false_positive", "confirmed", "investigating"):
        return jsonify({"error": "Invalid feedback type"}), 400
    if not db.update_alert_feedback(alert_id, feedback):
        return jsonify({"error": "Alert not found"}), 404
    return jsonify({"message": "Feedback recorded"})


@app.route("/api/whatsapp/webhook", methods=["POST"])
def whatsapp_webhook():
    """Receive incoming WhatsApp messages from Fonnte and parse feedback.

    Fonnte webhook posts JSON like:
    {"device": "...", "sender": "62...", "message": "FALSE", "member": null, ...}
    """
    data = request.json or request.form.to_dict()
    sender = data.get("sender", "")
    message = data.get("message", "")
    feedback = whatsapp.parse_feedback_reply(message)

    if not feedback:
        return jsonify({"ok": True, "action": "ignored", "reason": "no feedback intent"})

    # Find the most recent unread alert for this sender's recipient
    # In production, you'd map sender phone to recipient and find their pending alerts
    recent_alerts = db.get_alerts(unread_only=True, limit=10)
    if not recent_alerts:
        return jsonify({"ok": True, "action": "no_pending_alerts"})

    # Mark the most recent unread alert with this feedback
    target = recent_alerts[0]
    db.update_alert_feedback(target["id"], feedback)

    return jsonify({
        "ok": True,
        "action": "feedback_recorded",
        "alert_id": target["id"],
        "feedback": feedback,
        "sender": sender,
    })


# ─── Cron router (handles ?cron= from vercel.json rewrite) ─

@app.before_request
def handle_cron_param():
    if request.path in ("/api/index", "/api/app"):
        c = request.args.get("cron")
        if c == "analyze":
            return cron_analyze()
        if c == "digest":
            return cron_digest()


# ─── Helpers ────────────────────────────────────────────────

def _create_alerts(result, analysis_id, camera_id, image_url="", after_hours=False):
    """Create alerts from analysis results and send WhatsApp notifications."""
    alerts = []
    theft_score = result.get("theft_risk_score", 0)
    alert_level = result.get("alert_level", "none")
    service_score = result.get("customer_service_score", 5)
    fraud_indicators = result.get("fraud_indicators", [])
    people_count = result.get("people_count", 0)

    # AFTER HOURS: any people = critical alert
    if after_hours and people_count > 0:
        if not db.is_duplicate_alert(camera_id, "theft"):
            aid = db.add_alert(
                analysis_id, camera_id, "critical", "theft",
                f"⚠️ AFTER-HOURS: {people_count} person(s) detected",
                result.get("summary", "Motion detected outside business hours"), image_url,
            )
            alerts.append(aid)
            _notify_whatsapp(camera_id, "critical", "theft",
                           f"AFTER-HOURS: {people_count} person(s) detected",
                           result.get("summary", ""), image_url, aid)
        return alerts

    # Theft alert
    if theft_score >= 7 or alert_level == "critical":
        severity = "critical" if theft_score >= 9 else "high"
        if not db.is_duplicate_alert(camera_id, "theft"):
            aid = db.add_alert(
                analysis_id, camera_id, severity, "theft",
                f"High theft risk detected (score: {theft_score}/10)",
                result.get("theft_description", ""), image_url,
            )
            alerts.append(aid)
            _notify_whatsapp(camera_id, severity, "theft",
                           f"High theft risk (score: {theft_score}/10)",
                           result.get("theft_description", ""), image_url, aid)

    elif theft_score >= 4 or alert_level == "high":
        if not db.is_duplicate_alert(camera_id, "theft"):
            aid = db.add_alert(
                analysis_id, camera_id, "medium", "theft",
                f"Elevated theft risk (score: {theft_score}/10)",
                result.get("theft_description", ""), image_url,
            )
            alerts.append(aid)

    # Fraud
    if len(fraud_indicators) >= 2:
        severity = "high" if len(fraud_indicators) >= 3 else "medium"
        if not db.is_duplicate_alert(camera_id, "fraud"):
            aid = db.add_alert(
                analysis_id, camera_id, severity, "fraud",
                f"{len(fraud_indicators)} fraud indicators",
                "; ".join(fraud_indicators), image_url,
            )
            alerts.append(aid)
            if severity == "high":
                _notify_whatsapp(camera_id, severity, "fraud",
                               f"{len(fraud_indicators)} fraud indicators",
                               "; ".join(fraud_indicators), image_url, aid)

    # Poor service
    if service_score <= 3:
        if not db.is_duplicate_alert(camera_id, "service"):
            staff = result.get("staff_behavior", {})
            desc = "; ".join(f"{k}: {v}" for k, v in staff.items() if v)
            aid = db.add_alert(
                analysis_id, camera_id, "medium", "service",
                f"Poor customer service (score: {service_score}/10)",
                desc, image_url,
            )
            alerts.append(aid)

    return alerts


def _notify_whatsapp(camera_id, severity, category, title, description, image_url, alert_id=""):
    """Send WhatsApp notification if enabled for this camera."""
    cam = db.get_camera(camera_id)
    if cam and cam.get("whatsapp_alert"):
        whatsapp.send_alert(cam["name"], severity, category, title, description, image_url, alert_id)
