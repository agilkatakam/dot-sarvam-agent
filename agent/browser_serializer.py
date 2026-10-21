"""Wire format for the browser call page (static/call.html).

Browser -> server: binary = raw 16-bit little-endian mono PCM at 16 kHz.
Server -> browser: binary = raw 16-bit PCM at the pipeline output rate (16 kHz),
                   text   = JSON control messages: {"type": "clear"} (caller barged in,
                            drop queued audio), {"type": "end"} (agent hung up),
                            or any app message (e.g. live captions).

Plain PCM over one WebSocket (rather than WebRTC) so the call works through any
HTTPS tunnel with no TURN server.
"""

import json

from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

BROWSER_SAMPLE_RATE = 16000


class BrowserPCMSerializer(FrameSerializer):
    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"type": "clear"})
        if isinstance(frame, EndFrame):
            return json.dumps({"type": "end"})
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            if self.should_ignore_frame(frame):
                return None
            return json.dumps(frame.message)
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, bytes) and data:
            # Drop a trailing odd byte rather than hand the resampler a half sample.
            audio = data[: len(data) - (len(data) % 2)]
            return InputAudioRawFrame(audio=audio, sample_rate=BROWSER_SAMPLE_RATE, num_channels=1)
        return None
