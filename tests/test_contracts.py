from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from negotiator.core.contracts import CallCard, CallOutcome, CallStatus, Citation, DateWindow, Deposit, EstimateType, InventorySource, JobSpec, LineItem, NegotiationPhase, Quote, SEED_CALL_CARD, Source, SourceType, Speaker


def job_spec(**updates):
    data = dict(origin="Boston, MA", destination="New York, NY", distance_mi=215, size="2BR", date_window=DateWindow(start=date(2026, 8, 1), end=date(2026, 8, 3)), floors=2, elevator=False, specialty_items=("piano",), inventory_src=InventorySource.VOICE, budget_ceiling=Decimal("6000"), confirmed=True)
    data.update(updates)
    return JobSpec(**data)


def test_job_spec_is_confirmed_and_private_in_schema():
    assert job_spec().confirmed is True
    with pytest.raises(ValidationError):
        job_spec(confirmed=False)
    assert JobSpec.model_json_schema()["properties"]["budget_ceiling"]["private"] is True


def test_call_card_rejects_duplicate_fact_ids():
    with pytest.raises(ValidationError):
        CallCard(version=1, phase=NegotiationPhase.OPENING, phase_goal="rapport", next_move="disclose", allowed_fact_ids=("x", "x"), tone_preset="warm")


def test_seed_card_is_always_valid_for_cold_start():
    assert SEED_CALL_CARD.phase is NegotiationPhase.OPENING
    assert SEED_CALL_CARD.allowed_fact_ids == ()
    assert SEED_CALL_CARD.tone_preset == "warm"


def test_transcript_provenance_requires_span():
    with pytest.raises(ValidationError):
        Source(type=SourceType.TRANSCRIPT, ref="call.jsonl")


def quote() -> Quote:
    return Quote(mover_id="m1", total=Decimal("4000"), line_items=(LineItem(code=1, amount=Decimal("4000"), disclosed=True),), estimate_type=EstimateType.BINDING, deposit=Deposit(amount=Decimal("1000"), pct_of_total=25, refundable=True, payment_methods=("card",)), carrier_or_broker="carrier", usdot="123456", transcript_ref="transcript:10-20")


def test_quote_invariants_and_outcome_pairing():
    assert quote().total == Decimal("4000")
    with pytest.raises(ValidationError):
        CallOutcome(call_id="c", mover_id="m", status=CallStatus.QUOTED, transcript_ref="t")
    with pytest.raises(ValidationError):
        Quote(**{**quote().model_dump(), "deposit": Deposit(amount=1, pct_of_total=25, refundable=True)})


def test_citation_requires_audio_offset():
    with pytest.raises(ValidationError):
        Citation(transcript_span="1-2", recording_url="https://audio.test/a.mp3", speaker=Speaker.AGENT, quote="hello")
    assert Citation(transcript_span="1-2", recording_url="https://audio.test/a.mp3#t=1.5", speaker=Speaker.AGENT, quote="hello").recording_url.endswith("#t=1.5")
