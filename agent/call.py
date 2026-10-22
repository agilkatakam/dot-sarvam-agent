"""One live call: Sarvam STT -> Sarvam LLM -> Sarvam TTS, plus everything the KPIs need.

Per call it reads the active prompt version and the runtime settings, so dashboard edits
apply from the next call on. What gets measured here (see agent/kpis.py for aggregation):
  - attempt:   a row is inserted the moment the caller's audio socket opens
  - connected: the first time agent audio actually starts playing to the caller
  - latency:   caller stops speaking -> agent audio starts, per reply, with STT/LLM/TTS split
  - outcome:   the end_call tool's classification, or "abandoned" if the caller hangs up first
  - cost:      call minutes (STT), LLM tokens and TTS characters, priced from .env
  - transcript + stereo recording, for human review (STT and intent accuracy)
"""

import asyncio
import re
import time
import wave
from dataclasses import dataclass, field

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    EndFrame,
    MetricsFrame,
    OutputTransportMessageUrgentFrame,
    TTSSpeakFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData, TTSUsageMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.services.sarvam.llm import SarvamLLMService
from pipecat.services.sarvam.stt import SarvamRealtimeSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transports.base_transport import BaseTransport
from pipecat.turns.user_start import ExternalUserTurnStartStrategy
from pipecat.turns.user_stop import ExternalUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from agent import db, meetings, prompt, runtime
from agent.config import settings
from agent.tools import HANDLERS, build_tools

SAMPLE_RATE = 16000
FILLERS = ("One moment.", "Okay, just a second.", "Right, give me a moment.")
DEVANAGARI = re.compile(r"[\u0900-\u097F]")


def reply_language(caller_text: str, current: str) -> str:
    """The voice language for the agent's next reply, following the caller between English and Hindi.

    With codemix transcripts, Hindi words arrive in Devanagari and English words in Latin letters.
    Codemix also writes the odd English phrase in Devanagari ("गो विद 3 PM"), so it takes a mostly
    Hindi turn to switch to Hindi, and a full English sentence to switch back: a short "okay" from
    a Hindi speaker must not flip the voice.
    """
    words = caller_text.split()
    if not words:
        return current
    hindi = sum(1 for w in words if DEVANAGARI.search(w)) / len(words)
    if hindi >= 0.5:
        return "hi-IN"
    if current == "hi-IN" and hindi < 0.2 and len(words) >= 4:
        return "en-IN"
    return current


@dataclass
class Call:
    id: str
    channel: str  # browser | phone | simulated
    caller: str | None = None
    tester: str | None = None  # name typed on the call page by whoever is testing
    record: dict = field(default_factory=dict)  # columns written to the calls row at the end
    transcript: list[dict] = field(default_factory=list)
    send_captions: bool = False
    personas: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"llm_in": 0, "llm_out": 0, "tts_chars": 0})
    caption: object = None  # async (role, text) -> None; set by run_call so tools can add to the transcript
    _pending_latency: dict | None = None


def _ms(seconds: float | None) -> int | None:
    return None if seconds is None else int(round(seconds * 1000))


def _service_ms(breakdown, kind: str) -> int | None:
    # Match the class-name suffix: a bare "TTS" would also match "...STTService".
    total = [m.duration_secs for m in breakdown.ttfb if f"{kind}Service" in m.processor]
    return _ms(sum(total)) if total else None


def estimate_cost(duration_secs: float, llm_in: int, llm_out: int, tts_chars: int) -> float:
    """INR, ex-GST. STT bills the whole streamed call, not just the caller's speech."""
    return round(
        duration_secs / 3600 * settings.price_stt_per_hour
        + tts_chars / 1000 * settings.price_tts_per_1k_chars
        + llm_in / 1e6 * settings.price_llm_in_per_1m
        + llm_out / 1e6 * settings.price_llm_out_per_1m,
        2,
    )


def startup_error_message(error: object) -> str:
    """Turn a provider failure into a useful, key-safe message for a tester."""
    detail = str(error).lower()
    if "invalid_api_key" in detail or "403" in detail or "forbidden" in detail:
        return "Sarvam rejected this API key. Create/copy a new key, save it in .env, then restart Start."
    if "insufficient_quota" in detail or "402" in detail or "quota" in detail:
        return "This Sarvam account has no credit left. Add credit in the Sarvam dashboard, then start again."
    if "rate_limit" in detail or "429" in detail:
        return "Sarvam is temporarily rate-limiting this account. Wait a minute, then try again."
    return "Sarvam's voice service couldn't start. Please try once more; if it repeats, send the call reference to Akhil."


class _CallObserver(BaseObserver):
    """Watches frames for: first agent audio (connected), agent speech starts (cancels the
    filler timer), and usage metrics (cost). Observers see a frame at every hop, so each
    frame is counted once by id."""

    def __init__(self, call: Call, on_first_audio, on_bot_speaking):
        super().__init__()
        self._call, self._on_first, self._on_speaking = call, on_first_audio, on_bot_speaking
        self._connected = False
        self._seen: set[int] = set()

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if isinstance(frame, BotStartedSpeakingFrame):
            if frame.id in self._seen:
                return
            self._seen.add(frame.id)
            self._on_speaking()
            if not self._connected:
                self._connected = True
                await self._on_first()
        elif isinstance(frame, MetricsFrame) and frame.id not in self._seen:
            self._seen.add(frame.id)
            for m in frame.data:
                if isinstance(m, LLMUsageMetricsData):
                    self._call.usage["llm_in"] += m.value.prompt_tokens or 0
                    self._call.usage["llm_out"] += m.value.completion_tokens or 0
                elif isinstance(m, TTSUsageMetricsData):
                    self._call.usage["tts_chars"] += m.value or 0


def build_services(rt: dict, system_prompt: str):
    key = settings.sarvam_api_key
    stt_settings = SarvamRealtimeSTTService.Settings(
        language_code=rt["stt_language"], mode=rt["stt_mode"], stream_type=rt["stt_stream_type"],
        prompt=rt["stt_prompt"] or None,
    )
    if rt["turn_mode"] == "sarvam":
        stt_settings.silence_duration_ms = rt["stt_silence_ms"]
    stt = SarvamRealtimeSTTService(
        api_key=key,
        sample_rate=SAMPLE_RATE,
        # smart: our VAD + Smart Turn decide the end of turn and tell Sarvam to flush right away.
        endpointing="manual" if rt["turn_mode"] == "smart" else "vad",
        settings=stt_settings,
    )
    llm = SarvamLLMService(
        api_key=key,
        settings=SarvamLLMService.Settings(
            model=rt["llm_model"],
            system_instruction=system_prompt,
            temperature=rt["llm_temperature"],
            max_tokens=rt["llm_max_tokens"],
        ),
        # A request that produces nothing for this long is re-sent once (the 18 s stall in testing).
        retry_timeout_secs=rt["llm_timeout_secs"],
        retry_on_timeout=True,
    )
    for name, handler in HANDLERS.items():
        llm.register_function(name, handler, cancel_on_interruption=False)
    tts = SarvamTTSService(
        api_key=key,
        sample_rate=SAMPLE_RATE,
        settings=SarvamTTSService.Settings(
            model=rt["tts_model"], voice=rt["tts_voice"], language=rt["tts_language"],
            pace=rt["tts_pace"], min_buffer_size=rt["tts_min_buffer"],
        ),
    )
    return stt, llm, tts


def turn_params(rt: dict) -> LLMUserAggregatorParams:
    idle = max(8.0, rt["idle_hangup_secs"] / 2)  # nudge once, then hang up on the second idle
    if rt["turn_mode"] == "smart":
        return LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=rt["vad_stop_secs"])),
            user_turn_strategies=UserTurnStrategies(),  # VAD start + Smart Turn v3 stop (on-device)
            user_idle_timeout=idle,
        )
    return LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(),
        # Sarvam's server-side VAD decides when the caller stopped; then wait only briefly
        # for a straggling transcript, since that wait is added to every reply.
        user_turn_strategies=UserTurnStrategies(
            start=[ExternalUserTurnStartStrategy(enable_interruptions=True)],
            stop=[ExternalUserTurnStopStrategy(timeout=rt["turn_stop_wait_secs"])],
        ),
        user_idle_timeout=idle,
    )


def call_prompt() -> tuple[int, dict, str]:
    """(prompt version, document, rendered system prompt) for a call starting now."""
    version, doc = prompt.active()
    at = meetings.now()
    values = prompt.variables(doc, meetings.calendar_days_text(at), meetings.today_text(at))
    return version, doc, prompt.render(doc, values)


async def run_call(transport: BaseTransport, call: Call) -> None:
    db.insert_call(call.id, call.channel, call.caller, call.tester)
    rt = runtime.load()
    worker: PipelineWorker | None = None
    timers: dict[str, asyncio.Task] = {}
    started = {"connected": None, "client": None}
    voice = {"language": rt["tts_language"]}

    try:
        version, doc, system_prompt = call_prompt()
        greeting = prompt.opening_line(doc)
        call.personas = prompt.persona_tags(doc)
        call.record.update(llm_model=rt["llm_model"], prompt_version=version, runtime_settings=rt)

        stt, llm, tts = build_services(rt, system_prompt)
        context = LLMContext(tools=build_tools(doc))
        user_agg, assistant_agg = LLMContextAggregatorPair(context, user_params=turn_params(rt))
        recorder = AudioBufferProcessor(num_channels=2)  # left = caller, right = agent

        pipeline = Pipeline(
            [transport.input(), stt, user_agg, llm, tts, transport.output(), recorder, assistant_agg]
        )

        latency = UserBotLatencyObserver()

        def cancel_timer(name: str):
            task = timers.pop(name, None)
            if task:
                task.cancel()

        async def mark_connected():
            started["connected"] = time.monotonic()
            call.record["connected_at"] = db.now()
            db.update_call(call.id, connected_at=call.record["connected_at"])
            if started["client"] is not None:  # greeting latency: socket open -> first audio
                db.insert_turn(call.id, "greeting", _ms(started["connected"] - started["client"]))

        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=SAMPLE_RATE, audio_out_sample_rate=SAMPLE_RATE,
                enable_metrics=True, enable_usage_metrics=True,
            ),
            observers=[latency, _CallObserver(call, mark_connected, lambda: cancel_timer("filler"))],
            app_resources=call,
            enable_rtvi=False,
            idle_timeout_secs=max(60, rt["idle_hangup_secs"] * 2),
        )

        @worker.event_handler("on_pipeline_error")
        async def _pipeline_error(_worker, frame):
            # Typically a bad/expired key or exhausted credit. If the caller never heard
            # the agent, the call can't recover: tell them why and hang up.
            call.record.setdefault("error", str(frame.error)[:1000])
            if "connected_at" not in call.record and "end_reason" not in call.record:
                call.record["end_reason"] = "error"
                if call.send_captions:
                    await worker.queue_frame(
                        OutputTransportMessageUrgentFrame(
                            message={"type": "error", "text": startup_error_message(frame.error)}
                        )
                    )
                await worker.cancel()

        async def caption(role: str, text: str):
            call.transcript.append({"role": role, "text": text, "at": db.now()})
            if call.send_captions:
                await worker.queue_frame(
                    OutputTransportMessageUrgentFrame(message={"type": "caption", "role": role, "text": text})
                )

        async def filler_after(delay: float):
            # The line must never go dead: if no reply has started by now, hold the floor.
            await asyncio.sleep(delay)
            timers.pop("filler", None)
            if "end_reason" in call.record:
                return
            call.record["filler_count"] = call.record.get("filler_count", 0) + 1
            line = FILLERS[(call.record["filler_count"] - 1) % len(FILLERS)]
            await caption("agent", line + " [filler]")
            await worker.queue_frame(TTSSpeakFrame(line, append_to_context=False))

        call.caption = caption

        @user_agg.event_handler("on_user_turn_started")
        async def _user_started(_agg, _strategy):
            cancel_timer("filler")

        @user_agg.event_handler("on_user_turn_stopped")
        async def _user_said(_agg, _strategy, message):
            call.record["idle_nudges"] = 0
            if message.content:
                await caption("user", message.content)
                # Only between English and Hindi: another configured language is left alone.
                if voice["language"] in ("en-IN", "hi-IN"):
                    language = reply_language(message.content, voice["language"])
                    if language != voice["language"]:
                        voice["language"] = language
                        await worker.queue_frame(
                            TTSUpdateSettingsFrame(delta=SarvamTTSService.Settings(language=language), service=tts)
                        )
            if rt["filler_after_secs"] > 0 and "end_reason" not in call.record:
                cancel_timer("filler")
                timers["filler"] = asyncio.create_task(filler_after(rt["filler_after_secs"]))

        @user_agg.event_handler("on_user_turn_idle")
        async def _user_idle(_agg):
            nudges = call.record.get("idle_nudges", 0)
            if "end_reason" in call.record:
                return
            if nudges == 0:
                call.record["idle_nudges"] = 1
                await caption("agent", "Hello, are you still there?")
                await worker.queue_frame(TTSSpeakFrame("Hello, are you still there?", append_to_context=False))
            else:
                call.record["end_reason"] = "idle"
                await caption("agent", "I'll let you go. Thank you, goodbye!")
                await worker.queue_frames([TTSSpeakFrame("I'll let you go. Thank you, goodbye!", append_to_context=False), EndFrame()])

        @assistant_agg.event_handler("on_assistant_turn_stopped")
        async def _agent_said(_agg, message):
            text = (message.content or "").strip()
            last = call.transcript[-1] if call.transcript else {}
            # end_call's closing line is already captioned by the tool when it is spoken.
            if text and text != greeting and (last.get("role"), last.get("text")) != ("agent", text):
                await caption("agent", text + (" [interrupted]" if message.interrupted else ""))

        @latency.event_handler("on_latency_measured")
        async def _reply_latency(_obs, secs):
            call._pending_latency = {"kind": "response", "ms": _ms(secs)}

        @latency.event_handler("on_latency_breakdown")
        async def _breakdown(_obs, breakdown):
            pending, call._pending_latency = call._pending_latency, None
            if pending and pending["ms"] is not None:
                db.insert_turn(
                    call.id,
                    pending["kind"],
                    pending["ms"],
                    stt_ms=_service_ms(breakdown, "STT"),
                    llm_ms=_service_ms(breakdown, "LLM"),
                    tts_ms=_service_ms(breakdown, "TTS"),
                )

        @recorder.event_handler("on_audio_data")
        async def _save_recording(_rec, audio: bytes, sample_rate: int, channels: int):
            if not audio:
                return
            settings.recordings_dir.mkdir(parents=True, exist_ok=True)
            path = settings.recordings_dir / f"{call.id}.wav"
            with wave.open(str(path), "wb") as f:
                f.setnchannels(channels)
                f.setsampwidth(2)
                f.setframerate(sample_rate)
                f.writeframes(audio)
            call.record["recording"] = path.name

        @transport.event_handler("on_client_connected")
        async def _connected(_transport, _client):
            started["client"] = time.monotonic()
            await recorder.start_recording()
            if call.send_captions:  # lets the call page / simulator link to this call's record
                await worker.queue_frame(OutputTransportMessageUrgentFrame(message={"type": "call", "id": call.id}))
            await caption("agent", greeting)
            # Spoken verbatim, not generated: the AI disclosure can't be skipped or reworded.
            await worker.queue_frame(TTSSpeakFrame(greeting))

        @transport.event_handler("on_client_disconnected")
        async def _disconnected(_transport, _client):
            call.record.setdefault("end_reason", "caller_hangup")
            await recorder.stop_recording()
            await worker.cancel()

        async def _call_time_limit():
            await asyncio.sleep(rt["max_call_seconds"])
            if "end_reason" not in call.record:
                call.record["end_reason"] = "timeout"
                await worker.queue_frames(
                    [TTSSpeakFrame("We've reached the time limit for this call. Thank you, goodbye!"), EndFrame()]
                )

        timers["limit"] = asyncio.create_task(_call_time_limit())
        try:
            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(worker)
            await runner.run()
        finally:
            await recorder.stop_recording()
    except Exception as e:  # noqa: BLE001 - any failure must still be logged as a call
        logger.exception(f"call {call.id} failed")
        call.record["end_reason"] = "error"
        call.record["error"] = f"{type(e).__name__}: {e}"[:1000]
    finally:
        for task in timers.values():
            task.cancel()
        call.record.setdefault("end_reason", "caller_hangup")
        call.record.pop("idle_nudges", None)
        if call.record.get("booking_id") and not call.record.get("agent_outcome"):
            # A real calendar booking is the outcome even if the caller hung up before the wrap-up.
            call.record.update(next_step="meeting_booked", agent_outcome="meeting_booked")
        duration = time.monotonic() - started["client"] if started["client"] else 0.0
        call.record.update(
            duration_secs=round(duration, 1),
            llm_in_tokens=call.usage["llm_in"],
            llm_out_tokens=call.usage["llm_out"],
            tts_chars=call.usage["tts_chars"],
            cost_inr=estimate_cost(duration, call.usage["llm_in"], call.usage["llm_out"], call.usage["tts_chars"]),
        )
        db.update_call(call.id, ended_at=db.now(), transcript=call.transcript, **call.record)
        logger.info(
            f"call {call.id} ended: {call.record.get('end_reason')} / {call.record.get('agent_outcome')} "
            f"/ ₹{call.record['cost_inr']}"
        )
