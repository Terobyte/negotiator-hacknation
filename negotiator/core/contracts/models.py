from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class NegotiationPhase(StrEnum):
    OPENING = "OPENING"
    DISCOVERY = "DISCOVERY"
    PRESSURE_TEST = "PRESSURE_TEST"
    LEVERAGE = "LEVERAGE"
    COMMIT = "COMMIT"
    WRAP = "WRAP"


class InventorySource(StrEnum):
    VOICE = "voice"
    DOC = "doc"
    BOTH = "both"


class DateWindow(Contract):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> DateWindow:
        if self.end < self.start:
            raise ValueError("date_window.end must not precede start")
        return self


class JobSpec(Contract):
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    distance_mi: float = Field(gt=0)
    size: Literal["studio", "1BR", "2BR", "3BR", "4BR+"]
    date_window: DateWindow
    floors: int = Field(default=0, ge=0)
    elevator: bool = False
    specialty_items: tuple[str, ...] = ()
    inventory_src: InventorySource
    budget_ceiling: Decimal = Field(gt=0, json_schema_extra={"private": True})
    confirmed: Literal[True]

    @field_validator("origin", "destination")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("location cannot be blank")
        return value.strip()


class CallCard(Contract):
    version: int = Field(ge=1)
    phase: NegotiationPhase
    phase_goal: str = Field(min_length=1)
    next_move: str = Field(min_length=1)
    allowed_fact_ids: tuple[str, ...] = ()
    tone_preset: str = Field(min_length=1)
    client_directives: tuple[str, ...] = ()
    stance: str = "neutral"

    @field_validator("allowed_fact_ids")
    @classmethod
    def unique_fact_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item for item in value):
            raise ValueError("allowed_fact_ids entries must be non-empty strings and unique")
        return value

    @field_validator("stance")
    @classmethod
    def known_stance(cls, value: str) -> str:
        if value not in ("neutral", "good", "bad"):
            raise ValueError("stance must be one of 'neutral', 'good', 'bad'")
        return value


_GATE_CAPABILITY = object()


class ApprovedUtterance(Contract):
    """A gate-issued capability, not a generally constructible data container."""

    text: str = Field(min_length=1)
    card_version: int = Field(ge=1)
    gate_verdict_ref: str = Field(min_length=1)
    _gate_issued: bool = PrivateAttr(default=False)

    def __init__(self, **data: Any) -> None:
        capability = data.pop("_gate_capability", None)
        if capability is not _GATE_CAPABILITY:
            raise TypeError("ApprovedUtterance can only be issued by the honesty gate")
        super().__init__(**data)
        object.__setattr__(self, "_gate_issued", True)

    @property
    def gate_issued(self) -> bool:
        """Runtime proof for the TTS boundary in the next build phase."""

        return self._gate_issued


class SourceType(StrEnum):
    TRANSCRIPT = "transcript"
    CONFIG = "config"
    API = "api"


class Source(Contract):
    type: SourceType
    ref: str = Field(min_length=1)
    span: str | None = None

    @model_validator(mode="after")
    def transcript_needs_span(self) -> Source:
        if self.type is SourceType.TRANSCRIPT and not self.span:
            raise ValueError("transcript source requires span")
        return self


class LedgerFactKind(StrEnum):
    QUOTE = "quote"
    BENCHMARK = "benchmark"
    JOBSPEC = "jobspec"
    VERIFICATION = "verification"
    DIRECTIVE = "directive"


class LedgerFact(Contract):
    id: str = Field(min_length=1)
    kind: LedgerFactKind
    value: dict[str, Any] | str | bool
    source: Source
    call_id: str = Field(min_length=1)
    ts: datetime

    @model_validator(mode="after")
    def value_matches_kind(self) -> LedgerFact:
        value = self.value
        if self.kind in {LedgerFactKind.QUOTE, LedgerFactKind.BENCHMARK, LedgerFactKind.JOBSPEC} and not isinstance(value, dict):
            raise ValueError(f"{self.kind.value} facts require a structured object value")
        if self.kind is LedgerFactKind.QUOTE:
            _positive_number(value.get("total"), "quote.total")
        elif self.kind is LedgerFactKind.BENCHMARK:
            _positive_number(value.get("low"), "benchmark.low")
            if value.get("high") is not None:
                _positive_number(value["high"], "benchmark.high")
        elif self.kind is LedgerFactKind.JOBSPEC and not value:
            raise ValueError("jobspec facts require a non-empty object")
        elif self.kind is LedgerFactKind.VERIFICATION and not isinstance(value, (dict, bool)):
            raise ValueError("verification facts require a boolean or structured object")
        elif self.kind is LedgerFactKind.DIRECTIVE:
            if not isinstance(value, (dict, str)) or not value:
                raise ValueError("directive facts require non-empty text or a structured object")
        return self


def _positive_number(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a finite positive number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return parsed


class LineItem(Contract):
    code: int = Field(ge=1, le=14)
    amount: Decimal = Field(ge=0)
    disclosed: bool


class EstimateType(StrEnum):
    BINDING = "binding"
    NON_BINDING = "non_binding"
    BNTE = "BNTE"


class Deposit(Contract):
    amount: Decimal = Field(ge=0)
    pct_of_total: float = Field(ge=0, le=100)
    refundable: bool
    payment_methods: tuple[str, ...] = ()


class Quote(Contract):
    mover_id: str = Field(min_length=1)
    total: Decimal = Field(gt=0)
    line_items: tuple[LineItem, ...]
    estimate_type: EstimateType
    deposit: Deposit
    carrier_or_broker: Literal["carrier", "broker"]
    usdot: str | None = None
    mc: str | None = None
    transcript_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def line_items_and_totals(self) -> Quote:
        codes = [item.code for item in self.line_items]
        if len(codes) != len(set(codes)):
            raise ValueError("line item codes must be unique")
        expected_pct = float(self.deposit.amount / self.total * 100)
        if abs(expected_pct - self.deposit.pct_of_total) > 0.11:
            raise ValueError("deposit pct_of_total does not match amount/total")
        return self


class TacticType(StrEnum):
    PRESSURE = "pressure"
    VAGUE = "vague"
    STONEWALL = "stonewall"
    DEADLINE = "deadline"
    LOWBALL = "lowball"


class TacticEvent(Contract):
    type: TacticType
    utterance_ref: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class StanceEventKind(StrEnum):
    """The fixed vocabulary of typed stance signals. Suspicion-bearing: INJECTION_DETECTED,
    RED_FLAG_FEE, FEE_DENIAL_CAUGHT, PRICE_JUMP, PRESSURE_DEADLINE. Trust-bearing:
    WILLING_ITEMIZATION, PRICE_NEAR_BENCHMARK, CONCESSION_MADE. Which bucket a kind belongs to
    is a stance.py concern (logic), not this contract's."""

    INJECTION_DETECTED = "injection_detected"
    RED_FLAG_FEE = "red_flag_fee"
    FEE_DENIAL_CAUGHT = "fee_denial_caught"
    PRICE_JUMP = "price_jump"
    PRESSURE_DEADLINE = "pressure_deadline"
    WILLING_ITEMIZATION = "willing_itemization"
    PRICE_NEAR_BENCHMARK = "price_near_benchmark"
    CONCESSION_MADE = "concession_made"


class StanceEvent(Contract):
    """A typed, caller-detected signal fed to a brain StanceMachine. The machine never detects
    anything itself -- callers (e.g. the arena, or a live call in a later build) classify the
    evidence and hand over one of these; the machine only accumulates weight and switches.
    ``weight_key`` looks up the event's weight in the stance config; it defaults to ``kind``
    but callers may point two events of the same kind at different weight buckets."""

    kind: StanceEventKind
    weight_key: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class CallStatus(StrEnum):
    QUOTED = "quoted"
    REFUSED = "refused"
    CALLBACK = "callback"
    HANGUP = "hangup"


class CallOutcome(Contract):
    call_id: str = Field(min_length=1)
    mover_id: str = Field(min_length=1)
    status: CallStatus
    quote: Quote | None = None
    red_flags: tuple[str, ...] = ()
    transcript_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def quoted_has_quote(self) -> CallOutcome:
        if (self.status is CallStatus.QUOTED) != (self.quote is not None):
            raise ValueError("only quoted outcomes must contain quote")
        return self


class Speaker(StrEnum):
    AGENT = "agent"
    COUNTERPARTY = "counterparty"


class Citation(Contract):
    transcript_span: str = Field(min_length=1)
    recording_url: str = Field(min_length=1)
    speaker: Speaker
    quote: str = Field(min_length=1)

    @field_validator("recording_url")
    @classmethod
    def audio_fragment(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")) or "#t=" not in value:
            raise ValueError("recording_url must be an HTTP URL with #t= offset")
        try:
            if float(value.rsplit("#t=", 1)[1]) < 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError("recording_url must have a non-negative numeric offset") from exc
        return value


class RankedMover(Contract):
    mover: str = Field(min_length=1)
    normalized_total: Decimal = Field(gt=0)
    missing_items: tuple[str, ...] = ()
    red_flags: tuple[str, ...] = ()
    citations: tuple[Citation, ...] = Field(min_length=1)


class Report(Contract):
    recommendation_plain: str = Field(min_length=1)
    ranked: tuple[RankedMover, ...] = Field(min_length=1)


class BusEvent(Contract):
    call_id: str = Field(min_length=1)
    module: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    payload: dict[str, Any]
    refs: tuple[str, ...] = ()
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class JournalEvent(BusEvent):
    seq: int = Field(ge=1)


SEED_CALL_CARD = CallCard(
    version=1,
    phase=NegotiationPhase.OPENING,
    phase_goal="AI-disclosure + rapport",
    next_move="Disclose that I am an AI assistant, then build rapport.",
    allowed_fact_ids=(),
    tone_preset="warm",
    client_directives=(),
)
