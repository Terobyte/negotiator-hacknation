from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

import pytest

from negotiator.product.estimator import (
    ConfirmationRequired,
    DocumentConfirmationRequired,
    IdempotencyConflict,
    JobSpecStore,
    document_to_job_spec,
    map_submit_job_spec,
    submit_job_spec,
)


FIXTURES = Path("negotiator/fixtures")


def load(name):
    return json.loads((FIXTURES / name).read_text())


def test_voice_and_document_map_to_same_job_fields():
    payload = load("estimator_webhook.json")
    _, voice = map_submit_job_spec(payload)
    document = document_to_job_spec(FIXTURES / "old_quote.pdf", confirmed=True)
    expected = load("estimator_golden.json")
    assert document.model_dump(mode="json") == expected
    # Source differs by design; all actual estimate fields are identical.
    assert voice.model_copy(update={"inventory_src": "doc"}) == document


def test_retry_and_concurrency_are_idempotent(tmp_path):
    payload = load("estimator_webhook.json")
    store = JobSpecStore(tmp_path / "jobs.sqlite3")
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: submit_job_spec(payload, store), range(40)))
    assert sum(result.created for result in results) == 1
    assert len({result.job_spec.model_dump_json() for result in results}) == 1


def test_conflicting_retry_fails_closed(tmp_path):
    payload = load("estimator_webhook.json")
    store = JobSpecStore(tmp_path / "jobs.sqlite3")
    submit_job_spec(payload, store)
    changed = json.loads(json.dumps(payload))
    changed["job_spec"]["destination"] = "Philadelphia, PA"
    with pytest.raises(IdempotencyConflict):
        submit_job_spec(changed, store)


def test_unconfirmed_voice_and_document_are_rejected():
    payload = load("estimator_webhook.json")
    payload.pop("read_back_confirmed")
    with pytest.raises(ConfirmationRequired):
        map_submit_job_spec(payload)
    payload = load("estimator_webhook.json")
    payload["job_spec"]["confirmed"] = False
    with pytest.raises(ConfirmationRequired):
        map_submit_job_spec(payload)
    with pytest.raises(DocumentConfirmationRequired):
        document_to_job_spec(FIXTURES / "old_quote.txt", confirmed=False)


def test_verified_ocr_wins_and_unverified_ocr_does_not():
    payload = load("estimator_webhook.json")
    _, protected = map_submit_job_spec(payload)
    assert protected.origin == "New York, NY"
    payload.pop("verified_ocr_fields")
    payload["dynamic_variables"]["verified_ocr_fields"] = []
    _, unprotected = map_submit_job_spec(payload)
    assert unprotected.origin == "LLM tried to overwrite this"


def test_verified_ocr_source_mismatch_fails_closed():
    payload = load("estimator_webhook.json")
    payload["verified_ocr_fields"] = []
    with pytest.raises(ValueError, match="conflicts"):
        map_submit_job_spec(payload)


def test_missing_fields_are_not_hallucinated(tmp_path):
    source = tmp_path / "partial.txt"
    source.write_text("origin: New York, NY\nconfirmed: true\n")
    with pytest.raises(ValueError, match="missing extracted intake fields"):
        document_to_job_spec(source, confirmed=True)


@pytest.mark.parametrize("field", ["floors", "elevator", "specialty_items"])
def test_defaults_cannot_hide_uncollected_fields(field):
    payload = load("estimator_webhook.json")
    payload["job_spec"].pop(field)
    with pytest.raises(ValueError, match="missing collected"):
        map_submit_job_spec(payload)
    document = load("estimator_golden.json")
    document.pop(field)
    with pytest.raises(ValueError, match="missing extracted"):
        document_to_job_spec(document, confirmed=True)


def test_in_memory_store_survives_multiple_connections():
    payload = load("estimator_webhook.json")
    store = JobSpecStore(":memory:")
    assert submit_job_spec(payload, store).created
    assert not submit_job_spec(payload, store).created
