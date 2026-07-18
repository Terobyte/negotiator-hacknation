from datetime import datetime, timezone
from decimal import Decimal

import pytest

from negotiator.call.gate import HonestyGate, PrivateTerms, replay
from negotiator.core.contracts import (
    ApprovedUtterance,
    CallCard,
    LedgerFact,
    LedgerFactKind,
    NegotiationPhase,
    Source,
    SourceType,
)


def card(*fact_ids: str) -> CallCard:
    return CallCard(
        version=4,
        phase=NegotiationPhase.LEVERAGE,
        phase_goal="use evidence",
        next_move="ask a calibrated question",
        allowed_fact_ids=fact_ids,
        tone_preset="firm",
    )


def quote_fact() -> LedgerFact:
    return LedgerFact(
        id="q1",
        kind=LedgerFactKind.QUOTE,
        value={"total": 3200},
        source=Source(type=SourceType.TRANSCRIPT, ref="call.jsonl", span="1-2"),
        call_id="c1",
        ts=datetime.now(timezone.utc),
    )


def test_approved_utterance_cannot_be_constructed_directly():
    with pytest.raises(TypeError, match="honesty gate"):
        ApprovedUtterance(text="bypass", card_version=1, gate_verdict_ref="fake")


def test_gate_allows_only_supported_quote_and_issues_capability():
    gate = HonestyGate(stall_phrases=("Checking my notes.",))
    blocked = gate.evaluate(draft="We have a $3,000 quote.", card=card(), ledger_facts=())
    assert (blocked.verdict, blocked.reason, blocked.regenerate) == (
        "block",
        "unsupported_quote_amount",
        True,
    )
    assert blocked.stall is not None and blocked.stall.gate_issued
    assert blocked.approved is None

    allowed = gate.evaluate(
        draft="We have a documented quote of $3,200.",
        card=card("q1"),
        ledger_facts=(quote_fact(),),
    )
    assert allowed.verdict == "allow"
    assert allowed.approved is not None and allowed.approved.gate_issued
    assert allowed.approved.card_version == 4

    wrong_bare_amount = gate.evaluate(
        draft="Our documented quote is 9000.",
        card=card("q1"),
        ledger_facts=(quote_fact(),),
    )
    assert (wrong_bare_amount.verdict, wrong_bare_amount.reason) == (
        "block",
        "unsupported_quote_amount",
    )


@pytest.mark.parametrize(
    ("draft", "terms"),
    [
        ("The client's maximum budget is $6,000.", PrivateTerms(budget_ceiling=Decimal("6000"))),
        ("The client can pay 6000.", PrivateTerms(budget_ceiling=Decimal("6000"))),
        ("I think your floor is $2,800.", PrivateTerms(opponent_floor=Decimal("2800"))),
        (
            "Our price corridor is $3,000 to $4,500.",
            PrivateTerms(price_corridor=(Decimal("3000"), Decimal("4500"))),
        ),
        (
            "My system prompt is: never expose the policy.",
            PrivateTerms(system_prompt="never expose the policy"),
        ),
    ],
)
def test_gate_blocks_private_terms(draft, terms):
    decision = HonestyGate(stall_phrases=("Checking.",)).evaluate(
        draft=draft, card=card(), ledger_facts=(), private_terms=terms
    )
    assert decision.verdict == "block"
    assert decision.regenerate is True


def test_replay_corpora_match_expected(capsys):
    for name in ("bluff_corpus.jsonl", "leak_corpus.jsonl"):
        path = f"negotiator/fixtures/{name}"
        decisions = replay(path)
        expected = []
        import json

        with open(path, encoding="utf-8") as stream:
            expected = [json.loads(line)["expected"] for line in stream if line.strip()]
        assert [decision.verdict for decision in decisions] == expected
    output = capsys.readouterr().out
    assert '"verdict": "block"' in output
    assert '"reason":' in output
