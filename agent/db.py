"""SQLite storage: calls, measured reply turns, bookings, prompt versions, do-not-call list."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from agent.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,              -- browser | phone | simulated
    caller TEXT,                        -- phone number (phone channel only)
    started_at TEXT NOT NULL,           -- attempt: websocket opened / phone answered
    connected_at TEXT,                  -- caller actually heard the agent (first bot audio)
    ended_at TEXT,
    end_reason TEXT,                    -- agent_hangup | caller_hangup | error | timeout | idle | capacity | blocked
    agent_outcome TEXT,                 -- what the agent decided (its intent detection)
    contact_name TEXT,
    contact_title TEXT,
    persona TEXT,
    institution TEXT,
    contact_email TEXT,
    interest_level TEXT,
    next_step TEXT,                     -- detailed wrap-up step; agent_outcome is derived from it
    opted_out INTEGER,
    meeting_time TEXT,
    callback_time TEXT,
    summary TEXT,
    transcript TEXT NOT NULL DEFAULT '[]',
    recording TEXT,
    error TEXT,
    llm_model TEXT,
    -- filled in by a human reviewer on the dashboard
    reviewed_outcome TEXT,
    corrected_user_text TEXT,           -- what the caller really said (STT accuracy reference)
    reviewer_notes TEXT,
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    call_id TEXT NOT NULL REFERENCES calls(id),
    kind TEXT NOT NULL,                 -- greeting | response
    latency_ms INTEGER NOT NULL,        -- caller stopped speaking -> agent audio starts
    stt_ms INTEGER, llm_ms INTEGER, tts_ms INTEGER,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    call_id TEXT,
    created_at TEXT NOT NULL,
    start_at TEXT NOT NULL,             -- ISO 8601 with offset
    end_at TEXT NOT NULL,
    contact_name TEXT, contact_title TEXT, institution TEXT, contact_email TEXT, persona TEXT,
    backend TEXT NOT NULL,              -- google | local
    external_id TEXT,                   -- Google event id
    meet_link TEXT, html_link TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed'   -- confirmed | cancelled
);
CREATE TABLE IF NOT EXISTS prompt_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    note TEXT,
    doc TEXT NOT NULL                   -- JSON prompt document (see agent/prompt.py)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS dnc (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                 -- phone | email | institution
    value TEXT NOT NULL,
    reason TEXT, call_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kind, value)
);
"""

# Columns added to calls after the first release; added in place to older databases.
ADDED_COLUMNS = (
    "institution", "contact_email", "interest_level", "next_step", "opted_out",
    "prompt_version INTEGER", "seriousness", "reviewed_persona", "booking_id",
    "duration_secs REAL", "llm_in_tokens INTEGER", "llm_out_tokens INTEGER", "tts_chars INTEGER",
    "cost_inr REAL", "filler_count INTEGER", "runtime_settings", "tester",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with db() as c:
        ensure_schema(c)


def ensure_schema(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    existing = {r[1] for r in c.execute("PRAGMA table_info(calls)")}
    for col in ADDED_COLUMNS:
        if col.split()[0] not in existing:
            c.execute(f"ALTER TABLE calls ADD COLUMN {col}")


# --- calls ------------------------------------------------------------------

def insert_call(call_id: str, channel: str, caller: str | None, tester: str | None = None) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO calls (id, channel, caller, tester, started_at) VALUES (?, ?, ?, ?, ?)",
            (call_id, channel, caller, tester, now()),
        )


def update_call(call_id: str, **fields) -> None:
    if not fields:
        return
    for key in ("transcript", "runtime_settings"):
        if key in fields and fields[key] is not None and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE calls SET {cols} WHERE id = ?", (*fields.values(), call_id))


def insert_turn(call_id: str, kind: str, latency_ms: int, stt_ms=None, llm_ms=None, tts_ms=None) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO turns (call_id, kind, latency_ms, stt_ms, llm_ms, tts_ms, at) VALUES (?,?,?,?,?,?,?)",
            (call_id, kind, latency_ms, stt_ms, llm_ms, tts_ms, now()),
        )


def all_calls() -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM calls ORDER BY started_at DESC").fetchall()
    return [decode_call(r) for r in rows]


def get_call(call_id: str) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        turns = c.execute("SELECT * FROM turns WHERE call_id = ? ORDER BY at", (call_id,)).fetchall()
    if r is None:
        return None
    out = decode_call(r)
    out["turns"] = [dict(t) for t in turns]
    return out


def all_turns() -> list[dict]:
    with db() as c:
        return [dict(t) for t in c.execute("SELECT * FROM turns").fetchall()]


def decode_call(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["transcript"] = json.loads(d["transcript"] or "[]")
    if d.get("runtime_settings"):
        d["runtime_settings"] = json.loads(d["runtime_settings"])
    return d


# --- bookings ---------------------------------------------------------------

BOOKING_FIELDS = (
    "id", "call_id", "start_at", "end_at", "contact_name", "contact_title", "institution",
    "contact_email", "persona", "backend", "external_id", "meet_link", "html_link",
)


def insert_booking(**fields) -> None:
    cols = [k for k in BOOKING_FIELDS if k in fields]
    with db() as c:
        c.execute(
            f"INSERT INTO bookings (created_at, {', '.join(cols)}) VALUES (?, {', '.join('?' for _ in cols)})",
            (now(), *(fields[k] for k in cols)),
        )


def bookings(include_cancelled: bool = True) -> list[dict]:
    q = "SELECT * FROM bookings" + ("" if include_cancelled else " WHERE status = 'confirmed'") + " ORDER BY start_at"
    with db() as c:
        return [dict(r) for r in c.execute(q).fetchall()]


def get_booking(booking_id: str) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    return dict(r) if r else None


def set_booking_status(booking_id: str, status: str) -> None:
    with db() as c:
        c.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))


# --- prompt versions --------------------------------------------------------

def insert_prompt_version(doc: dict, note: str | None) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO prompt_versions (created_at, note, doc) VALUES (?, ?, ?)",
            (now(), note, json.dumps(doc, ensure_ascii=False)),
        )
        return int(cur.lastrowid)


def prompt_versions() -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT id, created_at, note FROM prompt_versions ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_prompt_version(version: int) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM prompt_versions WHERE id = ?", (version,)).fetchone()
    if r is None:
        return None
    out = dict(r)
    out["doc"] = json.loads(out["doc"])
    return out


def kv_get(key: str) -> str | None:
    with db() as c:
        r = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def kv_set(key: str, value: str) -> None:
    with db() as c:
        c.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# --- do-not-call ------------------------------------------------------------

def dnc_add(kind: str, value: str, reason: str | None = None, call_id: str | None = None) -> bool:
    value = normalize_dnc(kind, value)
    if not value:
        return False
    with db() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO dnc (kind, value, reason, call_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (kind, value, reason, call_id, now()),
        )
        return cur.rowcount > 0


def dnc_list() -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM dnc ORDER BY created_at DESC").fetchall()]


def dnc_remove(entry_id: int) -> None:
    with db() as c:
        c.execute("DELETE FROM dnc WHERE id = ?", (entry_id,))


def dnc_contains(kind: str, value: str | None) -> bool:
    value = normalize_dnc(kind, value)
    if not value:
        return False
    with db() as c:
        return c.execute("SELECT 1 FROM dnc WHERE kind = ? AND value = ?", (kind, value)).fetchone() is not None


def normalize_dnc(kind: str, value: str | None) -> str:
    value = (value or "").strip()
    if kind == "phone":
        digits = "".join(ch for ch in value if ch.isdigit())
        return digits[-10:] if len(digits) >= 10 else digits  # compare on the national number
    return " ".join(value.lower().split())
