"""Unit tests for CodeChangeHandler in mini_nitpicker_daemon.py.

Covers: directory-event filtering, file-extension filtering,
debounce logic, successful review dispatch, and exception safety.

Note: on_modified() dispatches _run_review via threading.Thread. Tests
patch threading.Thread to run synchronously so assertions are race-free.
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from jemmin.mini_reviewer import MiniReviewerSettings

# ---------------------------------------------------------------------------
# Load the daemon module from bin/ without permanently cluttering sys.path.
# conftest.py already adds src/ so jemmin imports inside the daemon succeed.
# ---------------------------------------------------------------------------
_DAEMON_FILE = Path(__file__).resolve().parents[1] / "bin" / "mini_nitpicker_daemon.py"
_spec = importlib.util.spec_from_file_location("mini_nitpicker_daemon", _DAEMON_FILE)
_daemon_module = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_daemon_module)  # type: ignore[union-attr]
CodeChangeHandler = _daemon_module.CodeChangeHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(**overrides) -> MiniReviewerSettings:
    defaults = dict(
        gemini_api_key="demo",
        gemini_model="gemini-test",
        watch_path="src",
        debounce_seconds=0.5,
        file_extensions=(".py",),
        skip=False,
    )
    defaults.update(overrides)
    return MiniReviewerSettings(**defaults)


def _event(src_path: str, *, is_directory: bool = False) -> SimpleNamespace:
    return SimpleNamespace(src_path=src_path, is_directory=is_directory)


def _sync_thread_patch():
    """Patch threading.Thread so target() is called synchronously (race-free assertions)."""
    class _SyncThread:
        def __init__(self, target=None, args=(), daemon=False, **kwargs):
            self._target = target
            self._args = args

        def start(self):
            if self._target:
                self._target(*self._args)

    return patch.object(_daemon_module.threading, "Thread", _SyncThread)


# ---------------------------------------------------------------------------
# Extension & directory filtering
# ---------------------------------------------------------------------------

class TestFiltering:
    def test_directory_events_are_ignored(self) -> None:
        handler = CodeChangeHandler(_settings())
        with _sync_thread_patch():
            with patch.object(handler, "_run_review") as mock_review:
                handler.on_modified(_event("src/some_dir", is_directory=True))
                mock_review.assert_not_called()

    def test_wrong_extension_is_ignored(self) -> None:
        handler = CodeChangeHandler(_settings(file_extensions=(".py",)))
        with _sync_thread_patch():
            with patch.object(handler, "_run_review") as mock_review:
                handler.on_modified(_event("src/styles.css"))
                mock_review.assert_not_called()

    def test_matching_extension_triggers_review(self) -> None:
        handler = CodeChangeHandler(_settings(file_extensions=(".py",)))
        with _sync_thread_patch():
            with patch.object(handler, "_run_review") as mock_review:
                handler.on_modified(_event("src/main.py"))
                mock_review.assert_called_once_with("src/main.py")

    def test_multiple_allowed_extensions(self) -> None:
        handler = CodeChangeHandler(_settings(file_extensions=(".py", ".hpp")))
        with _sync_thread_patch():
            with patch.object(handler, "_run_review") as mock_review:
                handler.on_modified(_event("include/widget.hpp"))
                mock_review.assert_called_once_with("include/widget.hpp")

    def test_created_event_triggers_review(self) -> None:
        handler = CodeChangeHandler(_settings(file_extensions=(".py",)))
        with _sync_thread_patch():
            with patch.object(handler, "_run_review") as mock_review:
                handler.on_created(_event("src/generated.py"))
                mock_review.assert_called_once_with("src/generated.py")

    def test_moved_event_uses_destination_path(self) -> None:
        handler = CodeChangeHandler(_settings(file_extensions=(".py",)))
        move_event = SimpleNamespace(
            src_path="src/.tmp123",
            dest_path="src/final.py",
            is_directory=False,
        )
        with _sync_thread_patch():
            with patch.object(handler, "_run_review") as mock_review:
                handler.on_moved(move_event)
                mock_review.assert_called_once_with("src/final.py")

    def test_moved_event_to_non_matching_extension_is_ignored(self) -> None:
        handler = CodeChangeHandler(_settings(file_extensions=(".py",)))
        move_event = SimpleNamespace(
            src_path="src/.tmp123",
            dest_path="src/final.tmp",
            is_directory=False,
        )
        with _sync_thread_patch():
            with patch.object(handler, "_run_review") as mock_review:
                handler.on_moved(move_event)
                mock_review.assert_not_called()


# ---------------------------------------------------------------------------
# Debounce logic
# ---------------------------------------------------------------------------

class TestDebounce:
    def test_second_event_within_window_is_suppressed(self) -> None:
        handler = CodeChangeHandler(_settings(debounce_seconds=10.0))
        with _sync_thread_patch():
            with patch.object(handler, "_run_review") as mock_review:
                event = _event("src/main.py")
                handler.on_modified(event)
                handler.on_modified(event)   # same file, same moment → debounced
                mock_review.assert_called_once()

    def test_event_after_window_triggers_review_again(self) -> None:
        handler = CodeChangeHandler(_settings(debounce_seconds=0.0))
        with _sync_thread_patch():
            with patch.object(handler, "_run_review") as mock_review:
                event = _event("src/main.py")
                handler.on_modified(event)
                time.sleep(0.01)
                handler.on_modified(event)
                assert mock_review.call_count == 2

    def test_different_files_have_independent_debounce_windows(self) -> None:
        handler = CodeChangeHandler(_settings(debounce_seconds=10.0))
        with _sync_thread_patch():
            with patch.object(handler, "_run_review") as mock_review:
                handler.on_modified(_event("src/a.py"))
                handler.on_modified(_event("src/b.py"))
                assert mock_review.call_count == 2


# ---------------------------------------------------------------------------
# _run_review dispatch
# ---------------------------------------------------------------------------

class TestRunReview:
    def test_successful_review_prints_summary(self, capsys) -> None:
        handler = CodeChangeHandler(_settings())
        payloads = [
            {
                "result_code": "REVIEW_PASSED",
                "target_file": "src/main.py",
                "summary": "모든 검사 통과",
                "confidence_score": 1.0,
                "details": [],
                "suggested_patch": None,
            }
        ]
        with patch.object(_daemon_module, "review_targets", return_value=payloads):
            with patch.object(_daemon_module, "format_review_summary", return_value="[PASSED] 모든 검사 통과"):
                handler._run_review("src/main.py")

        out = capsys.readouterr().out
        assert "reviewing src/main.py" in out
        assert "[PASSED] 모든 검사 통과" in out

    def test_empty_payloads_skips_artifact_log_lines(self, capsys) -> None:
        handler = CodeChangeHandler(_settings())
        with patch.object(_daemon_module, "review_targets", return_value=[]):
            handler._run_review("src/main.py")

        out = capsys.readouterr().out
        assert "review log written" not in out

    def test_exception_propagates_for_fail_fast(self) -> None:
        """Exceptions from review_targets must propagate (Fail-Fast); watchdog catches them."""
        handler = CodeChangeHandler(_settings())
        with patch.object(_daemon_module, "review_targets", side_effect=RuntimeError("API timeout")):
            import pytest
            with pytest.raises(RuntimeError, match="API timeout"):
                handler._run_review("src/main.py")
