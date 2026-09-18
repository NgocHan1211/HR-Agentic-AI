"""Deterministic payroll calculation and pre-publication anomaly checks."""

from .anomaly_rules import AnomalyThresholds, check_anomaly_rules
from .builtin_functions import prorate, round_down, tax_bracket_vn
from .canonical_fields import (
    ATTENDANCE_FIELDS,
    CANONICAL_INPUT_FIELDS,
    CANONICAL_INPUT_FIELDS_BY_SOURCE,
    CANONICAL_OUTPUT_FIELDS,
    EMPLOYEE_FIELDS,
    FIELD_CODE_ALIASES,
    canonical_field_code,
)
from .engine import PayrollEngine, run_payroll
from .expression_evaluator import ExpressionEvaluationError, evaluate
from .models import AnomalyFlag, LineItem, PayrollResult

__all__ = [
    "AnomalyFlag",
    "AnomalyThresholds",
    "ATTENDANCE_FIELDS",
    "CANONICAL_INPUT_FIELDS",
    "CANONICAL_INPUT_FIELDS_BY_SOURCE",
    "CANONICAL_OUTPUT_FIELDS",
    "ExpressionEvaluationError",
    "EMPLOYEE_FIELDS",
    "FIELD_CODE_ALIASES",
    "LineItem",
    "PayrollEngine",
    "PayrollResult",
    "check_anomaly_rules",
    "canonical_field_code",
    "evaluate",
    "prorate",
    "round_down",
    "run_payroll",
    "tax_bracket_vn",
]


