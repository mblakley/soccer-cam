"""soccer-cam's integration ports -- the only way it reaches outside services
such as Team Tech Tools.

``build_integrations`` is the one composition point: it resolves each port to
the implementation that serves this install (TTT when enabled, otherwise the
Null implementation) and owns the single shared TTT session. Consumers receive
ports from here; they never construct a TTT client or read ``[TTT]`` config.

Prefer having the ports handed in (constructor / ``StepContext``). Code that
cannot be -- a task restored from a queue file, a helper given only ``config``
-- uses :func:`active`, which the app sets once at startup and which defaults
to the Null implementations.

Plan: ``.claude/plans/ttt-integration-ports/PLAN.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from .entitlements import IEntitlements
from .recordings import IRecordingSink
from .registry import IntegrationContext, build_port
from .work import IWorkSource


@dataclass
class Integrations:
    entitlements: IEntitlements
    recordings: IRecordingSink
    work: IWorkSource
    context: IntegrationContext

    def shared_ttt_client(self) -> Any:
        """TRANSITIONAL: the shared TTT client, for processors not yet moved
        onto a port. Goes away when the last one moves (plan phase 6)."""
        return self.context.ttt_client()


def build_integrations(config: Any, storage_path: str, **services: Any) -> Integrations:
    """Resolve every port for this install.

    ``services`` lets the caller hand in an already-built shared object (for
    example ``ttt=<client>``) so one session backs every port.
    """
    context = IntegrationContext(config, str(storage_path), dict(services))
    return Integrations(
        entitlements=build_port(IEntitlements, context),
        recordings=build_port(IRecordingSink, context),
        work=build_port(IWorkSource, context),
        context=context,
    )


_active: Integrations | None = None


def activate(integrations: Integrations) -> Integrations:
    """Make ``integrations`` the process-wide set returned by :func:`active`."""
    global _active
    _active = integrations
    return integrations


def active() -> Integrations:
    """The app's integrations; the Null set until the app activates its own."""
    global _active
    if _active is None:
        _active = build_integrations(
            SimpleNamespace(ttt=SimpleNamespace(enabled=False)), "."
        )
    return _active


__all__ = [
    "IntegrationContext",
    "Integrations",
    "activate",
    "active",
    "build_integrations",
]
