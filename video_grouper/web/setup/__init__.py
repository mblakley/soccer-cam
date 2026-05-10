"""Onboarding wizard at ``/setup/*`` — replaces ``tray/onboarding_wizard.py``.

Multi-page web flow with cookie-keyed in-memory state. Each page is a
GET (renders form prefilled with state) + POST (validates the field,
saves to state, redirects to the next page). On the final summary the
state is materialized into a `Config` and saved to disk.

Scope: the bare minimum to take a fresh install to a working config —
welcome, storage, camera, **youtube**, summary. The YouTube step is in
the wizard because YouTube uploads need an interactive OAuth flow that
the user must complete in the browser, and we want to walk them
through the one-time GCP setup the first time. The other integration
sections (NTFY/PlayMetrics/TeamSnap) get configured via ``/config``
after the wizard finishes; they don't need wizard pages of their own.
"""
