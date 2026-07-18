# negotiator-hacknation

**The Negotiator** — a voice AI agent that negotiates fair prices on high-variance services on your behalf.

Built for **Hack-Nation 6** · Challenge 1 (The Negotiator, sponsored by ElevenLabs).

## What it does

A closed loop, end to end:

1. **Intake** — a voice interview (plus document parsing) builds one structured, user-confirmed job spec.
2. **Call** — the agent calls the market against several providers and captures each itemized quote in a comparable, structured form.
3. **Close** — it negotiates using only real leverage (competing quotes and market benchmarks it actually gathered — never fabricated), then returns a ranked report that cites the call transcripts.

The frame is **principled negotiation**: real information as leverage, not manipulation.

## Stack

A realtime voice pipeline built on [pipecat](https://github.com/pipecat-ai/pipecat) — low-latency, interruptible TTS via **ElevenLabs**, streaming STT, and mid-call tool-calling behind a fail-closed honesty gate.

## Design docs

- [`docs/narrative.md`](docs/narrative.md) — the project as one organism: the unifying frame and the pitch
- [`docs/call-architecture.md`](docs/call-architecture.md) — dual-loop brain, negotiation FSM, honesty gate
- [`docs/spec.md`](docs/spec.md) — module map: contracts, invariants, the 21h build plan
- [`docs/inherit-vs-build.md`](docs/inherit-vs-build.md) — what we reuse vs build (main working doc)
- [`docs/research.md`](docs/research.md) — research → decisions (evidence · confidence · sources)
- [`docs/bio-metaphors.md`](docs/bio-metaphors.md) · [`docs/neuro-architecture.md`](docs/neuro-architecture.md) — architecture lenses
