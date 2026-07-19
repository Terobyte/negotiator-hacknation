from __future__ import annotations

import argparse
import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Turn(StrEnum):
    AGENT = "agent"
    COUNTERPARTY = "counterparty"
    SILENCE = "silence"


class VadKind(StrEnum):
    COUNTERPARTY_STARTED = "counterparty_started"
    COUNTERPARTY_STOPPED = "counterparty_stopped"
    AGENT_STARTED = "agent_started"
    AGENT_STOPPED = "agent_stopped"
    TACTICAL_PAUSE = "tactical_pause"
    TICK = "tick"


class VadEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: VadKind
    at: float = Field(ge=0)
    duration: float = Field(default=0, ge=0)


class Arbiter:
    """Deterministic turn ownership and barge-in state, driven by VAD timestamps."""

    def __init__(self) -> None:
        self.turn = Turn.SILENCE
        self.pause_until = 0.0
        self.interruptions = 0
        self.agent_active = False
        self.counterparty_active = False

    def apply(self, event: VadEvent) -> Turn:
        if event.kind is VadKind.COUNTERPARTY_STARTED:
            self.counterparty_active = True
            if self.turn is Turn.AGENT:
                self.interruptions += 1
            self.turn = Turn.COUNTERPARTY
        elif event.kind is VadKind.COUNTERPARTY_STOPPED:
            self.counterparty_active = False
            self.turn = Turn.AGENT if self.agent_active else Turn.SILENCE
        elif event.kind is VadKind.AGENT_STARTED:
            if event.at < self.pause_until or self.turn is Turn.COUNTERPARTY:
                return self.turn
            self.agent_active = True
            self.turn = Turn.AGENT
        elif event.kind is VadKind.AGENT_STOPPED:
            self.agent_active = False
            self.turn = Turn.COUNTERPARTY if self.counterparty_active else Turn.SILENCE
        elif event.kind is VadKind.TACTICAL_PAUSE:
            self.pause_until = max(self.pause_until, event.at + event.duration)
            self.agent_active = False
            self.turn = Turn.SILENCE
        return self.turn


def replay(path: str | Path) -> Arbiter:
    arbiter = Arbiter()
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            raw = json.loads(line)
            event = VadEvent.model_validate(raw.get("payload", raw))
            turn = arbiter.apply(event)
            print(json.dumps({"at": event.at, "turn": turn.value, "interruptions": arbiter.interruptions}))
    return arbiter


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay VAD events through the turn arbiter")
    parser.add_argument("--replay", required=True)
    args = parser.parse_args()
    replay(args.replay)


if __name__ == "__main__":
    main()
