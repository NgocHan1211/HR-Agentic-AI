"""Deterministic payroll calculation and pre-publication anomaly checks."""

from .anomaly_rules import AnomalyThresholds, check_anomaly_rules
from .builtin_functions import prorate, round_down, tax_bracket_vn
from .engine import PayrollEngine, run_payroll
from .expression_evaluator import ExpressionEvaluationError, evaluate
from .models import AnomalyFlag, LineItem, PayrollResult

__all__ = [
    "AnomalyFlag",
    "AnomalyThresholds",
    "ExpressionEvaluationError",
    "LineItem",
    "PayrollEngine",
    "PayrollResult",
    "check_anomaly_rules",
    "evaluate",
    "prorate",
    "round_down",
    "run_payroll",
    "tax_bracket_vn",
]


