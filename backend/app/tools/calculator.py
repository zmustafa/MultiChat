from __future__ import annotations

import ast
import operator

from .base import ToolContext, ToolDef, ToolResult

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("Only numeric constants allowed")
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("Unsupported expression")


class CalculatorTool:
    definition = ToolDef(
        name="calculator",
        description="Evaluate a basic arithmetic expression "
        "(+, -, *, /, //, %, ** and parentheses).",
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Arithmetic expression, e.g. '2 * (3 + 4)'",
                },
            },
            "required": ["expression"],
        },
    )

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        expr = (args.get("expression") or "").strip()
        try:
            tree = ast.parse(expr, mode="eval")
            result = _eval(tree.body)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"Invalid expression: {exc}", citations=None)
        return ToolResult(content=str(result), citations=None)
