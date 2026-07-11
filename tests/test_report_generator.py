"""Phase VI-D: ReportGenerator 테스트.

§1 ReportGenerator 단위 테스트               (6 tests)
Total: 6 tests
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from jemmin.services.report_generator import ReportGenerator
from jemmin.utils.duckdb_logger import DuckDbLogger


def _logger_with_data() -> DuckDbLogger:
    """테스트용 in-memory DuckDB에 샘플 데이터를 넣고 반환."""
    logger = DuckDbLogger(db_path=":memory:")
    events = [
        {"request_id": "r1", "event_type": "pipeline_complete", "status": "pass", "result_code": "REVIEW_PASSED", "confidence_score": 0.95},
        {"request_id": "r2", "event_type": "pipeline_complete", "status": "rejected", "result_code": "REVIEW_REJECTED", "confidence_score": 0.8},
        {"request_id": "r3", "event_type": "pipeline_complete", "status": "pass", "result_code": "REVIEW_PASSED", "confidence_score": 0.9},
        {"request_id": "r2", "event_type": "l3_review_complete", "status": "done", "result_code": "REVIEW_REJECTED", "latency_ms": 3200},
        {"request_id": "r3", "event_type": "l3_review_complete", "status": "done", "result_code": "REVIEW_PASSED", "latency_ms": 2800},
        {"request_id": "r1", "event_type": "agents_run", "status": "done", "agent_name": "fast_gate", "latency_ms": 10},
    ]
    for e in events:
        logger.write(e)
    return logger


class TestReportGenerator:
    def test_summary_stats_counts(self):
        logger = _logger_with_data()
        report = ReportGenerator(logger)
        stats = report.summary_stats()
        assert stats["total_events"] == 6
        assert stats["unique_requests"] == 3
        logger.close()

    def test_results_distribution(self):
        logger = _logger_with_data()
        stats = ReportGenerator(logger).summary_stats()
        assert stats["results"]["REVIEW_PASSED"] == 2
        assert stats["results"]["REVIEW_REJECTED"] == 1
        logger.close()

    def test_l3_stats(self):
        logger = _logger_with_data()
        stats = ReportGenerator(logger).summary_stats()
        assert stats["l3_count"] == 2
        assert stats["l3_avg_latency_ms"] == 3000.0
        logger.close()

    def test_text_report_format(self):
        logger = _logger_with_data()
        text = ReportGenerator(logger).text_report()
        assert "Nitpicker Review Report" in text
        assert "REVIEW_PASSED" in text
        assert "REVIEW_REJECTED" in text
        logger.close()

    def test_html_report_creates_file(self):
        logger = _logger_with_data()
        with tempfile.TemporaryDirectory() as tmp:
            out = ReportGenerator(logger).html_report(Path(tmp) / "report.html")
            assert out.is_file()
            content = out.read_text(encoding="utf-8")
            assert "<!DOCTYPE html>" in content
            assert "REVIEW_PASSED" in content
            assert "REVIEW_REJECTED" in content
        logger.close()

    def test_empty_db_no_error(self):
        logger = DuckDbLogger(db_path=":memory:")
        report = ReportGenerator(logger)
        text = report.text_report()
        assert "Nitpicker Review Report" in text
        stats = report.summary_stats()
        assert stats["total_events"] == 0
        logger.close()
