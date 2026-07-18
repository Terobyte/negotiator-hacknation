from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol


class WebSocketLike(Protocol):
    async def send(self, data: str | bytes) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


Connector = Callable[[str, Mapping[str, str]], Awaitable[WebSocketLike]]


@dataclass(frozen=True, slots=True)
class DeepgramConfig:
    api_key: str | None = None
    endpoint: str = "wss://api.deepgram.com/v1/listen"
    model: str = "nova-2-phonecall"
    language: str = "en-US"
    encoding: str = "linear16"
    sample_rate: int = 8_000
    channels: int = 1
    interim_results: bool = True
    endpointing_ms: int = 100
    utterance_end_ms: int = 1_000
    watchdog_s: float = 8.0

    def url(self) -> str:
        query = urllib.parse.urlencode(
            {
                "model": self.model,
                "language": self.language,
                "encoding": self.encoding,
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "interim_results": str(self.interim_results).lower(),
                "endpointing": self.endpointing_ms,
                "utterance_end_ms": self.utterance_end_ms,
                "smart_format": "true",
            }
        )
        return f"{self.endpoint}?{query}"


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    is_final: bool
    speech_final: bool
    confidence: float
    start_s: float | None = None
    duration_s: float | None = None


class DeepgramStream:
    """Small reconnectable Deepgram stream; lifecycle is owned by the call supervisor."""

    def __init__(
        self,
        config: DeepgramConfig = DeepgramConfig(),
        *,
        connector: Connector | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._connector = connector or _websockets_connector
        self._clock = clock
        self._socket: WebSocketLike | None = None
        self.last_activity: float | None = None
        self.generation = 0

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def watchdog_expired(self, *, now: float | None = None) -> bool:
        if self.last_activity is None:
            return False
        return (self._clock() if now is None else now) - self.last_activity > self.config.watchdog_s

    async def connect(self) -> int:
        if self._socket is not None:
            await self.close()
        api_key = self.config.api_key or os.getenv("DEEPGRAM_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY is not configured")
        self._socket = await self._connector(
            self.config.url(), {"Authorization": f"Token {api_key}"}
        )
        self.generation += 1
        self.last_activity = self._clock()
        return self.generation

    async def reconnect(self) -> int:
        return await self.connect()

    async def send_audio(self, pcm: bytes) -> None:
        if self._socket is None:
            raise RuntimeError("Deepgram stream is not connected")
        if not pcm:
            return
        await self._socket.send(pcm)
        self.last_activity = self._clock()

    async def keep_alive(self) -> None:
        await self._send_control({"type": "KeepAlive"})

    async def finalize(self) -> None:
        await self._send_control({"type": "Finalize"})

    async def receive(self) -> Transcript | None:
        if self._socket is None:
            raise RuntimeError("Deepgram stream is not connected")
        raw = await self._socket.recv()
        self.last_activity = self._clock()
        return decode_deepgram_message(raw)

    async def transcripts(self) -> AsyncIterator[Transcript]:
        while self._socket is not None:
            transcript = await self.receive()
            if transcript is not None:
                yield transcript

    async def close(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                await socket.send(json.dumps({"type": "CloseStream"}))
            finally:
                await socket.close()

    async def _send_control(self, message: Mapping[str, Any]) -> None:
        if self._socket is None:
            raise RuntimeError("Deepgram stream is not connected")
        await self._socket.send(json.dumps(message, separators=(",", ":")))
        self.last_activity = self._clock()


def decode_deepgram_message(raw: str | bytes) -> Transcript | None:
    if isinstance(raw, bytes):
        raw = raw.decode()
    message = json.loads(raw)
    if message.get("type") != "Results":
        return None
    channel = message.get("channel", {})
    alternatives = channel.get("alternatives") or ()
    if not alternatives:
        return None
    best = alternatives[0]
    text = str(best.get("transcript", "")).strip()
    if not text:
        return None
    return Transcript(
        text=text,
        is_final=bool(message.get("is_final")),
        speech_final=bool(message.get("speech_final")),
        confidence=float(best.get("confidence", 0.0)),
        start_s=_optional_float(message.get("start")),
        duration_s=_optional_float(message.get("duration")),
    )


async def _websockets_connector(url: str, headers: Mapping[str, str]) -> WebSocketLike:
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("install negotiator[voice] for live STT") from exc
    return await websockets.connect(url, additional_headers=dict(headers), ping_interval=5)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
