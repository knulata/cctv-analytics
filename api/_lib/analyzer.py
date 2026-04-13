"""OpenAI Vision API analyzer for CCTV frames — tiered model strategy."""
import json
import os
import base64
import hashlib
import requests
from openai import OpenAI

# Frame-diff storage: camera_id -> hash of last analyzed frame
_frame_hashes = {}

# Configurable thresholds
ESCALATION_THRESHOLD = 6  # mini score >= this triggers GPT-4o deep analysis
DIFF_BLOCK_SIZE = 16  # downsample grid for perceptual hashing


def _get_client():
    return OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))


ZONE_FOCUS = {
    "checkout": "the CHECKOUT/CASHIER area. Pay extreme attention to: items not being scanned (sweethearting), cash handling discrepancies, voids/refunds without customers, employees giving free items to friends, customer concealment at the bag area.",
    "aisle": "a STORE AISLE. Pay attention to: customers concealing items in bags/clothing, tag removal, opening packaging, lingering near high-value items, sleight-of-hand behavior, group distraction tactics.",
    "storeroom": "a STOREROOM/STOCK area. This area should usually be empty or staff-only. Watch for: unauthorized access, items being removed in personal bags, employees lingering without purpose, hidden corners being used.",
    "entrance": "the ENTRANCE/EXIT. Watch for: people leaving with items not in bags from this store, tailgating, suspicious loitering, people running out, returning customers (could be theft return scams).",
    "parking": "the PARKING area. Watch for: vehicles loading suspicious items, people transferring goods between vehicles, surveillance of customers, abandoned items.",
    "warehouse": "a WAREHOUSE/LOADING area. Watch for: pallets/boxes leaving without paperwork, unauthorized vehicles, stock count discrepancies, employees moving inventory in unusual patterns.",
    "dining": "a RESTAURANT DINING area. Watch for: customers leaving without paying (dine-and-dash), staff giving free food/drinks, plate sharing without ordering, table attentiveness.",
    "kitchen": "a KITCHEN/PREP area. Watch for: ingredient theft, employees eating inventory, sanitation issues, unauthorized personnel.",
    "general": "a general retail area. Apply standard loss prevention and customer service observation.",
}


def build_prompt(business_type, deep=False, zone_type="general", after_hours=False):
    context = {
        "retail": "a retail store. Watch for shoplifting, concealment of merchandise, tag removal, bag stuffing, distraction theft, employee collusion, and sweethearting (not scanning items at checkout). For customer service, evaluate staff greeting customers, attentiveness, product knowledge assistance, and checkout efficiency.",
        "wholesale": "a wholesale/warehouse operation. Watch for inventory theft, unauthorized access to restricted areas, loading dock theft, falsified counts, employee pilferage, and unauthorized removal of goods. For customer service, evaluate order processing speed, staff helpfulness, and professional handling of bulk orders.",
        "restaurant": "a restaurant. Watch for cash register theft, free food given to friends, ingredient theft, dine-and-dash behavior, and unauthorized discounts. For customer service, evaluate greeting speed, table attentiveness, order accuracy behavior, cleanliness, and staff professionalism.",
    }

    zone_focus = ZONE_FOCUS.get(zone_type, ZONE_FOCUS["general"])

    deep_instruction = """
IMPORTANT: This frame was flagged as suspicious by initial screening. Analyze carefully and in detail.
Look closely at hand positions, body language, item handling, and any concealment behavior.
""" if deep else ""

    after_hours_instruction = """
⚠️ AFTER-HOURS MODE: This camera is monitoring during closed/non-business hours.
ANY person, motion, or unusual activity must be flagged as critical.
The expected state is EMPTY. Even employees in the area outside their normal shift is suspicious.
If you see ANY people, set theft_risk_score >= 8 and alert_level = "critical".
""" if after_hours else ""

    return f"""You are a CCTV security and operations analyst for {context.get(business_type, context['retail'])}

ZONE FOCUS: This camera is positioned at {zone_focus}
{deep_instruction}{after_hours_instruction}
Analyze this camera frame and provide a JSON assessment. Be specific about what you observe.

Respond ONLY with valid JSON in this exact structure:
{{
    "theft_risk_score": <0-10, 0=no risk, 10=active theft observed>,
    "theft_description": "<what you see that indicates theft risk, or 'No indicators'>",
    "fraud_indicators": ["<list of specific fraud/theft indicators observed, empty if none>"],
    "customer_service_score": <1-10, 1=terrible, 10=exceptional>,
    "staff_behavior": {{
        "attentiveness": "<description of staff attentiveness>",
        "greeting": "<whether staff are greeting/engaging customers>",
        "response_time": "<observation about response speed>",
        "professionalism": "<overall professionalism observation>"
    }},
    "alert_level": "<none|low|medium|high|critical>",
    "people_count": <estimated number of people visible>,
    "summary": "<one-line summary of the scene>"
}}

Rules:
- Be conservative with theft scores — only rate high if you see clear suspicious behavior
- Customer service scores should reflect observable behavior, not assumptions
- alert_level: none (routine), low (minor concern), medium (should review), high (immediate attention), critical (active incident)
- If the image is unclear or obstructed, note this in the summary"""


# ─── Frame diff / motion detection ─────────────────────────

def compute_frame_hash(b64_image):
    """Compute a perceptual hash of the image for change detection.
    Uses a simple approach: hash the raw bytes in chunks.
    Two nearly-identical frames will have the same hash.
    A scene change (person enters, movement) changes the hash."""
    raw = base64.b64decode(b64_image)
    # Use MD5 of downsampled image data — fast and sufficient for diff detection
    # We sample every Nth byte to be resilient to JPEG compression variance
    step = max(1, len(raw) // 1024)  # sample ~1024 points from the image
    sampled = bytes(raw[i] for i in range(0, len(raw), step))
    return hashlib.md5(sampled).hexdigest()


def has_scene_changed(camera_id, b64_image):
    """Check if the scene has changed since last analysis.
    Returns True if scene changed or if this is the first frame."""
    new_hash = compute_frame_hash(b64_image)
    old_hash = _frame_hashes.get(camera_id)
    _frame_hashes[camera_id] = new_hash
    if old_hash is None:
        return True  # first frame, always analyze
    return new_hash != old_hash


def has_scene_changed_redis(camera_id, b64_image):
    """Redis-backed scene change detection (persists across serverless invocations)."""
    from _lib import db
    new_hash = compute_frame_hash(b64_image)
    r = db.get_redis()
    if r:
        key = f"{db.PREFIX}framehash:{camera_id}"
        old_hash = r.get(key)
        r.set(key, new_hash, ex=3600)  # expire after 1 hour
    else:
        old_hash = _frame_hashes.get(camera_id)
        _frame_hashes[camera_id] = new_hash
    if old_hash is None:
        return True
    return new_hash != old_hash


# ─── Tiered analysis ───────────────────────────────────────

def analyze_image_url(image_url, business_type, zone_type="general", after_hours=False):
    """Analyze an image from a URL — uses tiered model strategy."""
    client = _get_client()
    try:
        result = _call_vision(client, "gpt-4o-mini", image_url, business_type, is_url=True,
                              zone_type=zone_type, after_hours=after_hours)
        if _should_escalate(result, after_hours):
            deep_result = _call_vision(client, "gpt-4o", image_url, business_type, is_url=True,
                                       deep=True, zone_type=zone_type, after_hours=after_hours)
            deep_result["escalated"] = True
            deep_result["mini_scores"] = {
                "theft_risk": result.get("theft_risk_score", 0),
                "service": result.get("customer_service_score", 0),
            }
            return deep_result
        result["escalated"] = False
        result["model_used"] = "gpt-4o-mini"
        return result
    except Exception as e:
        return _error_result(str(e))


def analyze_image_base64(b64_image, business_type, zone_type="general", after_hours=False):
    """Analyze a base64-encoded image — uses tiered model strategy."""
    client = _get_client()
    try:
        result = _call_vision(client, "gpt-4o-mini", b64_image, business_type, is_url=False,
                              zone_type=zone_type, after_hours=after_hours)
        if _should_escalate(result, after_hours):
            deep_result = _call_vision(client, "gpt-4o", b64_image, business_type, is_url=False,
                                       deep=True, zone_type=zone_type, after_hours=after_hours)
            deep_result["escalated"] = True
            deep_result["mini_scores"] = {
                "theft_risk": result.get("theft_risk_score", 0),
                "service": result.get("customer_service_score", 0),
            }
            return deep_result
        result["escalated"] = False
        result["model_used"] = "gpt-4o-mini"
        return result
    except Exception as e:
        return _error_result(str(e))


def _should_escalate(result, after_hours=False):
    """Determine if a mini result warrants deep analysis with GPT-4o."""
    theft = result.get("theft_risk_score", 0)
    alert = result.get("alert_level", "none")
    fraud = len(result.get("fraud_indicators", []))
    people = result.get("people_count", 0)
    # After hours: any people detected escalates immediately
    if after_hours and people > 0:
        return True
    return (
        theft >= ESCALATION_THRESHOLD
        or alert in ("high", "critical")
        or fraud >= 2
    )


def _call_vision(client, model, image_data, business_type, is_url=False, deep=False,
                 zone_type="general", after_hours=False):
    """Call OpenAI Vision API with specified model."""
    if is_url:
        image_content = {"type": "image_url", "image_url": {"url": image_data, "detail": "low" if model == "gpt-4o-mini" else "high"}}
    else:
        detail = "low" if model == "gpt-4o-mini" else "high"
        image_content = {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}", "detail": detail}}

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": build_prompt(business_type, deep=deep, zone_type=zone_type, after_hours=after_hours)},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Analyze this CCTV frame:"},
                    image_content,
                ],
            },
        ],
        max_tokens=600 if model == "gpt-4o-mini" else 800,
        temperature=0.1,
    )
    result = _parse_response(response.choices[0].message.content)
    result["model_used"] = model
    return result


# ─── Utilities ──────────────────────────────────────────────

def is_in_business_hours(camera):
    """Check if camera is currently within configured business hours."""
    from datetime import datetime, timezone, timedelta
    open_str = camera.get("hours_open", "09:00")
    close_str = camera.get("hours_close", "22:00")
    # 24/7 mode
    if open_str == close_str == "00:00":
        return True
    try:
        # Jakarta time (WIB UTC+7)
        jakarta = datetime.now(timezone.utc) + timedelta(hours=7)
        now_minutes = jakarta.hour * 60 + jakarta.minute
        oh, om = map(int, open_str.split(":"))
        ch, cm = map(int, close_str.split(":"))
        open_min = oh * 60 + om
        close_min = ch * 60 + cm
        if open_min < close_min:
            return open_min <= now_minutes <= close_min
        else:
            # crosses midnight (e.g. 22:00-06:00)
            return now_minutes >= open_min or now_minutes <= close_min
    except (ValueError, AttributeError):
        return True


def fetch_snapshot(url):
    """Fetch a snapshot image from a camera URL, return base64."""
    try:
        resp = requests.get(url, timeout=10, verify=False)
        if resp.status_code == 200 and len(resp.content) > 1000:
            return base64.b64encode(resp.content).decode("utf-8"), None
        return None, f"HTTP {resp.status_code}, size={len(resp.content)}"
    except Exception as e:
        return None, str(e)


def _parse_response(content):
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return _error_result(f"JSON parse failed: {content[:200]}")


def _error_result(error_msg):
    return {
        "theft_risk_score": 0,
        "customer_service_score": 0,
        "alert_level": "none",
        "fraud_indicators": [],
        "staff_behavior": {},
        "people_count": 0,
        "summary": f"Analysis error: {error_msg}",
        "error": error_msg,
    }
