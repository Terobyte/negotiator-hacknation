# Talker LLM bench — VERDICT (2026-07-18)

**Question:** who drives the Talker (fast loop, ~700 ms TTFT budget) — Gemini Flash 3.5 or a fast OpenAI model?

**Method:** `bench/llm_bench.py`. TTFT = time to first *content* token (the metric that governs
barge-in), streamed, 5 measured trials/model after a warm-up. Same hard negotiation prompt for all;
two scenarios with a trap. Gemini via OpenRouter (only path in-repo); OpenAI via direct api.openai.com.

## Speed (TTFT)

| Model | Path | TTFT p50 | worst-of-5 | total p50 |
|---|---|---|---|---|
| **gpt-4.1-mini** | openai-direct | **517 ms** | 1585 ms | 1142 ms |
| gpt-4o-mini | openai-direct | 668 ms | 1438 ms | 1100 ms |
| gpt-5.4-nano | openai-direct | 788 ms | 940 ms | 1239 ms |
| gemini-2.5-flash-lite | openrouter | 790 ms | 1049 ms | 962 ms |
| gemini-3.5-flash | openrouter | 1900 ms | 2049 ms | 1995 ms |

## Quality (judged on: no leak · real leverage only · refuse fake deadline · don't take the $2,300 trap · speakable)

- **gpt-4.1-mini — A (winner).** Only model to name and reject the anchor ("$2,300 feels a bit high… can you do better?"). Real leverage in BOTH scenarios, explicit budget refusal, crisp.
- gpt-5.4-nano — A content / verbose. Flips the fake deadline back on the provider; fullest leverage; but turns are long for a single spoken line.
- gpt-4o-mini — A−. Clean, explicit "I can't disclose the budget," but thinner leverage in S2.
- gemini-2.5-flash-lite — B+. Fast and factually clean, but passive: never says "no," no counter-ask.
- gemini-3.5-flash — not scored (warm-up capture truncated); disqualified on latency regardless.

## Decision

**Talker = `gpt-4.1-mini` @ OpenAI direct.** Fixed in `.env` (`TALKER_MODEL` / `TALKER_PROVIDER`),
2026-07-18. Fastest median AND best negotiator.

- Known risk: spiky tail (~1.6 s worst-of-5). Mitigation: inherited arbiter buffering / short filler
  token; firm up with an N=20 rerun if it bites on the live leg.
- Rejected: `gemini-3.5-flash` (~1900 ms TTFT, 2.7× budget). Its sibling `gemini-2.5-flash-lite`
  runs 790 ms on the SAME path → the problem is that model, not "Gemini." flash-lite kept as the
  non-OpenAI fallback (`OPENROUTER_TALKER_FALLBACK`).
- Caveats on record: N=5 → "worst-of-5" is not a true p95; Gemini never got a direct-Google rematch
  (agentx bypasses OpenRouter for latency-critical Gemini). Neither changes the pick.

Strategist (slow loop) is decided separately — candidate `gpt-5.6-sol` or local Claude Code — and did
not race here (latency non-binding there).
