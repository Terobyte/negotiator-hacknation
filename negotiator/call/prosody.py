from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from negotiator.core.contracts import NegotiationPhase


# Pure data: keep style and speaker boost off on the latency-sensitive path.
_PRESETS: Mapping[NegotiationPhase, Mapping[str, Any]] = MappingProxyType(
    {
        NegotiationPhase.OPENING: MappingProxyType(
            {"stability": 0.35, "similarity_boost": 0.75, "style": 0.0, "speed": 1.0, "use_speaker_boost": False}
        ),
        NegotiationPhase.DISCOVERY: MappingProxyType(
            {"stability": 0.45, "similarity_boost": 0.75, "style": 0.0, "speed": 1.0, "use_speaker_boost": False}
        ),
        NegotiationPhase.PRESSURE_TEST: MappingProxyType(
            {"stability": 0.55, "similarity_boost": 0.75, "style": 0.0, "speed": 0.98, "use_speaker_boost": False}
        ),
        NegotiationPhase.LEVERAGE: MappingProxyType(
            {"stability": 0.65, "similarity_boost": 0.75, "style": 0.0, "speed": 0.96, "use_speaker_boost": False}
        ),
        NegotiationPhase.COMMIT: MappingProxyType(
            {"stability": 0.62, "similarity_boost": 0.75, "style": 0.0, "speed": 0.98, "use_speaker_boost": False}
        ),
        NegotiationPhase.WRAP: MappingProxyType(
            {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "speed": 1.0, "use_speaker_boost": False}
        ),
    }
)


def voice_settings(phase: NegotiationPhase | str) -> dict[str, Any]:
    """Return a fresh ElevenLabs voice-settings object for a negotiation phase."""

    return dict(_PRESETS[NegotiationPhase(phase)])


PROSODY_PRESETS = _PRESETS
