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

# Shared, single-source-of-truth implementations (see note below _ALLOWED_FUNCS
# for why these are no longer redefined in this module).
from ..builtin_functions import prorate, round_down, tax_bracket_vn

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
    ast.In: operator.contains, ast.NotIn: lambda values, value: not operator.contains(values, value),
}

def _eval_node(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool, str)) or node.value is None:
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
            matches = op(right, left) if isinstance(op_node, (ast.In, ast.NotIn)) else op(left, right)
            result = result and matches
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


def _apply_rule_rounding(value: float, rounding: str | None) -> float:
    """Apply a FormulaRule.rounding keyword to `value`.

    `rounding` is a short keyword string -- "round", "round_down", or
    "round_down_<positive unit>" (e.g. "round_down_1000") -- exactly the
    format `formula_validator._validate_rounding` enforces and the extraction
    prompt in formula_extractor.py instructs the LLM to produce. It is a
    keyword, not an expression: it must NOT be passed to safe_eval(), which
    would parse e.g. "round_down_1000" as an (undefined) variable name and
    raise UnsafeExpressionError.
    """
    if rounding is None:
        return value
    if rounding == "round":
        return round(value)
    if rounding == "round_down":
        return round_down(value)
    prefix = "round_down_"
    if rounding.startswith(prefix):
        return round_down(value, float(rounding[len(prefix):]))
    raise UnsafeExpressionError(f"unsupported rounding rule: {rounding!r}")

def summarize_by_section(spec: "FormulaSpec", outputs: dict[str, Any]) -> dict[str, Any]:
    """
    Group the rule outputs of a compiled formula run by accounting section
    (spec.field_categories: field_code -> 'line_items' | 'deductions' | 'employer_cost')
    and compute NET pay:

        NET = tong thu nhap (line_items) - tong khau tru (deductions: BHXH + PIT + khac)

    Employer-side costs (BHXH company portion, service fee, ...) are reported under
    'employer_cost' and are never subtracted from NET pay. field_codes with no entry in
    spec.field_categories are ignored here (they're typically intermediate helper outputs,
    not final salary components).
    """
    totals = {"line_items": 0.0, "deductions": 0.0, "employer_cost": 0.0}
    breakdown: dict[str, dict[str, Any]] = {"line_items": {}, "deductions": {}, "employer_cost": {}}
    field_categories = getattr(spec, "field_categories", {}) or {}
    for field_code, value in outputs.items():
        section = field_categories.get(field_code)
        if section not in totals:
            continue
        totals[section] += value
        breakdown[section][field_code] = value
    return {"totals": totals, "breakdown": breakdown, "net_salary": totals["line_items"] - totals["deductions"]}


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
            value = _apply_rule_rounding(value, rule.rounding)
            outputs[rule.output_field] = value

        return outputs

    return run