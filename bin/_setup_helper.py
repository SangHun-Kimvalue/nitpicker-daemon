"""Set_Env.bat / Run_Mode*.bat helper — cmd if-블록 안에 python -c를 쓰면
CMD가 닫는 괄호를 오파싱하므로, 별도 스크립트로 분리합니다.

Usage:
    python bin/_setup_helper.py --show-provider
    python bin/_setup_helper.py --set-provider ollama
    python bin/_setup_helper.py --show-review
    python bin/_setup_helper.py --count-reviews
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_YAML  = ROOT / "config" / "reviewer_config.yaml"
LATEST_JSON  = ROOT / ".jemmin" / "logs" / "latest_review.json"
REVIEWS_JSONL = ROOT / ".jemmin" / "logs" / "mini_reviews.jsonl"


# ── provider config ────────────────────────────────────────────────────────

def _read_provider() -> str:
    if not CONFIG_YAML.exists():
        return "unknown"
    for line in CONFIG_YAML.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s+default:\s+(\S+)", line)   # 들여쓰기 있는 provider.default
        if m:
            return m.group(1)
    return "unknown"


def set_provider(name: str) -> None:
    if not CONFIG_YAML.exists():
        print(f"[setup] config not found: {CONFIG_YAML}")
        return
    lines = CONFIG_YAML.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if re.match(r"\s+default:\s+\S+", line):
            lines[i] = re.sub(r"(default:\s+)\S+", rf"\g<1>{name}", line)
            changed = True
            break
    if changed:
        CONFIG_YAML.write_text("".join(lines), encoding="utf-8")
        print(f"  reviewer_config.yaml : default provider -> {name}")
    else:
        print("  reviewer_config.yaml : 'default:' line not found (no change)")


def show_provider() -> None:
    provider = _read_provider()
    labels = {
        "mock":   "Mock   (테스트용, API 키 불필요)",
        "ollama": "Ollama (로컬 LLM, 비용 0원)",
        "gemini": "Gemini (클라우드, API 과금)",
    }
    print(f"  Provider config : {provider}  {labels.get(provider, '')}")


# ── review log helpers ─────────────────────────────────────────────────────

def show_review() -> None:
    """latest_review.json 의 result_code / summary 출력."""
    if not LATEST_JSON.exists():
        return
    try:
        d = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
        print(f"Result  : {d.get('result_code', '')}")
        print(f"Summary : {d.get('summary', '')}")
    except Exception as exc:
        print(f"[setup] failed to read latest_review.json: {exc}")


def count_reviews() -> None:
    """mini_reviews.jsonl 의 총 리뷰 수 출력."""
    if not REVIEWS_JSONL.exists():
        print("No review log found.")
        return
    try:
        count = sum(1 for _ in REVIEWS_JSONL.open(encoding="utf-8"))
        print(f"Total reviews: {count} entries")
        print(f"Log file: {REVIEWS_JSONL}")
    except Exception as exc:
        print(f"[setup] failed to count reviews: {exc}")


# ── entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--set-provider" in args:
        set_provider(args[args.index("--set-provider") + 1])
    elif "--show-provider" in args:
        show_provider()
    elif "--show-review" in args:
        show_review()
    elif "--count-reviews" in args:
        count_reviews()
