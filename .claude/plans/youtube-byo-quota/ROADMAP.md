# YouTube onboarding — user-owned quota with a publisher OAuth client

**Status**: E1 COMPLETE — PASSED. Design confirmed viable; E1.6 and E2 next.
**Goal**: Cut the per-user Google Cloud Console walkthrough down to a sign-in
plus two console clicks. The API quota is **always** supplied by the user's own
Cloud project; only the OAuth client varies.

Two supported configurations:

| | OAuth client | Quota | Setup cost |
|---|---|---|---|
| Publisher client | injected at build time via `SOCCER_CAM_YT_CLIENT_ID`, verified | user's project | sign-in + create project + enable API |
| Self-provisioned | user creates their own | user's project | full console walkthrough, unverified-app warning, 7-day token expiry |

A build with no client injected falls back to self-provisioned, so building from
source stays fully functional. There is deliberately **no** configuration in
which the publisher's project supplies quota -- see "Fail closed" below.

---

## Why

Today `web/setup/router.py` walks every camera manager through ~15 Cloud Console
steps: create project, enable API, configure the consent screen, paste three
scopes, add themselves as a test user, create a Desktop OAuth client, add a
redirect URI, download `client_secret.json`, upload it to the wizard. That
produces an app in *Testing* status owned by a soccer parent, which means a
"Google hasn't verified this app" screen on every re-auth and refresh tokens
that die every 7 days.

By default YouTube quota is attributed to the project that owns the OAuth
client, so a shared client would mean a single shared 10,000 units/day across
every install. The `x-goog-user-project` quota-project override is what breaks
that coupling. E1 confirmed it works -- see results below.

## Target flow

```
1. Wizard: "Sign in with Google"   -> system browser, publisher client,
                                      youtube.* scopes only, no warning
2. Wizard generates project id     -> soccer-cam-<8 chars>, displayed
3. Wizard opens projectcreate      -> user accepts GCP ToS (first-timers),
                                      clicks Edit, enters our id, Create
4. Wizard polls channels.list with x-goog-user-project: <id>
     permission denied / not found -> keep waiting
     SERVICE_DISABLED              -> project exists, auto-advance
5. Wizard opens enableflow deep link -> user clicks Enable
6. Poll returns 200                -> auto-advance to Ready, id -> config
```

The user never pastes anything back into the app: they type an id we gave them.
Every step is initiated by the app and completion is *observed*, not trusted.
ToS needs no separate detection — without it they cannot create the project, so
the same poll covers it.

## Current code

- **Live**: BYO `client_secret.json`. `make_youtube_flow()` in
  `video_grouper/utils/youtube_upload.py`; wizard step at
  `web/setup/router.py:294`; dashboard re-auth in `web/auth_server.py`.
- **Dormant**: `authenticate_youtube_embedded()` + `EMBEDDED_CLIENT_CONFIG`
  (project `team-tech-tools-hh`), fed at build time by `_youtube_secrets.py`
  from `build-installer.ps1:40` and `build-windows-service.yml:70`. No
  production callers — this is the hook the new flow re-enters through.

## Verified facts

Established 2026-08-22. Re-check anything Google-side before relying on it.

| # | Fact | Source |
|---|---|---|
| F1 | Quota is per-project; `videos.insert` = 1600 units; default 10,000/day → ~6 videos ≈ 3 games/day | YouTube Data API docs |
| F2 | External + *Testing* publishing status → refresh tokens expire in 7 days | Google OAuth docs |
| F3 | Unverified in *Production* → warning screen + 100-new-user cap | support.google.com/cloud/answer/7454865 |
| F4 | `youtube.upload` is **sensitive**, not restricted → verification needs justification + demo video, no CASA assessment | Google verification docs |
| F5 | `projects.create` needs `cloud-platform` **or** `cloudplatformprojects`; `services.enable` needs `cloud-platform` **or** `service.management` — all sensitive | Resource Manager v3 / Service Usage v1 |
| F6 | `projects.create` fails until the account has accepted GCP ToS (violation type `TOS`); no API accepts it | Resource Manager, confirmed reports |
| F7 | `projectcreate` **ignores** `projectName`/`projectId` params; id is auto-generated random (observed `arctic-operand-506401-h6`) and only changeable via *Edit* before Create | observed in browser |
| F8 | Consumer accounts have a project quota (~25; observed "23 projects remaining") | observed in browser |
| F9 | `console.cloud.google.com/apis/enableflow?apiid=<api>&project=<id>` is a real one-click enable deep link | Google Chat API setup docs |
| F10 | Google blocks WebDriver-driven sign-in; every workaround is detection evasion → not shippable for an app seeking OAuth verification | Google sign-in behavior |
| F11 | Unlisted uploads work today from the current BYO setup — the docs' "unverified projects locked to private" note does not bite our existing project | Mark, in production |

## Open questions

- **Q1 (blocking)** Does YouTube Data API honor `x-goog-user-project` when the OAuth client belongs to a different project?
- **Q2** Does it work with `youtube.*` scopes only, or must the token carry `cloud-platform`?
- **Q3** Is quota actually decremented on the header project rather than the client's?
- **Q4** Can a *fresh*, unaudited project upload `unlisted`? (F11 covers our aged project, not a new one.)
- **Q5** What exact status/reason does each failure state return? The wizard's auto-advance is only as reliable as our ability to tell them apart.
- **Q6** Do refresh tokens survive past 7 days once TTT's app is in *Production*?

---

## E1 — the decisive experiment

**Gates everything else. ~1 hr hands-on. Prod untouched — two throwaway projects.**

- **Project X** (new): owns a Desktop OAuth client, YouTube Data API enabled. Stands in for TTT.
- **Project Y** (new): API **not** enabled yet. Stands in for a camera manager's quota project.
- Authorize once against X's client with **youtube scopes only**. Testing-mode warning is expected and irrelevant here.

| Step | Call | Pass signal | Answers |
|---|---|---|---|
| 1 | `channels.list?mine=true`, header `x-goog-user-project: Y` (API still disabled in Y) | `403 SERVICE_DISABLED` for Y → honored. `200` → ignored, design is dead | Q1, Q2 |
| 2 | Enable API in Y, repeat | `200` | Q1 |
| 3 | `videos.insert` a 5-sec clip, `privacyStatus=unlisted`, header `Y`; read back with `videos.list` | `unlisted` sticks | Q4 |
| 4 | Burn Y's remaining quota: ~84x `search.list` (100 units each), header `Y` | quota drains | Q3 |
| 5 | Call with header `Y` → `403 quotaExceeded`; same call with header `X` → `200` | **quota charged to Y, not X** | Q3 |

Step 1 is the cheap kill-switch; step 5 is the proof. Steps 4/5 replace waiting on
Cloud Console metrics graphs, which lag by hours. Order matters — upload before
burning quota.

**Gates:**

- **Pass** → E2.
- **Step 1 returns 200** → re-run authorization adding `cloud-platform` and repeat.
  Honored-only-with-`cloud-platform` is a *bad* pass: the consent screen then reads
  "See, edit, configure, and delete your Google Cloud data" to a soccer parent, and
  pulls a heavier verification review. Decision point, not an automatic yes.
- **Ignored either way** → user-owned quota under a shared client is impossible.

### E1 results — 2026-08-23, PASSED

Client project `soccer-cam-e1-client` (project number 396182566355), quota
project `project-x-506413`. Token carried the three `youtube.*` scopes and
nothing else.

| Q | Result | Evidence |
|---|---|---|
| Q1 | **Honored** | API disabled in quota project → `403 PERMISSION_DENIED` / `accessNotConfigured` / `SERVICE_DISABLED`, `consumer: projects/project-x-506413`. After enabling → `200` |
| Q2 | **youtube-only scopes suffice** | No `cloud-platform` on the token at any point. Consent screen stays YouTube-only |
| Q3 | **Metering follows the header** | 350 headered `search.list` calls drained only the quota project: header=quota → `429 rateLimitExceeded/search_list`; header=client → `200`; no header → `200` |
| Q4 | **Fresh unaudited project uploads unlisted** | `privacyStatus=unlisted`, `uploadStatus=uploaded`, no `rejectionReason`, from a project hours old |
| Q5 | Partial | Captured `SERVICE_DISABLED` (with `activationUrl`) and `RATE_LIMIT_EXCEEDED` envelopes |

**Caveats.**

- Attribution is proven on the `search_list` metric. Upload *units* landing on
  the same project is an inference from shared machinery, not a direct
  measurement.
- Enforcement lags: ~10,000 units drew no refusal; the refusal came in a later
  burn. The `search_list` 100/day cap tripped, the 10,000-unit budget never did
  despite ~35,000 units charged. Do not treat "call succeeded" as "quota fine",
  and do not promise users a crisp 3-games/day ceiling until this is understood.
- Console counters lag hours -- both projects read ~0 immediately after the burn,
  so the console is useless for live checks.

## Fail closed

Because a missing or invalid header silently bills the *client's* project, and
that project is shared across every install using the publisher client, a bug
that drops the header would drain one 10,000/day pool for all users at once with
no obvious cause.

**soccer-cam must refuse to upload when no quota project is configured** rather
than falling back. Setup polling must always carry the header and use bounded
retry, since a stuck wizard otherwise becomes a slow drain on a shared resource.
Pre-project calls (the post-sign-in channel lookup) unavoidably hit the publisher
project; keep them to a minimum.

## E1.6 — error taxonomy

**~45 min. Needs a Gmail account that has never opened Cloud Console.** Answers Q5,
and is worth doing even in a deep-link-only design so the wizard never surfaces a
raw 403.

Capture the exact status + reason for each state: ToS not accepted; project
absent; project exists + API disabled; project exists + API enabled; caller
lacks `serviceusage.services.use`. Also confirm F6 and F7 hold for a genuinely
fresh account.

## The rest — gated on E1

| # | What | Verifies |
|---|---|---|
| E2 | Stand up the publisher OAuth client: branded consent screen, published to **Production**, submit brand + sensitive-scope verification. **No YouTube API quota-extension audit is needed** -- the publisher project never serves quota | Consent screen loses the warning. ~2 hrs to submit, days-to-weeks for Google |
| E3 | Mint a token against the Production client, park it, re-check at day 8. Also check the token response for `refresh_token_expires_in` | Q6 — kills the weekly re-auth treadmill |
| E4 | Wire it up: re-enter through `authenticate_youtube_embedded()`, thread the quota header through the API client, rebuild the wizard step | Full game (raw + processed + playlist) lands unlisted, quota moves on the user's project |
| E5 | Negative paths: wrong project id, API not enabled, ToS unaccepted, no `serviceusage.services.use`, project-quota exhausted (F8), **and header missing/invalid** | Each produces a fixable wizard message; the missing-header case refuses to upload rather than silently billing the publisher project |
| E6 | Keep `make_youtube_flow()` BYO alive behind a disclosure | Escape hatch if TTT's client is ever suspended — it is a single point of failure for *identity* even though quota stays distributed |
| E7 | Re-run the probe at service startup, not just setup | A project that later loses its API enablement surfaces as a wizard state, not upload failures |
| E8 | Customer-perspective run: clean Windows box, fresh Google account, CI-built installer with real `_youtube_secrets.py`, no dev shortcuts | The whole thing, as a real camera manager |

## Decided along the way

- **Quota is always user-owned.** There is no configuration where the publisher
  project supplies quota. That keeps per-user isolation, removes the YouTube API
  quota-extension audit from the critical path, and confines abuse blast radius
  to the offending user's own project.
- **No browser puppeteering of Google** (F10). "Control" means the app opens each
  step and polls for completion, not that it drives the clicks. The Selenium
  TeamSnap dev-portal automation that suggested otherwise was orphaned dead code
  and has been removed on `chore/remove-teamsnap-dev-portal-automation`.
- **No auto-creation of the user's project.** It needs two sensitive scopes (F5)
  and still cannot skip the ToS trip (F6), so it buys ~3 clicks at the cost of a
  scarier consent screen and a harder review. Revisit only if E1.6 shows ToS is
  already accepted for a meaningful share of accounts. Note that adding scopes to
  an already-approved app triggers re-verification, so this is a now-decision.
- **The 100-user cap (F3) is not the binding constraint** at ~1 camera manager
  per team, and verification removes it regardless.
