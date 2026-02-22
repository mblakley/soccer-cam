#!/bin/bash
# Ralph Wiggum stop hook for Claude Code.
# Intercepts Claude's exit attempts and feeds the same prompt back,
# creating an iterative development loop until completion criteria are met.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SCRIPT_DIR/../ralph-state.json"

# Read hook input from stdin
INPUT=$(cat)

# No state file means no active loop — allow stop
if [ ! -f "$STATE_FILE" ]; then
  exit 0
fi

# Read state (allow stop on parse failure)
STATE=$(cat "$STATE_FILE" 2>/dev/null) || exit 0
echo "$STATE" | jq empty 2>/dev/null || { rm -f "$STATE_FILE"; exit 0; }

PROMPT=$(echo "$STATE" | jq -r '.prompt // ""')
MAX_ITERATIONS=$(echo "$STATE" | jq -r '.max_iterations // 100')
COMPLETION_PROMISE=$(echo "$STATE" | jq -r '.completion_promise // ""')
ITERATION=$(echo "$STATE" | jq -r '.iteration // 0')
LAST_MESSAGE=$(echo "$INPUT" | jq -r '.last_assistant_message // ""')

# Check completion promise in Claude's last message
if [ -n "$COMPLETION_PROMISE" ] && echo "$LAST_MESSAGE" | grep -qF "$COMPLETION_PROMISE"; then
  rm -f "$STATE_FILE"
  echo "Ralph loop completed: promise '$COMPLETION_PROMISE' found after $ITERATION iterations." >&2
  exit 0
fi

# Increment iteration
ITERATION=$((ITERATION + 1))

# Check max iterations
if [ "$ITERATION" -gt "$MAX_ITERATIONS" ]; then
  rm -f "$STATE_FILE"
  echo "Ralph loop stopped: max iterations ($MAX_ITERATIONS) reached." >&2
  exit 0
fi

# Update state file with new iteration count
echo "$STATE" | jq --argjson iter "$ITERATION" '.iteration = $iter' > "$STATE_FILE"

# Block the stop and feed the prompt back
jq -n \
  --arg decision "block" \
  --arg reason "[Ralph loop iteration ${ITERATION}/${MAX_ITERATIONS}] Continue working on the task. Review your previous work and iterate.

${PROMPT}" \
  '{"decision": $decision, "reason": $reason}'

exit 0
