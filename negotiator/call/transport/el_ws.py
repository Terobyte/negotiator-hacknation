from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol


class WebSocketLike(Protocol):
    async def send(self, data: str | bytes) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


Connector = Callable[[str, Mapping[str, str]], Awaitable[WebSocketLike]]


@dataclass(frozen=True, slots=True)
class ElevenLabsAgentConfig:
    agent_id: str
    signed_url: str | None = None
    api_key: str | None = None
    websocket_base: str = "wss://api.elevenlabs.io/v1/convai/conversation"
    watchdog_s: float = 10.0

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("agent_id is required")

    def connection_url(self) -> str:
        if self.signed_url:
            return self.signed_url
        return f"{self.websocket_base}?{urllib.parse.urlencode({'agent_id': self.agent_id})}"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    type: str
    audio: bytes | None = None
    text: str | None = None
    event_id: int | str | None = None
    raw: Mapping[str, Any] | None = None


class ElevenLabsAgentBridge:
    """Direct sim-market bridge to the same ElevenLabs counter-agents used by Twilio."""

    def __init__(
        self,
        config: ElevenLabsAgentConfig,
        *,
        connector: Connector | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._connector = connector or _websockets_connector
        self._clock = clock
        self._socket: WebSocketLike | None = None
        self.last_activity: float | None = None

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def watchdog_expired(self, *, now: float | None = None) -> bool:
        return self.last_activity is not None and (
            (self._clock() if now is None else now) - self.last_activity > self.config.watchdog_s
        )

    async def connect(
        self,
        *,
        dynamic_variables: Mapping[str, str] | None = None,
        custom_llm_extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        headers: dict[str, str] = {}
        api_key = self.config.api_key or os.getenv("ELEVENLABS_API_KEY")
        if api_key:
            headers["xi-api-key"] = api_key
        self._socket = await self._connector(self.config.connection_url(), headers)
        self.last_activity = self._clock()
        if dynamic_variables or custom_llm_extra_body:
            await self._send(
                encode_initiation(dynamic_variables or {}, custom_llm_extra_body or {})
            )

    async def send_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        await self._send(encode_user_audio(pcm))

    async def receive(self) -> AgentEvent:
        if self._socket is None:
            raise RuntimeError("ElevenLabs agent bridge is not connected")
        event = decode_agent_event(await self._socket.recv())
        self.last_activity = self._clock()
        if event.type == "ping" and event.event_id is not None:
            await self._send(encode_pong(event.event_id))
        return event

    async def close(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            await socket.close()

    async def _send(self, message: str) -> None:
        if self._socket is None:
            raise RuntimeError("ElevenLabs agent bridge is not connected")
        await self._socket.send(message)
        self.last_activity = self._clock()


def encode_user_audio(pcm: bytes) -> str:
    return json.dumps(
        {"user_audio_chunk": base64.b64encode(pcm).decode("ascii")}, separators=(",", ":")
    )


def encode_initiation(
    dynamic_variables: Mapping[str, str], custom_llm_extra_body: Mapping[str, Any]
) -> str:
    data: dict[str, Any] = {
        "type": "conversation_initiation_client_data",
        "dynamic_variables": dict(dynamic_variables),
    }
    if custom_llm_extra_body:
        data["custom_llm_extra_body"] = dict(custom_llm_extra_body)
    return json.dumps(data, separators=(",", ":"))


def encode_pong(event_id: int | str) -> str:
    return json.dumps({"type": "pong", "event_id": event_id}, separators=(",", ":"))


def decode_agent_event(raw: str | bytes) -> AgentEvent:
    if isinstance(raw, bytes):
        raw = raw.decode()
    message = json.loads(raw)
    event_type = str(message.get("type", "unknown"))
    if event_type == "audio":
        data = message.get("audio_event", {})
        encoded = data.get("audio_base_64") or data.get("audio_base64")
        return AgentEvent(event_type, audio=base64.b64decode(encoded) if encoded else b"", raw=message)
    if event_type in {"agent_response", "user_transcript"}:
        data = message.get(f"{event_type}_event", {})
        text = data.get("agent_response") or data.get("user_transcript") or data.get("text")
        return AgentEvent(event_type, text=str(text) if text is not None else None, raw=message)
    if event_type == "ping":
        data = message.get("ping_event", {})
        return AgentEvent(event_type, event_id=data.get("event_id", message.get("event_id")), raw=message)
    if event_type == "interruption":
        data = message.get("interruption_event", {})
        return AgentEvent(event_type, event_id=data.get("event_id"), raw=message)
    return AgentEvent(event_type, raw=message)


async def _websockets_connector(url: str, headers: Mapping[str, str]) -> WebSocketLike:
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("install negotiator[voice] for the live ElevenLabs bridge") from exc
    return await websockets.connect(url, additional_headers=dict(headers), ping_interval=5)


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.responses = [
            json.dumps({"type": "ping", "ping_event": {"event_id": 7}}),
            json.dumps(
                {
                    "type": "audio",
                    "audio_event": {"audio_base_64": base64.b64encode(b"fake-pcm").decode()},
                }
            ),
        ]

    async def send(self, data: str | bytes) -> None:
        self.sent.append(data)

    async def recv(self) -> str | bytes:
        return self.responses.pop(0)

    async def close(self) -> None:
        return None


async def smoke() -> dict[str, Any]:
    fake = _FakeSocket()

    async def connector(url: str, headers: Mapping[str, str]) -> WebSocketLike:
        return fake

    bridge = ElevenLabsAgentBridge(
        ElevenLabsAgentConfig(agent_id="offline-smoke", signed_url="wss://offline.invalid"),
        connector=connector,
    )
    await bridge.connect(dynamic_variables={"role": "pressure_closer"})
    await bridge.send_audio(b"caller-pcm")
    ping = await bridge.receive()
    audio = await bridge.receive()
    await bridge.close()
    return {"ok": ping.type == "ping" and audio.audio == b"fake-pcm", "sent_messages": len(fake.sent)}


def main() -> None:
    parser = argparse.ArgumentParser(description="ElevenLabs direct counter-agent WebSocket bridge")
    parser.add_argument("--smoke", action="store_true", help="run the network-free protocol smoke")
    args = parser.parse_args()
    if not args.smoke:
        parser.error("only --smoke is supported; live wiring belongs in app.py")
    print(json.dumps(asyncio.run(smoke()), sort_keys=True))


if __name__ == "__main__":
    main()
