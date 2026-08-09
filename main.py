"""
Remnant Co — Multi-Tenant Client Agent Platform
────────────────────────────────────────────────
One codebase. Many clients. Each client is a YAML config in clients/.

PUBLIC   /{tenant}                 branded chat widget
PUBLIC   /{tenant}/api/chat        chat endpoint (rate limited)
PUBLIC   /{tenant}/api/config      branding only, no sensitive data

PRIVATE  /{tenant}/api/leads       requires ADMIN_TOKEN
PRIVATE  /{tenant}/api/insights    requires ADMIN_TOKEN
PRIVATE  /{tenant}/report          requires ADMIN_TOKEN
PRIVATE  /{tenant}/api/outbox      requires ADMIN_TOKEN
PRIVATE  /admin                    requires ADMIN_TOKEN
"""

import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from anthropic import AsyncAnthropic

import storage
import notifications
import scheduler as sched
from config import load_client, list_clients, build_system_prompt
from skills import get_tool_schemas, call_skill
from insight_engine import log_conversation, generate_report, format_report_text

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
