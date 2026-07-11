"""Interactive setup wizard — called from Set_Env.bat.

Collects: LLM provider, Gemini API key, watch folder, review mode.
Writes results to config/nitpicker.local.json and config/reviewer_config.yaml.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT       = Path(__file__).resolve().parents[1]
LOCAL_JSON = ROOT / "config" / "nitpicker.local.json"
CONFIG_YAML = ROOT / "config" / "reviewer_config.yaml"
DEFAULT_GEMINI_MODEL = "gemini-3.1-pro-preview"
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-2.0-flash"

SEP  = "=" * 60
SEP2 = "-" * 60


# ── helpers ────────────────────────────────────────────────────

def ask(prompt: str, valid: list[str] | None = None, default: str = "") -> str:
    while True:
        raw = input(prompt).strip()
        if not raw:
            raw = default
        if valid is None or raw.upper() in [v.upper() for v in valid]:
            return raw
        print(f"  Please enter one of: {valid}")


def _read_provider() -> str:
    if not CONFIG_YAML.exists():
        return "mock"
    for line in CONFIG_YAML.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s+default:\s+(\S+)", line)
        if m:
            return m.group(1)
    return "mock"


def _set_provider(name: str) -> None:
    if not CONFIG_YAML.exists():
        return
    lines = CONFIG_YAML.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if re.match(r"\s+default:\s+\S+", line):
            lines[i] = re.sub(r"(default:\s+)\S+", rf"\g<1>{name}", line)
            break
    CONFIG_YAML.write_text("".join(lines), encoding="utf-8")


def _read_local_config() -> dict:
    if not LOCAL_JSON.exists():
        return {}
    try:
        return json.loads(LOCAL_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    return value[:8] + "..." + value[-4:] if len(value) > 12 else "***"


_CLAUDE_MD_MARKER = "<!-- nitpicker-review-instructions -->"

_CLAUDE_MD_TEMPLATE = """\

{marker}
## Code Review (Nitpicker Daemon)

### Review Workflow
1. Complete code changes
2. Run Nitpicker review on changed files
3. If REJECTED → fix issues and re-run until ALL PASS
4. Report "Review PASSED. Ready to commit." with review results
5. User confirms → commit with `--no-verify` (pre-commit hook already reviewed)

### Review Command
```bash
del "{nitpicker_root}\\.jemmin\\spool.db"
set PYTHONIOENCODING=utf-8
"{nitpicker_root}\\.venv\\Scripts\\python.exe" "{nitpicker_root}\\bin\\jemmin_cli.py" ^
  --file <changed_file> ^
  --diff "<diff_text>" ^
  --no-daemon --provider {provider}
```

### Notes
- Review result: `{nitpicker_root}\\.jemmin\\logs\\LATEST_REVIEW.txt`
- Review persona/rules: `{nitpicker_root}\\config\\system_prompt.md`
- User's direct `git commit` triggers pre-commit hook automatically (y/n confirmation)
- Skip review: `git commit --no-verify`
{marker}
"""


def _inject_instructions(target_path: Path, nitpicker_root: Path, provider: str, *, label: str = "") -> None:
    """대상 파일에 Nitpicker 리뷰 지침을 주입합니다 (CLAUDE.md / agent.md 공용)."""
    display = label or target_path.name
    content = ""
    if target_path.exists():
        content = target_path.read_text(encoding="utf-8")

    block = _CLAUDE_MD_TEMPLATE.format(
        marker=_CLAUDE_MD_MARKER,
        nitpicker_root=nitpicker_root,
        provider=provider,
    )

    if _CLAUDE_MD_MARKER in content:
        parts = content.split(_CLAUDE_MD_MARKER)
        if len(parts) >= 3:
            content = parts[0].rstrip() + block + parts[2].lstrip("\n")
        print(f"   Updated existing Nitpicker section in {display}")
    else:
        content = content.rstrip() + "\n" + block if content.strip() else block.lstrip()
        print(f"   Added Nitpicker review section to {display}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8")
    print(f"   Written: {target_path}")


def _inject_claude_md(target_path: Path, nitpicker_root: Path, provider: str) -> None:
    """CLAUDE.md에 Nitpicker 리뷰 지침을 주입합니다."""
    _inject_instructions(target_path, nitpicker_root, provider, label="CLAUDE.md")


def _inject_copilot_agent(project_root: Path, nitpicker_root: Path, provider: str) -> None:
    """GitHub Copilot .github/agents/<project>-expert.agent.md에 주입합니다."""
    project_name = project_root.name.lower().replace(" ", "-")
    agent_file = project_root / ".github" / "agents" / f"{project_name}-expert.agent.md"
    _inject_instructions(agent_file, nitpicker_root, provider, label=agent_file.name)


def _inject_precommit_hook(project_root: Path, nitpicker_root: Path, provider: str) -> None:
    """git pre-commit hook을 설치합니다. 커밋 시 자동 리뷰 + 사용자 확인."""
    git_dir = project_root / ".git"
    if not git_dir.is_dir():
        print(f"   [SKIP] .git not found in {project_root}")
        return

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_file = hooks_dir / "pre-commit"

    # 경로를 슬래시로 통일 (bash 스크립트)
    nr = str(nitpicker_root).replace("\\", "/")

    script = f'''#!/bin/bash
# Nitpicker pre-commit hook (auto-generated by setup_wizard)
# staged .cpp/.h 파일에 대해 {provider} 리뷰 실행
# REJECT → 커밋 차단, PASS → 사용자 y/n 확인

NITPICKER_DIR='{nr}'
PYTHON="$NITPICKER_DIR/.venv/Scripts/python.exe"
[ ! -f "$PYTHON" ] && PYTHON="$NITPICKER_DIR/.venv/bin/python3"
CLI="$NITPICKER_DIR/bin/jemmin_cli.py"
SPOOL="$NITPICKER_DIR/.jemmin/spool.db"
REVIEW_LOG="$NITPICKER_DIR/.jemmin/logs/LATEST_REVIEW.txt"

FILES=$(git diff --cached --name-only --diff-filter=ACM | grep -E '\\.(cpp|h|py)$')
[ -z "$FILES" ] && exit 0

TOTAL=0; PASSED=0; FAILED=0; RESULTS=""

for f in $FILES; do
    TOTAL=$((TOTAL + 1))
    rm -f "$SPOOL" 2>/dev/null || true
    DIFF=$(git diff --cached -- "$f" 2>/dev/null || true)
    [ -z "$DIFF" ] && continue

    RESULT=$(PYTHONIOENCODING=utf-8 "$PYTHON" "$CLI" \\
        --file "$f" --diff "$DIFF" --no-daemon --provider {provider} 2>&1)

    if echo "$RESULT" | grep -qiE 'rejected|reject'; then
        FAILED=$((FAILED + 1))
        SUMMARY=$(cat "$REVIEW_LOG" 2>/dev/null | grep '요약:' | sed 's/요약: //')
        RESULTS="$RESULTS\\n  REJECT  $f\\n     -> $SUMMARY"
    else
        PASSED=$((PASSED + 1))
        RESULTS="$RESULTS\\n  PASS    $f"
    fi
done

echo ""
echo "============================================"
echo "  Nitpicker Review Result"
echo "============================================"
printf "$RESULTS\\n"
echo "  Result: $PASSED PASS / $FAILED REJECT ($TOTAL files)"
echo "============================================"

if [ $FAILED -gt 0 ]; then
    echo "  REJECTED files found. Fix and retry."
    echo "  Skip: git commit --no-verify"
    exit 1
fi

echo ""
exec < /dev/tty
read -p "  Proceed with commit? (y/n): " answer
case "$answer" in
    [yY]*) exit 0 ;;
    *) echo "  -> Commit cancelled"; exit 1 ;;
esac
'''

    hook_file.write_text(script, encoding="utf-8", newline="\n")
    # 실행 권한 (Unix)
    try:
        import stat
        hook_file.chmod(hook_file.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass
    print(f"   Installed pre-commit hook: {hook_file}")
    print(f"   Provider: {provider}, targets: .cpp/.h/.py")


# \u2500\u2500 wizard \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

def main() -> int:
    print()
    print(SEP)
    print("  Setup Wizard")
    print(SEP)

    # -- Skip if already configured --
    existing_config = _read_local_config()
    existing_key = str(existing_config.get("gemini_api_key", "")).strip()
    if "YOUR_GEMINI_API_KEY" in existing_key:   # treat placeholder as no key
        existing_key = ""
    existing_model = str(existing_config.get("gemini_model", DEFAULT_GEMINI_MODEL)).strip() or DEFAULT_GEMINI_MODEL
    existing_fallback_model = (
        str(existing_config.get("gemini_fallback_model", DEFAULT_GEMINI_FALLBACK_MODEL)).strip()
        or DEFAULT_GEMINI_FALLBACK_MODEL
    )

    if LOCAL_JSON.exists():
        text = LOCAL_JSON.read_text(encoding="utf-8")
        if "YOUR_GEMINI_API_KEY" not in text:
            print()
            print("  Existing config found:")
            try:
                print(f"    provider     : {_read_provider()}")
                print(f"    watch_path   : {existing_config.get('watch_path', 'src')}")
                print(f"    auto_apply   : {existing_config.get('auto_apply_patches', False)}")
                print(f"    gemini_model : {existing_model}")
                print(f"    fallback     : {existing_fallback_model}")
                key = existing_key
                if key:
                    print(f"    gemini_key   : {_mask_secret(key)}")
            except Exception:
                pass
            print()
            skip = ask("  Reconfigure? [Y/N]: ", ["Y", "N"], "N")
            if skip.upper() == "N":
                print("  Skipped.")
                return 0

    # ── A: LLM Provider ──────────────────────────────────────
    print()
    print(SEP2)
    print("  [A] LLM Provider")
    print(SEP2)
    print("   1. Gemini API   cloud, fast, paid")
    print("                   requires API key")
    print("   2. Ollama       local, free, private")
    print("                   requires GPU (VRAM 8GB+), ~5GB download")
    print()
    p_choice = ask("  Choice [1/2]: ", ["1", "2"], "2")
    provider = "gemini" if p_choice == "1" else "ollama"

    gemini_key = existing_key
    if provider == "gemini":
        print()
        if gemini_key:
            print(f"  Existing Gemini API key found: {_mask_secret(gemini_key)}")
            print("  Keeping saved key and skipping key prompt.")
        else:
            print("  Get your key: https://aistudio.google.com/apikey")
            gemini_key = input("  Gemini API key: ").strip()
        if not gemini_key:
            print("  (no key entered — you can add it later in config/nitpicker.local.json)")

    # ── B: Watch folder ───────────────────────────────────────
    print()
    print(SEP2)
    print("  [B] Watch Folder  (auto-review when files are saved)")
    print(SEP2)
    print("   1. src\\          default")
    print("   2. .\\            entire project root")
    print("   3. Custom path")
    print()
    w_choice = ask("  Choice [1/2/3]: ", ["1", "2", "3"], "1")
    if w_choice == "1":
        watch_path = "src"
    elif w_choice == "2":
        watch_path = "."
    else:
        watch_path = input("  Enter folder path (e.g. lib\\): ").strip() or "src"

    # ── C: Review Mode ────────────────────────────────────────
    log_dir = ROOT / ".jemmin" / "logs"
    print()
    print(SEP2)
    print("  [C] Review Mode")
    print(SEP2)
    print("   1. Review Only   agent suggests patches")
    print("                    -> apply manually via Quick-Fix (💡) in IDE")
    print("                    SAFE: files are never touched automatically")
    print()
    print("   2. Auto-Fix      agent applies patches automatically")
    print("                    git apply --check first, then auto-apply")
    print("                    MAGIC: code fixes itself while you type!")
    print()
    print(f"   Review output : {log_dir}")
    print("                   latest_review.json / latest_review.txt")
    print()
    m_choice = ask("  Choice [1/2]: ", ["1", "2"], "1")
    auto_apply = m_choice == "2"

    # ── D: AI Agent instructions injection ─────────────────────
    print()
    print(SEP2)
    print("  [D] AI Agent Review Instructions")
    print(SEP2)
    print("   Inject Nitpicker review instructions into your project?")
    print("   Supported targets:")
    print("     - CLAUDE.md              (Claude Code)")
    print("     - .github/agents/*.md    (GitHub Copilot)")
    print()

    # watch_path 기준으로 대상 프로젝트 루트 결정
    watch_abs = (ROOT / watch_path).resolve()
    target_project = watch_abs.parent if watch_abs.name in ("src", "lib", "app") else watch_abs
    target_claude_md = target_project / "CLAUDE.md"
    inject_claude = False
    inject_copilot = False

    if target_project == ROOT:
        print("   (Target is Nitpicker itself — skipping)")
    else:
        print(f"   Project root: {target_project}")
        print()
        print("   1. CLAUDE.md only          (Claude Code)")
        print("   2. Copilot agent.md only   (GitHub Copilot)")
        print("   3. Both")
        print("   4. Skip")
        print()
        d_choice = ask("  Choice [1/2/3/4]: ", ["1", "2", "3", "4"], "3")
        inject_claude = d_choice in ("1", "3")
        inject_copilot = d_choice in ("2", "3")

    if inject_claude:
        _inject_claude_md(target_claude_md, ROOT, provider)
    if inject_copilot:
        _inject_copilot_agent(target_project, ROOT, provider)

    # ── E: Git pre-commit hook ────────────────────────────────
    # .git은 watch_path가 아닌 프로젝트 루트에 있을 수 있으므로 상위로 탐색
    def _find_git_root(start: Path) -> Path | None:
        p = start.resolve()
        while p != p.parent:
            if (p / ".git").is_dir():
                return p
            p = p.parent
        return None

    git_root = _find_git_root(target_project)
    inject_hook = False
    if target_project != ROOT and git_root:
        print()
        print(SEP2)
        print("  [E] Git Pre-Commit Hook")
        print(SEP2)
        print("   Install auto-review hook on 'git commit'?")
        print("   - Reviews staged .cpp/.h/.py files before commit")
        print("   - REJECT → commit blocked")
        print("   - PASS → shows result, asks y/n")
        print("   - Skip with: git commit --no-verify")
        print()
        e_choice = ask("  Install pre-commit hook? [Y/N]: ", ["y", "n"], "y")
        inject_hook = e_choice.lower() == "y"

    if inject_hook and git_root:
        _inject_precommit_hook(git_root, ROOT, provider)

    # ── Write configs ─────────────────────────────────────────
    config = {
        "gemini_api_key":   gemini_key,
        "gemini_model":     existing_model,
        "gemini_fallback_model": existing_fallback_model,
        "watch_path":       watch_path,
        "debounce_seconds": 2.0,
        "file_extensions":  [".py", ".cpp", ".h", ".hpp"],
        "skip":             False,
        "auto_apply_patches": auto_apply,
    }
    LOCAL_JSON.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_JSON.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    _set_provider(provider)

    # ── Summary ───────────────────────────────────────────────
    print()
    print(SEP)
    print("  Config saved!")
    print(SEP)
    print(f"    Provider   : {provider}")
    print(f"    Watch path : {ROOT / watch_path}")
    print(f"    Model      : {existing_model}")
    print(f"    Fallback   : {existing_fallback_model}")
    mode_label = "Auto-Fix (magic mode)" if auto_apply else "Review Only (safe mode)"
    print(f"    Mode       : {mode_label}")
    print(f"    Review log : {log_dir}")
    if gemini_key:
        print(f"    API key    : {_mask_secret(gemini_key)}")
    if inject_claude:
        print(f"    CLAUDE.md  : {target_claude_md}")
    if inject_copilot:
        project_name = target_project.name.lower().replace(" ", "-")
        print(f"    Copilot    : {target_project / '.github' / 'agents' / (project_name + '-expert.agent.md')}")
    if inject_hook and git_root:
        print(f"    Pre-commit : {git_root / '.git' / 'hooks' / 'pre-commit'}")
    print()

    # Return provider as exit code hint for batch
    # 0 = gemini, 2 = ollama
    return 2 if provider == "ollama" else 0


if __name__ == "__main__":
    sys.exit(main())
