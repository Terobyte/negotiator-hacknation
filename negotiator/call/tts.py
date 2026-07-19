from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from negotiator.core.contracts import ApprovedUtterance


class UnapprovedUtteranceError(TypeError):
    pass


@dataclass(frozen=True, slots=True)
class ElevenLabsTTSConfig:
    voice_id: str
    api_key: str | None = None
    model_id: str = "eleven_flash_v2_5"
    output_format: str = "ulaw_8000"
    base_url: str = "https://api.elevenlabs.io"
    optimize_streaming_latency: int = 3
    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if not self.voice_id.strip():
            raise ValueError("voice_id is required")
        if self.model_id != "eleven_flash_v2_5":
            raise ValueError("phase 3 TTS must use ElevenLabs Flash v2.5")


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    audio: bytes
    content_type: str
    source: str
    cache_key: str


Transport = Callable[[str, Mapping[str, str], bytes, float], tuple[bytes, str]]


class ElevenLabsTTS:
    """Flash v2.5 adapter with a disk cache and deterministic offline fallback."""

    def __init__(
        self,
        config: ElevenLabsTTSConfig,
        *,
        cache_dir: str | Path | None = None,
        transport: Transport | None = None,
        deterministic_fallback: bool = True,
    ) -> None:
        self.config = config
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._transport = transport or _http_transport
        self.deterministic_fallback = deterministic_fallback

    def synthesize(
        self, utterance: ApprovedUtterance, voice_settings: Mapping[str, Any]
    ) -> SynthesisResult:
        if not isinstance(utterance, ApprovedUtterance) or not utterance.gate_issued:
            raise UnapprovedUtteranceError("TTS accepts only a gate-issued ApprovedUtterance")
        settings = _validated_settings(voice_settings)
        payload = {
            "text": utterance.text,
            "model_id": self.config.model_id,
            "voice_settings": settings,
        }
        key = hashlib.sha256(
            json.dumps(
                {"voice_id": self.config.voice_id, "output_format": self.config.output_format, **payload},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        cached = self._read_cache(key)
        if cached is not None:
            return SynthesisResult(cached, _content_type(self.config.output_format), "cache", key)

        api_key = self.config.api_key or os.getenv("ELEVENLABS_API_KEY")
        if api_key:
            query = urllib.parse.urlencode(
                {
                    "output_format": self.config.output_format,
                    "optimize_streaming_latency": self.config.optimize_streaming_latency,
                }
            )
            url = (
                f"{self.config.base_url.rstrip('/')}/v1/text-to-speech/"
                f"{urllib.parse.quote(self.config.voice_id, safe='')}?{query}"
            )
            try:
                audio, content_type = self._transport(
                    url,
                    {"xi-api-key": api_key, "content-type": "application/json", "accept": "audio/*"},
                    json.dumps(payload, separators=(",", ":")).encode(),
                    self.config.timeout_s,
                )
                if not audio:
                    raise RuntimeError("ElevenLabs returned empty audio")
                self._write_cache(key, audio)
                return SynthesisResult(audio, content_type, "elevenlabs", key)
            except (OSError, RuntimeError, urllib.error.URLError):
                if not self.deterministic_fallback:
                    raise
        elif not self.deterministic_fallback:
            raise RuntimeError("ELEVENLABS_API_KEY is not configured")

        audio = deterministic_pcm(utterance.text, sample_rate=_sample_rate(self.config.output_format))
        if self.config.output_format.startswith("ulaw_"):
            audio = _pcm16_to_mulaw(audio)
        return SynthesisResult(audio, _content_type(self.config.output_format), "deterministic", key)

    def _read_cache(self, key: str) -> bytes | None:
        if self.cache_dir is None:
            return None
        path = self.cache_dir / f"{key}.audio"
        return path.read_bytes() if path.is_file() else None

    def _write_cache(self, key: str, audio: bytes) -> None:
        if self.cache_dir is None:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        destination = self.cache_dir / f"{key}.audio"
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(audio)
        temporary.replace(destination)


def deterministic_pcm(text: str, *, sample_rate: int = 16_000) -> bytes:
    """A repeatable, audible placeholder (raw signed 16-bit mono PCM)."""

    digest = hashlib.sha256(text.encode()).digest()
    frequency = 300 + int.from_bytes(digest[:2], "big") % 320
    duration_s = min(1.2, max(0.18, 0.035 * len(text.split())))
    amplitude = 2400
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * frequency * index / sample_rate)))
        for index in range(int(sample_rate * duration_s))
    )


def _validated_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"stability", "similarity_boost", "style", "speed", "use_speaker_boost"}
    unknown = set(settings) - allowed
    if unknown:
        raise ValueError(f"unknown ElevenLabs voice settings: {sorted(unknown)}")
    result = dict(settings)
    if result.get("style", 0) != 0 or result.get("use_speaker_boost", False) is not False:
        raise ValueError("hot-path TTS requires style=0 and use_speaker_boost=false")
    return result


def _http_transport(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> tuple[bytes, str]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers.get_content_type()


def _sample_rate(output_format: str) -> int:
    try:
        if output_format.startswith(("pcm_", "ulaw_")):
            return int(output_format.split("_", 1)[1])
        return 16_000
    except ValueError:
        return 16_000


def _content_type(output_format: str) -> str:
    if output_format.startswith("pcm_"):
        return "audio/pcm"
    if output_format.startswith("ulaw_"):
        return "audio/basic"
    return "audio/mpeg"


def _pcm16_to_mulaw(pcm: bytes) -> bytes:
    """Encode little-endian signed PCM16 as G.711 mu-law without audioop."""

    result = bytearray()
    for (sample,) in struct.iter_unpack("<h", pcm):
        sign = 0x80 if sample < 0 else 0
        magnitude = min(32635, abs(sample)) + 132
        exponent = max(0, magnitude.bit_length() - 8)
        mantissa = (magnitude >> (exponent + 3)) & 0x0F
        result.append(~(sign | (exponent << 4) | mantissa) & 0xFF)
    return bytes(result)
