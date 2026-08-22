from __future__ import annotations

import ast
import operator
from typing import Any, Mapping

from .builtin_functions import prorate, round_down, tax_bracket_vn


class ExpressionEvaluationError(ValueError):
    pass


_BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
            ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
            ast.Pow: operator.pow}
_CMP_OPS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
            ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}
_FUNCTIONS = {"abs": abs, "min": min, "max": max, "round": round,
              "prorate": prorate, "round_down": round_down, "tax_bracket_vn": tax_bracket_vn}


def evaluate(expression: str, variables: Mapping[str, Any]) -> Any:
    """Evaluate a small arithmetic DSL; never executes Python code or attributes."""
    if not isinstance(expression, str) or not expression.strip():
        raise ExpressionEvaluationError("expression must be a non-empty string")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionEvaluationError(f"invalid expression: {exc.msg}") from exc
    return _eval(tree.body, variables)


def _eval(node: ast.AST, variables: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise ExpressionEvaluationError(f"missing variable: {node.id}")
        value = variables[node.id]
        if not isinstance(value, (int, float, bool)):
            raise ExpressionEvaluationError(f"variable {node.id} must be numeric or boolean")
        return value
    if isinstance(node, ast.UnaryOp) and type(node.op) in (ast.USub, ast.UAdd, ast.Not):
        value = _eval(node.operand, variables)
        return -value if isinstance(node.op, ast.USub) else (+value if isinstance(node.op, ast.UAdd) else not value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval(node.left, variables), _eval(node.right, variables))
    if isinstance(node, ast.Compare):
        left = _eval(node.left, variables)
        for operation, comparator in zip(node.ops, node.comparators):
            if type(operation) not in _CMP_OPS or not _CMP_OPS[type(operation)](left, _eval(comparator, variables)):
                return False
            left = _eval(comparator, variables)
        return True
    if isinstance(node, ast.BoolOp) and type(node.op) in (ast.And, ast.Or):
        values = [_eval(value, variables) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.IfExp):
        return _eval(node.body if _eval(node.test, variables) else node.orelse, variables)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS:
        if node.keywords:
            raise ExpressionEvaluationError("keyword arguments are not allowed")
        return _FUNCTIONS[node.func.id](*[_eval(arg, variables) for arg in node.args])
    raise ExpressionEvaluationError(f"unsupported syntax: {type(node).__name__}")
