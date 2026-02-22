Parse the arguments from `$ARGUMENTS` to extract:

1. **The prompt** — the main quoted string (everything that isn't a flag)
2. **--max-iterations N** — maximum number of iterations before stopping (default: 100)
3. **--completion-promise "text"** — a phrase that, when output by you, signals the task is complete

Then do the following:

1. Write a JSON file to `.claude/ralph-state.json` (relative to the project root) with this structure:
   ```json
   {
     "prompt": "<the extracted prompt>",
     "max_iterations": <N>,
     "completion_promise": "<the promise text>",
     "iteration": 0
   }
   ```

2. Confirm the Ralph loop has started by printing:
   - The prompt being worked on
   - Max iterations setting
   - Completion promise (if set)
   - Reminder: use `/cancel-ralph` to stop the loop early

3. **Immediately begin working on the prompt.** Execute the task described in the prompt autonomously. When you believe the task is complete, output the completion promise text wrapped in `<promise>` tags (e.g., `<promise>DONE</promise>`).

**Important:** The Stop hook at `.claude/hooks/stop-hook.py` will intercept your exit attempts and feed the prompt back to you. Each iteration, review your previous work in the files and git history, then continue improving. Do not fight the loop — embrace iteration.
