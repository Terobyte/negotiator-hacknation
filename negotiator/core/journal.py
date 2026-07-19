from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator, TextIO

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

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
        self._seq_path = self.path.with_suffix(self.path.suffix + ".seq")
        self._lock = RLock()
        with self._lock_path.open("a+") as lock_stream, _file_lock(lock_stream):
            self._seq = max(self._read_last_seq(), self._read_seq_file())
            self._write_seq_file(self._seq)
        self._fsync = fsync

    def attach(self, bus: EventBus):
        """Globally subscribe; returned callback detaches the journal."""
        return bus.subscribe_all(self.append)

    def append(self, event: BusEvent) -> JournalEvent:
        with self._lock:
            with self._lock_path.open("a+") as lock_stream:
                with _file_lock(lock_stream):
                    self._seq = max(self._seq, self._read_seq_file()) + 1
                    # Reserve the sequence before appending. A crash may leave a harmless
                    # gap, but can never let another writer reuse an already-appended seq.
                    self._write_seq_file(self._seq)
                    recorded = JournalEvent(seq=self._seq, **event.model_dump())
                    line = recorded.model_dump_json() + "\n"
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write(line)
                        stream.flush()
                        if self._fsync:
                            os.fsync(stream.fileno())
                    return recorded

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

    def _read_seq_file(self) -> int:
        try:
            raw = self._seq_path.read_text(encoding="ascii").strip()
            return int(raw) if raw else 0
        except (FileNotFoundError, ValueError):
            return 0

    def _write_seq_file(self, seq: int) -> None:
        self._seq_path.write_text(str(seq), encoding="ascii")


@contextmanager
def _file_lock(stream: TextIO) -> Iterator[None]:
    if fcntl is not None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return
    stream.seek(0)
    if not stream.read(1):
        stream.write("0"); stream.flush()
    stream.seek(0)
    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
    try:
        yield
    finally:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
