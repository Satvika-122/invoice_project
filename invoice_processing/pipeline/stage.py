from __future__ import annotations

from abc import ABC, abstractmethod

from invoice_processing.domain.models import ProcessingContext


class PipelineStage(ABC):
    name: str

    @abstractmethod
    def run(self, context: ProcessingContext) -> ProcessingContext:
        raise NotImplementedError
