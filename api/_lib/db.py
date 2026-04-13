"""Upstash Redis storage layer for CCTV Analytics."""
import os
import json
import time
import uuid

# Namespace prefix for all Redis keys — prevents collision when sharing a DB
PREFIX = "cctv:"

_redis = None
_redis_available = True


def get_redis():
    global _redis, _redis_available
    if not _redis_available:
        return None
    if _redis is None:
        url = os.environ.get("UPSTASH_REDIS_REST_URL", "")
        token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
        if not url or not token:
            _redis_available = False
            return None
        try:
            from upstash_redis import Redis
            _redis = Redis(url=url, token=token)
        except Exception:
            _redis_available = False
            return None
    return _redis


def is_configured():
    return get_redis() is not None


# --- Cameras ---

def get_cameras(active_only=False):
    r = get_redis()
    if not r:
        return []
    keys = r.smembers(f"{PREFIX}cameras")
    cameras = []
    for key in keys:
        data = r.hgetall(f"{PREFIX}camera:{key}")
        if data:
            data["id"] = key
            if active_only and data.get("is_active") == "0":
                continue
            cameras.append(_parse_camera(data))
    cameras.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return cameras


def get_camera(camera_id):
    r = get_redis()
    if not r:
        return None
    data = r.hgetall(f"{PREFIX}camera:{camera_id}")
    if not data:
        return None
    data["id"] = camera_id
    return _parse_camera(data)


def add_camera(name, snapshot_url, business_type, location="", whatsapp_alert=True,
               zone_type="general", hours_open="09:00", hours_close="22:00",
               after_hours_mode="critical", timezone="Asia/Jakarta"):
    r = get_redis()
    if not r:
        return None
    camera_id = str(uuid.uuid4())[:8]
    data = {
        "name": name,
        "snapshot_url": snapshot_url,
        "business_type": business_type,
        "location": location,
        "is_active": "1",
        "whatsapp_alert": "1" if whatsapp_alert else "0",
        "zone_type": zone_type,
        "hours_open": hours_open,
        "hours_close": hours_close,
        "after_hours_mode": after_hours_mode,
        "timezone": timezone,
        "last_seen": "",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    r.hset(f"{PREFIX}camera:{camera_id}", values=data)
    r.sadd(f"{PREFIX}cameras", camera_id)
    return camera_id


def update_camera(camera_id, **kwargs):
    r = get_redis()
    if not r:
        return
    updates = {k: str(v) for k, v in kwargs.items()}
    r.hset(f"{PREFIX}camera:{camera_id}", values=updates)


def deactivate_camera(camera_id):
    update_camera(camera_id, is_active="0")


def _parse_camera(data):
    return {
        "id": data.get("id", ""),
        "name": data.get("name", ""),
        "snapshot_url": data.get("snapshot_url", ""),
        "business_type": data.get("business_type", "retail"),
        "location": data.get("location", ""),
        "is_active": data.get("is_active", "1") == "1",
        "whatsapp_alert": data.get("whatsapp_alert", "1") == "1",
        "zone_type": data.get("zone_type", "general"),
        "hours_open": data.get("hours_open", "09:00"),
        "hours_close": data.get("hours_close", "22:00"),
        "after_hours_mode": data.get("after_hours_mode", "critical"),
        "timezone": data.get("timezone", "Asia/Jakarta"),
        "last_seen": data.get("last_seen", ""),
        "created_at": data.get("created_at", ""),
    }


# --- Analyses ---

def add_analysis(camera_id, result, image_url="", shift=""):
    r = get_redis()
    if not r:
        return None
    analysis_id = str(uuid.uuid4())[:8]
    data = {
        "camera_id": camera_id,
        "theft_risk_score": str(result.get("theft_risk_score", 0)),
        "customer_service_score": str(result.get("customer_service_score", 0)),
        "alert_level": result.get("alert_level", "none"),
        "fraud_indicators": json.dumps(result.get("fraud_indicators", [])),
        "staff_behavior": json.dumps(result.get("staff_behavior", {})),
        "summary": result.get("summary", ""),
        "people_count": str(result.get("people_count", 0)),
        "image_url": image_url,
        "shift": shift or _current_shift(),
        "model_used": result.get("model_used", "gpt-4o-mini"),
        "escalated": "1" if result.get("escalated") else "0",
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    r.hset(f"{PREFIX}analysis:{analysis_id}", values=data)
    r.lpush(f"{PREFIX}analyses:camera:{camera_id}", analysis_id)
    r.lpush(f"{PREFIX}analyses:all", analysis_id)
    r.ltrim(f"{PREFIX}analyses:all", 0, 999)
    r.ltrim(f"{PREFIX}analyses:camera:{camera_id}", 0, 499)
    update_camera(camera_id, last_seen=data["analyzed_at"])
    return analysis_id


def _current_shift():
    """Determine current shift based on Jakarta time."""
    from datetime import datetime, timezone, timedelta
    jakarta = datetime.now(timezone.utc) + timedelta(hours=7)
    h = jakarta.hour
    if 6 <= h < 14:
        return "pagi"
    elif 14 <= h < 22:
        return "sore"
    else:
        return "malam"


def get_recent_analyses(camera_id=None, limit=50):
    r = get_redis()
    if not r:
        return []
    if camera_id:
        ids = r.lrange(f"{PREFIX}analyses:camera:{camera_id}", 0, limit - 1)
    else:
        ids = r.lrange(f"{PREFIX}analyses:all", 0, limit - 1)

    analyses = []
    for aid in ids:
        data = r.hgetall(f"{PREFIX}analysis:{aid}")
        if data:
            data["id"] = aid
            cam = get_camera(data.get("camera_id", ""))
            data["camera_name"] = cam["name"] if cam else "Unknown"
            data["business_type"] = cam["business_type"] if cam else "retail"
            data["theft_risk_score"] = float(data.get("theft_risk_score", 0))
            data["customer_service_score"] = float(data.get("customer_service_score", 0))
            data["people_count"] = int(data.get("people_count", 0))
            data["fraud_indicators"] = json.loads(data.get("fraud_indicators", "[]"))
            data["staff_behavior"] = json.loads(data.get("staff_behavior", "{}"))
            analyses.append(data)
    return analyses


def get_score_trends(camera_id=None, business_type=None, limit=200):
    r = get_redis()
    if not r:
        return []
    if camera_id:
        ids = r.lrange(f"{PREFIX}analyses:camera:{camera_id}", 0, limit - 1)
    else:
        ids = r.lrange(f"{PREFIX}analyses:all", 0, limit - 1)

    trends = []
    for aid in ids:
        data = r.hgetall(f"{PREFIX}analysis:{aid}")
        if not data:
            continue
        if business_type:
            cam = get_camera(data.get("camera_id", ""))
            if not cam or cam["business_type"] != business_type:
                continue
        trends.append({
            "customer_service_score": float(data.get("customer_service_score", 0)),
            "theft_risk_score": float(data.get("theft_risk_score", 0)),
            "analyzed_at": data.get("analyzed_at", ""),
            "camera_id": data.get("camera_id", ""),
        })
    trends.reverse()
    return trends


# --- Alerts ---

def add_alert(analysis_id, camera_id, severity, category, title, description="", image_url=""):
    r = get_redis()
    if not r:
        return None
    alert_id = str(uuid.uuid4())[:8]
    data = {
        "analysis_id": analysis_id,
        "camera_id": camera_id,
        "severity": severity,
        "category": category,
        "title": title,
        "description": description,
        "image_url": image_url,
        "is_read": "0",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    r.hset(f"{PREFIX}alert:{alert_id}", values=data)
    r.lpush(f"{PREFIX}alerts:all", alert_id)
    r.lpush(f"{PREFIX}alerts:camera:{camera_id}", alert_id)
    r.ltrim(f"{PREFIX}alerts:all", 0, 499)
    return alert_id


def get_alerts(severity=None, camera_id=None, category=None, unread_only=False, limit=100):
    r = get_redis()
    if not r:
        return []
    if camera_id:
        ids = r.lrange(f"{PREFIX}alerts:camera:{camera_id}", 0, limit - 1)
    else:
        ids = r.lrange(f"{PREFIX}alerts:all", 0, limit - 1)

    alerts = []
    for aid in ids:
        data = r.hgetall(f"{PREFIX}alert:{aid}")
        if not data:
            continue
        data["id"] = aid
        data["is_read"] = data.get("is_read", "0") == "1"
        if severity and data.get("severity") != severity:
            continue
        if category and data.get("category") != category:
            continue
        if unread_only and data["is_read"]:
            continue
        cam = get_camera(data.get("camera_id", ""))
        data["camera_name"] = cam["name"] if cam else "Unknown"
        data["business_type"] = cam["business_type"] if cam else "retail"
        alerts.append(data)
        if len(alerts) >= limit:
            break
    return alerts


def mark_alert_read(alert_id):
    r = get_redis()
    if not r:
        return
    r.hset(f"{PREFIX}alert:{alert_id}", "is_read", "1")


def is_duplicate_alert(camera_id, category, window_seconds=300):
    r = get_redis()
    if not r:
        return False
    ids = r.lrange(f"{PREFIX}alerts:camera:{camera_id}", 0, 9)
    now = time.time()
    for aid in ids:
        data = r.hgetall(f"{PREFIX}alert:{aid}")
        if data and data.get("category") == category:
            created = data.get("created_at", "")
            try:
                from datetime import datetime
                ts = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").timestamp()
                if now - ts < window_seconds:
                    return True
            except (ValueError, TypeError):
                pass
    return False


# --- Dashboard Stats ---

def get_dashboard_stats():
    r = get_redis()
    if not r:
        return {
            "total_cameras": 0, "total_alerts": 0, "critical_alerts": 0,
            "avg_service_score": 0, "avg_theft_risk": 0, "total_analyses": 0,
            "db_configured": False,
        }

    cameras = get_cameras(active_only=True)
    total_cameras = len(cameras)

    alert_ids = r.lrange(f"{PREFIX}alerts:all", 0, 99)
    unread = 0
    critical = 0
    for aid in alert_ids:
        data = r.hgetall(f"{PREFIX}alert:{aid}")
        if data and data.get("is_read", "0") == "0":
            unread += 1
            if data.get("severity") == "critical":
                critical += 1

    analysis_ids = r.lrange(f"{PREFIX}analyses:all", 0, 49)
    service_scores = []
    theft_scores = []
    for aid in analysis_ids:
        data = r.hgetall(f"{PREFIX}analysis:{aid}")
        if data:
            try:
                service_scores.append(float(data.get("customer_service_score", 0)))
                theft_scores.append(float(data.get("theft_risk_score", 0)))
            except (ValueError, TypeError):
                pass

    return {
        "total_cameras": total_cameras,
        "total_alerts": unread,
        "critical_alerts": critical,
        "avg_service_score": round(sum(service_scores) / len(service_scores), 1) if service_scores else 0,
        "avg_theft_risk": round(sum(theft_scores) / len(theft_scores), 1) if theft_scores else 0,
        "total_analyses": len(analysis_ids),
        "db_configured": True,
    }


# --- Recipients (WhatsApp) ---

def add_recipient(phone, name, role="manager", digest=True, alerts=True):
    r = get_redis()
    if not r:
        return None
    rid = str(uuid.uuid4())[:8]
    r.hset(f"{PREFIX}recipient:{rid}", values={
        "phone": phone,
        "name": name,
        "role": role,
        "digest": "1" if digest else "0",
        "alerts": "1" if alerts else "0",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    r.sadd(f"{PREFIX}recipients", rid)
    return rid


def get_recipients(digest_only=False, alerts_only=False):
    r = get_redis()
    if not r:
        return []
    ids = r.smembers(f"{PREFIX}recipients")
    out = []
    for rid in ids:
        d = r.hgetall(f"{PREFIX}recipient:{rid}")
        if not d:
            continue
        d["id"] = rid
        d["digest"] = d.get("digest", "1") == "1"
        d["alerts"] = d.get("alerts", "1") == "1"
        if digest_only and not d["digest"]:
            continue
        if alerts_only and not d["alerts"]:
            continue
        out.append(d)
    return out


def delete_recipient(rid):
    r = get_redis()
    if not r:
        return
    r.delete(f"{PREFIX}recipient:{rid}")
    r.srem(f"{PREFIX}recipients", rid)


# --- Daily digest stats ---

def get_yesterday_stats():
    """Aggregate yesterday's analyses for daily digest."""
    from datetime import datetime, timezone, timedelta
    r = get_redis()
    if not r:
        return None

    jakarta = datetime.now(timezone.utc) + timedelta(hours=7)
    yesterday = jakarta - timedelta(days=1)
    y_start = yesterday.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    y_end = yesterday.replace(hour=23, minute=59, second=59).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Note: stored timestamps are UTC, so adjust window
    # Simpler: just look at last 24 hours of data
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    analysis_ids = r.lrange(f"{PREFIX}analyses:all", 0, 499)
    by_shift = {"pagi": [], "sore": [], "malam": []}
    by_camera = {}
    total = 0
    critical_alerts = 0
    high_alerts = 0
    service_scores = []
    theft_scores = []

    for aid in analysis_ids:
        d = r.hgetall(f"{PREFIX}analysis:{aid}")
        if not d:
            continue
        if d.get("analyzed_at", "") < cutoff:
            continue
        total += 1
        try:
            svc = float(d.get("customer_service_score", 0))
            theft = float(d.get("theft_risk_score", 0))
            service_scores.append(svc)
            theft_scores.append(theft)
            shift = d.get("shift", "")
            if shift in by_shift:
                by_shift[shift].append(svc)
            cam_id = d.get("camera_id", "")
            if cam_id not in by_camera:
                by_camera[cam_id] = []
            by_camera[cam_id].append(svc)
            level = d.get("alert_level", "")
            if level == "critical":
                critical_alerts += 1
            elif level == "high":
                high_alerts += 1
        except (ValueError, TypeError):
            pass

    # Per-shift averages
    shift_avgs = {}
    for s, scores in by_shift.items():
        if scores:
            shift_avgs[s] = round(sum(scores) / len(scores), 1)

    # Best/worst shift
    best_shift = max(shift_avgs.items(), key=lambda x: x[1])[0] if shift_avgs else None
    worst_shift = min(shift_avgs.items(), key=lambda x: x[1])[0] if shift_avgs else None

    # Per-camera averages
    camera_avgs = []
    for cam_id, scores in by_camera.items():
        cam = get_camera(cam_id)
        if cam and scores:
            camera_avgs.append({
                "name": cam["name"],
                "avg_service": round(sum(scores) / len(scores), 1),
                "samples": len(scores),
            })
    camera_avgs.sort(key=lambda x: x["avg_service"], reverse=True)

    return {
        "date": yesterday.strftime("%Y-%m-%d"),
        "total_analyses": total,
        "critical_alerts": critical_alerts,
        "high_alerts": high_alerts,
        "avg_service": round(sum(service_scores) / len(service_scores), 1) if service_scores else 0,
        "avg_theft_risk": round(sum(theft_scores) / len(theft_scores), 1) if theft_scores else 0,
        "shift_avgs": shift_avgs,
        "best_shift": best_shift,
        "worst_shift": worst_shift,
        "best_camera": camera_avgs[0] if camera_avgs else None,
        "worst_camera": camera_avgs[-1] if len(camera_avgs) > 1 else None,
    }


# --- Shift leaderboard ---

def get_shift_leaderboard(days=7):
    """Compute shift performance over the last N days."""
    from datetime import datetime, timezone, timedelta
    r = get_redis()
    if not r:
        return {}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    analysis_ids = r.lrange(f"{PREFIX}analyses:all", 0, 999)
    by_shift = {"pagi": {"service": [], "theft": [], "alerts": 0, "samples": 0},
                "sore": {"service": [], "theft": [], "alerts": 0, "samples": 0},
                "malam": {"service": [], "theft": [], "alerts": 0, "samples": 0}}

    for aid in analysis_ids:
        d = r.hgetall(f"{PREFIX}analysis:{aid}")
        if not d or d.get("analyzed_at", "") < cutoff:
            continue
        shift = d.get("shift", "")
        if shift not in by_shift:
            continue
        try:
            by_shift[shift]["service"].append(float(d.get("customer_service_score", 0)))
            by_shift[shift]["theft"].append(float(d.get("theft_risk_score", 0)))
            by_shift[shift]["samples"] += 1
            if d.get("alert_level") in ("high", "critical"):
                by_shift[shift]["alerts"] += 1
        except (ValueError, TypeError):
            pass

    out = {}
    for s, data in by_shift.items():
        if data["samples"] > 0:
            out[s] = {
                "avg_service": round(sum(data["service"]) / len(data["service"]), 1),
                "avg_theft": round(sum(data["theft"]) / len(data["theft"]), 1),
                "alerts": data["alerts"],
                "samples": data["samples"],
            }
    return out


# --- Alert feedback ---

def update_alert_feedback(alert_id, feedback):
    """Mark alert with user feedback (false_positive, confirmed, investigating)."""
    r = get_redis()
    if not r:
        return False
    if not r.hgetall(f"{PREFIX}alert:{alert_id}"):
        return False
    r.hset(f"{PREFIX}alert:{alert_id}", values={
        "feedback": feedback,
        "feedback_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "is_read": "1",
    })
    return True


def get_camera_false_positive_rate(camera_id, days=30):
    """Compute false positive rate for a camera over the last N days."""
    from datetime import datetime, timezone, timedelta
    r = get_redis()
    if not r:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = r.lrange(f"{PREFIX}alerts:camera:{camera_id}", 0, 199)
    total = 0
    false_positives = 0
    for aid in ids:
        d = r.hgetall(f"{PREFIX}alert:{aid}")
        if not d or d.get("created_at", "") < cutoff:
            continue
        if not d.get("feedback"):
            continue  # only count alerts with feedback
        total += 1
        if d.get("feedback") == "false_positive":
            false_positives += 1
    if total == 0:
        return None
    return round(false_positives / total * 100, 1)
