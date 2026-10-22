"""HTTP + WebSocket server.

  GET  /                 browser call page (anyone with the link can call, free)
  WS   /ws/browser       browser call audio
  POST /twilio/voice     Twilio "A call comes in" webhook (optional phone path)
  WS   /ws/twilio        Twilio media stream
  GET  /dashboard        KPI dashboard + call review          (password protected, as is
  GET  /studio           prompt studio: edit, preview, test    everything under /api)
  GET  /admin            settings, meetings, do-not-call list
  GET  /api/export.zip   test results bundle (see agent/export.py)
  GET  /results.zip      the same, without a password, only when the tester launcher started us

Run:  python -m agent.server
"""

import asyncio
import base64
import json
import secrets
import shutil
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from loguru import logger
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pydantic import BaseModel

from agent import db, export, kpis, meetings, playground, prompt, runtime
from agent.browser_serializer import BrowserPCMSerializer
from agent.call import Call, run_call
from agent.config import settings
from agent.kpis import ALL_OUTCOMES
from agent.prompt import NEXT_STEP_OUTCOME, VARIABLES

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(_app):
    db.init()
    if not settings.sarvam_api_key:
        logger.warning("SARVAM_API_KEY is not set - calls will fail until you add it to .env")
    yield


app = FastAPI(title="DOT Sarvam voice agent", lifespan=_lifespan)
security = HTTPBasic()
_active = 0
_lock = asyncio.Lock()


def _auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
    if not secrets.compare_digest(creds.password.encode(), settings.dashboard_password.encode()):
        raise HTTPException(401, "Wrong password", headers={"WWW-Authenticate": "Basic"})
    return creds.username or "reviewer"


async def _reserve() -> bool:
    global _active
    async with _lock:
        if _active >= settings.max_concurrent_calls:
            return False
        _active += 1
        return True


async def _release() -> None:
    global _active
    async with _lock:
        _active = max(0, _active - 1)


def _transport(ws: WebSocket, serializer) -> FastAPIWebsocketTransport:
    return FastAPIWebsocketTransport(
        websocket=ws,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
            session_timeout=runtime.load()["max_call_seconds"] + 30,
        ),
    )


async def _refuse(call_id: str, channel: str, caller: str | None, reason: str) -> None:
    db.insert_call(call_id, channel, caller)
    db.update_call(call_id, ended_at=db.now(), end_reason=reason)


# --- browser calls ---------------------------------------------------------

@app.get("/")
async def call_page():
    return FileResponse(STATIC / "call.html")


@app.get("/demo")
async def demo_page():
    return FileResponse(STATIC / "demo.html")


DEMO_FIELDS = (
    "end_reason", "agent_outcome", "next_step", "outcome_source", "persona", "interest_level", "seriousness",
    "contact_name", "contact_title", "institution", "contact_email", "meeting_time", "callback_time", "opted_out",
    "summary", "duration_secs", "cost_inr", "llm_in_tokens", "llm_out_tokens", "tts_chars", "filler_count",
    "llm_model", "prompt_version", "started_at", "connected_at", "ended_at",
)


@app.get("/api/demo/latest")
async def api_demo_latest(_user: str = Depends(_auth)):
    """For the presenter's watch view: the newest call and its transcript so far (password protected,
    since through a share link anyone could otherwise read whoever called last)."""
    calls = db.all_calls()
    if not calls:
        return {"id": None}
    call = db.get_call(calls[0]["id"])
    return {"id": call["id"], "transcript": call.get("transcript") or [], "ended": bool(call.get("ended_at"))}


@app.get("/api/demo/calls/{call_id}")
async def api_demo_call(call_id: str):
    """One call's KPIs for the demo page, live while it runs and complete once ended_at is set.
    No password: only the page that placed the call knows its id (a random UUID)."""
    call = db.get_call(call_id)
    if call is None:
        raise HTTPException(404)
    out = {k: call.get(k) for k in DEMO_FIELDS}
    out["final_outcome"] = kpis.final_outcome(call)
    out["turns"] = [{k: t.get(k) for k in ("kind", "latency_ms", "stt_ms", "llm_ms", "tts_ms")} for t in call["turns"]]
    out["user_turns"] = sum(1 for m in call.get("transcript") or [] if m.get("role") == "user")
    return out


@app.websocket("/ws/browser")
async def browser_ws(ws: WebSocket):
    await ws.accept()
    call_id = str(uuid.uuid4())
    if not settings.sarvam_api_key:
        await _refuse(call_id, "browser", None, "error")
        await ws.send_text(json.dumps({"type": "error", "text": "Server has no SARVAM_API_KEY configured."}))
        await ws.close()
        return
    if not await _reserve():
        await _refuse(call_id, "browser", None, "capacity")
        await ws.send_text(json.dumps({"type": "error", "text": "All lines are busy. Try again in a minute."}))
        await ws.close()
        return
    # scripts/simulate_call.py connects with ?channel=simulated so its calls can be filtered out.
    channel = "simulated" if ws.query_params.get("channel") == "simulated" else "browser"
    tester = (ws.query_params.get("tester") or "").strip()[:80] or None
    try:
        call = Call(id=call_id, channel=channel, tester=tester, send_captions=True)
        await run_call(_transport(ws, BrowserPCMSerializer()), call)
    finally:
        await _release()


# --- phone calls (Twilio) --------------------------------------------------

@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    form = await request.form()
    caller = escape(str(form.get("From", "")), {'"': "&quot;"})
    host = request.headers.get("x-forwarded-host") or request.headers["host"]
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Connect><Stream url="wss://{host}/ws/twilio"><Parameter name="caller" value="{caller}"/></Stream></Connect></Response>"""
    return Response(twiml, media_type="application/xml")


@app.websocket("/ws/twilio")
async def twilio_ws(ws: WebSocket):
    await ws.accept()
    start = None
    async for raw in ws.iter_text():  # "connected" then "start"
        msg = json.loads(raw)
        if msg.get("event") == "start":
            start = msg["start"]
            break
    if start is None:
        return
    caller = (start.get("customParameters") or {}).get("caller")
    call_id = start.get("callSid") or str(uuid.uuid4())
    if not settings.sarvam_api_key or not await _reserve():
        await _refuse(call_id, "phone", caller, "capacity" if settings.sarvam_api_key else "error")
        await ws.close()
        return
    can_hang_up = bool(settings.twilio_account_sid and settings.twilio_auth_token)
    serializer = TwilioFrameSerializer(
        stream_sid=start["streamSid"],
        call_sid=start.get("callSid"),
        account_sid=settings.twilio_account_sid or None,
        auth_token=settings.twilio_auth_token or None,
        params=TwilioFrameSerializer.InputParams(auto_hang_up=can_hang_up),
    )
    try:
        await run_call(_transport(ws, serializer), Call(id=call_id, channel="phone", caller=caller))
    finally:
        await _release()


# --- pages ------------------------------------------------------------------

ASSETS = {"app.css": "text/css", "app.js": "text/javascript"}


@app.get("/assets/{name}")
async def asset(name: str):
    if name not in ASSETS:
        raise HTTPException(404)
    return FileResponse(STATIC / name, media_type=ASSETS[name])


@app.get("/dashboard")
async def dashboard(_user: str = Depends(_auth)):
    return FileResponse(STATIC / "dashboard.html")


@app.get("/studio")
async def studio(_user: str = Depends(_auth)):
    return FileResponse(STATIC / "studio.html")


@app.get("/admin")
async def admin(_user: str = Depends(_auth)):
    return FileResponse(STATIC / "admin.html")


# --- KPIs and calls ---------------------------------------------------------

def _calls_for(version: int | None, channel: str | None) -> list[dict]:
    calls = db.all_calls()
    if version is not None:
        calls = [c for c in calls if c.get("prompt_version") == version]
    if channel:
        calls = [c for c in calls if c["channel"] == channel]
    return calls


@app.get("/api/kpis")
async def api_kpis(version: int | None = None, channel: str | None = None, _user: str = Depends(_auth)):
    calls, turns = _calls_for(version, channel), db.all_turns()
    out = kpis.summary(calls, turns, settings.latency_target_ms, settings.latency_working_target_ms)
    out["by_prompt_version"] = kpis.by_prompt_version(_calls_for(None, channel), turns, settings.latency_target_ms)
    out["targets"] = {"latency_ms": settings.latency_target_ms, "working_latency_ms": settings.latency_working_target_ms}
    return out


@app.get("/api/calls")
async def api_calls(version: int | None = None, channel: str | None = None, _user: str = Depends(_auth)):
    calls = _calls_for(version, channel)
    for c in calls:
        c["final_outcome"] = kpis.final_outcome(c)
        c.pop("transcript")
        c.pop("runtime_settings", None)
    return calls


@app.get("/api/calls.csv")
async def api_calls_csv(version: int | None = None, channel: str | None = None, _user: str = Depends(_auth)):
    return Response(export.calls_csv(_calls_for(version, channel)), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="calls.csv"'})


async def _bundle_response() -> FileResponse:
    tmp = Path(tempfile.mkdtemp())
    path = await asyncio.to_thread(export.write_bundle, tmp / export.bundle_name())
    return FileResponse(path, media_type="application/zip", filename=path.name,
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))


@app.get("/api/export.zip")
async def api_export(_user: str = Depends(_auth)):
    return await _bundle_response()


# --- tester kit (scripts/launch.py runs the server on 127.0.0.1 with TESTER_MODE=1) ---

@app.get("/api/tester")
async def api_tester(name: str | None = None):
    """Public: lets the call page show the tester's progress and detect the server going away."""
    if not settings.tester_mode:
        return {"tester_mode": False}
    calls = [c for c in db.all_calls() if c["channel"] == "browser" and c.get("connected_at")]
    mine = [c for c in calls if name and (c.get("tester") or "").lower() == name.strip().lower()]
    return {"tester_mode": True, "calls": len(calls), "my_calls": len(mine), "has_key": bool(settings.sarvam_api_key)}


@app.get("/results.zip")
async def tester_results():
    if not settings.tester_mode:
        raise HTTPException(404)
    return await _bundle_response()


@app.get("/api/calls/{call_id}")
async def api_call(call_id: str, _user: str = Depends(_auth)):
    call = db.get_call(call_id)
    if call is None:
        raise HTTPException(404)
    call["final_outcome"] = kpis.final_outcome(call)
    call["heard_user_text"] = kpis.heard_user_text(call)
    if call.get("corrected_user_text"):
        edits, words = kpis.word_edits(call["corrected_user_text"], call["heard_user_text"])
        call["wer"] = round(edits / words, 4) if words else None
    if call.get("booking_id"):
        call["booking"] = db.get_booking(call["booking_id"])
    return call


class Review(BaseModel):
    reviewed_outcome: str | None = None
    reviewed_persona: str | None = None
    corrected_user_text: str | None = None
    reviewer_notes: str | None = None


@app.post("/api/calls/{call_id}/review")
async def api_review(call_id: str, review: Review, user: str = Depends(_auth)):
    if db.get_call(call_id) is None:
        raise HTTPException(404)
    if review.reviewed_outcome and review.reviewed_outcome not in ALL_OUTCOMES:
        raise HTTPException(400, f"outcome must be one of {ALL_OUTCOMES}")
    db.update_call(
        call_id,
        reviewed_outcome=review.reviewed_outcome or None,
        reviewed_persona=(review.reviewed_persona or "").strip() or None,
        corrected_user_text=(review.corrected_user_text or "").strip() or None,
        reviewer_notes=review.reviewer_notes or None,
        reviewed_at=db.now(),
    )
    return {"ok": True, "reviewer": user}


@app.get("/api/recordings/{name}")
async def recording(name: str, _user: str = Depends(_auth)):
    path = (settings.recordings_dir / name).resolve()
    if path.parent != settings.recordings_dir.resolve() or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/wav")


# --- prompt studio ----------------------------------------------------------

def _values(doc: dict) -> dict:
    at = meetings.now()
    return prompt.variables(doc, meetings.calendar_days_text(at), meetings.today_text(at))


def _preview(doc: dict) -> dict:
    doc = prompt.normalize(doc)
    values = _values(doc)
    body = playground.request_body(doc, runtime.load(), values)
    system = body["messages"][0]["content"]
    return {
        "errors": prompt.validate(doc),
        "system_prompt": system,
        "sections": prompt.render_parts(doc, values),
        "request": body,
        "endpoint": playground.base_url(body["model"]) + "/chat/completions",
        "stats": {"chars": len(system), "words": len(system.split()), "approx_tokens": round(len(system) / 4)},
        "persona_tags": prompt.persona_tags(doc),
    }


@app.get("/api/prompt")
async def api_prompt(_user: str = Depends(_auth)):
    version, doc = prompt.active()
    return {
        "active_version": version,
        "doc": doc,
        "versions": db.prompt_versions(),
        "variables": [{"name": k, "help": v, "value": _values(doc)[k]} for k, v in VARIABLES.items()],
        "kinds": list(prompt.KINDS),
        "next_steps": NEXT_STEP_OUTCOME,
    }


@app.get("/api/prompt/versions/{version}")
async def api_prompt_version(version: int, _user: str = Depends(_auth)):
    row = db.get_prompt_version(version)
    if row is None:
        raise HTTPException(404)
    row["doc"] = prompt.normalize(row["doc"])
    return row


class PromptSave(BaseModel):
    doc: dict
    note: str | None = None
    activate: bool = True


@app.post("/api/prompt")
async def api_prompt_save(body: PromptSave, _user: str = Depends(_auth)):
    try:
        version = prompt.save(body.doc, body.note, body.activate)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True, "version": version, "active_version": prompt.active()[0]}


@app.post("/api/prompt/activate/{version}")
async def api_prompt_activate(version: int, _user: str = Depends(_auth)):
    try:
        prompt.activate(version)
    except KeyError:
        raise HTTPException(404) from None
    return {"ok": True, "active_version": version}


@app.post("/api/prompt/reset")
async def api_prompt_reset(_user: str = Depends(_auth)):
    version = prompt.save(prompt.default_doc(), "Reset to factory default (script.yaml)")
    return {"ok": True, "version": version}


@app.get("/api/prompt/preview")
async def api_prompt_preview_active(_user: str = Depends(_auth)):
    return _preview(prompt.active()[1])


class PromptDraft(BaseModel):
    doc: dict


@app.post("/api/prompt/preview")
async def api_prompt_preview(body: PromptDraft, _user: str = Depends(_auth)):
    return _preview(body.doc)


class PlaygroundTurn(BaseModel):
    doc: dict | None = None        # unsaved draft; default = active prompt
    messages: list[dict] = []      # conversation so far, OpenAI format (after the system prompt)
    state: dict = {}


@app.post("/api/playground")
async def api_playground(body: PlaygroundTurn, _user: str = Depends(_auth)):
    if not settings.sarvam_api_key:
        raise HTTPException(400, "SARVAM_API_KEY is not set.")
    doc = prompt.normalize(body.doc) if body.doc else prompt.active()[1]
    errors = prompt.validate(doc)
    if errors:
        raise HTTPException(400, " ".join(errors))
    try:
        return await playground.chat(doc, runtime.load(), body.messages, dict(body.state))
    except Exception as e:  # noqa: BLE001 - show the API error in the studio
        logger.exception("playground turn failed")
        raise HTTPException(502, f"{type(e).__name__}: {e}") from None


# --- runtime settings -------------------------------------------------------

@app.get("/api/settings")
async def api_settings(_user: str = Depends(_auth)):
    return {"values": runtime.load(), "schema": runtime.schema()}


@app.post("/api/settings")
async def api_settings_save(changes: dict, _user: str = Depends(_auth)):
    try:
        return {"ok": True, "values": runtime.save(changes)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/settings/reset")
async def api_settings_reset(_user: str = Depends(_auth)):
    settings.runtime_path.unlink(missing_ok=True)
    return {"ok": True, "values": runtime.load()}


class VoicePreview(BaseModel):
    text: str | None = None
    voice: str | None = None
    pace: float | None = None
    language: str | None = None
    model: str | None = None


@app.post("/api/voice-preview")
async def api_voice_preview(body: VoicePreview, _user: str = Depends(_auth)):
    """Speak a line in the chosen voice (Sarvam REST TTS, ~₹0.3 per preview)."""
    if not settings.sarvam_api_key:
        raise HTTPException(400, "SARVAM_API_KEY is not set.")
    rt = runtime.load()
    payload = {
        "text": (body.text or prompt.opening_line())[:500],
        "target_language_code": body.language or rt["tts_language"],
        "speaker": body.voice or rt["tts_voice"],
        "model": body.model or rt["tts_model"],
        "pace": body.pace or rt["tts_pace"],
        "speech_sample_rate": 22050,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.sarvam.ai/text-to-speech", json=payload,
                              headers={"api-subscription-key": settings.sarvam_api_key})
    if r.status_code != 200:
        raise HTTPException(502, f"Sarvam TTS: {r.text[:300]}")
    return Response(base64.b64decode(r.json()["audios"][0]), media_type="audio/wav")


# --- meetings ---------------------------------------------------------------

@app.get("/api/calendar")
async def api_calendar(_user: str = Depends(_auth)):
    return {
        **await asyncio.to_thread(meetings.status),
        "timezone": settings.timezone,
        "meeting_minutes": settings.meeting_minutes,
        "hours": f"{settings.working_hours_start}-{settings.working_hours_end}",
        "days": settings.working_days,
        "team_emails": settings.meeting_team_emails,
    }


@app.get("/api/availability")
async def api_availability(date: str, part_of_day: str = "any", _user: str = Depends(_auth)):
    return await asyncio.to_thread(meetings.check_availability, date, part_of_day)


@app.get("/api/bookings")
async def api_bookings(_user: str = Depends(_auth)):
    rows = db.bookings()
    for b in rows:
        b["say"] = meetings.spoken(datetime.fromisoformat(b["start_at"]))
    return rows


@app.post("/api/bookings/{booking_id}/cancel")
async def api_booking_cancel(booking_id: str, _user: str = Depends(_auth)):
    try:
        return await asyncio.to_thread(meetings.cancel, booking_id)
    except KeyError:
        raise HTTPException(404) from None


@app.get("/api/bookings/{booking_id}.ics")
async def api_booking_ics(booking_id: str, _user: str = Depends(_auth)):
    b = db.get_booking(booking_id)
    if b is None:
        raise HTTPException(404)
    return Response(meetings.ics(b), media_type="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="meeting-{booking_id}.ics"'})


# --- do-not-call list -------------------------------------------------------

@app.get("/api/dnc")
async def api_dnc(_user: str = Depends(_auth)):
    return db.dnc_list()


class DncEntry(BaseModel):
    kind: str
    value: str
    reason: str | None = None


@app.post("/api/dnc")
async def api_dnc_add(entry: DncEntry, _user: str = Depends(_auth)):
    if entry.kind not in ("phone", "email", "institution") or not entry.value.strip():
        raise HTTPException(400, "kind must be phone, email or institution, with a value")
    added = db.dnc_add(entry.kind, entry.value, entry.reason or "Added on dashboard")
    return {"ok": True, "added": added}


@app.delete("/api/dnc/{entry_id}")
async def api_dnc_remove(entry_id: int, _user: str = Depends(_auth)):
    db.dnc_remove(entry_id)
    return {"ok": True}


@app.get("/api/dnc/check")
async def api_dnc_check(phone: str | None = None, email: str | None = None, institution: str | None = None,
                        _user: str = Depends(_auth)):
    """For a future outbound dialer: may this contact be called?"""
    hits = [k for k, v in (("phone", phone), ("email", email), ("institution", institution)) if v and db.dnc_contains(k, v)]
    return {"allowed": not hits, "blocked_by": hits}


def main() -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    if settings.tester_mode:
        # Testers aren't developers: keep their window calm. Everything still goes to the log file.
        logger.remove()
        logger.add(sys.stderr, level="ERROR", format="<red>{message}</red>")
    logger.add(settings.logs_dir / "server_{time:YYYY-MM-DD}.log", level="DEBUG", rotation="20 MB",
               retention=20, encoding="utf-8", enqueue=True)
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=True, forwarded_allow_ips="*",
                log_level="error" if settings.tester_mode else "info")


if __name__ == "__main__":
    main()
