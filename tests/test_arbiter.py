import json

from negotiator.call.arbiter import Arbiter, Turn, VadEvent, VadKind, replay


def test_barge_in_yields_to_counterparty_and_counts_interrupt():
    arbiter = Arbiter()
    arbiter.apply(VadEvent(kind=VadKind.AGENT_STARTED, at=1))
    assert arbiter.apply(VadEvent(kind=VadKind.COUNTERPARTY_STARTED, at=1.1)) is Turn.COUNTERPARTY
    assert arbiter.interruptions == 1


def test_tactical_pause_blocks_agent_until_deadline():
    arbiter = Arbiter()
    arbiter.apply(VadEvent(kind=VadKind.TACTICAL_PAUSE, at=2, duration=1.5))
    assert arbiter.apply(VadEvent(kind=VadKind.AGENT_STARTED, at=3)) is Turn.SILENCE
    assert arbiter.apply(VadEvent(kind=VadKind.AGENT_STARTED, at=3.5)) is Turn.AGENT


def test_replay_smoke(tmp_path, capsys):
    fixture = tmp_path / "vad.jsonl"
    fixture.write_text("\n".join(json.dumps(row) for row in [{"kind": "agent_started", "at": 0}, {"kind": "counterparty_started", "at": 0.2}]))
    result = replay(fixture)
    assert result.turn is Turn.COUNTERPARTY
    assert len(capsys.readouterr().out.splitlines()) == 2
