"""Generic port registry: how every integration port finds its implementation.

A *port* is an abstract interface for one job soccer-cam needs done by an
outside service (check entitlements, report recordings, take remote work, ...).
Implementations live one module each in ``video_grouper/integrations/<port>/
sources/`` and register themselves with ``@provides(<Port>)``. Building a port
asks each registered implementation, lowest ``priority`` first, whether it can
serve this install (``from_context``); the first that says yes wins. Every port
has a Null implementation at the bottom that always says yes, so a port always
resolves -- "TTT off" is an implementation with tested behaviour, not a
scattered ``if client:``.

Adding a provider is one new module. Consumers never name one.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


class Provider:
    """Mixin every port implementation carries."""

    name: ClassVar[str] = ""
    # Lower wins. Real providers use small numbers; Null uses NULL_PRIORITY.
    priority: ClassVar[int] = 50

    @classmethod
    def from_context(cls, context: IntegrationContext) -> Any:
        """An instance that can serve this install, or None to pass."""
        raise NotImplementedError


NULL_PRIORITY = 1000


_REGISTRY: dict[type, dict[str, type[Provider]]] = {}
_LOADED: set[str] = set()


def provides(port: type):
    """Class decorator: register an implementation of ``port``."""

    def decorator(cls):
        if not getattr(cls, "name", ""):
            raise ValueError(f"{cls.__name__} must set a non-empty `name`")
        if not issubclass(cls, port):
            raise TypeError(f"{cls.__name__} does not implement {port.__name__}")
        _REGISTRY.setdefault(port, {})[cls.name] = cls
        return cls

    return decorator


def _load_sources(port: type) -> None:
    """Import every module in ``<port package>/sources/`` once."""
    package = port.__module__.rsplit(".", 1)[0] + ".sources"
    if package in _LOADED:
        return
    _LOADED.add(package)
    try:
        pkg = importlib.import_module(package)
    except ModuleNotFoundError:
        return
    for info in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{package}.{info.name}")


def implementations(port: type) -> dict[str, type[Provider]]:
    """Registered implementations of ``port``, by name."""
    _load_sources(port)
    return dict(_REGISTRY.get(port, {}))


def build_port(port: Any, context: IntegrationContext) -> Any:
    """The implementation of ``port`` that serves this install. (``port`` is
    an abstract class, which mypy will not accept as ``type[P]``.)"""
    for cls in sorted(implementations(port).values(), key=lambda c: c.priority):
        instance = cls.from_context(context)
        if instance is not None:
            logger.info("%s -> %s", port.__name__, cls.name)
            return instance
    raise LookupError(f"no implementation of {port.__name__} accepted this install")


@dataclass
class IntegrationContext:
    """What an implementation may look at to decide whether it applies."""

    config: Any
    storage_path: str
    services: dict[str, Any] = field(default_factory=dict)

    def ttt_enabled(self) -> bool:
        ttt = getattr(self.config, "ttt", None)
        return bool(ttt is not None and getattr(ttt, "enabled", False))

    def ttt_client(self) -> Any:
        """The one shared TTT client for this install (built on first use).

        This is the only place soccer-cam constructs a TTT client for a port;
        every TTT implementation shares it, so a sign-in reaches all of them.
        """
        if "ttt" not in self.services:
            from video_grouper.api_integrations.ttt_api import TTTApiClient

            ttt = self.config.ttt
            self.services["ttt"] = TTTApiClient(
                supabase_url=ttt.supabase_url,
                anon_key=ttt.anon_key,
                api_base_url=ttt.api_base_url,
                storage_path=str(self.storage_path),
            )
        return self.services["ttt"]
