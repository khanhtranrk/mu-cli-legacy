"""Versioned Mu CEL profile. Interpreter only; no Python eval or transpilation."""

from __future__ import annotations

import json
import math
import operator
from typing import Any

import celpy
from celpy import celtypes
from celpy.evaluation import CELEvalError, base_functions
from lark import Token

ENGINE_KEY = "__mu_expression_engine"
CEL = "cel-v1"
LEGACY = "python-format-v1"
CALLS = {"has", "size", "string", "int", "double", "bool"}
METHODS = {"size", "contains", "startsWith", "endsWith"}
FORBIDDEN = {"__proto__", "constructor", "prototype"}


def engine_for(spec: dict) -> str:
    if not isinstance(spec, dict):
        raise TypeError("Spec must be a mapping")
    engine = spec.get("expression_engine", LEGACY)
    if engine not in (CEL, LEGACY):
        raise ValueError("expression_engine: expected cel-v1 or python-format-v1")
    return engine


def json_value(value: Any, depth: int = 0) -> Any:
    if depth > 16:
        raise ValueError("CEL JSON depth limit exceeded")
    if value is None:
        return None
    if isinstance(value, (bool, celtypes.BoolType)):
        return bool(value)
    if isinstance(value, (str, celtypes.StringType)):
        return str(value)
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or (
            number.is_integer() and abs(number) > 9007199254740991
        ):
            raise ValueError("CEL result must be a finite safe JSON number")
        return float(value) if isinstance(value, float) else int(value)
    if isinstance(value, (list, dict)):
        if len(value) > 128:
            raise ValueError("CEL collection limit exceeded")
        if isinstance(value, list):
            return [json_value(x, depth + 1) for x in value]
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or key in FORBIDDEN or len(key) > 160:
                raise ValueError("invalid CEL JSON map key")
            result[str(key)] = json_value(item, depth + 1)
        return result
    raise ValueError("CEL supports JSON values only")


def closing(text: str, start: int) -> int:
    depth, quote, triple, i = 0, "", False, start
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if triple and text.startswith(quote * 3, i):
                quote = ""
                i += 3
                continue
            if not triple and c == quote:
                quote = ""
        elif c in ("'", '"'):
            quote = c
            triple = text.startswith(c * 3, i)
            if triple:
                i += 2
        elif text.startswith("//", i):
            end = text.find("\n", i)
            if end < 0:
                break
            i = end
        elif c == "{":
            depth += 1
        elif c == "}" and depth:
            depth -= 1
        elif text.startswith("}}", i):
            return i
        i += 1
    raise ValueError("Unclosed ${{ ... }} expression")


def compile_source(source: str, budget: list[int]):
    budget[0] += 1
    if len(source) > 2048 or budget[0] > 192:
        raise ValueError("CEL expression/work limit exceeded")
    env = celpy.Environment()
    ast = env.compile(source)
    nodes = list(ast.iter_subtrees())
    budget[1] += len(nodes)
    # celpy's grammar has several wrapping nodes per semantic expression.
    if len(nodes) > 2048 or budget[1] > 65536:
        raise ValueError("CEL AST limit exceeded")
    for node in nodes:
        # celpy's cooked multiline literal decoder omits literal newlines.
        # Encode them as equivalent escapes before its normal literal decoder.
        if node.data == "literal" and len(node.children) == 1:
            token = node.children[0]
            if (
                isinstance(token, Token)
                and token.type == "MLSTRING_LIT"
                and not str(token).startswith(("r", "R"))
            ):
                normalized = str(token).replace("\r", "\\r").replace("\n", "\\n")
                node.children[0] = Token.new_borrow_pos(token.type, normalized, token)
        if node.data == "ident_arg" and str(node.children[0]) not in CALLS:
            raise ValueError("CEL function is outside the Mu v1 profile")
        if node.data == "member_dot_arg" and str(node.children[1]) not in METHODS:
            raise ValueError("CEL method is outside the Mu v1 profile")
        if node.data in {"member_object", "dot_ident", "dot_ident_arg"}:
            raise ValueError("CEL object construction/global namespaces are disabled")
    return env, ast


def validate_templates(
    value: Any, budget: list[int] | None = None, depth: int = 0
) -> None:
    budget = budget if budget is not None else [0, 0]
    if depth > 64:
        raise ValueError("Spec CEL syntax depth limit exceeded")
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                validate_templates(key, budget, depth + 1)
            validate_templates(item, budget, depth + 1)
    elif isinstance(value, list):
        for item in value:
            validate_templates(item, budget, depth + 1)
    elif isinstance(value, str):
        local = [0, 0]
        i = 0
        while i < len(value):
            escaped = value.startswith("$${{", i)
            if not escaped and not value.startswith("${{", i):
                i += 1
                continue
            try:
                end = closing(value, i + (4 if escaped else 3))
            except ValueError:
                if escaped:
                    return
                raise
            if not escaped:
                previous_nodes = local[1]
                compile_source(value[i + 3 : end].strip(), local)
                budget[0] += 1
                budget[1] += local[1] - previous_nodes
                if budget[0] > 4096 or budget[1] > 2097152:
                    raise ValueError("Spec CEL syntax work limit exceeded")
            i = end + 2


def activation_value(value: Any) -> Any:
    # JSON has one numeric type; integral numbers become CEL ints on both runtimes.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: activation_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [activation_value(v) for v in value]
    return value


def _boolean(value):
    return isinstance(value, (bool, celtypes.BoolType))


def _compatible_boolean(left, right):
    # Python's bool is int-like; CEL never treats a boolean as a number.
    if _boolean(left) != _boolean(right):
        raise TypeError("no such overload: boolean and non-boolean")
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        for a, b in zip(left, right):
            _compatible_boolean(a, b)
    if (
        isinstance(left, dict)
        and isinstance(right, dict)
        and left.keys() == right.keys()
    ):
        for key in left:
            _compatible_boolean(left[key], right[key])


def _signature(value):
    if isinstance(value, (list, dict)):
        children = list(value.values()) if isinstance(value, dict) else value
        kinds = {_signature(v) for v in children}
        return (
            "map" if isinstance(value, dict) else "list",
            next(iter(kinds)) if len(kinds) == 1 else "dyn",
        )
    return "bool" if _boolean(value) else type(value).__name__


def _aggregate_equal(left, right):
    if len(left) != len(right):
        return False
    if isinstance(left, dict) and left.keys() != right.keys():
        return False
    a, b = _signature(left), _signature(right)
    if a != b and "dyn" not in repr(a) and "dyn" not in repr(b):
        raise TypeError("no such aggregate equality overload")
    pairs = (
        ((left[k], right[k]) for k in left)
        if isinstance(left, dict)
        else zip(left, right)
    )
    for x, y in pairs:
        if _boolean(x) != _boolean(y):
            return False
        if isinstance(x, (list, dict)) and isinstance(y, type(x)):
            if not _aggregate_equal(x, y):
                return False
        elif not base_functions["_==_"](x, y):
            return False
    return True


def _guarded_operator(name):
    def apply(left, right):
        if name in {"_+_", "_-_", "_*_", "_/_", "_%_"}:
            if _boolean(left) or _boolean(right):
                raise TypeError("boolean arithmetic is not CEL")
        else:
            if (
                name in {"_==_", "_!=_"}
                and isinstance(left, (list, dict))
                and isinstance(right, type(left))
            ):
                return celtypes.BoolType(
                    _aggregate_equal(left, right) != (name == "_!=_")
                )
            _compatible_boolean(left, right)
            if name in {"_<_", "_<=_", "_>_", "_>=_"} and all(
                isinstance(v, (int, float)) and not _boolean(v) for v in (left, right)
            ):
                native = [
                    float(v) if isinstance(v, float) else int(v) for v in (left, right)
                ]
                fn = {
                    "_<_": operator.lt,
                    "_<=_": operator.le,
                    "_>_": operator.gt,
                    "_>=_": operator.ge,
                }[name]
                return celtypes.BoolType(fn(*native))
        return base_functions[name](left, right)

    return apply


def _in(item, container):
    if isinstance(item, CELEvalError):
        return item
    if isinstance(container, CELEvalError):
        return container
    if not isinstance(container, (list, dict)):
        raise TypeError("in requires a list or map")
    error = None
    for candidate in container:
        try:
            _compatible_boolean(item, candidate)
            if item == candidate:
                return celtypes.BoolType(True)
        except TypeError as exc:
            error = exc
    if error:
        raise error
    return celtypes.BoolType(False)


def _convert(kind, value):
    if kind == "bool":
        if _boolean(value):
            return celtypes.BoolType(value)
        if isinstance(value, str) and value in {
            "1",
            "t",
            "true",
            "TRUE",
            "True",
            "0",
            "f",
            "false",
            "FALSE",
            "False",
        }:
            return celtypes.BoolType(value in {"1", "t", "true", "TRUE", "True"})
        raise TypeError("bool expects boolean or a supported boolean string")
    if kind == "string":
        if _boolean(value):
            return celtypes.StringType("true" if value else "false")
        if value is None or not isinstance(value, (str, int, float)):
            raise TypeError("string expects a scalar")
        return celtypes.StringType(scalar_text(json_value(value)))
    if _boolean(value) or value is None or not isinstance(value, (str, int, float)):
        raise TypeError("numeric conversion expects string or number")
    return (celtypes.IntType if kind == "int" else celtypes.DoubleType)(value)


def _negate(value):
    if _boolean(value):
        raise TypeError("boolean negation must use !")
    return base_functions["-_"](value)


# Narrow profile adapters close celpy's Python-bool/int and cast differences.
# Evaluation, parsing, short circuiting and all other operators remain in celpy.
PROFILE_FUNCTIONS = {
    name: _guarded_operator(name)
    for name in (
        "_==_",
        "_!=_",
        "_<_",
        "_<=_",
        "_>_",
        "_>=_",
        "_+_",
        "_-_",
        "_*_",
        "_/_",
        "_%_",
    )
}
PROFILE_FUNCTIONS.update({"_in_": _in, "-_": _negate})
for _kind in ("bool", "string", "int", "double"):
    PROFILE_FUNCTIONS[_kind] = lambda value, kind=_kind: _convert(kind, value)


def evaluate(source: str, context: dict, budget: list[int]) -> Any:
    env, ast = compile_source(source, budget)
    activation = {
        k: celpy.json_to_cel(activation_value(v))
        for k, v in context.items()
        if k != ENGINE_KEY
    }
    result = json_value(
        env.program(ast, functions=PROFILE_FUNCTIONS).evaluate(activation)
    )
    if len(json.dumps(result, ensure_ascii=False).encode()) > 32768:
        raise ValueError("CEL result size limit exceeded")
    return result


def scalar_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        if value == 0:
            return "0"
        raw = repr(value)
        if "e" in raw:
            from decimal import Decimal

            exponent = int(raw.split("e")[1])
            if -6 <= exponent < 21:
                decimal = format(Decimal(raw), "f")
                return decimal.rstrip("0").rstrip(".") if "." in decimal else decimal
            base = raw.split("e")[0].removesuffix(".0")
            return base + "e" + ("+" if exponent >= 0 else "-") + str(abs(exponent))
        return raw.removesuffix(".0")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render(value: Any, context: dict, budget: list[int] | None = None) -> Any:
    budget = budget if budget is not None else [0, 0]
    if isinstance(value, list):
        return [render(x, context, budget) for x in value]
    if isinstance(value, dict):
        return {k: render(v, context, budget) for k, v in value.items()}
    if not isinstance(value, str):
        return value
    parts, literal, i = [], "", 0
    while i < len(value):
        escaped = value.startswith("$${{", i)
        if escaped or value.startswith("${{", i):
            start = i + (4 if escaped else 3)
            try:
                end = closing(value, start)
            except ValueError:
                if not escaped:
                    raise
                literal += value[i + 1 :]
                break
            if escaped:
                literal += value[i + 1 : end + 2]
            else:
                if literal:
                    parts.append((False, literal))
                literal = ""
                parts.append(
                    (True, evaluate(value[start:end].strip(), context, budget))
                )
            i = end + 2
        else:
            literal += value[i]
            i += 1
    if literal:
        parts.append((False, literal))
    meaningful = [(e, v) for e, v in parts if e or str(v).strip()]
    if len(meaningful) == 1 and meaningful[0][0]:
        return meaningful[0][1]
    output = ""
    for expression, item in parts:
        if expression and isinstance(item, (dict, list)):
            raise ValueError("List/map results require a whole-value expression")
        output += scalar_text(item) if expression else str(item)
    return output
