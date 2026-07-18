from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass(frozen=True, slots=True)
class PCMFrame:
    data: bytes
    sample_rate: int = 16_000
    channels: int = 1


class AudioTransport(Protocol):
    """Minimal boundary shared by browser transports; signaling stays adapter-specific."""

    async def send(self, frame: PCMFrame) -> None: ...
    def receive(self) -> AsyncIterator[PCMFrame]: ...
    async def close(self) -> None: ...
