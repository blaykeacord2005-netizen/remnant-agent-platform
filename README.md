# Remnant Co — Multi-Tenant Client Agent Platform

One codebase. Many clients. Each client is a YAML config.

## Adding a new client (2 minutes)

1. Copy `clients/rock_solid.yaml` → `clients/newclient.yaml`
2. Change the values (business info, services, facts, branding colors)
3. Push. Their agent is live at `/newclient`

No code changes. No new repo. No new deployment.

## Structure

```
main.py              FastAPI app + agent loop (tenant-scoped)
config.py            Loads client YAML, builds system prompts
skills.py            Shared, client-agnostic skills
clients/*.yaml       One file per client — all client data lives here
templates/widget.html  Auto-themes from each client's branding config
leads/               Captured leads, one file per tenant
```

## Routes

| Route | What it does |
|---|---|
| `/{tenant}` | The client's branded chat widget |
| `/{tenant}/api/chat` | Chat endpoint (tenant-scoped) |
| `/{tenant}/api/leads` | Leads for that client only |
| `/admin` | List all clients + lead counts |
| `/health` | Health check |

## Deploy

1. Push to GitHub
2. Railway → New Project → Deploy from GitHub
3. Add env var: `ANTHROPIC_API_KEY`
4. Rock Solid's agent is live at `your-app.railway.app/rock_solid`

## Tenant isolation

Every skill call receives the tenant's config explicitly. Skills never look up
global state. Sessions are namespaced `tenant::session`. Leads are stored in
per-tenant files. One client can never reach another client's data.

## Notes

- Leads currently save to disk — Railway wipes disk on redeploy.
  Move to Supabase before real production use.
- Conversations are in-memory. Fine for demos, move to Redis at scale.

## The insight layer (the differentiator)

Everyone builds chat widgets and review automation. Almost nobody tells the
owner what their data *means*.

`insight_engine.py` analyzes every conversation and lead to surface findings
the owner can act on:

- **Demand mix** — what people actually want vs what they advertise
- **Geography** — which towns are producing demand they aren't marketing to
- **Timing** — how much traffic arrives outside business hours
- **Drop-off** — engaged visitors who left without giving contact info
- **Repeat questions** — what keeps coming up (free SEO signal)
- **Reputation saves** — unhappy customers caught before they posted publicly

Routes:
- `GET /{tenant}/report` — plain text, email-ready
- `GET /{tenant}/api/insights` — JSON

Every finding pairs a stat with a "WHAT TO DO". A number with no action is noise.
