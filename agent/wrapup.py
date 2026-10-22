"""KPIs for calls the agent never got to wrap up.

The end_call tool is what records the outcome, contact details and persona. When the caller
hangs up first (common on real calls), it never runs and the call would show up empty. Then
one extra request reads the transcript and fills in the same end_call fields, so the dashboard
still counts the call. Costs a few paise; any failure just leaves the call as it was.
"""

import json

from loguru import logger
from openai import AsyncOpenAI

from agent.config import settings
from agent.playground import base_url
from agent.tools import apply_end_call, tools_json

INSTRUCTIONS = (
    "You review the transcript of a finished outbound sales call made on behalf of {org} to a school "
    "or college. The caller hung up before the agent could wrap up. Call the end_call tool exactly once "
    "to record what actually happened: only details the person really said (their name, title, "
    "institution, email), which persona they were, their interest level and seriousness, and the "
    "next_step that best describes where the call ended. The transcript comes from speech recognition, "
    "so a single stray word is not a name. Leave out anything they did not say: no title unless they "
    "named their role, and asked_for_email only if they asked to be sent details by email. If the call "
    "ended before any next step was agreed, use interested_no_next_step when they showed interest and "
    "not_interested otherwise. closing_line is not spoken; set it to an empty string."
)


def transcript_text(transcript: list[dict]) -> str:
    return "\n".join(
        f"{'AGENT' if t['role'] == 'agent' else 'PERSON'}: {t['text']}" for t in transcript if t.get("text")
    )


def needs_wrapup(record: dict, transcript: list[dict]) -> bool:
    return not record.get("agent_outcome") and any(t.get("role") == "user" for t in transcript)


async def fill_from_transcript(record: dict, transcript: list[dict], doc: dict, personas: list[str],
                               model: str, client: AsyncOpenAI | None = None) -> bool:
    """Fill the end_call fields on `record` from the transcript. True if it did."""
    if not needs_wrapup(record, transcript):
        return False
    client = client or AsyncOpenAI(
        api_key=settings.sarvam_api_key, base_url=base_url(model),
        default_headers={"api-subscription-key": settings.sarvam_api_key}, timeout=20,
    )
    tools = [t for t in tools_json(doc) if t["function"]["name"] == "end_call"]
    try:
        resp = await client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=600,
            tools=tools,
            messages=[
                {"role": "system", "content": INSTRUCTIONS.format(org=settings.org_name)},
                {"role": "user", "content": transcript_text(transcript)},
            ],
        )
        calls = resp.choices[0].message.tool_calls or []
        args = next(json.loads(c.function.arguments) for c in calls if c.function.name == "end_call")
    except Exception as e:  # noqa: BLE001 - a missing wrap-up must never break saving the call
        logger.warning(f"transcript wrap-up failed: {type(e).__name__}: {e}")
        return False
    apply_end_call(record, args, personas)
    record["outcome_source"] = "transcript"
    return True
