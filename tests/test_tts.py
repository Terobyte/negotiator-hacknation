from pathlib import Path

import pytest

from negotiator.call.gate import HonestyGate
from negotiator.call.prosody import voice_settings
from negotiator.call.tts import (
    ElevenLabsTTS,
    ElevenLabsTTSConfig,
    UnapprovedUtteranceError,
)
from negotiator.core.contracts import ApprovedUtterance, SEED_CALL_CARD


def approved(text: str = "Hello, I am an AI assistant.") -> ApprovedUtterance:
    decision = HonestyGate(stall_phrases=("Let me check.",)).evaluate(
        draft=text, card=SEED_CALL_CARD, ledger_facts=()
    )
    assert decision.approved is not None
    return decision.approved


def test_deterministic_fallback_does_not_poison_live_cache(tmp_path: Path) -> None:
    adapter = ElevenLabsTTS(
        ElevenLabsTTSConfig(voice_id="voice"), cache_dir=tmp_path
    )
    first = adapter.synthesize(approved(), voice_settings("OPENING"))
    second = adapter.synthesize(approved(), voice_settings("OPENING"))
    assert first.source == "deterministic"
    assert second.source == "deterministic"
    assert first.audio == second.audio
    assert len(first.audio) > 100
    assert list(tmp_path.iterdir()) == []


def test_live_request_shape_uses_flash_v25_and_caches_real_audio(tmp_path: Path) -> None:
    seen = {}
    calls = 0

    def transport(url, headers, body, timeout):
        nonlocal calls
        calls += 1
        seen.update(url=url, headers=headers, body=body, timeout=timeout)
        return b"pcm", "audio/pcm"

    adapter = ElevenLabsTTS(
        ElevenLabsTTSConfig(voice_id="voice/id", api_key="not-a-secret"),
        cache_dir=tmp_path,
        transport=transport,
    )
    result = adapter.synthesize(approved(), voice_settings("LEVERAGE"))
    cached = adapter.synthesize(approved(), voice_settings("LEVERAGE"))
    assert result.source == "elevenlabs"
    assert cached.source == "cache" and cached.audio == b"pcm"
    assert calls == 1
    assert b'"model_id":"eleven_flash_v2_5"' in seen["body"]
    assert "voice%2Fid" in seen["url"]
    assert seen["headers"]["xi-api-key"] == "not-a-secret"


def test_tts_rejects_direct_and_forged_utterances() -> None:
    with pytest.raises(TypeError):
        ApprovedUtterance(text="forged", card_version=1, gate_verdict_ref="fake")
    forged = ApprovedUtterance.model_construct(
        text="forged", card_version=1, gate_verdict_ref="fake"
    )
    adapter = ElevenLabsTTS(ElevenLabsTTSConfig(voice_id="voice"))
    with pytest.raises(UnapprovedUtteranceError):
        adapter.synthesize(forged, voice_settings("OPENING"))


def test_tts_rejects_slow_hot_path_settings() -> None:
    adapter = ElevenLabsTTS(ElevenLabsTTSConfig(voice_id="voice"))
    with pytest.raises(ValueError, match="style=0"):
        adapter.synthesize(approved(), {"style": 0.2})
