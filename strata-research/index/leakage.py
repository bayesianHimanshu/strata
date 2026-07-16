from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol, TypeVar

from core.config import LEAKAGE_BUFFER


class Dated(Protocol):
    """Anything carrying a normalized document date (SourceRecord, Chunk, ...)."""

    doc_date: date | None


T = TypeVar("T", bound=Dated)


class LeakageError(Exception):
    """Raised when a document that fails the temporal bound reaches a guarded path."""


@dataclass(frozen=True)
class LeakageFilter:
    """The admissibility test for one decision's retrieval corpus."""

    decision_date: date
    buffer: timedelta = LEAKAGE_BUFFER

    @property
    def cutoff(self) -> date:
        """Documents must be strictly older than this date to be admitted."""
        return self.decision_date - self.buffer

    def admits(self, doc_date: date | None) -> bool:
        """True iff a document with this date may enter the retrieval corpus."""
        return doc_date is not None and doc_date < self.cutoff

    def assert_admits(self, item: Dated) -> None:
        """Defense in depth: raise if a guarded item violates the bound."""
        if not self.admits(item.doc_date):
            raise LeakageError(
                f"leakage: doc_date={item.doc_date!r} not strictly before "
                f"cutoff={self.cutoff.isoformat()} "
                f"(decision {self.decision_date.isoformat()} - buffer {self.buffer})"
            )

    def filter(self, items: Iterable[T]) -> list[T]:
        """Drop every item that is not admissible. The corpus-construction gate."""
        return [it for it in items if self.admits(it.doc_date)]
