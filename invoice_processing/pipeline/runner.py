from __future__ import annotations

import logging
from collections.abc import Iterable

from invoice_processing.domain.models import ProcessingContext
from invoice_processing.pipeline.stage import PipelineStage

logger = logging.getLogger(__name__)


class PipelineRunner:
    def __init__(self, stages: Iterable[PipelineStage]) -> None:
        self._stages = list(stages)
        for index, stage in enumerate(self._stages, start=1):
            setattr(stage, "stage_number", index)

    def run(self, context: ProcessingContext | None = None) -> ProcessingContext:
        current_context = context or ProcessingContext()
        for stage in self._stages:
            logger.info("pipeline_stage_started", extra={"stage": stage.name})
            current_context = stage.run(current_context)
            logger.info("pipeline_stage_completed", extra={"stage": stage.name})
        return current_context
