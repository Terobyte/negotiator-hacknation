"""Text-mode coverage for the two-agent call simulation. No network, no audio."""

from __future__ import annotations

from negotiator.core.contracts import NegotiationPhase
from negotiator.tools.duet import (
    _wav16,
    _write_playable,
    run_duet,
)

PHASE_ORDER = [
    NegotiationPhase.OPENING,
    NegotiationPhase.DISCOVERY,
    NegotiationPhase.PRESSURE_TEST,
    NegotiationPhase.LEVERAGE,
    NegotiationPhase.COMMIT,
    NegotiationPhase.WRAP,
]


def _leverage(result):
    return next(t for t in result.turns if t.phase is NegotiationPhase.LEVERAGE)


def test_full_phase_sequence_all_approved():
    result = run_duet(persona="pressure_closer")
    assert [t.phase for t in result.turns] == PHASE_ORDER
    assert result.gate_blocks == 0
    assert all(t.gate_verdict == "allow" for t in result.turns)


def test_gated_leverage_is_backed_by_the_ledger_and_wins_a_concession():
    result = run_duet(persona="pressure_closer", competitor_quote=3000)
    lev = _leverage(result)
    assert lev.gate_verdict == "allow"
    assert "$3,000" in lev.negotiator            # the documented competitor quote
    assert result.opening_total == 5600
    assert result.final_total == 5200
    assert result.concession == 400


def test_bluff_is_refused_by_the_honesty_gate():
    result = run_duet(persona="pressure_closer", bluff=True)
    lev = _leverage(result)
    assert lev.gate_verdict == "block"
    assert lev.gate_reason == "unsupported_quote_amount"
    assert "$1,500" not in lev.negotiator         # the fabricated number never reaches TTS
    assert "check my notes" in lev.negotiator.lower()
    # No leverage was legitimately applied, so the provider does not concede.
    assert result.final_total == result.opening_total
    assert result.gate_blocks == 1


def test_lowball_broker_prices_relative_to_benchmark():
    result = run_duet(persona="lowball_broker", benchmark_low=4000)
    assert result.opening_total == 2600           # round(4000 * 0.65)
    # A persona with no concession rule holds firm even against real leverage.
    assert _leverage(result).gate_verdict == "allow"
    assert result.final_total == result.opening_total


def test_rushed_dispatcher_itemizes_hidden_fees_on_request():
    result = run_duet(persona="rushed_dispatcher")
    discovery = next(t for t in result.turns if t.phase is NegotiationPhase.DISCOVERY)
    # hidden_line_item_codes [4, 7, 11] -> unpacking, elevator, storage
    for fee in ("unpacking", "elevator", "storage"):
        assert fee in discovery.carrier


def test_offline_audio_fallback_is_a_valid_wav(tmp_path):
    # The deterministic branch writes a real RIFF/WAVE container afplay can read.
    pcm = b"\x00\x01" * 800
    path = _write_playable(pcm, "deterministic", tmp_path / "clip")
    assert path.suffix == ".wav"
    data = path.read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    assert data.endswith(pcm)


def test_real_audio_branch_is_written_as_mp3(tmp_path):
    audio = b"ID3fake-mp3-bytes"
    path = _write_playable(audio, "elevenlabs", tmp_path / "clip")
    assert path.suffix == ".mp3"
    assert path.read_bytes() == audio


def test_wav_header_is_well_formed():
    header = _wav16(b"\x00\x00" * 10, 16_000)[:44]
    assert header[:4] == b"RIFF"
    assert header[22:24] == (1).to_bytes(2, "little")     # mono
    assert header[24:28] == (16_000).to_bytes(4, "little")  # sample rate
