#!/bin/bash
# PostToolUse(Write|Edit): ruff-format + ruff-fix the Python file Claude just wrote, and report lint
# errors ruff can't auto-fix (exit 2 -> shown to Claude). The edited path arrives in the hook JSON
# on stdin (.tool_input.file_path); Claude Code sets no per-file environment variable (the
# previous inline hook read $CLAUDE_FILE_PATH, which is never set, so it silently did nothing).

f=$(jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -n "$f" ] && [ -f "$f" ] || exit 0
case "$f" in *.py|*.pyi) ;; *) exit 0 ;; esac
p=$(cygpath -u "$f" 2>/dev/null || printf '%s' "$f")
root=$(git -C "$(dirname "$p")" rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0

uv run --quiet ruff format --quiet "$p" >/dev/null 2>&1
errs=$(uv run --quiet ruff check --fix --quiet "$p" 2>&1)
if [ -n "$errs" ]; then
  printf 'ruff errors remain in %s (fix before committing):\n%s\n' "$f" "$errs" >&2
  exit 2
fi
exit 0
