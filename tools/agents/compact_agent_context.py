"""Build compact Claude/Codex handoff context for the Orion agent loop.

This is intentionally dependency-light. Headroom can be added later as a drop-in
compression layer, but the loop should not block on a third-party install during
gameplay debugging.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


IMPORTANT_PATTERNS = re.compile(
    r"(error|fail|failed|fatal|warning|blocked|needs-changes|approved|"
    r"max_hold_safety|no_sample_ever|green_confirmed|predictive_target|"
    r"waiting_for_meter|stale|frame_age|release|AutomationEngine|ctest|pytest)",
    re.IGNORECASE,
)


def run_git(args: list[str]) -> str:
    try:
        out = subprocess.run(
            ["git", "-c", "core.autocrlf=false", *args],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
        )
        return out.stdout.strip()
    except Exception as exc:  # pragma: no cover - defensive diagnostics
        return f"[git failed: {exc}]"


def read_text(path: Path, max_chars: int = 20000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return f"{head}\n\n[... truncated {len(text) - len(head) - len(tail)} chars ...]\n\n{tail}"


def compact_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text

    important = [line for line in lines if IMPORTANT_PATTERNS.search(line)]
    head = lines[: max_lines // 5]
    tail = lines[-max_lines // 3 :]

    merged: list[str] = []
    seen: set[str] = set()
    for line in [*head, *important, *tail]:
        key = line.strip()
        if key in seen:
            continue
        seen.add(key)
        merged.append(line)
        if len(merged) >= max_lines:
            break
    return "\n".join(merged)


def current_status_block() -> str:
    status = read_text(ROOT / "STATUS.md", 12000)
    match = re.search(r"## Current Status\s+```(.*?)```", status, re.S)
    if match:
        return match.group(1).strip()
    return compact_lines(status, 40)


def build_context(mode: str, max_lines: int) -> str:
    diff_stat = run_git(["diff", "--stat"])
    changed = run_git(["status", "--short", "--untracked-files=all"])
    name_only = run_git(["diff", "--name-only"])
    staged_name_only = run_git(["diff", "--name-only", "--cached"])

    allowed = read_text(ROOT / "TASK.md", 12000)
    rules = read_text(ROOT / "AGENT_RULES.md", 12000)
    dialogue = read_text(ROOT / "AGENT_DIALOGUE.md", 12000)
    tests = read_text(ROOT / "TEST_COMMANDS.md", 8000)

    if mode == "codex":
        diff = run_git(["diff", "--", "TASK.md", "STATUS.md", "agent-loop.ps1", "scripts", "native_orion/src/AutomationEngine.cpp", "native_orion/src/AutomationEngine.h", "native_orion/tests/AutomationEngineTests.cpp"])
        diff = compact_lines(diff, max_lines)
    else:
        diff = ""

    recent_log = ""
    log_dir = ROOT / "logs" / "agent-loop"
    if log_dir.exists():
        logs = sorted(log_dir.glob("verify-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            recent_log = compact_lines(read_text(logs[0], 24000), max_lines // 2)

    return "\n\n".join(
        part
        for part in [
            f"# Compact {mode} context",
            "## Current STATUS.md tokens\n" + current_status_block(),
            "## git status\n" + changed,
            "## git diff --stat\n" + diff_stat,
            "## changed files\n" + "\n".join(x for x in [name_only, staged_name_only] if x),
            "## TASK.md\n" + compact_lines(allowed, 80),
            "## AGENT_RULES.md\n" + compact_lines(rules, 80),
            "## AGENT_DIALOGUE.md\n" + compact_lines(dialogue, 80),
            "## TEST_COMMANDS.md\n" + compact_lines(tests, 60),
            ("## focused diff\n" + diff if diff else ""),
            ("## latest verify log\n" + recent_log if recent_log else ""),
        ]
        if part
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["claude", "codex"], required=True)
    parser.add_argument("--max-lines", type=int, default=220)
    args = parser.parse_args()
    print(build_context(args.mode, args.max_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
