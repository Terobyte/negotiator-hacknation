from __future__ import annotations

import argparse
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


_ROLE_DELIMITERS = re.compile(
    r"(?i)\b(?:system|developer|assistant|tool|function|user)\s*[:>]|"
    r"<\|(?:im_start|im_end|system|assistant|developer)\|>|"
    r"\[/?(?:INST|SYSTEM|ASSISTANT|DEVELOPER)\]"
)
_INJECTION = re.compile(
    r"(?i)(?:\b(?:ignore|disregard|forget|override|reveal|print|repeat|expose|dump)\b.{0,120}"
    r"\b(?:instruction|prompt|policy|role|secret|developer|system|config)\w*\b|"
    r"\bact\s+as\b|\bignore\s+everything\b)"
)
_MARKUP = re.compile(r"(?i)(?:#{1,6}\s*(?:system|developer|assistant)|</?(?:system|developer|assistant|tool)[^>]*>|```(?:system|prompt|xml)?)")
MAX_TRANSCRIPT_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class FirewallDecision:
    sanitized: str
    suspicious: bool
    reasons: tuple[str, ...]


def sanitize_transcript(text: str) -> FirewallDecision:
    """Neutralize role control syntax while preserving evidence for the journal."""
    reasons: list[str] = []
    sanitized = unicodedata.normalize("NFKC", text)
    control_removed = "".join(ch for ch in sanitized if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    if control_removed != sanitized:
        reasons.append("control_character")
    sanitized = control_removed[:MAX_TRANSCRIPT_CHARS].strip()
    if len(control_removed) > MAX_TRANSCRIPT_CHARS:
        reasons.append("length_limit")
    if _MARKUP.search(sanitized):
        reasons.append("markup_role_delimiter")
        sanitized = _MARKUP.sub(" [quoted-role-marker] ", sanitized)
    if _ROLE_DELIMITERS.search(sanitized):
        reasons.append("role_delimiter")
        sanitized = _ROLE_DELIMITERS.sub(" [quoted-role-marker] ", sanitized)
    if _INJECTION.search(sanitized):
        reasons.append("prompt_injection")
        sanitized = _INJECTION.sub("[untrusted instruction removed]", sanitized)
    sanitized = " ".join(sanitized.split())
    return FirewallDecision(sanitized=sanitized, suspicious=bool(reasons), reasons=tuple(reasons))


def replay(path: str | Path) -> list[FirewallDecision]:
    decisions: list[FirewallDecision] = []
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            decision = sanitize_transcript(str(raw["text"]))
            decisions.append(decision)
            print(json.dumps({"line": line_number, "suspicious": decision.suspicious,
                              "reasons": decision.reasons, "sanitized": decision.sanitized}))
    return decisions


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay untrusted transcripts through the firewall")
    parser.add_argument("--replay", required=True)
    args = parser.parse_args()
    replay(args.replay)


if __name__ == "__main__":
    main()
