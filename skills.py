"""
Shared Skills
──────────────
These are CLIENT-AGNOSTIC. They take the tenant's config as a parameter and
contain zero client-specific logic. Update a skill once, every client benefits.

Adding a client should never require touching this file.
"""

from datetime import datetime, timezone

import storage
import notifications
from review_engine import (
    request_review, log_satisfaction, get_pending_reviews, mark_reviewed
)
from followup_engine import (
    start_followup, stop_followup, get_pending_followups
)

LEADS_KIND = "leads"


# ── SKILL: capture a lead ──────────────────────────────────────
def _norm_phone(p: str) -> str:
    """Strip everything but digits so formatting differences don't create dupes."""
    return "".join(c for c in (p or "") if c.isdigit())[-10:]


def capture_lead(cfg: dict, name: str = "", phone: str = "",
                 project_type: str = "", project_details: str = "",
                 timeline: str = "") -> str:
    """
    Saves a lead to persistent storage, scoped to this tenant.

    Deduped by phone: an existing customer gets UPDATED, not duplicated, so
    one person never generates five owner notifications.
    """
    tenant_id = cfg["tenant_id"]
    owner = cfg["business"]["owner"]
    key = _norm_phone(phone)

    if not key:
        return "No usable phone number yet — ask for it before saving."

    existing = storage.get_one(tenant_id, LEADS_KIND, key)
    is_new = existing is None

    lead = dict(existing) if existing else {}
    if name:
        lead["name"] = name
    lead["phone"] = phone
    if project_type:
        lead["project_type"] = project_type
    if project_details and len(project_details) > len(lead.get("project_details", "")):
        lead["project_details"] = project_details
    if timeline:
        lead["timeline"] = timeline
    lead.setdefault("project_type", "")
    lead.setdefault("project_details", "")
    lead.setdefault("timeline", "")

    saved = storage.upsert(tenant_id, LEADS_KIND, key, lead)

    if is_new:
        # Notify the owner for real — email + SMS
        try:
            notifications.notify_new_lead(cfg, saved)
        except Exception as e:
            print(f"[notify] failed: {e}")

        # Enroll in the speed-to-lead sequence
        try:
            start_followup(cfg, customer_name=name, phone=phone,
                           project_type=project_type)
        except Exception as e:
            print(f"[followup] enroll failed: {e}")

        return (
            f"New lead saved and {owner} notified by email and text. Follow-up "
            f"sequence started. Confirm to the customer that {owner} will reach "
            f"out shortly. Do NOT call this tool again for this person unless "
            f"they give genuinely new information."
        )

    return (
        f"Existing lead updated — no duplicate created and {owner} was not "
        f"re-notified. Continue the conversation naturally; don't tell the "
        f"customer you saved anything again."
    )


# ── SKILL: check service area ──────────────────────────────────
def check_service_area(cfg: dict, location: str = "") -> str:
    """Checks whether a location falls in this client's service area."""
    area = [a.lower() for a in cfg["business"].get("service_area", [])]
    loc = location.lower().strip()

    if not loc:
        return "No location given. Ask the customer what town they're in."

    if any(a in loc or loc in a for a in area):
        return f"YES — {location} is inside the service area. Proceed normally."

    return (
        f"{location} is not on the standard service area list. Don't say no "
        f"outright — tell them {cfg['business']['owner']} will confirm whether "
        f"they can cover that area, and collect their info anyway."
    )


# ── SKILL: list services ───────────────────────────────────────
def list_services(cfg: dict) -> str:
    """Returns this client's service list."""
    services = cfg.get("services", [])
    if not services:
        return "No services configured."
    return "\n".join(f"- {s['name']}: {s['description']}" for s in services)


# ── SKILL: get business info ───────────────────────────────────
def get_business_info(cfg: dict) -> str:
    """Returns hours, contact info, and location for this client."""
    b = cfg["business"]
    return (
        f"Name: {b['name']}\n"
        f"Phone: {b['phone']}\n"
        f"Email: {b.get('email', 'N/A')}\n"
        f"Address: {b['address']}\n"
        f"Hours: {b['hours']}"
    )


# ── REGISTRY ───────────────────────────────────────────────────
SKILLS = {
    "capture_lead": {
        "fn": capture_lead,
        "schema": {
            "name": "capture_lead",
            "description": (
                "Save a customer's contact info and project details as a lead. "
                "Call this ONCE, as soon as you have their name and phone number. "
                "It automatically dedupes by phone, so calling it again for the "
                "same person just updates their record — but don't call it "
                "repeatedly as details trickle in. Wait until you have their "
                "name and number, save once, and move on."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Customer's name"},
                    "phone": {"type": "string", "description": "Phone number"},
                    "project_type": {"type": "string", "description": "Driveway, patio, retaining wall, etc."},
                    "project_details": {"type": "string", "description": "Size, condition, anything they mentioned"},
                    "timeline": {"type": "string", "description": "When they want it done"},
                },
                "required": ["name", "phone"],
            },
        },
    },
    "check_service_area": {
        "fn": check_service_area,
        "schema": {
            "name": "check_service_area",
            "description": "Check whether a town or city is within the service area. Call this whenever a customer mentions where they're located.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Town, city, or area the customer mentioned"},
                },
                "required": ["location"],
            },
        },
    },
    "list_services": {
        "fn": list_services,
        "schema": {
            "name": "list_services",
            "description": "Get the full list of services this business offers, with descriptions.",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
    "get_business_info": {
        "fn": get_business_info,
        "schema": {
            "name": "get_business_info",
            "description": "Get hours, phone, email, and address for the business.",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
    "request_review": {
        "fn": request_review,
        "schema": {
            "name": "request_review",
            "description": "Enroll a customer whose job just finished into the review request sequence. Only use for completed jobs.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "phone": {"type": "string"},
                    "job_type": {"type": "string", "description": "What work was done"},
                },
                "required": ["customer_name", "phone"],
            },
        },
    },
    "log_satisfaction": {
        "fn": log_satisfaction,
        "schema": {
            "name": "log_satisfaction",
            "description": (
                "Record whether a customer is happy with the work. CRITICAL: call "
                "this any time a customer expresses satisfaction or dissatisfaction. "
                "Unhappy customers are automatically kept away from public review "
                "sites and routed to the owner instead."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "happy": {"type": "boolean", "description": "True if satisfied, false if not"},
                    "notes": {"type": "string", "description": "What they said, especially any complaint"},
                },
                "required": ["customer_name", "happy"],
            },
        },
    },
    "get_pending_reviews": {
        "fn": get_pending_reviews,
        "schema": {
            "name": "get_pending_reviews",
            "description": "See which review requests are due to send and which unhappy customers need a personal call.",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
    "mark_reviewed": {
        "fn": mark_reviewed,
        "schema": {
            "name": "mark_reviewed",
            "description": "Stop the review sequence for a customer who already left a review.",
            "input_schema": {
                "type": "object",
                "properties": {"customer_name": {"type": "string"}},
                "required": ["customer_name"],
            },
        },
    },
    "stop_followup": {
        "fn": stop_followup,
        "schema": {
            "name": "stop_followup",
            "description": "Stop the follow-up sequence for a lead who replied, booked, or asked to be left alone.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "reason": {"type": "string", "description": "replied, booked, or dead"},
                },
                "required": ["customer_name"],
            },
        },
    },
    "get_pending_followups": {
        "fn": get_pending_followups,
        "schema": {
            "name": "get_pending_followups",
            "description": "See which leads are due for a follow-up message right now.",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
}


def get_tool_schemas() -> list:
    return [s["schema"] for s in SKILLS.values()]


def call_skill(name: str, cfg: dict, tool_input: dict) -> str:
    """
    Executes a skill with the tenant's config injected.
    The tenant config is ALWAYS passed explicitly — skills never look it up
    themselves, which is what keeps tenants isolated.
    """
    if name not in SKILLS:
        return f"Unknown skill: {name}"
    return SKILLS[name]["fn"](cfg, **tool_input)
