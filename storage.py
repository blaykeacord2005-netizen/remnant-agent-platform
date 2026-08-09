"""
Storage Layer
──────────────
All persistence goes through here. Two backends:

  1. Supabase (production) — survives redeploys, used when SUPABASE_URL and
     SUPABASE_KEY are set
  2. Local disk (development) — automatic fallback, zero config

The rest of the app doesn't know or care which is active. Same interface either way.

TENANT SCOPING: every method takes tenant_id as the first argument and filters
on it. There is no way to read a table without specifying a tenant.

── Supabase setup ──
Run this SQL once in the Supabase SQL editor:

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
"""

import os
import json
from datetime import datetime, timezone
from typing import Optional

BASE = os.path.dirname(__file__)
DISK_DIR = os.path.join(BASE, "_localdata")
os.makedirs(DISK_DIR, exist_ok=True)

_supabase = None
_backend = "disk"


def _init():
    """Connects to Supabase if credentials exist, otherwise uses disk."""
    global _supabase, _backend
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip() or os.getenv("SUPABASE_SERVICE_KEY", "").strip()

    if url and key:
        try:
            from supabase import create_client
            _supabase = create_client(url, key)
            _backend = "supabase"
            print("[storage] Supabase connected — data persists across redeploys")
            return
        except Exception as e:
            print(f"[storage] Supabase init failed ({e}) — falling back to disk")

    print("[storage] Using local disk. Set SUPABASE_URL + SUPABASE_KEY for production.")


_init()


def backend() -> str:
    return _backend


# ── Disk helpers ───────────────────────────────────────────────
def _disk_path(tenant_id: str, kind: str) -> str:
    safe = "".join(c for c in f"{tenant_id}_{kind}" if c.isalnum() or c in "_-")
    return os.path.join(DISK_DIR, f"{safe}.json")


def _disk_read(tenant_id: str, kind: str) -> list:
    p = _disk_path(tenant_id, kind)
    if not os.path.exists(p):
        return []
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return []


def _disk_write(tenant_id: str, kind: str, rows: list):
    with open(_disk_path(tenant_id, kind), "w") as f:
        json.dump(rows, f, indent=2)


# ── Public interface ───────────────────────────────────────────
def get_all(tenant_id: str, kind: str) -> list:
    """Every record of a kind for one tenant."""
    if _backend == "supabase":
        try:
            res = (_supabase.table("agent_records")
                   .select("data")
                   .eq("tenant_id", tenant_id)
                   .eq("kind", kind)
                   .order("created_at")
                   .execute())
            return [r["data"] for r in (res.data or [])]
        except Exception as e:
            print(f"[storage] read failed ({e}) — using disk")
    return _disk_read(tenant_id, kind)


def get_one(tenant_id: str, kind: str, record_key: str) -> Optional[dict]:
    """One record by key, or None."""
    if _backend == "supabase":
        try:
            res = (_supabase.table("agent_records")
                   .select("data")
                   .eq("tenant_id", tenant_id)
                   .eq("kind", kind)
                   .eq("record_key", record_key)
                   .limit(1)
                   .execute())
            rows = res.data or []
            return rows[0]["data"] if rows else None
        except Exception as e:
            print(f"[storage] read failed ({e}) — using disk")

    for r in _disk_read(tenant_id, kind):
        if r.get("_key") == record_key:
            return r
    return None


def upsert(tenant_id: str, kind: str, record_key: str, data: dict) -> dict:
    """Insert or update a record. record_key makes it idempotent."""
    now = datetime.now(timezone.utc).isoformat()
    data = dict(data)
    data["_key"] = record_key
    data["tenant_id"] = tenant_id
    data.setdefault("created_at", now)
    data["updated_at"] = now

    if _backend == "supabase":
        try:
            (_supabase.table("agent_records")
             .upsert({
                 "tenant_id": tenant_id,
                 "kind": kind,
                 "record_key": record_key,
                 "data": data,
                 "updated_at": now,
             }, on_conflict="tenant_id,kind,record_key")
             .execute())
            return data
        except Exception as e:
            print(f"[storage] upsert failed ({e}) — using disk")

    rows = _disk_read(tenant_id, kind)
    for i, r in enumerate(rows):
        if r.get("_key") == record_key:
            rows[i] = data
            break
    else:
        rows.append(data)
    _disk_write(tenant_id, kind, rows)
    return data


def delete_all(tenant_id: str, kind: str):
    """Wipe one kind for one tenant. Used for demo resets."""
    if _backend == "supabase":
        try:
            (_supabase.table("agent_records")
             .delete()
             .eq("tenant_id", tenant_id)
             .eq("kind", kind)
             .execute())
            return
        except Exception as e:
            print(f"[storage] delete failed ({e}) — using disk")
    _disk_write(tenant_id, kind, [])


# ── Conversation persistence ───────────────────────────────────
def save_conversation(tenant_id: str, session_id: str, messages: list):
    """
    Persists chat history so a server restart doesn't lose active conversations.
    Only text content is stored — tool_use blocks aren't JSON-serializable.
    """
    clean = [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if isinstance(m.get("content"), str)
    ]
    upsert(tenant_id, "chat_history", session_id, {"messages": clean})


def load_conversation(tenant_id: str, session_id: str) -> list:
    rec = get_one(tenant_id, "chat_history", session_id)
    return rec.get("messages", []) if rec else []
