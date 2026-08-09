"""
Insight Engine
───────────────
This is the part competitors don't build.

Everyone captures leads. Almost nobody tells the owner what the leads MEAN.
This module turns raw conversation + lead data into findings the owner can
act on Monday morning.

Design principles:
  1. Never show a number without a "so what" — a stat with no action is noise
  2. Only surface findings backed by enough data to be real
  3. Write it the way you'd say it out loud, not like a dashboard
"""

from collections import Counter
from datetime import datetime, timezone, timedelta

import storage

# Don't state a pattern as fact below this many data points
MIN_SAMPLE = 3


# ── Storage ────────────────────────────────────────────────────
def log_conversation(tenant_id: str, session_id: str, messages: list,
                     captured_lead: bool = False):
    """
    Records a conversation for later analysis. This is the raw material
    the insight engine runs on.
    """
    user_msgs = [m["content"] for m in messages
                 if m.get("role") == "user" and isinstance(m.get("content"), str)]
    now = datetime.now(timezone.utc)

    existing = storage.get_one(tenant_id, "conversations", session_id)
    row = dict(existing) if existing else {
        "session_id": session_id,
        "started_at": now.isoformat(),
        "hour_utc": now.hour,
    }
    row["user_messages"] = user_msgs
    row["turns"] = len(user_msgs)
    row["captured_lead"] = captured_lead or row.get("captured_lead", False)

    storage.upsert(tenant_id, "conversations", session_id, row)


def _within(iso_str: str, days: int) -> bool:
    try:
        ts = datetime.fromisoformat(iso_str)
        return (datetime.now(timezone.utc) - ts) <= timedelta(days=days)
    except Exception:
        return False


# ── Analysis ───────────────────────────────────────────────────
def _demand_mix(leads: list, cfg: dict) -> dict:
    """What people actually want vs what the business markets."""
    types = Counter(
        (l.get("project_type") or "unspecified").strip().title()
        for l in leads if l.get("project_type")
    )
    if not types:
        return {}

    total = sum(types.values())
    top, top_n = types.most_common(1)[0]
    share = round(top_n / total * 100)

    listed = len(cfg.get("services", []))
    finding = {
        "headline": f"{share}% of your inquiries were {top.lower()} work",
        "detail": (
            f"Out of {total} inquiries, {top_n} were {top.lower()}. "
            f"You advertise {listed} services, but demand is concentrated."
        ),
        "action": (
            f"Lead with {top.lower()} in your ads and on your site. "
            f"You'll convert better selling the thing people already want you for."
        ),
        "breakdown": dict(types.most_common()),
    }
    return finding


def _geography(leads: list, cfg: dict) -> dict:
    """Where demand is actually coming from."""
    area = cfg["business"].get("service_area", [])
    hits = Counter()

    for l in leads:
        blob = " ".join([
            str(l.get("project_details", "")),
            str(l.get("notes", "")),
        ]).lower()
        for town in area:
            if town.lower() in blob:
                hits[town] += 1

    if not hits:
        return {}

    total = sum(hits.values())
    top, n = hits.most_common(1)[0]
    home = area[0] if area else ""

    finding = {
        "headline": f"{top} showed up in {n} of {total} located inquiries",
        "detail": f"Location mentions: " + ", ".join(f"{t} ({c})" for t, c in hits.most_common(5)),
        "breakdown": dict(hits.most_common()),
    }

    if top.lower() != home.lower():
        finding["action"] = (
            f"{top} is producing real demand and it's not your home base. "
            f"A targeted push there is the cheapest growth available to you."
        )
    else:
        finding["action"] = (
            f"Demand is concentrated in {home}. Surrounding towns are "
            f"underpenetrated — that's your expansion room."
        )
    return finding


def _timing(convos: list, cfg: dict) -> dict:
    """When people reach out vs when the business is reachable."""
    if not convos:
        return {}

    after_hours = 0
    for c in convos:
        h = c.get("hour_utc")
        if h is None:
            continue
        # UTC -> approx America/Indiana/Indianapolis (UTC-4/-5)
        local = (h - 4) % 24
        if local < 7 or local >= 17:
            after_hours += 1

    total = len(convos)
    if total < MIN_SAMPLE:
        return {}

    pct = round(after_hours / total * 100)
    hours = cfg["business"].get("hours", "")

    return {
        "headline": f"{pct}% of inquiries came in outside business hours",
        "detail": (
            f"{after_hours} of {total} conversations started before 7am or "
            f"after 5pm. Your posted hours: {hours}"
        ),
        "action": (
            "Every one of those would have hit voicemail or gone to whoever "
            "answered first. This is the single clearest thing the agent is "
            "already saving you."
        ) if pct >= 30 else (
            "Most people reach you during hours, so after-hours coverage is "
            "insurance rather than your main win."
        ),
    }


def _drop_off(convos: list) -> dict:
    """People who engaged but never left contact info."""
    if len(convos) < MIN_SAMPLE:
        return {}

    engaged = [c for c in convos if c.get("turns", 0) >= 2]
    if not engaged:
        return {}

    lost = [c for c in engaged if not c.get("captured_lead")]
    pct = round(len(lost) / len(engaged) * 100)
    noun = "person" if len(lost) == 1 else "people"
    verb = "asked" if len(lost) == 1 else "asked"

    return {
        "headline": f"{len(lost)} {noun} {verb} real questions but never left contact info",
        "detail": (
            f"{len(engaged)} conversations went past a single message. "
            f"{len(lost)} of those ({pct}%) ended without a name or number."
        ),
        "action": (
            "These are warm prospects who went and called someone else. "
            "Worth tightening how early the agent asks for a number."
        ) if pct > 40 else (
            "Capture rate is healthy — most engaged visitors are converting to leads."
        ),
        "topics": [c["user_messages"][0][:90] for c in lost[:5] if c.get("user_messages")],
    }


def _questions(convos: list) -> dict:
    """What people actually ask — often reveals a missing page or service."""
    words = Counter()
    stop = {
        "the","a","an","and","or","but","is","are","was","how","what","do","does",
        "can","you","your","i","my","me","we","for","to","of","in","on","it","that",
        "this","with","have","has","need","want","get","would","like","much","if",
        "just","about","there","here","be","been","im","its","yes","no","ok","okay",
        "thanks","thank","hi","hey","hello","looking","also","some","any",
    }
    for c in convos:
        for m in c.get("user_messages", []):
            for w in "".join(ch if ch.isalnum() else " " for ch in m.lower()).split():
                if len(w) > 3 and w not in stop:
                    words[w] += 1

    common = [(w, n) for w, n in words.most_common(12) if n >= MIN_SAMPLE]
    if not common:
        return {}

    return {
        "headline": "What people keep asking about",
        "detail": ", ".join(f"{w} ({n}x)" for w, n in common[:8]),
        "action": (
            "Anything showing up repeatedly deserves its own section on the "
            "website. Repeat questions are free SEO and fewer phone calls."
        ),
    }


def _reputation(cfg: dict, tenant_id: str) -> dict:
    """Review pipeline health, including the unhappy-customer save."""
    rows = storage.get_all(tenant_id, "reviews")
    if not rows:
        return {}

    reviewed = sum(1 for r in rows if r.get("status") == "reviewed")
    unhappy = [r for r in rows if r.get("status") == "unhappy"]
    pending = sum(1 for r in rows if r.get("status") == "pending")

    finding = {
        "headline": f"{reviewed} review(s) collected, {pending} still in sequence",
        "detail": f"{len(rows)} completed jobs entered the review pipeline.",
    }

    if unhappy:
        finding["action"] = (
            f"{len(unhappy)} customer(s) flagged unhappy and pulled from public "
            f"review requests before they could post. Each one of those is a "
            f"1-star you didn't get."
        )
        finding["saved"] = [
            {"name": r["customer_name"], "issue": r.get("notes", "")}
            for r in unhappy
        ]
    else:
        finding["action"] = "No unhappy customers flagged. Everyone cleared to be asked."

    return finding


# ── Report assembly ────────────────────────────────────────────
def generate_report(cfg: dict, days: int = 30) -> dict:
    """Builds the full insight report for a client."""
    tenant_id = cfg["tenant_id"]
    b = cfg["business"]

    all_leads = storage.get_all(tenant_id, "leads")
    all_convos = storage.get_all(tenant_id, "conversations")

    leads = [l for l in all_leads if _within(l.get("captured_at", ""), days)]
    convos = [c for c in all_convos if _within(c.get("started_at", ""), days)]

    findings = {}
    for key, fn in [
        ("demand_mix", lambda: _demand_mix(leads, cfg)),
        ("geography", lambda: _geography(leads, cfg)),
        ("timing", lambda: _timing(convos, cfg)),
        ("drop_off", lambda: _drop_off(convos)),
        ("questions", lambda: _questions(convos)),
        ("reputation", lambda: _reputation(cfg, tenant_id)),
    ]:
        try:
            result = fn()
            if result:
                findings[key] = result
        except Exception as e:
            print(f"[insight] {key} failed: {e}")

    conv_rate = round(len(leads) / len(convos) * 100) if convos else 0

    return {
        "business": b["name"],
        "owner": b["owner"],
        "period_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "conversations": len(convos),
            "leads_captured": len(leads),
            "conversion_rate_pct": conv_rate,
        },
        "findings": findings,
        "has_enough_data": len(convos) >= MIN_SAMPLE,
    }


def format_report_text(report: dict) -> str:
    """Plain-text version — what actually gets emailed to the owner."""
    t = report["totals"]
    lines = [
        f"{report['business'].upper()} — LAST {report['period_days']} DAYS",
        "=" * 52,
        "",
        f"Conversations: {t['conversations']}",
        f"Leads captured: {t['leads_captured']}",
        f"Conversion rate: {t['conversion_rate_pct']}%",
        "",
    ]

    if not report["has_enough_data"]:
        lines.append(
            "Not enough activity yet for reliable patterns. This report gets "
            "sharper every week as more conversations come in."
        )
        return "\n".join(lines)

    lines.append("WHAT THE DATA SHOWS")
    lines.append("-" * 52)
    lines.append("")

    for f in report["findings"].values():
        lines.append(f"→ {f['headline']}")
        if f.get("detail"):
            lines.append(f"  {f['detail']}")
        if f.get("action"):
            lines.append(f"  WHAT TO DO: {f['action']}")
        lines.append("")

    return "\n".join(lines)
