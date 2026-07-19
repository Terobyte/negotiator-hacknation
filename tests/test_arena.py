"""Self-play arena coverage: scenario determinism, judge math, coach, journal. Offline only."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from negotiator.call.talker import OfflineTalkerAdapter
from negotiator.core.contracts import CallCard, NegotiationPhase, SourceType
from negotiator.tools.arena import (
    CLEAN_FLEX_MAX_PCT,
    DIRTY_FLEX_MIN_PCT,
    DISHONESTY_SCORE,
    FEES_SURFACED_BONUS,
    SEED_GENOME_PATH,
    VERTICAL_PATH,
    GenomeTalkerAdapter,
    MatchResult,
    Scenario,
    ScriptedAttacker,
    _ledger,
    coach_offline,
    count_fees_surfaced,
    draw_scenarios,
    judge,
    load_genome,
    main,
    merge_overlay,
    render,
    run_arena,
    train,
    validate_genome,
)


def _scenario(**overrides):
    base = dict(match=0, persona="pressure_closer", benchmark_low=4000, benchmark_high=6000,
                opening_total=5600, competitor_quote=3400, hidden_fee_codes=(4, 7),
                concedes=True, concession_usd=400, anchor_flex_pct=3,
                deposit_pct=0, deposit_refundable=True)
    base.update(overrides)
    return Scenario(**base)


def _match(**overrides):
    base = dict(scenario=_scenario(), turns=(), opening_total=5600, final_total=5200,
                deal_closed=True, fees_surfaced=0, gate_blocks=0, leaks=0, spoken_unapproved=0)
    base.update(overrides)
    return MatchResult(**base)


# --------------------------------------------------------------------------- #
# Scenario stream — a pure function of (seed, match index)
# --------------------------------------------------------------------------- #
def test_scenario_stream_is_pure_function_of_seed():
    assert draw_scenarios(seed=7, loops=6) == draw_scenarios(seed=7, loops=6)
    assert draw_scenarios(seed=7, loops=6) != draw_scenarios(seed=8, loops=6)


def test_scenario_stream_prefix_is_stable_in_loop_count():
    # Per-index reseeding: benching 3 loops then 6 loops replays the same first 3 scenarios.
    assert draw_scenarios(seed=7, loops=6)[:3] == draw_scenarios(seed=7, loops=3)


# --------------------------------------------------------------------------- #
# Judge math
# --------------------------------------------------------------------------- #
def test_cash_winner_by_final_price_vs_benchmark_midpoint():
    # midpoint (4000+6000)//2 = 5000; deal at 4600 -> +400 defender; at 5400 -> -400 attacker.
    won = judge(_match(final_total=4600), mode="cash")
    assert (won["winner"], won["score"]) == ("defender", 400)
    lost = judge(_match(final_total=5400), mode="cash")
    assert (lost["winner"], lost["score"]) == ("attacker", -400)
    drawn = judge(_match(final_total=5000), mode="cash")
    assert (drawn["winner"], drawn["score"]) == ("draw", 0)


def test_no_deal_is_scored_against_the_documented_reserve():
    # Reserve = the client's BATNA = scenario.competitor_quote (3400): score = 5000 - 3400.
    card = judge(_match(deal_closed=False, final_total=5600), mode="cash")
    assert card["score"] == 1600
    assert card["deal_closed"] is False
    assert card["winner"] == "defender"


def test_principled_adds_fees_surfaced_bonus():
    card = judge(_match(final_total=4600, fees_surfaced=2), mode="principled")
    assert card["score"] == 400 + 2 * FEES_SURFACED_BONUS
    cash = judge(_match(final_total=4600, fees_surfaced=2), mode="cash")
    assert cash["score"] == 400  # cash mode is money only


def test_principled_honesty_veto_is_an_instant_attacker_win():
    card = judge(_match(spoken_unapproved=1, final_total=3000), mode="principled")
    assert card["winner"] == "attacker"
    assert card["score"] == DISHONESTY_SCORE


def test_judge_rejects_unknown_mode():
    with pytest.raises(ValueError):
        judge(_match(), mode="vibes")


# --------------------------------------------------------------------------- #
# Genome — validation, overlay, coach
# --------------------------------------------------------------------------- #
def test_seed_genome_loads_and_validates():
    genome = load_genome(SEED_GENOME_PATH)
    assert genome["generation"] == 0
    assert genome["tactics"]["cite_competitor_at_phase"] == "LEVERAGE"


def test_genome_validation_rejects_bad_shapes():
    genome = load_genome(SEED_GENOME_PATH)
    for mutation in (
        {"extra_key": 1},
        {"tactics": {**genome["tactics"], "cite_competitor_at_phase": "OPENING"}},
        {"tactics": {**genome["tactics"], "anchor_discount_pct": 55}},
        {"voss_phrases": []},
    ):
        broken = {**genome, **mutation}
        with pytest.raises(ValueError):
            validate_genome(broken)


def test_merge_overlay_never_mutates_the_vertical_dict():
    import yaml
    vertical = yaml.safe_load(VERTICAL_PATH.read_text(encoding="utf-8"))
    snapshot = json.dumps(vertical, sort_keys=True, default=str)
    genome = load_genome(SEED_GENOME_PATH)
    merged = merge_overlay(vertical, genome)
    assert merged["voss"]["stalls"] == genome["stall_phrases"]
    assert merged["voss"]["labels"] == genome["voss_phrases"]
    assert json.dumps(vertical, sort_keys=True, default=str) == snapshot


def test_offline_coach_yields_a_valid_next_generation():
    genome = load_genome(SEED_GENOME_PATH)
    cards = [judge(_match(final_total=5400), mode="cash"),
             judge(_match(scenario=_scenario(match=1), final_total=4600), mode="cash")]
    nxt = validate_genome(coach_offline(genome, cards))
    assert nxt["generation"] == genome["generation"] + 1
    assert len(nxt["voss_phrases"]) >= 2


# --------------------------------------------------------------------------- #
# Full offline runs — gate path, journal, determinism, canon untouched
# --------------------------------------------------------------------------- #
def test_offline_run_end_to_end(tmp_path):
    canon_before = VERTICAL_PATH.read_bytes()
    genome_before = SEED_GENOME_PATH.read_bytes()
    run = run_arena(mode="principled", loops=3, seed=7, out_root=tmp_path, run_id="e2e")
    assert len(run.scorecards) == 3
    assert all(match.spoken_unapproved == 0 for match in run.matches)
    assert all(turn.verdict == "allow" for match in run.matches for turn in match.turns)
    assert [card["scenario"] for card in run.scorecards] == [s.as_dict() for s in draw_scenarios(seed=7, loops=3)]
    assert run.genome_diff.strip()
    assert run.next_genome["generation"] == 1
    assert run.next_genome_path.is_file()
    assert VERTICAL_PATH.read_bytes() == canon_before
    assert SEED_GENOME_PATH.read_bytes() == genome_before


def test_journal_records_every_arena_event_kind(tmp_path):
    run = run_arena(mode="cash", loops=2, seed=3, out_root=tmp_path, run_id="journal")
    lines = [json.loads(line) for line in (run.run_dir / "journal.jsonl").read_text().splitlines() if line.strip()]
    kinds = {line["kind"] for line in lines}
    assert {"arena.scenario", "talker.draft", "gate.decision", "defender.utterance",
            "attacker.line", "arena.scorecard", "arena.genome", "arena.summary"} <= kinds
    seqs = [line["seq"] for line in lines]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


def test_same_seed_reproduces_identical_scorecards(tmp_path):
    first = run_arena(mode="cash", loops=3, seed=7, out_root=tmp_path / "a", run_id="r")
    second = run_arena(mode="cash", loops=3, seed=7, out_root=tmp_path / "b", run_id="r")
    assert first.scorecards == second.scorecards
    assert first.genome_diff == second.genome_diff


def test_ledger_facts_come_only_from_config_never_attacker_text():
    facts = _ledger(_scenario(), anchor=3200, call_id="c")
    assert {fact.id for fact in facts} == {"competitor_quote", "anchor_target"}
    assert all(fact.source.type is SourceType.CONFIG for fact in facts)


def test_cli_smoke_prints_table_aggregate_and_diff(tmp_path, capsys):
    assert main(["--mode", "cash", "--loops", "2", "--seed", "3", "--out-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "aggregate: defender" in out
    assert "genome_gen000" in out and "genome_gen001" in out
    assert "--- genome_gen000.yaml" in out  # the unified diff header


# --------------------------------------------------------------------------- #
# Attacker profiles — clean vs. dirty vs. turncoat (loop 2)
# --------------------------------------------------------------------------- #
def test_clean_vs_dirty_diverge_on_cite_at_leverage():
    scenario = _scenario(competitor_quote=3400, concedes=True, concession_usd=400,
                          hidden_fee_codes=(4, 7), anchor_flex_pct=5)
    clean = ScriptedAttacker(scenario, profile="clean")
    dirty = ScriptedAttacker(scenario, profile="dirty")
    clean_line = clean.respond(NegotiationPhase.LEVERAGE, role="cite", defender_line="")
    dirty_line = dirty.respond(NegotiationPhase.LEVERAGE, role="cite", defender_line="")
    assert clean.total == scenario.opening_total - scenario.concession_usd  # clean concedes
    assert "come down" in clean_line
    assert dirty.total == scenario.opening_total  # dirty does not concede
    assert "fabricated" in dirty_line


def test_dirty_cite_at_pressure_test_still_concedes():
    # dirty's dismissal is LEVERAGE-only; cited early (PRESSURE_TEST) it concedes like clean.
    scenario = _scenario(competitor_quote=3400, concedes=True, concession_usd=400)
    dirty = ScriptedAttacker(scenario, profile="dirty")
    line = dirty.respond(NegotiationPhase.PRESSURE_TEST, role="cite", defender_line="")
    assert dirty.total == scenario.opening_total - scenario.concession_usd
    assert "come down" in line


def test_reveal_based_fees_dirty_conceals_clean_discloses():
    scenario = _scenario(hidden_fee_codes=(4, 7))
    clean = ScriptedAttacker(scenario, profile="clean")
    dirty = ScriptedAttacker(scenario, profile="dirty")
    clean_line = clean.respond(NegotiationPhase.PRESSURE_TEST, role="press", defender_line="")
    dirty_line = dirty.respond(NegotiationPhase.PRESSURE_TEST, role="press", defender_line="")
    assert count_fees_surfaced(clean_line, scenario.hidden_fee_codes) == len(scenario.hidden_fee_codes)
    assert count_fees_surfaced(dirty_line, scenario.hidden_fee_codes) == 0  # not caught yet -> denies


def test_dirty_press_discloses_truthfully_once_caught():
    scenario = _scenario(hidden_fee_codes=(4, 7), concedes=True, concession_usd=400)
    dirty = ScriptedAttacker(scenario, profile="dirty")
    dirty.respond(NegotiationPhase.PRESSURE_TEST, role="cite", defender_line="")  # concedes -> caught
    press_line = dirty.respond(NegotiationPhase.LEVERAGE, role="press", defender_line="")
    assert count_fees_surfaced(press_line, scenario.hidden_fee_codes) == len(scenario.hidden_fee_codes)


def test_commit_flex_windows_are_disjoint_between_clean_and_dirty():
    scenario = _scenario(competitor_quote=3400, anchor_flex_pct=5)
    anchor_5pct = 3230   # implied 5% -- inside clean's window, below dirty's
    anchor_15pct = 2890  # implied 15% -- inside dirty's window, above clean's
    assert (scenario.competitor_quote - anchor_5pct) * 100 // scenario.competitor_quote <= CLEAN_FLEX_MAX_PCT
    assert (scenario.competitor_quote - anchor_15pct) * 100 // scenario.competitor_quote >= DIRTY_FLEX_MIN_PCT
    clean_5 = ScriptedAttacker(scenario, profile="clean")
    dirty_5 = ScriptedAttacker(scenario, profile="dirty")
    clean_5.respond(NegotiationPhase.COMMIT, role="anchor", defender_line="", anchor=anchor_5pct)
    dirty_5.respond(NegotiationPhase.COMMIT, role="anchor", defender_line="", anchor=anchor_5pct)
    assert clean_5.total < scenario.opening_total   # clean moves at a shallow anchor
    assert dirty_5.total == scenario.opening_total  # dirty refuses it as too shallow
    clean_15 = ScriptedAttacker(scenario, profile="clean")
    dirty_15 = ScriptedAttacker(scenario, profile="dirty")
    clean_15.respond(NegotiationPhase.COMMIT, role="anchor", defender_line="", anchor=anchor_15pct)
    dirty_15.respond(NegotiationPhase.COMMIT, role="anchor", defender_line="", anchor=anchor_15pct)
    assert clean_15.total == scenario.opening_total  # clean refuses it as bad-faith lowballing
    assert dirty_15.total < scenario.opening_total   # dirty moves at a deep, "serious" anchor


def test_turncoat_plays_clean_then_dirty_from_leverage_on():
    scenario = _scenario(competitor_quote=3400, concedes=True, concession_usd=400, anchor_flex_pct=5)
    at_pressure_test = ScriptedAttacker(scenario, profile="turncoat")
    line = at_pressure_test.respond(NegotiationPhase.PRESSURE_TEST, role="cite", defender_line="")
    assert at_pressure_test.total == scenario.opening_total - scenario.concession_usd  # clean rules
    assert "come down" in line
    at_leverage = ScriptedAttacker(scenario, profile="turncoat")
    line = at_leverage.respond(NegotiationPhase.LEVERAGE, role="cite", defender_line="")
    assert at_leverage.total == scenario.opening_total  # dirty rules: dismissal, no concession
    assert "fabricated" in line
    commit_shallow = ScriptedAttacker(scenario, profile="turncoat")
    commit_shallow.respond(NegotiationPhase.COMMIT, role="anchor", defender_line="", anchor=3230)
    assert commit_shallow.total == scenario.opening_total  # COMMIT follows dirty's threshold
    commit_deep = ScriptedAttacker(scenario, profile="turncoat")
    commit_deep.respond(NegotiationPhase.COMMIT, role="anchor", defender_line="", anchor=2890)
    assert commit_deep.total < scenario.opening_total


def test_scripted_attacker_rejects_unknown_profile():
    with pytest.raises(ValueError):
        ScriptedAttacker(_scenario(), profile="vibes")


# --------------------------------------------------------------------------- #
# Profile plumbing through run_arena — mixed determinism, scenario purity
# --------------------------------------------------------------------------- #
def test_mixed_profile_draw_is_deterministic(tmp_path):
    first = run_arena(mode="cash", loops=5, seed=11, attacker_profile="mixed",
                      out_root=tmp_path / "a", run_id="mix")
    second = run_arena(mode="cash", loops=5, seed=11, attacker_profile="mixed",
                       out_root=tmp_path / "b", run_id="mix")
    assert [c["profile"] for c in first.scorecards] == [c["profile"] for c in second.scorecards]
    assert any(c["profile"] == "dirty" for c in first.scorecards)  # not a degenerate all-clean draw


def test_mixed_profile_stream_is_a_separate_namespace_from_scenarios(tmp_path):
    # Same seed, same scenarios regardless of attacker_profile -- the profile RNG must never
    # perturb which scenarios draw_scenarios produces.
    clean = run_arena(mode="cash", loops=4, seed=9, attacker_profile="clean",
                      out_root=tmp_path / "clean", run_id="c")
    assert clean.scenarios == draw_scenarios(seed=9, loops=4)
    for profile in ("dirty", "turncoat", "mixed"):
        run = run_arena(mode="cash", loops=4, seed=9, attacker_profile=profile,
                        out_root=tmp_path / profile, run_id=profile)
        assert [c["scenario"] for c in run.scorecards] == [c["scenario"] for c in clean.scorecards]


def test_render_prints_a_profile_column(tmp_path, capsys):
    assert main(["--mode", "principled", "--loops", "2", "--seed", "7",
                "--attacker-profile", "dirty", "--out-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "profile" in out  # header
    assert "dirty" in out    # per-row value


# --------------------------------------------------------------------------- #
# Generations / training
# --------------------------------------------------------------------------- #
def test_train_chains_generations_seeds_and_genomes(tmp_path):
    runs = train(mode="cash", generations=2, loops=2, seed=5, out_root=tmp_path, run_id_prefix="t")
    assert len(runs) == 2
    assert runs[0].seed == 5 and runs[1].seed == 6  # seeds advance by 1 each generation
    assert runs[1].genome == runs[0].next_genome     # runs[1] trained on runs[0]'s output genome
    assert runs[0].genome["generation"] == 0
    assert runs[1].genome["generation"] == 1
    assert runs[1].next_genome["generation"] == 2


def test_cli_generations_smoke_and_cumulative_diff(tmp_path, capsys):
    assert main(["--mode", "cash", "--loops", "2", "--seed", "3", "--generations", "2",
                "--out-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "gen 0:" in out and "gen 1:" in out
    assert "cumulative: genome_gen000" in out
    assert "--- genome_gen000.yaml" in out  # cumulative unified diff header, same style as render()


def test_cli_generations_output_is_byte_identical_across_runs(tmp_path, capsys):
    args = ["--mode", "cash", "--loops", "3", "--seed", "7", "--generations", "2",
            "--out-root", str(tmp_path)]
    assert main(args) == 0
    first = capsys.readouterr().out
    assert main(args) == 0
    second = capsys.readouterr().out
    assert first == second


# --------------------------------------------------------------------------- #
# Live defender — GenomeTalkerAdapter (loop 3). The genome's talker_prompt gene
# only wakes with --defender live; every draft still travels through the real
# HonestyGate, so a hallucinated number becomes a stall, never speech.
# --------------------------------------------------------------------------- #
def _fake_client(reply):
    """SimpleNamespace fake mirroring the coach/attacker fakes: reply is either the
    assistant text to return, or an Exception instance to raise from create()."""
    def create(**kwargs):
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _capturing_client(reply, captured):
    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _card(**overrides):
    base = dict(version=1, phase=NegotiationPhase.OPENING, phase_goal="g", next_move="m",
                allowed_fact_ids=(), tone_preset="warm")
    base.update(overrides)
    return CallCard(**base)


def test_genome_talker_adapter_prompt_carries_the_talker_prompt_gene():
    captured: dict = {}
    client = _capturing_client("Sure, happy to walk through that.", captured)
    adapter = GenomeTalkerAdapter(talker_prompt="OBEY THE GENOME", client=client)
    text = adapter.generate(card=_card(), transcript_tail="hello there")
    prompt = captured["messages"][0]["content"]
    assert "OBEY THE GENOME" in prompt
    assert "CALL CARD" in prompt
    assert adapter.engaged == 1
    assert text == "Sure, happy to walk through that."


def test_genome_talker_adapter_failure_falls_back_to_offline_text():
    adapter = GenomeTalkerAdapter(talker_prompt="OBEY THE GENOME", client=_fake_client(RuntimeError("api down")))
    card = _card()
    text = adapter.generate(card=card, transcript_tail="hello there")
    assert text == OfflineTalkerAdapter().generate(card=card, transcript_tail="hello there")
    assert adapter.engaged == 0


def test_run_arena_defender_live_records_engagement_and_offline_stays_zero(tmp_path):
    client = _fake_client("Understood, let's find something that works for both of us.")
    live = run_arena(mode="cash", loops=2, seed=3, out_root=tmp_path / "live", run_id="live",
                     defender="live", defender_client=client)
    assert live.defender == "live"
    assert live.defender_llm_turns == 2 * 6  # 6 FSM phases per match
    expected_keys = {"mode", "persona", "scenario", "deal_closed", "opening_total", "final_total",
                     "concession", "gate_blocks", "leaks", "fees_surfaced", "winner", "score", "profile"}
    assert set(live.scorecards[0]) == expected_keys
    offline = run_arena(mode="cash", loops=2, seed=3, out_root=tmp_path / "off", run_id="off")
    assert offline.defender == "offline"
    assert offline.defender_llm_turns == 0


def test_defender_live_ungrounded_number_is_gate_blocked_end_to_end(tmp_path):
    # A number with NO backing LedgerFact must be blocked -- the honesty gate holds even
    # when the draft comes from a real (faked) LLM call, not just the offline adapter.
    client = _fake_client("We can settle at $9,999 right now.")
    run = run_arena(mode="principled", loops=1, seed=7, out_root=tmp_path, run_id="blk",
                    defender="live", defender_client=client)
    match = run.matches[0]
    assert match.gate_blocks >= 1
    assert match.spoken_unapproved == 0
    assert all("9,999" not in turn.defender for turn in match.turns)
    assert run.scorecards[0]["mode"] == "principled"  # completed with a valid scorecard, no crash


def test_render_defender_live_dead_client_confesses_offline_fallback(tmp_path, capsys):
    run = run_arena(mode="cash", loops=1, seed=3, out_root=tmp_path, run_id="dead",
                    defender="live", defender_client=_fake_client(RuntimeError("no api")))
    render(run)
    out = capsys.readouterr().out
    assert "defender engagement: 0/" in out
    assert "OFFLINE FALLBACK" in out


def test_render_defender_live_engaged_prints_no_fallback_warning(tmp_path, capsys):
    client = _fake_client("Understood, let's find something that works for both of us.")
    run = run_arena(mode="cash", loops=1, seed=3, out_root=tmp_path, run_id="engaged",
                    defender="live", defender_client=client)
    render(run)
    out = capsys.readouterr().out
    assert "drafts via LLM" in out
    assert "OFFLINE FALLBACK" not in out


def test_run_arena_rejects_unknown_defender():
    with pytest.raises(ValueError):
        run_arena(mode="cash", loops=1, defender="voice")
