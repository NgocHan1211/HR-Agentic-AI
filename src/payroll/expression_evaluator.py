from __future__ import annotations


def evaluate(expression: str, variables: dict) -> float:
    return eval(expression, {"__builtins__": {}}, variables)
