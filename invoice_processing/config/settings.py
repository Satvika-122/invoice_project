from __future__ import annotations

import json
from pathlib import Path
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class InputSettings(BaseModel):
    """Configurable controls for Stage 1 input loading."""

    model_config = ConfigDict(extra="forbid")

    max_input_size_bytes: int = Field(gt=0)


def load_input_settings(path: Path) -> InputSettings:
    """Load Stage 1 input settings from JSON config."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return InputSettings.model_validate(raw)


class ValidationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_fields: dict[str, list[str]]
    valid_invoice_types: list[str]


class ToleranceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rounding_tolerance_amount: Decimal
    header_amount_tolerance_percent: Decimal
    header_amount_tolerance_amount: Decimal
    line_unit_price_tolerance_percent: Decimal
    line_quantity_tolerance: Decimal
    line_total_tolerance_amount: Decimal
    tax_tolerance_amount: Decimal
    freight_max_without_review: Decimal
    discount_max_percent: Decimal
    duplicate_date_window_days: int = Field(ge=0)
    fx_rate_max_age_days: int = Field(ge=0)


class VendorPolicySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_inactive_vendor_manual_review: bool
    allow_vendor_name_fallback: bool
    require_vendor_tax_profile: bool
    allowed_vendor_statuses_for_payment: list[str]
    manual_review_vendor_statuses: list[str]
    supported_countries: list[str]
    bank_change_review_days: int = Field(ge=0)


class RiskSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocking_failure: int
    high: int
    medium: int
    low: int
    manual_review_threshold: int
    reject_threshold: int
    rules: dict[str, int]


class ApprovalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_approve_max_amount: Decimal
    manual_review_risk_threshold: int
    reject_risk_threshold: int
    departments: dict[str, list[dict[str, str]]]


class SystemSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: InputSettings
    validation: ValidationSettings
    tolerances: ToleranceSettings
    vendor_policy: VendorPolicySettings
    risk: RiskSettings
    approval: ApprovalSettings


def _load_model(path: Path, model: type[BaseModel]) -> BaseModel:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return model.model_validate(raw)


def load_system_settings(config_dir: Path = Path("config")) -> SystemSettings:
    """Load all AP processing configuration files."""
    return SystemSettings(
        input=load_input_settings(config_dir / "input.json"),
        validation=_load_model(config_dir / "validation.json", ValidationSettings),  # type: ignore[arg-type]
        tolerances=_load_model(config_dir / "tolerances.json", ToleranceSettings),  # type: ignore[arg-type]
        vendor_policy=_load_model(config_dir / "vendor_policy.json", VendorPolicySettings),  # type: ignore[arg-type]
        risk=_load_model(config_dir / "risk_weights.json", RiskSettings),  # type: ignore[arg-type]
        approval=_load_model(config_dir / "approval_thresholds.json", ApprovalSettings),  # type: ignore[arg-type]
    )
