# The Negotiator — project memory

Evidence-first **voice negotiation runtime**. A voice AI agent that negotiates fair prices on
high-variance services (default vertical: long-distance moving). Closed loop: **intake → call the
market → negotiate on real leverage → ranked report that cites the transcripts.** Built for
Hack-Nation 6, Challenge 1 (ElevenLabs). Python 3.11+ package `negotiator` + a Next.js war-room dashboard.

The frame is **principled negotiation**: leverage from real information the agent actually gathered,
never fabricated. That honesty guarantee is enforced deterministically in code, not by prompt.

## Source of truth

- **`docs/spec.md`** is canon — the module map, contracts (§2), per-module invariants (§3), the debug
  matrix (§5), and **§10 the decision journal** (every design choice + its reversal history). Read it
  before any non-trivial change. Other `docs/*.md` are narrative/research lenses.
- ⚠️ `docs/spec.md` writes replay commands in shorthand (`python -m negotiator.gate`). The **real**
  module paths are nested (`python -m negotiator.call.gate`). Use the table below, not the spec's shorthand.
- `bugs.md` is a bug-hunt report; `tests/test_bug_regressions_*.py` are its regression pins.

## Commands

```bash
# Setup (venv already present + editable-installed as `negotiator`)
source .venv/bin/activate
pip install -e ".[dev,openai,voice,webhook]"    # extras: openai, voice(websockets), webhook(fastapi)

# Tests — 191 tests, ~0.7s, FULLY OFFLINE (no network, no live vendors). Run this constantly.
pytest                                           # pyproject sets pythonpath=["."], testpaths=["tests"], -q
pytest tests/test_gate.py -k leak                 # single file / filter

# Whole product loop, offline (intake→3 sim calls→report), one deterministic assertion:
python app.py --smoke                             # prints {"ok": true, "mode": "sim", "winner": ...}

# Dashboard (Next.js 16 on Cloudflare/vinext war-room)
cd negotiator/dashboard && npm run dev            # also: build | start | test | lint  (node >=22.13)
```

## Per-module debug CLIs (the core workflow)

**Every decision module runs alone, offline, from a journal slice — no pipeline, no live audio.**
"Doesn't run alone → not finished." Fixtures live in `negotiator/fixtures/`.

| Module | Command |
|---|---|
| `gate` (honesty) | `python -m negotiator.call.gate --replay negotiator/fixtures/bluff_corpus.jsonl` (also `leak_corpus.jsonl`) |
| `firewall` | `python -m negotiator.call.firewall --replay negotiator/fixtures/injection_corpus.jsonl` |
| `arbiter` (VAD/barge-in) | `python -m negotiator.call.arbiter --replay negotiator/fixtures/vad_smoke.jsonl` |
| `talker` | `python -m negotiator.call.talker --card negotiator/fixtures/card_leverage.json --transcript negotiator/fixtures/tail.txt` |
| `fsm` | `python -m negotiator.brain.fsm --replay negotiator/fixtures/fsm_smoke.jsonl` |
| `opponent` (pure math) | `python -m negotiator.brain.opponent --prices 5200,4900,4750,4700` |
| `strategist` | `python -m negotiator.brain.strategist --replay negotiator/fixtures/strategy_journal_slice.jsonl` |
| `ledger` | `python -m negotiator.brain.ledger list --provenance` (subcommands: `add` / `cite <id>` / `list`) |
| `report` | `python -m negotiator.product.report --outcomes negotiator/fixtures/three_calls.json` |
| `verify` (FMCSA live) | `python -m negotiator.product.verify --dot 123456`  ·  `--mc 654321` (MC uses `docket-number`) |
| `estimator` | `python -m negotiator.product.estimator --doc negotiator/fixtures/old_quote.pdf` · `--webhook <json>` |
| `discovery` (Google Places) | `python -m negotiator.product.discovery --city "Boston, MA"` (needs Places key) |
| tool: `slice` | `python -m negotiator.tools.slice <journal.jsonl> --call-id C --module gate --kind draft` → 1-module fixture |
| tool: `latency_report` | `python -m negotiator.tools.latency_report <journal.jsonl> [--enforce]` (mouth-to-ear breakdown) |
| transports | `python -m negotiator.call.transport.el_ws --smoke` · `...transport.twilio --smoke` (I/O adapters: smoke only) |

## Architecture — three rings, one connective tissue

```
negotiator/
  core/                 # imported by EVERYONE; imports NO other module ring
    contracts/          #   models.py = pydantic schemas (§2), zero logic. THE only shared types.
    bus.py              #   tiny pub/sub of inter-module EVENTS (not audio frames)
    journal.py          #   append-only JSONL, monotonic seq; subscribed to the whole bus
  call/                 # hot path: transport/(twilio,webrtc,el_ws)  stt firewall arbiter talker gate prosody tts
  brain/                # async, off the latency budget: fsm ledger strategist opponent
  product/              # before/after the call: estimator/ discovery verify market report
  config/verticals/     # moving.yaml (canon) + plumbing.yaml (skeleton — proves "vertical = config, not code")
  fixtures/  tools/  dashboard/
app.py                  # the ONLY wiring/composition point (FastAPI + degradation ladder + orchestrator)
counteragents/          # 3 ElevenLabs persona configs (lowball_broker → rushed_dispatcher → pressure_closer)
```

### Non-negotiable invariants (violating these is a review-stop — verify before editing)

1. **Core-only imports.** Ring modules (`call/`, `brain/`, `product/`) NEVER import each other. They talk
   only through `core/` (`contracts` types + `bus` events + `journal`). If you reach for a cross-ring
   import, add a bus event or a contract field instead.
2. **Journal is a reproducer by construction.** `journal` subscribes to the entire bus, so every
   inter-module message is already on disk. Any bug → `slice` the journal → fixture → single-module replay.
3. **Honesty gate is fail-closed and unbypassable in the type system.** `tts` accepts *only* an
   `ApprovedUtterance`, which can be constructed *only* inside `HonestyGate` (guarded by the private
   `_GATE_CAPABILITY` token in `core/contracts/models.py`). There is no "warn and pass." Do not add an
   alternate constructor, a raw-string TTS path, or a bypass flag — that would make dishonesty *expressible*.
   - Invariant A: a quote-shaped number/claim with no backing ledger fact → **block** + regenerate.
   - Invariant B (leak-guard): private fields (`budget_ceiling`, opponent `floor`, price corridor, system
     prompt) must never appear in a draft, even under injection → **block**.
   - Invariant C: a block is never silence — Talker speaks a stall phrase (from vertical YAML) and regenerates.
4. **Ledger write-authority.** A `LedgerFact` is created only from a tool result, config, or a captured
   quote with a transcript span. Free-text from the opponent can NEVER mint a fact (guards the Chevy-`$1` case).
5. **Every decision module answers `--replay`** (`gate fsm talker strategist ledger opponent estimator
   report firewall`). I/O adapters (`transport stt tts dashboard`) get smoke tests only.
6. **`CallOutcome` always exists** — even on hangup/crash. Guaranteed two ways: fsm raises on illegal
   phase transitions, and `market.supervise_call_async` uses try/finally to rebuild the outcome from the
   journal tail. Don't add an early return that skips outcome publication.
7. **Vertical = config, not code.** Behavior (taxonomy, benchmarks, 14 fee codes, red-flag rules, Voss
   phrase library, stall phrases, `demo_number_map`) lives in `config/verticals/*.yaml`. Never hardcode
   moving-specific values in a module.

### Two latency budgets (don't conflate)

- **sim / WebRTC leg:** target ≤800ms (≈700ms is an *optimistic* target, not a gate; typically 900ms–1.8s).
- **live Twilio leg:** +150–400ms PSTN → mouth-to-ear ≈1.1s. Never promise sub-500ms live.

## Debug matrix (symptom → module → offline repro)

| Symptom on a run | Module | Repro without the pipeline |
|---|---|---|
| Said a number not in the ledger | `gate` | `gate --replay bluff_corpus.jsonl` — must block |
| Leaked corridor/floor under injection | `gate` | `gate --replay leak_corpus.jsonl` — must block (inv. B) |
| Floor estimate jumps / nonsense | `opponent` | `opponent --prices …` (pure fn) |
| Phase skipped illegally | `fsm` | exception stacktrace + table test |
| Call with no `CallOutcome` | `market` | supervisor unit: kill mid-call → outcome from journal tail |
| Reply out of sync with a live card | `talker` | `--card --transcript` fixture |
| Card didn't update on a new quote | `strategist` | journal slice → diff of cards |
| "Believed" the opponent's words | `ledger` | write-authority unit test |
| Silent >800ms (sim) / >1.1s (live) | — | `tools/latency_report` → the guilty hop (usually VAD or LLM TTFT) |

## Configuration & environment

- Runtime toggles come from the vertical YAML `runtime:` block (`live_enabled`, `sim_enabled`,
  `tts_cache_enabled`, `recording_fallback_enabled`) → the `DegradationRouter` ladder in `app.py`
  (order = what fails first: live → sim → cache → recording). Default posture is **offline/sim**.
- Live Twilio requires: `DASHBOARD_BEARER_TOKEN`, `TWILIO_AUTH_TOKEN`, and a canonical **HTTPS**
  `PUBLIC_BASE_URL`; `create_api` refuses to start live without them.
- `.env` keys (values gitignored — never commit or echo them): `ELEVENLABS_API_KEY/MODEL/VOICE_ID`,
  `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `OPENROUTER_TALKER_FALLBACK`, `TALKER_MODEL/PROVIDER`,
  `STRATEGIST_MODEL/PROVIDER/REASONING_EFFORT`, `TWILIO_ACCOUNT_SID/AUTH_TOKEN/PHONE_NUMBER`,
  `TAVILY_API_KEY`. Env overrides: `NEGOTIATOR_VERTICAL`, `NEGOTIATOR_LIVE_ENABLED`, `DASHBOARD_ALLOWED_ORIGINS`.
- Model choices (locked by `bench/`, see spec §10 p.18–19): **Talker** = `gpt-4.1-mini` (OpenAI direct,
  low TTFT), fallback `gemini-2.5-flash-lite`; **Strategist** = `gpt-5.6-sol` (OpenAI direct,
  `reasoning_effort=medium`, off the latency budget → use `max_completion_tokens`, not `max_tokens`/`temperature`).

## Conventions

- Add a bug-regression test to `tests/test_bug_regressions_*.py` when fixing a bug; keep the whole suite
  offline and fast (no live vendor calls in tests — mock or use fixtures).
- Modules are written in a dense, one-statement-per-line style (see `app.py`, `gate.py`); match it.
- Commit style follows the global rule in `~/.claude/CLAUDE.md` (lowercase, no prefixes, no AI attribution).
