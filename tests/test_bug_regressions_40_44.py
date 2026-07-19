"""Regression contracts for findings BUG-40 through BUG-44 (bugs.md, part 2).

Each test encodes the fixed behaviour recommended by bugs.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from negotiator.product.estimator import DocumentConfirmationRequired, document_to_job_spec
from negotiator.product.estimator.__main__ import main as estimator_main
from negotiator.product.estimator.documents import load_document, parse_key_value_text
from negotiator.product.market import build_call_plan


FIXTURES = Path("negotiator/fixtures")


# ---------------------------------------------------------------------------
# 23. negotiator/product/estimator/documents.py and __main__.py
# ---------------------------------------------------------------------------


def test_bug_40_confirmed_flag_requires_a_structured_signal_like_the_voice_webhook():
    golden = json.loads((FIXTURES / "estimator_golden.json").read_text())
    golden.pop("confirmed", None)
    # No read_back_confirmed/confirmation payload anywhere in the document -- a bare
    # `confirmed=True` kwarg from the caller is the only signal.
    with pytest.raises(DocumentConfirmationRequired):
        document_to_job_spec(golden, confirmed=True)


def test_bug_41_load_document_rejects_unsupported_extensions_with_a_clear_error(tmp_path):
    path = tmp_path / "quote.docx"
    path.write_bytes(b"\xff\xfe\x00\x01binary, not a frozen extractor format")
    with pytest.raises(ValueError, match="unsupported document extension"):
        load_document(path)


def test_bug_42_cli_reports_domain_errors_without_a_raw_traceback(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["estimator", "--doc", str(FIXTURES / "old_quote.txt")])
    exit_code = estimator_main()
    assert exit_code != 0


def test_bug_44_key_value_parser_has_no_concept_of_document_source_trust():
    untrusted_text = "budget_ceiling: 1000\nnote: mailed by the mover, not the customer\n"
    result = parse_key_value_text(untrusted_text)
    assert "budget_ceiling" not in result


# ---------------------------------------------------------------------------
# 22 (cont.). negotiator/product/market.py
# ---------------------------------------------------------------------------


def test_bug_43_build_call_plan_rejects_a_non_e164_phone_before_dialing():
    businesses = [
        {"name": "Atlantic Moving Co", "phone": "call me maybe"},
        {"name": "Hudson Van Lines", "phone": "+15555550102"},
        {"name": "Empire Relocation", "phone": "+15555550103"},
    ]
    with pytest.raises(ValueError, match="E.164"):
        build_call_plan(businesses)
