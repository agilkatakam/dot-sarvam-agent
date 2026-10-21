"""Test results bundle: everything a tester sends back, in one zip.

  calls.csv            one row per call (outcome, persona, latency inputs, cost, transcript)
  transcripts/*.txt    each call as a readable conversation, with its outcome and summary
  calls.db             the full SQLite database (open it with this app's dashboard, or any SQLite tool)
  recordings/*.wav     call audio (left = caller, right = agent)
  logs/*.log           server logs, with the Sarvam API key removed
  runtime.json         dashboard settings in use

Used by GET /api/export.zip (dashboard button) and `python -m scripts.export`.
"""

import csv
import io
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from agent import db, kpis
from agent.config import settings

CSV_COLUMNS = (
    "id", "tester", "channel", "caller", "started_at", "connected_at", "ended_at", "end_reason", "duration_secs",
    "prompt_version", "llm_model", "agent_outcome", "final_outcome", "next_step", "persona", "reviewed_persona",
    "seriousness", "interest_level", "contact_name", "contact_title", "institution", "contact_email",
    "meeting_time", "callback_time", "opted_out", "summary", "cost_inr", "llm_in_tokens", "llm_out_tokens",
    "tts_chars", "filler_count", "reviewed_outcome", "reviewer_notes", "error",
)


def calls_csv(calls: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=(*CSV_COLUMNS, "transcript"), extrasaction="ignore")
    w.writeheader()
    for c in calls:
        c = dict(c, final_outcome=kpis.final_outcome(c))
        c["transcript"] = "\n".join(f"{m['role']}: {m['text']}" for m in c["transcript"])
        w.writerow(c)
    return buf.getvalue()


def transcript_text(call: dict, turns: list[dict]) -> str:
    latencies = [t["latency_ms"] for t in turns if t["call_id"] == call["id"]]
    head = [
        f"Call {call['id']}",
        f"Tester: {call.get('tester') or '-'}    Channel: {call['channel']}    Prompt version: {call.get('prompt_version')}",
        f"Started: {call['started_at']}    Duration: {call.get('duration_secs')} s    Ended by: {call.get('end_reason')}",
        f"Outcome: {kpis.final_outcome(call)}    Persona: {call.get('persona')}    Seriousness: {call.get('seriousness')}",
        f"Reply latency (ms): {', '.join(map(str, latencies)) or '-'}",
        f"Cost: ₹{call.get('cost_inr')}",
    ]
    if call.get("summary"):
        head.append(f"Summary: {call['summary']}")
    if call.get("error"):
        head.append(f"Error: {call['error']}")
    lines = [f"{m['role'].upper():>6}: {m['text']}" for m in call["transcript"]]
    return "\n".join(head) + "\n\n" + "\n".join(lines) + "\n"


def _scrub(text: str) -> str:
    key = settings.sarvam_api_key
    return text.replace(key, "[SARVAM_API_KEY]") if key else text


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "tester"


def default_folder() -> Path:
    """Where a tester will look for a saved file: their Downloads folder."""
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def bundle_name() -> str:
    testers = sorted({c["tester"] for c in db.all_calls() if c.get("tester")})
    who = safe_name("+".join(testers)) if testers else "tester"
    return f"dot-test-results-{who}-{datetime.now():%Y%m%d-%H%M}.zip"


def write_bundle(dest: Path) -> Path:
    calls, turns = db.all_calls(), db.all_turns()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("calls.csv", calls_csv(calls))
        for c in calls:
            z.writestr(f"transcripts/{c['started_at'][:19].replace(':', '')}_{c['id'][:8]}.txt", transcript_text(c, turns))
        # A consistent copy even if a call is writing to the database right now.
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "calls.db"
            src, dst = sqlite3.connect(settings.db_path), sqlite3.connect(copy)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
            z.write(copy, "calls.db")
        if settings.recordings_dir.exists():
            for wav in sorted(settings.recordings_dir.glob("*.wav")):
                z.write(wav, f"recordings/{wav.name}", compress_type=zipfile.ZIP_STORED)
        if settings.logs_dir.exists():
            for log in sorted(settings.logs_dir.glob("*.log*")):
                z.writestr(f"logs/{log.name}", _scrub(log.read_text(encoding="utf-8", errors="replace")))
        if settings.runtime_path.exists():
            z.write(settings.runtime_path, "runtime.json")
    return dest
