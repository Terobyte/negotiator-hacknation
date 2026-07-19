---
name: arena
description: Self-play arena for the negotiator — seeded attacker vs. the gated defender stack, deterministic judge, coach, genome diff. Use when the user says "прогони N а-лупов", "прогони N в-лупов", "арена", "arena", "self-play", "а-лупы", "в-лупы". а-луп = cash mode, в-луп = principled mode.
---

# Arena — self-play loops

Map the user's request to the CLI:

- **а-луп(ы)** → `--mode cash` (money only: final price vs. benchmark midpoint)
- **в-луп(ы)** → `--mode principled` (honesty veto + money + fees surfaced)
- "N лупов" → `--loops N`. Default seed is 7 unless the user names one.
- **"чистые" / "clean"** → `--attacker-profile clean` (honest bargainer, the default)
- **"грязные лупы" / "dirty"** → `--attacker-profile dirty` (bluffs, fake deadlines, denies fees
  until caught, attempts prompt injection — the gate still holds on the defender side)
- **"turncoat" / "перевёртыш"** → `--attacker-profile turncoat` (clean through PRESSURE_TEST,
  turns dirty from LEVERAGE on)
- **"полигон" / "mixed"** → `--attacker-profile mixed` (deterministic 50/50 clean/dirty draw
  per match, seeded separately from the scenario stream)
- **"обучи N поколений"** → `--generations N` (chains N coach cycles, seed advances each
  generation, prints a cumulative genome diff); add `--live-coach` when the user asks for real
  sol training (real gpt-5.6-sol coach over a scripted deterministic attacker).

Run it (offline by default — zero env keys needed, fully deterministic):

```bash
.venv/bin/python -m negotiator.tools.arena --mode <cash|principled> --loops <N> --seed <S>
```

Then SHOW the user, verbatim from the CLI output:

1. the per-match table (now includes a `profile` column),
2. the aggregate W/L line,
3. the genome unified diff (gen k → gen k+1) — with `--generations N > 1` this is instead
   one compact "gen g: seed S, aggregate ..." line per generation plus a single cumulative
   diff (gen0 → final gen).

Rules:

- Use `--live` (LLM attacker + `gpt-5.6-sol` coach) ONLY when the user explicitly asks for
  live models; never turn it on by default. `--live-coach` alone keeps the attacker scripted
  and deterministic but makes the coach a real `gpt-5.6-sol` call — use it for genuine
  training runs the user wants graded by sol without spending on a live attacker too.
- Never edit `negotiator/config/verticals/*` or `negotiator/config/arena/genome_gen000.yaml`
  as part of a run — the coach writes new genome generations only into
  `runs/arena/<run_id>/` (gitignored).
- Artifacts of every run live in `runs/arena/<run_id>/`: `journal.jsonl` (every attacker
  line, defender draft, gate decision, and scorecard) plus the next genome YAML.
- Same seed ⇒ identical scenario stream regardless of attacker model or `--attacker-profile`
  — use that when the user wants to bench one attacker model/profile against another.
- `--attacker-profile` never touches the defender: the honesty gate is unbypassable
  regardless of how dirty the attacker plays.
