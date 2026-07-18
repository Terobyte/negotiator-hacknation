"""Deterministic intake adapters for voice and document submissions."""

from .documents import DocumentConfirmationRequired, document_to_job_spec
from .voice import (
    ConfirmationRequired,
    IdempotencyConflict,
    JobSpecStore,
    SubmissionResult,
    map_submit_job_spec,
    submit_job_spec,
)

__all__ = [
    "ConfirmationRequired",
    "DocumentConfirmationRequired",
    "IdempotencyConflict",
    "JobSpecStore",
    "SubmissionResult",
    "document_to_job_spec",
    "map_submit_job_spec",
    "submit_job_spec",
]
