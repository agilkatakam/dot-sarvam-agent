"""Pre-recorded audio for the agent's fixed lines (opening line, fillers, idle and time-limit
goodbyes). Each line is synthesised once with the current voice settings and kept on disk, so
later calls play it instantly and pay no TTS characters for it. A change of voice, pace,
language, model or wording simply makes a new cache entry.
"""

import asyncio
import base64
import hashlib
import io
import wave

import httpx
from loguru import logger

from agent.config import settings

_locks: dict[str, asyncio.Lock] = {}


def _key(text: str, rt: dict, rate: int) -> str:
    raw = "|".join([text, rt["tts_model"], rt["tts_voice"], str(rt["tts_pace"]), rt["tts_language"], str(rate)])
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _dir():
    return settings.data_dir / "audio_cache"


def cached(text: str, rt: dict, rate: int) -> bytes | None:
    path = _dir() / f"{_key(text, rt, rate)}.pcm"
    return path.read_bytes() if path.exists() else None


async def get(text: str, rt: dict, rate: int) -> bytes | None:
    """16-bit mono PCM for the line, synthesising it on first use. None if that fails
    (the caller then falls back to live TTS)."""
    key = _key(text, rt, rate)
    async with _locks.setdefault(key, asyncio.Lock()):
        if (audio := cached(text, rt, rate)) is not None:
            return audio
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    "https://api.sarvam.ai/text-to-speech",
                    headers={"api-subscription-key": settings.sarvam_api_key},
                    json={"text": text, "target_language_code": rt["tts_language"], "speaker": rt["tts_voice"],
                          "model": rt["tts_model"], "pace": rt["tts_pace"], "speech_sample_rate": rate},
                )
            r.raise_for_status()
            audio = base64.b64decode(r.json()["audios"][0])
        except Exception as e:  # noqa: BLE001 - live TTS is the fallback
            logger.warning(f"audio cache: couldn't synthesise {text[:40]!r}: {e}")
            return None
        if audio[:4] == b"RIFF":
            with wave.open(io.BytesIO(audio)) as w:
                audio = w.readframes(w.getnframes())
        _dir().mkdir(parents=True, exist_ok=True)
        (_dir() / f"{key}.pcm").write_bytes(audio)
        logger.info(f"audio cache: stored {text[:40]!r} ({len(audio) // (rate * 2)} s)")
        return audio


async def warm(lines: list[str], rt: dict, rate: int) -> None:
    await asyncio.gather(*(get(line, rt, rate) for line in lines))
