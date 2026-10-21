"""Meeting booking: open slots and real bookings.

Two backends, chosen automatically:
  google  A Google Calendar (after `python -m scripts.connect_google_calendar`). Busy times are
          read from it, and each booking creates a real event with a Google Meet link and an
          email invite to the contact and to MEETING_TEAM_EMAILS.
  local   No Google set up: bookings live in the database, show on the dashboard's Meetings
          tab, and can be downloaded as .ics invites.
Either way every booking is stored in the bookings table, and those slots are treated as busy.

The Google client is synchronous; tool handlers call into this module via asyncio.to_thread.
"""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from loguru import logger

from agent import db
from agent.config import settings

SCOPES = ["https://www.googleapis.com/auth/calendar"]
SLOT_STEP_MINUTES = 30
_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_google = None


class BookingError(Exception):
    """A booking the caller should be told about (slot gone, outside hours...)."""


def tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def now() -> datetime:
    return datetime.now(tz())


def _hm(value: str) -> time:
    h, m = (int(x) for x in value.split(":"))
    return time(h, m)


def is_working_day(d: date) -> bool:
    days = {x.strip()[:3] for x in settings.working_days.split(",") if x.strip()}
    return _DAY_NAMES[d.weekday()] in days


# --- spoken / prompt text ---------------------------------------------------

def spoken_time(dt: datetime) -> str:
    hour = dt.strftime("%I").lstrip("0")
    minutes = "" if dt.minute == 0 else f":{dt.minute:02d}"
    return f"{hour}{minutes} {dt.strftime('%p')}"


def spoken(dt: datetime) -> str:
    """'Tuesday 21 October at 3 PM'"""
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')} at {spoken_time(dt)}"


def today_text(at: datetime | None = None) -> str:
    at = at or now()
    return f"{at.strftime('%A')} {at.day} {at.strftime('%B %Y')}"


def calendar_days_text(at: datetime | None = None, days: int = 14) -> str:
    """One line per day so the model never has to work out weekdays itself."""
    at = at or now()
    start, end = settings.working_hours_start, settings.working_hours_end
    lines = []
    for i in range(days):
        d = at.date() + timedelta(days=i)
        label = "today" if i == 0 else "tomorrow" if i == 1 else ""
        note = f"meetings {start} to {end}" if is_working_day(d) else "no meetings"
        lines.append(f"{d.strftime('%A')} {d.day} {d.strftime('%B')} = {d.isoformat()}{' (' + label + ')' if label else ''}: {note}")
    return "\n".join(lines)


# --- backends ---------------------------------------------------------------

def backend() -> str:
    return "google" if settings.google_token_path.exists() else "local"


def status() -> dict:
    if backend() == "google":
        try:
            _google_service()
            return {"backend": "google", "ok": True, "detail": f"Google Calendar '{settings.google_calendar_id}' connected."}
        except Exception as e:  # noqa: BLE001 - report, don't crash the dashboard
            return {"backend": "google", "ok": False, "detail": f"Google Calendar token found but not usable: {e}"}
    return {
        "backend": "local", "ok": True,
        "detail": "Built-in calendar (bookings on the Meetings tab, .ics invites). "
                  "Run `python -m scripts.connect_google_calendar` to book into Google Calendar instead.",
    }


def _google_service():
    global _google
    if _google is not None:
        return _google
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(settings.google_token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        settings.google_token_path.write_text(creds.to_json(), encoding="utf-8")
    _google = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _google


def _busy(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    busy = []
    for b in db.bookings(include_cancelled=False):
        busy.append((datetime.fromisoformat(b["start_at"]), datetime.fromisoformat(b["end_at"])))
    if backend() == "google":
        result = _google_service().freebusy().query(body={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": settings.timezone,
            "items": [{"id": settings.google_calendar_id}],
        }).execute()
        for b in result["calendars"][settings.google_calendar_id].get("busy", []):
            busy.append((datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                         datetime.fromisoformat(b["end"].replace("Z", "+00:00"))))
    return busy


# --- slots ------------------------------------------------------------------

def candidate_slots(day: date, at: datetime | None = None) -> list[datetime]:
    """Every slot start on a day by policy alone (hours, working days, lead time, horizon)."""
    at = at or now()
    if not is_working_day(day) or day > at.date() + timedelta(days=settings.booking_horizon_days):
        return []
    length = timedelta(minutes=settings.meeting_minutes)
    cursor = datetime.combine(day, _hm(settings.working_hours_start), tzinfo=tz())
    close = datetime.combine(day, _hm(settings.working_hours_end), tzinfo=tz())
    earliest = at + timedelta(hours=settings.booking_min_lead_hours)
    slots = []
    while cursor + length <= close:
        if cursor >= earliest:
            slots.append(cursor)
        cursor += timedelta(minutes=SLOT_STEP_MINUTES)
    return slots


def free_slots(day: date, part_of_day: str = "any", at: datetime | None = None) -> list[datetime]:
    slots = candidate_slots(day, at)
    if not slots:
        return []
    length = timedelta(minutes=settings.meeting_minutes)
    busy = _busy(slots[0], slots[-1] + length)
    slots = [s for s in slots if not any(s < b_end and s + length > b_start for b_start, b_end in busy)]
    if part_of_day == "morning":
        slots = [s for s in slots if s.hour < 12] or slots
    elif part_of_day == "afternoon":
        slots = [s for s in slots if s.hour >= 12] or slots
    return slots


def _spread(slots: list[datetime], n: int) -> list[datetime]:
    """Up to n slots spread across the day rather than the first n back to back."""
    if len(slots) <= n:
        return slots
    step = (len(slots) - 1) / (n - 1)
    return [slots[round(i * step)] for i in range(n)]


def check_availability(day_iso: str, part_of_day: str = "any") -> dict:
    try:
        day = date.fromisoformat(day_iso.strip()[:10])
    except ValueError:
        return {"ok": False, "error": f"Couldn't read the date '{day_iso}'. Use YYYY-MM-DD from the calendar list."}
    if day < now().date():
        return {"ok": False, "error": "That date is in the past. Ask for another day."}
    if not is_working_day(day):
        return {"ok": False, "error": f"No meetings on {day.strftime('%A')}s. Suggest a weekday."}
    try:
        slots = free_slots(day, part_of_day)
    except Exception as e:  # noqa: BLE001 - tell the model, never crash the call
        logger.error(f"check_availability failed: {e}")
        return {"ok": False, "error": "The calendar can't be checked right now. Offer to have a colleague follow up to fix a time."}
    if not slots:
        return {"ok": True, "date": day.isoformat(), "slots": [], "note": "Nothing free that day. Ask for another day."}
    return {
        "ok": True,
        "date": day.isoformat(),
        "slots": [{"start_iso": s.isoformat(), "say": spoken(s)} for s in _spread(slots, 4)],
    }


# --- booking ----------------------------------------------------------------

def _parse_start(start_iso: str) -> datetime:
    try:
        start = datetime.fromisoformat(start_iso.strip())
    except (AttributeError, ValueError):
        raise BookingError("start_iso must be one of the start_iso values check_availability returned.") from None
    return start.replace(tzinfo=tz()) if start.tzinfo is None else start.astimezone(tz())


def book(start_iso: str, *, call_id: str | None, contact_name=None, contact_title=None,
         institution=None, contact_email=None, persona=None) -> dict:
    """Create the booking; returns the booking row. Raises BookingError for caller-facing problems."""
    start = _parse_start(start_iso)
    end = start + timedelta(minutes=settings.meeting_minutes)
    if start not in free_slots(start.date()):
        raise BookingError("That time isn't free any more (or is outside meeting hours). Check availability again and offer another time.")

    booking = {
        "id": uuid.uuid4().hex[:12], "call_id": call_id, "start_at": start.isoformat(), "end_at": end.isoformat(),
        "contact_name": contact_name, "contact_title": contact_title, "institution": institution,
        "contact_email": contact_email, "persona": persona, "backend": backend(),
    }
    if booking["backend"] == "google":
        event = _google_create(booking)
        booking.update(external_id=event.get("id"), meet_link=event.get("hangoutLink"), html_link=event.get("htmlLink"))
    db.insert_booking(**booking)
    return booking


def _title(b: dict) -> str:
    who = ", ".join(x for x in (b.get("contact_name"), b.get("institution")) if x) or "new contact"
    return f"{settings.org_name} walkthrough: {who}"


def _description(b: dict) -> str:
    lines = [
        f"{settings.meeting_minutes} minute walkthrough of {settings.org_name}, booked by the {settings.org_name} AI voice assistant.",
        f"Contact: {b.get('contact_name') or '-'} ({b.get('contact_title') or '-'})",
        f"Institution: {b.get('institution') or '-'}",
        f"Email: {b.get('contact_email') or '-'}",
        f"Persona: {b.get('persona') or '-'}",
    ]
    if b.get("call_id"):
        lines.append(f"Call: {b['call_id']} (recording and transcript on the dashboard)")
    return "\n".join(lines)


def _attendees(b: dict) -> list[str]:
    emails = [e.strip() for e in settings.meeting_team_emails.split(",") if e.strip()]
    if b.get("contact_email") and "@" in b["contact_email"]:
        emails.append(b["contact_email"].strip())
    return list(dict.fromkeys(emails))


def _google_create(b: dict) -> dict:
    body = {
        "summary": _title(b),
        "description": _description(b),
        "start": {"dateTime": b["start_at"], "timeZone": settings.timezone},
        "end": {"dateTime": b["end_at"], "timeZone": settings.timezone},
        "attendees": [{"email": e} for e in _attendees(b)],
        "conferenceData": {"createRequest": {"requestId": f"dot-{b['id']}", "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
    }
    return _google_service().events().insert(
        calendarId=settings.google_calendar_id, body=body, conferenceDataVersion=1, sendUpdates="all"
    ).execute()


def cancel(booking_id: str) -> dict:
    b = db.get_booking(booking_id)
    if b is None:
        raise KeyError(booking_id)
    if b["backend"] == "google" and b.get("external_id") and b["status"] != "cancelled":
        _google_service().events().delete(
            calendarId=settings.google_calendar_id, eventId=b["external_id"], sendUpdates="all"
        ).execute()
    db.set_booking_status(booking_id, "cancelled")
    return db.get_booking(booking_id)


def ics(b: dict) -> str:
    """A standalone .ics invite for a booking (built-in calendar, or to forward)."""
    def stamp(value: str) -> str:
        return datetime.fromisoformat(value).astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DOT voice agent//EN", "METHOD:REQUEST", "BEGIN:VEVENT",
        f"UID:{b['id']}@dot-voice-agent", f"DTSTAMP:{stamp(b['created_at'])}",
        f"DTSTART:{stamp(b['start_at'])}", f"DTEND:{stamp(b['end_at'])}",
        f"SUMMARY:{esc(_title(b))}", f"DESCRIPTION:{esc(_description(b))}",
        *(f"ATTENDEE;RSVP=TRUE:mailto:{e}" for e in _attendees(b)),
        "STATUS:" + ("CANCELLED" if b["status"] == "cancelled" else "CONFIRMED"),
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"
