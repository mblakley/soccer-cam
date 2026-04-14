"""Tests for the TeamSnap dev portal Selenium automation.

Selenium calls are stubbed with a tiny FakeDriver so the suite runs
without Chrome. The point of these tests is to lock in the contract
(what selectors get queried, what URLs get visited, and how the helper
turns a successful page into a TeamSnapCredentials object).
"""

from typing import Callable
from unittest.mock import patch

import pytest

from video_grouper.api_integrations.teamsnap_dev_portal_automation import (
    DEFAULT_APP_NAME,
    DEFAULT_REDIRECT_URI,
    TeamSnapAutomationError,
    obtain_teamsnap_credentials,
)


# ---------------------------------------------------------------------------
# Tiny fake driver — strongly typed enough to exercise the SUT
# ---------------------------------------------------------------------------


class FakeElement:
    def __init__(
        self,
        text: str = "",
        attrs: dict | None = None,
        on_click: Callable[[], None] | None = None,
    ):
        self.text = text
        self._attrs = attrs or {}
        self._on_click = on_click
        self.cleared = False
        self.sent_keys: list[str] = []

    def get_attribute(self, name: str):
        return self._attrs.get(name)

    def clear(self):
        self.cleared = True

    def send_keys(self, value: str):
        self.sent_keys.append(value)

    def click(self):
        if self._on_click:
            self._on_click()

    def find_element(self, by, selector: str):
        return self._attrs["__nested__"][selector]


class FakeDriver:
    """Minimal selenium-like driver that records URLs and returns
    pre-registered elements for selectors.

    ``redirect_map`` lets a test simulate TeamSnap's
    "unauthenticated visit to /oauth/applications/new bounces to /login"
    behavior — keys are requested URLs, values are the URL the driver
    actually ends up on. URLs not in the map land where requested.
    """

    def __init__(self):
        self.current_url = ""
        self.visited: list[str] = []
        # Per-URL element maps: { url: { selector: element_or_list } }
        self.elements_per_url: dict[str, dict] = {}
        # Default elements applied on any URL
        self.default_elements: dict = {}
        # Optional URL → effective URL redirects
        self.redirect_map: dict[str, str] = {}

    def get(self, url: str):
        self.visited.append(url)
        self.current_url = self.redirect_map.get(url, url)

    def find_element(self, by, selector: str):
        # Per-URL takes precedence
        per_url = self.elements_per_url.get(self.current_url, {})
        if selector in per_url:
            entry = per_url[selector]
            return entry[0] if isinstance(entry, list) else entry
        if selector in self.default_elements:
            entry = self.default_elements[selector]
            return entry[0] if isinstance(entry, list) else entry
        raise _make_no_such_element(f"{by!s} {selector}")

    def find_elements(self, by, selector: str):
        per_url = self.elements_per_url.get(self.current_url, {})
        if selector in per_url:
            entry = per_url[selector]
            return entry if isinstance(entry, list) else [entry]
        if selector in self.default_elements:
            entry = self.default_elements[selector]
            return entry if isinstance(entry, list) else [entry]
        return []

    def quit(self):
        pass


def _make_no_such_element(msg: str):
    from selenium.common.exceptions import NoSuchElementException

    return NoSuchElementException(msg)


def _build_login_elements(driver: FakeDriver, after_login_url: str):
    """Register login form elements that, on submit, advance the URL."""
    email = FakeElement()
    password = FakeElement()

    def submit_action():
        driver.current_url = after_login_url

    submit = FakeElement(on_click=submit_action)
    return email, password, submit


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@patch(
    "video_grouper.api_integrations.teamsnap_dev_portal_automation.time.sleep",
    lambda *_args, **_kwargs: None,
)
def test_obtain_credentials_creates_new_app_when_none_exists():
    driver = FakeDriver()
    # Unauthenticated visits to /oauth/applications/new bounce to /login,
    # exactly like the real TeamSnap dev portal.
    driver.redirect_map["https://auth.teamsnap.com/oauth/applications/new"] = (
        "https://auth.teamsnap.com/login"
    )

    email, password, submit = _build_login_elements(
        driver, after_login_url="https://auth.teamsnap.com/oauth/applications"
    )

    # After a successful login, subsequent visits to NEW_APP_URL should
    # land on the form (no more redirect).
    def remove_redirect():
        driver.redirect_map.pop(
            "https://auth.teamsnap.com/oauth/applications/new", None
        )
        driver.current_url = "https://auth.teamsnap.com/oauth/applications"

    submit._on_click = remove_redirect

    name_field = FakeElement()
    desc_field = FakeElement()
    redirect_field = FakeElement()

    def commit_action():
        driver.current_url = "https://auth.teamsnap.com/oauth/applications/9999"

    commit = FakeElement(on_click=commit_action)

    client_id_code = FakeElement(text="abc-client-id")
    client_secret_code = FakeElement(text="def-client-secret")

    # On the login page, the username + password + submit live
    driver.elements_per_url["https://auth.teamsnap.com/login"] = {
        "input#username": email,
        "input#password": password,
        "button[type='submit']": submit,
    }
    # Listing page → no rows
    driver.elements_per_url["https://auth.teamsnap.com/oauth/applications"] = {
        "table tbody tr": [],
    }
    # New application form
    driver.elements_per_url["https://auth.teamsnap.com/oauth/applications/new"] = {
        "input#doorkeeper_application_name": name_field,
        "input#doorkeeper_application_description": desc_field,
        "textarea#doorkeeper_application_redirect_uri": redirect_field,
        "input[type='submit'][name='commit']": commit,
    }
    # Detail page after create
    driver.elements_per_url["https://auth.teamsnap.com/oauth/applications/9999"] = {
        "code#client_id": client_id_code,
        "code#client_secret": client_secret_code,
    }

    with (
        patch(
            "video_grouper.api_integrations.teamsnap_dev_portal_automation."
            "WebDriverWait"
        ) as mock_wait,
        patch(
            "video_grouper.api_integrations.teamsnap_dev_portal_automation.webdriver"
        ) as mock_webdriver,
    ):
        mock_webdriver.Chrome.return_value = driver
        # WebDriverWait().until(condition) → just return the same kind of
        # element a synchronous find would, by routing through the driver.
        mock_wait.return_value.until.side_effect = lambda condition: condition(driver)

        result = obtain_teamsnap_credentials(
            email="user@example.com",
            password="pw",
            headless=True,
        )

    assert result.client_id == "abc-client-id"
    assert result.client_secret == "def-client-secret"
    assert result.application_id == "9999"
    assert result.reused_existing is False

    # Form was filled with the expected defaults
    assert name_field.sent_keys == [DEFAULT_APP_NAME]
    assert redirect_field.sent_keys == [DEFAULT_REDIRECT_URI]


@patch(
    "video_grouper.api_integrations.teamsnap_dev_portal_automation.time.sleep",
    lambda *_args, **_kwargs: None,
)
def test_obtain_credentials_reuses_existing_app():
    driver = FakeDriver()
    driver.redirect_map["https://auth.teamsnap.com/oauth/applications/new"] = (
        "https://auth.teamsnap.com/login"
    )

    email, password, submit = _build_login_elements(
        driver, after_login_url="https://auth.teamsnap.com/oauth/applications"
    )

    def remove_redirect():
        driver.redirect_map.pop(
            "https://auth.teamsnap.com/oauth/applications/new", None
        )
        driver.current_url = "https://auth.teamsnap.com/oauth/applications"

    submit._on_click = remove_redirect

    # Existing app row pointing to /oauth/applications/4242
    existing_link = FakeElement(
        text=DEFAULT_APP_NAME,
        attrs={"href": "https://auth.teamsnap.com/oauth/applications/4242"},
    )

    class FakeRow:
        def find_element(self, by, selector):
            if "oauth/applications/" in selector:
                return existing_link
            raise _make_no_such_element(selector)

    client_id_code = FakeElement(text="reused-client-id")
    client_secret_code = FakeElement(text="reused-client-secret")

    driver.elements_per_url["https://auth.teamsnap.com/login"] = {
        "input#username": email,
        "input#password": password,
        "button[type='submit']": submit,
    }
    driver.elements_per_url["https://auth.teamsnap.com/oauth/applications"] = {
        "table tbody tr": [FakeRow()],
    }
    driver.elements_per_url["https://auth.teamsnap.com/oauth/applications/4242"] = {
        "code#client_id": client_id_code,
        "code#client_secret": client_secret_code,
    }

    with (
        patch(
            "video_grouper.api_integrations.teamsnap_dev_portal_automation."
            "WebDriverWait"
        ) as mock_wait,
        patch(
            "video_grouper.api_integrations.teamsnap_dev_portal_automation.webdriver"
        ) as mock_webdriver,
    ):
        mock_webdriver.Chrome.return_value = driver
        mock_wait.return_value.until.side_effect = lambda condition: condition(driver)

        result = obtain_teamsnap_credentials(
            email="user@example.com",
            password="pw",
            headless=True,
        )

    assert result.client_id == "reused-client-id"
    assert result.client_secret == "reused-client-secret"
    assert result.application_id == "4242"
    assert result.reused_existing is True


@patch(
    "video_grouper.api_integrations.teamsnap_dev_portal_automation.time.sleep",
    lambda *_args, **_kwargs: None,
)
def test_obtain_credentials_fails_when_login_does_not_advance():
    driver = FakeDriver()
    # Stay stuck on /login regardless of clicks
    driver.redirect_map["https://auth.teamsnap.com/oauth/applications/new"] = (
        "https://auth.teamsnap.com/login"
    )

    email = FakeElement()
    password = FakeElement()
    # Submit click is a no-op — URL stays on /login
    submit = FakeElement(on_click=lambda: None)

    driver.elements_per_url["https://auth.teamsnap.com/login"] = {
        "input#username": email,
        "input#password": password,
        "button[type='submit']": submit,
    }

    with (
        patch(
            "video_grouper.api_integrations.teamsnap_dev_portal_automation."
            "WebDriverWait"
        ) as mock_wait,
        patch(
            "video_grouper.api_integrations.teamsnap_dev_portal_automation.webdriver"
        ) as mock_webdriver,
    ):
        mock_webdriver.Chrome.return_value = driver
        mock_wait.return_value.until.side_effect = lambda condition: condition(driver)

        with pytest.raises(TeamSnapAutomationError, match="login failed"):
            obtain_teamsnap_credentials(
                email="user@example.com",
                password="bad",
                headless=True,
            )


@patch(
    "video_grouper.api_integrations.teamsnap_dev_portal_automation.time.sleep",
    lambda *_args, **_kwargs: None,
)
def test_obtain_credentials_fails_when_credentials_missing_on_detail_page():
    driver = FakeDriver()
    driver.redirect_map["https://auth.teamsnap.com/oauth/applications/new"] = (
        "https://auth.teamsnap.com/login"
    )

    email, password, submit = _build_login_elements(
        driver, after_login_url="https://auth.teamsnap.com/oauth/applications"
    )

    def remove_redirect():
        driver.redirect_map.pop(
            "https://auth.teamsnap.com/oauth/applications/new", None
        )
        driver.current_url = "https://auth.teamsnap.com/oauth/applications"

    submit._on_click = remove_redirect

    name_field = FakeElement()
    desc_field = FakeElement()
    redirect_field = FakeElement()

    def commit_action():
        driver.current_url = "https://auth.teamsnap.com/oauth/applications/8888"

    commit = FakeElement(on_click=commit_action)

    driver.elements_per_url["https://auth.teamsnap.com/login"] = {
        "input#username": email,
        "input#password": password,
        "button[type='submit']": submit,
    }
    driver.elements_per_url["https://auth.teamsnap.com/oauth/applications"] = {
        "table tbody tr": [],
    }
    driver.elements_per_url["https://auth.teamsnap.com/oauth/applications/new"] = {
        "input#doorkeeper_application_name": name_field,
        "input#doorkeeper_application_description": desc_field,
        "textarea#doorkeeper_application_redirect_uri": redirect_field,
        "input[type='submit'][name='commit']": commit,
    }
    # Detail page lacks code#client_id — find_element will raise

    with (
        patch(
            "video_grouper.api_integrations.teamsnap_dev_portal_automation."
            "WebDriverWait"
        ) as mock_wait,
        patch(
            "video_grouper.api_integrations.teamsnap_dev_portal_automation.webdriver"
        ) as mock_webdriver,
    ):
        mock_webdriver.Chrome.return_value = driver
        mock_wait.return_value.until.side_effect = lambda condition: condition(driver)

        with pytest.raises(TeamSnapAutomationError, match="client_id"):
            obtain_teamsnap_credentials(
                email="user@example.com",
                password="pw",
                headless=True,
            )
