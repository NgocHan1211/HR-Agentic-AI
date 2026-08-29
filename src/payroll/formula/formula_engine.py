"""
Compiles an already-reviewed FormulaSpec (proposed by the LLM in formula_extractor.py)
into an actual, callable Python function.

Design intent (unchanged from formula_extractor.py):
    The LLM NEVER calculates payroll and NEVER produces executable code.
    It only proposes structured data: variables + rules using a small whitelist
    of operations (arithmetic, prorate, round_down, tax_bracket_vn).

This module is the piece that turns that reviewed data into something you can
actually call like a normal function:

    fn = compile_formula(spec)
    outputs = fn({"base_salary": 15_000_000, "worked_days": 20, "total_days": 22})

Safety: expressions are parsed with `ast` and walked by hand. Only a small,
explicit set of node types and function names are allowed to execute. There is
no `eval()`/`exec()` on raw strings, so a malformed or malicious `expression`
in the spec can raise UnsafeExpressionError but cannot run arbitrary code.
"""
from __future__ import annotations

import ast
import operator
from typing import Any, Callable

# formula_schema.FormulaSpec is expected to look like the object built in
# formula_extractor.py: spec.variables (name, source, value) and
# spec.rules (output_field, expression, condition, rounding, section, description).
try:
    from .formula_schema import FormulaSpec  # type: ignore
except ImportError:  # pragma: no cover - allows standalone testing/import
    FormulaSpec = Any  # type: ignore


class UnsafeExpressionError(ValueError):
    """Raised when a rule's expression uses anything outside the whitelist."""


# ---------------------------------------------------------------------------
# Whitelisted domain functions (same names the extraction prompt allows)
# ---------------------------------------------------------------------------

def prorate(base: float, worked_units: float, total_units: float) -> float:
    """Pro-rate `base` by worked_units / total_units. Returns 0 if total_units is 0."""
    if not total_units:
        return 0.0
    return base * worked_units / total_units


def round_down(value: float, unit: float = 1) -> float:
    """Round `value` down to the nearest multiple of `unit` (unit must be > 0)."""
    if unit <= 0:
        raise ValueError("round_down: unit must be positive")
    return (value // unit) * unit


def tax_bracket_vn(income: float, brackets: list[tuple[float | None, float]]) -> float:
    """
    Progressive tax, Vietnam-style bracket table.
    brackets: list of (upper_bound, rate) sorted ascending; last upper_bound may be None
              (open-ended top bracket). Example:
              [(5_000_000, 0.05), (10_000_000, 0.10), (None, 0.15)]
    """
    tax = 0.0
    lower = 0.0
    for upper, rate in brackets:
        if upper is None or income <= upper:
            tax += max(income - lower, 0.0) * rate
            return round(tax)
        tax += max(upper - lower, 0.0) * rate
        lower = upper
    return round(tax)


_ALLOWED_FUNCS: dict[str, Callable[..., Any]] = {
    "prorate": prorate,
    "round_down": round_down,
    "tax_bracket_vn": tax_bracket_vn,
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
}

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_CMPOPS = {
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
    ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne,
}


def _eval_node(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool)) or node.value is None:
            return node.value
        raise UnsafeExpressionError(f"constant not allowed: {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise UnsafeExpressionError(f"unknown variable: {node.id!r}")
        return env[node.id]
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise UnsafeExpressionError(f"operator not allowed: {type(node.op).__name__}")
        return op(_eval_node(node.left, env), _eval_node(node.right, env))
    if isinstance(node, ast.UnaryOp):
        op = _UNARYOPS.get(type(node.op))
        if op is None:
            raise UnsafeExpressionError(f"unary operator not allowed: {type(node.op).__name__}")
        return op(_eval_node(node.operand, env))
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, env)
        result = True
        for op_node, comparator in zip(node.ops, node.comparators):
            op = _CMPOPS.get(type(op_node))
            if op is None:
                raise UnsafeExpressionError(f"comparison not allowed: {type(op_node).__name__}")
            right = _eval_node(comparator, env)
            result = result and op(left, right)
            left = right
        return result
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, env) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.IfExp):
        return _eval_node(node.body, env) if _eval_node(node.test, env) else _eval_node(node.orelse, env)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise UnsafeExpressionError("only whitelisted functions may be called")
        args = [_eval_node(a, env) for a in node.args]
        kwargs = {kw.arg: _eval_node(kw.value, env) for kw in node.keywords}
        return _ALLOWED_FUNCS[node.func.id](*args, **kwargs)
    if isinstance(node, ast.List):
        return [_eval_node(e, env) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(e, env) for e in node.elts)
    raise UnsafeExpressionError(f"expression not allowed: {type(node).__name__}")


def safe_eval(expression: str, env: dict[str, Any]) -> Any:
    """Evaluate `expression` using only the whitelisted grammar above. No eval()/exec()."""
    tree = ast.parse(expression, mode="eval")
    return _eval_node(tree, env)


def compile_formula(spec: "FormulaSpec") -> Callable[[dict[str, Any]], dict[str, Any]]:
    """
    Turn a reviewed FormulaSpec into a plain Python function.

        fn = compile_formula(spec)
        outputs = fn({"base_salary": 15_000_000, "worked_days": 20, "total_days": 22})
        # -> {"gross_salary": 13_636_363, ...}

    - Variables whose source == "literal" are baked in from spec.variables[i].value.
    - All other variables (employee, attendance, rate_config, regulatory) must be
      supplied by the caller in the `inputs` dict when calling fn(...).
    - Rules run in order; each rule's `output_field` becomes available to later rules.
    - `condition`, if present, is evaluated first (as a boolean expression); the
      rule is skipped if it evaluates falsy.
    """
    literal_values = {
        v.name: v.value for v in spec.variables
        if getattr(v, "source", None) == "literal" and v.value is not None
    }
    required_inputs = {
        v.name for v in spec.variables if getattr(v, "source", None) != "literal"
    }

    def run(inputs: dict[str, Any]) -> dict[str, Any]:
        missing = required_inputs - inputs.keys()
        if missing:
            raise ValueError(f"missing required inputs: {sorted(missing)}")

        env: dict[str, Any] = {**literal_values, **inputs}
        outputs: dict[str, Any] = {}

        for rule in spec.rules:
            scope = {**env, **outputs}
            if rule.condition and not safe_eval(rule.condition, scope):
                continue
            value = safe_eval(rule.expression, scope)
            value = _apply_rounding(value, rule.rounding)
            outputs[rule.output_field] = value

        return outputs

    return run


def _apply_rounding(value: Any, rounding: str | None) -> Any:
    """Apply the same persisted rounding format accepted by formula_validator.

    ``round_down_1000`` is metadata, not a Python expression; evaluating it as
    an expression incorrectly looks for a variable named ``round_down_1000``.
    """
    if rounding is None:
        return value
    if rounding == "round":
        return round(value)
    prefix = "round_down_"
    if rounding.startswith(prefix):
        try:
            return round_down(value, float(rounding.removeprefix(prefix)))
        except ValueError as exc:
            raise ValueError(f"invalid rounding rule: {rounding!r}") from exc
    raise ValueError(f"unsupported rounding rule: {rounding!r}")
