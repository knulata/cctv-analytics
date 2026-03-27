"""Upstash Redis storage layer for CCTV Analytics."""
import os
import json
import time
import uuid

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
    keys = r.smembers("cameras")
    cameras = []
    for key in keys:
        data = r.hgetall(f"camera:{key}")
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
    data = r.hgetall(f"camera:{camera_id}")
    if not data:
        return None
    data["id"] = camera_id
    return _parse_camera(data)


def add_camera(name, snapshot_url, business_type, location="", whatsapp_alert=True):
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
        "last_seen": "",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    r.hset(f"camera:{camera_id}", values=data)
    r.sadd("cameras", camera_id)
    return camera_id


def update_camera(camera_id, **kwargs):
    r = get_redis()
    if not r:
        return
    updates = {k: str(v) for k, v in kwargs.items()}
    r.hset(f"camera:{camera_id}", values=updates)


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
        "last_seen": data.get("last_seen", ""),
        "created_at": data.get("created_at", ""),
    }


# --- Analyses ---

def add_analysis(camera_id, result, image_url=""):
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
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    r.hset(f"analysis:{analysis_id}", values=data)
    r.lpush(f"analyses:camera:{camera_id}", analysis_id)
    r.lpush("analyses:all", analysis_id)
    r.ltrim("analyses:all", 0, 999)
    r.ltrim(f"analyses:camera:{camera_id}", 0, 499)
    update_camera(camera_id, last_seen=data["analyzed_at"])
    return analysis_id


def get_recent_analyses(camera_id=None, limit=50):
    r = get_redis()
    if not r:
        return []
    if camera_id:
        ids = r.lrange(f"analyses:camera:{camera_id}", 0, limit - 1)
    else:
        ids = r.lrange("analyses:all", 0, limit - 1)

    analyses = []
    for aid in ids:
        data = r.hgetall(f"analysis:{aid}")
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
        ids = r.lrange(f"analyses:camera:{camera_id}", 0, limit - 1)
    else:
        ids = r.lrange("analyses:all", 0, limit - 1)

    trends = []
    for aid in ids:
        data = r.hgetall(f"analysis:{aid}")
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
    r.hset(f"alert:{alert_id}", values=data)
    r.lpush("alerts:all", alert_id)
    r.lpush(f"alerts:camera:{camera_id}", alert_id)
    r.ltrim("alerts:all", 0, 499)
    return alert_id


def get_alerts(severity=None, camera_id=None, category=None, unread_only=False, limit=100):
    r = get_redis()
    if not r:
        return []
    if camera_id:
        ids = r.lrange(f"alerts:camera:{camera_id}", 0, limit - 1)
    else:
        ids = r.lrange("alerts:all", 0, limit - 1)

    alerts = []
    for aid in ids:
        data = r.hgetall(f"alert:{aid}")
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
    r.hset(f"alert:{alert_id}", "is_read", "1")


def is_duplicate_alert(camera_id, category, window_seconds=300):
    r = get_redis()
    if not r:
        return False
    ids = r.lrange(f"alerts:camera:{camera_id}", 0, 9)
    now = time.time()
    for aid in ids:
        data = r.hgetall(f"alert:{aid}")
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

    alert_ids = r.lrange("alerts:all", 0, 99)
    unread = 0
    critical = 0
    for aid in alert_ids:
        data = r.hgetall(f"alert:{aid}")
        if data and data.get("is_read", "0") == "0":
            unread += 1
            if data.get("severity") == "critical":
                critical += 1

    analysis_ids = r.lrange("analyses:all", 0, 49)
    service_scores = []
    theft_scores = []
    for aid in analysis_ids:
        data = r.hgetall(f"analysis:{aid}")
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
