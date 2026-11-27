#!/bin/bash
# Stop hook: don't end a turn with work stranded in the session's checkout — uncommitted changes,
# or commits that exist on no remote. Blocks the stop ONCE (exit 2, reason fed back to Claude);
# when Claude stops again (stop_hook_active=true) it is let through, so a deliberate "left
# uncommitted because ..." explanation to the user is enough.

input=$(cat)
[ "$(printf '%s' "$input" | jq -r '.stop_hook_active // false' 2>/dev/null)" = "true" ] && exit 0
cwd=$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)
[ -n "$cwd" ] || exit 0
git -C "$cwd" rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

branch=$(git -C "$cwd" branch --show-current 2>/dev/null)
dirty=$(git -C "$cwd" status --porcelain 2>/dev/null | head -15)
if git -C "$cwd" rev-parse '@{u}' >/dev/null 2>&1; then
  n=$(git -C "$cwd" rev-list --count '@{u}..HEAD' 2>/dev/null)
  [ "${n:-0}" -gt 0 ] && unpushed="$n commit(s) not pushed to $(git -C "$cwd" rev-parse --abbrev-ref '@{u}')"
else
  n=$(git -C "$cwd" rev-list --count HEAD --not --remotes 2>/dev/null)
  [ "${n:-0}" -gt 0 ] && unpushed="$n commit(s) on '$branch' exist on no remote"
fi

[ -z "$dirty" ] && [ -z "$unpushed" ] && exit 0
{
  echo "Stranded work in $cwd (branch '$branch'):"
  [ -n "$dirty" ] && printf 'Uncommitted:\n%s\n' "$dirty"
  [ -n "$unpushed" ] && echo "$unpushed"
  echo "Commit and push it (worktree branch), or tell Mark explicitly why it is being left."
} >&2
exit 2
