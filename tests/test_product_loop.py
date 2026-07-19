from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal

from negotiator.core.contracts import CallOutcome, CallStatus
from negotiator.product.discovery import FIELD_MASK, PLACES_URL, PlacesClient
from negotiator.product.market import (
    CALL_ORDER,
    build_call_plan,
    run_market,
    supervise_call,
    supervise_call_async,
)
from negotiator.product.report import (
    build_report,
    is_lowball,
    load_moving_config,
    load_records,
    normalize_total,
    red_flags,
)
from negotiator.product.verify import FMCSAClient


def test_places_exact_request_and_parsing():
    captured = {}

    def http(method, url, headers, body, timeout):
        captured.update(method=method, url=url, headers=headers, body=body, timeout=timeout)
        return {"places": [{"displayName": {"text": "Mover"}, "nationalPhoneNumber": "555", "formattedAddress": "NY"}]}

    result = PlacesClient(api_key="key", http=http).search_movers("New York")
    assert captured["url"] == PLACES_URL
    assert captured["headers"]["X-Goog-FieldMask"] == FIELD_MASK
    assert captured["body"] == {"textQuery": "movers in New York", "includedType": "moving_company", "pageSize": 8}
    assert result[0].name == "Mover"


def test_places_offline_fixture_fallback():
    assert len(PlacesClient(api_key="").search_movers("New York")) >= 3


def test_fmcsa_dot_and_hyphenated_docket_endpoint():
    urls = []

    def http(url, _headers, _timeout):
        urls.append(url)
        return {"content": {"carrier": {"dotNumber": 123}}}

    client = FMCSAClient(web_key="key", http=http)
    assert client.verify_dot("USDOT-123")["fallback"] is False
    assert len(urls) == 3
    urls.clear()
    assert client.verify_mc("MC654321")["fallback"] is False
    assert "/docket-number/654321?" in urls[0]
    try:
        FMCSAClient(web_key="").verify_mc("654321")
    except RuntimeError as exc:
        assert "FMCSA_WEB_KEY" in str(exc)
    else:
        raise AssertionError("missing FMCSA configuration must fail loudly")


def test_market_order_mapping_and_real_mode():
    businesses = [{"name": f"m{i}", "phone": f"+1555000010{i}"} for i in range(3)]
    real = build_call_plan(businesses, {})
    demo = build_call_plan(businesses, {role: f"+1555000020{i}" for i, role in enumerate(CALL_ORDER)})
    assert tuple(call.role for call in real) == CALL_ORDER
    assert real[0].dial_phone == "+15550000100" and not real[0].demo
    assert demo[0].dial_phone == "+15550000200" and demo[0].demo
    try:
        build_call_plan(businesses, {CALL_ORDER[0]: "+15550000200"})
    except ValueError as exc:
        assert "demo_number_map" in str(exc)
    else:
        raise AssertionError("partial demo map must not leak into real dialing")


def test_sync_supervisor_guarantees_outcome_after_exception():
    planned = build_call_plan([{"name": f"m{i}", "phone": f"+1555000010{i}"} for i in range(3)])[0]

    def crash():
        raise RuntimeError("transport died")

    result = supervise_call(planned, crash, journal_tail=lambda _call_id: [])
    assert result.status is CallStatus.HANGUP
    assert result.call_id == planned.call_id

    result = supervise_call(planned, crash, journal_tail=lambda _id: 1 / 0)
    assert result.status is CallStatus.HANGUP

    wrong = CallOutcome(
        call_id="another-call",
        mover_id=planned.mover_id,
        status="callback",
        transcript_ref="wrong",
    )
    result = supervise_call(planned, lambda: wrong, journal_tail=lambda _id: [])
    assert result.status is CallStatus.HANGUP and result.call_id == planned.call_id


def test_async_supervisor_recovers_latest_journal_outcome():
    planned = build_call_plan([{"name": f"m{i}", "phone": f"+1555000010{i}"} for i in range(3)])[0]
    expected = CallOutcome(call_id=planned.call_id, mover_id=planned.mover_id, status="callback", transcript_ref="tx")

    async def crash():
        raise RuntimeError("socket closed")

    tail = [{"seq": 7, "kind": "call_outcome", "payload": expected.model_dump(mode="json")}]
    result = asyncio.run(supervise_call_async(planned, crash, journal_tail=lambda _id: tail))
    assert result == expected


def test_journal_recovery_uses_newest_partial_events():
    planned = build_call_plan([{"name": f"m{i}", "phone": f"+1555000010{i}"} for i in range(3)])[0]
    tail = [
        {"seq": 1, "kind": "callback", "payload": {"transcript_ref": "old"}},
        {"seq": 2, "kind": "refused", "payload": {"transcript_ref": "new"}},
    ]
    result = supervise_call(planned, lambda: None, journal_tail=lambda _id: tail)
    assert result.status is CallStatus.REFUSED
    assert result.transcript_ref == "new"


def test_lowball_boundary_is_strict_and_report_replays():
    assert is_lowball(699.99, 1000)
    assert not is_lowball(700, 1000)
    assert not is_lowball(700.01, 1000)
    benchmark, fees = load_moving_config()
    records = load_records("negotiator/fixtures/three_calls.json")
    report = build_report(records, benchmark_low=benchmark, fee_names=fees)
    assert report.ranked[0].mover == "Empire Relocation"
    assert "tx-closer:65-74" in report.recommendation_plain
    assert all("#t=" in cite.recording_url for mover in report.ranked for cite in mover.citations)
    assert {flag[:4] for flag in red_flags(records[0], benchmark)} == {
        "RF-A", "RF-B", "RF-C", "RF-E", "RF-F"
    }
    assert "RF-A" not in {flag[:4] for flag in red_flags(replace(records[0], sight_unseen=False), benchmark)}
    assert normalize_total(records[1].outcome) == Decimal("4100")


def test_report_rejects_missing_real_citation():
    benchmark, fees = load_moving_config()
    record = replace(load_records("negotiator/fixtures/three_calls.json")[0], citations=())
    try:
        build_report([record], benchmark_low=benchmark, fee_names=fees)
    except ValueError as exc:
        assert "citation" in str(exc)
    else:
        raise AssertionError("report must not fabricate evidence")


def test_market_passes_prior_quote_evidence_to_later_calls():
    records = load_records("negotiator/fixtures/three_calls.json")
    businesses = [
        {"name": record.outcome.mover_id, "phone": f"+1555000030{index}"}
        for index, record in enumerate(records)
    ]
    evidence_seen = []

    def runner(planned, evidence):
        evidence_seen.append(len(evidence))
        return next(record.outcome for record in records if record.outcome.mover_id == planned.mover_id)

    result = run_market(businesses, runner)
    assert evidence_seen == [0, 1, 2]
    assert len(result.evidence) == 3
