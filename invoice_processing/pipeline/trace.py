from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from time import perf_counter
from typing import Any

from pydantic import BaseModel

from invoice_processing.domain.models import (
    ProcessingContext,
    RuleResult,
    RuleEvaluation,
    StageExecutionTrace,
)


def to_debug_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: to_debug_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_debug_value(item) for item in value]
    if isinstance(value, tuple):
        return [to_debug_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


class StageTraceRecorder:
    def __init__(self, context: ProcessingContext, stage: Any, purpose: str) -> None:
        self._context = context
        self._stage_name = stage.name
        self._stage_number = int(getattr(stage, "stage_number", 0))
        self._purpose = purpose
        self._started_at = perf_counter()
        self._input_received: dict[str, Any] = {}
        self._repositories_accessed: list[str] = []
        self._rules: list[RuleEvaluation] = []
        self._comparisons: list[dict[str, Any]] = []
        self._calculations: list[dict[str, Any]] = []
        self._output_produced: dict[str, Any] = {}

    def set_input(self, **values: Any) -> None:
        self._input_received.update(to_debug_value(values))

    def add_repository(self, repository: str) -> None:
        self._repositories_accessed.append(repository)

    def add_rule(
        self,
        rule: str,
        expected: str,
        actual: Any,
        passed: bool,
        rule_id: str | None = None,
        **details: Any,
    ) -> None:
        self._rules.append(
            RuleEvaluation(
                rule_id=rule_id,
                rule=rule,
                expected=expected,
                actual=str(to_debug_value(actual)),
                passed=passed,
                details=to_debug_value(details),
            )
        )

    def add_rule_result(self, result: RuleResult) -> None:
        self.add_rule(
            result.rule_name,
            result.expected,
            result.actual,
            result.passed,
            rule_id=result.rule_id,
            status=result.status.value,
            severity=result.severity.value,
            reason=result.reason,
            **result.details,
        )

    def add_rule_results(self, results: list[RuleResult]) -> None:
        for result in results:
            self.add_rule_result(result)

    def add_comparison(self, name: str, **values: Any) -> None:
        self._comparisons.append({"name": name, **to_debug_value(values)})

    def add_calculation(self, name: str, **values: Any) -> None:
        self._calculations.append({"name": name, **to_debug_value(values)})

    def set_output(self, **values: Any) -> None:
        self._output_produced.update(to_debug_value(values))

    def finish(self, result: str, conclusion_reason: str) -> None:
        elapsed_ms = round((perf_counter() - self._started_at) * 1000, 3)
        passed = sum(1 for rule in self._rules if rule.passed)
        failed = len(self._rules) - passed
        trace = StageExecutionTrace(
            stage_number=self._stage_number,
            stage_name=self._stage_name,
            purpose=self._purpose,
            input_received=self._input_received,
            repositories_accessed=self._repositories_accessed,
            validation_rules_executed=self._rules,
            comparisons_performed=self._comparisons,
            intermediate_calculations=self._calculations,
            output_produced=self._output_produced,
            result=result,
            conclusion_reason=conclusion_reason,
            processing_time_ms=elapsed_ms,
            validations_executed=len(self._rules),
            validations_passed=passed,
            validations_failed=failed,
        )
        self._context.execution_trace.append(trace)
        print(format_stage_trace(trace))


def format_stage_trace(trace: StageExecutionTrace) -> str:
    title = f"STAGE {trace.stage_number} : {trace.stage_name.replace('_', ' ').upper()}"
    lines = ["=" * 50, title, "=" * 50, ""]
    lines.extend(_section("Purpose", [trace.purpose]))
    lines.extend(_section("Input", _format_mapping(trace.input_received)))
    repositories = trace.repositories_accessed or ["No repository or external data source accessed."]
    lines.extend(_section("Repository / Data Source", repositories))

    if trace.validation_rules_executed:
        rule_lines: list[str] = []
        for rule in trace.validation_rules_executed:
            marker = "PASS" if rule.passed else "FAIL"
            label = f"{rule.rule_id} - {rule.rule}" if rule.rule_id else rule.rule
            rule_lines.extend(
                [
                    f"Rule: {label}",
                    f"Expected: {rule.expected}",
                    f"Actual: {rule.actual}",
                    f"Result: {marker}",
                ]
            )
            for key, value in rule.details.items():
                rule_lines.append(f"{key}: {value}")
            rule_lines.append("")
    else:
        rule_lines = ["No validation rules executed in this stage."]
    lines.extend(_section("Rules Executed", rule_lines))

    comparisons = _format_list_of_mappings(trace.comparisons_performed) or [
        "No direct comparisons performed."
    ]
    lines.extend(_section("Comparisons Performed", comparisons))

    calculations = _format_list_of_mappings(trace.intermediate_calculations) or [
        "No intermediate calculations performed."
    ]
    lines.extend(_section("Intermediate Calculations", calculations))

    lines.extend(
        _section(
            "Result",
            [
                trace.result,
                f"Processing Time: {trace.processing_time_ms} ms",
                f"Validations Executed: {trace.validations_executed}",
                f"Passed: {trace.validations_passed}",
                f"Failed: {trace.validations_failed}",
            ],
        )
    )
    lines.extend(_section("Why", [trace.conclusion_reason]))
    lines.extend(_section("Output Stored", _format_mapping(trace.output_produced)))
    return "\n".join(lines)


def _section(title: str, body: list[str]) -> list[str]:
    return [title, "-" * len(title), *body, ""]


def _format_mapping(values: dict[str, Any]) -> list[str]:
    if not values:
        return ["No input or output values recorded."]
    return [f"{key}: {value}" for key, value in values.items()]


def _format_list_of_mappings(values: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in values:
        lines.extend(_format_mapping(item))
        lines.append("")
    return lines
