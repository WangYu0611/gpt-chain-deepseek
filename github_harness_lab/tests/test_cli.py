import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.__main__ import main
from lab_harness.demo import example_event


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        config = json.loads(Path("config.example.json").read_text())
        config["runtime_dir"] = str(root / "runtime")
        self.config = root / "config.json"
        self.config.write_text(json.dumps(config))
        self.event = root / "event.json"
        self.event.write_text(json.dumps(example_event()))

    def invoke(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(list(args))
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_plan_and_default_run_never_spawn(self):
        with patch("subprocess.Popen", side_effect=AssertionError("unexpected subprocess")):
            for command in ("plan", "run"):
                code, result = self.invoke(command, "--config", str(self.config), "--event", str(self.event))
                self.assertEqual(code, 0)
                self.assertEqual(result["mode"], "dry-run")
                self.assertEqual(result["external_actions"], [])

    def test_doctor_never_starts_desktop_or_harness(self):
        with patch("subprocess.Popen", side_effect=AssertionError("unexpected subprocess")):
            code, result = self.invoke("doctor", "--config", str(self.config))
        self.assertEqual(code, 0)
        self.assertEqual(result["network_calls"], 0)

    def test_execute_without_authorization_is_blocked_before_spawn(self):
        with patch("subprocess.Popen", side_effect=AssertionError("unexpected subprocess")):
            code, result = self.invoke("run", "--execute", "--config", str(self.config), "--event", str(self.event))
        self.assertEqual(code, 2)
        self.assertFalse(result["sent"])


if __name__ == "__main__":
    unittest.main()
