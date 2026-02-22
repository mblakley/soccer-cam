Review the changes on the current branch compared to main. Look for opportunities to simplify:

- Remove unnecessary abstractions or over-engineering
- Consolidate duplicated logic
- Simplify complex conditionals
- Remove dead code or unused imports
- Ensure naming is clear and consistent

Changes to review:
$( git diff main...HEAD --stat )

Only suggest simplifications that preserve existing behavior. Do not change test files unless tests are testing removed code.
