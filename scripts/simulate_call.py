"""Simulated caller: an LLM plays a contact at an institution and talks to the running agent
over the same audio WebSocket the browser uses, so the whole pipeline (STT, turn detection,
LLM, tools, TTS) is exercised end to end with no human.

The caller's lines are synthesised with Sarvam TTS and streamed in real time; the agent's
reply latency is measured from the end of each caller line to the first agent audio.
Afterwards the script writes what the caller really said onto the call record as the STT
reference, so the dashboard reports STT accuracy for simulated calls automatically.
With --label it also sets the scenario's expected outcome as the reviewed outcome.

Costs Sarvam credit on both sides (roughly ₹1-3 per call). Calls are tagged channel=simulated,
so the dashboard can show or hide them.

  .venv/bin/python -m scripts.simulate_call --list
  .venv/bin/python -m scripts.simulate_call placement_books
  .venv/bin/python -m scripts.simulate_call all --label
"""

import argparse
import asyncio
import base64
import json
import time

import httpx
import websockets
from openai import AsyncOpenAI

from agent import kpis
from agent.config import settings
from agent.playground import base_url

RATE = 16000
CHUNK = RATE * 2 // 50  # 20 ms of 16-bit mono
AGENT_DONE_AFTER = 1.2  # seconds of agent silence = its turn is over
CALLER_MODEL = "sarvam-105b-conversations"

SCENARIOS = {
    "placement_books": {
        "expected": "meeting_booked",
        "voice": "priya",
        "who": "Dr. Meera Iyer, Training and Placement Officer at Sri Venkateswara Engineering College, Chennai. "
               "Your email is meera.iyer@svec.edu.in.",
        "goal": "You struggle to get students interview-ready and track placement readiness. You're curious, ask one "
                "question about how it works, then agree to a walkthrough on a weekday afternoon next week and give your email.",
    },
    "reception_declines": {
        "expected": "polite_decline",
        "voice": "neha",
        "who": "The front-desk receptionist at a polytechnic in Pune.",
        "goal": "You don't connect sales calls. Ask what it is about, then politely say the principal isn't interested "
                "and you can't share any contact. Don't budge.",
    },
    "callback_busy": {
        "expected": "call_again_later",
        "voice": "rahul",
        "who": "Prof. Arvind Rao, Dean of Academics at a university in Hyderabad.",
        "goal": "You're interested but walking into a meeting. Ask them to call back Thursday after 3 PM.",
    },
    "librarian_email": {
        "expected": "initial_discussion",
        "voice": "kavya",
        "who": "Ms. Sunita Sharma, Chief Librarian at a college in Jaipur.",
        "goal": "Speak in Hinglish (mix Hindi and English). You're mildly interested but don't want a meeting. Ask them "
                "to email details to library@jaipurcollege.ac.in.",
    },
    "hindi_placement": {
        "expected": "meeting_booked",
        "voice": "ritu",
        "language": "hi-IN",
        "who": "Smt. Kavita Verma, Placement Officer at a degree college in Lucknow. Your email is kavita.verma@lkocollege.in.",
        "goal": "Speak only in Hindi, written in Devanagari, the way people talk on the phone (English words like "
                "placement or meeting are fine). In your very first reply give your name and that you are the "
                "placement officer. If they ask for your role again, say a little irritated that you already told them. "
                "Agree to a walkthrough on a weekday morning next week and give your email.",
    },
    "joker": {
        "expected": "polite_decline",
        "voice": "aditya",
        "who": "A student who picked up the college's phone for fun.",
        "goal": "Make silly jokes, pretend to be the Prime Minister, and don't take the call seriously. "
                "If asked for a callback, say 'yeah call me in 100 years'.",
    },
    "opt_out": {
        "expected": "polite_decline",
        "voice": "amit",
        "who": "The registrar of a college in Lucknow.",
        "goal": "You're annoyed at sales calls. Firmly say: don't call this number again, remove us from your list.",
    },
}


def caller_system(s: dict) -> str:
    return (
        f"You are role-playing a person who just answered a phone call. You are: {s['who']}\n"
        f"How you behave: {s['goal']}\n"
        "Reply with ONLY what you say out loud next, one or two short spoken sentences, no stage directions. "
        "Don't end the call yourself: wait for the caller to say goodbye. Only after they have said goodbye, "
        "reply with exactly: [HANGUP]"
    )


async def caller_line(client: AsyncOpenAI, s: dict, transcript: list[tuple[str, str]]) -> str:
    # From the simulated person's side the agent is "user" and they are "assistant".
    msgs = [{"role": "system", "content": caller_system(s)}]
    msgs += [{"role": "user" if who == "agent" else "assistant", "content": text} for who, text in transcript]
    resp = await client.chat.completions.create(model=CALLER_MODEL, messages=msgs, temperature=0.7, max_tokens=120)
    return (resp.choices[0].message.content or "").strip()


async def synthesize(http: httpx.AsyncClient, text: str, voice: str, language: str = "en-IN") -> bytes:
    r = await http.post(
        "https://api.sarvam.ai/text-to-speech",
        headers={"api-subscription-key": settings.sarvam_api_key},
        json={"text": text, "target_language_code": language, "speaker": voice, "model": "bulbul:v3",
              "speech_sample_rate": RATE},
    )
    r.raise_for_status()
    audio = base64.b64decode(r.json()["audios"][0])
    return audio[44:] if audio[:4] == b"RIFF" else audio


async def run_scenario(name: str, url: str, label: bool, max_turns: int) -> dict:
    s = SCENARIOS[name]
    llm = AsyncOpenAI(api_key=settings.sarvam_api_key, base_url=base_url(CALLER_MODEL),
                      default_headers={"api-subscription-key": settings.sarvam_api_key})
    http = httpx.AsyncClient(timeout=30)
    transcript: list[tuple[str, str]] = []
    said: list[str] = []
    latencies: list[int] = []
    state = {"call_id": None, "last_audio": 0.0, "got_audio": False, "ended": False, "first_audio_after": None}
    outgoing: asyncio.Queue[bytes] = asyncio.Queue()

    async with websockets.connect(f"{url}?channel=simulated", max_size=None) as ws:
        async def receive():
            async for msg in ws:
                if isinstance(msg, bytes):
                    now = time.monotonic()
                    if state["first_audio_after"] is not None:
                        latencies.append(round((now - state["first_audio_after"]) * 1000))
                        state["first_audio_after"] = None
                    state["last_audio"], state["got_audio"] = now, True
                    continue
                m = json.loads(msg)
                if m.get("type") == "call":
                    state["call_id"] = m["id"]
                elif m.get("type") == "caption" and m.get("role") == "agent":
                    transcript.append(("agent", m["text"].replace(" [filler]", "").replace(" [interrupted]", "")))
                    print(f"  agent : {m['text']}")
                elif m.get("type") == "caption" and m.get("role") == "user":
                    print(f"  (heard: {m['text']})")
                elif m.get("type") in ("end", "error"):
                    if m.get("type") == "error":
                        print(f"  error : {m.get('text')}")
                    state["ended"] = True
            state["ended"] = True

        async def send():
            # A real line never goes quiet: stream silence between caller lines so VAD sees the gaps.
            silence = b"\x00" * CHUNK
            next_at = time.monotonic()
            while not state["ended"]:
                chunk = silence if outgoing.empty() else outgoing.get_nowait()
                try:
                    await ws.send(chunk)
                except websockets.ConnectionClosed:
                    return
                next_at += 0.02
                await asyncio.sleep(max(0, next_at - time.monotonic()))

        tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
        try:
            for _ in range(max_turns):
                # Wait for the agent to finish its turn.
                while not state["ended"] and (not state["got_audio"] or time.monotonic() - state["last_audio"] < AGENT_DONE_AFTER):
                    await asyncio.sleep(0.1)
                if state["ended"]:
                    break
                line = await caller_line(llm, s, transcript)
                if not line or "[HANGUP]" in line:
                    print("  caller hangs up")
                    break
                print(f"  caller: {line}")
                transcript.append(("caller", line))
                said.append(line)
                audio = await synthesize(http, line, s["voice"], s.get("language", "en-IN"))
                for i in range(0, len(audio), CHUNK):
                    outgoing.put_nowait(audio[i:i + CHUNK].ljust(CHUNK, b"\x00"))
                await asyncio.sleep(len(audio) / (RATE * 2))  # it's being "spoken" in real time
                while not outgoing.empty():
                    await asyncio.sleep(0.02)
                state["got_audio"] = False
                state["first_audio_after"] = time.monotonic()
            await asyncio.sleep(1)
        finally:
            for t in tasks:
                t.cancel()
    await http.aclose()
    await llm.close()

    result = {"scenario": name, "call_id": state["call_id"], "expected": s["expected"], "latencies_ms": latencies}
    if state["call_id"]:
        await asyncio.sleep(1.5)  # let the server write the final call row
        await write_reference(state["call_id"], " ".join(said), s["expected"] if label else None, name)
        result.update(await fetch_call(state["call_id"]))
    return result


def _dashboard() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=f"http://localhost:{settings.port}", auth=("sim", settings.dashboard_password), timeout=15)


async def write_reference(call_id: str, said: str, expected: str | None, scenario: str) -> None:
    async with _dashboard() as d:
        await d.post(f"/api/calls/{call_id}/review", json={
            "corrected_user_text": said, "reviewed_outcome": expected,
            "reviewer_notes": f"simulated: {scenario}" + (" (outcome = scenario's expected outcome)" if expected else ""),
        })


async def fetch_call(call_id: str) -> dict:
    async with _dashboard() as d:
        r = await d.get(f"/api/calls/{call_id}")
    if r.status_code != 200:
        return {}
    c = r.json()
    return {k: c.get(k) for k in ("agent_outcome", "persona", "seriousness", "meeting_time", "cost_inr", "wer", "end_reason")}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", nargs="?", default="placement_books", help="scenario name, or 'all'")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--label", action="store_true", help="set the expected outcome as the reviewed outcome")
    ap.add_argument("--url", default=f"ws://localhost:{settings.port}/ws/browser")
    ap.add_argument("--max-turns", type=int, default=10)
    args = ap.parse_args()
    if args.list:
        for k, s in SCENARIOS.items():
            print(f"{k:20} expects {s['expected']:18} {s['who']}")
        return
    if not settings.sarvam_api_key:
        raise SystemExit("SARVAM_API_KEY is not set.")
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = []
    for name in names:
        if name not in SCENARIOS:
            raise SystemExit(f"Unknown scenario {name}. Use --list.")
        print(f"\n=== {name}")
        results.append(await run_scenario(name, args.url, args.label, args.max_turns))
    print("\n=== results")
    for r in results:
        lat = r["latencies_ms"]
        p50 = kpis._percentile(lat, 0.5)
        ok = "OK " if r.get("agent_outcome") == r["expected"] else "MISS"
        print(f"{ok} {r['scenario']:20} expected {r['expected']:18} got {r.get('agent_outcome')!s:18} "
              f"persona={r.get('persona')} seriousness={r.get('seriousness')} p50={p50}ms wer={r.get('wer')} ₹{r.get('cost_inr')}")


if __name__ == "__main__":
    asyncio.run(main())
