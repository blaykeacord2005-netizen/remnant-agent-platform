"""
Review Automation
──────────────────
The reputation engine. Client-agnostic, tenant-scoped.

Flow:
  1. Job marked complete → customer enters the review sequence
  2. Day 1: friendly ask with a direct review link
  3. Day 4: single reminder if they haven't left one
  4. Day 9: final nudge, then stop

Sentiment gate: if a customer indicates they're unhappy, they are NEVER
sent to a public review site. They get routed to the owner privately instead.
This is what turns a 1-star risk into a phone call.
"""

from datetime import datetime, timezone, timedelta

import storage

# Cadence in days after job completion
SEQUENCE_DAYS = [1, 4, 9]

KIND = "reviews"


def _load(tenant_id: str) -> list:
    return storage.get_all(tenant_id, KIND)


def _save(tenant_id: str, rows: list):
    for r in rows:
        key = r.get("_key") or "".join(c for c in r.get("phone", "") if c.isdigit())[-10:] \
              or r.get("customer_name", "unknown").lower().replace(" ", "_")
        storage.upsert(tenant_id, KIND, key, r)


# ── SKILL: start a review sequence ─────────────────────────────
def request_review(cfg: dict, customer_name: str = "", phone: str = "",
                   job_type: str = "") -> str:
    """Enrolls a completed-job customer into the review request sequence."""
    tenant_id = cfg["tenant_id"]
    b = cfg["business"]
    now = datetime.now(timezone.utc)

    rows = _load(tenant_id)
    rows.append({
        "customer_name": customer_name,
        "phone": phone,
        "job_type": job_type,
        "enrolled_at": now.isoformat(),
        "sends_completed": 0,
        "next_send": (now + timedelta(days=SEQUENCE_DAYS[0])).isoformat(),
        "status": "pending",          # pending | reviewed | unhappy | exhausted
        "sentiment": None,
    })
    _save(tenant_id, rows)

    return (
        f"{customer_name} enrolled in the review sequence for {b['name']}. "
        f"First request sends in {SEQUENCE_DAYS[0]} day(s), then day "
        f"{SEQUENCE_DAYS[1]} and day {SEQUENCE_DAYS[2]} if no review comes in."
    )


# ── SKILL: log satisfaction (the sentiment gate) ───────────────
def log_satisfaction(cfg: dict, customer_name: str = "",
                     happy: bool = True, notes: str = "") -> str:
    """
    Records whether a customer is happy. This is the gate that decides
    whether they get sent to a public review page or routed privately
    to the owner.
    """
    tenant_id = cfg["tenant_id"]
    owner = cfg["business"]["owner"]
    review_url = cfg.get("reviews", {}).get("google_review_url", "")

    rows = _load(tenant_id)
    match = None
    for r in rows:
        if r["customer_name"].lower() == customer_name.lower():
            match = r
            break

    if match is None:
        match = {
            "customer_name": customer_name,
            "phone": "",
            "job_type": "",
            "enrolled_at": datetime.now(timezone.utc).isoformat(),
            "sends_completed": 0,
            "next_send": None,
            "status": "pending",
            "sentiment": None,
        }
        rows.append(match)

    match["sentiment"] = "positive" if happy else "negative"
    match["notes"] = notes

    if happy:
        match["status"] = "pending"
        _save(tenant_id, rows)
        link = f" Send them here: {review_url}" if review_url else ""
        return (
            f"{customer_name} is happy. Cleared to receive a public review "
            f"request.{link}"
        )

    # Unhappy — never send to a public review site
    match["status"] = "unhappy"
    match["next_send"] = None
    _save(tenant_id, rows)
    return (
        f"{customer_name} is NOT happy. Pulled from the public review sequence. "
        f"Flagged for {owner} to call personally today. Notes: {notes or 'none given'}"
    )


# ── SKILL: what's due to send ──────────────────────────────────
def get_pending_reviews(cfg: dict) -> str:
    """Returns everyone whose review request is due to send now."""
    tenant_id = cfg["tenant_id"]
    now = datetime.now(timezone.utc)
    rows = _load(tenant_id)

    due, unhappy = [], []
    for r in rows:
        if r["status"] == "unhappy":
            unhappy.append(r)
            continue
        if r["status"] != "pending" or not r.get("next_send"):
            continue
        try:
            if datetime.fromisoformat(r["next_send"]) <= now:
                due.append(r)
        except Exception:
            continue

    lines = []
    if due:
        lines.append(f"{len(due)} review request(s) due to send:")
        for r in due:
            n = r["sends_completed"] + 1
            lines.append(f"- {r['customer_name']} ({r['phone']}) — send #{n}, {r['job_type']}")
    else:
        lines.append("No review requests due right now.")

    if unhappy:
        lines.append(f"\n{len(unhappy)} unhappy customer(s) needing a personal call:")
        for r in unhappy:
            lines.append(f"- {r['customer_name']} ({r['phone']}) — {r.get('notes','no notes')}")

    return "\n".join(lines)


# ── SKILL: mark a review as left ───────────────────────────────
def mark_reviewed(cfg: dict, customer_name: str = "") -> str:
    """Stops the sequence for someone who left a review."""
    tenant_id = cfg["tenant_id"]
    rows = _load(tenant_id)
    for r in rows:
        if r["customer_name"].lower() == customer_name.lower():
            r["status"] = "reviewed"
            r["next_send"] = None
            _save(tenant_id, rows)
            return f"{customer_name} marked as reviewed. Sequence stopped."
    return f"No review record found for {customer_name}."


# ── Advance the sequence (called by scheduler, not by the model) ──
def advance_sequence(tenant_id: str) -> list:
    """
    Moves everyone due forward one step and returns who should be messaged.
    Meant to be called by a daily cron/scheduler, not by the agent.
    """
    now = datetime.now(timezone.utc)
    rows = _load(tenant_id)
    to_send = []

    for r in rows:
        if r["status"] != "pending" or not r.get("next_send"):
            continue
        try:
            if datetime.fromisoformat(r["next_send"]) > now:
                continue
        except Exception:
            continue

        to_send.append(dict(r))
        r["sends_completed"] += 1

        if r["sends_completed"] >= len(SEQUENCE_DAYS):
            r["status"] = "exhausted"
            r["next_send"] = None
        else:
            enrolled = datetime.fromisoformat(r["enrolled_at"])
            r["next_send"] = (
                enrolled + timedelta(days=SEQUENCE_DAYS[r["sends_completed"]])
            ).isoformat()

    _save(tenant_id, rows)
    return to_send


def build_review_message(cfg: dict, customer_name: str, attempt: int) -> str:
    """Generates the actual SMS copy for a given attempt number."""
    b = cfg["business"]
    url = cfg.get("reviews", {}).get("google_review_url", "")
    first = customer_name.split()[0] if customer_name else "there"

    if attempt == 0:
        return (
            f"Hey {first}, {b['owner']} here from {b['short_name']}. Thanks again "
            f"for the work — hope you're happy with how it turned out. If you've "
            f"got 30 seconds, a quick review would mean a lot to us: {url}"
        )
    if attempt == 1:
        return (
            f"Hey {first}, just following up — if you had a good experience with "
            f"{b['short_name']}, a quick review really helps a small local shop "
            f"like ours: {url}"
        )
    return (
        f"Hi {first}, last time I'll bug you about it — if you've got a minute "
        f"for a review we'd really appreciate it: {url} "
        f"Either way, thanks for the business."
    )
