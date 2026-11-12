"""Reproducible local verification; no installs, model requests or publication."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab_harness.demo import run_demo
from lab_harness.evidence import write_json
from lab_harness.worker import utcnow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(".runtime/verification"))
    args = parser.parse_args()
    os.chdir(ROOT)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = utcnow()
    with (output / "unittest.log").open("w", encoding="utf-8") as stream:
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    report = {"mode": "offline-verification", "started_at": started, "finished_at": utcnow(), "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped), "passed": result.wasSuccessful(), "log": str(output / "unittest.log"), "real_end_to_end": "NOT_RUN", "external_deployment_or_sending": "NOT_PERFORMED"}
    if result.wasSuccessful():
        demo = run_demo(output / "demo", "all")
        report["demo_summary"] = str(Path(demo["root"]) / "summary.json")
        report["demo_scenarios"] = demo["scenarios"]
        schema_path = ROOT / "schemas/evidence.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        report["schema_json_syntax"] = "PASS"
        if importlib.util.find_spec("jsonschema"):
            from jsonschema import Draft202012Validator, FormatChecker
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            Draft202012Validator.check_schema(schema)
            manifests = list(Path(demo["root"]).rglob("evidence.json"))
            for manifest in manifests:
                validator.validate(json.loads(manifest.read_text(encoding="utf-8")))
            report["full_json_schema_validation"] = {"status": "PASS", "manifests": len(manifests)}
        else:
            report["full_json_schema_validation"] = "NOT_RUN: optional jsonschema dependency is not installed"
        if importlib.util.find_spec("yaml"):
            import yaml
            for path in [*ROOT.glob("workflows/*.yml"), *ROOT.glob("templates/*.yml")]:
                yaml.safe_load(path.read_text(encoding="utf-8"))
            report["yaml_syntax"] = "PASS"
        else:
            report["yaml_syntax"] = "NOT_RUN: optional PyYAML dependency is not installed"
    report["finished_at"] = utcnow()
    write_json(output / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
