from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from negotiator.core.contracts.models import StanceEvent, StanceEventKind

DEFAULT_VERTICAL_PATH = Path(__file__).parents[1] / "config" / "verticals" / "moving.yaml"

# Suspicion-bearing vs trust-bearing kinds -- the ONLY place this classification lives. The
# machine itself never inspects a kind's meaning beyond this bucket lookup: it does not detect.
_SUSPICION_KINDS = frozenset({
    StanceEventKind.INJECTION_DETECTED, StanceEventKind.RED_FLAG_FEE,
    StanceEventKind.FEE_DENIAL_CAUGHT, StanceEventKind.PRICE_JUMP,
    StanceEventKind.PRESSURE_DEADLINE,
})
_TRUST_KINDS = frozenset({
    StanceEventKind.WILLING_ITEMIZATION, StanceEventKind.PRICE_NEAR_BENCHMARK,
    StanceEventKind.CONCESSION_MADE,
})

_DEFAULT_WEIGHTS: dict[str, float] = {
    "injection_detected": 40.0,
    "red_flag_fee": 35.0,
    "fee_denial_caught": 30.0,
    "price_jump": 20.0,
    "pressure_deadline": 15.0,
    "willing_itemization": 15.0,
    "price_near_benchmark": 10.0,
    "concession_made": 10.0,
}
_DEFAULT_THETA_BAD = 60.0
_DEFAULT_THETA_GOOD = 25.0
_DEFAULT_MIN_DWELL_TURNS = 2

STANCES = ("neutral", "good", "bad")


@dataclass(frozen=True, slots=True)
class StanceConfig:
    weights: Mapping[str, float]
    theta_bad: float = _DEFAULT_THETA_BAD
    theta_good: float = _DEFAULT_THETA_GOOD
    min_dwell_turns: int = _DEFAULT_MIN_DWELL_TURNS

    def weight(self, weight_key: str) -> float:
        return float(self.weights.get(weight_key, 0.0))


DEFAULT_STANCE_CONFIG = StanceConfig(weights=dict(_DEFAULT_WEIGHTS))


def stance_config_from_vertical(vertical: Mapping[str, Any]) -> StanceConfig:
    """Pure: build a StanceConfig from an already-loaded vertical dict (the same dict every
    other arena/report reader gets from ``yaml.safe_load``). A vertical without a ``stance:``
    block yields exactly ``DEFAULT_STANCE_CONFIG`` -- this is what makes plumbing.yaml work."""

    raw = vertical.get("stance") or {}
    weights = {**_DEFAULT_WEIGHTS, **{str(key): float(value) for key, value in (raw.get("weights") or {}).items()}}
    return StanceConfig(
        weights=weights,
        theta_bad=float(raw.get("theta_bad", _DEFAULT_THETA_BAD)),
        theta_good=float(raw.get("theta_good", _DEFAULT_THETA_GOOD)),
        min_dwell_turns=int(raw.get("min_dwell_turns", _DEFAULT_MIN_DWELL_TURNS)),
    )


def load_stance_config(path: str | Path = DEFAULT_VERTICAL_PATH) -> StanceConfig:
    vertical = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return stance_config_from_vertical(vertical)


def make_event(kind: StanceEventKind | str, detail: str, *, weight_key: str | None = None) -> StanceEvent:
    """Convenience constructor: ``weight_key`` defaults to ``kind`` (the common case), but a
    caller may point two events of the same kind at different weight buckets."""

    resolved_kind = StanceEventKind(kind)
    return StanceEvent(kind=resolved_kind, weight_key=weight_key or resolved_kind.value, detail=detail)


@dataclass(frozen=True, slots=True)
class StanceSwitch:
    turn: int
    from_stance: str
    to_stance: str
    suspicion: float
    trust: float
    evidence: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn, "from_stance": self.from_stance, "to_stance": self.to_stance,
            "suspicion": self.suspicion, "trust": self.trust, "evidence": list(self.evidence),
        }


class StanceMachine:
    """Pure decision module: two monotone non-decreasing scores, suspicion S and trust T.

    Rule (exact arithmetic -- do not implement a fancier one, it makes the invariants free):
      - S >= theta_bad -> BAD. S is monotone, so BAD is absorbing by construction.
      - else if stance is NEUTRAL and (T - S) >= theta_good and >= min_dwell turns elapsed
        -> GOOD.
      - GOOD never returns to NEUTRAL (trust is revocable only via GOOD -> BAD).
    Corollary: at most 2 switches per call (NEUTRAL -> GOOD -> BAD worst case); oscillation is
    arithmetically impossible, not merely avoided by convention.

    The machine NEVER detects: callers classify evidence into ``StanceEvent``s and call
    ``step``; this class only scores and switches. Every switch is recorded with the score
    that triggered it and the cumulative cited evidence, so a caller (e.g. arena) can journal
    it honestly.
    """

    def __init__(self, config: StanceConfig | None = None) -> None:
        self.config = config or DEFAULT_STANCE_CONFIG
        self.stance = "neutral"
        self.suspicion = 0.0
        self.trust = 0.0
        self.turn = 0
        self.switches: list[StanceSwitch] = []
        self._first_suspicion_turn: int | None = None
        self._evidence_log: list[str] = []

    @property
    def first_suspicion_turn(self) -> int | None:
        """The turn of the first suspicion-bearing event with positive weight, or None."""

        return self._first_suspicion_turn

    @property
    def switch_latency(self) -> int | None:
        """Turns from the first suspicion-bearing event to the BAD flip, or None if either
        never happened."""

        bad = next((switch for switch in self.switches if switch.to_stance == "bad"), None)
        if bad is None or self._first_suspicion_turn is None:
            return None
        return bad.turn - self._first_suspicion_turn

    def step(self, events: Sequence[StanceEvent] = ()) -> str:
        self.turn += 1
        for event in events:
            weight = self.config.weight(event.weight_key)
            if event.kind in _SUSPICION_KINDS:
                self.suspicion += weight
                if self._first_suspicion_turn is None and weight > 0:
                    self._first_suspicion_turn = self.turn
                self._evidence_log.append(event.detail)
            elif event.kind in _TRUST_KINDS:
                self.trust += weight
                self._evidence_log.append(event.detail)
        self._maybe_switch()
        return self.stance

    def _maybe_switch(self) -> None:
        if self.stance != "bad" and self.suspicion >= self.config.theta_bad:
            self._switch("bad")
            return
        if self.stance == "neutral":
            dwelled = (self.turn - 0) >= self.config.min_dwell_turns
            if dwelled and (self.trust - self.suspicion) >= self.config.theta_good:
                self._switch("good")

    def _switch(self, to_stance: str) -> None:
        switch = StanceSwitch(turn=self.turn, from_stance=self.stance, to_stance=to_stance,
                              suspicion=self.suspicion, trust=self.trust,
                              evidence=tuple(self._evidence_log))
        self.switches.append(switch)
        self.stance = to_stance


def _event_from_dict(raw: Mapping[str, Any]) -> StanceEvent:
    kind = StanceEventKind(raw["kind"])
    return make_event(kind, raw.get("detail", kind.value), weight_key=raw.get("weight_key"))


def replay(path: str | Path, *, config: StanceConfig | None = None) -> StanceMachine:
    machine = StanceMachine(config or load_stance_config())
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            raw = json.loads(line)
            events = tuple(_event_from_dict(item) for item in raw.get("events", ()))
            stance = machine.step(events)
            print(json.dumps({"turn": machine.turn, "stance": stance,
                              "suspicion": machine.suspicion, "trust": machine.trust}))
    for switch in machine.switches:
        print(json.dumps({"switch": f"{switch.from_stance}->{switch.to_stance}", **switch.as_dict()}))
    return machine


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a stance machine's event stream")
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--vertical", type=Path, default=DEFAULT_VERTICAL_PATH)
    args = parser.parse_args()
    replay(args.replay, config=load_stance_config(args.vertical))


if __name__ == "__main__":
    main()
