import json
from pathlib import Path

from negotiator.tools.latency_report import build_report, percentile, read_latency_samples, report_file


FIXTURE = Path(__file__).parents[1] / "negotiator" / "fixtures" / "latency_smoke.jsonl"


def test_latency_report_from_stages_and_explicit_spans() -> None:
    samples = read_latency_samples(FIXTURE)
    assert samples["stt_final"] == [90.0]
    assert samples["llm_ttft"] == [230.0]
    assert samples["tts_ttfb"] == [130.0]
    assert samples["transport"] == [50.0]
    assert samples["mouth_to_ear"] == [1100.0, 500.0]
    report = report_file(FIXTURE)
    assert report.milestone_met is True
    assert report.segments["mouth_to_ear"].median_ms == 800.0


def test_percentile_interpolates_and_unknown_segments_have_no_target() -> None:
    assert percentile([0.0, 100.0], 0.95) == 95.0
    stats = build_report({"vendor_queue": [10, 20, 30]}).segments["vendor_queue"]
    assert stats.median_ms == 20
    assert stats.target_met is None
