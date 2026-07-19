"""Regression contracts for findings BUG-48 through BUG-54 (self-play arena + gate audit).

These are NEW findings from the round-3 audit (2026-07-18) that were NOT yet fixed
in the working tree at the time of writing — so each test asserts the *current*
(buggy) behaviour. They turn red the moment someone fixes the bug without removing
the test, which is exactly the trip-wire CLAUDE.md asks for.

Scope:
  BUG-48  gate._word_money_amounts emits phantom suffix amounts → false-positive
          leak-guard blocks on legitimate supported quotes.
  BUG-49  arena genome.talker_prompt is validated + mutated across generations
          but never reaches the OfflineTalkerAdapter — dead steering wheel.
  BUG-50  arena judge() compares the closed price to the benchmark midpoint; the
          scenario generator draws competitor_quote < low < opening, so the
          defender is scored as a loss even after winning a real concession.
  BUG-51  MatchResult.deal_closed is `not red_flag_known` — True even when the
          scripted attacker refused every ask and no deal was struck.
  BUG-52  fsm.finish() unconditionally sets phase=WRAP, dropping the early-exit
          guard — a call that never left OPENING now "finishes" with no abort
          signal.
  BUG-53  LiveAttacker.total is read from the LLM reply on success but reset to
          _script.total on failure, so one failed turn after a successful one
          reports a final_total that does not match what the defender just heard.
  BUG-54  arena red_flag_known keys only on deposit_pct/refundable; broker
          concealment / hidden-fee / non-binding red flags (RF-A..RF-E in
          report.py) cannot influence deal_closed at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from negotiator.brain.fsm import NegotiationFSM
from negotiator.call.gate import HonestyGate, _word_money_amounts
from negotiator.core.bus import EventBus
from negotiator.core.contracts import (
    CallCard,
    LedgerFact,
    LedgerFactKind,
    NegotiationPhase,
    Source,
    SourceType,
)
from negotiator.tools.arena import (
    ROOT,
    SEED_GENOME_PATH,
    VERTICAL_PATH,
    LiveAttacker,
    Scenario,
    ScriptedAttacker,
    judge,
    load_genome,
    merge_overlay,
    run_arena,
    run_match,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _quote_fact(total: int, fact_id: str = "competitor_quote") -> LedgerFact:
    return LedgerFact(
        id=fact_id,
        kind=LedgerFactKind.QUOTE,
        value={"total": total},
        source=Source(type=SourceType.CONFIG, ref="test"),
        call_id="test",
        ts=datetime.now(timezone.utc),
    )


def _leverage_card(facts: tuple[str, ...] = ("competitor_quote",)) -> CallCard:
    return CallCard(
        version=1,
        phase=NegotiationPhase.LEVERAGE,
        phase_goal="apply real leverage",
        next_move="leverage",
        allowed_fact_ids=facts,
        tone_preset="firm",
    )


def _scenario(*, concedes: bool = False, concession_usd: int = 0,
              deposit_pct: int = 0, deposit_refundable: bool = True,
              anchor_flex_pct: int = 0, hidden_fee_codes: tuple[int, ...] = (),
              competitor_quote: int = 3000, opening_total: int = 5000) -> Scenario:
    return Scenario(
        match=0, persona="pressure_closer",
        benchmark_low=4000, benchmark_high=4400,
        opening_total=opening_total, competitor_quote=competitor_quote,
        hidden_fee_codes=hidden_fee_codes, concedes=concedes,
        concession_usd=concession_usd, anchor_flex_pct=anchor_flex_pct,
        deposit_pct=deposit_pct, deposit_refundable=deposit_refundable,
    )


class _FakeClient:
    """Minimal stand-in for the OpenAI client surface LiveAttacker uses:
    ``client.chat.completions.create(...)`` → response with .choices[0].message.content."""

    def __init__(self, response_or_exc: object) -> None:
        self._r = response_or_exc
        # Make client.chat.completions all resolve to `self` so .create() lands here.
        self.chat = self
        self.completions = self

    def create(self, **_kwargs):  # noqa: ANN201
        if isinstance(self._r, BaseException):
            raise self._r
        class _Msg: pass
        class _Choice: pass
        class _Resp: pass
        msg = _Msg(); msg.content = self._r
        choice = _Choice(); choice.message = msg
        resp = _Resp(); resp.choices = [choice]
        return resp


def _merged_genome() -> tuple[dict, dict]:
    genome = load_genome(SEED_GENOME_PATH)
    vertical = yaml.safe_load(VERTICAL_PATH.read_text(encoding="utf-8"))
    return genome, merge_overlay(vertical, genome)


# --------------------------------------------------------------------------- #
# BUG-48 — phantom suffix amounts in the word-money parser
# --------------------------------------------------------------------------- #
def test_bug_48_word_money_parser_emits_phantom_suffix_amounts():
    """_word_money_amounts slides a 7-token window over the text and *aggregates
    every* sub-phrase that parses, including bare suffixes like ["thousand"]. The
    phantom amounts then leak into the gate's `unsupported` set, blocking drafts
    that cite a fully-supported total. Regression of the BUG-08 fix."""
    # Parser-level: bare suffixes become standalone phantom amounts.
    assert _word_money_amounts("five thousand dollars") == {5000, 1000}   # 1000 is phantom
    assert _word_money_amounts("twenty five thousand dollars") == {25000, 5000, 1000}
    assert _word_money_amounts("пять тысяч рублей") == {5000, 1000}

    # End-to-end through HonestyGate: a draft that quotes EXACTLY the supported
    # total of $5,000 is refused, because the phantom $1,000 has no backing fact.
    fact = _quote_fact(5000)
    card = _leverage_card()
    gate = HonestyGate(stall_phrases=["hold on"])
    decision = gate.evaluate(
        draft="My quote is five thousand dollars.",
        card=card, ledger_facts=(fact,),
    )
    assert decision.verdict == "block"
    assert decision.reason == "unsupported_quote_amount"
    # Invariant C violation surface: a fully-supported claim was treated as a leak.


# --------------------------------------------------------------------------- #
# BUG-49 — genome.talker_prompt is dead code
# --------------------------------------------------------------------------- #
def test_bug_49_genome_talker_prompt_has_no_effect_on_defender_drafts():
    """genome.talker_prompt is in _GENOME_KEYS, validated, persisted into the
    overlay, and mutated by the coach across generations — but run_match builds
    the Talker as OfflineTalkerAdapter() with no arguments and the prompt is
    never read. Two genomes differing only in talker_prompt produce identical
    defender drafts, so the field is a dead steering wheel."""
    genome_a, _ = _merged_genome()
    genome_b = {**genome_a, "talker_prompt": "BE LOUD. " * 50 + "DIFFERENT."}
    # Both genomes pass validation (the field is checked) — proving the dead
    # surface is real, not silently dropped on input.
    from negotiator.tools.arena import validate_genome
    validate_genome(genome_a)
    validate_genome(genome_b)

    vertical = yaml.safe_load(VERTICAL_PATH.read_text(encoding="utf-8"))
    scenario = _scenario(concedes=True, concession_usd=400)
    match_a = run_match(scenario=scenario, genome=genome_a,
                        merged=merge_overlay(vertical, genome_a),
                        attacker=ScriptedAttacker(scenario), bus=EventBus(), call_id="a")
    match_b = run_match(scenario=scenario, genome=genome_b,
                        merged=merge_overlay(vertical, genome_b),
                        attacker=ScriptedAttacker(scenario), bus=EventBus(), call_id="b")
    drafts_a = [turn.defender for turn in match_a.turns]
    drafts_b = [turn.defender for turn in match_b.turns]
    assert drafts_a == drafts_b  # talker_prompt changed nothing


# --------------------------------------------------------------------------- #
# BUG-50 — judge() scores a real concession as a defender loss
# --------------------------------------------------------------------------- #
def test_bug_50_judge_marks_a_real_concession_as_a_loss(tmp_path):
    """judge() computes money = midpoint - effective, and draw_scenarios emits
    competitor_quote < low and an opening above the benchmark midpoint. The
    scripted concession (150–600 USD) is never enough to cross below midpoint,
    so every conceding match is scored as an attacker win. The arena cannot
    reward the defender for actually moving the price."""
    run = run_arena(mode="principled", loops=5, seed=7, out_root=tmp_path)
    conceded_but_lost = [
        card for card in run.scorecards
        if card["concession"] > 0 and card["winner"] != "defender"
    ]
    assert conceded_but_lost, (
        "expected at least one match where the defender extracted a real concession "
        "but judge() still scored it as a loss/draw"
    )


# --------------------------------------------------------------------------- #
# BUG-51 — deal_closed ignores whether the attacker actually agreed
# --------------------------------------------------------------------------- #
def test_bug_51_deal_closed_true_when_attacker_never_agreed():
    """MatchResult.deal_closed = `not red_flag_known`, computed only from the
    deposit trap. With no deposit trap the match is recorded as closed even when
    the scripted attacker refused every ask, conceded nothing, and the price
    never moved — judge() then treats the un-moved opening as the deal price."""
    scenario = _scenario(concedes=False, concession_usd=0, anchor_flex_pct=0,
                         deposit_pct=0, deposit_refundable=True)
    genome, merged = _merged_genome()
    match = run_match(scenario=scenario, genome=genome, merged=merged,
                      attacker=ScriptedAttacker(scenario), bus=EventBus(), call_id="t")
    # No movement happened: the attacker held firm at every phase.
    assert match.scenario.concedes is False
    assert match.final_total == scenario.opening_total
    # Yet the match is recorded as a closed deal at the un-moved opening price.
    assert match.deal_closed is True


# --------------------------------------------------------------------------- #
# BUG-52 — fsm.finish() silently accepts any phase
# --------------------------------------------------------------------------- #
def test_bug_52_fsm_finish_silently_exits_from_opening():
    """finish() now unconditionally sets phase=WRAP. The early-exit guard that
    used to raise ForbiddenTransition is gone, and no abort flag is recorded —
    a call that never left OPENING can be 'finished' with no signal that it
    exited abnormally."""
    fsm = NegotiationFSM()  # starts at OPENING
    fsm.finish()            # used to raise ForbiddenTransition; now silently OK
    assert fsm.phase is NegotiationPhase.WRAP
    # No aborted/abnormal flag exists on the FSM to distinguish this from a call
    # that legitimately reached WRAP via the full phase ladder.


# --------------------------------------------------------------------------- #
# BUG-53 — LiveAttacker.total desyncs from dialogue after an LLM failure
# --------------------------------------------------------------------------- #
def test_bug_53_live_attacker_total_desyncs_after_llm_failure():
    """On LLM success, LiveAttacker.total is parsed from the reply; on any
    failure it is reset to _script.total. The script never observed the LLM's
    prior number, so one failed turn after a successful one snaps `total` back
    to the scripted opening — the judge then scores against a price the defender
    never heard in the dialogue."""
    scenario = _scenario(concedes=False, anchor_flex_pct=5)

    # Turn 1: LLM blurts "$4,200" — below the scripted opening of 5000.
    attacker = LiveAttacker(scenario, {"prompt": "x"}, client=_FakeClient("$4,200 is my best."))
    first = attacker.respond(NegotiationPhase.LEVERAGE, role="cite",
                             defender_line="I have a written quote for 3000.")
    assert "4,200" in first
    assert attacker.total == 4200

    # Turn 2: LLM fails. Fallback text + total come from the script, whose total
    # is still the opening — the script never saw the LLM's 4200.
    attacker._client = _FakeClient(RuntimeError("network down"))
    attacker.respond(NegotiationPhase.COMMIT, role="anchor",
                     defender_line="let's land at 4200 today.", anchor=4200)
    # The defender heard 4200 one turn ago, but the judge will be told a
    # different final_total derived from the script.
    assert attacker.total != 4200


# --------------------------------------------------------------------------- #
# BUG-54 — arena red_flag_known only sees the deposit trap
# --------------------------------------------------------------------------- #
def test_bug_54_arena_red_flag_cannot_capture_broker_concealment():
    """Scenario has no field for broker-concealment, hidden-fee disclosure, or
    non-binding estimate type — yet report.py raises RF-A..RF-E for exactly those.
    red_flag_known (the only signal that flips deal_closed) keys exclusively on
    deposit_pct >= 30 + non-refundable, so a carrier that would draw multiple
    red flags in production still records deal_closed=True when no deposit trap
    is present. The defender cannot be rewarded for surfacing them."""
    scenario_fields = set(Scenario.__dataclass_fields__)
    for missing in ("broker_disclosed", "conceals_carrier",
                    "estimate_type", "usdot", "mc"):
        assert missing not in scenario_fields, (
            f"Scenario gained a {missing!r} field — revisit whether red_flag_known "
            "now covers more than the deposit trap"
        )

    # lowball_broker persona conceals carrier status and uses a 35% non-refundable
    # deposit in production (RF-B + RF-C), but here we set deposit_pct=0 → the
    # only red_flag the arena can express is absent → deal_closed=True.
    scenario = _scenario(concedes=False, deposit_pct=0, deposit_refundable=True,
                         competitor_quote=2200, opening_total=2600)
    genome, merged = _merged_genome()
    match = run_match(scenario=scenario, genome=genome, merged=merged,
                      attacker=ScriptedAttacker(scenario), bus=EventBus(), call_id="t")
    assert match.deal_closed is True  # would be RF-C/RF-D in the real report path
