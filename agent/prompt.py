"""The editable prompt: a structured document, versioned in the database, rendered into the
exact system prompt sent to the language model.

Document shape (JSON, same as script.yaml):
  {"opening_line": str,
   "sections": [{"id", "title", "kind", "enabled", "body", "footer",
                 "items": [...],             # list: str; objections: {"trigger", "response"}
                 "personas": [{"key", "name", "roles", "pain", "pitch", "questions": [str]}]}]}

Every save creates a new version; the active version is used for the next call, and each call
records which version it ran on so KPIs can be compared across prompt changes.
"""

import copy
import re
from functools import lru_cache
from pathlib import Path

import yaml

from agent import db
from agent.config import settings

# The outcomes the KPI dashboard reports on ("abandoned" is added by the system when
# the caller hangs up before the agent wraps up). "other" = wrong number / voicemail.
OUTCOMES = ("meeting_booked", "call_again_later", "initial_discussion", "polite_decline", "other")

# The agent's detailed wrap-up next step -> KPI outcome.
NEXT_STEP_OUTCOME = {
    "meeting_booked": "meeting_booked",
    "callback_requested": "call_again_later",
    "asked_for_email": "initial_discussion",
    "interested_no_next_step": "initial_discussion",
    "declined_at_reception": "polite_decline",
    "not_interested": "polite_decline",
    "opted_out": "polite_decline",
    "wrong_number": "other",
    "voicemail": "other",
}

# Persona tags that always exist besides the ones defined in the prompt.
EXTRA_PERSONAS = ("reception_only", "unclear")
KINDS = ("text", "list", "personas", "objections")
VARIABLES = {
    "org": "Brand name from ORG_NAME in .env",
    "opening_line": "The opening line, after {org} is filled in",
    "today": "Today's date and weekday, e.g. Tuesday 14 October 2026",
    "calendar_days": "The next 14 days with weekdays and which take meetings",
    "meeting_minutes": "Walkthrough length, from MEETING_MINUTES",
    "timezone": "Timezone, from TIMEZONE",
    "next_steps": "Allowed next_step values for end_call",
    "personas": "Allowed persona tags for end_call",
}
_ACTIVE_KEY = "active_prompt_version"
_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


@lru_cache
def default_doc() -> dict:
    raw = yaml.safe_load((Path(__file__).parent / "script.yaml").read_text(encoding="utf-8"))
    return normalize(raw)


def normalize(doc: dict) -> dict:
    """Fill optional keys so every section has the same shape."""
    out = {"opening_line": " ".join(str(doc.get("opening_line") or "").split()), "sections": []}
    for s in doc.get("sections") or []:
        out["sections"].append({
            "id": str(s.get("id") or "").strip(),
            "title": str(s.get("title") or "").strip(),
            "kind": s.get("kind") or "text",
            "enabled": s.get("enabled", True) is not False,
            "body": str(s.get("body") or "").strip(),
            "footer": str(s.get("footer") or "").strip(),
            "items": copy.deepcopy(s.get("items") or []),
            "personas": copy.deepcopy(s.get("personas") or []),
        })
    return out


def validate(doc: dict) -> list[str]:
    """Problems that would make the prompt unsafe or break the tools. Empty = OK."""
    errors = []
    opening = (doc.get("opening_line") or "").lower()
    if not opening:
        errors.append("The opening line is empty.")
    ids, keys = set(), set()
    for i, s in enumerate(doc.get("sections") or [], 1):
        label = f"Section {i} ({s.get('title') or s.get('id') or 'untitled'})"
        if not _SLUG.match(s.get("id") or ""):
            errors.append(f"{label}: id must be lowercase letters, digits or _.")
        elif s["id"] in ids:
            errors.append(f"{label}: duplicate id '{s['id']}'.")
        ids.add(s.get("id"))
        if s.get("kind") not in KINDS:
            errors.append(f"{label}: kind must be one of {', '.join(KINDS)}.")
        if s.get("kind") == "personas":
            for p in s.get("personas") or []:
                key = (p.get("key") or "").strip()
                if not _SLUG.match(key):
                    errors.append(f"{label}: persona key '{key}' must be lowercase letters, digits or _.")
                elif key in keys or key in EXTRA_PERSONAS:
                    errors.append(f"{label}: persona key '{key}' is already used.")
                keys.add(key)
                if not (p.get("pitch") or "").strip():
                    errors.append(f"{label}: persona '{key}' has no pitch.")
        if s.get("kind") == "objections":
            for o in s.get("items") or []:
                if not isinstance(o, dict) or not (o.get("trigger") or "").strip() or not (o.get("response") or "").strip():
                    errors.append(f"{label}: every objection needs a trigger and a response.")
                    break
    if "ending" not in ids or not any(s.get("id") == "ending" and s.get("enabled", True) for s in doc.get("sections") or []):
        errors.append("Keep the 'ending' section enabled: it tells the agent to call end_call, which records the outcome and hangs up.")
    return errors


# --- versions ---------------------------------------------------------------

def active() -> tuple[int, dict]:
    """(version id, document) used for new calls. Seeds version 1 from script.yaml."""
    raw = db.kv_get(_ACTIVE_KEY)
    if raw:
        row = db.get_prompt_version(int(raw))
        if row:
            return row["id"], normalize(row["doc"])
    version = db.insert_prompt_version(default_doc(), "Factory default (script.yaml)")
    db.kv_set(_ACTIVE_KEY, str(version))
    return version, default_doc()


def save(doc: dict, note: str | None, activate: bool = True) -> int:
    doc = normalize(doc)
    errors = validate(doc)
    if errors:
        raise ValueError(" ".join(errors))
    version = db.insert_prompt_version(doc, (note or "").strip() or None)
    if activate:
        db.kv_set(_ACTIVE_KEY, str(version))
    return version


def activate(version: int) -> None:
    if db.get_prompt_version(version) is None:
        raise KeyError(version)
    db.kv_set(_ACTIVE_KEY, str(version))


# --- rendering --------------------------------------------------------------

def persona_tags(doc: dict) -> list[str]:
    tags = [p["key"] for s in doc["sections"] if s["kind"] == "personas" and s["enabled"] for p in s["personas"] if p.get("key")]
    return [*dict.fromkeys(tags), *EXTRA_PERSONAS]


def opening_line(doc: dict | None = None) -> str:
    doc = doc or active()[1]
    return " ".join(doc["opening_line"].replace("{org}", settings.org_name).split())


def variables(doc: dict, calendar_days: str, today: str) -> dict:
    return {
        "org": settings.org_name,
        "opening_line": opening_line(doc),
        "today": today,
        "calendar_days": calendar_days,
        "meeting_minutes": f"{settings.meeting_minutes}",
        "timezone": settings.timezone,
        "next_steps": ", ".join(NEXT_STEP_OUTCOME),
        "personas": ", ".join(persona_tags(doc)),
    }


def _fill(text: str, values: dict) -> str:
    # Only known {names} are replaced, so any other braces in the text are left alone.
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), text)


def _render_section(s: dict) -> str:
    lines = [s["title"].upper()] if s["title"] else []
    if s["body"]:
        lines.append(s["body"])
    if s["kind"] == "list":
        lines += [f"- {item}" for item in s["items"] if str(item).strip()]
    elif s["kind"] == "objections":
        lines += [f'"{o["trigger"].strip()}": {o["response"].strip()}' for o in s["items"] if isinstance(o, dict)]
    elif s["kind"] == "personas":
        for p in s["personas"]:
            block = [f"{p.get('name') or p['key']} (tag: {p['key']})."]
            if p.get("roles"):
                block.append(f"Typical roles: {p['roles']}.")
            if p.get("pain"):
                block.append(f"Pain point: {p['pain']}")
            block.append(f"Pitch: {p['pitch']}")
            questions = [q for q in p.get("questions") or [] if str(q).strip()]
            if questions:
                block.append("Discovery questions: " + " or ".join(f'"{q}"' for q in questions))
            lines.append(" ".join(block))
    if s["footer"]:
        lines.append(s["footer"])
    return "\n".join(lines)


def render(doc: dict, values: dict) -> str:
    parts = [_render_section(s) for s in doc["sections"] if s["enabled"]]
    return _fill("\n\n".join(p for p in parts if p), values).strip()


def render_parts(doc: dict, values: dict) -> list[dict]:
    """Per-section rendered text, for the studio's side-by-side preview."""
    return [
        {"id": s["id"], "title": s["title"], "enabled": s["enabled"], "text": _fill(_render_section(s), values)}
        for s in doc["sections"]
    ]
