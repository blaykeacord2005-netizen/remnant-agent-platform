"""
Notifications
──────────────
Actually delivers messages. Previously the system claimed "owner notified"
without sending anything — this fixes that.

Providers (all optional, degrade gracefully):
  Email : Resend (RESEND_API_KEY) → SMTP (SMTP_HOST/USER/PASS) → queue only
  SMS   : Telnyx (TELNYX_API_KEY) → Twilio (TWILIO_*) → queue only

If nothing is configured, messages are queued to storage and visible at
/{tenant}/api/outbox so you can see exactly what WOULD have sent. Nothing
silently claims success.
"""

import os
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone

import httpx

import storage


def _queue(tenant_id: str, channel: str, to: str, subject: str,
           body: str, status: str, error: str = ""):
    """Every send attempt is recorded, successful or not."""
    key = f"{channel}_{to}_{datetime.now(timezone.utc).timestamp()}"
    storage.upsert(tenant_id, "outbox", key, {
        "channel": channel,
        "to": to,
        "subject": subject,
        "body": body,
        "status": status,
        "error": error,
        "attempted_at": datetime.now(timezone.utc).isoformat(),
    })


# ── EMAIL ──────────────────────────────────────────────────────
def send_email(tenant_id: str, to: str, subject: str, body: str) -> dict:
    if not to:
        return {"sent": False, "reason": "no recipient configured"}

    resend_key = os.getenv("RESEND_API_KEY", "").strip()
    from_addr = os.getenv("FROM_EMAIL", "").strip() or "onboarding@resend.dev"

    if resend_key:
        try:
            r = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}",
                         "Content-Type": "application/json"},
                json={"from": from_addr, "to": [to],
                      "subject": subject, "text": body},
                timeout=12.0,
            )
            if r.status_code < 300:
                _queue(tenant_id, "email", to, subject, body, "sent")
                return {"sent": True, "provider": "resend"}
            _queue(tenant_id, "email", to, subject, body, "failed", r.text[:200])
            return {"sent": False, "reason": f"resend {r.status_code}"}
        except Exception as e:
            _queue(tenant_id, "email", to, subject, body, "failed", str(e)[:200])

    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    pw = os.getenv("SMTP_PASS", "").strip()

    if host and user and pw:
        try:
            msg = EmailMessage()
            msg["From"] = user
            msg["To"] = to
            msg["Subject"] = subject
            msg.set_content(body)
            with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as s:
                s.starttls()
                s.login(user, pw)
                s.send_message(msg)
            _queue(tenant_id, "email", to, subject, body, "sent")
            return {"sent": True, "provider": "smtp"}
        except Exception as e:
            _queue(tenant_id, "email", to, subject, body, "failed", str(e)[:200])
            return {"sent": False, "reason": str(e)[:120]}

    _queue(tenant_id, "email", to, subject, body, "queued_no_provider")
    return {"sent": False, "reason": "no email provider configured"}


# ── SMS ────────────────────────────────────────────────────────
def send_sms(tenant_id: str, to: str, body: str) -> dict:
    if not to:
        return {"sent": False, "reason": "no recipient configured"}

    telnyx_key = os.getenv("TELNYX_API_KEY", "").strip()
    telnyx_from = os.getenv("TELNYX_PHONE_NUMBER", "").strip()

    if telnyx_key and telnyx_from:
        try:
            r = httpx.post(
                "https://api.telnyx.com/v2/messages",
                headers={"Authorization": f"Bearer {telnyx_key}",
                         "Content-Type": "application/json"},
                json={"from": telnyx_from, "to": to, "text": body},
                timeout=12.0,
            )
            if r.status_code < 300:
                _queue(tenant_id, "sms", to, "", body, "sent")
                return {"sent": True, "provider": "telnyx"}
            _queue(tenant_id, "sms", to, "", body, "failed", r.text[:200])
        except Exception as e:
            _queue(tenant_id, "sms", to, "", body, "failed", str(e)[:200])

    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    tok = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    tw_from = os.getenv("TWILIO_PHONE_NUMBER", "").strip()

    if sid and tok and tw_from:
        try:
            r = httpx.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                auth=(sid, tok),
                data={"From": tw_from, "To": to, "Body": body},
                timeout=12.0,
            )
            if r.status_code < 300:
                _queue(tenant_id, "sms", to, "", body, "sent")
                return {"sent": True, "provider": "twilio"}
            _queue(tenant_id, "sms", to, "", body, "failed", r.text[:200])
            return {"sent": False, "reason": f"twilio {r.status_code}"}
        except Exception as e:
            _queue(tenant_id, "sms", to, "", body, "failed", str(e)[:200])
            return {"sent": False, "reason": str(e)[:120]}

    _queue(tenant_id, "sms", to, "", body, "queued_no_provider")
    return {"sent": False, "reason": "no SMS provider configured"}


# ── Owner alert on a new lead ──────────────────────────────────
def notify_new_lead(cfg: dict, lead: dict) -> dict:
    tenant_id = cfg["tenant_id"]
    b = cfg["business"]
    n = cfg.get("notifications", {})

    subject = f"New lead — {lead.get('name','Unknown')} ({lead.get('project_type','general')})"
    body = (
        f"NEW LEAD — {b['name']}\n"
        f"{'=' * 44}\n\n"
        f"Name:     {lead.get('name','—')}\n"
        f"Phone:    {lead.get('phone','—')}\n"
        f"Project:  {lead.get('project_type','—')}\n"
        f"Details:  {lead.get('project_details','—')}\n"
        f"Timeline: {lead.get('timeline','—')}\n\n"
        f"Captured: {lead.get('created_at','just now')}\n\n"
        f"Call them back today — speed wins these jobs.\n"
    )

    results = {
        "email": send_email(tenant_id, n.get("email_to", ""), subject, body),
        "sms": send_sms(
            tenant_id, n.get("sms_to", ""),
            f"New lead: {lead.get('name','?')} — {lead.get('project_type','?')} "
            f"— {lead.get('phone','?')}. Call them back."
        ),
    }
    return results


def get_outbox(tenant_id: str, limit: int = 50) -> list:
    rows = storage.get_all(tenant_id, "outbox")
    rows.sort(key=lambda r: r.get("attempted_at", ""), reverse=True)
    return rows[:limit]
