from __future__ import annotations

import argparse
import json
from pathlib import Path


def slice_journal(
    source: str | Path,
    *,
    call_id: str | None = None,
    module: str | None = None,
    kind: str | None = None,
) -> list[dict]:
    selected: list[dict] = []
    with Path(source).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}") from exc
            if call_id is not None and row.get("call_id") != call_id:
                continue
            if module is not None and row.get("module") != module:
                continue
            if kind is not None and row.get("kind") != kind:
                continue
            selected.append(row)
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract a replayable JSONL journal slice")
    parser.add_argument("source", type=Path)
    parser.add_argument("--call-id")
    parser.add_argument("--module")
    parser.add_argument("--kind")
    args = parser.parse_args()
    for row in slice_journal(args.source, call_id=args.call_id, module=args.module, kind=args.kind):
        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
