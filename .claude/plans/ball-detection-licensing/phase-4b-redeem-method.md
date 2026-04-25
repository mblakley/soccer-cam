# Phase 4b — Support Grant Redeem (soccer-cam side, partial)

Branch: `feature/ball-detection-loader` (continuing on the same branch as Phase 3)
Status: PARTIAL — API client method landed; UI integration deferred.

This is the soccer-cam-side counterpart to TTT Phase 4a (`team-tech-tools` commit
`420d868` on `feature/ball-detection-licensing`). The full plan lives at
`~/.claude/plans/explain-how-soccer-cam-s-design-robust-avalanche.md`.

## What landed

### `TTTApiClient.redeem_support_grant(code)`

`POST /api/grants/redeem` with the code in the body and **no Authorization
header** — the redeem endpoint is intentionally public (the code itself is the
credential). Bypasses the usual `_request()` helper, which would attach Bearer
auth + auto-refresh. Works even when `self._access_token is None`, which is the
whole point of a support code.

Returns `{grant_id, target_user_id, entitlement_key, expires_at}`. After
successful redemption the customer's account has the entitlement; subsequent
`acquire_model_license()` calls succeed normally.

### Tests (`tests/test_ttt_api.py`)

7 new tests:
- `test_list_model_versions` + filtered variant
- `test_acquire_model_license`
- `test_redeem_support_grant_happy_path` — body shape, URL, method
- `test_redeem_support_grant_does_not_send_bearer_auth` — explicit guard
- `test_redeem_support_grant_propagates_4xx`
- `test_redeem_support_grant_works_when_not_authenticated`

38/38 passing in `test_ttt_api.py`.

## Out of scope (deferred to follow-up commits)

- **Settings UI** "Enter support code" input in `video_grouper/tray/config_ui.py`.
  The PyQt6 tab structure is well-established; adding a "Support Code" tab with a
  small input + submit button is straightforward but needs UX design decisions
  (success message, error display, post-redeem refresh) that are best made with
  a real user in front of the screen.
- **Tray license-status surface** — countdown / warning at day 25 / "subscription
  required" at day 30. Needs persistent state for "last successful license
  acquisition time" and tray notification logic. Larger piece; separate commit.
- **TTT admin UI** for issuing codes (admin-side, lives in `team-tech-tools/frontend/`
  not soccer-cam).

## Verification

- `uv run ruff check video_grouper/api_integrations/ttt_api.py tests/test_ttt_api.py` → clean
- `uv run pytest tests/test_ttt_api.py` → 38/38 passing
