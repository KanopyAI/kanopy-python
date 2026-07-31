"""Small transport-independent SDK value objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    """One page returned by a Kanopy collection endpoint."""

    items: list[T]
    next_cursor: str | None = None
    total_count: int | None = None

    @property
    def has_next(self) -> bool:
        return bool(self.next_cursor)
