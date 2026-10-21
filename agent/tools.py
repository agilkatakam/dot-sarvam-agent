"""The agent's tools: check_availability and book_meeting (real calendar), end_call (wrap-up + hang up).

The persona enum in end_call comes from the active prompt, so adding a persona in the
prompt studio also teaches the agent to tag it.
"""

import asyncio
from datetime import datetime

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import EndWorkerFrame, FunctionCallResultProperties, TTSSpeakFrame
from pipecat.services.llm_service import FunctionCallParams

from agent import db, meetings
from agent.prompt import NEXT_STEP_OUTCOME, persona_tags

INTEREST = ["high", "medium", "low", "none", "unknown"]
SERIOUSNESS = ["genuine", "gatekeeper", "time_waster", "unclear"]

CHECK_AVAILABILITY = FunctionSchema(
    name="check_availability",
    description="Look up free walkthrough slots for one day. Only needed if the free times listed in your instructions don't suit them.",
    properties={
        "date": {"type": "string", "description": "The day, as YYYY-MM-DD, taken from the calendar list in your instructions."},
        "part_of_day": {"type": "string", "enum": ["any", "morning", "afternoon"], "description": "Narrow to morning or afternoon if they said so."},
    },
    required=["date"],
)

BOOK_MEETING = FunctionSchema(
    name="book_meeting",
    description="Book the walkthrough once the person has agreed to one of the free times.",
    properties={
        "start_iso": {"type": "string", "description": "Date and time of the agreed slot as YYYY-MM-DDTHH:MM, e.g. 2026-10-19T13:00 (from the [HH:MM] in your instructions or a start_iso from check_availability)."},
        "contact_name": {"type": "string", "description": "Who the meeting is with."},
        "contact_title": {"type": "string", "description": "Their role, if known."},
        "institution": {"type": "string", "description": "Institution name, if known."},
        "contact_email": {"type": "string", "description": "Their email for the calendar invite, if they gave one."},
    },
    required=["start_iso"],
)

FIELDS = (
    "contact_name", "contact_title", "contact_email", "institution", "persona",
    "interest_level", "seriousness", "meeting_time", "callback_time", "summary",
)


def end_call_schema(personas: list[str]) -> FunctionSchema:
    return FunctionSchema(
        name="end_call",
        description="Wrap up: record what you learned, say closing_line, and hang up. Call exactly once at the end of every call.",
        properties={
            "next_step": {"type": "string", "enum": list(NEXT_STEP_OUTCOME), "description": "How the call ended."},
            "closing_line": {"type": "string", "description": "Short goodbye confirming the next step."},
            "contact_name": {"type": "string", "description": "Person's name, if given."},
            "contact_title": {"type": "string", "description": "Their title or role, if given."},
            "contact_email": {"type": "string", "description": "Their email, if given."},
            "institution": {"type": "string", "description": "Institution name, if known."},
            "persona": {"type": "string", "enum": personas, "description": "Who you ended up speaking to."},
            "interest_level": {"type": "string", "enum": INTEREST},
            "seriousness": {
                "type": "string", "enum": SERIOUSNESS,
                "description": "genuine = engaged honestly; gatekeeper = reception doing their job; time_waster = joking, abusive or not engaging.",
            },
            "callback_time": {"type": "string", "description": "Best time to call back, only if they genuinely asked for a callback."},
            "summary": {"type": "string", "description": "One sentence summary of the call."},
        },
        required=["next_step", "closing_line", "persona", "interest_level", "seriousness"],
    )


def build_tools(doc: dict) -> ToolsSchema:
    return ToolsSchema(standard_tools=[CHECK_AVAILABILITY, BOOK_MEETING, end_call_schema(persona_tags(doc))])


def tools_json(doc: dict) -> list[dict]:
    """The tools exactly as sent to the API (OpenAI-compatible function format)."""
    return [{"type": "function", "function": s.to_default_dict()} for s in build_tools(doc).standard_tools]


def _clean(value) -> str | None:
    return str(value).strip()[:500] if value not in (None, "") else None


def apply_end_call(record: dict, args: dict, personas: list[str] | None = None) -> str:
    """Copy validated tool arguments onto the call record; returns the closing line."""
    step = args.get("next_step")
    if step not in NEXT_STEP_OUTCOME:
        step = "meeting_booked" if record.get("booking_id") else "interested_no_next_step"
    if step == "meeting_booked" and not record.get("booking_id"):
        # Only the calendar can make a meeting real; a claimed booking without one is a callback.
        step = "callback_requested" if args.get("callback_time") else "interested_no_next_step"
    if step == "callback_requested" and args.get("seriousness") == "time_waster":
        step = "not_interested"  # someone who told the agent off didn't ask for a callback
    record["next_step"] = step
    record["agent_outcome"] = NEXT_STEP_OUTCOME[step]
    record["opted_out"] = 1 if step == "opted_out" else 0
    for key in FIELDS:
        value = _clean(args.get(key))
        if not value:
            continue
        if key == "persona" and personas and value not in personas:
            value = "unclear"
        if key == "interest_level" and value not in INTEREST:
            continue
        if key == "seriousness" and value not in SERIOUSNESS:
            continue
        if key == "meeting_time" and record.get("meeting_time"):
            continue  # the booked slot wins over whatever the model restates
        record[key] = value
    if step != "callback_requested" and args.get("seriousness") == "time_waster":
        record.pop("callback_time", None)
    return _clean(args.get("closing_line")) or "Thank you for your time. Goodbye!"


def record_opt_out(call) -> None:
    """Opted-out contacts go on the do-not-call list (checked before any future dial)."""
    reason = f"Opted out on call {call.id[:8]}"
    for kind, value in (("phone", call.caller), ("email", call.record.get("contact_email")),
                        ("institution", call.record.get("institution"))):
        if value and db.dnc_add(kind, value, reason, call.id):
            logger.info(f"call {call.id}: added {kind} to do-not-call list")


# --- handlers ---------------------------------------------------------------

async def check_availability(params: FunctionCallParams) -> None:
    args = dict(params.arguments)
    result = await asyncio.to_thread(meetings.check_availability, str(args.get("date") or ""), args.get("part_of_day") or "any")
    await params.result_callback(result)


async def book_meeting(params: FunctionCallParams) -> None:
    call = params.app_resources
    args = {k: _clean(v) for k, v in dict(params.arguments).items()}
    for key in ("contact_name", "contact_title", "institution", "contact_email"):
        if args.get(key):
            call.record[key] = args[key]
        else:
            args[key] = call.record.get(key)
    if call.record.get("booking_id"):
        await params.result_callback({"confirmed": False, "reason": "A meeting is already booked on this call. Don't book twice."})
        return
    try:
        booking = await asyncio.to_thread(
            meetings.book, args.get("start_iso") or "", call_id=call.id, persona=call.record.get("persona"),
            contact_name=args["contact_name"], contact_title=args["contact_title"],
            institution=args["institution"], contact_email=args["contact_email"],
        )
    except meetings.BookingError as e:
        await params.result_callback({"confirmed": False, "reason": str(e)})
        return
    except Exception as e:  # noqa: BLE001 - never crash the call; never fake a confirmation
        logger.exception(f"call {call.id}: booking failed")
        call.record["error"] = f"booking failed: {e}"[:1000]
        await params.result_callback({"confirmed": False, "reason": "The calendar didn't accept the booking. Say a colleague will follow up to lock the slot."})
        return
    start = datetime.fromisoformat(booking["start_at"])
    call.record.update(booking_id=booking["id"], meeting_time=meetings.spoken(start))
    await params.result_callback({
        "confirmed": True,
        "say": meetings.spoken(start),
        "contact_name": booking.get("contact_name"),
        "invite_sent_to": booking.get("contact_email") if booking["backend"] == "google" else None,
    })


async def end_call(params: FunctionCallParams) -> None:
    call = params.app_resources
    closing = apply_end_call(call.record, dict(params.arguments), call.personas)
    call.record["end_reason"] = "agent_hangup"
    if call.record.get("opted_out"):
        await asyncio.to_thread(record_opt_out, call)
    # Tell the LLM it worked without asking it to speak again.
    await params.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))
    if call.caption:
        await call.caption("agent", closing)
    # Downstream, in order: speak the goodbye, then end once that audio has played.
    await params.llm.push_frame(TTSSpeakFrame(closing))
    await params.llm.push_frame(EndWorkerFrame(reason="agent_hangup"))


HANDLERS = {"check_availability": check_availability, "book_meeting": book_meeting, "end_call": end_call}
