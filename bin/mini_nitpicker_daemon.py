from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Collection, Protocol

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

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


class DaemonSettings(Protocol):
    file_extensions: Collection[str]
    debounce_seconds: float
    watch_path: str


class CodeChangeHandler(FileSystemEventHandler):
    def __init__(self, settings: DaemonSettings) -> None:
        self._settings = settings
        self._last_modified: dict[str, float] = {}
        self._lock = threading.Lock()
        self._review_lock = threading.Lock()

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle_path_event(event.src_path, is_directory=event.is_directory)

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle_path_event(event.src_path, is_directory=event.is_directory)

    def on_moved(self, event: FileSystemEvent) -> None:
        dest_path: str = getattr(event, "dest_path", event.src_path)
        self._handle_path_event(dest_path, is_directory=event.is_directory)

    def _handle_path_event(self, path: str, *, is_directory: bool) -> None:
        if is_directory:
            return
        suffix: str = Path(path).suffix.lower()
        if suffix not in self._settings.file_extensions:
            return
        now: float = time.time()
        with self._lock:
            previous: float = self._last_modified.get(path, 0.0)
            if now - previous < self._settings.debounce_seconds:
                return
            self._last_modified[path] = now
        threading.Thread(target=self._run_review, args=(path,), daemon=True).start()

    def _run_review(self, path: str) -> None:
        with self._review_lock:
            print(f"[mini_nitpicker_daemon] reviewing {path}")
            payloads: list[Any] = review_targets([path], staged=False, settings=self._settings)
            if payloads:
                outputs: list[str] = []
                for payload in payloads:
                    outputs.append(format_review_summary(payload))
                    outputs.append(json.dumps(payload, ensure_ascii=False, indent=2))
                print("\n".join(outputs))
                print(f"[mini_nitpicker_daemon] review log written to {LOG_PATH}")
                print(f"[mini_nitpicker_daemon] latest json written to {LATEST_JSON_PATH}")
                print(f"[mini_nitpicker_daemon] latest text written to {LATEST_TEXT_PATH}")


def main() -> int:
    try:
        settings: DaemonSettings = load_settings()
    except Exception as error:
        print(f"[mini_nitpicker_daemon] {error}", file=sys.stderr)
        return 2

    watch_path: Path = (ROOT / settings.watch_path).resolve()
    if not watch_path.exists():
        print(f"[mini_nitpicker_daemon] watch path does not exist: {watch_path}", file=sys.stderr)
        return 2

    observer: Observer = Observer()
    observer.schedule(CodeChangeHandler(settings), str(watch_path), recursive=True)
    observer.start()

    print(f"[mini_nitpicker_daemon] watching {watch_path}")
    print("[mini_nitpicker_daemon] press Ctrl+C to stop")
    try:
        while observer.is_alive():
            observer.join(1)
    except KeyboardInterrupt:
        observer.stop()
        print("[mini_nitpicker_daemon] stopping")
    observer.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())