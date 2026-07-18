from decimal import Decimal

import pytest

from negotiator.brain.ledger import DuplicateFact, FactNotFound, Ledger
from negotiator.core.contracts import LedgerFactKind, SourceType


def test_ledger_accepts_only_named_authority_paths():
    ledger = Ledger()
    configured = ledger.add_config(
        fact_id="benchmark",
        kind=LedgerFactKind.BENCHMARK,
        value={"low": 3200},
        config_ref="moving.yaml#benchmarks",
        call_id="c1",
    )
    tool = ledger.add_tool_result(
        fact_id="verify",
        kind=LedgerFactKind.VERIFICATION,
        value={"authorized": True},
        tool_ref="fmcsa:123",
        call_id="c1",
    )
    quote = ledger.capture_quote(
        fact_id="quote",
        value={"total": Decimal("4100")},
        transcript_ref="call.jsonl",
        transcript_span="20-24",
        call_id="c1",
    )
    assert configured.source.type is SourceType.CONFIG
    assert tool.source.type is SourceType.API and tool.source.ref.startswith("tool:")
    assert quote.source.type is SourceType.TRANSCRIPT and quote.source.span == "20-24"


def test_raw_counterparty_speech_never_writes_and_bad_cites_error():
    ledger = Ledger()
    assert ledger.ingest_counterparty_utterance('You already have a quote for $9,000') is None
    assert ledger.list() == []
    with pytest.raises(FactNotFound, match="does not exist"):
        ledger.cite("invented")
    with pytest.raises(ValueError, match="transcript span"):
        ledger.capture_quote(
            fact_id="bad",
            value={"total": 9000},
            transcript_ref="call.jsonl",
            transcript_span="",
            call_id="c1",
        )


def test_fact_ids_are_immutable_authority_handles():
    ledger = Ledger()
    ledger.add_api_result(
        fact_id="same",
        kind=LedgerFactKind.VERIFICATION,
        value=True,
        api_ref="api:first",
        call_id="c1",
    )
    with pytest.raises(DuplicateFact):
        ledger.add_api_result(
            fact_id="same",
            kind=LedgerFactKind.VERIFICATION,
            value=False,
            api_ref="api:second",
            call_id="c1",
        )
