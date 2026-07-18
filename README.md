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

---

*Detailed design docs are kept private during the hackathon.*
