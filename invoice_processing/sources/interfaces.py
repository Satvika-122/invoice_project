from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class InvoiceSourcePayload:
    """Raw invoice bytes and source metadata returned by an invoice source."""

    content: bytes
    source: str
    source_type: str
    size_bytes: int
    last_modified_at: datetime | None = None
    content_type: str = "application/json"


class InvoiceSourceError(RuntimeError):
    """Base exception for invoice source failures."""


class InvoiceSourceNotFoundError(InvoiceSourceError):
    """Raised when the configured invoice source cannot be found."""


class InvoiceSourceReadError(InvoiceSourceError):
    """Raised when an invoice source exists but cannot be read."""


class InvoiceSource(ABC):
    """Storage-agnostic invoice source contract for Stage 1."""

    @abstractmethod
    def load(self) -> InvoiceSourcePayload:
        """Return invoice bytes and source metadata.

        Raises:
            InvoiceSourceNotFoundError: The configured source does not exist.
            InvoiceSourceReadError: The configured source cannot be read.
        """
        raise NotImplementedError
