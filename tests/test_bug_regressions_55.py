"""Regression contract for finding BUG-55 (gate bare-number bypass).

Found 2026-07-19 by loop-3's adversarial judge in the arena self-play run, via a
hostile live-defender fake: a free-form LLM draft can say a price-shaped bare
number ("We can do this for 9999 right now, final answer.") with no `$` sign and
no quote-language marker, and `HonestyGate._unsupported_claim_reason` never adds
it to the checked `amounts` set at all -- only `_money_amounts` (unconditional)
and, when `quote_language` is truthy, `_bare_amounts` / `_bare_word_amounts`. A
bare digit or word-form number with no currency word and no quote-language cue
sails past every check and reaches the honesty gate as "supported" even though no
ledger fact backs it. Root cause: gate.py:112-128 only ever consults the
bare/word-amount extractors *inside* the `if quote_language:` branch, so
quote-language was (wrongly) treated as a precondition for bare-number checking
instead of an *additional* trigger that also sweeps in sub-100 amounts.

The word-form case is narrower: `_bare_word_amounts` only returns a value when a
marker token (quote/quoted/estimate/estimated/offer/bid/price/rate, or the
Russian смета/сметы/цену/цена/ставка) sits within 3 tokens of the number. For
English, every one of those markers is also a `_QUOTE_LANGUAGE_RE` trigger, so an
English word-form bypass isn't reachable through that path (marker present =>
quote_language already True => already checked pre-fix). The Russian marker
"ставка" (rate) is the one word in `_bare_word_amounts`'s marker set that
`_QUOTE_LANGUAGE_RE` does NOT match (its Russian side only covers
котиров*/смет*/цен*) -- so a Russian "ставка"-flagged bare word-number is a
genuine quote-language-free bypass, exercised below.

Fix (gate.py, this bug): bare and word amounts >= _PRICE_SHAPED_MIN (100) are now
added to `amounts` unconditionally; quote-language keeps its existing behaviour
of sweeping in ALL bare/word amounts regardless of magnitude (unchanged).
"""

from __future__ import annotations

from datetime import datetime, timezone

from negotiator.call.gate import HonestyGate
from negotiator.core.contracts import (
    CallCard,
    LedgerFact,
    LedgerFactKind,
    NegotiationPhase,
    Source,
    SourceType,
)


def _card(*fact_ids: str) -> CallCard:
    return CallCard(
        version=1,
        phase=NegotiationPhase.LEVERAGE,
        phase_goal="use evidence",
        next_move="ask a calibrated question",
        allowed_fact_ids=fact_ids,
        tone_preset="firm",
    )


def _quote_fact(total: int, fact_id: str = "q1") -> LedgerFact:
    return LedgerFact(
        id=fact_id,
        kind=LedgerFactKind.QUOTE,
        value={"total": total},
        source=Source(type=SourceType.TRANSCRIPT, ref="call.jsonl", span="1-2"),
        call_id="c1",
        ts=datetime.now(timezone.utc),
    )


def _gate() -> HonestyGate:
    return HonestyGate(stall_phrases=("One moment while I check my notes.",))


def test_bug_55_bare_digit_price_with_no_dollar_sign_and_no_quote_language_blocks():
    decision = _gate().evaluate(
        draft="We can do this for 9999 right now, final answer.",
        card=_card(),
        ledger_facts=(),
    )
    assert decision.verdict == "block"
    assert decision.reason == "unsupported_quote_amount"


def test_bug_55_bare_word_form_price_with_no_quote_language_blocks():
    # "ставка" (rate) is in _bare_word_amounts' own marker set but is NOT matched
    # by _QUOTE_LANGUAGE_RE's Russian side (котиров*/смет*/цен* only) -- the one
    # marker word that lets a word-form number through quote-language-free.
    decision = _gate().evaluate(
        draft="Наша ставка девятьсот девяносто девять, решайте сейчас.",
        card=_card(),
        ledger_facts=(),
    )
    assert decision.verdict == "block"
    assert decision.reason == "unsupported_quote_amount"


def test_bug_55_bare_amount_backed_by_ledger_fact_still_allowed():
    decision = _gate().evaluate(
        draft="We can do this for 3400 total, no surprises.",
        card=_card("q1"),
        ledger_facts=(_quote_fact(3400),),
    )
    assert decision.verdict == "allow"
    assert decision.approved is not None


def test_bug_55_small_operational_numbers_stay_allowed():
    hours = _gate().evaluate(
        draft="I need 24 hours to confirm.",
        card=_card(),
        ledger_facts=(),
    )
    assert hours.verdict == "allow"

    days = _gate().evaluate(
        draft="Give me 3 days.",
        card=_card(),
        ledger_facts=(),
    )
    assert days.verdict == "allow"


def test_bug_55_dollar_sign_amount_unbacked_still_blocks_unchanged():
    decision = _gate().evaluate(
        draft="$9,999",
        card=_card(),
        ledger_facts=(),
    )
    assert decision.verdict == "block"
    assert decision.reason == "unsupported_quote_amount"
