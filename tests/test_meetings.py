from datetime import date, datetime, timedelta

import pytest

from agent import db, meetings
from agent.config import settings


def next_weekday(after_days=3) -> date:
    d = meetings.now().date() + timedelta(days=after_days)
    while not meetings.is_working_day(d):
        d += timedelta(days=1)
    return d


def test_candidate_slots_respect_hours_and_weekends():
    day = next_weekday()
    slots = meetings.candidate_slots(day)
    assert slots and slots[0].strftime("%H:%M") == settings.working_hours_start
    last_end = slots[-1] + timedelta(minutes=settings.meeting_minutes)
    assert last_end.strftime("%H:%M") <= settings.working_hours_end
    saturday = day + timedelta(days=(5 - day.weekday()) % 7)
    assert meetings.candidate_slots(saturday) == []


def test_lead_time_hides_slots_too_soon():
    day = next_weekday()
    at = datetime.combine(day, datetime.min.time(), tzinfo=meetings.tz()).replace(hour=11)
    assert all(s >= at + timedelta(hours=settings.booking_min_lead_hours) for s in meetings.candidate_slots(day, at))


def test_check_availability_and_book_then_slot_is_taken():
    day = next_weekday()
    r = meetings.check_availability(day.isoformat(), "afternoon")
    assert r["ok"] and 1 <= len(r["slots"]) <= 4
    assert all(datetime.fromisoformat(s["start_iso"]).hour >= 12 for s in r["slots"])
    start = r["slots"][0]["start_iso"]
    b = meetings.book(start, call_id="c1", contact_name="Dr Iyer", institution="SVEC", contact_email="a@b.in", persona="placement")
    assert b["backend"] == "local" and db.get_booking(b["id"])["status"] == "confirmed"
    with pytest.raises(meetings.BookingError):
        meetings.book(start, call_id="c2")  # double booking
    assert datetime.fromisoformat(start) not in meetings.free_slots(day)
    meetings.cancel(b["id"])
    assert datetime.fromisoformat(start) in meetings.free_slots(day)


def test_check_availability_rejects_bad_input():
    assert meetings.check_availability("next tuesday")["ok"] is False
    assert meetings.check_availability("2020-01-01")["ok"] is False
    with pytest.raises(meetings.BookingError):
        meetings.book("tomorrow at 3", call_id=None)


def test_ics_has_the_meeting():
    day = next_weekday()
    start = meetings.check_availability(day.isoformat())["slots"][0]["start_iso"]
    b = meetings.book(start, call_id=None, contact_name="Ms Rao", contact_email="rao@college.in")
    text = meetings.ics(db.get_booking(b["id"]))
    assert "BEGIN:VEVENT" in text and "ATTENDEE;RSVP=TRUE:mailto:rao@college.in" in text and "Ms Rao" in text


def test_calendar_days_text_lists_meeting_days_with_slots():
    lines = meetings.calendar_days_text().splitlines()
    assert 8 <= len(lines) <= 14 and all("=" in line for line in lines)
