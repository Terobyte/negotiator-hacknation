"""Coverage for the brain-side stance machine (negotiator/brain/stance.py): pure scoring,
switch arithmetic (>=2 switches is structurally impossible), config loading, and --replay."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from negotiator.core.contracts.models import StanceEvent, StanceEventKind
from negotiator.brain.stance import (
    DEFAULT_STANCE_CONFIG,
    StanceConfig,
    StanceMachine,
    load_stance_config,
    main,
    make_event,
    replay,
    stance_config_from_vertical,
)

FIXTURE = Path("negotiator/fixtures/stance_smoke.jsonl")
ALL_KINDS = tuple(StanceEventKind)


def _random_events(rng: random.Random, count: int) -> tuple[StanceEvent, ...]:
    events = []
    for _ in range(count):
        kind = rng.choice(ALL_KINDS)
        events.append(make_event(kind, f"evidence for {kind.value}"))
    return tuple(events)


# --------------------------------------------------------------------------- #
# Property tests: arbitrary event streams (manual randomized trials -- no
# hypothesis dependency in this repo).
# --------------------------------------------------------------------------- #
def test_property_switch_count_never_exceeds_two():
    for seed in range(200):
        rng = random.Random(seed)
        machine = StanceMachine()
        for _ in range(25):
            machine.step(_random_events(rng, rng.randint(0, 3)))
        assert len(machine.switches) <= 2
        path = [switch.to_stance for switch in machine.switches]
        assert path in ([], ["good"], ["bad"], ["good", "bad"])


def test_property_bad_is_absorbing():
    for seed in range(200):
        rng = random.Random(seed)
        machine = StanceMachine()
        went_bad = False
        for _ in range(30):
            machine.step(_random_events(rng, rng.randint(0, 3)))
            went_bad = went_bad or machine.stance == "bad"
            if went_bad:
                assert machine.stance == "bad"


def test_property_good_never_returns_to_neutral():
    for seed in range(200):
        rng = random.Random(seed)
        machine = StanceMachine()
        was_good = False
        for _ in range(30):
            machine.step(_random_events(rng, rng.randint(0, 3)))
            was_good = was_good or machine.stance == "good"
            if was_good:
                assert machine.stance in ("good", "bad")


def test_property_determinism_same_events_same_trajectory():
    for seed in range(50):
        rng = random.Random(seed)
        turns = [_random_events(rng, rng.randint(0, 3)) for _ in range(20)]
        first, second = StanceMachine(), StanceMachine()
        trace1 = [first.step(turn) for turn in turns]
        trace2 = [second.step(turn) for turn in turns]
        assert trace1 == trace2
        assert [s.as_dict() for s in first.switches] == [s.as_dict() for s in second.switches]
        assert first.suspicion == second.suspicion and first.trust == second.trust


def test_dwell_is_respected():
    config = StanceConfig(weights={"willing_itemization": 100.0}, theta_bad=1000, theta_good=10, min_dwell_turns=3)
    machine = StanceMachine(config)
    trust_event = make_event(StanceEventKind.WILLING_ITEMIZATION, "trust")
    machine.step((trust_event,))  # turn 1: T-S=100 >= 10, but dwell 1 < 3
    assert machine.stance == "neutral"
    machine.step(())  # turn 2: dwell 2 < 3
    assert machine.stance == "neutral"
    machine.step(())  # turn 3: dwell 3 >= 3 -> GOOD
    assert machine.stance == "good"


def test_bad_check_takes_priority_over_good_in_the_same_step():
    config = StanceConfig(weights={"willing_itemization": 100.0, "injection_detected": 100.0},
                          theta_bad=50, theta_good=10, min_dwell_turns=1)
    machine = StanceMachine(config)
    machine.step((make_event(StanceEventKind.WILLING_ITEMIZATION, "trust"),
                 make_event(StanceEventKind.INJECTION_DETECTED, "suspicion")))
    assert machine.stance == "bad"  # both thresholds crossed in one step -> BAD wins
    assert len(machine.switches) == 1


# --------------------------------------------------------------------------- #
# Config loading -- additive stance: block, in-code defaults for verticals
# without one.
# --------------------------------------------------------------------------- #
def test_vertical_without_stance_block_uses_defaults():
    synthetic = {"vertical": "bare", "gate": {"fail_closed": True}}  # no stance: block at all
    config = stance_config_from_vertical(synthetic)
    assert config.theta_bad == DEFAULT_STANCE_CONFIG.theta_bad
    assert config.theta_good == DEFAULT_STANCE_CONFIG.theta_good
    assert config.min_dwell_turns == DEFAULT_STANCE_CONFIG.min_dwell_turns
    assert dict(config.weights) == dict(DEFAULT_STANCE_CONFIG.weights)


def test_plumbing_vertical_stance_block_parses():
    config = load_stance_config(Path("negotiator/config/verticals/plumbing.yaml"))
    assert config.theta_bad == 60 and config.min_dwell_turns == 2


def test_moving_vertical_stance_block_is_read():
    config = load_stance_config()  # default path = moving.yaml
    assert config.theta_bad == 60
    assert config.theta_good == 25
    assert config.min_dwell_turns == 2
    assert config.weight("injection_detected") == 40


def test_stance_config_from_vertical_is_pure_and_matches_load():
    import yaml
    vertical = yaml.safe_load(Path("negotiator/config/verticals/moving.yaml").read_text())
    assert stance_config_from_vertical(vertical) == load_stance_config()


def test_vertical_missing_stance_key_entirely_still_works():
    assert stance_config_from_vertical({}) == DEFAULT_STANCE_CONFIG


# --------------------------------------------------------------------------- #
# StanceEvent / make_event
# --------------------------------------------------------------------------- #
def test_make_event_defaults_weight_key_to_kind():
    event = make_event(StanceEventKind.RED_FLAG_FEE, "a deposit red flag")
    assert event.weight_key == "red_flag_fee"


def test_make_event_accepts_a_custom_weight_key():
    event = make_event(StanceEventKind.RED_FLAG_FEE, "severe", weight_key="red_flag_fee_severe")
    assert event.weight_key == "red_flag_fee_severe"
    config = StanceConfig(weights={"red_flag_fee": 35.0})
    assert config.weight(event.weight_key) == 0.0  # unknown weight_key -> 0, never crashes


def test_stance_event_rejects_an_unknown_kind():
    with pytest.raises(ValueError):
        make_event("not_a_real_kind", "x")


# --------------------------------------------------------------------------- #
# --replay / CLI, matching the other brain modules' debug-matrix style.
# --------------------------------------------------------------------------- #
def test_replay_fixture_prints_trajectory_and_switches(capsys):
    machine = replay(FIXTURE)
    assert machine.stance == "bad"
    assert [switch.to_stance for switch in machine.switches] == ["good", "bad"]
    assert machine.switches[0].turn == 3
    assert machine.switches[1].turn == 6
    assert machine.switch_latency == 1  # first suspicion at turn 5, BAD flip at turn 6
    out = capsys.readouterr().out
    assert '"turn": 6' in out and '"stance": "bad"' in out
    assert '"switch": "neutral->good"' in out
    assert '"switch": "good->bad"' in out


def test_replay_is_deterministic():
    assert [s.as_dict() for s in replay(FIXTURE).switches] == [s.as_dict() for s in replay(FIXTURE).switches]


def test_cli_main_smoke(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["stance", "--replay", str(FIXTURE)])
    main()
    out = capsys.readouterr().out
    assert '"switch": "good->bad"' in out
