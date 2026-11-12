"""A deliberately small code language for the one-shot arithmetic integration test.

This permits no imports, calls, attributes, loops, decorators or executable module
statements. It is not a sandbox for arbitrary Python or a general coding worker.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re

REPOSITORY = "WangYu0611/chatgpt-deepseek-harness-test-20261028"
ACTOR = "WangYu0611"
SCHEMA = "bounded-dsh-smoke/v1"


class SmokeError(RuntimeError):
    pass


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_source(source: str) -> str:
    """Validate and canonicalize only add(left,right) and multiply(left,right)."""
    if not isinstance(source, str) or len(source.encode("utf-8")) > 4096:
        raise SmokeError("Model source exceeds the 4096-byte limit")
    source = source.strip()
    if source.startswith("```python\n") and source.endswith("```"):
        source = source[len("```python\n"):-3].strip()
    elif source.startswith("```\n") and source.endswith("```"):
        source = source[4:-3].strip()
    try:
        module = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        raise SmokeError("Model did not return the required Python module") from None
    if len(list(ast.walk(module))) > 50 or len(module.body) != 2:
        raise SmokeError("Only two small arithmetic functions are allowed")
    names = []
    for function in module.body:
        if not isinstance(function, ast.FunctionDef):
            raise SmokeError("Executable module statements are forbidden")
        names.append(function.name)
        args = function.args
        if (function.name not in {"add", "multiply"} or function.decorator_list or function.returns
                or function.type_comment or getattr(function, "type_params", [])
                or args.posonlyargs or args.kwonlyargs or args.defaults or args.kw_defaults
                or args.vararg or args.kwarg or [x.arg for x in args.args] != ["left", "right"]
                or any(x.annotation or x.type_comment for x in args.args)
                or len(function.body) != 1 or not isinstance(function.body[0], ast.Return)):
            raise SmokeError("Function signature or body is outside the fixed smoke task")
        def expression(node, depth=0):
            if depth > 4:
                raise SmokeError("Arithmetic expression is too deep")
            if isinstance(node, ast.Name) and node.id in {"left", "right"} and isinstance(node.ctx, ast.Load):
                return
            if isinstance(node, ast.Constant) and type(node.value) is int and -10 <= node.value <= 10:
                return
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
                expression(node.left, depth + 1)
                expression(node.right, depth + 1)
                return
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                expression(node.operand, depth + 1)
                return
            raise SmokeError("Only bounded arithmetic expressions are permitted")
        expression(function.body[0].value)
        if function.name == "add":
            expected = ast.parse("left + right", mode="eval").body
            if ast.dump(function.body[0].value) != ast.dump(expected):
                raise SmokeError("The existing add implementation must remain unchanged")
    if sorted(names) != ["add", "multiply"]:
        raise SmokeError("Both distinct task functions are required")
    return ast.unparse(module) + "\n"


def validate_result(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("repository") != REPOSITORY:
        raise SmokeError("Wrong result schema or repository")
    if type(value.get("issue")) is not int or value["issue"] <= 0:
        raise SmokeError("Invalid Issue identity")
    for name, size in (("base_sha", 40), ("issue_body_sha256", 64), ("source_sha256", 64)):
        if not isinstance(value.get(name), str) or not re.fullmatch("[0-9a-f]{" + str(size) + "}", value[name]):
            raise SmokeError("Invalid result digest: " + name)
    if value.get("source_path") != "src/arithmetic.py":
        raise SmokeError("Only src/arithmetic.py may be published")
    normalized = validate_source(value.get("source_code"))
    if normalized != value["source_code"] or digest(normalized) != value["source_sha256"]:
        raise SmokeError("Source digest/canonical representation differs")
    if not isinstance(value.get("session_id"), str) or not value["session_id"].strip():
        raise SmokeError("Missing real DSH session identity")
    if value.get("harness_status") != "completed" or type(value.get("model_request_count")) is not int or value["model_request_count"] != 1:
        raise SmokeError("Expected one completed model request")
    tests = value.get("tests", {})
    if (type(tests.get("exit_code")) is not int or tests["exit_code"] != 0
            or type(tests.get("count")) is not int or tests["count"] < 7
            or tests.get("timed_out") is not False or tests.get("output_limit_exceeded") is not False):
        raise SmokeError("Actual trusted tests did not complete")
    for name in ("model", "version", "run_id", "run_attempt", "runner_name", "started_at", "finished_at"):
        if not isinstance(value.get(name), str) or not value[name] or len(value[name]) > 200:
            raise SmokeError("Missing or oversized result metadata: " + name)
    if not value["run_id"].isdigit() or not value["run_attempt"].isdigit():
        raise SmokeError("Invalid Actions run identity")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 32768:
        raise SmokeError("Result artifact exceeds its bound")
    return value


def check_issue(issue: dict, expected_number: int, body_sha: str) -> None:
    if (issue.get("number") != expected_number or issue.get("state") != "open" or issue.get("pull_request")
            or issue.get("user", {}).get("login") != ACTOR
            or "agent:ready" not in {x.get("name") for x in issue.get("labels", []) if isinstance(x, dict)}
            or digest(issue.get("body") or "") != body_sha):
        raise SmokeError("Live Issue no longer matches the authorized fixed task")


TRUSTED_TESTS = '''import unittest
from src.arithmetic import add, multiply

class ArithmeticSmokeTests(unittest.TestCase):
    def test_add_positive(self): self.assertEqual(add(2, 3), 5)
    def test_add_negative(self): self.assertEqual(add(-3, 2), -1)
    def test_add_zero(self): self.assertEqual(add(0, 0), 0)
    def test_multiply_positive(self): self.assertEqual(multiply(6, 7), 42)
    def test_multiply_negative(self): self.assertEqual(multiply(-4, 3), -12)
    def test_multiply_zero(self): self.assertEqual(multiply(9, 0), 0)
    def test_multiply_decimal(self): self.assertAlmostEqual(multiply(0.5, 0.2), 0.1)

if __name__ == '__main__': unittest.main(verbosity=2)
'''
