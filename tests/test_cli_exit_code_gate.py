from __future__ import annotations

from typing import Any

import pytest

from bin import jemmin_cli


@pytest.mark.parametrize(
    ("result_code", "expected"),
    [
        ("REVIEW_PASSED", 0),
        ("REVIEW_REJECTED", 1),
        ("PATCH_PROPOSED", 1),
        ("PATCH_REQUIRED", 1),
        ("PATCH_VERIFY_FAILED", 1),
        ("POLICY_REJECTED", 1),
        ("RESOURCE_REJECTED", 1),
        ("LLM_TIMEOUT", 1),
        ("SYSTEM_FAILED", 1),
        ("DUPLICATE_REQUEST_IGNORED", 1),
        ("NEW_RESULT_CODE", 1),
        (None, 1),
    ],
)
def test_exit_code_mapping_fails_closed(result_code: str | None, expected: int) -> None:
    assert jemmin_cli._exit_code_from_result_code(result_code) == expected


@pytest.mark.parametrize(
    ("result_code", "expected_token"),
    [
        ("REVIEW_PASSED", "JEMMIN_RESULT_CODE=REVIEW_PASSED"),
        ("REVIEW_REJECTED", "JEMMIN_RESULT_CODE=REVIEW_REJECTED"),
        ("DUPLICATE_REQUEST_IGNORED", "JEMMIN_RESULT_CODE=DUPLICATE_REQUEST_IGNORED"),
        (None, "JEMMIN_RESULT_CODE=UNKNOWN"),
    ],
)
def test_emit_result_code_prints_exact_single_token(
    result_code: str | None,
    expected_token: str,
    capsys,
) -> None:
    assert jemmin_cli._emit_result_code(result_code) == expected_token
    assert capsys.readouterr().out.splitlines() == [expected_token]


def test_run_direct_returns_nonzero_for_security_reject(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(jemmin_cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        jemmin_cli,
        "_LOCAL_CONFIG_PATH",
        tmp_path / "config" / "nitpicker.local.json",
    )

    diff = "\n".join(
        [
            "diff --git a/secret.py b/secret.py",
            "new file mode 100644",
            "+++ b/secret.py",
            '+password = "abcd"',
        ]
    )

    rc = jemmin_cli._run_direct("secret.py", diff, provider_name="mock")

    assert rc == 1
    stdout = capsys.readouterr().out
    assert "delivered rejected" in stdout
    assert stdout.splitlines().count("JEMMIN_RESULT_CODE=REVIEW_REJECTED") == 1
    latest = tmp_path / ".jemmin" / "logs" / "LATEST_REVIEW.txt"
    assert "결과 코드: REVIEW_REJECTED" in latest.read_text(encoding="utf-8")


def test_run_direct_returns_zero_for_review_passed(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(jemmin_cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        jemmin_cli,
        "_LOCAL_CONFIG_PATH",
        tmp_path / "config" / "nitpicker.local.json",
    )

    diff = "\n".join(
        [
            "diff --git a/ok.py b/ok.py",
            "new file mode 100644",
            "+++ b/ok.py",
            "+value = 42",
        ]
    )

    rc = jemmin_cli._run_direct("ok.py", diff, provider_name="mock")

    assert rc == 0
    stdout = capsys.readouterr().out
    assert "delivered pass" in stdout
    assert stdout.splitlines().count("JEMMIN_RESULT_CODE=REVIEW_PASSED") == 1
    latest = tmp_path / ".jemmin" / "logs" / "LATEST_REVIEW.txt"
    assert "결과 코드: REVIEW_PASSED" in latest.read_text(encoding="utf-8")


def test_run_direct_duplicate_emits_machine_code_once(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(jemmin_cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        jemmin_cli,
        "_LOCAL_CONFIG_PATH",
        tmp_path / "config" / "nitpicker.local.json",
    )

    assert jemmin_cli._run_direct("ok.py", "+value = 42\n", provider_name="mock") == 0
    capsys.readouterr()

    rc = jemmin_cli._run_direct("ok.py", "+value = 42\n", provider_name="mock")

    assert rc == 1
    stdout = capsys.readouterr().out
    assert "delivered ignored" in stdout
    assert stdout.splitlines().count("JEMMIN_RESULT_CODE=DUPLICATE_REQUEST_IGNORED") == 1


@pytest.mark.parametrize(
    ("result_code", "expected_exit", "expected_token"),
    [
        ("REVIEW_PASSED", 0, "JEMMIN_RESULT_CODE=REVIEW_PASSED"),
        ("REVIEW_REJECTED", 1, "JEMMIN_RESULT_CODE=REVIEW_REJECTED"),
        ("DUPLICATE_REQUEST_IGNORED", 1, "JEMMIN_RESULT_CODE=DUPLICATE_REQUEST_IGNORED"),
        (None, 1, "JEMMIN_RESULT_CODE=UNKNOWN"),
        ("NEW_RESULT_CODE", 1, "JEMMIN_RESULT_CODE=NEW_RESULT_CODE"),
    ],
)
def test_run_via_daemon_uses_result_code_gate(
    monkeypatch,
    capsys,
    result_code: str | None,
    expected_exit: int,
    expected_token: str,
) -> None:
    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def send(self, payload):
            return {
                "status": "success",
                "response": {
                    "state": "delivered",
                    "status": "pass" if result_code == "REVIEW_PASSED" else "ignored",
                    "summary": "human summary",
                    "result_code": result_code,
                },
            }

    monkeypatch.setattr(jemmin_cli, "ZmqClient", FakeClient)

    rc = jemmin_cli._run_via_daemon("ok.py", "+value = 42\n")

    assert rc == expected_exit
    stdout = capsys.readouterr().out
    assert "delivered" in stdout and "human summary" in stdout
    assert stdout.splitlines().count(expected_token) == 1


def test_run_via_daemon_error_has_no_result_code_token(monkeypatch, capsys) -> None:
    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def send(self, payload):
            return {
                "status": "error",
                "error_message": "daemon rejected request",
            }

    monkeypatch.setattr(jemmin_cli, "ZmqClient", FakeClient)

    rc = jemmin_cli._run_via_daemon("ok.py", "+value = 42\n")

    captured = capsys.readouterr()
    assert rc == 1
    assert "daemon rejected request" in captured.err
    assert "JEMMIN_RESULT_CODE=" not in captured.out
    assert "JEMMIN_RESULT_CODE=" not in captured.err


def test_run_direct_uses_runtime_dir_override(tmp_path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    runtime_dir = tmp_path / "isolated" / "run-1"
    monkeypatch.setattr(jemmin_cli, "ROOT", engine_root)
    monkeypatch.setattr(
        jemmin_cli,
        "_LOCAL_CONFIG_PATH",
        engine_root / "config" / "nitpicker.local.json",
    )
    monkeypatch.setenv("JEMMIN_RUNTIME_DIR", str(runtime_dir))

    rc = jemmin_cli._run_direct("ok.py", "+value = 42\n", provider_name="mock")

    assert rc == 0
    assert (runtime_dir / "spool.db").is_file()
    assert (runtime_dir / "analytics.duckdb").is_file()
    assert (runtime_dir / "logs" / "LATEST_REVIEW.txt").is_file()
    assert not (engine_root / ".jemmin" / "spool.db").exists()
    assert not (engine_root / ".jemmin" / "analytics.duckdb").exists()


def test_run_direct_falls_back_to_global_runtime_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(jemmin_cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        jemmin_cli,
        "_LOCAL_CONFIG_PATH",
        tmp_path / "config" / "nitpicker.local.json",
    )
    monkeypatch.delenv("JEMMIN_RUNTIME_DIR", raising=False)

    rc = jemmin_cli._run_direct("ok.py", "+value = 42\n", provider_name="mock")

    assert rc == 0
    assert (tmp_path / ".jemmin" / "spool.db").is_file()
    assert (tmp_path / ".jemmin" / "analytics.duckdb").is_file()


def test_run_direct_closes_analytics_logger_on_success(tmp_path, monkeypatch) -> None:
    instances: list[Any] = []

    class SpyLogger:
        def __init__(self, db_path) -> None:
            self.db_path = db_path
            self.closed = False
            instances.append(self)

        def write(self, payload) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(jemmin_cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        jemmin_cli,
        "_LOCAL_CONFIG_PATH",
        tmp_path / "config" / "nitpicker.local.json",
    )
    monkeypatch.setattr(jemmin_cli, "DuckDbLogger", SpyLogger)

    rc = jemmin_cli._run_direct("ok.py", "+value = 42\n", provider_name="mock")

    assert rc == 0
    assert len(instances) == 1
    assert instances[0].closed is True


def test_run_direct_closes_analytics_logger_on_exception(tmp_path, monkeypatch) -> None:
    instances: list[Any] = []

    class SpyLogger:
        def __init__(self, db_path) -> None:
            self.db_path = db_path
            self.closed = False
            instances.append(self)

        def write(self, payload) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class RaisingOrchestrator:
        def __init__(self, **kwargs) -> None:
            return None

        def run_once(self, request):
            raise RuntimeError("boom")

    monkeypatch.setattr(jemmin_cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        jemmin_cli,
        "_LOCAL_CONFIG_PATH",
        tmp_path / "config" / "nitpicker.local.json",
    )
    monkeypatch.setattr(jemmin_cli, "DuckDbLogger", SpyLogger)
    monkeypatch.setattr(jemmin_cli, "ReviewOrchestrator", RaisingOrchestrator)

    with pytest.raises(RuntimeError, match="boom"):
        jemmin_cli._run_direct("ok.py", "+value = 42\n", provider_name="mock")

    assert len(instances) == 1
    assert instances[0].closed is True
