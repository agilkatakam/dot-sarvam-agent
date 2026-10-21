"""KPI definitions. Pure functions over the dicts returned by agent.db.

System KPIs (quality of the system):
  connectivity_rate        connected calls / call attempts
  call_completion_rate     connected calls that reached a real outcome / connected calls
  latency_under_target     agent replies starting <= 600 ms after the caller stopped / all replies
  stt_accuracy             1 - word error rate, on calls a reviewer has corrected
  intent_accuracy          agent's outcome == reviewer's outcome / reviewed calls

Funnel KPIs (KPI framework, sections 1 and 3):
  decision_maker_reach     finished calls that got past reception to a persona / finished calls
  routing_accuracy         agent's persona == reviewer's persona / calls with a reviewed persona
  positive_intent_rate     finished calls with high or medium interest / finished calls
  meetings_per_1000        meetings booked per 1,000 call attempts (the north star, unqualified)

Outcomes (share of finished connected calls; they add up to 100% with errors):
  abandoned, polite_decline, meeting_booked, call_again_later, initial_discussion, other
"""

import re
from collections import Counter

from agent.prompt import EXTRA_PERSONAS, OUTCOMES

ABANDONED = "abandoned"
ALL_OUTCOMES = (ABANDONED, *OUTCOMES)
SERIOUSNESS = ("genuine", "gatekeeper", "time_waster", "unclear")


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def is_connected(call: dict) -> bool:
    return call.get("connected_at") is not None


def final_outcome(call: dict) -> str | None:
    """Reviewer's label wins; otherwise the agent's; otherwise inferred from how the call ended."""
    if not is_connected(call):
        return None
    if call.get("reviewed_outcome"):
        return call["reviewed_outcome"]
    if call.get("agent_outcome"):
        return call["agent_outcome"]
    if call.get("end_reason") in ("caller_hangup", "timeout", "idle"):
        return ABANDONED
    if call.get("end_reason") is None:
        return None  # still live
    return "error"


def final_persona(call: dict) -> str | None:
    return call.get("reviewed_persona") or call.get("persona")


# --- word error rate --------------------------------------------------------

def _words(text: str) -> list[str]:
    return re.sub(r"[^\w\s']", " ", (text or "").lower()).split()


def word_edits(reference: str, hypothesis: str) -> tuple[int, int]:
    """(substitutions + deletions + insertions, reference word count)."""
    ref, hyp = _words(reference), _words(hypothesis)
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1], len(ref)


def heard_user_text(call: dict) -> str:
    return " ".join(m["text"] for m in call.get("transcript", []) if m.get("role") == "user")


# --- latency ----------------------------------------------------------------

def _percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, round(p * (len(v) - 1)))]


def latency_stats(turns: list[dict], target_ms: int = 600, working_target_ms: int = 1500) -> dict:
    replies = [t["latency_ms"] for t in turns if t["kind"] == "response"]
    greetings = [t["latency_ms"] for t in turns if t["kind"] == "greeting"]

    def avg(key):
        vals = [t[key] for t in turns if t["kind"] == "response" and t.get(key) is not None]
        return round(sum(vals) / len(vals)) if vals else None

    return {
        "replies_measured": len(replies),
        "under_target_rate": _rate(sum(1 for x in replies if x <= target_ms), len(replies)),
        "under_working_target_rate": _rate(sum(1 for x in replies if x <= working_target_ms), len(replies)),
        "p50_ms": _percentile(replies, 0.5),
        "p90_ms": _percentile(replies, 0.9),
        "greeting_p50_ms": _percentile(greetings, 0.5),
        "avg_stt_ms": avg("stt_ms"),
        "avg_llm_ms": avg("llm_ms"),
        "avg_tts_ms": avg("tts_ms"),
    }


# --- summary ----------------------------------------------------------------

def summary(calls: list[dict], turns: list[dict], target_ms: int = 600, working_target_ms: int = 1500) -> dict:
    ids = {c["id"] for c in calls}
    turns = [t for t in turns if t["call_id"] in ids] if turns and "call_id" in turns[0] else turns
    connected = [c for c in calls if is_connected(c)]
    finished = [c for c in connected if c.get("end_reason") is not None]
    outcomes = Counter(final_outcome(c) for c in finished)

    stt_calls = [c for c in finished if c.get("corrected_user_text")]
    edits = ref_words = 0
    for c in stt_calls:
        e, n = word_edits(c["corrected_user_text"], heard_user_text(c))
        edits, ref_words = edits + e, ref_words + n
    wer = _rate(edits, ref_words)

    intent_calls = [c for c in finished if c.get("reviewed_outcome") and c.get("agent_outcome")]
    intent_hits = sum(1 for c in intent_calls if c["reviewed_outcome"] == c["agent_outcome"])
    confusion = Counter(f"{c['agent_outcome']} -> {c['reviewed_outcome']}" for c in intent_calls)

    reached = [c for c in finished if final_persona(c) and final_persona(c) not in EXTRA_PERSONAS]
    routed = [c for c in finished if c.get("reviewed_persona") and c.get("persona")]
    positive = [c for c in finished if c.get("interest_level") in ("high", "medium")]
    callbacks = [c for c in finished if final_outcome(c) == "call_again_later"]

    costs = [c["cost_inr"] for c in calls if c.get("cost_inr") is not None]
    minutes = sum((c.get("duration_secs") or 0) for c in connected) / 60
    wasted = sum(c.get("cost_inr") or 0 for c in finished if c.get("seriousness") == "time_waster")

    return {
        "attempts": len(calls),
        "connected": len(connected),
        "finished": len(finished),
        "kpis": {
            "connectivity_rate": _rate(len(connected), len(calls)),
            "call_completion_rate": _rate(sum(outcomes[o] for o in OUTCOMES), len(finished)),
            "latency": latency_stats(turns, target_ms, working_target_ms),
            "stt_accuracy": None if wer is None else round(max(0.0, 1 - wer), 4),
            "stt_word_error_rate": wer,
            "stt_calls_reviewed": len(stt_calls),
            "intent_accuracy": _rate(intent_hits, len(intent_calls)),
            "intent_calls_reviewed": len(intent_calls),
            "intent_confusion": dict(confusion),
        },
        "funnel": {
            "decision_maker_reach": _rate(len(reached), len(finished)),
            "routing_accuracy": _rate(sum(1 for c in routed if c["reviewed_persona"] == c["persona"]), len(routed)),
            "routing_calls_reviewed": len(routed),
            "positive_intent_rate": _rate(len(positive), len(finished)),
            "meetings_per_1000": round(outcomes["meeting_booked"] / len(calls) * 1000, 1) if calls else None,
            "callback_time_captured": _rate(sum(1 for c in callbacks if c.get("callback_time")), len(callbacks)),
            "personas": dict(Counter(final_persona(c) or "unknown" for c in finished)),
            "seriousness": {s: sum(1 for c in finished if c.get("seriousness") == s) for s in SERIOUSNESS},
            "opted_out": sum(1 for c in finished if c.get("opted_out")),
            "filler_rate": _rate(sum(1 for c in finished if c.get("filler_count")), len(finished)),
        },
        "cost": {
            "total_inr": round(sum(costs), 2),
            "per_call_inr": round(sum(costs) / len(costs), 2) if costs else None,
            "per_connected_minute_inr": round(sum(costs) / minutes, 2) if minutes else None,
            "per_meeting_inr": round(sum(costs) / outcomes["meeting_booked"], 2) if outcomes["meeting_booked"] else None,
            "time_waster_inr": round(wasted, 2),
        },
        "outcomes": {o: {"count": outcomes[o], "rate": _rate(outcomes[o], len(finished))} for o in ALL_OUTCOMES},
        "errors": outcomes["error"],
    }


def by_prompt_version(calls: list[dict], turns: list[dict], target_ms: int = 600) -> list[dict]:
    """One row per prompt version, to see whether a prompt edit helped."""
    rows = []
    versions = sorted({c.get("prompt_version") for c in calls}, key=lambda v: (v is None, -(v or 0)))
    for v in versions:
        group = [c for c in calls if c.get("prompt_version") == v]
        s = summary(group, turns, target_ms)
        rows.append({
            "prompt_version": v,
            "attempts": s["attempts"],
            "completion": s["kpis"]["call_completion_rate"],
            "meetings": s["outcomes"]["meeting_booked"]["count"],
            "decision_maker_reach": s["funnel"]["decision_maker_reach"],
            "intent_accuracy": s["kpis"]["intent_accuracy"],
            "p50_ms": s["kpis"]["latency"]["p50_ms"],
            "per_call_inr": s["cost"]["per_call_inr"],
        })
    return rows
