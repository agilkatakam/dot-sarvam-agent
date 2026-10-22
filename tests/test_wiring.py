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
    record = {}
    apply_end_call(record, {"next_step": "not_interested", "institution": "unknown", "contact_title": "Unclear",
                            "interest_level": "unknown", "seriousness": "unclear"}, PERSONAS)
    assert "institution" not in record and "contact_title" not in record
    assert record["interest_level"] == "unknown" and record["seriousness"] == "unclear"


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
    assert reply_language("நான் placement officer தான்", "en-IN") == "ta-IN"
    assert reply_language("నేను లైబ్రేరియన్ ని", "hi-IN") == "te-IN"
    assert reply_language("मी प्लेसमेंट अधिकारी आहे", "en-IN", heard="mr-IN") == "mr-IN"
    assert reply_language("हाँ", "en-IN") == "en-IN"  # one word could be a misdetection


def test_services_use_the_listening_settings():
    from agent import runtime
    from agent.call import build_services

    rt = runtime.load()
    assert (rt["stt_mode"], rt["stt_stream_type"]) == ("codemix", "balanced")
    stt, _llm, _tts = build_services(rt, "test prompt")
    params = stt._query_params()
    assert params["stream_type"] == "balanced" and params["mode"] == "codemix" and "Dot" in params["prompt"]
    stt, _llm, _tts = build_services({**rt, "stt_prompt": ""}, "test prompt")
    assert "prompt" not in stt._query_params()


def test_smart_turn_wait_is_capped():
    from agent import runtime
    from agent.call import turn_params

    params = turn_params(runtime.load())
    [stop] = params.user_turn_strategies.stop
    assert stop._turn_analyzer.params.stop_secs == 1.0


def test_every_followed_language_is_a_valid_voice_language():
    from pipecat.services.sarvam.tts import language_to_sarvam_language
    from pipecat.transcriptions.language import Language

    from agent.call import SCRIPTS

    for _block, code in SCRIPTS:
        assert language_to_sarvam_language(Language(code))
    assert language_to_sarvam_language(Language("mr-IN"))


async def test_hangup_before_wrapup_still_records_kpis():
    from types import SimpleNamespace

    from agent import prompt, wrapup

    args = {"next_step": "asked_for_email", "closing_line": "", "contact_name": "Akhil", "institution": "DPS",
            "contact_email": "akhil127@gmail.com", "contact_title": "unclear", "persona": "reception_only", "interest_level": "medium",
            "seriousness": "genuine"}
    sent = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            sent.update(kwargs)
            call = SimpleNamespace(function=SimpleNamespace(name="end_call", arguments=json.dumps(args)))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    transcript = [{"role": "agent", "text": "Hi, I'm calling from Dot."},
                  {"role": "user", "text": "Save my email akhil127@gmail.com, I'm Akhil from DPS"}]
    record = {}
    assert await wrapup.fill_from_transcript(record, transcript, prompt.default_doc(), PERSONAS, "m", client)
    assert record["agent_outcome"] == "initial_discussion" and record["outcome_source"] == "transcript"
    assert record["contact_email"] == "akhil127@gmail.com" and record["institution"] == "DPS"
    assert "contact_title" not in record  # "unclear" means not given
    assert "PERSON: Save my email" in sent["messages"][1]["content"]
    # The agent's own wrap-up wins, and a call where nobody spoke has nothing to fill in.
    assert not await wrapup.fill_from_transcript({"agent_outcome": "polite_decline"}, transcript, prompt.default_doc(), PERSONAS, "m", client)
    assert not await wrapup.fill_from_transcript({}, transcript[:1], prompt.default_doc(), PERSONAS, "m", client)


async def test_stt_holds_audio_back_and_releases_it_at_turn_end(monkeypatch):
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.services.sarvam.stt import SarvamRealtimeSTTService

    from agent.call import LookbackSarvamSTTService

    sent = bytearray()

    async def fake_run_stt(self, audio):
        sent.extend(audio)
        yield None

    async def fake_process_frame(self, frame, direction):
        sent.extend(self._audio_buffer)  # what the parent flushes before speech_end
        self._audio_buffer.clear()

    monkeypatch.setattr(SarvamRealtimeSTTService, "run_stt", fake_run_stt)
    monkeypatch.setattr(SarvamRealtimeSTTService, "process_frame", fake_process_frame)
    stt = LookbackSarvamSTTService(api_key="k", sample_rate=16000, endpointing="manual", lookback_secs=0.4)
    monkeypatch.setattr(LookbackSarvamSTTService, "sample_rate", property(lambda self: 16000))
    second = b"\x01\x00" * 16000  # 1 s of audio
    async for _ in stt.run_stt(second):
        pass
    assert len(sent) == len(second) - 12800  # the last 0.4 s is held back
    await stt.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert bytes(sent) == second  # and released, in order, at the end of the turn


async def test_language_switch_reaches_voice_and_model_before_the_transcript(monkeypatch):
    from pipecat.frames.frames import LLMUpdateSettingsFrame, TranscriptionFrame, TTSUpdateSettingsFrame

    from agent.call import LanguageFollower

    pushed = []
    follower = LanguageFollower("en-IN", "PROMPT", llm=object(), tts=object())

    async def capture(frame, direction=None):
        pushed.append(frame)

    monkeypatch.setattr(follower, "push_frame", capture)
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    async def noop(self, frame, direction):
        return None
    monkeypatch.setattr(FrameProcessor, "process_frame", noop)

    frame = TranscriptionFrame("हाँ बोलिए मैं placement officer हूँ", "u", "t")
    await follower.process_frame(frame, FrameDirection.DOWNSTREAM)
    assert [type(f) for f in pushed] == [TTSUpdateSettingsFrame, LLMUpdateSettingsFrame, TranscriptionFrame]
    assert pushed[0].delta.language == "hi-IN"
    assert "Reply only in Hindi" in pushed[1].delta.system_instruction
    pushed.clear()
    await follower.process_frame(TranscriptionFrame("haan", "u", "t"), FrameDirection.DOWNSTREAM)
    assert [type(f) for f in pushed] == [TranscriptionFrame]  # no switch, just passed on


def test_spelled_emails_are_stored_as_addresses():
    from agent.tools import normalize_email

    assert normalize_email("a-k-h-i-l-one-two-seven@gmail.com") == "akhil127@gmail.com"
    assert normalize_email("meera.iyer@svec.edu.in") == "meera.iyer@svec.edu.in"
    assert normalize_email("mary-jane@school.in") == "mary-jane@school.in"


def test_demo_endpoint_returns_one_calls_kpis_without_a_password():
    from fastapi.testclient import TestClient

    from agent.server import app

    db.insert_call("demo-1", "browser", None, "demo")
    db.update_call("demo-1", connected_at=db.now(), ended_at=db.now(), end_reason="agent_hangup", agent_outcome="initial_discussion",
                   next_step="asked_for_email", contact_email="a@b.in", cost_inr=1.5,
                   transcript=[{"role": "agent", "text": "Hi"}, {"role": "user", "text": "Email me"}])
    db.insert_turn("demo-1", "response", 1400, stt_ms=700, llm_ms=300, tts_ms=300)
    client = TestClient(app)
    r = client.get("/api/demo/calls/demo-1")
    assert r.status_code == 200
    c = r.json()
    assert c["final_outcome"] == "initial_discussion" and c["contact_email"] == "a@b.in" and c["user_turns"] == 1
    assert c["turns"] == [{"kind": "response", "latency_ms": 1400, "stt_ms": 700, "llm_ms": 300, "tts_ms": 300}]
    assert "transcript" not in c  # KPIs only
    assert client.get("/api/demo/calls/nope").status_code == 404
    assert client.get("/demo").status_code == 200
