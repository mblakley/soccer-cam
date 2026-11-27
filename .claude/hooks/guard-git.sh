#!/bin/bash
# PreToolUse(Bash) guard: enforce the branching rules mechanically.
#   - no `git commit` on a protected branch
#   - no `git commit` in the primary checkout (worktree-first), when REQUIRE_WORKTREE=1
#   - no push to a protected branch; force-push only as --force-with-lease to a feature branch
# Exit 2 = block; stderr is fed back to Claude as the reason.
# Input: hook JSON on stdin (.tool_input.command, .cwd).

PROTECTED_BRANCHES="main"
REQUIRE_WORKTREE=1

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""' 2>/dev/null)
cwd=$(printf '%s' "$input" | jq -r '.cwd // ""' 2>/dev/null)
[ -z "$cmd" ] && exit 0

# Fast path: nothing to do unless the command runs git commit/push.
printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+(commit|push)([[:space:]]|$)' || exit 0

# Resolve the directory git will run in: `git -C <dir>` wins, else the last `cd <dir>` before
# the git call, else the session cwd.
dir="$cwd"
unquote() { sed -E "s/^[\"']//; s/[\"']\$//"; }
cd_dir=$(printf '%s' "$cmd" | grep -Eo "(^|[;&|[:space:]])cd[[:space:]]+(\"[^\"]+\"|'[^']+'|[^[:space:];&|]+)" | tail -1 | sed -E 's/^[;&|[:space:]]*cd[[:space:]]+//' | unquote)
c_dir=$(printf '%s' "$cmd" | grep -Eo "git[[:space:]]+-C[[:space:]]+(\"[^\"]+\"|'[^']+'|[^[:space:]]+)" | tail -1 | sed -E 's/^git[[:space:]]+-C[[:space:]]+//' | unquote)
[ -n "$cd_dir" ] && { case "$cd_dir" in /*|[A-Za-z]:*) dir="$cd_dir" ;; *) dir="$cwd/$cd_dir" ;; esac; }
[ -n "$c_dir" ] && { case "$c_dir" in /*|[A-Za-z]:*) dir="$c_dir" ;; *) dir="$dir/$c_dir" ;; esac; }

branch=$(git -C "$dir" branch --show-current 2>/dev/null)
block() { echo "BLOCKED by .claude/hooks/guard-git.sh: $1" >&2; exit 2; }
is_protected() { for b in $PROTECTED_BRANCHES; do [ "$1" = "$b" ] && return 0; done; return 1; }

if printf '%s' "$cmd" | grep -Eq 'git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+commit([[:space:]]|$)'; then
  if is_protected "$branch"; then
    block "commit on protected branch '$branch'. Create a worktree + short-lived branch off origin/<base> (see .claude/rules/branching.md)."
  fi
  if [ "$REQUIRE_WORKTREE" = "1" ]; then
    gd=$(git -C "$dir" rev-parse --git-dir 2>/dev/null); cd_common=$(git -C "$dir" rev-parse --git-common-dir 2>/dev/null)
    if [ -n "$gd" ] && [ "$gd" = "$cd_common" ]; then
      block "commit in the primary checkout ($dir). Worktree-first: git worktree add -b <branch> .claude/worktrees/<name> origin/<base>."
    fi
  fi
fi

if printf '%s' "$cmd" | grep -Eq 'git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push([[:space:]]|$)'; then
  # Rebasing a PR branch onto current base needs a force-push: allow --force-with-lease on
  # feature branches (checked against protected targets below); refuse the unguarded forms.
  if printf '%s' "$cmd" | grep -Eq '[[:space:]](-f|--force)([[:space:]]|$)|[[:space:]]\+[^[:space:]]+'; then
    block "unguarded force-push. Use --force-with-lease on your own feature branch."
  fi
  # Positional args after `push` (options dropped), up to the next shell separator.
  args=$(printf '%s' "$cmd" | sed -E 's/.*git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push//; s/[;&|].*//')
  set -f; set -- $args; set +f
  pos=(); for a in "$@"; do case "$a" in -*) ;; *) pos+=("$a") ;; esac; done
  if [ "${#pos[@]}" -le 1 ]; then
    # `git push` / `git push origin` pushes the current branch.
    is_protected "$branch" && block "push from protected branch '$branch'. Changes reach it only via a reviewed PR merge."
  else
    for ref in "${pos[@]:1}"; do
      dst="${ref#+}"; dst="${dst##*:}"; dst="${dst#refs/heads/}"
      [ "$dst" = "HEAD" ] && dst="$branch"
      is_protected "$dst" && block "push to protected branch '$dst'. Changes reach it only via a reviewed PR merge."
    done
  fi
fi
exit 0
