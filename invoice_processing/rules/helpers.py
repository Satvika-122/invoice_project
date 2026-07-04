from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from invoice_processing.domain.enums import RuleStatus, Severity
from invoice_processing.domain.models import Finding, RuleResult


def has_blocking_failure(results: Iterable[RuleResult]) -> bool:
    return any(
        result.status == RuleStatus.FAIL
        and result.severity in {Severity.ERROR, Severity.BLOCKING}
        for result in results
    )


def has_manual_review(results: Iterable[RuleResult]) -> bool:
    return any(result.status == RuleStatus.MANUAL_REVIEW for result in results)


def within_amount_tolerance(actual: Decimal, expected: Decimal, tolerance: Decimal) -> bool:
    return abs(actual - expected) <= tolerance


def results_to_findings(results: Iterable[RuleResult]) -> list[Finding]:
    findings: list[Finding] = []
    for result in results:
        if result.status == RuleStatus.PASS:
            continue
        findings.append(
            Finding(
                code=result.rule_id,
                message=result.reason,
                severity=result.severity,
                details={
                    "rule_name": result.rule_name,
                    "expected": result.expected,
                    "actual": result.actual,
                    **result.details,
                },
            )
        )
    return findings


def fingerprint(values: list[str]) -> str:
    return "|".join(sorted(value.strip().lower() for value in values if value))
