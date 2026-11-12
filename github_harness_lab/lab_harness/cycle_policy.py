"""Fixed two-rework drill: explicit fault injection and actual bounded tests.

Product failures may be published to this disposable test PR. AST/scope or
evidence failures may not. This module never grants remote or model access.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .process import safe_environment
from .smoke_policy import REPOSITORY, validate_source, digest

SCHEMA = "bounded-dsh-cycle/v1"
LOOP_ID = "DSH-REWORK-002"
ISSUE = 5
CRITERIA = ("arithmetic-correctness", "scope-safety", "execution-evidence")
FAULTS = ("replace-multiply-add", "multiply-plus-one", "none")


def inject_fault(source: str, revision: int) -> tuple[str, dict]:
    canonical = validate_source(source)
    if type(revision) is not int or revision not in (0, 1, 2):
        raise ValueError("Only three candidate revisions are authorized")
    if revision < 2:
        tree = ast.parse(canonical)
        multiply = next(node for node in tree.body if node.name == "multiply")
        multiply.body[0].value = ast.parse("left + right" if revision == 0 else "left * right + 1", mode="eval").body
        canonical = validate_source(ast.unparse(ast.fix_missing_locations(tree)) + "\n")
    return canonical, {"kind": FAULTS[revision], "disclosed": True}


TEST_CODE = '''import importlib.util, pathlib, unittest
spec = importlib.util.spec_from_file_location("candidate", pathlib.Path(__file__).with_name("arithmetic.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
class Product(unittest.TestCase):
    def test_add_positive(self): self.assertEqual(m.add(2, 3), 5)
    def test_add_negative(self): self.assertEqual(m.add(-4, 1), -3)
    def test_add_zero(self): self.assertEqual(m.add(0, 0), 0)
    def test_add_fraction(self): self.assertAlmostEqual(m.add(0.1, 0.2), 0.3)
    def test_multiply_positive(self): self.assertEqual(m.multiply(6, 7), 42)
    def test_multiply_negative(self): self.assertEqual(m.multiply(-3, 5), -15)
    def test_multiply_two_negative(self): self.assertEqual(m.multiply(-3, -5), 15)
    def test_multiply_zero(self): self.assertEqual(m.multiply(123, 0), 0)
    def test_multiply_fraction(self): self.assertAlmostEqual(m.multiply(0.5, 0.2), 0.1)
    def test_multiply_large(self): self.assertEqual(m.multiply(10**8, 3), 300000000)
unittest.main(verbosity=2)
'''


def test_source(source: str) -> dict:
    """Execute only AST-allowed arithmetic; preserve genuine failed assertions."""
    canonical = validate_source(source)
    with tempfile.TemporaryDirectory(prefix="dsh-cycle-tests-") as temporary:
        root = Path(temporary)
        (root / "arithmetic.py").write_text(canonical, encoding="utf-8")
        test = root / "test_product.py"
        test.write_text(TEST_CODE, encoding="utf-8")
        result = subprocess.run([sys.executable, "-I", str(test)], cwd=root,
                                env=safe_environment(), capture_output=True,
                                timeout=15, encoding="utf-8", errors="replace")
    logs = result.stdout + result.stderr
    if len(logs.encode("utf-8")) > 65536 or "Ran 10 tests" not in logs or result.returncode not in (0, 1):
        raise ValueError("Independent tests did not produce a complete bounded result")
    failures = re.findall(r"^(?:FAIL|ERROR): (test_[A-Za-z0-9_]+)", logs, re.M)
    if (result.returncode == 0) != (not failures):
        raise ValueError("Inconsistent test result")
    return {"count": 10, "exit_code": result.returncode,
            "conclusion": "success" if result.returncode == 0 else "failure",
            "failed_tests": failures, "passed": 10 - len(failures),
            "log_sha256": digest(logs), "log": logs,
            "timed_out": False, "output_limit_exceeded": False}


def validate_cycle_artifact(data: dict) -> dict:
    if (not isinstance(data, dict) or data.get("schema") != SCHEMA
            or data.get("repository") != REPOSITORY or data.get("issue") != ISSUE
            or type(data.get("issue")) is not int or data.get("loop_id") != LOOP_ID
            or data.get("source_path") != "src/arithmetic.py"):
        raise ValueError("Unexpected fixed cycle artifact")
    revision = data.get("revision")
    if type(revision) is not int or revision not in (0, 1, 2):
        raise ValueError("Excessive candidate revision")
    for key in ("base_sha", "parent_sha", "workflow_sha"):
        if not isinstance(data.get(key), str) or not re.fullmatch(r"[0-9a-f]{40}", data[key]):
            raise ValueError("Invalid revision digest")
    for key in ("issue_body_sha256", "source_sha256", "model_source_sha256"):
        if not isinstance(data.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", data[key]):
            raise ValueError("Invalid content digest")
    for key in ("run_id", "run_attempt"):
        if not re.fullmatch(r"[1-9][0-9]*", str(data.get(key, ""))):
            raise ValueError("Missing Actions identity")
    if data.get("execution_stage") != f"generate-{revision}":
        raise ValueError("Invalid generation stage")
    if data.get("model_request_count") != 1 or type(data.get("model_request_count")) is not int or data.get("harness_status") != "completed":
        raise ValueError("One completed model request is required")
    if type(data.get("harness_exit_code")) is not int or data["harness_exit_code"] != 0:
        raise ValueError("A real successful DSH process receipt is required")
    for key in ("session_id", "model", "version"):
        if not isinstance(data.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", data[key]):
            raise ValueError("Invalid model receipt")
    model = validate_source(data.get("model_source_code"))
    source = validate_source(data.get("source_code"))
    if model != data["model_source_code"] or source != data["source_code"]:
        raise ValueError("Sources must be canonical")
    if digest(model) != data["model_source_sha256"] or digest(source) != data["source_sha256"]:
        raise ValueError("Source digest mismatch")
    expected, fault = inject_fault(model, revision)
    if source != expected or data.get("fault_injection") != fault:
        raise ValueError("Undisclosed or unexpected fault injection")
    for tests in (data.get("tests"), data.get("model_tests")):
        if (not isinstance(tests, dict) or type(tests.get("count")) is not int or tests["count"] != 10
                or type(tests.get("exit_code")) is not int or tests["exit_code"] not in (0, 1)
                or tests.get("timed_out") is not False or tests.get("output_limit_exceeded") is not False
                or not isinstance(tests.get("log"), str) or digest(tests["log"]) != tests.get("log_sha256")
                or not isinstance(tests.get("failed_tests"), list)
                or len(set(tests["failed_tests"])) != len(tests["failed_tests"])
                or any(not re.fullmatch(r"test_[A-Za-z0-9_]+", name) for name in tests["failed_tests"])
                or type(tests.get("passed")) is not int or tests["passed"] != 10 - len(tests["failed_tests"])
                or tests.get("conclusion") != ("success" if tests["exit_code"] == 0 else "failure")
                or (tests["exit_code"] == 0) != (not tests["failed_tests"])):
            raise ValueError("Incomplete local test evidence")
    # The publisher reruns both the raw model source and the actual candidate.
    if data.get("model_tests", {}).get("exit_code") != 0:
        raise ValueError("The model failed to repair the product before injection")
    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > 65536:
        raise ValueError("Artifact exceeds fixed bound")
    return data
