"""OpenAI Vision API analyzer for CCTV frames."""
import json
import os
import base64
import requests
from openai import OpenAI


def _get_client():
    return OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))


def build_prompt(business_type):
    context = {
        "retail": "a retail store. Watch for shoplifting, concealment of merchandise, tag removal, bag stuffing, distraction theft, employee collusion, and sweethearting (not scanning items at checkout). For customer service, evaluate staff greeting customers, attentiveness, product knowledge assistance, and checkout efficiency.",
        "wholesale": "a wholesale/warehouse operation. Watch for inventory theft, unauthorized access to restricted areas, loading dock theft, falsified counts, employee pilferage, and unauthorized removal of goods. For customer service, evaluate order processing speed, staff helpfulness, and professional handling of bulk orders.",
        "restaurant": "a restaurant. Watch for cash register theft, free food given to friends, ingredient theft, dine-and-dash behavior, and unauthorized discounts. For customer service, evaluate greeting speed, table attentiveness, order accuracy behavior, cleanliness, and staff professionalism.",
    }

    return f"""You are a CCTV security and operations analyst for {context.get(business_type, context['retail'])}

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


def analyze_image_url(image_url, business_type):
    """Analyze an image from a URL using OpenAI Vision."""
    client = _get_client()
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": build_prompt(business_type)},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Analyze this CCTV frame:"},
                        {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
                    ],
                },
            ],
            max_tokens=800,
            temperature=0.1,
        )
        return _parse_response(response.choices[0].message.content)
    except Exception as e:
        return _error_result(str(e))


def analyze_image_base64(b64_image, business_type):
    """Analyze a base64-encoded image using OpenAI Vision."""
    client = _get_client()
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": build_prompt(business_type)},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Analyze this CCTV frame:"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_image}",
                                "detail": "high",
                            },
                        },
                    ],
                },
            ],
            max_tokens=800,
            temperature=0.1,
        )
        return _parse_response(response.choices[0].message.content)
    except Exception as e:
        return _error_result(str(e))


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
