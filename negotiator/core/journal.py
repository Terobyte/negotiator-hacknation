from __future__ import annotations

import json
import os
import fcntl
from pathlib import Path
from threading import RLock

from negotiator.core.bus import EventBus
from negotiator.core.contracts import BusEvent, JournalEvent


class Journal:
    """Append-only JSONL journal with a monotonic sequence across writers."""

    def __init__(self, path: str | Path, *, fsync: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock_path.touch(exist_ok=True)
        self._lock = RLock()
        self._seq = self._read_last_seq()
        self._fsync = fsync

    def attach(self, bus: EventBus):
        """Globally subscribe; returned callback detaches the journal."""
        return bus.subscribe_all(self.append)

    def append(self, event: BusEvent) -> JournalEvent:
        with self._lock:
            with self._lock_path.open("a+") as lock_stream:
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
                try:
                    self._seq = max(self._seq, self._read_last_seq()) + 1
                    recorded = JournalEvent(seq=self._seq, **event.model_dump())
                    line = recorded.model_dump_json() + "\n"
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write(line)
                        stream.flush()
                        if self._fsync:
                            os.fsync(stream.fileno())
                    return recorded
                finally:
                    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)

    def replay(self) -> list[JournalEvent]:
        with self.path.open(encoding="utf-8") as stream:
            return [JournalEvent.model_validate_json(line) for line in stream if line.strip()]

    def _read_last_seq(self) -> int:
        last = 0
        with self.path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    seq = json.loads(line)["seq"]
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    raise ValueError(f"invalid journal line {line_number}") from exc
                if not isinstance(seq, int) or seq <= last:
                    raise ValueError(f"non-monotonic seq on journal line {line_number}")
                last = seq
        return last
