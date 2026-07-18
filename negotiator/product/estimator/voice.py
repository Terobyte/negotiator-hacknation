"""ElevenLabs ``submit_job_spec`` webhook mapper and durable idempotency store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from negotiator.core.contracts import JobSpec


class ConfirmationRequired(ValueError):
    """The customer did not explicitly confirm the complete read-back."""


class IdempotencyConflict(RuntimeError):
    """A conversation id was reused for a different confirmed JobSpec."""


_JOB_FIELDS = frozenset(JobSpec.model_fields)
_REQUIRED_INTAKE_FIELDS = _JOB_FIELDS - {"confirmed", "inventory_src"}
_ALIASES = {
    "ocr_origin": "origin",
    "ocr_destination": "destination",
    "ocr_distance_mi": "distance_mi",
    "ocr_size": "size",
    "ocr_rooms": "size",
    "ocr_date_window": "date_window",
    "ocr_floors": "floors",
    "ocr_elevator": "elevator",
    "ocr_specialty_items": "specialty_items",
    "ocr_budget_ceiling": "budget_ceiling",
}


def _explicit_confirmation(payload: Mapping[str, Any]) -> bool:
    confirmation = payload.get("confirmation")
    candidates = (
        payload.get("read_back_confirmed"),
        payload.get("confirmed_read_back"),
        confirmation.get("read_back_confirmed") if isinstance(confirmation, Mapping) else None,
        confirmation.get("answer") if isinstance(confirmation, Mapping) else None,
    )
    return any(value is True or (isinstance(value, str) and value.strip().lower() in {"yes", "y"}) for value in candidates)


def _job_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("job_spec", "parameters", "tool_input"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            nested = value.get("job_spec")
            return dict(nested if isinstance(nested, Mapping) else value)
    return {key: value for key, value in payload.items() if key in _JOB_FIELDS}


def _dynamic_variables(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("dynamic_variables", "conversation_initiation_client_data"):
        value = payload.get(key)
        if not isinstance(value, Mapping):
            continue
        nested = value.get("dynamic_variables")
        return nested if isinstance(nested, Mapping) else value
    return {}


def _verified_fields(payload: Mapping[str, Any], dynamic: Mapping[str, Any]) -> set[str]:
    raw = dynamic.get("verified_ocr_fields", ())
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raise ValueError("verified_ocr_fields must be a list or comma-separated string")
    fields: set[str] = set()
    for source_name in raw:
        if not isinstance(source_name, str):
            raise ValueError("verified OCR field names must be strings")
        field = _ALIASES.get(source_name, source_name.removeprefix("ocr_"))
        if field not in _JOB_FIELDS or field in {"confirmed", "inventory_src"}:
            raise ValueError(f"unsupported verified OCR field: {source_name}")
        fields.add(field)
    claimed = payload.get("verified_ocr_fields")
    if claimed is not None:
        if isinstance(claimed, str):
            claimed = [part.strip() for part in claimed.split(",") if part.strip()]
        if not isinstance(claimed, (list, tuple, set, frozenset)):
            raise ValueError("top-level verified_ocr_fields must be a list")
        normalized_claim = {_ALIASES.get(item, item.removeprefix("ocr_")) for item in claimed if isinstance(item, str)}
        if len(normalized_claim) != len(claimed) or normalized_claim != fields:
            raise ValueError("verified_ocr_fields conflicts with authoritative Dynamic Variables")
    return fields


def _ocr_value(field: str, dynamic: Mapping[str, Any]) -> Any:
    candidates = (f"ocr_{field}", field)
    if field == "size":
        candidates = ("ocr_size", "ocr_rooms", "size")
    for key in candidates:
        if key in dynamic:
            return dynamic[key]
    raise ValueError(f"verified OCR field {field!r} has no Dynamic Variable value")


def map_submit_job_spec(payload: Mapping[str, Any]) -> tuple[str, JobSpec]:
    """Validate an EL tool body and return its stable key and canonical contract."""

    conversation_id = payload.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("conversation_id is required")
    if not _explicit_confirmation(payload):
        raise ConfirmationRequired("submit_job_spec is allowed only after an explicit yes to the full read-back")

    body = _job_body(payload)
    if body.get("confirmed") is not True:
        raise ConfirmationRequired("JobSpec.confirmed must reflect the customer's explicit yes")
    dynamic = _dynamic_variables(payload)
    verified = _verified_fields(payload, dynamic)
    for field in verified:
        body[field] = _ocr_value(field, dynamic)
    missing = sorted(_REQUIRED_INTAKE_FIELDS - body.keys())
    if missing:
        raise ValueError(f"JobSpec is missing collected intake fields: {', '.join(missing)}")
    # When OCR contributed to a voice interview, provenance is necessarily both.
    body["inventory_src"] = "both" if verified else body.get("inventory_src", "voice")
    try:
        spec = JobSpec.model_validate(body)
    except ValidationError as exc:
        raise ValueError(f"invalid JobSpec: {exc}") from exc
    return conversation_id.strip(), spec


@dataclass(frozen=True)
class SubmissionResult:
    conversation_id: str
    job_spec: JobSpec
    created: bool

    def as_response(self) -> dict[str, Any]:
        return {
            "ok": True,
            "conversation_id": self.conversation_id,
            "created": self.created,
            "job_spec": self.job_spec.model_dump(mode="json"),
        }


class JobSpecStore:
    """Small local SQLite store safe across threads and worker processes."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._memory_uri = f"file:jobspec-{id(self)}?mode=memory&cache=shared" if self.path == ":memory:" else None
        self._keeper: sqlite3.Connection | None = self._connection() if self._memory_uri else None
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = self._keeper or self._connection()
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS job_specs ("
                "conversation_id TEXT PRIMARY KEY, digest TEXT NOT NULL, spec_json TEXT NOT NULL, "
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        finally:
            if connection is not self._keeper:
                connection.close()

    def _connection(self) -> sqlite3.Connection:
        target = self._memory_uri or self.path
        connection = sqlite3.connect(target, timeout=30, isolation_level=None, uri=self._memory_uri is not None)
        connection.execute("PRAGMA busy_timeout=30000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def put(self, conversation_id: str, spec: JobSpec) -> SubmissionResult:
        encoded = spec.model_dump_json()
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT digest, spec_json FROM job_specs WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO job_specs(conversation_id, digest, spec_json) VALUES (?, ?, ?)",
                    (conversation_id, digest, encoded),
                )
                connection.commit()
                return SubmissionResult(conversation_id, spec, True)
            connection.commit()
            if row[0] != digest:
                raise IdempotencyConflict(f"conversation_id {conversation_id!r} already has a different JobSpec")
            return SubmissionResult(conversation_id, JobSpec.model_validate_json(row[1]), False)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()


def submit_job_spec(payload: Mapping[str, Any], store: JobSpecStore) -> SubmissionResult:
    conversation_id, spec = map_submit_job_spec(payload)
    return store.put(conversation_id, spec)


def create_router(store_path: str | Path):
    """Create a FastAPI router lazily; importing estimator never requires FastAPI."""

    try:
        from fastapi import APIRouter, HTTPException
    except ImportError as exc:  # pragma: no cover - depends on optional deployment extra
        raise RuntimeError("FastAPI is optional; install it to create the webhook router") from exc

    router = APIRouter()
    store = JobSpecStore(store_path)

    @router.post("/webhooks/elevenlabs/submit_job_spec")
    def webhook(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return submit_job_spec(payload, store).as_response()
        except ConfirmationRequired as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
