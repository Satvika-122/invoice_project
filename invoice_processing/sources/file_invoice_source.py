from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from invoice_processing.sources.interfaces import (
    InvoiceSource,
    InvoiceSourceNotFoundError,
    InvoiceSourcePayload,
    InvoiceSourceReadError,
)


class FileInvoiceSource(InvoiceSource):
    """Invoice source backed by a local JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> InvoiceSourcePayload:
        """Read invoice bytes from local storage."""
        if not self._path.exists():
            raise InvoiceSourceNotFoundError(f"Invoice source not found: {self._path}")
        if not self._path.is_file():
            raise InvoiceSourceReadError(f"Invoice source is not a file: {self._path}")

        try:
            content = self._path.read_bytes()
            stat = self._path.stat()
        except OSError as exc:
            raise InvoiceSourceReadError(f"Could not read invoice source: {self._path}") from exc

        return InvoiceSourcePayload(
            content=content,
            source=str(self._path),
            source_type="file",
            size_bytes=stat.st_size,
            last_modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
        )
