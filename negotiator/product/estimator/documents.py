"""Deterministic document/text/OCR-injection mapping to ``JobSpec``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from negotiator.core.contracts import JobSpec


_REQUIRED_INTAKE_FIELDS = frozenset(JobSpec.model_fields) - {"confirmed", "inventory_src"}


class DocumentConfirmationRequired(ValueError):
    pass


def parse_key_value_text(
    text: str, *, trusted_private_fields: bool = False
) -> dict[str, Any]:
    """Parse frozen extractor output without trusting private fields by default."""

    result: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, raw_value = (part.strip() for part in line.split(":", 1))
        if key not in JobSpec.model_fields:
            continue
        if key == "budget_ceiling" and not trusted_private_fields:
            continue
        try:
            result[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            result[key] = raw_value
    return result


def load_document(path: str | Path, *, pdf_extractor: Callable[[Path], str] | None = None) -> Mapping[str, Any]:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".json":
        value = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("document JSON must contain an object")
        return value
    if suffix == ".pdf":
        if pdf_extractor is None:
            companion = source.with_suffix(".txt")
            if not companion.exists():
                raise RuntimeError("PDF extraction is optional; provide pdf_extractor or a frozen .txt companion")
            text = companion.read_text(encoding="utf-8")
        else:
            text = pdf_extractor(source)
    elif suffix in {".txt", ".md"}:
        text = source.read_text(encoding="utf-8")
    else:
        raise ValueError(f"unsupported document extension: {suffix or '<none>'}")
    return parse_key_value_text(text, trusted_private_fields=True)


def document_to_job_spec(
    document: str | Path | Mapping[str, Any],
    *,
    confirmed: bool | None = None,
    pdf_extractor: Callable[[Path], str] | None = None,
) -> JobSpec:
    body = dict(document if isinstance(document, Mapping) else load_document(document, pdf_extractor=pdf_extractor))
    extracted_confirmation = body.pop("confirmed", None)
    if confirmed is not True or extracted_confirmation is not True:
        raise DocumentConfirmationRequired("document fields require one explicit customer confirmation")
    missing = sorted(_REQUIRED_INTAKE_FIELDS - body.keys())
    if missing:
        raise ValueError(f"document is missing extracted intake fields: {', '.join(missing)}")
    body["confirmed"] = True
    body["inventory_src"] = "doc"
    try:
        return JobSpec.model_validate(body)
    except ValidationError as exc:
        raise ValueError(f"document does not contain a complete valid JobSpec: {exc}") from exc
