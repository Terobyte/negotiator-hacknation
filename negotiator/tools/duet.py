"""Two-agent call simulation: our gated negotiator vs. a counteragent persona.

Text always; audio on demand (``--voice``). The negotiator side is the real
runtime — ``NegotiationFSM`` phases, ``Talker`` drafts bounded by a ``CallCard``,
and the fail-closed ``HonestyGate`` — so any price it cites must be backed by a
real ``LedgerFact``. The provider side is a deterministic responder scripted from
``counteragents/<slug>.json`` ``behavior`` fields, clearly a simulated counterparty.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from negotiator.brain.fsm import NegotiationFSM
from negotiator.brain.strategist import FEE_NAMES
from negotiator.call.gate import HonestyGate
from negotiator.call.talker import OfflineTalkerAdapter, Talker
from negotiator.call.tts import ElevenLabsTTSConfig, ElevenLabsTTS
from negotiator.core.contracts import (
    ApprovedUtterance,
    CallCard,
    LedgerFact,
    LedgerFactKind,
    NegotiationPhase,
    Source,
    SourceType,
)

ROOT = Path(__file__).resolve().parents[2]
COUNTERAGENTS = ROOT / "counteragents"
STALL_PHRASES = ("One moment while I check my notes.",)

# A premade, public ElevenLabs voice for the provider side (overridable). The
# negotiator side defaults to ELEVENLABS_VOICE_ID from the environment / .env.
DEFAULT_CARRIER_VOICE = "pNInz6obpgDQGcFmaJgB"  # "Adam"
VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75}


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Turn:
    index: int
    phase: NegotiationPhase
    negotiator: str
    gate_verdict: str          # "allow" | "block"
    gate_reason: str
    carrier: str
    carrier_total: int | None


@dataclass(slots=True)
class DuetResult:
    persona: str
    opening_total: int | None
    final_total: int | None
    competitor_quote: int
    turns: list[Turn] = field(default_factory=list)

    @property
    def gate_blocks(self) -> int:
        return sum(1 for t in self.turns if t.gate_verdict == "block")

    @property
    def concession(self) -> int | None:
        if self.opening_total is None or self.final_total is None:
            return None
        return self.opening_total - self.final_total


# --------------------------------------------------------------------------- #
# Negotiator plan — phase, goal, next_move, tone. The offline talker templates
# prepend a fixed phrase per phase; next_move carries the rest, so any money we
# put here (leverage) is checked by the gate against the ledger.
# --------------------------------------------------------------------------- #
def _negotiator_plan(competitor_quote: int, *, bluff: bool) -> list[dict[str, Any]]:
    if bluff:
        # Deliberately cite an amount the ledger does not hold: watch the gate refuse.
        leverage_move = f"I already have a written quote of ${competitor_quote - 1500:,} for the same job."
    else:
        leverage_move = f"I already have a written quote of ${competitor_quote:,} from another carrier for the same job."
    return [
        {"phase": NegotiationPhase.OPENING, "goal": "AI-disclosure + rapport",
         "move": "Disclose that I am an AI assistant, then build rapport.", "tone": "warm", "facts": ()},
        {"phase": NegotiationPhase.DISCOVERY, "goal": "surface the full breakdown",
         "move": "Could you walk me through everything that's included in that number?", "tone": "calm", "facts": ()},
        {"phase": NegotiationPhase.PRESSURE_TEST, "goal": "probe hidden fees",
         "move": "Are there any fees not in that number — stairs, long carry, fuel, or a deposit?", "tone": "calm", "facts": ()},
        {"phase": NegotiationPhase.LEVERAGE, "goal": "apply real leverage",
         "move": leverage_move, "tone": "firm", "facts": ("competitor_quote",)},
        {"phase": NegotiationPhase.COMMIT, "goal": "close today",
         "move": "If we can land there today, I can confirm right now.", "tone": "firm", "facts": ()},
        {"phase": NegotiationPhase.WRAP, "goal": "confirm and thank",
         "move": "Thank you — I'll send the confirmation.", "tone": "warm", "facts": ()},
    ]


# --------------------------------------------------------------------------- #
# Provider persona — a deterministic responder driven by behavior JSON.
# --------------------------------------------------------------------------- #
class Carrier:
    def __init__(self, persona: dict[str, Any], *, benchmark_low: int) -> None:
        self.slug: str = persona["slug"]
        self.role: str = persona.get("role", "provider")
        self.behavior: dict[str, Any] = persona.get("behavior", {})
        self.total = self._opening_total(benchmark_low)

    def _opening_total(self, benchmark_low: int) -> int:
        if "opening_total_usd" in self.behavior:
            return int(self.behavior["opening_total_usd"])
        rel = self.behavior.get("quote_relative_to_benchmark_low")
        if rel is not None:
            return int(round(benchmark_low * float(rel)))
        return benchmark_low

    def respond(self, phase: NegotiationPhase, *, negotiator_cited_quote: bool) -> str:
        b = self.behavior
        if phase is NegotiationPhase.OPENING:
            line = f"Thanks for calling. For that job we're at ${self.total:,} all in."
            if b.get("deadline_claim"):
                line += f" {b['deadline_claim']}"
            return line
        if phase is NegotiationPhase.DISCOVERY:
            codes = b.get("hidden_line_item_codes")
            if codes:
                items = ", ".join(FEE_NAMES.get(int(c), f"code {c}") for c in codes)
                return f"It's bundled, but itemized it's base transport plus {items}."
            if b.get("quote_relative_to_benchmark_low") is not None:
                return "It's a flat sight-unseen rate — I don't break it out line by line."
            return "Sure: it's transport, labor, and materials rolled into that figure."
        if phase is NegotiationPhase.PRESSURE_TEST:
            parts: list[str] = []
            if b.get("deposit_pct"):
                refund = "non-refundable" if b.get("deposit_refundable") is False else "refundable"
                methods = " or ".join(b.get("payment_methods", ["card"]))
                parts.append(f"We do take a {b['deposit_pct']}% {refund} deposit by {methods}.")
            if b.get("conceals_carrier_until_challenged"):
                parts.append("And to be straight with you, we're a broker — the carrier gets assigned later.")
            if not parts:
                parts.append("No hidden fees — that's the complete number.")
            return " ".join(parts)
        if phase is NegotiationPhase.LEVERAGE:
            if negotiator_cited_quote and b.get("concedes_after_cited_competitor_quote"):
                self.total -= int(b.get("concession_usd", 0))
                return f"I can't fully match that, but I'll come down to ${self.total:,}."
            return f"I hear you, but ${self.total:,} is already sharp — I can't go lower."
        if phase is NegotiationPhase.COMMIT:
            return f"Alright — ${self.total:,} it is. Want me to send the paperwork?"
        return "Great, I'll email the confirmation. Thanks for your time."


# --------------------------------------------------------------------------- #
# Core simulation — pure, no network, no audio. Tests call this directly.
# --------------------------------------------------------------------------- #
def run_duet(
    *,
    persona: str = "pressure_closer",
    competitor_quote: int = 3000,
    benchmark_low: int = 4000,
    bluff: bool = False,
) -> DuetResult:
    persona_data = _load_persona(persona)
    carrier = Carrier(persona_data, benchmark_low=benchmark_low)
    result = DuetResult(persona=carrier.slug, opening_total=carrier.total,
                        final_total=None, competitor_quote=competitor_quote)

    fsm = NegotiationFSM()
    talker = Talker(adapter=OfflineTalkerAdapter())
    gate = HonestyGate(stall_phrases=STALL_PHRASES)
    ledger = (_competitor_quote_fact(competitor_quote),)

    tail = ""
    for i, step in enumerate(_negotiator_plan(competitor_quote, bluff=bluff), start=1):
        phase: NegotiationPhase = step["phase"]
        if phase is not fsm.phase:
            fsm.transition(phase, full_estimate=True)

        card = CallCard(version=i, phase=phase, phase_goal=step["goal"],
                        next_move=step["move"], allowed_fact_ids=step["facts"], tone_preset=step["tone"])
        draft = talker.draft(card=card, transcript_tail=tail).text
        decision = gate.evaluate(draft=draft, card=card, ledger_facts=ledger)
        approved: ApprovedUtterance = decision.approved if decision.verdict == "allow" else decision.stall
        negotiator_line = approved.text
        tail = f"{tail}\nNEGOTIATOR: {negotiator_line}"[-1200:]

        cited = phase is NegotiationPhase.LEVERAGE and decision.verdict == "allow"
        carrier_line = carrier.respond(phase, negotiator_cited_quote=cited)
        tail = f"{tail}\nPROVIDER: {carrier_line}"[-1200:]

        result.turns.append(Turn(
            index=i, phase=phase, negotiator=negotiator_line,
            gate_verdict=decision.verdict, gate_reason=decision.reason,
            carrier=carrier_line, carrier_total=carrier.total,
        ))

    fsm.finish()
    result.final_total = carrier.total
    return result


def _load_persona(persona: str) -> dict[str, Any]:
    path = persona if os.sep in persona else str(COUNTERAGENTS / f"{persona}.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        available = ", ".join(sorted(p.stem for p in COUNTERAGENTS.glob("*.json")))
        raise SystemExit(f"duet: cannot load persona {persona!r} ({exc}). available: {available}")


def _competitor_quote_fact(total: int) -> LedgerFact:
    return LedgerFact(
        id="competitor_quote",
        kind=LedgerFactKind.QUOTE,
        value={"total": total},
        source=Source(type=SourceType.CONFIG, ref="duet.sim.prior_call_quote"),
        call_id="duet-sim",
        ts=datetime.now(timezone.utc),
    )


# --------------------------------------------------------------------------- #
# Rendering + optional voice
# --------------------------------------------------------------------------- #
def render(result: DuetResult, *, voice: bool, negotiator_voice: str | None, carrier_voice: str) -> None:
    print(f"\n  ☎  Simulated negotiation call — {result.persona}")
    print(f"     🤖 Negotiator (gated, evidence-first)   vs.   🏢 Provider (scripted counteragent)")
    print(f"     leverage on hand: a documented ${result.competitor_quote:,} competitor quote in the ledger\n")

    tmp: Path | None = None
    if voice:
        _prepare_voice()
        tmp = Path(tempfile.mkdtemp(prefix="duet-"))
        negotiator_voice = negotiator_voice or os.getenv("ELEVENLABS_VOICE_ID") or DEFAULT_CARRIER_VOICE

    for turn in result.turns:
        badge = "allow · supported" if turn.gate_verdict == "allow" else f"BLOCK ({turn.gate_reason}) → stall"
        print(f"  ── Turn {turn.index} · {turn.phase.value} " + "─" * max(0, 40 - len(turn.phase.value)))
        print(f"  🤖 Negotiator  [gate: {badge}]")
        print(f"     \"{turn.negotiator}\"")
        if tmp is not None:
            _speak(turn.negotiator, negotiator_voice, tmp, f"n{turn.index}")
        print(f"  🏢 Provider ({result.persona})")
        print(f"     \"{turn.carrier}\"")
        if tmp is not None:
            _speak(turn.carrier, carrier_voice, tmp, f"c{turn.index}")
        print()

    delta = result.concession
    print("  ── Outcome " + "─" * 40)
    if result.opening_total is not None and result.final_total is not None:
        moved = f"  (moved ${delta:,})" if delta else "  (held firm)"
        print(f"     opening ${result.opening_total:,}  →  final ${result.final_total:,}{moved}")
    print(f"     honesty gate: {result.gate_blocks} block(s), "
          f"{len(result.turns) - result.gate_blocks} approved utterance(s)\n")


def _prepare_voice() -> None:
    _load_dotenv()
    if not os.getenv("ELEVENLABS_API_KEY"):
        print("  ⚠  ELEVENLABS_API_KEY not set — voice will use the offline placeholder tone.\n")


def _speak(text: str, voice_id: str, tmp: Path, tag: str) -> None:
    # Re-issue the line as a gate-approved utterance so TTS accepts it (TTS only
    # speaks capabilities, never raw strings). A neutral card with no claims passes.
    gate = HonestyGate(stall_phrases=STALL_PHRASES)
    card = CallCard(version=1, phase=NegotiationPhase.WRAP, phase_goal="speak",
                    next_move="speak", allowed_fact_ids=(), tone_preset="warm")
    decision = gate.evaluate(draft=text, card=card, ledger_facts=())
    utter = decision.approved if decision.verdict == "allow" else decision.stall
    config = ElevenLabsTTSConfig(voice_id=voice_id, output_format="mp3_44100_128")
    try:
        out = ElevenLabsTTS(config).synthesize(utter, VOICE_SETTINGS)
    except Exception as exc:  # never let a demo crash on a voice hiccup
        print(f"     (voice unavailable: {exc})")
        return
    path = _write_playable(out.audio, out.source, tmp / tag)
    player = shutil.which("afplay") or shutil.which("ffplay")
    if player:
        subprocess.run([player, str(path)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print(f"     (no audio player found; wrote {path})")


def _write_playable(audio: bytes, source: str, stem: Path) -> Path:
    if source in ("elevenlabs", "cache"):  # real MP3 container
        path = stem.with_suffix(".mp3")
        path.write_bytes(audio)
        return path
    # Offline fallback is raw signed 16-bit mono PCM at 16 kHz — wrap it as WAV.
    path = stem.with_suffix(".wav")
    path.write_bytes(_wav16(audio, 16_000))
    return path


def _wav16(pcm: bytes, rate: int) -> bytes:
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.is_file():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulate a two-agent negotiation call (text always, voice optional).")
    parser.add_argument("--persona", default="pressure_closer",
                        help="counteragent slug or path (default: pressure_closer)")
    parser.add_argument("--competitor-quote", type=int, default=3000,
                        help="the documented competitor quote our negotiator leverages (default: 3000)")
    parser.add_argument("--benchmark-low", type=int, default=4000,
                        help="benchmark low, used to price relative personas like lowball_broker (default: 4000)")
    parser.add_argument("--bluff", action="store_true",
                        help="make the negotiator cite an unsupported number — watch the honesty gate refuse it")
    parser.add_argument("--voice", action="store_true", help="speak each turn via ElevenLabs and play it")
    parser.add_argument("--negotiator-voice", default=None, help="ElevenLabs voice id for our agent (default: env ELEVENLABS_VOICE_ID)")
    parser.add_argument("--carrier-voice", default=DEFAULT_CARRIER_VOICE, help="ElevenLabs voice id for the provider")
    args = parser.parse_args(argv)

    result = run_duet(persona=args.persona, competitor_quote=args.competitor_quote,
                      benchmark_low=args.benchmark_low, bluff=args.bluff)
    render(result, voice=args.voice, negotiator_voice=args.negotiator_voice, carrier_voice=args.carrier_voice)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
