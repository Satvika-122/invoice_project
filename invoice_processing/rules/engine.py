from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from invoice_processing.domain.enums import RuleStatus, Severity
from invoice_processing.domain.models import RuleResult


RuleCallable = Callable[[], RuleResult]


@dataclass(frozen=True)
class RuleDefinition:
    """Independent, reusable rule definition."""

    rule_id: str
    name: str
    severity: Severity
    evaluator: RuleCallable

    def execute(self) -> RuleResult:
        """Execute the rule and return a structured result."""
        return self.evaluator()


class RuleExecutor:
    """Executes deterministic AP rules without side effects."""

    def execute(self, rules: list[RuleDefinition]) -> list[RuleResult]:
        return [rule.execute() for rule in rules]


def pass_result(
    rule_id: str,
    name: str,
    expected: str,
    actual: Any,
    reason: str,
    details: dict[str, Any] | None = None,
) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        rule_name=name,
        status=RuleStatus.PASS,
        severity=Severity.INFO,
        expected=expected,
        actual=str(actual),
        reason=reason,
        details=details or {},
    )


def fail_result(
    rule_id: str,
    name: str,
    expected: str,
    actual: Any,
    reason: str,
    severity: Severity = Severity.ERROR,
    details: dict[str, Any] | None = None,
) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        rule_name=name,
        status=RuleStatus.FAIL,
        severity=severity,
        expected=expected,
        actual=str(actual),
        reason=reason,
        details=details or {},
    )


def review_result(
    rule_id: str,
    name: str,
    expected: str,
    actual: Any,
    reason: str,
    severity: Severity = Severity.WARNING,
    details: dict[str, Any] | None = None,
) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        rule_name=name,
        status=RuleStatus.MANUAL_REVIEW,
        severity=severity,
        expected=expected,
        actual=str(actual),
        reason=reason,
        details=details or {},
    )


def result_from_bool(
    rule_id: str,
    name: str,
    expected: str,
    actual: Any,
    passed: bool,
    pass_reason: str,
    fail_reason: str,
    severity: Severity = Severity.ERROR,
    details: dict[str, Any] | None = None,
) -> RuleResult:
    if passed:
        return pass_result(rule_id, name, expected, actual, pass_reason, details)
    return fail_result(rule_id, name, expected, actual, fail_reason, severity, details)
