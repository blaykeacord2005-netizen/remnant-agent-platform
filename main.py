"""
Remnant Co — Multi-Tenant Client Agent Platform
────────────────────────────────────────────────
One codebase. Many clients. Each client is a YAML config in clients/.

PUBLIC   /{tenant}                 branded chat widget
PUBLIC   /{tenant}/api/chat        chat endpoint (rate limited)
PUBLIC   /{tenant}/api/config      branding only, no sensitive data
PUBLIC   /{tenant}/api/twilio/call-status   missed-call text-back (Twilio-signed)
PUBLIC   /{tenant}/api/twilio/sms           inbound SMS, stops follow-ups + forwards reply to owner (Twilio-signed)

PRIVATE  /{tenant}/api/leads       requires ADMIN_TOKEN
PRIVATE  /{tenant}/api/insights    requires ADMIN_TOKEN
PRIVATE  /{tenant}/report          requires ADMIN_TOKEN
PRIVATE  /{tenant}/api/outbox      requires ADMIN_TOKEN
PRIVATE  /admin                    requires ADMIN_TOKEN
"""

import base64
import hashlib
import hmac
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, HTTPException, Header, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from anthropic import AsyncAnthropic

import storage
import notifications
import followup_engine
import scheduler as sched
from config import load_client, list_clients, build_system_prompt
from skills import get_tool_schemas, call_skill, capture_lead, _norm_phone, LEADS_KIND
from insight_engine import log_conversation, generate_report, format_report_text

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

# Twilio signs the exact public URL it called. Behind Railway's proxy that is
# the https URL, so set this to e.g. https://your-app.railway.app
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

MISSED_CALL_STATUSES = {"no-answer", "busy", "failed", "canceled"}
DEFAULT_MISSED_CALL_SMS = "Sorry we missed your call! This is {short_name} — how can we help?"
# What Twilio puts in From when the caller hides their number
ANONYMOUS_CALLERS = {"+266696687", "+86282452253"}

RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))
RATE_WINDOW = 60
_hits: dict = defaultdict(deque)

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))


@asynccontextmanager
async def lifespan(app: FastAPI):
    sched.start()
    yield
    sched.stop()


app = FastAPI(title="Remnant Co Agent Platform", lifespan=lifespan)


def _tenant_or_404(tenant_id: str) -> dict:
    try:
        return load_client(tenant_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown client: {tenant_id}")


def _require_admin(authorization):
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_TOKEN not set. Set it in env before exposing client data.",
        )
    supplied = (authorization or "").replace("Bearer ", "").strip()
    if supplied != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Slow down a moment.")
    q.append(now)


async def run_agent_turn(cfg: dict, messages: list, max_hops: int = 5) -> str:
    system = build_system_prompt(cfg)
    tools = get_tool_schemas()
    working = list(messages)
    owner = cfg["business"]["owner"]
    phone = cfg["business"]["phone"]

    for hop in range(max_hops):
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            tools=tools,
            messages=working,
        )

        text_parts = [b.text for b in resp.content
                      if b.type == "text" and b.text.strip()]

        if resp.stop_reason != "tool_use":
            if text_parts:
                return "\n".join(text_parts)
            print(f"[warn] empty response hop {hop}, stop_reason={resp.stop_reason}")
            working.append({
                "role": "user",
                "content": "Give the customer a short, natural reply now.",
            })
            continue

        working.append({"role": "assistant", "content": resp.content})

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                try:
                    out = call_skill(block.name, cfg, block.input)
                except Exception as e:
                    print(f"[skill error] {block.name}: {e}")
                    out = f"That lookup failed. Tell the customer {owner} will follow up."
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(out),
                })
        working.append({"role": "user", "content": results})

    return (f"Let me have {owner} follow up with you on that directly — "
            f"you can also reach us at {phone}.")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "storage": storage.backend(),
        "scheduler": sched.scheduler is not None,
        "tenants": list_clients(),
    }


@app.post("/{tenant_id}/api/chat")
async def chat(tenant_id: str, request: Request):
    cfg = _tenant_or_404(tenant_id)
    _rate_limit(request)

    body = await request.json()
    message = (body.get("message") or "").strip()
    session_id = body.get("session_id", "default")

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)
    if len(message) > 2000:
        return JSONResponse({"error": "message too long"}, status_code=400)

    history = storage.load_conversation(tenant_id, session_id)
    history.append({"role": "user", "content": message})
    history = history[-40:]

    try:
        reply = await run_agent_turn(cfg, history)
        history.append({"role": "assistant", "content": reply})
        storage.save_conversation(tenant_id, session_id, history)

        try:
            captured = storage.get_all(tenant_id, "leads")
            log_conversation(tenant_id, session_id, history, len(captured) > 0)
        except Exception as e:
            print(f"[insight log] {e}")

        return JSONResponse({"reply": reply})
    except Exception as e:
        print(f"[chat error] {e}")
        return JSONResponse({"error": "Something went wrong."}, status_code=500)


@app.get("/{tenant_id}/api/config")
async def public_config(tenant_id: str):
    cfg = _tenant_or_404(tenant_id)
    return JSONResponse({
        "business_name": cfg["business"]["name"],
        "short_name": cfg["business"]["short_name"],
        "agent_name": cfg["agent"]["name"],
        "greeting": cfg["agent"]["greeting"],
        "phone": cfg["business"]["phone"],
        "branding": cfg["branding"],
    })


@app.get("/admin")
async def admin(authorization: str = Header(None)):
    _require_admin(authorization)
    out = []
    for tid in list_clients():
        cfg = load_client(tid)
        out.append({
            "tenant_id": tid,
            "business": cfg["business"]["name"],
            "agent": cfg["agent"]["name"],
            "leads": len(storage.get_all(tid, "leads")),
            "conversations": len(storage.get_all(tid, "conversations")),
            "chat_url": f"/{tid}",
        })
    return JSONResponse({"storage": storage.backend(), "clients": out})


# NOTE: this catch-all must stay BELOW every literal route above it, or it
# will shadow them (/admin would resolve as a tenant named "admin").
@app.get("/{tenant_id}", response_class=HTMLResponse)
async def widget(tenant_id: str):
    _tenant_or_404(tenant_id)
    path = os.path.join(os.path.dirname(__file__), "templates", "widget.html")
    with open(path) as f:
        html = f.read()
    return HTMLResponse(html.replace("{{TENANT_ID}}", tenant_id))


@app.get("/{tenant_id}/api/leads")
async def leads(tenant_id: str, authorization: str = Header(None)):
    _require_admin(authorization)
    _tenant_or_404(tenant_id)
    rows = storage.get_all(tenant_id, "leads")
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return JSONResponse({"count": len(rows), "leads": rows})


@app.get("/{tenant_id}/api/insights")
async def insights(tenant_id: str, days: int = 30,
                   authorization: str = Header(None)):
    _require_admin(authorization)
    cfg = _tenant_or_404(tenant_id)
    return JSONResponse(generate_report(cfg, days))


@app.get("/{tenant_id}/report")
async def report(tenant_id: str, days: int = 30,
                 authorization: str = Header(None)):
    _require_admin(authorization)
    cfg = _tenant_or_404(tenant_id)
    return PlainTextResponse(format_report_text(generate_report(cfg, days)))


@app.get("/{tenant_id}/api/outbox")
async def outbox(tenant_id: str, authorization: str = Header(None)):
    _require_admin(authorization)
    _tenant_or_404(tenant_id)
    return JSONResponse({"messages": notifications.get_outbox(tenant_id)})


@app.post("/{tenant_id}/api/run-sequences")
async def run_now(tenant_id: str, authorization: str = Header(None)):
    _require_admin(authorization)
    _tenant_or_404(tenant_id)
    return JSONResponse(await sched.run_cycle())


# ── Twilio webhooks ────────────────────────────────────────────
async def _twilio_form(request: Request) -> dict:
    """
    Reads a Twilio webhook body and verifies X-Twilio-Signature. Fails closed:
    these routes text whatever number is in the payload, so unsigned requests
    must never get through.
    """
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="TWILIO_AUTH_TOKEN not set. Twilio webhooks are disabled until it is.",
        )

    raw = (await request.body()).decode("utf-8", errors="replace")
    fields = parse_qs(raw, keep_blank_values=True)

    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    base = PUBLIC_BASE_URL or f"{proto}://{request.headers.get('host', request.url.netloc)}"
    url = base + request.url.path + (f"?{request.url.query}" if request.url.query else "")

    signed = url + "".join(k + v for k in sorted(fields) for v in sorted(fields[k]))
    expected = base64.b64encode(
        hmac.new(token.encode(), signed.encode(), hashlib.sha1).digest()
    ).decode()
    supplied = request.headers.get("x-twilio-signature", "")
    if not hmac.compare_digest(expected.encode(), supplied.encode()):
        raise HTTPException(status_code=403, detail="Bad Twilio signature")

    return {k: v[0] for k, v in fields.items()}


class _BlankDict(dict):
    """format_map helper: unknown {placeholders} render as empty, not KeyError."""
    def __missing__(self, key):
        return ""


def _missed_call_text(cfg: dict) -> str:
    template = (cfg.get("missed_call") or {}).get("sms_template") or DEFAULT_MISSED_CALL_SMS
    values = _BlankDict(cfg["business"])
    try:
        return template.format_map(values)
    except (ValueError, IndexError, KeyError, AttributeError):
        return DEFAULT_MISSED_CALL_SMS.format_map(values)


def _usable_caller(caller: str) -> bool:
    digits = "".join(c for c in caller if c.isdigit())
    return (caller.startswith("+") and 10 <= len(digits) <= 15
            and caller not in ANONYMOUS_CALLERS)


def _recently_texted(tenant_id: str, phone_key: str, hours: float) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    for r in storage.get_all(tenant_id, "missed_calls"):
        if r.get("phone_key") != phone_key or r.get("sms") not in ("sent", "pending"):
            continue
        try:
            if datetime.fromisoformat(r["received_at"]) >= cutoff:
                return True
        except Exception:
            continue
    return False


def _process_missed_call(cfg: dict, rec: dict, suppress_sms: bool):
    """Text the caller, log them as a lead, start follow-up. Runs after the 200."""
    tenant_id = cfg["tenant_id"]
    mc = cfg.get("missed_call") or {}

    if suppress_sms:
        rec["sms"] = "skipped_cooldown"
    else:
        try:
            result = notifications.send_sms(
                tenant_id, rec["phone"], _missed_call_text(cfg),
                from_number=mc.get("sms_from", ""),
            )
            rec["sms"] = "sent" if result.get("sent") else "not_sent"
            rec["sms_detail"] = result.get("provider") or result.get("reason", "")
        except Exception as e:
            print(f"[missed call] sms failed: {e}")
            rec["sms"] = "not_sent"
            rec["sms_detail"] = str(e)[:120]

    # capture_lead dedups by phone, alerts the owner, and enrolls the follow-up
    # sequence — but only for a new lead, so repeat callers aren't re-enrolled.
    try:
        when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        capture_lead(cfg, phone=rec["phone"],
                     project_details=f"Missed call ({rec['status']}) on {when}")
        rec["lead"] = "captured"
    except Exception as e:
        print(f"[missed call] lead capture failed: {e}")
        rec["lead"] = "failed"

    storage.upsert(tenant_id, "missed_calls", rec["call_sid"], rec)


@app.post("/{tenant_id}/api/twilio/call-status")
async def twilio_call_status(tenant_id: str, request: Request,
                             background: BackgroundTasks):
    cfg = _tenant_or_404(tenant_id)
    params = await _twilio_form(request)

    mc = cfg.get("missed_call") or {}
    if not mc.get("enabled"):
        return JSONResponse({"status": "disabled"})

    # Terminal status can arrive as CallStatus (the dialed leg) or as
    # DialCallStatus (a <Dial> action callback on the parent call).
    status = next((s for s in (params.get("CallStatus"), params.get("DialCallStatus"))
                   if s in MISSED_CALL_STATUSES), None)
    if not status:
        return JSONResponse({"status": "ignored", "reason": "not a missed call"})

    call_sid = params.get("CallSid", "")
    caller = params.get("From", "").strip()
    if not call_sid:
        raise HTTPException(status_code=400, detail="CallSid missing")
    if params.get("Direction") == "outbound-api":
        return JSONResponse({"status": "ignored", "reason": "outbound call"})
    if not _usable_caller(caller):
        return JSONResponse({"status": "ignored", "reason": "no usable caller id"})

    key = _norm_phone(caller)
    own_numbers = {_norm_phone(n) for n in (
        mc.get("sms_from"),
        cfg["business"].get("phone"),
        (cfg.get("notifications") or {}).get("sms_to"),
    ) if n}
    if key in own_numbers:
        return JSONResponse({"status": "ignored", "reason": "caller is the business itself"})

    # Twilio retries on timeouts — one CallSid is handled once.
    if storage.get_one(tenant_id, "missed_calls", call_sid):
        return JSONResponse({"status": "duplicate"})

    suppress_sms = _recently_texted(tenant_id, key, float(mc.get("cooldown_hours", 24)))

    rec = {
        "call_sid": call_sid,
        "phone": caller,
        "phone_key": key,
        "to": params.get("To", ""),
        "status": status,
        "direction": params.get("Direction", ""),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sms": "pending",
    }
    # Claim the CallSid before the slow work so a retry sees it.
    storage.upsert(tenant_id, "missed_calls", call_sid, rec)
    background.add_task(_process_missed_call, cfg, rec, suppress_sms)
    return JSONResponse({"status": "accepted"})


@app.post("/{tenant_id}/api/twilio/sms")
async def twilio_inbound_sms(tenant_id: str, request: Request,
                             background: BackgroundTasks):
    cfg = _tenant_or_404(tenant_id)
    params = await _twilio_form(request)

    caller = params.get("From", "")
    stopped = followup_engine.stop_followup_by_phone(tenant_id, caller)
    if stopped:
        print(f"[sms] {tenant_id}: reply from ...{_norm_phone(caller)[-4:]} "
              f"stopped {stopped} follow-up(s)")

    # Forward the reply to the owner via the same path as new-lead alerts.
    # Skip the owner's own number so replying to an alert doesn't echo back.
    key = _norm_phone(caller)
    own_numbers = {_norm_phone(n) for n in (
        (cfg.get("notifications") or {}).get("sms_to"),
        (cfg.get("missed_call") or {}).get("sms_from"),
    ) if n}
    if key and key not in own_numbers:
        # Twilio retries on timeouts — one MessageSid is forwarded once.
        message_sid = params.get("MessageSid", "")
        if message_sid and storage.get_one(tenant_id, "inbound_sms", message_sid):
            return Response(content="<Response/>", media_type="text/xml")

        # Claim the MessageSid before the slow work so a retry sees it.
        if message_sid:
            storage.upsert(tenant_id, "inbound_sms", message_sid, {
                "message_sid": message_sid,
                "phone_key": key,
                "received_at": datetime.now(timezone.utc).isoformat(),
            })

        lead = storage.get_one(tenant_id, LEADS_KIND, key)
        background.add_task(notifications.notify_customer_reply,
                            cfg, caller, params.get("Body", ""), lead)

    # Empty TwiML: acknowledge without auto-replying to the customer.
    return Response(content="<Response/>", media_type="text/xml")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
