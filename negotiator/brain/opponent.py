from __future__ import annotations

import argparse
import json
import math
import statistics
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from negotiator.core.contracts import BusEvent, TacticEvent, TacticType


CurveType = str


def _series(prices: Sequence[float], ts: Sequence[float]) -> tuple[list[float], list[float]]:
    values = [float(value) for value in prices]
    times = [float(value) for value in ts]
    if len(values) != len(times) or len(values) < 3:
        raise ValueError("prices and ts must have the same length and contain at least 3 offers")
    if not all(math.isfinite(value) for value in (*values, *times)):
        raise ValueError("prices and timestamps must be finite")
    if any(price <= 0 for price in values):
        raise ValueError("prices must be positive")
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError("timestamps must be strictly increasing")
    if any(right > left for left, right in zip(values, values[1:])):
        raise ValueError("seller offers must be non-increasing")
    if values[0] == values[-1]:
        raise ValueError("at least one concession is required")
    return values, times


def estimate_floor(prices: Sequence[float], ts: Sequence[float]) -> tuple[float, tuple[float, float]]:
    """Invert a descending seller-offer series into a floor and uncertainty band.

    This is deliberately pure: no clock, model, bus, or global calibration is consulted.
    """

    values, _ = _series(prices, ts)
    deltas = [left - right for left, right in zip(values, values[1:])]
    positive_pairs = [
        current / previous
        for previous, current in zip(deltas, deltas[1:])
        if previous > 0
    ]
    ratio = min(max(statistics.median(positive_pairs or [0.0]), 0.0), 0.99)
    geometric = values[-1] - deltas[-1] * ratio / (1.0 - ratio)

    xs = values[:-1]
    ys = deltas
    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)
    variance = sum((x - x_mean) ** 2 for x in xs)
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / variance if variance else 0.0
    intercept = y_mean - slope * x_mean
    intercept_floor = -intercept / slope if slope > 1e-9 else geometric

    # A divergent series (r >= 1 before clamping) has no geometric asymptote.
    # Do not turn both invalid extrapolations into a falsely-certain zero floor:
    # cap the extrapolation to one more observed total-concession range.
    plausible_low = max(0.0, values[-1] - (values[0] - values[-1]))
    candidates = [max(plausible_low, min(value, values[-1])) for value in (geometric, intercept_floor)]
    floor = max(candidates)
    band = (min(candidates), max(candidates))
    return round(floor, 2), (round(band[0], 2), round(band[1], 2))


def classify_curve(prices: Sequence[float], ts: Sequence[float]) -> CurveType:
    """Classify time-normalized concession shape as boulware, linear, or conceder."""

    values, times = _series(prices, ts)
    rates = [
        (left - right) / (end - start)
        for left, right, start, end in zip(values, values[1:], times, times[1:])
    ]
    mid = max(1, len(rates) // 2)
    early = statistics.fmean(rates[:mid])
    late = statistics.fmean(rates[-mid:])
    scale = max(early, late, 1e-9)
    if abs(early - late) / scale <= 0.15:
        return "linear"
    return "boulware" if late > early else "conceder"


_TACTIC_PATTERNS: tuple[tuple[TacticType, tuple[str, ...]], ...] = (
    (TacticType.DEADLINE, ("today only", "tomorrow", "expires", "by end of day", "сегодня", "завтра", "истекает")),
    (TacticType.LOWBALL, ("sight unseen", "without seeing", "no inventory needed", "без осмотра", "без инвентар")),
    (TacticType.STONEWALL, ("non-negotiable", "final offer", "take it or leave it", "не обсуждается", "последняя цена")),
    (TacticType.PRESSURE, ("book now", "act now", "another customer", "lose the slot", "бронируйте сейчас", "другой клиент")),
    (TacticType.VAGUE, ("approximately", "should be around", "we'll see", "depends", "примерно", "посмотрим", "зависит")),
)


def classify_tactic(utterance: str, *, utterance_ref: str = "offline") -> TacticEvent | None:
    """Return the highest-priority deterministic tactic match for an utterance."""

    normalized = " ".join(utterance.casefold().split())
    if any(negation in normalized for negation in ("does not expire", "doesn't expire", "not urgent", "не истекает", "не срочно")):
        for marker in ("tomorrow", "expires", "завтра", "истекает"):
            normalized = normalized.replace(marker, "")
    if "depends on nothing" in normalized or "ни от чего не зависит" in normalized or "не зависит ни от чего" in normalized:
        normalized = normalized.replace("depends", "").replace("зависит", "")
    for tactic_type, patterns in _TACTIC_PATTERNS:
        matches = sum(pattern in normalized for pattern in patterns)
        if tactic_type is TacticType.DEADLINE and matches:
            urgency_context = ("price", "rate", "quote", "offer", "slot", "book", "цен", "ставк", "мест")
            matches = matches if any(token in normalized for token in urgency_context) else 0
        if tactic_type is TacticType.PRESSURE and matches and any(marker in normalized for marker in ("another customer", "другой клиент")):
            pressure_context = ("book", "slot", "availability", "waiting", "lose", "мест", "брон")
            matches = matches if any(token in normalized for token in pressure_context) else 0
        if matches:
            confidence = min(0.99, 0.72 + 0.09 * (matches - 1))
            return TacticEvent(type=tactic_type, utterance_ref=utterance_ref, confidence=confidence)
    return None


def summary_event(
    *, call_id: str, prices: Sequence[float], ts: Sequence[float], refs: Sequence[str] = ()
) -> BusEvent:
    """Build the serializable event consumed by the dashboard and strategist."""

    floor, band = estimate_floor(prices, ts)
    return BusEvent(
        call_id=call_id,
        module="opponent",
        kind="opponent.summary",
        payload={
            "prices": list(prices),
            "timestamps": list(ts),
            "floor": floor,
            "floor_band": list(band),
            "curve": classify_curve(prices, ts),
        },
        refs=tuple(refs),
    )


def replay(path: str | Path) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            item = json.loads(line)
            if "prices" in item:
                event = summary_event(
                    call_id=item.get("call_id", "offline"), prices=item["prices"], ts=item["ts"]
                )
                result = event.payload
            else:
                tactic = classify_tactic(item["utterance"], utterance_ref=item.get("ref", "offline"))
                result = {"tactic": tactic.model_dump(mode="json") if tactic else None}
            output.append(result)
            print(json.dumps(result, ensure_ascii=False))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate a seller floor and classify tactics")
    parser.add_argument("--prices", help="comma-separated seller prices")
    parser.add_argument("--timestamps", help="comma-separated timestamps; defaults to 0,1,...")
    parser.add_argument("--utterance")
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    if args.replay:
        replay(args.replay)
        return
    if args.utterance:
        tactic = classify_tactic(args.utterance)
        print(json.dumps(tactic.model_dump(mode="json") if tactic else None))
        return
    if not args.prices:
        parser.error("one of --prices, --utterance, or --replay is required")
    prices = [float(value) for value in args.prices.split(",")]
    ts = [float(value) for value in args.timestamps.split(",")] if args.timestamps else list(range(len(prices)))
    print(json.dumps(summary_event(call_id="offline", prices=prices, ts=ts).payload))


if __name__ == "__main__":
    main()
