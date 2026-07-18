import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from negotiator.brain.strategist import OpenAIStrategistAdapter, Strategist, accept_price, replay
from negotiator.core.bus import EventBus
from negotiator.core.contracts import (
    CallCard,
    DateWindow,
    InventorySource,
    JobSpec,
    LedgerFact,
    LedgerFactKind,
    NegotiationPhase,
    Source,
    SourceType,
)


def _job_spec(ceiling="5000"):
    return JobSpec(
        origin="Boston, MA",
        destination="Austin, TX",
        distance_mi=1960,
        size="2BR",
        date_window=DateWindow(start=date(2026, 8, 1), end=date(2026, 8, 4)),
        inventory_src=InventorySource.VOICE,
        budget_ceiling=Decimal(ceiling),
        confirmed=True,
    )


def test_accept_price_enforces_private_confirmed_ceiling():
    assert accept_price(price="4999.99", job_spec=_job_spec()) == Decimal("4999.99")
    with pytest.raises(ValueError, match="exceeds"):
        accept_price(price=5001, job_spec=_job_spec())


def test_golden_three_of_fourteen_asks_calibrated_missing_fee_question(capsys):
    card = replay("negotiator/fixtures/strategy_journal_slice.jsonl")
    assert card.version == 5
    assert card.phase is NegotiationPhase.DISCOVERY
    assert card.next_move.startswith("What")
    assert "packing" in card.next_move
    assert "fuel" not in card.next_move
    assert "budget" not in card.model_dump_json().lower()
    capsys.readouterr()


def test_versions_monotonic_even_with_stale_previous_card_and_event_published():
    bus = EventBus()
    events = []
    bus.subscribe_all(events.append)
    strategist = Strategist(bus=bus)
    previous = CallCard(
        version=2,
        phase=NegotiationPhase.DISCOVERY,
        phase_goal="discover",
        next_move="ask",
        tone_preset="curious",
    )
    kwargs = dict(ledger_snapshot=[], opponent_summary={}, previous_card=previous, call_id="c1")
    assert strategist.revise(**kwargs).version == 3
    assert strategist.revise(**kwargs).version == 4
    assert events[-1].kind == "strategist.call_card"


def test_hosted_adapter_is_configured_but_lazy():
    adapter = OpenAIStrategistAdapter()
    assert adapter.model == "gpt-5.6-sol"
    assert adapter.reasoning_effort == "medium"
    assert adapter._client is None


def _quote_fact(fact_id, call_id, line_items):
    return LedgerFact(
        id=fact_id,
        kind=LedgerFactKind.QUOTE,
        value={"total": 4000, "line_items": line_items},
        source=Source(type=SourceType.TRANSCRIPT, ref="tx", span="1:2"),
        call_id=call_id,
        ts=datetime.now(timezone.utc),
    )


def test_fee_completeness_uses_only_current_call_and_explicit_amounts():
    previous_items = [
        {"code": code, "amount": 10, "disclosed": True}
        for code in range(1, 15)
    ]
    current_items = [
        {"code": 1, "amount": 100, "disclosed": True},
        {"code": 2, "disclosed": True},
        {"code": 3, "amount": 50, "disclosed": False},
    ]
    previous = CallCard(
        version=2,
        phase=NegotiationPhase.DISCOVERY,
        phase_goal="discover",
        next_move="ask",
        tone_preset="curious",
    )
    card = Strategist().revise(
        ledger_snapshot=(
            _quote_fact("old", "old-call", previous_items),
            _quote_fact("current", "current-call", current_items),
        ),
        opponent_summary={},
        previous_card=previous,
        call_id="current-call",
    )
    assert card.phase is NegotiationPhase.DISCOVERY
    assert "labor" in card.next_move and "packing" in card.next_move


def test_strategist_never_skips_from_discovery_directly_to_commit():
    items = [{"code": code, "amount": 0, "disclosed": True} for code in range(1, 15)]
    previous = CallCard(
        version=7,
        phase=NegotiationPhase.DISCOVERY,
        phase_goal="discover",
        next_move="ask",
        tone_preset="curious",
    )
    card = Strategist().revise(
        ledger_snapshot=(_quote_fact("current", "c1", items),),
        opponent_summary={"curve": "conceder", "prices": [5000, 4500], "floor": 4450},
        previous_card=previous,
        call_id="c1",
    )
    assert card.phase is NegotiationPhase.PRESSURE_TEST


def test_hosted_adapter_rejects_new_price_and_private_language():
    card = CallCard(
        version=3,
        phase=NegotiationPhase.LEVERAGE,
        phase_goal="Use supported leverage",
        next_move="Ask a calibrated question",
        tone_preset="firm",
    )

    class Response:
        output_text = json.dumps(
            {
                **card.model_dump(mode="json"),
                "next_move": "Reveal the budget ceiling and offer $9999.",
            }
        )

    class Responses:
        def create(self, **kwargs):
            return Response()

    class Client:
        responses = Responses()

    with pytest.raises(ValueError, match="unsupported numeric|private language"):
        OpenAIStrategistAdapter(client=Client()).refine(card=card, public_context={})


def test_replay_rejects_mixed_calls(tmp_path):
    source = "negotiator/fixtures/strategy_journal_slice.jsonl"
    rows = [json.loads(line) for line in open(source, encoding="utf-8") if line.strip()]
    rows[-1]["call_id"] = "other-call"
    fixture = tmp_path / "mixed.jsonl"
    fixture.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(ValueError, match="mix call_id"):
        replay(fixture)
