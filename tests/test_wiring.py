import json

from pipecat.frames.frames import EndFrame, InputAudioRawFrame, InterruptionFrame, OutputAudioRawFrame

from agent import db
from agent.browser_serializer import BrowserPCMSerializer
from agent.tools import apply_end_call, tools_json

PERSONAS = ["placement", "academic_leadership", "library", "reception_only", "unclear"]


def test_apply_end_call_maps_next_step_to_outcome():
    record = {}
    line = apply_end_call(record, {"next_step": "callback_requested", "closing_line": "Talk Thursday!",
                                   "contact_name": "Dr Rao", "persona": "reception_only", "interest_level": "unknown",
                                   "seriousness": "gatekeeper"}, PERSONAS)
    assert line == "Talk Thursday!"
    assert record["agent_outcome"] == "call_again_later" and record["next_step"] == "callback_requested"
    assert record["contact_name"] == "Dr Rao" and record["opted_out"] == 0 and record["seriousness"] == "gatekeeper"
    apply_end_call(record, {"next_step": "opted_out"})
    assert record["agent_outcome"] == "polite_decline" and record["opted_out"] == 1


def test_meeting_needs_a_real_booking():
    record = {}
    apply_end_call(record, {"next_step": "meeting_booked", "meeting_time": "Tuesday 3pm"})
    assert record["agent_outcome"] == "initial_discussion"  # no calendar booking = no meeting
    record = {}
    apply_end_call(record, {"next_step": "meeting_booked", "callback_time": "Tuesday"})
    assert record["next_step"] == "callback_requested"
    record = {"booking_id": "abc", "meeting_time": "Tuesday 21 October at 3 PM"}
    apply_end_call(record, {"next_step": "meeting_booked", "meeting_time": "sometime"})
    assert record["agent_outcome"] == "meeting_booked" and record["meeting_time"] == "Tuesday 21 October at 3 PM"


def test_unknown_step_falls_back():
    record = {}
    line = apply_end_call(record, {"next_step": "sold_it"})
    assert record["agent_outcome"] == "initial_discussion" and "Goodbye" in line
    record = {"booking_id": "abc"}
    apply_end_call(record, {"next_step": "sold_it"})
    assert record["agent_outcome"] == "meeting_booked"


def test_time_waster_never_gets_a_callback():
    record = {}
    apply_end_call(record, {"next_step": "callback_requested", "callback_time": "in 100 years", "seriousness": "time_waster"})
    assert record["agent_outcome"] == "polite_decline" and "callback_time" not in record


def test_unknown_persona_and_bad_enums_are_cleaned():
    record = {}
    apply_end_call(record, {"next_step": "not_interested", "persona": "prime_minister", "interest_level": "huge",
                            "seriousness": "very"}, PERSONAS)
    assert record["persona"] == "unclear" and "interest_level" not in record and "seriousness" not in record


def test_tools_json_shape():
    from agent import prompt
    tools = tools_json(prompt.default_doc())
    names = [t["function"]["name"] for t in tools]
    assert names == ["check_availability", "book_meeting", "end_call"]
    end = tools[2]["function"]["parameters"]["properties"]
    assert "placement" in end["persona"]["enum"] and "reception_only" in end["persona"]["enum"]


def test_dnc_normalizes_phone_numbers():
    assert db.dnc_add("phone", "+91 98765-43210", "test")
    assert db.dnc_contains("phone", "09876543210")
    assert not db.dnc_add("phone", "9876543210")  # already there
    assert db.dnc_contains("email", None) is False


async def test_browser_serializer_roundtrip():
    s = BrowserPCMSerializer()
    frame = await s.deserialize(b"\x01\x00\x02\x00\x03")  # odd byte dropped
    assert isinstance(frame, InputAudioRawFrame) and frame.audio == b"\x01\x00\x02\x00"
    assert await s.deserialize("text is ignored") is None
    out = OutputAudioRawFrame(audio=b"\x00\x01", sample_rate=16000, num_channels=1)
    assert await s.serialize(out) == b"\x00\x01"
    assert json.loads(await s.serialize(InterruptionFrame())) == {"type": "clear"}
    assert json.loads(await s.serialize(EndFrame())) == {"type": "end"}
