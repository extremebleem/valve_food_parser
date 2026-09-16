"""Provider abstractions.

Two independent axes:

* :class:`DiscoveryProvider` answers "which venues exist near this point".
* :class:`LoadProvider`      answers "how busy is this venue right now".

Both are deliberately allowed to fail. The orchestrator catches
:class:`ProviderError`, records it and continues with the remaining providers,
so no single outage can take the run down.
"""

from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional, Sequence

from ..config import OfficeConfig, Settings
from ..logging_utils import get_logger
from ..models import LoadSignal, Venue

log = get_logger(__name__)


class ProviderError(RuntimeError):
    """Recoverable provider failure -- skip and continue."""


class ProviderUnavailable(ProviderError):
    """The provider is not configured (missing key) or is temporarily down."""


class DiscoveryProvider(abc.ABC):
    name: str = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return True

    @abc.abstractmethod
    def discover(self, office: OfficeConfig) -> List[Venue]:
        """Return every food venue this source knows about within the radius."""

    def close(self) -> None:  # pragma: no cover - default no-op
        return None


class LoadProvider(abc.ABC):
    """Current-load source.

    Implementations override :meth:`get_current_load`; overriding
    :meth:`get_current_load_batch` too is worthwhile whenever the upstream API
    supports batching (fewer requests, lower cost).
    """

    name: str = "base"
    #: providers are tried in this order, lowest first
    priority: int = 100

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return True

    def supports(self, venue: Venue) -> bool:
        """Cheap local check -- avoids burning quota on venues we cannot map."""
        return True

    @abc.abstractmethod
    def get_current_load(self, venue: Venue) -> Optional[LoadSignal]:
        """Return the current signal, or ``None`` when the source simply has no
        data for this venue right now (not an error)."""

    def get_current_load_batch(self, venues: Sequence[Venue]) -> Dict[str, Optional[LoadSignal]]:
        """Default: one request per venue, with per-venue error isolation."""
        results: Dict[str, Optional[LoadSignal]] = {}
        for venue in venues:
            try:
                results[venue.id] = self.get_current_load(venue)
            except ProviderError as exc:
                log.warning(
                    "load provider failed for venue",
                    extra={"provider": self.name, "venue": venue.name, "error": str(exc)},
                )
                results[venue.id] = None
        return results

    def prefetch(self, venues: Sequence[Venue]) -> None:  # pragma: no cover - optional hook
        """Optional warm-up (e.g. resolve provider-side ids in one call)."""
        return None

    def stats(self) -> Dict[str, Any]:  # pragma: no cover - diagnostics only
        return {}

    def close(self) -> None:  # pragma: no cover - default no-op
        return None
