from __future__ import annotations

import argparse
import json
from pathlib import Path

from negotiator.core.contracts import JournalEvent, NegotiationPhase


class ForbiddenTransition(RuntimeError):
    def __init__(self, source: NegotiationPhase, target: NegotiationPhase, reason: str) -> None:
        super().__init__(f"forbidden negotiation transition {source.value} -> {target.value}: {reason}")
        self.source = source
        self.target = target
        self.reason = reason


_NEXT = {
    NegotiationPhase.OPENING: NegotiationPhase.DISCOVERY,
    NegotiationPhase.DISCOVERY: NegotiationPhase.PRESSURE_TEST,
    NegotiationPhase.PRESSURE_TEST: NegotiationPhase.LEVERAGE,
    NegotiationPhase.LEVERAGE: NegotiationPhase.COMMIT,
    NegotiationPhase.COMMIT: NegotiationPhase.WRAP,
}


class NegotiationFSM:
    """The one state machine in the system: negotiation phases only."""

    def __init__(self, phase: NegotiationPhase = NegotiationPhase.OPENING) -> None:
        self.phase = phase

    def transition(self, target: NegotiationPhase | str, *, full_estimate: bool = False) -> NegotiationPhase:
        target = NegotiationPhase(target)
        if target == self.phase:
            return self.phase
        expected = _NEXT.get(self.phase)
        if target != expected:
            raise ForbiddenTransition(self.phase, target, "phases cannot be skipped or reversed")
        if target == NegotiationPhase.LEVERAGE and not full_estimate:
            raise ForbiddenTransition(self.phase, target, "a complete estimate is required before leverage")
        self.phase = target
        return self.phase

    def finish(self) -> None:
        """Close the lifecycle, including provider hangups before the normal WRAP phase."""

        self.phase = NegotiationPhase.WRAP


def replay(path: str | Path) -> NegotiationPhase:
    machine = NegotiationFSM()
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            raw = json.loads(line)
            event = JournalEvent.model_validate(raw) if "seq" in raw else raw
            payload = event.payload if isinstance(event, JournalEvent) else event.get("payload", event)
            target = NegotiationPhase(payload["target"])
            machine.transition(target, full_estimate=bool(payload.get("full_estimate", False)))
            print(json.dumps({"phase": machine.phase.value}))
    return machine.phase


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay negotiation phase events")
    parser.add_argument("--replay", required=True)
    args = parser.parse_args()
    replay(args.replay)


if __name__ == "__main__":
    main()
