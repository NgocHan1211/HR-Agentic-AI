"""Safe, deterministic evaluator for payroll formula expressions.

Only arithmetic, comparisons, boolean operators, and explicitly registered
functions are supported.  It deliberately never calls Python ``eval``.
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable, Mapping
from numbers import Real
from typing import Any


class ExpressionError(ValueError):
    """Raised when an expression is unsafe, invalid, or cannot be evaluated."""


def prorate(amount: float, actual_days: float, standard_days: float) -> float:
    if standard_days <= 0:
        raise ExpressionError("standard_days must be greater than 0")
    return amount * actual_days / standard_days


def round_down(amount: float, unit: float) -> float:
    if unit <= 0:
        raise ExpressionError("round_down unit must be greater than 0")
    return math.floor(amount / unit) * unit


def tax_bracket_vn(taxable_income: float) -> float:
    """Progressive PIT calculator; keep bracket data configurable in production."""
    brackets = ((5_000_000, .05), (10_000_000, .10), (18_000_000, .15),
                (32_000_000, .20), (52_000_000, .25), (80_000_000, .30))
    remaining, lower, tax = max(0.0, taxable_income), 0.0, 0.0
    for ceiling, rate in brackets:
        portion = min(remaining, ceiling - lower)
        tax += max(0.0, portion) * rate
        remaining -= max(0.0, portion)
        lower = ceiling
        if remaining <= 0:
            return tax
    return tax + remaining * .35


BUILTIN_FUNCTIONS: dict[str, Callable[..., float]] = {
    "prorate": prorate,
    "round_down": round_down,
    "tax_bracket_vn": tax_bracket_vn,
}

_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
_COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
            ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def evaluate(expression: str, variables: Mapping[str, Any], *,
             functions: Mapping[str, Callable[..., float]] | None = None) -> float | bool:
    if not expression or not expression.strip():
        raise ExpressionError("expression must not be empty")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"invalid expression syntax: {expression!r}") from exc
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise ExpressionError("expression is too complex")
    evaluator = _Evaluator(variables, {**BUILTIN_FUNCTIONS, **(functions or {})})
    return evaluator.visit(tree.body)


class _Evaluator(ast.NodeVisitor):
    def __init__(self, variables: Mapping[str, Any], functions: Mapping[str, Callable[..., float]]) -> None:
        self.variables, self.functions = variables, functions

    def generic_visit(self, node: ast.AST) -> Any:
        raise ExpressionError(f"unsupported expression feature: {type(node).__name__}")

    def visit_Constant(self, node: ast.Constant) -> float | bool:
        if isinstance(node.value, bool):
            return node.value
        if isinstance(node.value, Real):
            return float(node.value)
        raise ExpressionError("only numeric and boolean literals are allowed")

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id not in self.variables:
            raise ExpressionError(f"unknown variable: {node.id}")
        value = self.variables[node.id]
        if isinstance(value, bool) or isinstance(value, Real):
            return value
        raise ExpressionError(f"variable {node.id!r} must be numeric or boolean")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        value = self.visit(node.operand)
        if isinstance(node.op, ast.USub): return -value
        if isinstance(node.op, ast.UAdd): return +value
        if isinstance(node.op, ast.Not): return not value
        raise ExpressionError("unsupported unary operator")

    def visit_BinOp(self, node: ast.BinOp) -> float:
        operation = _BINARY.get(type(node.op))
        if operation is None:
            raise ExpressionError("unsupported binary operator")
        try:
            return float(operation(self.visit(node.left), self.visit(node.right)))
        except (ArithmeticError, TypeError) as exc:
            raise ExpressionError(f"could not evaluate arithmetic operation: {exc}") from exc

    def visit_BoolOp(self, node: ast.BoolOp) -> bool:
        values = [bool(self.visit(value)) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values) if isinstance(node.op, ast.Or) else self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            compare = _COMPARE.get(type(op))
            if compare is None or not compare(left, self.visit(comparator)):
                return False
            left = self.visit(comparator)
        return True

    def visit_IfExp(self, node: ast.IfExp) -> Any:
        return self.visit(node.body if self.visit(node.test) else node.orelse)

    def visit_Call(self, node: ast.Call) -> float:
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ExpressionError("only positional calls to approved functions are allowed")
        function = self.functions.get(node.func.id)
        if function is None:
            raise ExpressionError(f"function is not allowed: {node.func.id}")
        try:
            return float(function(*(self.visit(arg) for arg in node.args)))
        except (ArithmeticError, TypeError) as exc:
            raise ExpressionError(f"function {node.func.id} failed: {exc}") from exc
