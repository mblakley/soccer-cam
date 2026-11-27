# YouTube onboarding — user-owned quota with a publisher OAuth client

**Status**: E1 PASSED, E1.6 DONE (2026-11-27). Auto-create adopted
(2026-11-27) — E1.7 next, then E2.
**Goal**: Cut the per-user Google Cloud Console walkthrough down to a sign-in
plus a one-time Terms of Service acceptance; the app creates the project and
enables the API itself. The API quota is **always** supplied by the user's own
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

**Revised 2026-11-27 — auto-create** (see "Decided along the way"). The app
creates the project and enables the API through the Resource Manager / Service
Usage APIs using a setup-only token, so the user's part is sign-in + Allow and
the one-time GCP Terms. E1.7 proves this out.

```
1. Wizard: "Sign in with Google"   -> publisher client, youtube.* scopes
2. Wizard: "Set up your quota"     -> second, setup-only consent:
                                      cloudplatformprojects + service.management
3. projects.create(soccer-cam-<8>) -> TOS violation? open the GCP welcome page,
                                      user accepts Terms, wizard retries
4. Poll the create operation, then services.enable(youtube.googleapis.com)
5. Poll channels.list with x-goog-user-project (states: E1.6 table)
6. 200 -> revoke the setup-only token, id -> config, Ready
```

The deep-link flow below is the **fallback** (setup consent declined, or the
API path fails) and remains the E1.6-measured baseline:

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
  (project `team-tech-tools-hh`; replaced by `team-tech-tools-app` in E2), fed at build time by `_youtube_secrets.py`
  from `build-installer.ps1:40` and `build-windows-service.yml:70`. No
  production callers — this is the hook the new flow re-enters through.

## Verified facts

Established 2026-08-22. Re-check anything Google-side before relying on it.

| # | Fact | Source |
|---|---|---|
| F1 | ~~`videos.insert` = 1600 units; 10,000/day → ~3 games/day~~ **WRONG — retired 2026-08-23, see Upload limits below.** `videos.insert` and `search.list` have their own per-day call buckets; the unit pool governs other methods | measured |
| F2 | External + *Testing* publishing status → refresh tokens expire in 7 days | Google OAuth docs |
| F3 | Unverified in *Production* → warning screen + 100-new-user cap | support.google.com/cloud/answer/7454865 |
| F4 | `youtube.upload` is **sensitive**, not restricted → verification needs justification + demo video, no CASA assessment | Google verification docs |
| F5 | `projects.create` needs `cloud-platform` **or** `cloudplatformprojects`; `services.enable` needs `cloud-platform` **or** `service.management` — all sensitive | Resource Manager v3 / Service Usage v1 |
| F6 | `projects.create` fails until the account has accepted GCP ToS (violation type `TOS`); no API accepts it | Resource Manager, confirmed reports |
| F7 | `projectcreate` **ignores** `projectName`/`projectId` params; id is auto-generated random (observed `arctic-operand-506401-h6`, and `adroit-archive-516017-t8` on a fresh account 2026-11-27) and only changeable via *Edit* before Create | observed in browser |
| F8 | Consumer accounts have a project quota that **varies by account**: "23 projects remaining" (Aug), "12 projects remaining" on a never-used account (2026-11-27) | observed in browser |
| F9 | `console.cloud.google.com/apis/enableflow?apiid=<api>&project=<id>` is a real one-click enable deep link | Google Chat API setup docs |
| F10 | Google blocks WebDriver-driven sign-in; every workaround is detection evasion → not shippable for an app seeking OAuth verification | Google sign-in behavior |
| F11 | Unlisted uploads work today from the current BYO setup — the docs' "unverified projects locked to private" note does not bite our existing project | Mark, in production |

## Open questions

- **Q1 (blocking)** Does YouTube Data API honor `x-goog-user-project` when the OAuth client belongs to a different project?
- **Q2** Does it work with `youtube.*` scopes only, or must the token carry `cloud-platform`?
- **Q3** Is quota actually decremented on the header project rather than the client's?
- **Q4** Can a *fresh*, unaudited project upload `unlisted`? (F11 covers our aged project, not a new one.)
- **Q5** ~~What exact status/reason does each failure state return?~~ **Answered 2026-11-27 by E1.6** — see the results table there.
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

### E1.6 run sheet — 2026-11-27 (executed; results below)

Tooling: `e1_quota_project_probe.py` gained `new-id`, `e16-probe` (one call per
situation, classified) and `e16-watch` (polls `channels.list` with the quota
header the way the wizard will; prints each state change with elapsed seconds).
Every response is appended as JSON lines to `--log` — keep that log **outside the
repo** (scratchpad); only the summary table below is committed. `classify()`
states: `ok`, `ok_no_channel`, `service_disabled`, `project_denied`,
`project_invalid`, `quota`, else `other:<status>/<reason>` (record those verbatim).

Rig:
- OAuth client: `soccer-cam-e1-client` (Desktop, Testing mode), secret at
  `%LOCALAPPDATA%\credentials\e1-youtube-client.json`.
- Fresh token goes to `%LOCALAPPDATA%\credentials\e16-fresh-token.json`.
- The wizard-style project id for this run: **`soccer-cam-wrub0sw4`** (not created yet).
- "Someone else's project" case: `soccer-cam-e2-15a668` (the August rig's).
- The fresh account's address is Mark's to give; never commit it.
- The August test tokens are dead (Testing-mode 7-day expiry, F2) — don't reuse them.

State at handoff (11:45):
- Fresh account created. Opening Cloud Console as it showed the first-run
  "Welcome… Terms of Service" dialog → **F6 confirmed** on a never-used account.
  Mark was told **not** to accept yet, so "ToS not accepted" can be recorded first.
- Not yet confirmed: the fresh address added as a test user on
  `console.cloud.google.com/auth/audience?project=soccer-cam-e1-client` — must be
  done as the **owner** account (the fresh account gets "You need additional access").
- No token yet; nothing probed yet.

Steps (Mark clicks the two legal agreements himself — GCP ToS "Agree" and the
OAuth consent "Allow"; Claude may drive everything else in Chrome):
1. Owner account: add the fresh address as a test user.
2. `auth --port 8090 --client-secret <secret> --token-file <fresh token>` (run in
   background; send Mark the printed URL to open in the fresh account's window;
   "Google hasn't verified this app" → Continue → Allow).
3. **Before ToS**: `e16-probe --quota-project soccer-cam-wrub0sw4 --foreign-project soccer-cam-e2-15a668`
   → no-header (channel exists? U8), nonexistent id, own id (not created), foreign.
4. Start `e16-watch --quota-project soccer-cam-wrub0sw4` in the background.
   Fresh account opens `console.cloud.google.com/projectcreate`: note ToS shown?,
   pre-filled id (F7), "N projects remaining" (F8). Mark agrees to ToS; Edit id →
   `soccer-cam-wrub0sw4` → Create. Watch logs the absent → `service_disabled` lag.
5. `console.cloud.google.com/apis/enableflow?apiid=youtube.googleapis.com&project=soccer-cam-wrub0sw4`
   → Enable. Watch logs the `service_disabled` → `ok`/`ok_no_channel` lag (F9).
6. Optional (E7): disable the API again, re-run `e16-probe`, record the state.
7. Record a results table here (state → HTTP status / reason / classify state /
   observed lag), update Q5 + F6/F7/F8, commit + push on this branch. Then the
   wizard's state list for E4 comes from that table.

### E1.6 results — 2026-11-27, DONE

Rig change (more realistic than the run sheet): instead of adding the fresh
account as a test user, `soccer-cam-e1-client` was **published to Production**
(unverified). Branding needed a home page, privacy link and authorized domain
before *Publish app* un-greyed; filled with the GitHub repo URL / `github.com`.
So the fresh account went through exactly what a stranger sees before E2:
account chooser → "Google hasn't verified this app" → Advanced → Go to
(unsafe) → Allow. Token carried only the three `youtube.*` scopes, with a
refresh token. Revert with *Audience → Back to testing* if wanted.

All Cloud Console clicks after sign-in were made by Claude in Chrome as the fresh
account, to record the exact user path. Mark clicked the GCP Terms *Agree* and
the OAuth *Allow*.

**State catalogue** (`channels.list?part=id&mine=true`, header = quota project):

| Situation | HTTP | `error.status` | `errors[0].reason` | `details` reason | `classify()` | Message (start) |
|---|---|---|---|---|---|---|
| No header, account has no channel (U8) | 200 | — | — | — | `ok_no_channel` | empty `items` |
| Project id does not exist | 400 | `INVALID_ARGUMENT` | `badRequest` | `USER_PROJECT_DENIED` | `project_denied` | "Project 'projects/<id>' not found or deleted." |
| User's own id, not yet created | 400 | `INVALID_ARGUMENT` | `badRequest` | `USER_PROJECT_DENIED` | `project_denied` | identical to the row above |
| Someone else's project | 403 | `PERMISSION_DENIED` | `forbidden` | `USER_PROJECT_DENIED` | `project_denied` | "Caller does not have required permission to use project … serviceusage.services.use" |
| Own project, seconds old (IAM propagating) | 403 | `PERMISSION_DENIED` | `forbidden` | `USER_PROJECT_DENIED` | `project_denied` | identical to the row above |
| Own project, API not enabled | 403 | `PERMISSION_DENIED` | `accessNotConfigured` | `SERVICE_DISABLED` | `service_disabled` | "YouTube Data API v3 has not been used in project … or it is disabled." |
| Own project, API enabled | 200 | — | — | — | `ok_no_channel` | empty `items` (fresh account has no channel) |

**Lags** (watcher polling every 5 s, so ±5 s):

| Transition | Lag |
|---|---|
| *Create* clicked (12:20:28) → first `service_disabled` (12:22:00) | **~92 s** |
| then flapped: `service_disabled` → 403 `USER_PROJECT_DENIED` (12:22:10) → `service_disabled` (12:22:15) | ~15 s of flapping |
| *Enable* clicked (12:23:00) → `ok_no_channel` (12:23:07) | **~7 s** |

**Findings**

- **"ToS not accepted" is invisible to the YouTube API.** No project can exist
  without it, so it reads exactly like "project absent" (400). Via the deep-link
  path the wizard cannot say "accept the Terms first"; the auto-create path can,
  because `projects.create` returns a `TOS` violation (F6) — another reason for
  E1.7.
- **The wizard must key on HTTP status + `errors[0].reason`, not `classify()`.**
  `project_denied` lumps a 400 (absent — keep waiting) with a 403 (no access).
- **The 403 "no access" state is transient on a brand-new own project.** It
  appeared for one poll ~10 s after `service_disabled` first showed. So 403
  `forbidden`/`USER_PROJECT_DENIED` within ~2 min of creation means *keep
  waiting*; only a persistent one means "that's not your project".
- **States are not monotonic** — never auto-advance on a single poll; require
  the state to hold (e.g. 2 consecutive polls) before advancing.
- **F6 confirmed** on a never-used account: first Cloud Console visit shows the
  "Welcome" dialog (country + Terms checkbox + *Agree and continue*). Once
  accepted, `projectcreate` goes straight to the form.
- **F7 confirmed** (random pre-filled id). **F8 revised** — 12 remaining, not ~25.
- E7 (disable after enable) was not run; it's lower value now that auto-create
  can re-enable via API. Fold it into E1.7.

**Exact user actions, deep-link path** (the fallback): (1) choose account,
(2) unverified-app warning: Advanced → Go to (unsafe) *[gone after E2]*,
(3) Allow, (4) GCP Welcome: country + tick Terms + Agree and continue,
(5) `projectcreate`: Edit → type id → Create, (6) enableflow: Next → Enable.
About 10 interactions over four Google pages — the reason auto-create was adopted.

## E1.7 — auto-create the user's project via API

**Adopted 2026-11-27. Needs a second never-used Gmail account** (to see the
`TOS` violation before acceptance) **and** the existing fresh account (Terms
already accepted, 11 projects left).

Goal: prove the user's part shrinks to *Allow* + the one-time Terms, and capture
everything the wizard needs to drive it.

| Step | What | Answers |
|---|---|---|
| 1 | Second, setup-only consent on `soccer-cam-e1-client`: `cloudplatformprojects` + `service.management` (no `cloud-platform`). Screenshot the consent wording | How scary is it, exactly |
| 2 | `projects.create` **before** Terms (account 2) | Exact `TOS` envelope + the URL to send the user to |
| 3 | User accepts Terms; retry `projects.create(soccer-cam-<8>)`; poll the operation | Create → done lag; does the YouTube-side ~92 s lag still apply after the op completes |
| 4 | `services.enable(youtube.googleapis.com)`; poll the operation, then `channels.list` with header | Enable lag via API vs ~7 s via console |
| 5 | Revoke the setup-only token; confirm the youtube token still refreshes and uploads | Whether revoking one grant kills the other (incremental auth / `include_granted_scopes`) |
| 6 | `services.disable` then probe; re-enable via API (former E7) | Wizard can self-heal a disabled API |
| 7 | Re-run on an account at its project quota (F8) | Envelope for "no projects left" |

### E1.7 results so far — 2026-11-27 (run through the shipped code, not the probe)

Run against the real implementation (wizard → Connect YouTube → Set up YouTube
quota) on the E1.6 fresh account (Terms already accepted), publisher client =
`soccer-cam-e1-client` via `SOCCER_CAM_YT_CLIENT_ID/SECRET`.

| Step | Result |
|---|---|
| 1 consent wording | "Manage your Cloud Platform projects", "Manage your Google API service configuration", plus email/"associate you with your personal info" |
| 1 consent layout (2nd run, recorded) | **Granular consent: each Cloud permission is its own checkbox, unticked by default** ("Select what soccer-cam-e1-client can access"). A parent who just clicks Continue grants nothing. The quota callback now checks the granted scopes and asks the user to tick both; project creation never starts with half a grant |
| 2nd end-to-end run | Wizard -> TTT sign-in -> Connect YouTube -> Set up quota on the same account: new `soccer-cam-nn0xo82j`, ready in **80 s**, `has_channel=true` (Mark created a channel at 13:12). Recorded as two GIFs |
| — | **New: the publisher's own project must enable Cloud Resource Manager + Service Usage APIs.** Pre-project calls bill the OAuth client's project; without them `projects.create` → `403 SERVICE_DISABLED` naming project 396182566355. User can't fix it; provisioner now fails with `publisher_setup`. **E2 checklist item.** |
| 3 create → done | ~8 s (operation already done on first poll) |
| 4 enable → done | ~4 s; then `channels.list` with header → stable 200 at **+61 s total** from consent |
| — | Header proven on the upload path: same `build()` client with a bogus quota project → `400 not found`; with the real one → `200` |
| 5 grant persistence | **Online access does not mean the grant disappears.** The Cloud scopes stay listed in the user's Google Account third-party access after Allow; only soccer-cam's copy of the token is never saved. Wizard copy corrected to say so. Revocation test still open |
| 2, 6, 7 | **Not yet run:** `TOS` envelope (needs a second never-used account), disable/re-enable, out-of-projects |

Gate: if step 1's consent is unacceptable to show a soccer parent, or step 5
shows the grants can't be separated, fall back to the deep-link flow and record why.

## The rest — gated on E1

| # | What | Verifies |
|---|---|---|
| E2 | Stand up the publisher OAuth client: branded consent screen, published to **Production**, **enable Cloud Resource Manager + Service Usage APIs on the publisher project** (E1.7), submit brand + sensitive-scope verification (youtube.* **plus** the E1.7 setup scopes `cloudplatformprojects` + `service.management`). **No YouTube API quota-extension audit is needed** -- the publisher project never serves quota | Consent screen loses the warning. ~2 hrs to submit, days-to-weeks for Google |
| E3 | Mint a token against the Production client, park it, re-check at day 8. Also check the token response for `refresh_token_expires_in` | Q6 — kills the weekly re-auth treadmill |
| E4 | **Built 2026-11-27** (`web/youtube_connect.py`, `utils/youtube_quota_project.py`, wizard TTT step). Connect YouTube is a button after TTT sign-in / in the wizard; uploads attach the header via `Credentials.with_quota_project` and fail closed without it. Remaining: a real full-game upload through it | Full game (raw + processed + playlist) lands unlisted, quota moves on the user's project |
| E5 | Negative paths: wrong project id, API not enabled, ToS unaccepted, no `serviceusage.services.use`, project-quota exhausted (F8), **and header missing/invalid** | Each produces a fixable wizard message; the missing-header case refuses to upload rather than silently billing the publisher project |
| E6 | Keep `make_youtube_flow()` BYO alive behind a disclosure | Escape hatch if TTT's client is ever suspended — it is a single point of failure for *identity* even though quota stays distributed |
| E7 | Re-run the probe at service startup, not just setup | A project that later loses its API enablement surfaces as a wizard state, not upload failures |
| E8 | Customer-perspective run: clean Windows box, fresh Google account, CI-built installer with real `_youtube_secrets.py`, no dev shortcuts | The whole thing, as a real camera manager |


### E2 progress — 2026-11-27

Publisher project: **`team-tech-tools-app`** ("Team Tech Tools", org
teamtechtools.com, owner admin@teamtechtools.com, number 435575856533).

- [x] Cloud Resource Manager + Service Usage APIs enabled.
- [x] Data Access: `youtube`, `youtube.upload`, `youtube.readonly`,
      `cloudplatformprojects`, `service.management` (sensitive); `openid`,
      `userinfo.email` (non-sensitive).
- [x] Branding: home/privacy/terms on www.teamtechtools.com; authorized domains
      `teamtechtools.com` plus both TTT Supabase projects (Preview
      `opirofrhbffpszrnzsyp`, Prod `zmuwmngqqiaectpcqlfj`).
- [x] Published to **Production** (was Testing). Until verification the
      unverified-app warning and the 100-user sensitive-scope cap still apply.
- [x] Desktop client `soccer-cam publisher (production)`; its id/secret are the
      repo secrets `SOCCER_CAM_YT_CLIENT_ID` / `SOCCER_CAM_YT_CLIENT_SECRET`.
      One client serves TTT Preview and Prod alike: it only redirects to
      loopback, and the TTT environment is chosen by `[TTT].api_base_url`.
- [ ] Submit verification (Verification Center). Needs teamtechtools.com
      verified in Search Console for admin@teamtechtools.com, a demo video of
      each scope in use, and the scope justifications.

Note: this consent screen is shared with TTT Preview's Google sign-in
(`Web client 1`, redirect to the Preview Supabase callback), so its app name
and publishing status apply there too.

## Decided along the way

- **Quota is always user-owned.** There is no configuration where the publisher
  project supplies quota. That keeps per-user isolation, removes the YouTube API
  quota-extension audit from the critical path, and confines abuse blast radius
  to the offending user's own project.
- **No browser puppeteering of Google** (F10). "Control" means the app opens each
  step and polls for completion, not that it drives the clicks. The Selenium
  TeamSnap dev-portal automation that suggested otherwise was orphaned dead code
  and has been removed on `chore/remove-teamsnap-dev-portal-automation`.
- **2026-11-27 — Auto-create the user's project (reverses the entry below).**
  E1.6 measured the deep-link path at ~10 interactions across four Google pages
  for a user who doesn't want to touch Cloud Console at all. The Terms step stays
  either way (F6), but auto-create removes project creation, id entry and API
  enablement, and lets the wizard detect "Terms not accepted" directly (the
  YouTube API cannot). Cost accepted: two more sensitive scopes in E2's
  verification, requested in a separate setup-only consent and revoked after
  use. Must be settled before E2 is submitted. Proven out by E1.7; the
  deep-link flow stays as the fallback.
- ~~**No auto-creation of the user's project.**~~ *(Reversed 2026-11-27, above.)* It needs two sensitive scopes (F5)
  and still cannot skip the ToS trip (F6), so it buys ~3 clicks at the cost of a
  scarier consent screen and a harder review. Revisit only if E1.6 shows ToS is
  already accepted for a meaningful share of accounts. Note that adding scopes to
  an already-approved app triggers re-verification, so this is a now-decision.
- **The 100-user cap (F3) is not the binding constraint** at ~1 camera manager
  per team, and verification removes it regardless.


---

## Upload limits — measured 2026-08-23

Rig: publisher client `soccer-cam-e1-client`, account `flashparent4`, channel
"Throwaway" (`longUploadsStatus: eligible`), quota project `soccer-cam-e2-15a668`.

| # | Finding | Evidence |
|---|---|---|
| U1 | `videos.insert` is **not** 1600 units against a 10,000/day pool | 11 uploads on one account and 38 on another with no unit-quota refusal; the old model predicts failure at #7 |
| U2 | The binding limit is **account-level, not API quota** | Refused at #39 with `400 uploadLimitExceeded`, "The user has exceeded the number of videos they may upload", carrying **no** `quota_metric`/`quota_limit` metadata. The project's `videos.insert` bucket still had ~60 left |
| U3 | `uploadLimitExceeded` is a **rolling window of ~75-90 min**, not a daily reset | Probed every 15 min: refused at +0/+15/+30/+45/+60/+75, accepted at +90 |
| U4 | Capacity returns **one slot at a time** | The +90 min success consumed the slot; the next attempt 85 s later was refused again |
| U5 | YouTube can **accept an upload and then destroy it** | `200` + a real video id, then `videos.list` returns 0 items permanently. Only trace is a `"Deleted video"` tombstone in the uploads playlist. There is no `rejectionReason` to read -- the object is gone, so no field survives to carry one |
| U6 | Long uploads require channel verification | `youtube.com/verify` by SMS or voice call, then 12 h / 256 GB. Detectable as `longUploadsStatus`: `allowed` / `eligible` / `disallowed`. Videos rejected before verification are destroyed, not held -- Google's docs say to re-upload |
| U7 | There is a **propagation window of ~10-20 s** | Readback immediately after insert returns null status fields; `videos.delete` returned 404 for 8 of 11 videos seconds after upload and succeeded on retry |
| U8 | `channels.list` returns **200 with empty `items`** when the Google account has no YouTube channel | Not an error. Sign-in looks healthy and uploads fail later for an unrelated-looking reason |
| U9 | `statistics.videoCount` counts **public videos only** | Reported 0 for a channel holding 251 unlisted videos. Useless for detecting whether a channel is in use |
| U10 | Very short clips are **unreliable test data** | 30 of 38 two-second clips were tombstoned while the 2-minute control survived. Anything that short plausibly goes down the Shorts pipeline. Use >= 2 min clips for any future test |

| U11 | The duration limit **destroys long uploads on the production path too**, silently | 16-min via `MediaFileUpload(resumable=True)`: `next_chunk()` raised nothing, insert returned id `m9Rc44xume4`, then 0 items through t+240s. Two 2-minute controls on the same channel the same hour both reached `processed` and are alive |
| U12 | `uploadStatus: uploaded` is a **transient state that lies** | The doomed 16-min video reported `upload=uploaded reject=None privacy=unlisted` at t+10s, then vanished. Presence alone is not proof of success -- only `uploadStatus: processed` is. Both controls reached `processed` within ~20 s |

Duration failure is now n=2 per condition across two independent upload paths
(multipart and resumable), each with a contemporaneous 2-minute control that
survived. Treated as established.

### Not established

- **Whether the user is notified** when a video is destroyed.
- **The exact duration threshold.** Tested 16 min vs 2 min; the 15:00 boundary
  itself is untested.
- **How `uploadLimitExceeded` recovers.** Two intervals measured after a
  38-upload burst: +92 min for the first slot, +202 min for the second. Not a
  fixed window; possibly an adaptive throttle. Do not model it -- poll, which is
  what `base_queue_processor.py` already does.

### Corrections to earlier claims

- The deferral is **not** midnight-Pacific. `base_queue_processor.py:258`
  already polls on a bounded interval (`quota_retry_minutes`, default 30) and
  documents the same conclusion from 2026-08-04. The measured 75-90 min window
  confirms that design and shows 30 min is well matched.
  `docs/TROUBLESHOOTING.md` was stale and has been corrected.
- The claim that **burst uploading triggers mass discard** is probably wrong.
  The deaths track clip length (U10), not burst timing.

### Product actions

1. **Pre-flight `longUploadsStatus`** before any upload over 15 minutes; refuse
   with a pointer to `youtube.com/verify` rather than burning a 90-minute upload.
2. **Require `uploadStatus: processed`.** Not the insert response (U11), not
   mere presence, and not `uploaded` -- that state appears ~10 s in on videos
   that are about to be destroyed (U12). Poll until `processed`, and treat
   both disappearance and timeout as failure. Checking `rejectionReason` does
   not work at all: the video object is gone, so no field survives to carry
   one (U5).
3. **Handle the no-channel case** (U8) as its own wizard state.
4. **Fix the wizard's quota text** in `web/setup/router.py`, which still claims
   ~3 games/day per OAuth client.
