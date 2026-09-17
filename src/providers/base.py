"""Provider abstraction.

A provider answers one question: *what is the current public state of this
subject?* It is allowed to fail. The orchestrator catches
:class:`ProviderError`, records it and carries on with the remaining subjects,
so no single outage can take a run down.
"""

from __future__ import annotations

import abc
from typing import List

from ..config import Settings
from ..logging_utils import get_logger
from ..subjects import Subject, WatchValue

log = get_logger(__name__)


class ProviderError(RuntimeError):
    """Recoverable provider failure -- skip and continue."""


class ProviderUnavailable(ProviderError):
    """The provider is not configured (missing key) or is temporarily down."""


class WatchProvider(abc.ABC):
    name: str = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return True

    @abc.abstractmethod
    def supports(self, subject: Subject) -> bool:
        """Cheap local check -- can this provider read that subject at all?"""

    @abc.abstractmethod
    def read(self, subject: Subject) -> List[WatchValue]:
        """Current values.

        Returning ``[]`` means "nothing readable right now", which is not an
        error -- Deadlock has no dedicated-server version, and a news feed may
        carry no official posts. Raise :class:`ProviderError` for real failures.
        """

    def prefetch(self, subjects: List[Subject]) -> None:
        """Optional: fetch for several subjects at once.

        Worth overriding whenever the upstream costs more per *call* than per
        *subject* -- one steamcmd session answering for two apps takes 6.7s
        where two sessions take 10.5s.
        """
        return None

    def close(self) -> None:  # pragma: no cover - default no-op
        return None
