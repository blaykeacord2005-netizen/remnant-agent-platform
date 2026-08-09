# Setup

## Local (demo)

```bash
pip3 install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export ADMIN_TOKEN=pick-any-long-random-string
python3 -m uvicorn main:app --port 8000
```

Chat widget: http://localhost:8000/rock_solid

Private endpoints need the token:
```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" localhost:8000/rock_solid/api/leads
curl -H "Authorization: Bearer $ADMIN_TOKEN" localhost:8000/rock_solid/report
curl -H "Authorization: Bearer $ADMIN_TOKEN" localhost:8000/rock_solid/api/outbox
```

## Production (Railway)

### Required
| Var | Why |
|---|---|
| `ANTHROPIC_API_KEY` | the agent |
| `ADMIN_TOKEN` | without it, all private endpoints return 503 |

### Persistence — do this before a paying client
| Var | Why |
|---|---|
| `SUPABASE_URL` | data survives redeploys |
| `SUPABASE_KEY` | service role key |

Run once in the Supabase SQL editor:

```sql
create table if not exists agent_records (
  id          bigserial primary key,
  tenant_id   text not null,
  kind        text not null,
  record_key  text not null,
  data        jsonb not null,
  created_at  timestamptz default now(),
  updated_at  timestamptz default now(),
  unique (tenant_id, kind, record_key)
);
create index if not exists agent_records_lookup
  on agent_records (tenant_id, kind);
```

Confirm it worked: `GET /health` should report `"storage": "supabase"`.

### Delivery — needed for messages to actually send
Email (pick one):
- `RESEND_API_KEY` + `FROM_EMAIL` — easiest
- `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` / `SMTP_PORT` — Gmail app password works

SMS (pick one):
- `TELNYX_API_KEY` + `TELNYX_PHONE_NUMBER`
- `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` + `TWILIO_PHONE_NUMBER`

Without these, messages queue to `/{tenant}/api/outbox` marked
`queued_no_provider`. Nothing silently claims to have sent.

## Adding a client

1. Copy `clients/rock_solid.yaml` → `clients/newclient.yaml`
2. Change the values
3. Push

Live at `/newclient`. No code changes.

## Python version

Runs on Python 3.9+. If you see a `TypeError: unsupported operand type(s) for |`
you're on an older Python than the code expects — this version is patched for 3.9,
which is what ships on macOS.

Check yours: `python3 --version`
