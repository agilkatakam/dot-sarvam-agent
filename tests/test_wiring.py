import json

from pipecat.frames.frames import EndFrame, InputAudioRawFrame, InterruptionFrame, OutputAudioRawFrame

from agent import db
from agent.browser_serializer import BrowserPCMSerializer
from agent.tools import apply_end_call, tools_json

PERSONAS = ["placement", "academic_leadership", "library", "reception_only", "unclear"]


def test_startup_errors_are_actionable_without_leaking_provider_detail():
    from agent.call import startup_error_message

    assert "rejected" in startup_error_message("403 invalid_api_key_error").lower()
    assert "no credit" in startup_error_message("402 insufficient_quota_error").lower()
    assert "rate-limiting" in startup_error_message("429 rate_limit_exceeded_error").lower()
    assert "voice service" in startup_error_message("socket disconnected: internal details").lower()


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


def test_export_bundle_has_tester_transcripts_and_csv(tmp_path):
    import csv
    import io
    import zipfile

    from agent import export
    db.insert_call("call-1", "browser", None, "Priya")
    db.update_call("call-1", connected_at=db.now(), ended_at=db.now(), end_reason="agent_hangup", agent_outcome="polite_decline",
                   transcript=[{"role": "agent", "text": "Hello, this is an AI assistant"}, {"role": "user", "text": "Not interested"}])
    db.insert_turn("call-1", "greeting", 480)
    assert export.bundle_name().startswith("dot-test-results-Priya-")

    path = export.write_bundle(tmp_path / "out.zip")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        assert "calls.db" in names and "calls.csv" in names
        [txt] = [n for n in names if n.startswith("transcripts/")]
        body = z.read(txt).decode()
        assert "Tester: Priya" in body and "USER: Not interested" in body and "480" in body
        row = next(csv.DictReader(io.StringIO(z.read("calls.csv").decode())))
        assert row["tester"] == "Priya" and row["final_outcome"] == "polite_decline"


def test_voice_follows_caller_between_english_and_hindi():
    from agent.call import reply_language

    assert reply_language("Hello, who is this?", "en-IN") == "en-IN"
    assert reply_language("मैं placement head हूँ", "en-IN") == "hi-IN"
    assert reply_language("okay", "hi-IN") == "hi-IN"  # a short English word doesn't flip back
    assert reply_language("Can we continue in English please", "hi-IN") == "en-IN"
    # codemix sometimes writes an English phrase in Devanagari; that's still an English speaker
    assert reply_language("गो विद 3 PM on Wednesday the 28th and yes my email is fine", "en-IN") == "en-IN"
    assert reply_language("हाँ ठीक है", "hi-IN") == "hi-IN"


def test_services_use_the_listening_settings():
    from agent import runtime
    from agent.call import build_services

    rt = runtime.load()
    assert (rt["stt_language"], rt["stt_mode"], rt["stt_stream_type"]) == ("hi-IN", "codemix", "balanced")
    stt, _llm, _tts = build_services(rt, "test prompt")
    params = stt._query_params()
    assert params["stream_type"] == "balanced" and params["mode"] == "codemix" and "Dot" in params["prompt"]
    stt, _llm, _tts = build_services({**rt, "stt_prompt": ""}, "test prompt")
    assert "prompt" not in stt._query_params()
