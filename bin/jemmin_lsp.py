"""jemmin_lsp.py — minimal stdio Language Server Protocol (LSP) server.

Watches .jemmin/logs/latest_review.json and pushes
textDocument/publishDiagnostics notifications to the connected LSP client
(VS Code) whenever the review result changes.

Phase I: textDocument/codeAction — Quick-Fix code actions for review findings.

Protocol: JSON-RPC 2.0 over stdio (newline-delimited Content-Length frames).
No pygls dependency — implemented with stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# JSON-RPC framing helpers
# ---------------------------------------------------------------------------


def _read_message(stream: Any) -> dict[str, Any] | None:
    """Read one Content-Length-framed JSON-RPC message from *stream*."""
    content_length = 0
    while True:
        header_line = stream.readline()
        if not header_line:
            return None  # EOF
        header_line = header_line.strip()
        if header_line == "" or header_line == b"":
            break
        if isinstance(header_line, bytes):
            header_line = header_line.decode("utf-8", errors="replace")
        if header_line.lower().startswith("content-length:"):
            content_length = int(header_line.split(":", 1)[1].strip())
    if content_length == 0:
        return None
    body = stream.read(content_length)
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    return json.loads(body)


def _write_message(msg: dict[str, Any]) -> None:
    """Write a Content-Length-framed JSON-RPC message to stdout (binary)."""
    body = json.dumps(msg, ensure_ascii=False)
    encoded = body.encode("utf-8")
    header = f"Content-Length: {len(encoded)}\r\n\r\n"
    sys.stdout.buffer.write(header.encode("ascii") + encoded)
    sys.stdout.buffer.flush()


def _notify(method: str, params: Any) -> None:
    _write_message({"jsonrpc": "2.0", "method": method, "params": params})


def _respond(req_id: Any, result: Any) -> None:
    _write_message({"jsonrpc": "2.0", "id": req_id, "result": result})


# ---------------------------------------------------------------------------
# Diagnostic conversion
# ---------------------------------------------------------------------------

_SEVERITY_MAP = {
    "error": 1,
    "warning": 2,
    "warn": 2,
    "information": 3,
    "info": 3,
    "hint": 4,
}


def _review_to_diagnostics(review: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Convert a latest_review.json dict into (uri, diagnostics_list).

    Each diagnostic gets a ``data`` field with agent/code metadata so that
    textDocument/codeAction can match it back to quick-fix entries.
    """
    target_file: str = review.get("target_file", "")
    if not target_file:
        return "", []
    uri = Path(target_file).as_uri() if not target_file.startswith("file://") else target_file

    diagnostics: list[dict[str, Any]] = []
    for detail in review.get("details", []):
        line_no = max(0, (detail.get("line_number") or 1) - 1)
        severity_str: str = str(detail.get("severity", "warning")).lower()
        severity = _SEVERITY_MAP.get(severity_str, 2)
        message = str(detail.get("issue") or detail.get("message") or "review finding")
        code = detail.get("code", "")
        agent = detail.get("agent", "")

        diagnostics.append(
            {
                "range": {
                    "start": {"line": line_no, "character": 0},
                    "end": {"line": line_no, "character": 9999},
                },
                "severity": severity,
                "source": f"jemmin/{agent}" if agent else "jemmin",
                "message": message,
                "code": code,
                "data": {"agent": agent, "code": code, "line_number": line_no},
            }
        )
    return uri, diagnostics


# ---------------------------------------------------------------------------
# Code Action support (Phase I)
# ---------------------------------------------------------------------------

# In-memory cache of the latest review JSON — updated by the watcher thread.
_review_lock = threading.Lock()
_latest_review: dict[str, Any] = {}


def _build_code_actions(
    uri: str,
    request_range: dict[str, Any],
    context_diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build LSP CodeAction items from the latest review's code_actions.

    Matches code_actions by line range and returns:
    - quickfix.suppress: add ``# noqa: CODE`` to the offending line
    - quickfix.patch: apply a verified unified-diff patch via workspace/applyEdit
    - quickfix: apply a suggested fix inline
    """
    with _review_lock:
        review = dict(_latest_review)

    code_actions_data: list[dict[str, Any]] = review.get("code_actions", [])
    if not code_actions_data:
        return []

    start_pos = request_range.get("start") or {}
    req_start_line: int = start_pos.get("line", 0)
    end_pos = request_range.get("end") or {}
    req_end_line: int = end_pos.get("line", req_start_line)

    actions: list[dict[str, Any]] = []

    for ca in code_actions_data:
        kind: str = ca.get("kind", "quickfix")
        title: str = ca.get("title", "Quick Fix")

        # Patch-level code action: always include regardless of line range
        if kind == "quickfix.patch":
            edits: list[dict[str, Any]] = ca.get("edits", [])
            text_edits: list[dict[str, Any]] = []
            for hunk in edits:
                text_edits.append({
                    "range": {
                        "start": {"line": hunk["start_line"], "character": 0},
                        "end": {"line": hunk["end_line"], "character": 0},
                    },
                    "newText": "\n".join(hunk.get("new_lines", [])) + "\n",
                })

            actions.append({
                "title": title,
                "kind": "quickfix",
                "diagnostics": context_diagnostics,
                "isPreferred": True,
                "edit": {"changes": {uri: text_edits}} if text_edits else {},
            })
            continue

        # Line-scoped actions: only include if the action's line is within range
        action_line: int = (ca.get("line_number") or 1) - 1  # 0-based
        if not (req_start_line <= action_line <= req_end_line):
            continue

        if kind == "quickfix.suppress":
            suppress_code: str = ca.get("suppress_code", "")
            actions.append({
                "title": title,
                "kind": "quickfix",
                "diagnostics": [
                    d for d in context_diagnostics
                    if isinstance(d.get("data"), dict) and d["data"].get("code") == suppress_code
                ],
                "edit": {
                    "changes": {
                        uri: [{
                            "range": {
                                "start": {"line": action_line, "character": 9999},
                                "end": {"line": action_line, "character": 9999},
                            },
                            "newText": f"  # noqa: {suppress_code}",
                        }]
                    }
                },
            })
        elif "edit_text" in ca:
            actions.append({
                "title": title,
                "kind": "quickfix",
                "diagnostics": context_diagnostics,
                "edit": {
                    "changes": {
                        uri: [{
                            "range": {
                                "start": {"line": action_line, "character": 0},
                                "end": {"line": action_line + 1, "character": 0},
                            },
                            "newText": ca["edit_text"] + "\n",
                        }]
                    }
                },
            })

    return actions


# ---------------------------------------------------------------------------
# Review log watcher
# ---------------------------------------------------------------------------

_REVIEW_LOG_PATHS = [
    Path(".jemmin") / "logs" / "latest_review.json",
    Path("latest_review.json"),
]


def _find_review_log() -> Path | None:
    for candidate in _REVIEW_LOG_PATHS:
        if candidate.exists():
            return candidate
    return None


def _watch_and_publish(poll_interval: float = 1.0) -> None:
    """Poll latest_review.json; publish diagnostics when it changes."""
    global _latest_review
    last_mtime: float = -1.0
    last_uri: str = ""

    while True:
        log_path = _find_review_log()
        if log_path:
            try:
                mtime = log_path.stat().st_mtime
                if mtime != last_mtime:
                    last_mtime = mtime
                    review = json.loads(log_path.read_text(encoding="utf-8"))

                    # Update shared cache for codeAction handler
                    with _review_lock:
                        _latest_review = review

                    uri, diagnostics = _review_to_diagnostics(review)
                    if uri:
                        # Clear previous file's diagnostics if the target changed.
                        if last_uri and last_uri != uri:
                            _notify(
                                "textDocument/publishDiagnostics",
                                {"uri": last_uri, "diagnostics": []},
                            )
                        last_uri = uri
                        _notify(
                            "textDocument/publishDiagnostics",
                            {"uri": uri, "diagnostics": diagnostics},
                        )
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# LSP message dispatch
# ---------------------------------------------------------------------------


def _handle_initialize(req_id: Any, params: dict[str, Any]) -> None:  # noqa: ARG001
    _respond(
        req_id,
        {
            "capabilities": {
                "codeActionProvider": {
                    "codeActionKinds": ["quickfix"],
                },
                "textDocumentSync": {
                    "openClose": True,
                    "change": 0,  # None — we don't need edits, just file watching
                },
            },
            "serverInfo": {"name": "jemmin-lsp", "version": "0.2.0"},
        },
    )


def _handle_code_action(req_id: Any, params: dict[str, Any]) -> None:
    """Handle textDocument/codeAction: return Quick-Fix code actions."""
    text_doc = params.get("textDocument") or {}
    uri: str = text_doc.get("uri", "")
    request_range: dict[str, Any] = params.get("range") or {}
    context = params.get("context") or {}
    context_diags: list[dict[str, Any]] = context.get("diagnostics", [])

    actions = _build_code_actions(uri, request_range, context_diags)
    _respond(req_id, actions)


def _dispatch(msg: dict[str, Any]) -> bool:
    """Handle one JSON-RPC message. Returns False when server should quit."""
    method = msg.get("method", "")
    req_id = msg.get("id")

    if method == "initialize":
        _handle_initialize(req_id, msg.get("params") or {})
    elif method == "initialized":
        pass  # notification — no response required
    elif method == "textDocument/codeAction":
        _handle_code_action(req_id, msg.get("params") or {})
    elif method == "shutdown":
        _respond(req_id, None)
    elif method == "exit":
        return False
    elif req_id is not None:
        # Unknown request — return method-not-found error
        _write_message(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        )
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    # Switch stdin to binary mode on Windows so Content-Length framing works.
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)  # type: ignore[attr-defined]

    # Start watcher thread
    watcher = threading.Thread(target=_watch_and_publish, daemon=True)
    watcher.start()

    # Main LSP message loop on stdin
    stdin = sys.stdin.buffer
    while True:
        msg = _read_message(stdin)
        if msg is None:
            break
        if not _dispatch(msg):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
