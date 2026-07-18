import json

import pytest

from negotiator.brain.opponent import (
    classify_curve,
    classify_tactic,
    estimate_floor,
    replay,
    summary_event,
)
from negotiator.core.contracts import TacticType


@pytest.mark.parametrize(
    ("prices", "expected"),
    [
        ([5200, 5150, 5050, 4750], "boulware"),
        ([5200, 5000, 4800, 4600], "linear"),
        ([5200, 4800, 4650, 4600], "conceder"),
    ],
)
def test_curve_table(prices, expected):
    assert classify_curve(prices, [0, 1, 2, 3]) == expected


def test_floor_is_bounded_and_deterministic():
    first = estimate_floor([5200, 4900, 4750, 4700], [0, 1, 2, 3])
    assert first == estimate_floor([5200, 4900, 4750, 4700], [0, 1, 2, 3])
    floor, band = first
    assert 0 < band[0] <= floor == band[1] <= 4700


@pytest.mark.parametrize(
    "prices",
    ([5200, 5150, 5050, 4750], [5200, 5000, 4800, 4600]),
)
def test_floor_never_reports_false_certain_zero_for_canonical_curves(prices):
    floor, band = estimate_floor(prices, [0, 1, 2, 3])
    assert 0 < band[0] <= floor <= prices[-1]


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("This price expires tomorrow.", TacticType.DEADLINE),
        ("We can quote it sight unseen.", TacticType.LOWBALL),
        ("That is our final offer, take it or leave it.", TacticType.STONEWALL),
        ("Book now or another customer gets the slot.", TacticType.PRESSURE),
        ("It should be around five thousand, depends.", TacticType.VAGUE),
        ("The quote is $4,500.", None),
    ],
)
def test_tactic_table(utterance, expected):
    result = classify_tactic(utterance)
    assert (result.type if result else None) == expected


@pytest.mark.parametrize(
    "utterance",
    (
        "We can discuss the inventory tomorrow.",
        "The price does not expire tomorrow.",
        "It depends on nothing.",
        "Another customer told us your quote is fair.",
    ),
)
def test_tactic_classifier_avoids_common_context_false_positives(utterance):
    assert classify_tactic(utterance) is None


def test_dashboard_summary_is_bus_event_json():
    event = summary_event(call_id="c1", prices=[5200, 4900, 4750, 4700], ts=[0, 1, 2, 3])
    assert event.kind == "opponent.summary"
    json.dumps(event.model_dump(mode="json"))


def test_replay_fixture(capsys):
    rows = replay("negotiator/fixtures/opponent_curves.jsonl")
    assert rows[0]["curve"] == "boulware"
    assert rows[1]["curve"] == "conceder"
    assert json.loads(capsys.readouterr().out.splitlines()[2])["tactic"]["type"] == "deadline"
