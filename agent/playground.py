"""Text playground: chat with the agent by typing, using the same prompt, tools and model
as a phone call, but no audio. Costs LLM tokens only, so prompt edits can be tried cheaply.

check_availability runs for real (read-only). book_meeting is a dry run: it checks the slot
is free but books nothing. end_call returns the record the call would have saved.
"""

import json
import time

from openai import AsyncOpenAI

from agent import meetings, prompt
from agent.config import settings
from agent.tools import apply_end_call, tools_json

MAX_TOOL_ROUNDS = 4


def base_url(model: str) -> str:
    # Same rule as Pipecat's SarvamLLMService: the conversations model lives on /v1.
    return "https://api.sarvam.ai/" + ("v1" if model == "sarvam-105b-conversations" else "v2")


def request_body(doc: dict, rt: dict, values: dict) -> dict:
    """The chat-completions request a call starts with (before the caller says anything)."""
    return {
        "model": rt["llm_model"],
        "stream": True,
        "temperature": rt["llm_temperature"],
        "max_tokens": rt["llm_max_tokens"],
        "messages": [
            {"role": "system", "content": prompt.render(doc, values)},
            {"role": "assistant", "content": prompt.opening_line(doc)},
        ],
        "tools": tools_json(doc),
    }


def _dry_run_book(args: dict, state: dict) -> dict:
    if state.get("booking_id"):
        return {"confirmed": False, "reason": "A meeting is already booked on this call. Don't book twice."}
    try:
        start = meetings._parse_start(args.get("start_iso") or "")
        if start not in meetings.free_slots(start.date()):
            raise meetings.BookingError("That time isn't free any more (or is outside meeting hours). Check availability again.")
    except meetings.BookingError as e:
        return {"confirmed": False, "reason": str(e)}
    state["booking_id"] = "dry-run"
    state["meeting_time"] = meetings.spoken(start)
    return {"confirmed": True, "say": meetings.spoken(start), "contact_name": args.get("contact_name"), "dry_run": True}


def _run_tool(name: str, args: dict, state: dict, doc: dict) -> dict:
    if name == "check_availability":
        return meetings.check_availability(str(args.get("date") or ""), args.get("part_of_day") or "any")
    if name == "book_meeting":
        return _dry_run_book(args, state)
    if name == "end_call":
        record = {k: state[k] for k in ("booking_id", "meeting_time") if k in state}
        closing = apply_end_call(record, args, prompt.persona_tags(doc))
        state["ended"] = {"closing_line": closing, "record": record}
        return {"ok": True}
    return {"error": f"unknown tool {name}"}


async def chat(doc: dict, rt: dict, messages: list[dict], state: dict) -> dict:
    """One caller turn. `messages` is the conversation so far after the system prompt
    (OpenAI format, including earlier tool calls). Returns the new messages and timings."""
    at = meetings.now()
    values = prompt.variables(doc, meetings.calendar_days_text(at), meetings.today_text(at))
    system = {"role": "system", "content": prompt.render(doc, values)}
    client = AsyncOpenAI(
        api_key=settings.sarvam_api_key, base_url=base_url(rt["llm_model"]),
        default_headers={"api-subscription-key": settings.sarvam_api_key}, timeout=30,
    )
    new: list[dict] = []
    events: list[dict] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            started = time.monotonic()
            resp = await client.chat.completions.create(
                model=rt["llm_model"], messages=[system, *messages, *new], tools=tools_json(doc),
                temperature=rt["llm_temperature"], max_tokens=rt["llm_max_tokens"],
            )
            ms = round((time.monotonic() - started) * 1000)
            if resp.usage:
                usage["prompt_tokens"] += resp.usage.prompt_tokens or 0
                usage["completion_tokens"] += resp.usage.completion_tokens or 0
            msg = resp.choices[0].message
            out = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                out["tool_calls"] = [
                    {"id": t.id, "type": "function", "function": {"name": t.function.name, "arguments": t.function.arguments}}
                    for t in msg.tool_calls
                ]
            new.append(out)
            events.append({"type": "llm", "ms": ms, "text": out["content"]})
            if not msg.tool_calls:
                break
            for t in msg.tool_calls:
                try:
                    args = json.loads(t.function.arguments or "{}")
                except ValueError:
                    args = {}
                result = _run_tool(t.function.name, args, state, doc)
                events.append({"type": "tool", "name": t.function.name, "args": args, "result": result})
                new.append({"role": "tool", "tool_call_id": t.id, "content": json.dumps(result, ensure_ascii=False)})
            if state.get("ended"):
                break
    finally:
        await client.close()
    cost = usage["prompt_tokens"] / 1e6 * settings.price_llm_in_per_1m + usage["completion_tokens"] / 1e6 * settings.price_llm_out_per_1m
    return {"messages": new, "events": events, "state": state, "usage": usage, "cost_inr": round(cost, 4)}
