"""Config Hot-Reload — 파일 mtime 기반 설정 변경 감지.

데몬 재시작 없이 다음 설정 파일 변경을 감지하고 자동 반영합니다:
  - config/system_prompt.md  → PromptLoader가 자체 핫 리로드 (mtime 비교)
  - config/reviewer_config.yaml  → ConfigWatcher가 mtime 비교 후 콜백 호출
  - config/nitpicker.local.json  → ConfigWatcher가 mtime 비교 후 콜백 호출

사용법 (데몬에서)::

    watcher = ConfigWatcher(project_root=ROOT)
    watcher.register("reviewer_config.yaml", on_config_change)
    watcher.register("nitpicker.local.json", on_local_change)

    # 매 리뷰 사이클마다:
    watcher.check()  # 변경된 파일이 있으면 콜백 호출
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

_logger = logging.getLogger(__name__)

__all__ = ["ConfigWatcher"]


class ConfigWatcher:
    """mtime 기반 설정 파일 변경 감지기.

    stat() 호출은 0.01ms 이하이므로 매 리뷰마다 check() 호출해도 무방합니다.
    """

    def __init__(self, project_root: Path | str | None = None) -> None:
        self._root = Path(project_root) if project_root else Path.cwd()
        self._watches: dict[str, _WatchEntry] = {}

    def register(
        self,
        filename: str,
        callback: Callable[[Path, dict[str, Any]], None],
        *,
        subdir: str = "config",
    ) -> None:
        """감시 대상 파일과 변경 시 호출할 콜백을 등록합니다.

        Args:
            filename: 감시할 파일 이름 (e.g. "reviewer_config.yaml")
            callback: 변경 시 호출. (파일 경로, 파싱된 내용 dict) 전달
            subdir: config 하위 디렉터리 (기본 "config")
        """
        path = self._root / subdir / filename
        mtime = path.stat().st_mtime if path.is_file() else 0.0
        self._watches[filename] = _WatchEntry(path=path, mtime=mtime, callback=callback)
        _logger.debug("[ConfigWatcher] 등록: %s (mtime=%.1f)", filename, mtime)

    def check(self) -> list[str]:
        """등록된 모든 파일의 mtime을 체크하고 변경된 파일의 콜백을 호출합니다.

        Returns:
            변경이 감지된 파일 이름 목록.
        """
        changed: list[str] = []
        for filename, entry in self._watches.items():
            if not entry.path.is_file():
                continue
            try:
                current_mtime = entry.path.stat().st_mtime
            except OSError:
                continue

            if current_mtime != entry.mtime:
                _logger.info("[ConfigWatcher] 변경 감지: %s", filename)
                entry.mtime = current_mtime
                content = self._load_file(entry.path)
                try:
                    entry.callback(entry.path, content)
                except (ValueError, KeyError, TypeError, RuntimeError) as exc:
                    _logger.error("[ConfigWatcher] 콜백 오류 (%s): %s", filename, exc)
                changed.append(filename)

        return changed

    @staticmethod
    def _load_file(path: Path) -> dict[str, Any]:
        """파일을 읽어 dict로 반환. YAML과 JSON을 자동 감지합니다."""
        text = path.read_text(encoding="utf-8")
        if path.suffix in (".yaml", ".yml"):
            # yaml 파서가 없으면 간단한 key: value 파싱
            try:
                import yaml  # type: ignore[import-untyped]  # noqa: PLC0415

                return yaml.safe_load(text) or {}
            except ImportError:
                return _simple_yaml_parse(text)
        elif path.suffix == ".json":
            import json  # noqa: PLC0415

            return json.loads(text)
        else:
            return {"_raw": text}


class _WatchEntry:
    """감시 항목."""

    __slots__ = ("path", "mtime", "callback")

    def __init__(
        self,
        path: Path,
        mtime: float,
        callback: Callable[[Path, dict[str, Any]], None],
    ) -> None:
        self.path = path
        self.mtime = mtime
        self.callback = callback


def _simple_yaml_parse(text: str) -> dict[str, Any]:
    """yaml 패키지 없을 때 최소한의 key: value 파싱."""
    result: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result
