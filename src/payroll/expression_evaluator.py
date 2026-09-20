"""Safe, deterministic evaluator for the payroll expression DSL.

The module deliberately interprets a small AST whitelist; it never delegates
formula execution to Python ``eval``.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable, Mapping
from numbers import Real
from typing import Any

from .builtin_functions import prorate, round_down, tax_bracket_vn


class ExpressionEvaluationError(ValueError):
    """Raised when an expression is unsafe, invalid, or cannot be evaluated."""


ExpressionError = ExpressionEvaluationError

BUILTIN_FUNCTIONS: dict[str, Callable[..., float]] = {
    "abs": abs, "min": min, "max": max, "round": round,
    "prorate": prorate, "round_down": round_down, "tax_bracket_vn": tax_bracket_vn,
}
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
_COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
            ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def evaluate(expression: str, variables: Mapping[str, Any], *,
             functions: Mapping[str, Callable[..., float]] | None = None) -> float | bool:
    """Evaluate only numeric/boolean variables and whitelisted function calls."""
    if not isinstance(expression, str) or not expression.strip():
        raise ExpressionEvaluationError("expression must be a non-empty string")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionEvaluationError(f"invalid expression: {exc.msg}") from exc
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise ExpressionEvaluationError("expression is too complex")
    return _Evaluator(variables, {**BUILTIN_FUNCTIONS, **(functions or {})}).visit(tree.body)


class _Evaluator(ast.NodeVisitor):
    def __init__(self, variables: Mapping[str, Any], functions: Mapping[str, Callable[..., float]]) -> None:
        self.variables, self.functions = variables, functions

    def generic_visit(self, node: ast.AST) -> Any:
        raise ExpressionEvaluationError(f"unsupported syntax: {type(node).__name__}")

    def visit_Constant(self, node: ast.Constant) -> float | bool:
        if isinstance(node.value, bool): return node.value
        if isinstance(node.value, Real): return float(node.value)
        raise ExpressionEvaluationError("only numeric and boolean literals are allowed")

    def visit_Name(self, node: ast.Name) -> float | bool:
        if node.id not in self.variables:
            raise ExpressionEvaluationError(f"missing variable: {node.id}")
        value = self.variables[node.id]
        if isinstance(value, bool) or isinstance(value, Real): return value
        raise ExpressionEvaluationError(f"variable {node.id!r} must be numeric or boolean")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> float | bool:
        value = self.visit(node.operand)
        if isinstance(node.op, ast.USub): return -value
        if isinstance(node.op, ast.UAdd): return +value
        if isinstance(node.op, ast.Not): return not value
        return self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> float:
        operation = _BINARY.get(type(node.op))
        if operation is None: return self.generic_visit(node)
        try:
            return float(operation(self.visit(node.left), self.visit(node.right)))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ExpressionEvaluationError(f"could not evaluate arithmetic operation: {exc}") from exc

    def visit_BoolOp(self, node: ast.BoolOp) -> bool:
        if isinstance(node.op, ast.And): return all(bool(self.visit(value)) for value in node.values)
        if isinstance(node.op, ast.Or): return any(bool(self.visit(value)) for value in node.values)
        return self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            operation, right = _COMPARE.get(type(op)), self.visit(comparator)
            if operation is None or not operation(left, right): return False
            left = right
        return True

    def visit_IfExp(self, node: ast.IfExp) -> float | bool:
        return self.visit(node.body if self.visit(node.test) else node.orelse)

    def visit_Call(self, node: ast.Call) -> float:
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ExpressionEvaluationError("only positional calls to approved functions are allowed")
        function = self.functions.get(node.func.id)
        if function is None: raise ExpressionEvaluationError(f"function is not allowed: {node.func.id}")
        try:
            return float(function(*(self.visit(arg) for arg in node.args)))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ExpressionEvaluationError(f"function {node.func.id} failed: {exc}") from exc
