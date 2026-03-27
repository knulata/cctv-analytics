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

    if not name:
        return jsonify({"error": "Camera name is required"}), 400
    if business_type not in ("retail", "wholesale", "restaurant"):
        return jsonify({"error": "Invalid business type"}), 400

    # Test snapshot URL if provided
    if snapshot_url:
        b64, err = analyzer.fetch_snapshot(snapshot_url)
        if err:
            return jsonify({"error": f"Cannot fetch snapshot: {err}", "connectable": False}), 400

    cid = db.add_camera(name, snapshot_url, business_type, location, wa_alert)
    return jsonify({"id": cid, "message": "Camera added"}), 201


@app.route("/api/cameras/<camera_id>", methods=["PUT"])
def update_camera(camera_id):
    data = request.json or {}
    allowed = {"name", "snapshot_url", "business_type", "location", "is_active", "whatsapp_alert"}
    updates = {}
    for k, v in data.items():
        if k in allowed:
            if k == "is_active":
                updates[k] = "1" if v else "0"
            elif k == "whatsapp_alert":
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

    b64, err = analyzer.fetch_snapshot(cam["snapshot_url"])
    if not b64:
        return jsonify({"error": f"Cannot fetch snapshot: {err}"}), 502

    result = analyzer.analyze_image_base64(b64, cam["business_type"])
    analysis_id = db.add_analysis(camera_id, result, cam["snapshot_url"])
    _create_alerts(result, analysis_id, camera_id, cam["snapshot_url"])

    result["analysis_id"] = analysis_id
    return jsonify(result)


# ─── Cron (Vercel scheduled) ───────────────────────────────

@app.route("/api/cron/analyze", methods=["GET"])
def cron_analyze():
    """Called by Vercel cron to analyze all active cameras."""
    # Also handle ?cron=analyze from vercel.json rewrite
    cameras = db.get_cameras(active_only=True)
    results = []
    for cam in cameras:
        if not cam["snapshot_url"]:
            continue
        try:
            b64, err = analyzer.fetch_snapshot(cam["snapshot_url"])
            if not b64:
                results.append({"camera": cam["name"], "error": err})
                continue
            result = analyzer.analyze_image_base64(b64, cam["business_type"])
            analysis_id = db.add_analysis(cam["id"], result, cam["snapshot_url"])
            alerts = _create_alerts(result, analysis_id, cam["id"], cam["snapshot_url"])
            results.append({
                "camera": cam["name"],
                "service_score": result.get("customer_service_score"),
                "theft_risk": result.get("theft_risk_score"),
                "alerts_created": len(alerts),
            })
        except Exception as e:
            results.append({"camera": cam["name"], "error": str(e)})
    return jsonify({"analyzed": len(results), "results": results})


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


# ─── Cron router (handles ?cron= from vercel.json rewrite) ─

@app.before_request
def handle_cron_param():
    if request.args.get("cron") == "analyze" and request.path in ("/api/index", "/api/app"):
        return cron_analyze()


# ─── Helpers ────────────────────────────────────────────────

def _create_alerts(result, analysis_id, camera_id, image_url=""):
    """Create alerts from analysis results and send WhatsApp notifications."""
    alerts = []
    theft_score = result.get("theft_risk_score", 0)
    alert_level = result.get("alert_level", "none")
    service_score = result.get("customer_service_score", 5)
    fraud_indicators = result.get("fraud_indicators", [])

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
                           result.get("theft_description", ""), image_url)

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
                               "; ".join(fraud_indicators), image_url)

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


def _notify_whatsapp(camera_id, severity, category, title, description, image_url):
    """Send WhatsApp notification if enabled for this camera."""
    cam = db.get_camera(camera_id)
    if cam and cam.get("whatsapp_alert"):
        whatsapp.send_alert(cam["name"], severity, category, title, description, image_url)
