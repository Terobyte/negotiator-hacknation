"""Pins for bugs 45-46: --live silently ran the scripted fallback while the CLI claimed
"live sol coach" (found 2026-07-19 by diffing a --live journal against an offline baseline —
wall clock 1s, byte-identical events). The fix makes engagement observable: LiveAttacker
counts LLM turns, coach_live returns (genome, engaged), render prints honest labels."""

from __future__ import annotations

from types import SimpleNamespace

from negotiator.core.contracts import NegotiationPhase
from negotiator.tools import arena
from negotiator.tools.arena import (
    LiveAttacker,
    ScriptedAttacker,
    Scenario,
    coach_live,
    coach_offline,
    judge,
    load_genome,
    run_arena,
    SEED_GENOME_PATH,
)


def _scenario(**overrides):
    base = dict(match=0, persona="pressure_closer", benchmark_low=4000, benchmark_high=6000,
                opening_total=5600, competitor_quote=3400, hidden_fee_codes=(4,),
                concedes=True, concession_usd=400, anchor_flex_pct=3,
                deposit_pct=0, deposit_refundable=True)
    base.update(overrides)
    return Scenario(**base)


def _client(reply):
    def create(**kwargs):
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _cards():
    from negotiator.tools.arena import MatchResult
    match = MatchResult(scenario=_scenario(), turns=(), opening_total=5600, final_total=5200,
                        deal_closed=True, fees_surfaced=0, gate_blocks=0, leaks=0, spoken_unapproved=0)
    return [judge(match, mode="cash")]


# --------------------------------------------------------------------------- #
# Bug 45 — the attacker's LLM engagement must be observable, not inferred
# --------------------------------------------------------------------------- #
def test_45_live_attacker_counts_genuine_llm_turns():
    attacker = LiveAttacker(_scenario(), {"prompt": "x"}, client=_client("We are at $5,600, but for you $5,400."))
    text = attacker.respond(NegotiationPhase.OPENING, role=None, defender_line="hello")
    assert "for you" in text
    assert attacker.engaged == 1
    assert attacker.total == 5400  # priced from its own last $-amount


def test_45_fallback_keeps_engaged_at_zero_and_speaks_the_script():
    attacker = LiveAttacker(_scenario(), {"prompt": "x"}, client=_client(RuntimeError("api down")))
    text = attacker.respond(NegotiationPhase.OPENING, role=None, defender_line="hello")
    assert text == ScriptedAttacker(_scenario()).respond(NegotiationPhase.OPENING, role=None, defender_line="hello")
    assert attacker.engaged == 0


# --------------------------------------------------------------------------- #
# Bug 46 — coach_live must confess a fallback; render must not mislabel it
# --------------------------------------------------------------------------- #
def test_46_coach_live_reports_engagement_and_fallback():
    genome = load_genome(SEED_GENOME_PATH)
    import yaml as _yaml
    live_genome, engaged = coach_live(genome, _cards(), mode="cash",
                                      client=_client(_yaml.safe_dump(dict(genome), sort_keys=False)))
    assert engaged is True and live_genome["generation"] == 1
    fell_back, engaged = coach_live(genome, _cards(), mode="cash", client=_client(RuntimeError("boom")))
    assert engaged is False
    assert fell_back == coach_offline(genome, _cards())


def test_46_offline_run_records_zero_live_engagement(tmp_path):
    run = run_arena(mode="cash", loops=1, seed=3, out_root=tmp_path, run_id="off")
    assert run.live_attacker_turns == 0 and run.live_coach is False


def test_46_render_confesses_a_scripted_fallback_under_live(tmp_path, capsys, monkeypatch):
    class _Stub(ScriptedAttacker):
        engaged = 0

    monkeypatch.setattr(arena, "LiveAttacker", lambda scenario, persona, model: _Stub(scenario))
    run = run_arena(mode="cash", loops=1, seed=3, out_root=tmp_path, run_id="live",
                    live=True, coach_client=_client(RuntimeError("no coach")))
    arena.render(run)
    out = capsys.readouterr().out
    assert "live engagement: attacker 0/" in out
    assert "SCRIPTED FALLBACK" in out
    assert "live coach fell back" in out
    assert "live sol coach" not in out
