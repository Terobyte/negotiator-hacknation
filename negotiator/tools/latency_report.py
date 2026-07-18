from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


TARGETS_MS: Mapping[str, float] = {
    "transport": 80.0,
    "vad": 300.0,
    "stt_final": 150.0,
    "llm_ttft": 400.0,
    "tts_ttfb": 150.0,
    "mouth_to_ear": 1_200.0,
}

_STAGE_ALIASES = {
    "speech_start": "speech_start",
    "vad_start": "speech_start",
    "speech_end": "speech_end",
    "vad_end": "speech_end",
    "turn_end": "speech_end",
    "stt_final": "stt_final",
    "transcript_final": "stt_final",
    "llm_first_token": "llm_first_token",
    "talker_first_token": "llm_first_token",
    "tts_first_audio": "tts_first_audio",
    "tts_first_byte": "tts_first_audio",
    "audio_out": "audio_out",
    "playback_start": "audio_out",
}


@dataclass(frozen=True, slots=True)
class SegmentStats:
    count: int
    median_ms: float
    p95_ms: float
    target_ms: float | None
    target_met: bool | None


@dataclass(frozen=True, slots=True)
class LatencyReport:
    segments: Mapping[str, SegmentStats]
    milestone_ms: float = 1_200.0

    @property
    def milestone_met(self) -> bool | None:
        total = self.segments.get("mouth_to_ear")
        return None if total is None else total.median_ms <= self.milestone_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "milestone_ms": self.milestone_ms,
            "milestone_met": self.milestone_met,
            "segments": {name: asdict(stats) for name, stats in self.segments.items()},
        }


def read_latency_samples(path: str | Path) -> dict[str, list[float]]:
    samples: dict[str, list[float]] = defaultdict(list)
    stages: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}") from exc
            payload = record.get("payload") or {}
            if not isinstance(payload, Mapping):
                continue
            kind = str(record.get("kind", ""))
            if kind in {"latency_span", "latency.span"}:
                name = str(payload.get("segment") or payload.get("name") or "")
                duration = _duration_ms(payload)
                if name and duration is not None and duration >= 0:
                    samples[name].append(duration)
                continue
            stage = _STAGE_ALIASES.get(kind)
            if stage is None:
                stage = _STAGE_ALIASES.get(str(payload.get("stage", "")))
            if stage is None:
                continue
            timestamp = _timestamp_ms(payload.get("at", record.get("ts")))
            if timestamp is None:
                continue
            call_id = str(record.get("call_id", "unknown"))
            turn_id = str(payload.get("turn_id", payload.get("utterance_id", "default")))
            stages[(call_id, turn_id)][stage] = timestamp

    for turn in stages.values():
        _append_delta(samples, "vad", turn, "speech_start", "speech_end")
        _append_delta(samples, "stt_final", turn, "speech_end", "stt_final")
        _append_delta(samples, "llm_ttft", turn, "stt_final", "llm_first_token")
        _append_delta(samples, "tts_ttfb", turn, "llm_first_token", "tts_first_audio")
        _append_delta(samples, "transport", turn, "tts_first_audio", "audio_out")
        _append_delta(samples, "mouth_to_ear", turn, "speech_end", "audio_out")
    return dict(samples)


def build_report(samples: Mapping[str, Iterable[float]]) -> LatencyReport:
    segments: dict[str, SegmentStats] = {}
    for name, raw_values in sorted(samples.items()):
        values = sorted(float(value) for value in raw_values)
        if not values:
            continue
        target = TARGETS_MS.get(name)
        median = statistics.median(values)
        p95 = percentile(values, 0.95)
        segments[name] = SegmentStats(
            count=len(values),
            median_ms=round(median, 2),
            p95_ms=round(p95, 2),
            target_ms=target,
            target_met=None if target is None else median <= target,
        )
    return LatencyReport(segments)


def report_file(path: str | Path) -> LatencyReport:
    return build_report(read_latency_samples(path))


def percentile(sorted_values: list[float], quantile: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    rank = (len(sorted_values) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    fraction = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def _duration_ms(payload: Mapping[str, Any]) -> float | None:
    if "duration_ms" in payload:
        return float(payload["duration_ms"])
    start = _timestamp_ms(payload.get("start"))
    end = _timestamp_ms(payload.get("end"))
    return None if start is None or end is None else end - start


def _timestamp_ms(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric * 1_000 if abs(numeric) < 10_000_000_000 else numeric
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1_000
    except ValueError:
        return None


def _append_delta(
    samples: dict[str, list[float]], name: str, turn: Mapping[str, float], start: str, end: str
) -> None:
    if start in turn and end in turn and turn[end] >= turn[start]:
        samples[name].append(turn[end] - turn[start])


def main() -> None:
    parser = argparse.ArgumentParser(description="Report voice-loop latency from a journal JSONL file")
    parser.add_argument("journal")
    parser.add_argument("--enforce", action="store_true", help="exit non-zero if the 1.2s median milestone is missed")
    args = parser.parse_args()
    report = report_file(args.journal)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    if args.enforce and report.milestone_met is not True:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
