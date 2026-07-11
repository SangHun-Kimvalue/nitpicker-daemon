from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

# Windows cp949 인코딩에서 유니코드 출력 깨짐 방지
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jemmin.mini_reviewer import (
    LATEST_JSON_PATH,
    LATEST_TEXT_PATH,
    LOG_PATH,
    format_review_summary,
    load_settings,
    review_targets,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()

    try:
        settings = load_settings()
        payloads = review_targets(args.paths, staged=args.staged, settings=settings)
    except Exception as error:
        print(f"[mini_nitpicker] {error}", file=sys.stderr)
        return 2

    failures: list[str] = []
    outputs: list[str] = []
    for payload in payloads:
        outputs.append(format_review_summary(payload))
        outputs.append(json.dumps(payload, ensure_ascii=False, indent=2))
        if payload.get("result_code") != "REVIEW_PASSED":
            failures.append(str(payload.get("target_file", "unknown")))
    if outputs:
        print("\n".join(outputs))
    if payloads:
        print(f"[mini_nitpicker] review log written to {LOG_PATH}")
        print(f"[mini_nitpicker] latest json written to {LATEST_JSON_PATH}")
        print(f"[mini_nitpicker] latest text written to {LATEST_TEXT_PATH}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())