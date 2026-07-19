from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from negotiator.core.contracts import (
    CallOutcome,
    Citation,
    EstimateType,
    RankedMover,
    Report,
)


DEFAULT_CONFIG = Path(__file__).parents[1] / "config" / "verticals" / "moving.yaml"


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    outcome: CallOutcome
    sight_unseen: bool = False
    address_provided: bool = True
    history_evasive: bool = False
    carrier_or_broker_disclosed: bool = True
    mentions_110_percent: bool = False
    verification: Mapping[str, Any] | None = None
    citations: tuple[Citation, ...] = ()


def is_lowball(total: Decimal | float | int, benchmark_low: Decimal | float | int) -> bool:
    """The alarm is deliberately strict: equality at 70% is not below the line."""

    return Decimal(str(total)) < Decimal("0.70") * Decimal(str(benchmark_low))


def red_flags(record: OutcomeRecord, benchmark_low: Decimal) -> tuple[str, ...]:
    quote = record.outcome.quote
    flags = list(record.outcome.red_flags)
    if quote is None:
        return tuple(dict.fromkeys(flags))
    if is_lowball(quote.total, benchmark_low) and record.sight_unseen:
        flags.append("RF-A[HIGH]: quote is below 70% of benchmark and sight-unseen")
    methods = {method.strip().lower() for method in quote.deposit.payment_methods}
    cash_wire_only = bool(methods) and methods <= {"cash", "wire", "wire transfer"}
    if quote.deposit.pct_of_total > 25 or cash_wire_only:
        flags.append("RF-B[HIGH]: deposit exceeds 25% or is cash/wire-only")
    if not record.carrier_or_broker_disclosed or (not quote.usdot and not quote.mc):
        flags.append("RF-C[HIGH]: broker/carrier or USDOT/MC identity was not provided")
    verification = record.verification or {}
    if isinstance(verification, Mapping) and verification.get("fallback") is True:
        flags.append("RF-D[MED]: FMCSA verification is unavailable (fallback data)")
    elif _bad_verification(verification):
        flags.append("RF-D[HIGH]: FMCSA verification is adverse")
    if quote.estimate_type is EstimateType.NON_BINDING and not record.mentions_110_percent:
        flags.append("RF-E[MED]: non-binding estimate omitted the 110% rule")
    if not record.address_provided or record.history_evasive:
        flags.append("RF-F[MED]: missing address or evasive operating history")
    return tuple(dict.fromkeys(flags))


def normalize_total(outcome: CallOutcome) -> Decimal:
    if outcome.quote is None:
        raise ValueError("cannot normalize an outcome without a quote")
    disclosed_sum = sum(
        (item.amount for item in outcome.quote.line_items if item.disclosed), Decimal("0")
    )
    return max(outcome.quote.total, disclosed_sum)


def build_report(
    records: Iterable[OutcomeRecord],
    *,
    benchmark_low: Decimal | int | float,
    fee_names: Mapping[int, str],
) -> Report:
    benchmark = Decimal(str(benchmark_low))
    ranked: list[RankedMover] = []
    uncited = 0
    for record in records:
        quote = record.outcome.quote
        if quote is None:
            continue
        disclosed_codes = {item.code for item in quote.line_items if item.disclosed}
        missing = tuple(
            str(name) for code, name in sorted(fee_names.items()) if int(code) not in disclosed_codes
        )
        if not record.citations:
            uncited += 1
            continue
        ranked.append(
            RankedMover(
                mover=record.outcome.mover_id,
                normalized_total=normalize_total(record.outcome),
                missing_items=missing,
                red_flags=red_flags(record, benchmark),
                citations=record.citations,
            )
        )
    if not ranked:
        if uncited:
            raise ValueError("report requires at least one quoted outcome with a citation")
        raise ValueError("report requires at least one quoted outcome")
    ranked.sort(key=lambda item: (len(item.red_flags), item.normalized_total, len(item.missing_items)))
    winner = ranked[0]
    claim = winner.citations[0].transcript_span
    if winner.red_flags:
        reason = f"it has the fewest serious concerns ({len(winner.red_flags)})"
    else:
        reason = "it has no triggered red-flag rules"
    recommendation = (
        f"Choose {winner.mover}: {reason}, with a normalized quote of "
        f"${winner.normalized_total:,.2f}. Evidence: {claim}."
    )
    return Report(recommendation_plain=recommendation, ranked=tuple(ranked))


def load_records(path: str | Path) -> list[OutcomeRecord]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw.get("outcomes", raw) if isinstance(raw, Mapping) else raw
    result: list[OutcomeRecord] = []
    for row in rows:
        outcome_data = row.get("outcome", row)
        outcome = CallOutcome.model_validate(outcome_data)
        citations = tuple(Citation.model_validate(item) for item in row.get("citations", ()))
        result.append(
            OutcomeRecord(
                outcome=outcome,
                sight_unseen=bool(row.get("sight_unseen", False)),
                address_provided=bool(row.get("address_provided", True)),
                history_evasive=bool(row.get("history_evasive", False)),
                carrier_or_broker_disclosed=bool(row.get("carrier_or_broker_disclosed", True)),
                mentions_110_percent=bool(row.get("mentions_110_percent", False)),
                verification=row.get("verification"),
                citations=citations,
            )
        )
    return result


def load_moving_config(path: str | Path = DEFAULT_CONFIG) -> tuple[Decimal, dict[int, str]]:
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    benchmark = Decimal(str(config["benchmarks"]["low"]))
    fees = {int(code): str(name) for code, name in config["taxonomy"]["fee_codes"].items()}
    return benchmark, fees


def _bad_verification(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = key.lower().replace("_", "")
            text = str(child).strip().upper()
            if normalized in {"allowtooperate", "allowedtooperate"} and text in {"N", "NO", "FALSE"}:
                return True
            if normalized in {"outofservice", "oos"} and text in {"Y", "YES", "TRUE"}:
                return True
            if "complaint" in normalized:
                try:
                    if int(child) >= 3:
                        return True
                except (TypeError, ValueError):
                    pass
            if _bad_verification(child):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_bad_verification(child) for child in value)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a ranked, evidence-linked moving report")
    parser.add_argument("--outcomes", "--replay", dest="outcomes", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    benchmark, fee_names = load_moving_config(args.config)
    report = build_report(load_records(args.outcomes), benchmark_low=benchmark, fee_names=fee_names)
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
