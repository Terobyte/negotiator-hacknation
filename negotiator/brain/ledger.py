from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from negotiator.core.contracts import LedgerFact, LedgerFactKind, Source, SourceType


class FactNotFound(KeyError):
    """Raised when a caller tries to cite evidence that the ledger does not hold."""


class DuplicateFact(ValueError):
    """Raised when a fact id is reused with different evidence."""


class Ledger:
    """Evidence store with no public path for turning arbitrary speech into facts."""

    def __init__(self, facts: Iterable[LedgerFact] = ()) -> None:
        self._facts: dict[str, LedgerFact] = {}
        for fact in facts:
            self._store(fact)

    def add_config(
        self,
        *,
        fact_id: str,
        kind: LedgerFactKind,
        value: Any,
        config_ref: str,
        call_id: str,
        ts: datetime | None = None,
    ) -> LedgerFact:
        return self._store(
            LedgerFact(
                id=fact_id,
                kind=kind,
                value=value,
                source=Source(type=SourceType.CONFIG, ref=config_ref),
                call_id=call_id,
                ts=ts or datetime.now(timezone.utc),
            )
        )

    def add_api_result(
        self,
        *,
        fact_id: str,
        kind: LedgerFactKind,
        value: Any,
        api_ref: str,
        call_id: str,
        ts: datetime | None = None,
    ) -> LedgerFact:
        return self._store(
            LedgerFact(
                id=fact_id,
                kind=kind,
                value=value,
                source=Source(type=SourceType.API, ref=api_ref),
                call_id=call_id,
                ts=ts or datetime.now(timezone.utc),
            )
        )

    def add_tool_result(
        self,
        *,
        fact_id: str,
        kind: LedgerFactKind,
        value: Any,
        tool_ref: str,
        call_id: str,
        ts: datetime | None = None,
    ) -> LedgerFact:
        if not tool_ref.strip():
            raise ValueError("tool_ref cannot be blank")
        return self.add_api_result(
            fact_id=fact_id,
            kind=kind,
            value=value,
            api_ref=f"tool:{tool_ref}",
            call_id=call_id,
            ts=ts,
        )

    def capture_quote(
        self,
        *,
        fact_id: str,
        value: Any,
        transcript_ref: str,
        transcript_span: str,
        call_id: str,
        ts: datetime | None = None,
    ) -> LedgerFact:
        if not transcript_span.strip():
            raise ValueError("captured quotes require a transcript span")
        return self._store(
            LedgerFact(
                id=fact_id,
                kind=LedgerFactKind.QUOTE,
                value=value,
                source=Source(type=SourceType.TRANSCRIPT, ref=transcript_ref, span=transcript_span),
                call_id=call_id,
                ts=ts or datetime.now(timezone.utc),
            )
        )

    def cite(self, fact_id: str) -> LedgerFact:
        try:
            return self._facts[fact_id]
        except KeyError as exc:
            raise FactNotFound(f"ledger fact does not exist: {fact_id}") from exc

    def list(self, *, provenance: bool = False) -> list[dict[str, Any]]:
        facts = sorted(self._facts.values(), key=lambda fact: (fact.ts, fact.id))
        if provenance:
            return [fact.model_dump(mode="json") for fact in facts]
        return [{"id": fact.id, "kind": fact.kind.value, "value": fact.value} for fact in facts]

    def allowed(self, fact_ids: Iterable[str]) -> tuple[LedgerFact, ...]:
        return tuple(self.cite(fact_id) for fact_id in fact_ids)

    def ingest_counterparty_utterance(self, _text: str) -> None:
        """Deliberately non-authoritative: raw speech can never create a fact."""

        return None

    def _store(self, fact: LedgerFact) -> LedgerFact:
        existing = self._facts.get(fact.id)
        if existing is not None and existing != fact:
            raise DuplicateFact(f"ledger fact id already exists: {fact.id}")
        self._facts[fact.id] = fact
        return fact


def _load(path: Path) -> Ledger:
    if not path.exists():
        return Ledger()
    facts: list[LedgerFact] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                facts.append(LedgerFact.model_validate_json(line))
    return Ledger(facts)


def _save(path: Path, ledger: Ledger) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for fact in ledger.list(provenance=True):
            stream.write(json.dumps(fact, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect an evidence-authority ledger")
    parser.add_argument("--store", type=Path, default=Path("ledger.jsonl"))
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add")
    add.add_argument("--authority", choices=("config", "api", "tool", "quote"), required=True)
    add.add_argument("--id", required=True)
    add.add_argument("--kind", choices=tuple(kind.value for kind in LedgerFactKind), required=True)
    add.add_argument("--value", required=True, help="JSON value")
    add.add_argument("--ref", required=True)
    add.add_argument("--span")
    add.add_argument("--call-id", required=True)
    cite = sub.add_parser("cite")
    cite.add_argument("fact_id")
    listing = sub.add_parser("list")
    listing.add_argument("--provenance", action="store_true")
    args = parser.parse_args()
    ledger = _load(args.store)
    try:
        if args.command == "add":
            value = json.loads(args.value)
            kind = LedgerFactKind(args.kind)
            common = dict(fact_id=args.id, value=value, call_id=args.call_id)
            if args.authority == "config":
                fact = ledger.add_config(kind=kind, config_ref=args.ref, **common)
            elif args.authority == "api":
                fact = ledger.add_api_result(kind=kind, api_ref=args.ref, **common)
            elif args.authority == "tool":
                fact = ledger.add_tool_result(kind=kind, tool_ref=args.ref, **common)
            else:
                if kind is not LedgerFactKind.QUOTE:
                    parser.error("quote authority requires --kind quote")
                fact = ledger.capture_quote(
                    transcript_ref=args.ref, transcript_span=args.span or "", **common
                )
            _save(args.store, ledger)
            print(fact.model_dump_json())
        elif args.command == "cite":
            print(ledger.cite(args.fact_id).model_dump_json())
        else:
            print(json.dumps(ledger.list(provenance=args.provenance), ensure_ascii=False))
    except (FactNotFound, DuplicateFact, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
