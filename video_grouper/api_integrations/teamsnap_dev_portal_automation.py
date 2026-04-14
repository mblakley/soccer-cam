"""TeamSnap developer portal automation (one-shot, onboarding only).

Drives a headed (or headless) Chrome session through TeamSnap's
Doorkeeper-based OAuth dev portal so the tray onboarding wizard can
provision a per-user OAuth ``client_id`` / ``client_secret`` without
making the coach visit ``developers.teamsnap.com`` themselves.

This module is **only** used during onboarding. The sync path uses
``video_grouper/api_integrations/teamsnap.py`` (pure HTTP) — Selenium
never runs after the credentials are in TTT.

DOM selectors come from a manual reconnaissance pass on
2026-04-14:

    Login (https://auth.teamsnap.com/login)
      - input#username
      - input#password
      - button[type='submit']  (text "LOG IN")

    Listing (https://auth.teamsnap.com/oauth/applications)
      - table tbody tr  — first column is the app name
      - app detail href: a[href^='/oauth/applications/'] (digits only)

    New application (https://auth.teamsnap.com/oauth/applications/new)
      - input#doorkeeper_application_name
      - input#doorkeeper_application_description
      - textarea#doorkeeper_application_redirect_uri
      - input[type='submit'][name='commit']

    App detail / post-create (https://auth.teamsnap.com/oauth/applications/{id})
      - code#client_id     (text content is the client_id)
      - code#client_secret (text content is the client_secret)

The ``redirect_uri`` field accepts the OAuth out-of-band sentinel
``urn:ietf:wg:oauth:2.0:oob``, which is what we use because TTT's
``client_credentials`` flow never redirects.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

LOGIN_URL = "https://auth.teamsnap.com/login"
APPS_LIST_URL = "https://auth.teamsnap.com/oauth/applications"
NEW_APP_URL = "https://auth.teamsnap.com/oauth/applications/new"

DEFAULT_APP_NAME = "TTT Integration"
DEFAULT_APP_DESCRIPTION = (
    "Team Tech Tools schedule sync — pulls game schedules into TTT."
)
DEFAULT_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"

_LOGIN_WAIT_SECONDS = 4
_FORM_WAIT_SECONDS = 3


class TeamSnapAutomationError(Exception):
    """Raised when the Selenium flow can't complete end-to-end."""


@dataclass
class TeamSnapCredentials:
    client_id: str
    client_secret: str
    application_id: str
    reused_existing: bool


def obtain_teamsnap_credentials(
    *,
    email: str,
    password: str,
    headless: bool = False,
    app_name: str = DEFAULT_APP_NAME,
    app_description: str = DEFAULT_APP_DESCRIPTION,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
) -> TeamSnapCredentials:
    """Run the one-shot TeamSnap dev-portal automation.

    Logs in, looks for an existing app named ``app_name`` and reuses it
    if found, otherwise creates a new application and scrapes the
    resulting credentials.

    Raises ``TeamSnapAutomationError`` on any step failure so the tray
    wizard can fall back to the manual paste flow with a useful message.
    """
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,1000")

    try:
        driver = webdriver.Chrome(options=options)
    except WebDriverException as e:
        raise TeamSnapAutomationError(
            f"Could not start Chrome for TeamSnap onboarding: {e}"
        ) from e

    wait = WebDriverWait(driver, 20)

    try:
        # ── Step 1: log in ────────────────────────────────────────
        # Visiting the new-app URL while unauthenticated bounces us
        # to /login, which is where the form actually lives.
        driver.get(NEW_APP_URL)
        try:
            email_field = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input#username"))
            )
        except TimeoutException as e:
            raise TeamSnapAutomationError(
                "TeamSnap login page did not load in time"
            ) from e

        password_field = driver.find_element(By.CSS_SELECTOR, "input#password")

        email_field.clear()
        email_field.send_keys(email)
        password_field.clear()
        password_field.send_keys(password)

        try:
            submit = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        except NoSuchElementException as e:
            raise TeamSnapAutomationError(
                "TeamSnap login page changed shape — could not find submit button"
            ) from e
        submit.click()

        time.sleep(_LOGIN_WAIT_SECONDS)
        if "/login" in driver.current_url:
            raise TeamSnapAutomationError(
                "TeamSnap login failed (still on /login). "
                "Check the email and password, or sign in manually to clear "
                "any 2FA / CAPTCHA challenges."
            )

        # ── Step 2: try to reuse an existing app named `app_name` ─
        existing = _find_existing_app(driver, app_name)
        if existing:
            credentials = _scrape_credentials(driver, existing)
            return TeamSnapCredentials(
                client_id=credentials[0],
                client_secret=credentials[1],
                application_id=existing,
                reused_existing=True,
            )

        # ── Step 3: create a new app ──────────────────────────────
        driver.get(NEW_APP_URL)
        try:
            name_field = wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "input#doorkeeper_application_name")
                )
            )
        except TimeoutException as e:
            raise TeamSnapAutomationError(
                "TeamSnap new-application form did not load in time"
            ) from e

        desc_field = driver.find_element(
            By.CSS_SELECTOR, "input#doorkeeper_application_description"
        )
        redirect_field = driver.find_element(
            By.CSS_SELECTOR, "textarea#doorkeeper_application_redirect_uri"
        )
        commit_btn = driver.find_element(
            By.CSS_SELECTOR, "input[type='submit'][name='commit']"
        )

        name_field.clear()
        name_field.send_keys(app_name)
        desc_field.clear()
        desc_field.send_keys(app_description)
        redirect_field.clear()
        redirect_field.send_keys(redirect_uri)

        commit_btn.click()
        time.sleep(_FORM_WAIT_SECONDS)

        # After create, TeamSnap redirects to /oauth/applications/{id}
        if "/oauth/applications/" not in driver.current_url:
            raise TeamSnapAutomationError(
                "TeamSnap did not redirect to the application page after "
                f"submit (now at {driver.current_url}). The app may not "
                "have been created."
            )

        # Pull the app id out of the URL for return
        try:
            app_id = (
                driver.current_url.split("/oauth/applications/")[-1]
                .split("/")[0]
                .split("?")[0]
            )
        except Exception:
            app_id = ""

        client_id, client_secret = _scrape_credentials_on_current_page(driver)
        return TeamSnapCredentials(
            client_id=client_id,
            client_secret=client_secret,
            application_id=app_id,
            reused_existing=False,
        )

    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _find_existing_app(driver, app_name: str) -> Optional[str]:
    """Return the existing application id if one named ``app_name`` exists.

    Walks the listing table and matches on the first cell text. Returns
    None if not found or if the listing page can't be parsed.
    """
    try:
        driver.get(APPS_LIST_URL)
        time.sleep(_FORM_WAIT_SECONDS)
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        for row in rows:
            try:
                # Find the first link in this row — it points to the app detail page
                link = row.find_element(
                    By.CSS_SELECTOR, "a[href*='/oauth/applications/']"
                )
                text = (link.text or "").strip()
                if text and text.lower() == app_name.lower():
                    href = link.get_attribute("href") or ""
                    # Extract the numeric application id from the href
                    return (
                        href.split("/oauth/applications/")[-1]
                        .split("/")[0]
                        .split("?")[0]
                    )
            except NoSuchElementException:
                continue
    except Exception as e:
        logger.warning("TeamSnap listing scan failed: %s", e)
    return None


def _scrape_credentials(driver, application_id: str) -> tuple[str, str]:
    """Visit the app detail page and pull (client_id, client_secret)."""
    driver.get(f"{APPS_LIST_URL}/{application_id}")
    time.sleep(_FORM_WAIT_SECONDS)
    return _scrape_credentials_on_current_page(driver)


def _scrape_credentials_on_current_page(driver) -> tuple[str, str]:
    """Pull client_id / client_secret from a Doorkeeper application page.

    The post-create page renders both as ``<code id='client_id'>`` and
    ``<code id='client_secret'>``. Fail loudly if either is missing
    rather than guessing.
    """
    try:
        client_id_el = driver.find_element(By.CSS_SELECTOR, "code#client_id")
        client_secret_el = driver.find_element(By.CSS_SELECTOR, "code#client_secret")
    except NoSuchElementException as e:
        raise TeamSnapAutomationError(
            "Could not find client_id / client_secret on the TeamSnap "
            "application page. The dev portal layout may have changed — "
            "fall back to manual paste."
        ) from e

    client_id = (client_id_el.text or "").strip()
    client_secret = (client_secret_el.text or "").strip()
    if not client_id or not client_secret:
        raise TeamSnapAutomationError(
            "TeamSnap application page returned empty credentials"
        )
    return client_id, client_secret
