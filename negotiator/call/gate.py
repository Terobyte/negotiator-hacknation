from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from negotiator.core.contracts import (
    ApprovedUtterance,
    CallCard,
    LedgerFact,
    LedgerFactKind,
)
from negotiator.core.contracts.models import _GATE_CAPABILITY


_MONEY_RE = re.compile(
    r"(?i)(?:\$\s*([0-9][0-9,]*(?:\.\d{1,2})?)|([0-9][0-9,]*(?:\.\d{1,2})?)\s*(?:usd|dollars?|bucks?))"
)
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_WORD_MONEY_RE = re.compile(
    r"(?i)\b(" + "|".join(_NUMBER_WORDS) + r")\s+thousand(?:\s+dollars?)?\b"
)
_BARE_NUMBER_RE = re.compile(r"(?<![\w.])([0-9][0-9,]*(?:\.\d{1,2})?)(?![\w,])")
_QUOTE_LANGUAGE_RE = re.compile(
    r"(?i)\b(?:quote|quoted|estimate|estimated|offer|bid|price|rate|котиров\w*|смет\w*|цен\w*)\b"
)
_PRIVATE_LABEL_RE = re.compile(
    r"(?i)\b(?:budget[_ -]?ceiling|maximum\s+budget|our\s+maximum|walk[- ]away|"
    r"client(?:'s)?\s+maximum|client\s+can\s+pay|"
    r"opponent(?:'s)?\s+floor|your\s+floor|price\s+corridor|pricing\s+corridor|"
    r"system\s+prompt|hidden\s+instructions?|developer\s+message)\b"
)


@dataclass(frozen=True, slots=True)
class PrivateTerms:
    budget_ceiling: Decimal | None = None
    opponent_floor: Decimal | None = None
    price_corridor: tuple[Decimal, Decimal] | None = None
    system_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class GateDecision:
    verdict: str
    reason: str
    verdict_ref: str
    approved: ApprovedUtterance | None
    stall: ApprovedUtterance | None
    regenerate: bool


class HonestyGate:
    """Deterministic fail-closed output gate for negotiated claims and secrets."""

    def __init__(self, *, stall_phrases: Iterable[str]) -> None:
        phrases = tuple(phrase.strip() for phrase in stall_phrases if phrase.strip())
        if not phrases:
            raise ValueError("at least one stall phrase is required")
        self._stall_phrases = phrases

    def evaluate(
        self,
        *,
        draft: str,
        card: CallCard,
        ledger_facts: Mapping[str, LedgerFact] | Iterable[LedgerFact],
        private_terms: PrivateTerms | None = None,
    ) -> GateDecision:
        text = draft.strip()
        verdict_ref = self._verdict_ref(text, card.version)
        if not text:
            return self._blocked("empty_draft", verdict_ref, card.version)

        facts = self._resolve_allowed(card, ledger_facts)
        leak_reason = self._private_leak_reason(text, private_terms or PrivateTerms())
        if leak_reason:
            return self._blocked(leak_reason, verdict_ref, card.version)

        claim_reason = self._unsupported_claim_reason(text, facts)
        if claim_reason:
            return self._blocked(claim_reason, verdict_ref, card.version)

        approved = self._issue(text=text, card_version=card.version, verdict_ref=verdict_ref)
        return GateDecision("allow", "supported", verdict_ref, approved, None, False)

    def _resolve_allowed(
        self,
        card: CallCard,
        facts: Mapping[str, LedgerFact] | Iterable[LedgerFact],
    ) -> tuple[LedgerFact, ...]:
        values = facts.values() if isinstance(facts, Mapping) else facts
        by_id = {fact.id: fact for fact in values}
        missing = [fact_id for fact_id in card.allowed_fact_ids if fact_id not in by_id]
        if missing:
            return ()
        return tuple(by_id[fact_id] for fact_id in card.allowed_fact_ids)

    def _unsupported_claim_reason(self, text: str, facts: tuple[LedgerFact, ...]) -> str | None:
        quote_language = bool(_QUOTE_LANGUAGE_RE.search(text))
        amounts = _money_amounts(text)
        if quote_language:
            amounts.update(_bare_amounts(text))
        supported_amounts = {
            amount for fact in facts for amount in _numbers_in_value(fact.value)
        }
        unsupported = [amount for amount in amounts if amount not in supported_amounts]
        if unsupported:
            return "unsupported_quote_amount"
        if quote_language:
            quote_facts = [fact for fact in facts if fact.kind is LedgerFactKind.QUOTE]
            if not quote_facts:
                return "unsupported_quote_claim"
        return None

    def _private_leak_reason(self, text: str, terms: PrivateTerms) -> str | None:
        if _PRIVATE_LABEL_RE.search(text):
            return "private_term_label"
        lowered = " ".join(text.casefold().split())
        if terms.system_prompt:
            secret = " ".join(terms.system_prompt.casefold().split())
            if len(secret) >= 8 and secret in lowered:
                return "system_prompt_leak"
        private_amounts = {
            value
            for value in (
                terms.budget_ceiling,
                terms.opponent_floor,
                *(terms.price_corridor or ()),
            )
            if value is not None
        }
        if private_amounts.intersection(_money_amounts(text) | _bare_amounts(text)):
            return "private_price_leak"
        return None

    def _blocked(self, reason: str, verdict_ref: str, card_version: int) -> GateDecision:
        index = int(verdict_ref[-8:], 16) % len(self._stall_phrases)
        stall = self._issue(
            text=self._stall_phrases[index],
            card_version=card_version,
            verdict_ref=f"{verdict_ref}:stall",
        )
        return GateDecision("block", reason, verdict_ref, None, stall, True)

    @staticmethod
    def _issue(*, text: str, card_version: int, verdict_ref: str) -> ApprovedUtterance:
        return ApprovedUtterance(
            text=text,
            card_version=card_version,
            gate_verdict_ref=verdict_ref,
            _gate_capability=_GATE_CAPABILITY,
        )

    @staticmethod
    def _verdict_ref(text: str, card_version: int) -> str:
        digest = hashlib.sha256(f"{card_version}\0{text}".encode()).hexdigest()[:16]
        return f"gate:{digest}"


def _money_amounts(text: str) -> set[Decimal]:
    amounts: set[Decimal] = set()
    for match in _MONEY_RE.finditer(text):
        raw = next(group for group in match.groups() if group is not None)
        try:
            amounts.add(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            continue
    for match in _WORD_MONEY_RE.finditer(text):
        amounts.add(Decimal(_NUMBER_WORDS[match.group(1).casefold()] * 1000))
    return amounts


def _bare_amounts(text: str) -> set[Decimal]:
    amounts: set[Decimal] = set()
    for match in _BARE_NUMBER_RE.finditer(text):
        try:
            amounts.add(Decimal(match.group(1).replace(",", "")))
        except InvalidOperation:
            continue
    return amounts


def _numbers_in_value(value: Any) -> set[Decimal]:
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, (int, float, Decimal)):
        try:
            return {Decimal(str(value))}
        except InvalidOperation:
            return set()
    if isinstance(value, str):
        return _money_amounts(value)
    if isinstance(value, Mapping):
        return {number for child in value.values() for number in _numbers_in_value(child)}
    if isinstance(value, (list, tuple, set)):
        return {number for child in value for number in _numbers_in_value(child)}
    if hasattr(value, "model_dump"):
        return _numbers_in_value(value.model_dump(mode="json"))
    return set()


def replay(path: str | Path, *, stall_phrases: Iterable[str] = ("One moment while I check my notes.",)) -> list[GateDecision]:
    gate = HonestyGate(stall_phrases=stall_phrases)
    decisions: list[GateDecision] = []
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            facts = [LedgerFact.model_validate(item) for item in raw.get("facts", ())]
            card = CallCard.model_validate(raw["card"])
            private = raw.get("private_terms", {})
            terms = PrivateTerms(
                budget_ceiling=_decimal_or_none(private.get("budget_ceiling")),
                opponent_floor=_decimal_or_none(private.get("opponent_floor")),
                price_corridor=tuple(Decimal(str(v)) for v in private["price_corridor"])
                if private.get("price_corridor") else None,
                system_prompt=private.get("system_prompt"),
            )
            decision = gate.evaluate(
                draft=raw["draft"], card=card, ledger_facts=facts, private_terms=terms
            )
            decisions.append(decision)
            print(json.dumps({"line": line_number, "verdict": decision.verdict, "reason": decision.reason}))
    return decisions


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay drafts through the deterministic honesty gate")
    parser.add_argument("--replay", required=True)
    args = parser.parse_args()
    replay(args.replay)


if __name__ == "__main__":
    main()
