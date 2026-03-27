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


def build_prompt(business_type, deep=False):
    context = {
        "retail": "a retail store. Watch for shoplifting, concealment of merchandise, tag removal, bag stuffing, distraction theft, employee collusion, and sweethearting (not scanning items at checkout). For customer service, evaluate staff greeting customers, attentiveness, product knowledge assistance, and checkout efficiency.",
        "wholesale": "a wholesale/warehouse operation. Watch for inventory theft, unauthorized access to restricted areas, loading dock theft, falsified counts, employee pilferage, and unauthorized removal of goods. For customer service, evaluate order processing speed, staff helpfulness, and professional handling of bulk orders.",
        "restaurant": "a restaurant. Watch for cash register theft, free food given to friends, ingredient theft, dine-and-dash behavior, and unauthorized discounts. For customer service, evaluate greeting speed, table attentiveness, order accuracy behavior, cleanliness, and staff professionalism.",
    }

    deep_instruction = """
IMPORTANT: This frame was flagged as suspicious by initial screening. Analyze carefully and in detail.
Look closely at hand positions, body language, item handling, and any concealment behavior.
""" if deep else ""

    return f"""You are a CCTV security and operations analyst for {context.get(business_type, context['retail'])}
{deep_instruction}
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
        old_hash = r.get(f"framehash:{camera_id}")
        r.set(f"framehash:{camera_id}", new_hash, ex=3600)  # expire after 1 hour
    else:
        old_hash = _frame_hashes.get(camera_id)
        _frame_hashes[camera_id] = new_hash
    if old_hash is None:
        return True
    return new_hash != old_hash


# ─── Tiered analysis ───────────────────────────────────────

def analyze_image_url(image_url, business_type):
    """Analyze an image from a URL — uses tiered model strategy."""
    client = _get_client()
    try:
        # Tier 1: GPT-4o-mini (fast, cheap)
        result = _call_vision(client, "gpt-4o-mini", image_url, business_type, is_url=True)

        # Tier 2: Escalate to GPT-4o if suspicious
        if _should_escalate(result):
            deep_result = _call_vision(client, "gpt-4o", image_url, business_type, is_url=True, deep=True)
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


def analyze_image_base64(b64_image, business_type):
    """Analyze a base64-encoded image — uses tiered model strategy."""
    client = _get_client()
    try:
        # Tier 1: GPT-4o-mini
        result = _call_vision(client, "gpt-4o-mini", b64_image, business_type, is_url=False)

        # Tier 2: Escalate to GPT-4o if suspicious
        if _should_escalate(result):
            deep_result = _call_vision(client, "gpt-4o", b64_image, business_type, is_url=False, deep=True)
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


def _should_escalate(result):
    """Determine if a mini result warrants deep analysis with GPT-4o."""
    theft = result.get("theft_risk_score", 0)
    alert = result.get("alert_level", "none")
    fraud = len(result.get("fraud_indicators", []))
    return (
        theft >= ESCALATION_THRESHOLD
        or alert in ("high", "critical")
        or fraud >= 2
    )


def _call_vision(client, model, image_data, business_type, is_url=False, deep=False):
    """Call OpenAI Vision API with specified model."""
    if is_url:
        image_content = {"type": "image_url", "image_url": {"url": image_data, "detail": "low" if model == "gpt-4o-mini" else "high"}}
    else:
        detail = "low" if model == "gpt-4o-mini" else "high"
        image_content = {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}", "detail": detail}}

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": build_prompt(business_type, deep=deep)},
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
