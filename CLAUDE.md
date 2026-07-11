# CLAUDE.md -- AI Assistant Reference

## Project Overview

**Nitpicker Daemon** (`auto-research-review`) is a 3-layer AI code review daemon for Windows.

- **Layer 1 (L1):** File watcher + single Gemini API call for quick reviews (`bin/mini_nitpicker_daemon.py`)
- **Layer 2 (L2):** 10-agent (AST/regex/tool) pre-filter — free first line of defense (`bin/jemmin_daemon.py`)
- **Layer 3 (L3):** LLM final review — only when L2 passes (`src/jemmin/services/llm_review_gate.py`)

Providers: Gemini (cloud, fast) or Ollama (local, free). Tiered routing can mix both.

**Notification**: Windows toast + Webhook (Slack/Discord/generic). `src/jemmin/services/notifier.py`
**Reports**: DuckDB analytics → CLI text + HTML report. `src/jemmin/services/report_generator.py`

## Quick Start

```
Set_Env.bat          # Interactive setup wizard (provider, API key, watch folder)
Run_Gemini.bat       # L1 daemon with Gemini cloud
Run_Local.bat        # L1 daemon with Ollama local
Run_Tests.bat        # pytest (511+ tests)
```

## Architecture (3-Layer)

```
L1: File Watcher → 단일 LLM 호출 (빠른 피드백, 사전검증 없음)
      bin/mini_nitpicker_daemon.py, bin/mini_nitpicker.py

L2: 10 Sub-Agents (AST/regex/tool) — 무료 1차 방어막, 0.01s
      src/jemmin/orchestrator/controller.py
      ├─ REJECT → 파이프라인 즉시 종료 (LLM 호출 0원)
      └─ PASS   → L3로 전달

L3: LLM 최종 리뷰 — L2 통과 코드에 한해 호출
      PromptLoader + tier1~4 Context + agent findings → Ollama/Gemini
```

**핵심 원칙**:
- L2 REJECT 후 LLM 호출은 안티패턴 (AST가 0.01s에 확정 판정 가능한 건 비용 낭비)
- L2는 린트 이상 — AST 기반 아키텍처/보안 분석. "기본 안전성 통과" 코드만 L3으로 전달
- LSP 불필요 — 데몬 + watchdog + auto-fix로 DX 충분

### Pipeline (L2 → L3)

ReviewRequest -> Policy -> Context build -> 10 Agents -> Consensus -> [L3 LLM Review] -> Patch -> Verify -> Deliver

States: `QUEUED -> CONTEXT_READY -> ANALYZING -> CONSENSUS_REACHED -> PATCH_PROPOSED -> VERIFIED -> DELIVERED`
Terminal: `PRECHECK_FAILED | DEGRADED | FAILED | DELIVERED`

## Directory Structure

```
bin/                    Entry points and helpers
  mini_nitpicker_daemon.py   L1 daemon
  jemmin_daemon.py           L2 daemon
  jemmin_cli.py              CLI entry point
  setup_wizard.py            Interactive setup (called by Set_Env.bat)
  _setup_helper.py           Extracted helper for batch file logic

src/jemmin/
  models.py             Core dataclasses: ReviewRequest, ReviewResult, AgentDecision, ConsensusResult
  orchestrator/          controller.py (ReviewOrchestrator.run_once)
  agents/                10 agents: architecture, context, domain_rule, fast_gate, incident_triage,
                           patch, performance, security, verification, (each returns AgentDecision)
  providers/             base.py (protocol), gemini.py, ollama.py, local_llm.py (mock)
  prompts/               PromptLoader -- loads config/system_prompt.md for all LLM providers
  state/                 SQLite spooler (WAL mode)
  services/              Artifact publishing, feedback, patch, verification,
                           llm_review_gate (L3), notifier, report_generator,
                           autofix_svc, config_watcher
  ipc/                   ZMQ transport, offload gateway
  context/               Context building and caching (providers: diff, symbol, policy, history)
  triggers/              File watcher integration
  registry/              Agent manifest-based selection

config/
  system_prompt.md              Review persona + rules -- loaded by PromptLoader, user-editable
  reviewer_config.yaml          Provider selection, model config, tier routing
  nitpicker.local.json          Gitignored -- API keys, watch path, user prefs
  nitpicker.local.example.json  Template for local config

tests/                   pytest, phases D through VII
  conftest.py            Adds src/ and ROOT to sys.path

.jemmin/                 Runtime state (gitignored): SQLite WAL, logs, patches
```

## Code Conventions

- **Python 3.11+**, type hints encouraged (`from __future__ import annotations`)
- `dataclass(slots=True)` for models
- `Literal` types for status enums in dataclasses
- Dependencies: `google-genai`, `watchdog`, `pyzmq`, `pytest`, `duckdb`

### Batch Files (.bat)

- Always start with `chcp 65001 >nul`
- **Never put Korean text in .bat files** -- causes encoding errors
- Set `PYTHONIOENCODING=utf-8` before any Python call
- CMD `if (...)` blocks cannot contain `python -c "..."` with `)` inside -- extract complex logic to `bin/_setup_helper.py` or `bin/setup_wizard.py` instead

### Config

- `config/system_prompt.md` -- review persona + CORE RULES + JSON schema; loaded by `PromptLoader` and injected into all LLM providers (Gemini, Ollama, etc.). User-editable single source of truth.
- `config/nitpicker.local.json` -- gitignored, contains API keys; created by setup wizard
- `config/reviewer_config.yaml` -- provider selection (`default: gemini|ollama|mock`), tier routing, model names
- Placeholder API key `"YOUR_GEMINI_API_KEY_HERE"` must be treated as no key (empty)

### Providers

- `base.py` defines the provider protocol
- `gemini.py` -- cloud LLM (requires API key in nitpicker.local.json)
- `ollama.py` -- local LLM (requires Ollama server at localhost:11434). Streaming + total deadline timeout (180s)
- `local_llm.py` -- mock provider for testing

### Agents

- Each agent implements `run(request, context) -> AgentDecision`
- `AgentDecision` has: `agent_name`, `status` (pass/warn/reject/error), `confidence_score`, `findings`, `suggested_actions`
- Agent selection via manifest registry or direct list injection

## Testing

```bash
pytest                    # run all 511+ tests
pytest --phase D          # filter by phase
pytest -k test_name       # filter by name
```

- Run from project root; `tests/conftest.py` sets up `sys.path`
- Test phases: D, E, F, G, H, I, II, III, IV, V, VI, VII
- Batch shortcut: `Run_Tests.bat`

## Self-Review (Dogfooding)

After completing code changes, run the project's own review system before reporting done:

```bash
del ".jemmin\spool.db"
git diff HEAD -- <changed_file> > .jemmin\review.diff
PYTHONIOENCODING=utf-8 python bin/jemmin_cli.py \
  --file <changed_file> \
  --diff-file .jemmin\review.diff \
  --no-daemon --provider ollama
```

- If rejected, fix the issues and re-run until pass
- Delete spool.db before each run to avoid idempotency collisions
- Use `--provider ollama` for local review (no API key needed)
- Ollama CLI runs use `config/reviewer_config.yaml` (`provider.ollama_model`)
  by default. Use `--model qwen2.5-coder:3b` only for explicit fallback or
  comparison tests; do not rely on lingering `OLLAMA_MODEL` env state. CLI
  runs directly by default. The ZMQ daemon is a legacy mock-provider path and
  is used only when `--use-daemon --provider mock` is explicit, so provider
  and model overrides cannot be silently ignored.
- Prefer `--diff-file` or `--diff-stdin` for multi-line diffs. Passing a raw
  multi-line diff through PowerShell/CMD `--diff "..."` is fragile because
  whitespace, quotes, and newlines may be split before Python receives them.
- Keep local Ollama review chunks small. `qwen2.5-coder:7b` is the default
  balanced review model, but a large whole-file diff can timeout on local
  hardware. Split large changes by file, feature, function, or commit before
  running Nitpicker. Use `qwen2.5-coder:3b` primarily as fallback/comparison
  because its verdicts can be less stable.

## Common Pitfalls

1. **Korean in .bat files** -- never. Windows CMD mangles non-ASCII even with `chcp 65001`.
2. **CMD `if` blocks with parentheses** -- `python -c "print('x')"` inside `if (...)` breaks CMD parsing. Move to a .py helper.
3. **SQLite spool.db reuse** -- causes "state mismatch" errors. Delete `.jemmin/spool.db` between test runs if state is stale.
4. **OllamaProvider.available() race condition** -- uses `threading.Lock`; do not call without synchronization.
5. **Placeholder API key** -- `"YOUR_GEMINI_API_KEY_HERE"` must be treated as missing/empty, not as a real key.
6. **PYTHONIOENCODING** -- must be set to `utf-8` before invoking Python from batch files.
7. **Ollama urlopen timeout** -- `urlopen(timeout=N)` is a *socket read* timeout, NOT total response timeout. Ollama streams tokens slowly so each read succeeds but total generation takes 10min+. Solution: `stream=True` + `time.monotonic()` deadline (already implemented in `_http_post_streaming`).
