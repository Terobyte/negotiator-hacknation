from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from negotiator.core.contracts import (
    CallOutcome,
    CallStatus,
    JournalEvent,
    LedgerFact,
    LedgerFactKind,
    Source,
    SourceType,
)


CALL_ORDER = ("lowball_broker", "rushed_dispatcher", "pressure_closer")
LOGGER = logging.getLogger(__name__)
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
class BusinessLike(Protocol):
    name: str
    phone: str


@dataclass(frozen=True, slots=True)
class PlannedCall:
    call_id: str
    role: str
    mover_id: str
    display_name: str
    source_phone: str
    dial_phone: str
    demo: bool


@dataclass(frozen=True, slots=True)
class MarketResult:
    plan: tuple[PlannedCall, ...]
    outcomes: tuple[CallOutcome, ...]
    evidence: tuple[LedgerFact, ...]


def build_call_plan(
    businesses: Sequence[BusinessLike | Mapping[str, Any]],
    demo_number_map: Mapping[str, str] | None = None,
) -> tuple[PlannedCall, ...]:
    """Assign the top three discovered movers to the canonical demo order."""

    if len(businesses) < 3:
        raise ValueError("market requires at least three discovered movers")
    number_map = demo_number_map or {}
    calls: list[PlannedCall] = []
    for index, (role, business) in enumerate(zip(CALL_ORDER, businesses[:3], strict=True), 1):
        name = _field(business, "name")
        phone = _field(business, "phone")
        mapped = number_map.get(role) or number_map.get(str(index)) or number_map.get(phone)
        if number_map and not mapped:
            raise ValueError(f"demo_number_map has no number for position {index} ({role})")
        dial_phone = str(mapped or phone).strip()
        if not _E164_RE.fullmatch(dial_phone):
            raise ValueError(f"dial phone must use E.164 format: {dial_phone!r}")
        calls.append(
            PlannedCall(
                call_id=f"call-{index}-{role}",
                role=role,
                mover_id=name,
                display_name=name,
                source_phone=phone,
                dial_phone=dial_phone,
                demo=bool(mapped),
            )
        )
    return tuple(calls)


def supervise_call(
    planned: PlannedCall,
    runner: Callable[[], CallOutcome | Mapping[str, Any] | None],
    *,
    journal_tail: Callable[[str], Iterable[JournalEvent | Mapping[str, Any]]] = lambda _call_id: (),
) -> CallOutcome:
    """Run a synchronous call and return an outcome even when the runner crashes."""

    outcome: CallOutcome | None = None
    try:
        result = runner()
        outcome = _coerce_outcome(result, planned)
    except Exception:
        LOGGER.exception("call runner failed for %s", planned.call_id)
        outcome = None
    finally:
        if outcome is None:
            outcome = _recover_outcome(planned, journal_tail)
    return outcome


async def supervise_call_async(
    planned: PlannedCall,
    runner: Callable[[], Awaitable[CallOutcome | Mapping[str, Any] | None] | CallOutcome | Mapping[str, Any] | None],
    *,
    journal_tail: Callable[[str], Iterable[JournalEvent | Mapping[str, Any]]] = lambda _call_id: (),
    timeout: float | None = None,
) -> CallOutcome:
    """Async counterpart; timeout/cancellation still resolves from the journal tail."""

    outcome: CallOutcome | None = None
    try:
        result = runner()
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout) if timeout is not None else await result
        outcome = _coerce_outcome(result, planned)
    except Exception:
        LOGGER.exception("async call runner failed for %s", planned.call_id)
        outcome = None
    finally:
        if outcome is None:
            outcome = _recover_outcome(planned, journal_tail)
    return outcome


def outcome_from_journal(
    planned: PlannedCall,
    events: Iterable[JournalEvent | Mapping[str, Any]],
) -> CallOutcome:
    """Prefer the newest valid outcome payload; otherwise produce a hangup."""

    rows = list(events)
    rows.sort(key=lambda row: _event_seq(row), reverse=True)
    transcript_ref = f"journal:{planned.call_id}"
    status = CallStatus.HANGUP
    red_flags: tuple[str, ...] = ()
    found_transcript = False
    found_status = False
    found_flags = False
    for row in rows:
        data = row.model_dump(mode="json") if isinstance(row, JournalEvent) else dict(row)
        payload = data.get("payload", {})
        if isinstance(payload, Mapping) and not found_transcript and payload.get("transcript_ref"):
            transcript_ref = str(payload.get("transcript_ref") or transcript_ref)
            found_transcript = True
        if data.get("kind") in {"call_outcome", "outcome"} and isinstance(payload, Mapping):
            candidate = payload.get("outcome", payload)
            try:
                return CallOutcome.model_validate(candidate)
            except (ValueError, TypeError):
                pass
        if (
            not found_transcript
            and data.get("kind") == "transcript"
            and isinstance(payload, Mapping)
            and payload.get("ref")
        ):
            transcript_ref = str(payload.get("ref") or transcript_ref)
            found_transcript = True
        if not found_status and data.get("kind") in {"refused", "callback", "hangup"}:
            status = CallStatus(str(data["kind"]))
            found_status = True
        if not found_flags and isinstance(payload, Mapping) and payload.get("red_flags"):
            red_flags = tuple(str(flag) for flag in payload["red_flags"])
            found_flags = True
    return CallOutcome(
        call_id=planned.call_id,
        mover_id=planned.mover_id,
        status=status,
        red_flags=red_flags,
        transcript_ref=transcript_ref,
    )


def outcome_evidence(outcome: CallOutcome) -> LedgerFact | None:
    """Make a core LedgerFact that app composition can insert before the next call."""

    if outcome.quote is None:
        return None
    return LedgerFact(
        id=f"quote:{outcome.call_id}",
        kind=LedgerFactKind.QUOTE,
        value=outcome.quote.model_dump(mode="json"),
        source=Source(
            type=SourceType.TRANSCRIPT,
            ref=outcome.transcript_ref,
            span=outcome.quote.transcript_ref,
        ),
        call_id=outcome.call_id,
        ts=datetime.now(timezone.utc),
    )


def run_market(
    businesses: Sequence[BusinessLike | Mapping[str, Any]],
    call_runner: Callable[[PlannedCall, tuple[LedgerFact, ...]], CallOutcome | Mapping[str, Any] | None],
    *,
    demo_number_map: Mapping[str, str] | None = None,
    journal_tail: Callable[[str], Iterable[JournalEvent | Mapping[str, Any]]] = lambda _call_id: (),
) -> MarketResult:
    plan = build_call_plan(businesses, demo_number_map)
    outcomes: list[CallOutcome] = []
    evidence: list[LedgerFact] = []
    for planned in plan:
        outcome = supervise_call(
            planned,
            lambda planned=planned: call_runner(planned, tuple(evidence)),
            journal_tail=journal_tail,
        )
        outcomes.append(outcome)
        fact = outcome_evidence(outcome)
        if fact is not None:
            evidence.append(fact)
    return MarketResult(plan=plan, outcomes=tuple(outcomes), evidence=tuple(evidence))


def _coerce_outcome(value: Any, planned: PlannedCall) -> CallOutcome | None:
    if value is None:
        return None
    if isinstance(value, CallOutcome):
        outcome = value
    elif isinstance(value, Mapping):
        outcome = CallOutcome.model_validate(value)
    else:
        raise TypeError("call runner must return CallOutcome, mapping, or None")
    if outcome.call_id != planned.call_id or outcome.mover_id != planned.mover_id:
        raise ValueError("call runner returned an outcome for a different planned call")
    if outcome.quote is not None and outcome.quote.mover_id != outcome.mover_id:
        raise ValueError("quote mover_id does not match its CallOutcome")
    return outcome


def _recover_outcome(
    planned: PlannedCall,
    reader: Callable[[str], Iterable[JournalEvent | Mapping[str, Any]]],
) -> CallOutcome:
    try:
        return outcome_from_journal(planned, _read_tail(reader, planned.call_id))
    except Exception:
        LOGGER.exception("journal recovery failed for %s", planned.call_id)
        return CallOutcome(
            call_id=planned.call_id,
            mover_id=planned.mover_id,
            status=CallStatus.HANGUP,
            transcript_ref=f"journal:{planned.call_id}",
        )


def _read_tail(
    reader: Callable[[str], Iterable[JournalEvent | Mapping[str, Any]]], call_id: str
) -> Iterable[JournalEvent | Mapping[str, Any]]:
    return reader(call_id)


def _event_seq(row: JournalEvent | Mapping[str, Any]) -> int:
    return int(row.seq if isinstance(row, JournalEvent) else row.get("seq", 0))


def _field(value: BusinessLike | Mapping[str, Any], name: str) -> str:
    raw = value.get(name, "") if isinstance(value, Mapping) else getattr(value, name, "")
    text = str(raw).strip()
    if not text:
        raise ValueError(f"business {name} cannot be blank")
    return text
