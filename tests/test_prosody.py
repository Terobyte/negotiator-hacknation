from negotiator.call.prosody import PROSODY_PRESETS, voice_settings
from negotiator.core.contracts import NegotiationPhase


def test_all_phases_have_low_latency_voice_settings() -> None:
    assert set(PROSODY_PRESETS) == set(NegotiationPhase)
    for phase in NegotiationPhase:
        settings = voice_settings(phase)
        assert settings["style"] == 0
        assert settings["use_speaker_boost"] is False
        assert 0 <= settings["stability"] <= 1
        assert 0.7 <= settings["speed"] <= 1.2


def test_settings_are_a_fresh_mutable_copy() -> None:
    first = voice_settings("OPENING")
    first["stability"] = 1
    assert voice_settings("OPENING")["stability"] == 0.35
