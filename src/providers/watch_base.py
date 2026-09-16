"""Abstraction for "read the current public state of a subject"."""

from __future__ import annotations

import abc
from typing import List

from ..config import Settings
from ..logging_utils import get_logger
from ..subjects import Subject, WatchValue

log = get_logger(__name__)


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
        """Current values. Returning ``[]`` means "nothing readable right now",
        which is not an error. Raise :class:`ProviderError` for real failures."""

    def close(self) -> None:  # pragma: no cover - default no-op
        return None
