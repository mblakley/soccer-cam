"""soccer-cam's integration ports -- the only way it reaches outside services
such as Team Tech Tools.

``build_integrations`` is the one composition point: it resolves each port to
the implementation that serves this install (TTT when enabled, otherwise the
Null implementation) and owns the single shared TTT session. Consumers receive
ports from here; they never construct a TTT client or read ``[TTT]`` config.
Plan: ``.claude/plans/ttt-integration-ports/PLAN.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .entitlements import IEntitlements
from .registry import IntegrationContext, build_port


@dataclass
class Integrations:
    entitlements: IEntitlements
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
        entitlements=build_port(IEntitlements, context), context=context
    )


__all__ = ["IntegrationContext", "Integrations", "build_integrations"]
