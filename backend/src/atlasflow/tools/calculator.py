"""Restricted arithmetic tool and its argument schema."""

from __future__ import annotations

import ast
import math
import operator
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from atlasflow.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult


class CalculatorArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expression: str = Field(min_length=1, max_length=200)


class CalculatorTool(BaseTool):
    name = "calculator"
    description = "Evaluate an arithmetic expression with a restricted AST interpreter."
    risk_level = RiskLevel.LOW
    arguments_model = CalculatorArguments

    _binary: ClassVar[dict[type[ast.operator], Any]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.Mod: operator.mod,
    }
    _unary: ClassVar[dict[type[ast.unaryop], Any]] = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    async def run(self, arguments: CalculatorArguments, context: ToolContext) -> ToolResult:
        try:
            value = self._evaluate(ast.parse(arguments.expression, mode="eval").body)
            return ToolResult(success=True, data={"result": value})
        except (TypeError, ValueError, ZeroDivisionError, SyntaxError, OverflowError) as exc:
            return ToolResult(success=False, error=str(exc), retryable=False)

    @staticmethod
    def _bounded(value: float) -> float | int:
        if type(value) not in {int, float}:
            raise ValueError("Calculation must produce a real number")
        if isinstance(value, int) and value.bit_length() > 256:
            raise ValueError("Calculation exceeds the numeric limit")
        if isinstance(value, float) and (not math.isfinite(value) or abs(value) > 1e75):
            raise ValueError("Calculation exceeds the numeric limit")
        return value

    def _evaluate(self, node: ast.AST, depth: int = 0) -> float | int:
        if depth > 24:
            raise ValueError("Expression is too deeply nested")
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return self._bounded(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in self._binary:
            left = self._evaluate(node.left, depth + 1)
            right = self._evaluate(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("Exponent is too large")
            return self._bounded(self._binary[type(node.op)](left, right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in self._unary:
            return self._bounded(
                self._unary[type(node.op)](self._evaluate(node.operand, depth + 1))
            )
        raise ValueError("Only basic arithmetic expressions are allowed")
