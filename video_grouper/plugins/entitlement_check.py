"""Decorator for plugin entry points that re-checks the current entitlement.

Usage:
    from video_grouper.plugins.entitlement_check import (
        requires_entitlement, PluginEntitlementError,
    )

    @requires_entitlement("premium.example.feature")
    def run_premium_operation(entitlements, ...):
        ...

The decorator expects the wrapped callable's first positional argument (or a
keyword argument named ``entitlements``) to be the ``IEntitlements`` port
(``video_grouper.integrations.entitlements``). It calls
``entitlements.has(key)`` at most once per ``cache_ttl_seconds``, caching the
result in-process. Raises ``PluginEntitlementError`` when it returns False.
"""

import threading
import time
from collections.abc import Callable
from functools import wraps
from typing import Any


class PluginEntitlementError(RuntimeError):
    """Raised when a call decorated with @requires_entitlement is denied."""


class _EntitlementCache:
    """Per-(entitlements, key) TTL cache keyed by id(entitlements)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[int, str], tuple[bool, float]] = {}

    def get(self, entitlements: Any, key: str, ttl: float, now: float) -> bool:
        cache_key = (id(entitlements), key)
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is not None:
                value, stored_at = entry
                if now - stored_at < ttl:
                    return value
        # Miss / stale — hit the server outside the lock
        value = bool(entitlements.has(key))
        with self._lock:
            self._entries[cache_key] = (value, now)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _EntitlementCache()


def _resolve_client(args: tuple, kwargs: dict) -> Any:
    if "entitlements" in kwargs:
        return kwargs["entitlements"]
    if args:
        return args[0]
    return None


def requires_entitlement(
    key: str, *, cache_ttl_seconds: float = 86400
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            entitlements = _resolve_client(args, kwargs)
            if entitlements is None:
                raise PluginEntitlementError(
                    f"requires_entitlement({key!r}) could not resolve the entitlements port"
                )
            now = time.monotonic()
            if not _cache.get(entitlements, key, cache_ttl_seconds, now):
                raise PluginEntitlementError(
                    f"Current user does not hold entitlement {key!r}"
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def clear_entitlement_cache() -> None:
    """Drop all cached entitlement results. Exposed for tests."""
    _cache.clear()
