"""E1 -- does YouTube Data API honor x-goog-user-project?

Investigation tool for .claude/plans/youtube-byo-quota/ROADMAP.md. Delete once
the questions it answers (Q1-Q5) are recorded there.

Uses raw requests rather than googleapiclient so the header is explicit and the
exact error envelope is visible -- the wizard's auto-advance depends on telling
those envelopes apart.

Each step is a separate subcommand so side effects stay opt-in. Steps 3 and 4
cost real quota and step 3 publishes to a real channel; neither runs unless
named. Run order matters: upload BEFORE burning quota.

    python e1_quota_project_probe.py baseline
    python e1_quota_project_probe.py step1 --quota-project YYY
    python e1_quota_project_probe.py step2 --quota-project YYY
    python e1_quota_project_probe.py step3 --quota-project YYY --video FILE --i-mean-it
    python e1_quota_project_probe.py step4 --quota-project YYY --calls 84
    python e1_quota_project_probe.py step5 --quota-project YYY --client-project XXX
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

YT = "https://youtube.googleapis.com/youtube/v3"
UPLOAD = "https://youtube.googleapis.com/upload/youtube/v3/videos"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube",
]
DEFAULT_STORAGE = Path(r"C:\Users\markb\projects\soccer-cam\shared_data")


def paths(args) -> tuple[Path, Path]:
    """Credential locations. Explicit flags win; otherwise the storage layout."""
    secret = args.client_secret or (args.storage / "youtube" / "client_secret.json")
    token = args.token_file or (args.storage / "youtube" / "token.json")
    return Path(secret), Path(token)


def load_creds(secret_file: Path, token_file: Path) -> Credentials:
    """Refresh an existing token. No browser, no re-auth."""
    data = json.loads(token_file.read_text())
    blob = json.loads(secret_file.read_text())
    cfg = blob.get("installed") or blob.get("web")
    data.setdefault("client_id", cfg["client_id"])
    data.setdefault("client_secret", cfg["client_secret"])
    data.setdefault(
        "token_uri", cfg.get("token_uri", "https://oauth2.googleapis.com/token")
    )
    creds = Credentials.from_authorized_user_info(data, SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        token_file.write_text(creds.to_json())
        print("  (token refreshed and saved)")
    print(f"  scopes on token: {list(creds.scopes or [])}")
    return creds


def call(creds, method, url, quota_project=None, **kw):
    """One API call, with the quota-project header when asked. Reports the
    full error envelope -- status, reason, domain, message."""
    headers = {"Authorization": f"Bearer {creds.token}"}
    if quota_project:
        headers["x-goog-user-project"] = quota_project
    resp = requests.request(method, url, headers=headers, timeout=60, **kw)

    label = (
        f"x-goog-user-project: {quota_project}" if quota_project else "no quota header"
    )
    print(f"  [{resp.status_code}] {label}")
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {})
            print(f"      status : {err.get('status')}")
            print(f"      message: {err.get('message', '')[:300]}")
            for d in err.get("errors", []) or []:
                print(f"      reason : {d.get('reason')} (domain={d.get('domain')})")
            for d in err.get("details", []) or []:
                if d.get("reason"):
                    print(f"      detail : {d.get('reason')} / {d.get('metadata', {})}")
        except ValueError:
            print(f"      body   : {resp.text[:300]}")
    return resp


def channels_me(creds, quota_project=None):
    return call(
        creds,
        "GET",
        f"{YT}/channels",
        quota_project=quota_project,
        params={"part": "id", "mine": "true"},
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "step",
        choices=["auth", "baseline", "step1", "step2", "step3", "step4", "step5"],
    )
    ap.add_argument(
        "--port",
        type=int,
        default=8080,
        help="auth: loopback port -- must match a registered redirect URI",
    )
    ap.add_argument("--quota-project", help="project Y -- the camera manager's project")
    ap.add_argument(
        "--client-project", help="project X -- the project owning the OAuth client"
    )
    ap.add_argument("--storage", type=Path, default=DEFAULT_STORAGE)
    ap.add_argument(
        "--client-secret",
        type=Path,
        help="Desktop OAuth client JSON for the CLIENT project",
    )
    ap.add_argument("--token-file", type=Path, help="where to read/write the token")
    ap.add_argument("--video", type=Path, help="step3: small file to upload")
    ap.add_argument("--calls", type=int, default=84, help="step4: search.list calls")
    ap.add_argument(
        "--i-mean-it",
        action="store_true",
        help="step3: required -- publishes to a real channel",
    )
    args = ap.parse_args()

    print(f"== {args.step} ==")

    if args.step == "auth":
        # One-time browser sign-in, reusing the existing client. Writes a fresh
        # token beside it so every later step runs unattended.
        from google_auth_oauthlib.flow import InstalledAppFlow

        secret_file, token_file = paths(args)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), SCOPES)
        creds = flow.run_local_server(port=args.port, prompt="consent")
        token_file.write_text(creds.to_json())
        print(f"  token written to {token_file}")
        print(f"  scopes: {list(creds.scopes or [])}")
        return 0

    secret_file, token_file = paths(args)
    creds = load_creds(secret_file, token_file)

    if args.step == "baseline":
        print("Sanity: no header at all. Expect 200.")
        channels_me(creds)
        return 0

    if not args.quota_project:
        print("--quota-project is required")
        return 2

    if args.step in ("step1", "step2"):
        # step1: API still DISABLED in Y. SERVICE_DISABLED => header honored.
        #        200 => header ignored => design is dead.
        # step2: same call once Y has the API enabled. Expect 200.
        channels_me(creds, quota_project=args.quota_project)
        return 0

    if args.step == "step3":
        if not args.i_mean_it or not args.video:
            print(
                "step3 needs --video and --i-mean-it (it publishes to a real channel)"
            )
            return 2
        meta = {
            "snippet": {
                "title": "soccer-cam E1 quota probe -- safe to delete",
                "description": "Throwaway upload for the x-goog-user-project experiment.",
            },
            "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False},
        }
        with open(args.video, "rb") as fh:
            resp = call(
                creds,
                "POST",
                UPLOAD,
                quota_project=args.quota_project,
                params={"part": "snippet,status", "uploadType": "multipart"},
                files={
                    "metadata": ("metadata", json.dumps(meta), "application/json"),
                    "media": ("media", fh, "video/mp4"),
                },
            )
        if resp.ok:
            vid = resp.json()["id"]
            print(f"  uploaded id={vid} -- reading privacyStatus back")
            back = call(
                creds,
                "GET",
                f"{YT}/videos",
                quota_project=args.quota_project,
                params={"part": "status", "id": vid},
            )
            if back.ok:
                items = back.json().get("items", [])
                st = items[0]["status"] if items else {}
                print(
                    f"  privacyStatus={st.get('privacyStatus')} "
                    f"uploadStatus={st.get('uploadStatus')} "
                    f"rejectionReason={st.get('rejectionReason')}"
                )
            print(
                f"  DELETE THIS MANUALLY: https://studio.youtube.com/video/{vid}/edit"
            )
        return 0

    if args.step == "step4":
        # Drain Y's daily quota: search.list is 100 units a call.
        spent = 0
        for i in range(args.calls):
            r = call(
                creds,
                "GET",
                f"{YT}/search",
                quota_project=args.quota_project,
                params={"part": "id", "q": f"probe{i}", "maxResults": "1"},
            )
            spent += 100
            # Refusal arrives as 429/RESOURCE_EXHAUSTED for per-day caps and
            # 403/quotaExceeded for the unit budget -- catch both, or the loop
            # burns through the refusal and misreports "no refusal".
            if r.status_code in (403, 429) and "quota" in r.text.lower():
                print(f"  quota refused after ~{spent} units ({i} calls)")
                return 0
        print(f"  burned ~{spent} units without refusal -- rerun with more --calls")
        return 0

    if args.step == "step5":
        # The proof. Y should now refuse; X (or no header) should still answer.
        print("With Y as quota project -- expect 403 quotaExceeded:")
        channels_me(creds, quota_project=args.quota_project)
        if args.client_project:
            print("With X as quota project -- expect 200:")
            channels_me(creds, quota_project=args.client_project)
        print("With no header -- expect 200:")
        channels_me(creds)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
