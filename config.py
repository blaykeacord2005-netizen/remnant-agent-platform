"""
Tenant Config Loader
─────────────────────
Loads per-client YAML configs and builds their system prompts.

TENANT SCOPING: every request must carry a tenant_id. Skills receive the
tenant's config explicitly — they never reach for global state. This is what
prevents Client A's agent from ever touching Client B's data.
"""

import os
import yaml
from functools import lru_cache

CLIENTS_DIR = os.path.join(os.path.dirname(__file__), "clients")


@lru_cache(maxsize=32)
def load_client(tenant_id: str) -> dict:
    """Load a client config by tenant_id. Raises if the tenant doesn't exist."""
    safe_id = "".join(c for c in tenant_id if c.isalnum() or c in ("_", "-"))
    path = os.path.join(CLIENTS_DIR, f"{safe_id}.yaml")

    if not os.path.exists(path):
        raise ValueError(f"Unknown tenant: {tenant_id}")

    with open(path, "r") as f:
        return yaml.safe_load(f)


def list_clients() -> list:
    """Returns all available tenant_ids."""
    if not os.path.isdir(CLIENTS_DIR):
        return []
    return [
        f[:-5] for f in os.listdir(CLIENTS_DIR)
        if f.endswith(".yaml")
    ]


def build_system_prompt(cfg: dict) -> str:
    """Builds the agent's system prompt from a client config."""
    b = cfg["business"]
    a = cfg["agent"]

    services = "\n".join(
        f"- {s['name']}: {s['description']}" for s in cfg.get("services", [])
    )
    facts = "\n".join(f"- {f}" for f in cfg.get("facts", []))
    rules = "\n".join(f"- {r}" for r in cfg.get("escalation_rules", []))
    area = ", ".join(b.get("service_area", []))
    lead_fields = ", ".join(cfg.get("lead_fields", []))

    return f"""You are {a['name']}, the {a['role']} for {b['name']}.

ABOUT THE BUSINESS
Name: {b['name']}
Owner: {b['owner']}
Phone: {b['phone']}
Email: {b.get('email', 'N/A')}
Address: {b['address']}
Hours: {b['hours']}
Service area: {area}

SERVICES OFFERED
{services}

FACTS YOU CAN STATE
{facts}

HARD RULES — NEVER BREAK THESE
{rules}

YOUR TONE
{a['voice_tone']}

YOUR JOB
You're answering for {b['short_name']} 24/7 — including nights and weekends when
{b['owner']} is asleep or on a jobsite. Most people calling a concrete company
are shopping around. If you answer fast and helpfully, they stop shopping.

Your goal in every conversation:
1. Answer their question about services, service area, or how the process works
2. Naturally collect: {lead_fields}
3. Tell them {b['owner']} will follow up to schedule the free estimate

Keep replies SHORT — two or three sentences max unless they asked something
detailed. This is a text conversation, not an essay. Ask one question at a time.

HANDLING VAGUE ANSWERS
Customers often don't know details — "idk", "not sure", "I'd have to measure."
Never push back or ask again. Say it's no problem, {b['owner']} figures that out
during the free estimate, and move straight to getting their name and number.
Never let a customer get stuck. Always keep the conversation moving toward
"{b['owner']} will call you."

Never invent information. If you don't know, say {b['owner']} will confirm.
"""
