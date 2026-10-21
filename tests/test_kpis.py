from agent import kpis


def call(**kw):
    base = {"id": kw.get("id", str(id(kw))), "connected_at": "t", "end_reason": "agent_hangup", "transcript": []}
    base.update(kw)
    return base


def test_word_edits():
    assert kpis.word_edits("we already have a library", "we already have library") == (1, 5)
    assert kpis.word_edits("Hello, Sir!", "hello sir") == (0, 2)
    assert kpis.word_edits("", "noise") == (1, 0)


def test_final_outcome_precedence():
    assert kpis.final_outcome(call(connected_at=None)) is None
    assert kpis.final_outcome(call(end_reason="caller_hangup")) == "abandoned"
    assert kpis.final_outcome(call(agent_outcome="meeting_booked", end_reason="caller_hangup")) == "meeting_booked"
    assert kpis.final_outcome(call(agent_outcome="meeting_booked", reviewed_outcome="call_again_later")) == "call_again_later"
    assert kpis.final_outcome(call(end_reason="error")) == "error"
    assert kpis.final_outcome(call(end_reason=None)) is None  # still live


def test_summary_rates():
    calls = [
        call(connected_at=None, end_reason="error"),  # never connected
        call(agent_outcome="meeting_booked", reviewed_outcome="meeting_booked",
             transcript=[{"role": "user", "text": "yes tuesday works"}], corrected_user_text="yes tuesday works"),
        call(agent_outcome="polite_decline", reviewed_outcome="initial_discussion",
             transcript=[{"role": "user", "text": "send me the deck"}], corrected_user_text="send me a deck"),
        call(end_reason="caller_hangup"),
    ]
    turns = [
        {"kind": "greeting", "latency_ms": 900},
        {"kind": "response", "latency_ms": 500, "stt_ms": 200, "llm_ms": 200, "tts_ms": 100},
        {"kind": "response", "latency_ms": 700, "stt_ms": 300, "llm_ms": 300, "tts_ms": 100},
    ]
    s = kpis.summary(calls, turns)
    k = s["kpis"]
    assert s["attempts"] == 4 and s["connected"] == 3
    assert k["connectivity_rate"] == 0.75
    assert k["call_completion_rate"] == round(2 / 3, 4)
    assert s["outcomes"]["abandoned"]["rate"] == round(1 / 3, 4)
    assert s["outcomes"]["meeting_booked"]["count"] == 1
    assert s["outcomes"]["initial_discussion"]["count"] == 1  # reviewer label wins
    assert k["intent_accuracy"] == 0.5
    assert k["stt_word_error_rate"] == round(1 / 7, 4)
    assert k["latency"]["under_target_rate"] == 0.5
    assert k["latency"]["greeting_p50_ms"] == 900
    assert k["latency"]["avg_llm_ms"] == 250


def test_empty_summary():
    s = kpis.summary([], [])
    assert s["kpis"]["connectivity_rate"] is None
    assert s["kpis"]["latency"]["p50_ms"] is None


def test_funnel_and_cost():
    calls = [
        call(agent_outcome="meeting_booked", persona="placement", reviewed_persona="placement", interest_level="high",
             seriousness="genuine", cost_inr=2.0, duration_secs=120, prompt_version=2),
        call(agent_outcome="polite_decline", persona="reception_only", interest_level="none",
             seriousness="gatekeeper", cost_inr=1.0, duration_secs=60, prompt_version=2),
        call(agent_outcome="polite_decline", persona="library", reviewed_persona="placement", interest_level="low",
             seriousness="time_waster", cost_inr=1.0, duration_secs=60, prompt_version=1, filler_count=1),
        call(agent_outcome="call_again_later", persona="academic_leadership", interest_level="medium",
             seriousness="genuine", callback_time="Thursday 3 PM", cost_inr=0, duration_secs=0, prompt_version=1),
    ]
    s = kpis.summary(calls, [])
    f, c = s["funnel"], s["cost"]
    assert f["decision_maker_reach"] == 0.75
    assert f["routing_accuracy"] == 0.5
    assert f["positive_intent_rate"] == 0.5
    assert f["meetings_per_1000"] == 250.0
    assert f["callback_time_captured"] == 1.0
    assert f["seriousness"]["genuine"] == 2 and f["filler_rate"] == 0.25
    assert c["total_inr"] == 4.0 and c["per_call_inr"] == 1.0 and c["per_meeting_inr"] == 4.0
    assert c["per_connected_minute_inr"] == 1.0 and c["time_waster_inr"] == 1.0
    rows = kpis.by_prompt_version(calls, [])
    assert [r["prompt_version"] for r in rows] == [2, 1] and rows[0]["meetings"] == 1


def test_turns_are_filtered_to_the_calls():
    calls = [call(id="a"), call(id="b")]
    turns = [{"call_id": "a", "kind": "response", "latency_ms": 400}, {"call_id": "z", "kind": "response", "latency_ms": 4000}]
    assert kpis.summary(calls, turns)["kpis"]["latency"]["replies_measured"] == 1
