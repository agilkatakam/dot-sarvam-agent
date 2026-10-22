"""Settings you can change between calls from the dashboard (Settings tab), no restart.

Stored in data/runtime.json. Defaults come from .env so existing setups keep working.
Each field carries the metadata the dashboard needs to render and validate it.
"""

import json
import os
from dataclasses import dataclass

from agent.config import settings

TTS_VOICES = (
    "shubh", "aditya", "rahul", "rohan", "amit", "dev", "ratan", "varun", "manan", "sumit",
    "kabir", "aayan", "ashutosh", "advait", "ritu", "priya", "neha", "pooja", "simran",
    "kavya", "ishita", "shreya", "roopa", "amelia", "sophia",
)
STT_LANGUAGES = ("en-IN", "hi-IN", "auto", "bn-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN", "mr-IN", "gu-IN", "pa-IN")
TTS_LANGUAGES = ("en-IN", "hi-IN", "bn-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN", "mr-IN", "gu-IN", "pa-IN", "od-IN")


@dataclass(frozen=True)
class Field:
    key: str
    group: str
    label: str
    kind: str  # choice | int | float | bool | text
    default: object
    help: str = ""
    choices: tuple = ()
    min: float | None = None
    max: float | None = None


def _env(name: str, default):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    return type(default)(raw)


FIELDS = (
    # --- language model
    Field("llm_model", "Language model", "Model", "choice", _env("SARVAM_LLM_MODEL", "sarvam-105b-conversations"),
          "sarvam-105b-conversations is tuned for dialogue. Compare others on latency in the KPI tab.",
          choices=("sarvam-105b-conversations", "sarvam-105b", "gemma4")),
    Field("llm_temperature", "Language model", "Temperature", "float", _env("LLM_TEMPERATURE", 0.4),
          "Lower = more consistent and on-script; higher = more varied phrasing.", min=0, max=1.5),
    Field("llm_max_tokens", "Language model", "Max reply tokens", "int", _env("LLM_MAX_TOKENS", 500),
          "Caps each reply, including tool calls. Below ~400 the end_call wrap-up can be cut off mid-way and lost. "
          "Reply length is set by the prompt, not this.", min=200, max=1500),
    Field("llm_timeout_secs", "Language model", "Retry a silent model after (s)", "float", _env("LLM_TIMEOUT_SECS", 6.0),
          "If the model produces nothing for this long, the request is re-sent once.", min=2, max=30),
    Field("filler_after_secs", "Language model", "Say a filler after (s)", "float", _env("FILLER_AFTER_SECS", 2.5),
          "If the agent hasn't started replying this long after the caller stopped, it says a short "
          "holding line (e.g. 'One moment.') so the line never goes dead. 0 = off.", min=0, max=15),
    # --- voice
    Field("tts_model", "Voice", "Voice model", "choice", _env("SARVAM_TTS_MODEL", "bulbul:v3"), choices=("bulbul:v3", "bulbul:v2")),
    Field("tts_voice", "Voice", "Voice", "choice", _env("SARVAM_TTS_VOICE", "shubh"),
          "bulbul:v3 speakers. Try a few with the Preview button; one tester found the default 'eerie'.", choices=TTS_VOICES),
    Field("tts_language", "Voice", "Speaking language", "choice", _env("SARVAM_TTS_LANGUAGE", "en-IN"), choices=TTS_LANGUAGES),
    Field("tts_pace", "Voice", "Pace", "float", _env("SARVAM_TTS_PACE", 1.05), "1.0 = normal speed.", min=0.5, max=2.0),
    Field("tts_min_buffer", "Voice", "Start speaking after (chars)", "int", _env("SARVAM_TTS_MIN_BUFFER", 30),
          "Characters of reply buffered before audio is generated. Lower = faster start, slightly less natural prosody.",
          min=10, max=200),
    # --- listening / turn-taking
    Field("stt_language", "Listening", "Caller language", "choice", _env("SARVAM_STT_LANGUAGE", "auto"),
          "auto detects the caller's language (English, Hindi, Tamil, Telugu...) and the agent follows it. "
          "hi-IN is steadier for English/Hindi-only callers. en-IN forces everything into English.", choices=STT_LANGUAGES),
    Field("stt_mode", "Listening", "Transcript mode", "choice", _env("SARVAM_STT_MODE", "codemix"),
          "codemix keeps Hinglish as spoken: English words in English letters, Hindi in Devanagari.",
          choices=("transcribe", "codemix", "translit", "translate", "verbatim")),
    Field("stt_stream_type", "Listening", "Recognition speed", "choice", _env("SARVAM_STT_STREAM_TYPE", "balanced"),
          "balanced is more accurate on names and emails; fast returns words a little sooner but mishears more.",
          choices=("balanced", "fast")),
    Field("stt_prompt", "Listening", "Recognition hint", "text",
          _env("SARVAM_STT_PROMPT", "Phone call to an Indian school or college about Dot, a news app. "
               "Names, job titles and email addresses."),
          "Context that helps speech recognition with expected words. Leave empty to turn off."),
    Field("turn_mode", "Listening", "End-of-turn detection", "choice", _env("TURN_MODE", "smart"),
          "smart: on-device voice detector + Smart Turn model decide when the caller finished (fast, and waits "
          "for unfinished sentences). sarvam: Sarvam's server decides after a fixed silence.",
          choices=("smart", "sarvam")),
    Field("vad_stop_secs", "Listening", "Silence before checking for end of turn (s)", "float", _env("VAD_STOP_SECS", 0.5),
          "smart mode only. Every pause this long also cuts the audio sent to speech recognition, so below ~0.4 "
          "names and emails get split mid-word and misheard.", min=0.1, max=1.5),
    Field("turn_max_wait_secs", "Listening", "Reply anyway after silence (s)", "float", _env("TURN_MAX_WAIT_SECS", 1.0),
          "smart mode only. When the turn model thinks the caller is mid-sentence it keeps waiting; this caps the "
          "wait. It often misjudges Indian English, so a high value adds seconds to many replies.", min=0.5, max=3.0),
    Field("stt_silence_ms", "Listening", "Sarvam silence window (ms)", "int", _env("SARVAM_STT_SILENCE_MS", 400),
          "sarvam mode only. Lower = faster replies, more cut-ins.", min=150, max=2000),
    Field("turn_stop_wait_secs", "Listening", "Wait for late transcript (s)", "float", _env("TURN_STOP_WAIT_SECS", 0.15),
          "sarvam mode only.", min=0, max=2),
    # --- call limits
    Field("max_call_seconds", "Call limits", "Max call length (s)", "int", _env("MAX_CALL_SECONDS", 300),
          "Protects credit. The agent says goodbye when it's reached.", min=60, max=1800),
    Field("idle_hangup_secs", "Call limits", "Hang up after silence (s)", "int", _env("IDLE_HANGUP_SECS", 30),
          "If neither side speaks for this long the call is ended.", min=10, max=300),
)
BY_KEY = {f.key: f for f in FIELDS}


def _coerce(f: Field, value):
    if f.kind == "bool":
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if f.kind == "int":
        value = int(float(value))
    elif f.kind == "float":
        value = float(value)
    else:
        value = str(value).strip()
    if f.kind == "choice" and value not in f.choices:
        raise ValueError(f"{f.label}: must be one of {', '.join(f.choices)}")
    if f.min is not None and value < f.min or f.max is not None and value > f.max:
        raise ValueError(f"{f.label}: must be between {f.min} and {f.max}")
    return value


def defaults() -> dict:
    return {f.key: f.default for f in FIELDS}


def load() -> dict:
    """Current settings: defaults overlaid with whatever was saved from the dashboard."""
    values = defaults()
    try:
        saved = json.loads(settings.runtime_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    for key, value in saved.items():
        if key in BY_KEY:
            try:
                values[key] = _coerce(BY_KEY[key], value)
            except (TypeError, ValueError):
                pass  # a stale or hand-edited bad value falls back to the default
    return values


def save(changes: dict) -> dict:
    """Validate and persist changes; raises ValueError naming the first bad field."""
    values = load()
    for key, value in changes.items():
        if key not in BY_KEY:
            raise ValueError(f"Unknown setting: {key}")
        values[key] = _coerce(BY_KEY[key], value)
    settings.runtime_path.parent.mkdir(parents=True, exist_ok=True)
    settings.runtime_path.write_text(json.dumps(values, indent=2), encoding="utf-8")
    return values


def schema() -> list[dict]:
    return [
        {"key": f.key, "group": f.group, "label": f.label, "kind": f.kind, "default": f.default,
         "help": f.help, "choices": list(f.choices), "min": f.min, "max": f.max}
        for f in FIELDS
    ]
