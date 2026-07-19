from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from negotiator.core.bus import EventBus
from negotiator.core.contracts import (
    BusEvent,
    CallCard,
    JobSpec,
    JournalEvent,
    LedgerFact,
    LedgerFactKind,
    NegotiationPhase,
)


FEE_NAMES = {
    1: "transportation",
    2: "labor",
    3: "packing",
    4: "unpacking",
    5: "materials",
    6: "stairs",
    7: "elevator",
    8: "long carry",
    9: "shuttle",
    10: "bulky items",
    11: "storage",
    12: "valuation coverage",
    13: "fuel",
    14: "tolls and permits",
}

_PRIVATE_LANGUAGE = re.compile(
    r"(?i)\b(?:budget[_ -]?ceiling|maximum budget|client(?:'s)? maximum|opponent(?:'s)? floor|"
    r"price corridor|system prompt|hidden instructions?|developer message)\b"
)
_NUMBER = re.compile(r"(?<!\w)\$?\d[\d,]*(?:\.\d+)?")


class StrategistAdapter(Protocol):
    def refine(self, *, card: CallCard, public_context: Mapping[str, Any]) -> CallCard: ...


class OfflineStrategistAdapter:
    def refine(self, *, card: CallCard, public_context: Mapping[str, Any]) -> CallCard:
        del public_context
        return card


class OpenAIStrategistAdapter:
    """Lazy optional adapter. Network and the OpenAI package are touched only when used."""

    def __init__(self, *, model: str = "gpt-5.6-sol", client: object | None = None) -> None:
        self.model = model
        self.reasoning_effort = "medium"
        self._client = client

    def refine(self, *, card: CallCard, public_context: Mapping[str, Any]) -> CallCard:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("install negotiator[openai] to use the hosted strategist") from exc
            self._client = OpenAI()
        response = self._client.responses.create(
            model=self.model,
            reasoning={"effort": self.reasoning_effort},
            input=(
                "Refine wording only in this negotiation CallCard. Preserve version, phase, allowed "
                "fact IDs, and never add prices or private information. Return only CallCard JSON.\n"
                f"CARD={card.model_dump_json()}\nPUBLIC_CONTEXT={json.dumps(public_context)}"
            ),
            max_output_tokens=300,
        )
        refined = CallCard.model_validate_json(response.output_text)
        immutable = ("version", "phase", "allowed_fact_ids", "client_directives")
        if any(getattr(refined, name) != getattr(card, name) for name in immutable):
            raise ValueError("hosted strategist changed an authority field")
        original_numbers = set(_NUMBER.findall(card.model_dump_json()))
        refined_text = " ".join((refined.phase_goal, refined.next_move, *refined.client_directives))
        if set(_NUMBER.findall(refined_text)) - original_numbers:
            raise ValueError("hosted strategist introduced an unsupported numeric claim")
        if _PRIVATE_LANGUAGE.search(refined_text):
            raise ValueError("hosted strategist introduced private language")
        return refined


def accept_price(*, price: Decimal | float | int | str, job_spec: JobSpec) -> Decimal:
    """The sole acceptance boundary; a confirmed JobSpec and its private ceiling are mandatory."""

    amount = Decimal(str(price))
    if not job_spec.confirmed:
        raise ValueError("cannot accept a price without a confirmed JobSpec")
    if amount <= 0:
        raise ValueError("accepted price must be positive")
    if amount > job_spec.budget_ceiling:
        raise ValueError("price exceeds the confirmed budget ceiling")
    return amount


def _facts(snapshot: Sequence[LedgerFact | Mapping[str, Any]]) -> tuple[LedgerFact, ...]:
    return tuple(item if isinstance(item, LedgerFact) else LedgerFact.model_validate(item) for item in snapshot)


def _quote_codes(facts: Sequence[LedgerFact], *, call_id: str) -> set[int]:
    codes: set[int] = set()
    for fact in facts:
        if (
            fact.kind is not LedgerFactKind.QUOTE
            or fact.call_id != call_id
            or not isinstance(fact.value, Mapping)
        ):
            continue
        for item in fact.value.get("line_items", ()):
            if isinstance(item, Mapping) and item.get("disclosed") is True and "amount" in item:
                try:
                    code = int(item["code"])
                    amount = Decimal(str(item["amount"]))
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    continue
                if code in FEE_NAMES and amount.is_finite() and amount >= 0:
                    codes.add(code)
    return codes


def _benchmark_and_competing_quote(facts: Sequence[LedgerFact]) -> tuple[float | None, float | None]:
    benchmark: float | None = None
    competing: float | None = None
    for fact in facts:
        if fact.kind is LedgerFactKind.BENCHMARK and isinstance(fact.value, Mapping):
            low = fact.value.get("low")
            if low is not None:
                benchmark = _finite_float(low)
        elif fact.kind is LedgerFactKind.QUOTE and isinstance(fact.value, Mapping):
            total = fact.value.get("total")
            if total is not None:
                parsed = _finite_float(total)
                if parsed is not None:
                    competing = parsed if competing is None else min(competing, parsed)
    return benchmark, competing


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


class Strategist:
    """Slow-loop planner. Its boundary accepts snapshots, never transcript text or call modules."""

    def __init__(self, *, adapter: StrategistAdapter | None = None, bus: EventBus | None = None) -> None:
        self._adapter = adapter or OfflineStrategistAdapter()
        self._bus = bus
        self._last_version = 0

    def revise(
        self,
        *,
        ledger_snapshot: Sequence[LedgerFact | Mapping[str, Any]],
        opponent_summary: Mapping[str, Any],
        previous_card: CallCard,
        call_id: str = "offline",
    ) -> CallCard:
        facts = _facts(ledger_snapshot)
        version = max(self._last_version, previous_card.version) + 1
        disclosed = _quote_codes(facts, call_id=call_id)
        missing = [code for code in FEE_NAMES if code not in disclosed]
        allowed = tuple(fact.id for fact in facts if fact.kind in (LedgerFactKind.QUOTE, LedgerFactKind.BENCHMARK))
        if missing:
            names = ", ".join(FEE_NAMES[code] for code in missing)
            card = CallCard(
                version=version,
                phase=_safe_phase(previous_card.phase, NegotiationPhase.DISCOVERY),
                phase_goal="Complete all 14 estimate fee categories",
                next_move=f"What would each of these add to the final total: {names}?",
                allowed_fact_ids=allowed,
                tone_preset="curious",
                client_directives=("Get a disclosed amount or an explicit zero for every fee code.",),
            )
        else:
            benchmark, competing = _benchmark_and_competing_quote(facts)
            curve = str(opponent_summary.get("curve", "linear"))
            prices = opponent_summary.get("prices")
            current = _finite_float(prices[-1]) if isinstance(prices, Sequence) and prices else None
            floor = opponent_summary.get("floor")
            parsed_floor = _finite_float(floor)
            at_floor = current is not None and parsed_floor is not None and current <= parsed_floor * 1.03
            if at_floor or curve == "conceder":
                card = CallCard(
                    version=version,
                    phase=_safe_phase(previous_card.phase, NegotiationPhase.COMMIT),
                    phase_goal="Lock the complete terms without revealing the private ceiling",
                    next_move="What would it take to make this a binding, not-to-exceed final number today?",
                    allowed_fact_ids=allowed,
                    tone_preset="calm_firm",
                )
            elif benchmark is not None and competing is not None:
                target = min(benchmark, competing) * 0.97
                precise = int(target // 10 * 10 + 7)
                card = CallCard(
                    version=version,
                    phase=_safe_phase(previous_card.phase, NegotiationPhase.LEVERAGE),
                    phase_goal="Anchor first while the evidence advantage is private",
                    next_move=(
                        "You're probably going to think I'm grinding you on price. "
                        f"How can we get the complete binding total to ${precise:,}?"
                    ),
                    allowed_fact_ids=allowed,
                    tone_preset="calm_firm",
                    client_directives=("Anchor before disclosing benchmark details.",),
                )
            else:
                card = CallCard(
                    version=version,
                    phase=_safe_phase(previous_card.phase, NegotiationPhase.PRESSURE_TEST),
                    phase_goal="Make the counterparty establish the first supported number",
                    next_move="How was this complete number calculated?",
                    allowed_fact_ids=allowed,
                    tone_preset="curious_firm",
                )
        public_context = {"curve": opponent_summary.get("curve"), "missing_fee_codes": missing}
        card = self._adapter.refine(card=card, public_context=public_context)
        if card.version != version:
            raise ValueError("strategist adapter violated monotonic card versioning")
        self._last_version = version
        if self._bus is not None:
            self._bus.publish(
                BusEvent(
                    call_id=call_id,
                    module="strategist",
                    kind="strategist.call_card",
                    payload=card.model_dump(mode="json"),
                    refs=card.allowed_fact_ids,
                )
            )
        return card


def _safe_phase(current: NegotiationPhase, desired: NegotiationPhase) -> NegotiationPhase:
    phases = tuple(NegotiationPhase)
    current_index = phases.index(current)
    desired_index = phases.index(desired)
    if desired_index <= current_index:
        return current
    return phases[min(current_index + 1, desired_index)]


def replay(path: str | Path) -> CallCard:
    fixture = Path(path)
    if fixture.suffix == ".jsonl":
        raw: dict[str, Any] = {"call_id": "offline"}
        expected_call_id: str | None = None
        last_seq = 0
        positions: dict[str, int] = {}
        with fixture.open(encoding="utf-8") as stream:
            for position, line in enumerate(stream):
                if not line.strip():
                    continue
                event = JournalEvent.model_validate_json(line)
                if event.seq <= last_seq:
                    raise ValueError("journal slice seq must be strictly increasing")
                last_seq = event.seq
                if expected_call_id is None:
                    expected_call_id = event.call_id
                elif event.call_id != expected_call_id:
                    raise ValueError("journal slice cannot mix call_id values")
                raw["call_id"] = event.call_id
                if event.kind == "ledger.snapshot":
                    raw["ledger_snapshot"] = event.payload["facts"]
                    positions["ledger_snapshot"] = position
                elif event.kind == "opponent.summary":
                    raw["opponent_summary"] = event.payload
                    positions["opponent_summary"] = position
                elif event.kind == "strategist.call_card":
                    raw["previous_card"] = event.payload
                    positions["previous_card"] = position
        missing = {"ledger_snapshot", "opponent_summary", "previous_card"} - raw.keys()
        if missing:
            raise ValueError(f"journal slice lacks required events: {sorted(missing)}")
        if positions["previous_card"] >= min(positions["ledger_snapshot"], positions["opponent_summary"]):
            raise ValueError("journal snapshots must follow the previous call card")
    else:
        raw = json.loads(fixture.read_text(encoding="utf-8"))
    previous = CallCard.model_validate(raw["previous_card"])
    card = Strategist().revise(
        ledger_snapshot=raw["ledger_snapshot"],
        opponent_summary=raw["opponent_summary"],
        previous_card=previous,
        call_id=raw.get("call_id", "offline"),
    )
    print(json.dumps(card.model_dump(mode="json"), ensure_ascii=False))
    return card


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay strategist from ledger/opponent snapshots")
    parser.add_argument("--replay", type=Path, required=True)
    args = parser.parse_args()
    replay(args.replay)


if __name__ == "__main__":
    main()
