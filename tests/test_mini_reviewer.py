from __future__ import annotations

import json
from pathlib import Path

from jemmin import mini_reviewer
from jemmin.mini_reviewer import MiniReviewerSettings


def test_load_jsonc_supports_line_and_block_comments(tmp_path: Path) -> None:
    config_path = tmp_path / "nitpicker.local.json"
    config_path.write_text(
        """
        {
          // line comment
          "gemini_api_key": "demo-key",
          /* block comment */
          "file_extensions": [".py", ".hpp"]
        }
        """,
        encoding="utf-8",
    )

    loaded = mini_reviewer._load_jsonc(config_path)

    assert loaded["gemini_api_key"] == "demo-key"
    assert loaded["file_extensions"] == [".py", ".hpp"]


def test_review_targets_writes_log_and_latest_artifacts(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "logs" / "mini_reviews.jsonl"
    latest_json_path = tmp_path / "logs" / "latest_review.json"
    latest_text_path = tmp_path / "logs" / "latest_review.txt"

    monkeypatch.setattr(mini_reviewer, "LOG_PATH", log_path)
    monkeypatch.setattr(mini_reviewer, "LATEST_JSON_PATH", latest_json_path)
    monkeypatch.setattr(mini_reviewer, "LATEST_TEXT_PATH", latest_text_path)
    monkeypatch.setattr(mini_reviewer, "targets_from_args", lambda paths, staged: ["src/a.py", "src/b.py"])

    responses = {
        "src/a.py": {
            "result_code": "REVIEW_PASSED",
            "summary": "첫 번째 파일 통과",
            "confidence_score": 1.0,
            "details": [],
            "suggested_patch": None,
            "target_file": "src/a.py",
        },
        "src/b.py": {
            "result_code": "PATCH_PROPOSED",
            "summary": "두 번째 파일 수정 필요",
            "confidence_score": 0.9,
            "details": [{"line_number": 12, "issue": "루프 내부 I/O 제거 필요"}],
            "suggested_patch": "--- a/src/b.py",
            "target_file": "src/b.py",
        },
    }

    def fake_generate_review(path: str, staged: bool, settings: MiniReviewerSettings) -> dict:
        return responses[path]

    monkeypatch.setattr(mini_reviewer, "generate_review", fake_generate_review)

    settings = MiniReviewerSettings(
        gemini_api_key="demo",
        gemini_model="gemini-test",
        watch_path="src",
        debounce_seconds=0.1,
        file_extensions=(".py",),
        skip=False,
    )

    payloads = mini_reviewer.review_targets(["ignored.py"], staged=False, settings=settings)

    assert [payload["target_file"] for payload in payloads] == ["src/a.py", "src/b.py"]

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first_record = json.loads(lines[0])
    second_record = json.loads(lines[1])
    assert first_record["target_file"] == "src/a.py"
    assert second_record["target_file"] == "src/b.py"
    assert "timestamp" in first_record

    latest_payload = json.loads(latest_json_path.read_text(encoding="utf-8"))
    assert latest_payload["target_file"] == "src/b.py"
    assert latest_payload["result_code"] == "PATCH_PROPOSED"

    latest_text = latest_text_path.read_text(encoding="utf-8")
    assert "대상 파일: src/b.py" in latest_text
    assert "결과 코드: PATCH_PROPOSED" in latest_text
    assert "12번째 줄: 루프 내부 I/O 제거 필요" in latest_text


def test_review_targets_skips_cleanly_when_disabled(capsys) -> None:
    settings = MiniReviewerSettings(
        gemini_api_key="",
        gemini_model="",
        watch_path="src",
        debounce_seconds=0.1,
        file_extensions=(".py",),
        skip=True,
    )

    payloads = mini_reviewer.review_targets(["src/a.py"], staged=False, settings=settings)

    captured = capsys.readouterr()
    assert payloads == []
    assert "skipped by NITPICKER_SKIP=1" in captured.out