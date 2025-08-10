"""
Mock service integration for end-to-end testing.

This module provides factory functions and utilities to seamlessly integrate
mock services with the existing service infrastructure when running in test mode.
"""

import logging
import os

logger = logging.getLogger(__name__)


def should_use_mock_services() -> bool:
    """
    Determine if mock services should be used based on environment or config.

    Returns:
        True if mock services should be used, False otherwise
    """
    # Check environment variables
    use_mock = (
        os.environ.get("USE_MOCK_TEAMSNAP", "").lower() in ("true", "1", "yes")
        or os.environ.get("USE_MOCK_PLAYMETRICS", "").lower() in ("true", "1", "yes")
        or os.environ.get("USE_MOCK_SERVICES", "").lower() in ("true", "1", "yes")
    )

    return use_mock


def create_teamsnap_service(config):
    """
    Create TeamSnap service - mock or real based on configuration.

    Args:
        config: TeamSnap configuration object

    Returns:
        TeamSnapService instance (mock or real)
    """
    if should_use_mock_services():
        logger.info("Creating mock TeamSnap service for testing")
        from video_grouper.api_integrations.mock_teamsnap import MockTeamSnapService

        return MockTeamSnapService(config)
    else:
        logger.info("Creating real TeamSnap service")
        from .teamsnap_service import TeamSnapService

        return TeamSnapService(config)


def create_playmetrics_service(config):
    """
    Create PlayMetrics service - mock or real based on configuration.

    Args:
        config: PlayMetrics configuration object

    Returns:
        PlayMetricsService instance (mock or real)
    """
    if should_use_mock_services():
        logger.info("Creating mock PlayMetrics service for testing")
        from video_grouper.api_integrations.mock_playmetrics import (
            MockPlayMetricsService,
        )

        return MockPlayMetricsService(config)
    else:
        logger.info("Creating real PlayMetrics service")
        from .playmetrics_service import PlayMetricsService

        return PlayMetricsService(config)


def initialize_mock_services():
    """
    Initialize all mock services and patches for testing.

    This function should be called early in the test setup process to ensure
    all mock services are properly configured before the application starts.
    """
    if should_use_mock_services():
        logger.info("Initializing mock services for end-to-end testing")

        logger.info("Mock services initialization completed")
    else:
        logger.info("Using real services (mock services disabled)")


class MockServiceWrapper:
    """
    A wrapper that can dynamically switch between mock and real services.

    This is useful for services that need to be created at runtime and
    may need to switch between mock and real implementations.
    """

    def __init__(
        self, service_type: str, config, real_service_class, mock_service_class
    ):
        """
        Initialize the service wrapper.

        Args:
            service_type: Type of service (for logging)
            config: Service configuration
            real_service_class: Class for real service implementation
            mock_service_class: Class for mock service implementation
        """
        self.service_type = service_type
        self.config = config
        self.real_service_class = real_service_class
        self.mock_service_class = mock_service_class
        self._service = None

        # Create the appropriate service instance
        self._create_service()

    def _create_service(self):
        """Create the appropriate service instance."""
        if should_use_mock_services():
            logger.info(f"Creating mock {self.service_type} service")
            self._service = self.mock_service_class(self.config)
        else:
            logger.info(f"Creating real {self.service_type} service")
            self._service = self.real_service_class(self.config)

    def __getattr__(self, name):
        """Delegate attribute access to the wrapped service."""
        return getattr(self._service, name)

    def __call__(self, *args, **kwargs):
        """Make the wrapper callable if the service is callable."""
        return self._service(*args, **kwargs)
