"""ReportGenerator — DuckDB analytics 기반 리뷰 리포트 생성.

DuckDB에 쌓인 review_events 데이터로 CLI 텍스트 리포트 또는 HTML 리포트를 생성합니다.

사용법::

    from jemmin.utils.duckdb_logger import DuckDbLogger
    from jemmin.services.report_generator import ReportGenerator

    logger = DuckDbLogger(db_path=".jemmin/analytics.duckdb")
    report = ReportGenerator(logger)
    print(report.text_report())       # CLI 텍스트 리포트
    report.html_report("report.html") # HTML 파일 생성
"""
from __future__ import annotations

import html as html_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

__all__ = ["ReportGenerator"]

_KST = timezone(timedelta(hours=9), name="KST")


class ReportGenerator:
    """DuckDB analytics 데이터로 리뷰 리포트를 생성합니다."""

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def summary_stats(self) -> dict[str, Any]:
        """핵심 통계를 dict로 반환."""
        rows = self._logger.query(
            "SELECT COUNT(*) AS total, "
            "COUNT(DISTINCT request_id) AS unique_requests "
            "FROM review_events"
        )
        total = rows[0]["total"] if rows else 0
        unique = rows[0]["unique_requests"] if rows else 0

        result_rows = self._logger.query(
            "SELECT result_code, COUNT(*) AS cnt "
            "FROM review_events "
            "WHERE event_type = 'pipeline_complete' "
            "GROUP BY result_code ORDER BY cnt DESC"
        )
        results: dict[str, int] = {r["result_code"]: r["cnt"] for r in result_rows}

        agent_rows = self._logger.query(
            "SELECT agent_name, COUNT(*) AS cnt, AVG(latency_ms) AS avg_latency "
            "FROM review_events "
            "WHERE event_type = 'agents_run' AND agent_name != '' "
            "GROUP BY agent_name ORDER BY cnt DESC"
        )

        top_rejected = self._logger.query(
            "SELECT request_id, result_code, recorded_at "
            "FROM review_events "
            "WHERE event_type = 'pipeline_complete' AND result_code != 'REVIEW_PASSED' "
            "ORDER BY recorded_at DESC LIMIT 10"
        )

        l3_rows = self._logger.query(
            "SELECT COUNT(*) AS cnt, AVG(latency_ms) AS avg_latency "
            "FROM review_events "
            "WHERE event_type = 'l3_review_complete'"
        )
        l3_count = l3_rows[0]["cnt"] if l3_rows else 0
        l3_avg_latency = l3_rows[0]["avg_latency"] if l3_rows and l3_rows[0]["avg_latency"] else 0.0

        return {
            "total_events": total,
            "unique_requests": unique,
            "results": results,
            "agent_stats": agent_rows,
            "top_rejected": top_rejected,
            "l3_count": l3_count,
            "l3_avg_latency_ms": l3_avg_latency,
        }

    def text_report(self) -> str:
        """CLI 텍스트 리포트를 생성합니다."""
        stats = self.summary_stats()
        kst_now = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S KST")
        lines: list[str] = []

        lines.append("=" * 60)
        lines.append(f"  Nitpicker Review Report  ({kst_now})")
        lines.append("=" * 60)
        lines.append("")

        # 개요
        lines.append(f"  총 이벤트: {stats['total_events']}")
        lines.append(f"  고유 요청: {stats['unique_requests']}")
        lines.append(f"  L3 LLM 리뷰: {stats['l3_count']}건 (평균 {stats['l3_avg_latency_ms']:.0f}ms)")
        lines.append("")

        # 결과 분포
        lines.append("  --- 결과 분포 ---")
        for code, cnt in stats["results"].items():
            icon = "\u2705" if code == "REVIEW_PASSED" else "\u274c"
            lines.append(f"  {icon} {code}: {cnt}")
        lines.append("")

        # 최근 REJECT
        if stats["top_rejected"]:
            lines.append("  --- 최근 거부 ---")
            for r in stats["top_rejected"][:5]:
                ts = (r.get("recorded_at") or "")[:19]
                lines.append(f"  {ts}  {r.get('result_code', '')}  {r.get('request_id', '')}")
            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)

    def html_report(self, output_path: str | Path) -> Path:
        """HTML 리포트 파일을 생성합니다."""
        stats = self.summary_stats()
        kst_now = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S KST")
        h = html_mod.escape

        results_html = ""
        for code, cnt in stats["results"].items():
            color = "#36a64f" if code == "REVIEW_PASSED" else "#dc3545"
            results_html += f'<tr><td style="color:{color};font-weight:bold">{h(code)}</td><td>{cnt}</td></tr>\n'

        rejected_html = ""
        for r in stats.get("top_rejected", [])[:10]:
            ts = h((r.get("recorded_at") or "")[:19])
            rejected_html += (
                f"<tr><td>{ts}</td>"
                f"<td>{h(r.get('result_code', ''))}</td>"
                f"<td>{h(r.get('request_id', ''))}</td></tr>\n"
            )

        page = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>Nitpicker Review Report</title>
<style>
  body {{ font-family: -apple-system, 'Malgun Gothic', sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ border-bottom: 2px solid #333; padding-bottom: .5rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ border: 1px solid #ddd; padding: .5rem .75rem; text-align: left; }}
  th {{ background: #f5f5f5; }}
  .stat {{ display: inline-block; margin: .5rem 1.5rem .5rem 0; }}
  .stat-value {{ font-size: 1.5rem; font-weight: bold; }}
  .stat-label {{ color: #666; font-size: .85rem; }}
</style>
</head>
<body>
<h1>Nitpicker Review Report</h1>
<p>{h(kst_now)}</p>

<div>
  <div class="stat"><div class="stat-value">{stats['total_events']}</div><div class="stat-label">총 이벤트</div></div>
  <div class="stat"><div class="stat-value">{stats['unique_requests']}</div><div class="stat-label">고유 요청</div></div>
  <div class="stat"><div class="stat-value">{stats['l3_count']}</div><div class="stat-label">L3 LLM 리뷰</div></div>
</div>

<h2>결과 분포</h2>
<table><tr><th>Result Code</th><th>Count</th></tr>
{results_html}</table>

<h2>최근 거부</h2>
<table><tr><th>Time</th><th>Result</th><th>Request ID</th></tr>
{rejected_html}</table>

<footer style="margin-top:2rem;color:#999;font-size:.8rem">Generated by Nitpicker Daemon</footer>
</body>
</html>"""

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, encoding="utf-8")
        return out
