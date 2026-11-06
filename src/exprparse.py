"""Pull a single arithmetic claim out of a line of reasoning.

Ported from the PromptGraph parser. It recognises ``2 * 12.50 = 25.00``,
``total = 12.50``, and a pure-numeric chain such as ``2 + 3 * 4 = 14``.
The evaluator is recursive descent. It does not call ``eval``.

A line that is not a clean sum returns ``None``. Currency marks (£, $, %)
are stripped first. Numbers are ``Decimal`` so pence stay exact.
"""

from __future__ import annotations

import re
from decimal import Decimal

_NUMBER = r"[-+]?\d+(?:\.\d+)?"
_VAR = r"[A-Za-z][A-Za-z0-9_]*"
_OPERAND = rf"(?:{_NUMBER}|{_VAR})"
_OP = r"[+\-*/]"

_P_ARITH = re.compile(rf"\s*({_OPERAND})\s*({_OP})\s*({_OPERAND})\s*=\s*({_OPERAND})\s*")
_P_ARITH_REV = re.compile(rf"\s*({_OPERAND})\s*=\s*({_OPERAND})\s*({_OP})\s*({_OPERAND})\s*")
_P_ASSIGN = re.compile(rf"\s*({_VAR})\s*=\s*({_NUMBER})\s*")
_NUMBER_FULL = re.compile(rf"{_NUMBER}")


class _ParseError(Exception):
    pass


def extract_arithmetic(text: str) -> dict | None:
    """Return one parsed sum, or ``None`` when the line is not one clean equation."""
    cleaned = _clean(text)
    if not cleaned or "=" not in cleaned:
        return None

    match = _P_ARITH.fullmatch(cleaned)
    if match:
        return {
            "kind": "arith",
            "lhs_a": _operand(match.group(1)),
            "op": match.group(2),
            "lhs_b": _operand(match.group(3)),
            "rhs": _operand(match.group(4)),
            "raw": text,
        }

    match = _P_ASSIGN.fullmatch(cleaned)
    if match:
        return {
            "kind": "assign",
            "var": match.group(1),
            "value": Decimal(match.group(2)),
            "raw": text,
        }

    match = _P_ARITH_REV.fullmatch(cleaned)
    if match:
        return {
            "kind": "arith",
            "lhs_a": _operand(match.group(2)),
            "op": match.group(3),
            "lhs_b": _operand(match.group(4)),
            "rhs": _operand(match.group(1)),
            "raw": text,
        }

    if cleaned.count("=") == 1:
        lhs_raw, rhs_raw = (part.strip() for part in cleaned.split("="))
        if _NUMBER_FULL.fullmatch(rhs_raw):
            lhs_value = _safe_eval_numeric(lhs_raw)
            if lhs_value is not None:
                return {
                    "kind": "arith_expr",
                    "lhs_raw": lhs_raw,
                    "lhs_value": lhs_value,
                    "rhs": Decimal(rhs_raw),
                    "raw": text,
                }
    return None


def evaluate_calculation(parsed: dict) -> tuple[Decimal, Decimal] | None:
    """Return ``(computed, claimed)`` for a numeric sum, or ``None`` if it has variables."""
    kind = parsed.get("kind")
    if kind == "arith":
        left = _as_decimal(parsed.get("lhs_a"))
        right = _as_decimal(parsed.get("lhs_b"))
        claimed = _as_decimal(parsed.get("rhs"))
        if left is None or right is None or claimed is None:
            return None
        computed = _apply(left, str(parsed.get("op")), right)
        if computed is None:
            return None
        return computed, claimed
    if kind == "arith_expr":
        computed = _as_decimal(parsed.get("lhs_value"))
        claimed = _as_decimal(parsed.get("rhs"))
        if computed is None or claimed is None:
            return None
        return computed, claimed
    return None


def evaluate_numeric(expression: str) -> Decimal | None:
    """Evaluate a pure numeric expression such as ``22.00 + 10.50``. No variables, no ``eval``."""
    return _safe_eval_numeric(_clean(expression))


def _clean(text: str) -> str:
    cleaned = (text or "").replace("£", "").replace("$", "").replace("%", "").replace(",", "")
    cleaned = cleaned.replace("×", "*").strip()
    if cleaned.endswith("."):
        cleaned = cleaned[:-1]
    return cleaned.strip()


def _operand(token: str) -> Decimal | str:
    token = token.strip()
    if _NUMBER_FULL.fullmatch(token):
        return Decimal(token)
    return token


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    return None


def _apply(left: Decimal, op: str, right: Decimal) -> Decimal | None:
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if op == "/":
        if right == 0:
            return None
        return left / right
    return None


def _tokenize_numeric(source: str) -> list | None:
    tokens: list = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if char in "+-*/()":
            tokens.append(char)
            index += 1
            continue
        if char.isdigit() or char == ".":
            end = index
            while end < len(source) and (source[end].isdigit() or source[end] == "."):
                end += 1
            chunk = source[index:end]
            if chunk.count(".") > 1:
                return None
            try:
                tokens.append(Decimal(chunk))
            except ArithmeticError:
                return None
            index = end
            continue
        return None
    return tokens


def _safe_eval_numeric(expression: str) -> Decimal | None:
    tokens = _tokenize_numeric(expression)
    if not tokens:
        return None
    try:
        value, position = _parse_expr(tokens, 0)
    except _ParseError:
        return None
    if position != len(tokens):
        return None
    return value


def _parse_expr(tokens: list, position: int) -> tuple[Decimal, int]:
    value, position = _parse_term(tokens, position)
    while position < len(tokens) and tokens[position] in ("+", "-"):
        op = tokens[position]
        rhs, position = _parse_term(tokens, position + 1)
        value = value + rhs if op == "+" else value - rhs
    return value, position


def _parse_term(tokens: list, position: int) -> tuple[Decimal, int]:
    value, position = _parse_factor(tokens, position)
    while position < len(tokens) and tokens[position] in ("*", "/"):
        op = tokens[position]
        rhs, position = _parse_factor(tokens, position + 1)
        if op == "*":
            value = value * rhs
        else:
            if rhs == 0:
                raise _ParseError("division by zero")
            value = value / rhs
    return value, position


def _parse_factor(tokens: list, position: int) -> tuple[Decimal, int]:
    if position >= len(tokens):
        raise _ParseError("unexpected end of expression")
    token = tokens[position]
    if token == "+":
        return _parse_factor(tokens, position + 1)
    if token == "-":
        value, position = _parse_factor(tokens, position + 1)
        return -value, position
    if token == "(":
        value, position = _parse_expr(tokens, position + 1)
        if position >= len(tokens) or tokens[position] != ")":
            raise _ParseError("missing closing parenthesis")
        return value, position + 1
    if isinstance(token, Decimal):
        return token, position + 1
    raise _ParseError(f"unexpected token {token!r}")
