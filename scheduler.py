"""
Scheduler
──────────
The sequences were previously "built" but nothing ever ran them. This is the
loop that actually fires them.

Runs every 30 minutes:
  1. Advance follow-up sequences → send day 1 / 3 / 7 messages to cold leads
  2. Advance review sequences   → send day 1 / 4 / 9 review requests
  3. Alert the owner about any unhappy customer needing a personal call

Runs across ALL tenants, but every send is scoped to one tenant's config.
"""

import asyncio
import traceback
from typing import Optional
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import load_client, list_clients
import notifications
import followup_engine
import review_engine

# Optional[...] instead of "X | None" so this runs on Python 3.9
scheduler: Optional[AsyncIOScheduler] = None


def _run_followups(tenant_id: str, cfg: dict) -> int:
    """Sends any follow-up messages that came due."""
    sent = 0
    for lead in followup_engine.advance_followups(tenant_id):
        attempt = lead.get("sends_completed", 0)
        msg = followup_engine.build_followup_message(
            cfg,
            lead.get("customer_name", ""),
            attempt,
            lead.get("project_type", ""),
        )
        notifications.send_sms(tenant_id, lead.get("phone", ""), msg)
        sent += 1
    return sent


def _run_reviews(tenant_id: str, cfg: dict) -> int:
    """Sends any review requests that came due."""
    sent = 0
    for cust in review_engine.advance_sequence(tenant_id):
        attempt = cust.get("sends_completed", 0)
        msg = review_engine.build_review_message(
            cfg, cust.get("customer_name", ""), attempt
        )
        notifications.send_sms(tenant_id, cust.get("phone", ""), msg)
        sent += 1
    return sent


def _alert_unhappy(tenant_id: str, cfg: dict) -> int:
    """
    Tells the owner about unhappy customers immediately. This is the highest
    value alert in the system — it's a 1-star review that hasn't happened yet.
    """
    rows = review_engine._load(tenant_id)
    alerted = 0

    for r in rows:
        if r.get("status") != "unhappy" or r.get("owner_alerted"):
            continue

        b = cfg["business"]
        n = cfg.get("notifications", {})
        name = r.get("customer_name", "A customer")
        notes = r.get("notes", "no details given")

        notifications.send_email(
            tenant_id,
            n.get("email_to", ""),
            f"Call {name} today — unhappy customer",
            (
                f"{name} indicated they weren't happy with their job.\n\n"
                f"Phone: {r.get('phone','—')}\n"
                f"What they said: {notes}\n\n"
                f"They've been pulled from review requests so they can't be "
                f"prompted to post publicly. Calling them today is how this "
                f"stays a private conversation instead of a public review.\n\n"
                f"— {b['name']} automated alert"
            ),
        )
        notifications.send_sms(
            tenant_id, n.get("sms_to", ""),
            f"Unhappy customer: {name} ({r.get('phone','?')}). "
            f"Pulled from review requests. Call them today."
        )

        r["owner_alerted"] = True
        alerted += 1

    if alerted:
        review_engine._save(tenant_id, rows)
    return alerted


async def run_cycle():
    """One full pass across every tenant."""
    stamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    totals = {"followups": 0, "reviews": 0, "alerts": 0}

    for tenant_id in list_clients():
        try:
            cfg = load_client(tenant_id)
            totals["followups"] += _run_followups(tenant_id, cfg)
            totals["reviews"] += _run_reviews(tenant_id, cfg)
            totals["alerts"] += _alert_unhappy(tenant_id, cfg)
        except Exception:
            print(f"[scheduler] {tenant_id} failed:\n{traceback.format_exc()}")

    if any(totals.values()):
        print(f"[scheduler] {stamp} — {totals['followups']} follow-ups, "
              f"{totals['reviews']} review requests, {totals['alerts']} owner alerts")
    return totals


def start():
    global scheduler
    if scheduler:
        return scheduler
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(run_cycle, "interval", minutes=30,
                      id="sequences", max_instances=1, coalesce=True)
    scheduler.start()
    print("[scheduler] running — sequences fire every 30 min")
    return scheduler


def stop():
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        scheduler = None
