"""
Speed-to-Lead Follow-Up
────────────────────────
The single highest-ROI automation for a contractor: respond to a new lead
in under 60 seconds, then follow up on a fixed cadence until they reply.

Why it matters: for home services, the contractor who responds first usually
wins the job — not the cheapest or the best. Most small contractors respond
in hours or days because they're on a jobsite. This closes that gap.

Cadence after initial instant reply: day 1, day 3, day 7. Then stop.
"""

from datetime import datetime, timezone, timedelta

import storage

FOLLOWUP_DAYS = [1, 3, 7]

KIND = "followups"


def _load(tenant_id: str) -> list:
    return storage.get_all(tenant_id, KIND)


def _save(tenant_id: str, rows: list):
    for r in rows:
        key = r.get("_key") or "".join(c for c in r.get("phone", "") if c.isdigit())[-10:] \
              or r.get("customer_name", "unknown").lower().replace(" ", "_")
        storage.upsert(tenant_id, KIND, key, r)


# ── The instant reply (fires within 60s of a new lead) ─────────
def build_instant_reply(cfg: dict, customer_name: str, project_type: str = "") -> str:
    b = cfg["business"]
    first = customer_name.split()[0] if customer_name else "there"
    proj = f" about your {project_type.lower()}" if project_type else ""
    return (
        f"Hey {first} — this is {b['short_name']} in Kokomo. Got your message"
        f"{proj}. {b['owner']} will give you a call shortly to set up a free "
        f"on-site estimate. What's a good time to reach you?"
    )


def build_followup_message(cfg: dict, customer_name: str, attempt: int,
                           project_type: str = "") -> str:
    b = cfg["business"]
    first = customer_name.split()[0] if customer_name else "there"
    proj = project_type.lower() if project_type else "your project"

    if attempt == 0:
        return (
            f"Hey {first}, following up on {proj}. Still want us to come take a "
            f"look and get you a number? Free estimate, no pressure. "
            f"— {b['short_name']}"
        )
    if attempt == 1:
        return (
            f"Hi {first}, checking in one more time about {proj}. If the timing "
            f"isn't right, no worries at all — just let us know and we'll get out "
            f"of your hair. {b['phone']}"
        )
    return (
        f"Hey {first} — last note from us. If you still want an estimate on "
        f"{proj} down the road, we're here: {b['phone']}. Good luck either way. "
        f"— {b['owner']}, {b['short_name']}"
    )


# ── SKILL: enroll a new lead ───────────────────────────────────
def start_followup(cfg: dict, customer_name: str = "", phone: str = "",
                   project_type: str = "") -> str:
    tenant_id = cfg["tenant_id"]
    now = datetime.now(timezone.utc)

    rows = _load(tenant_id)
    rows.append({
        "customer_name": customer_name,
        "phone": phone,
        "project_type": project_type,
        "created_at": now.isoformat(),
        "instant_reply": build_instant_reply(cfg, customer_name, project_type),
        "sends_completed": 0,
        "next_send": (now + timedelta(days=FOLLOWUP_DAYS[0])).isoformat(),
        "status": "active",   # active | replied | booked | dead
    })
    _save(tenant_id, rows)

    return (
        f"{customer_name} enrolled in follow-up. Instant reply queued now, then "
        f"day {FOLLOWUP_DAYS[0]}, {FOLLOWUP_DAYS[1]}, and {FOLLOWUP_DAYS[2]} "
        f"if they don't respond."
    )


# ── SKILL: stop the sequence ───────────────────────────────────
def stop_followup(cfg: dict, customer_name: str = "", reason: str = "replied") -> str:
    tenant_id = cfg["tenant_id"]
    rows = _load(tenant_id)
    for r in rows:
        if r["customer_name"].lower() == customer_name.lower():
            r["status"] = reason
            r["next_send"] = None
            _save(tenant_id, rows)
            return f"Follow-up stopped for {customer_name} ({reason})."
    return f"No active follow-up found for {customer_name}."


# ── Stop by phone (inbound reply) ──────────────────────────────
def stop_followup_by_phone(tenant_id: str, phone: str, reason: str = "replied") -> int:
    """
    Stops every active follow-up for this phone number. Returns how many.

    Matches on phone, not name: stop_followup() matches by customer_name, and
    leads without a name (e.g. missed calls) would collide on "".
    """
    key = "".join(c for c in (phone or "") if c.isdigit())[-10:]
    if not key:
        return 0

    rows = _load(tenant_id)
    changed = []
    for r in rows:
        if r.get("status") != "active":
            continue
        if "".join(c for c in r.get("phone", "") if c.isdigit())[-10:] != key:
            continue
        r["status"] = reason
        r["next_send"] = None
        changed.append(r)

    if changed:
        _save(tenant_id, changed)
    return len(changed)


# ── SKILL: what's due ──────────────────────────────────────────
def get_pending_followups(cfg: dict) -> str:
    tenant_id = cfg["tenant_id"]
    now = datetime.now(timezone.utc)
    rows = _load(tenant_id)

    due = []
    for r in rows:
        if r["status"] != "active" or not r.get("next_send"):
            continue
        try:
            if datetime.fromisoformat(r["next_send"]) <= now:
                due.append(r)
        except Exception:
            continue

    if not due:
        active = sum(1 for r in rows if r["status"] == "active")
        return f"No follow-ups due right now. {active} lead(s) still in sequence."

    lines = [f"{len(due)} follow-up(s) due:"]
    for r in due:
        n = r["sends_completed"] + 1
        lines.append(
            f"- {r['customer_name']} ({r['phone']}) — follow-up #{n}, {r['project_type']}"
        )
    return "\n".join(lines)


# ── Scheduler-driven advance ───────────────────────────────────
def advance_followups(tenant_id: str) -> list:
    now = datetime.now(timezone.utc)
    rows = _load(tenant_id)
    to_send = []

    for r in rows:
        if r["status"] != "active" or not r.get("next_send"):
            continue
        try:
            if datetime.fromisoformat(r["next_send"]) > now:
                continue
        except Exception:
            continue

        to_send.append(dict(r))
        r["sends_completed"] += 1

        if r["sends_completed"] >= len(FOLLOWUP_DAYS):
            r["status"] = "dead"
            r["next_send"] = None
        else:
            created = datetime.fromisoformat(r["created_at"])
            r["next_send"] = (
                created + timedelta(days=FOLLOWUP_DAYS[r["sends_completed"]])
            ).isoformat()

    _save(tenant_id, rows)
    return to_send
