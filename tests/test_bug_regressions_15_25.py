"""Regression specifications for BUG-15 through BUG-25."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import negotiator.brain.ledger as ledger_module
from negotiator.brain.ledger import Ledger
from negotiator.brain.opponent import classify_tactic
from negotiator.brain.strategist import Strategist
from negotiator.core.contracts import (
    CallCard,
    LedgerFact,
    LedgerFactKind,
    NegotiationPhase,
    Source,
    SourceType,
    TacticType,
)
from negotiator.product.discovery import PlacesClient
from negotiator.product.report import build_report, load_moving_config, load_records
from negotiator.product.verify import FMCSAClient
from negotiator.tools.slice import slice_journal


FIXTURE_ROOT = Path(__file__).parents[1] / "negotiator" / "fixtures"


def _fact(*, fact_id: str, kind: LedgerFactKind, value: object, call_id: str = "c1") -> LedgerFact:
    return LedgerFact(
        id=fact_id,
        kind=kind,
        value=value,
        source=Source(type=SourceType.CONFIG, ref="test-config"),
        call_id=call_id,
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _previous_card() -> CallCard:
    return CallCard(
        version=1,
        phase=NegotiationPhase.DISCOVERY,
        phase_goal="discover",
        next_move="ask",
        tone_preset="curious",
    )


def test_bug_15_readding_identical_fact_without_timestamp_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ticks = iter((first, first + timedelta(microseconds=1)))

    class Clock:
        @staticmethod
        def now(_tz: timezone) -> datetime:
            return next(ticks)

    monkeypatch.setattr(ledger_module, "datetime", Clock)
    ledger = Ledger()
    ledger.add_config(
        fact_id="benchmark", kind=LedgerFactKind.BENCHMARK, value={"low": 3200},
        config_ref="moving.yaml#benchmark", call_id="c1",
    )
    repeated = ledger.add_config(
        fact_id="benchmark", kind=LedgerFactKind.BENCHMARK, value={"low": 3200},
        config_ref="moving.yaml#benchmark", call_id="c1",
    )

    assert repeated.value == {"low": 3200}


def test_bug_16_russian_negated_deadline_is_not_a_deadline_tactic() -> None:
    assert classify_tactic("Цена не истекает завтра.") is None


def test_bug_17_russian_another_customer_without_booking_context_is_not_pressure() -> None:
    assert classify_tactic("Другой клиент сказал, что цена нам подходит.") is None


def test_bug_18_strategist_skips_malformed_numeric_ledger_values() -> None:
    complete_quote = _fact(
        fact_id="quote",
        kind=LedgerFactKind.QUOTE,
        value={
            "total": 4000,
            "line_items": [
                {"code": code, "amount": 0, "disclosed": True}
                for code in range(1, 15)
            ],
        },
    )
    malformed_benchmark = LedgerFact.model_construct(
        id="benchmark", kind=LedgerFactKind.BENCHMARK, value={"low": "tbd"},
        source=Source(type=SourceType.CONFIG, ref="legacy"), call_id="c1",
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    card = Strategist().revise(
        ledger_snapshot=(complete_quote, malformed_benchmark),
        opponent_summary={"prices": [4000]},
        previous_card=_previous_card(),
        call_id="c1",
    )

    assert card.phase is NegotiationPhase.PRESSURE_TEST


def test_bug_19_quote_facts_require_a_structured_value() -> None:
    with pytest.raises(ValidationError):
        _fact(fact_id="quote", kind=LedgerFactKind.QUOTE, value="not a quote")


def test_bug_20_duplicate_fact_id_diagnostic_describes_the_actual_constraint() -> None:
    with pytest.raises(ValidationError) as caught:
        CallCard(
            version=1,
            phase=NegotiationPhase.OPENING,
            phase_goal="rapport",
            next_move="ask",
            allowed_fact_ids=("quote", "quote"),
            tone_preset="warm",
        )

    assert "entries must be non-empty strings and unique" in str(caught.value)


def test_bug_21_places_fallback_is_explicit_to_the_caller() -> None:
    result = PlacesClient(api_key="").search_movers("New York")

    assert result.fallback is True


@pytest.mark.parametrize(("method", "identifier"), (("verify_dot", "USDOT-123"), ("verify_mc", "MC-123")))
def test_bug_22_missing_fmcsa_key_is_a_configuration_error(method: str, identifier: str) -> None:
    with pytest.raises(RuntimeError, match="FMCSA_WEB_KEY"):
        getattr(FMCSAClient(web_key=""), method)(identifier)


def test_bug_23_load_records_rejects_unknown_outcome_fields(tmp_path: Path) -> None:
    raw = json.loads((FIXTURE_ROOT / "three_calls.json").read_text(encoding="utf-8"))
    raw["outcomes"][0]["outcome"]["call__id"] = "typo must not be ignored"
    source = tmp_path / "outcomes-with-typo.json"
    source.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValidationError, match="call__id"):
        load_records(source)


def test_bug_24_report_skips_uncited_record_when_other_records_are_usable() -> None:
    benchmark, fees = load_moving_config()
    records = load_records(FIXTURE_ROOT / "three_calls.json")

    report = build_report(
        (replace(records[0], citations=()), *records[1:]),
        benchmark_low=benchmark,
        fee_names=fees,
    )

    assert {item.mover for item in report.ranked} == {"Hudson Van Lines", "Empire Relocation"}


def test_bug_25_slice_journal_reports_invalid_jsonl_line_number(tmp_path: Path) -> None:
    source = tmp_path / "corrupt.jsonl"
    source.write_text('{"call_id":"c1"}\n{not-json}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSONL at line 2"):
        slice_journal(source)
