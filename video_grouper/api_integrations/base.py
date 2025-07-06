"""
Base types for API integrations.
"""

from typing import TypedDict, Union


class ApiResponse(TypedDict, total=False):
    """Represents a generic API response."""

    # Standard response fields
    success: bool
    status_code: int
    message: str
    data: Union[dict, list, str, int, float, bool, None]

    # Error fields
    error: str
    error_code: str
    error_details: dict

    # Pagination fields
    page: int
    total_pages: int
    total_items: int
    has_next: bool
    has_previous: bool

    # Rate limiting fields
    rate_limit_remaining: int
    rate_limit_reset: str

    # Custom response fields
    custom_fields: dict[str, Union[str, int, float, bool, list, dict]]
