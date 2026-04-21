"""Decorator for plugin entry points that re-queries TTT for the current entitlement.

Usage:
    from video_grouper.plugins.entitlement_check import (
        requires_entitlement, PluginEntitlementError,
    )

    @requires_entitlement("premium.example.feature")
    def run_premium_operation(ttt_client, ...):
        ...

The decorator expects the wrapped callable's first positional argument (or a
keyword argument named ``ttt_client``) to be the TTT API client. It calls
``ttt_client.check_entitlement(key)`` at most once per ``cache_ttl_seconds``,
caching the result in-process. Raises ``PluginEntitlementError`` when the
server returns False.
"""

import threading
import time
from collections.abc import Callable
from functools import wraps
from typing import Any


class PluginEntitlementError(RuntimeError):
    """Raised when a call decorated with @requires_entitlement is denied."""


class _EntitlementCache:
    """Per-(ttt_client, key) TTL cache keyed by id(ttt_client)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[int, str], tuple[bool, float]] = {}

    def get(self, ttt_client: Any, key: str, ttl: float, now: float) -> bool:
        cache_key = (id(ttt_client), key)
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is not None:
                value, stored_at = entry
                if now - stored_at < ttl:
                    return value
        # Miss / stale — hit the server outside the lock
        value = bool(ttt_client.check_entitlement(key))
        with self._lock:
            self._entries[cache_key] = (value, now)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _EntitlementCache()


def _resolve_client(args: tuple, kwargs: dict) -> Any:
    if "ttt_client" in kwargs:
        return kwargs["ttt_client"]
    if args:
        return args[0]
    return None


def requires_entitlement(
    key: str, *, cache_ttl_seconds: float = 86400
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ttt_client = _resolve_client(args, kwargs)
            if ttt_client is None:
                raise PluginEntitlementError(
                    f"requires_entitlement({key!r}) could not resolve ttt_client"
                )
            now = time.monotonic()
            if not _cache.get(ttt_client, key, cache_ttl_seconds, now):
                raise PluginEntitlementError(
                    f"Current user does not hold entitlement {key!r}"
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def clear_entitlement_cache() -> None:
    """Drop all cached entitlement results. Exposed for tests."""
    _cache.clear()
