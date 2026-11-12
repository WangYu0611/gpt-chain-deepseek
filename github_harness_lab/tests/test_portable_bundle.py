"""Offline packaging checks: no model, network, Runner or shell execution."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.portable_bundle import CODE_FILES, WORKFLOW_PATH, build_bundle, code_manifest, controller_digest
from lab_harness.project_profile import make_profile, profile_digest


class PortableBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        for name in CODE_FILES:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# reviewed fixture: " + name + "\n", encoding="utf-8")
        (self.source / ".private").mkdir()
        (self.source / ".private/activation.json").write_text("DO_NOT_COPY", encoding="utf-8")
        (self.source / ".env").write_text("DO_NOT_COPY", encoding="utf-8")
        self.profile = make_profile("First/project", "main", "First")

    def build(self, name="bundle", profile=None):
        return build_bundle(profile or self.profile, self.root / name, source_root=self.source)

    def read(self, bundle, path):
        return json.loads((bundle / path).read_text(encoding="utf-8"))

    def test_inert_bundle_has_exact_code_snapshot_and_no_private_files(self):
        with patch("subprocess.Popen", side_effect=AssertionError("No processes")), \
                patch("socket.create_connection", side_effect=AssertionError("No network")):
            bundle = self.build()
        meta = self.read(bundle, "bundle-manifest.json")
        controller = bundle / "repository/Tools/HarnessBridge"
        self.assertEqual(meta["code_sha256"], code_manifest(self.source))
        self.assertEqual(meta["controller_sha256"], controller_digest(controller))
        copied = {p.relative_to(controller).as_posix() for p in controller.rglob("*") if p.is_file()}
        self.assertEqual(copied, set(CODE_FILES) | {"project-profile.json"})
        for file in (p for p in bundle.rglob("*") if p.is_file()):
            self.assertNotIn("DO_NOT_COPY", file.read_text(encoding="utf-8"))
        self.assertFalse(meta["workflow_enabled"])
        self.assertEqual(meta["model_requests"], 0)
        self.assertEqual(meta["normalization"], "utf8-lf")

    def test_workflow_separates_read_only_generation_from_publication(self):
        bundle = self.build()
        workflow = self.read(bundle, "repository/" + WORKFLOW_PATH + ".disabled")
        self.assertEqual(workflow["permissions"], {})
        self.assertEqual(workflow["on"], {"issues": {"types": ["opened"]}})
        self.assertIn("github.repository", workflow["concurrency"]["group"])
        self.assertIn("github.event.issue.number", workflow["concurrency"]["group"])
        generate, publish = workflow["jobs"]["generate"], workflow["jobs"]["publish"]
        self.assertIn("vars.HARNESS_DOCUMENTS_ENABLED == 'true'", generate["if"])
        self.assertIn(self.profile["repository"], generate["if"])
        self.assertIn(self.profile["document_tasks"][0]["task_id"], generate["if"])
        self.assertIn(self.profile["document_tasks"][1]["task_id"], generate["if"])
        self.assertNotIn(self.profile["stop_task"]["task_id"], generate["if"])
        self.assertTrue(all(value == "read" for value in generate["permissions"].values()))
        self.assertEqual(generate["permissions"]["actions"], "read")
        self.assertEqual(generate["runs-on"][-1], self.profile["runner_label"])
        self.assertNotIn("actions/checkout", json.dumps(generate))
        self.assertEqual(publish["permissions"], {
            "contents": "write", "issues": "read", "pull-requests": "write", "actions": "read"})
        self.assertFalse(publish["steps"][0]["with"]["persist-credentials"])
        self.assertIn("github.workflow_sha", publish["steps"][0]["with"]["ref"])
        self.assertEqual(publish["steps"][1]["with"]["artifact-ids"],
                         "${{ needs.generate.outputs.artifact-id }}")
        self.assertFalse(list((bundle / "repository/.github/workflows").glob("*.yml")))

    def test_workflow_scripts_compile_and_never_interpolate_issue_body(self):
        workflow = self.read(self.build(), "repository/" + WORKFLOW_PATH + ".disabled")
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "run" in step:
                    compile(step["run"], "<generated workflow>", "exec")
                    self.assertNotIn("${{", step["run"])
                    self.assertNotIn("github.event.issue.body", step["run"])
        generate = workflow["jobs"]["generate"]["steps"][0]
        self.assertIn("HARNESS_ACTIVATION_FILE", generate["run"])
        self.assertIn("Path(sys.executable).resolve()", generate["run"])
        self.assertEqual(generate["env"]["EXPECTED_PROFILE_SHA256"], profile_digest(self.profile))
        self.assertEqual(generate["env"]["EXPECTED_CONTROLLER_SHA256"], controller_digest(self.source))

    def test_two_repositories_share_identical_engine_bytes_but_distinct_profiles(self):
        first = self.build("first")
        other = make_profile("Second/different", "trunk", "Second")
        second = self.build("second", other)
        left = self.read(first, "bundle-manifest.json")
        right = self.read(second, "bundle-manifest.json")
        self.assertEqual(left["controller_sha256"], right["controller_sha256"])
        self.assertEqual(left["code_sha256"], right["code_sha256"])
        self.assertNotEqual(left["profile_sha256"], right["profile_sha256"])
        a = self.read(first, "repository/" + WORKFLOW_PATH + ".disabled")
        b = self.read(second, "repository/" + WORKFLOW_PATH + ".disabled")
        self.assertNotEqual(a["jobs"]["generate"]["runs-on"][-1], b["jobs"]["generate"]["runs-on"][-1])
        self.assertNotEqual(a["jobs"]["generate"]["if"], b["jobs"]["generate"]["if"])

    def test_cloud_script_imports_controller_from_workdir_with_clean_pythonpath(self):
        # Actions writes Python run blocks to an unrelated temporary script path.
        # Exercise that launch shape instead of merely compiling the run block.
        workflow = self.read(self.build(), "repository/" + WORKFLOW_PATH + ".disabled")
        script = workflow["jobs"]["publish"]["steps"][-1]["run"]
        workdir = self.root / "trusted-controller"
        package = workdir / "lab_harness"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "portable.py").write_text(
            "import json\ndef main(argv):\n    print(json.dumps(argv))\n    return 0\n",
            encoding="utf-8")
        outside = self.root / "runner-temp"
        outside.mkdir()
        entry = outside / "script.py"
        entry.write_text(script, encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "SystemRoot", "WINDIR", "TEMP", "TMP"}}
        env.update(PYTHONNOUSERSITE="1", PYTHONUTF8="1", GITHUB_WORKSPACE=str(self.root / "checkout"))
        completed = subprocess.run([sys.executable, "-I", str(entry)], cwd=workdir, env=env,
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        argv = json.loads(completed.stdout)
        self.assertEqual(argv[:3], ["publish", "--profile", "project-profile.json"])
        self.assertEqual(argv[-1], "--execute")
        self.assertEqual(Path(argv[4]), self.root / "checkout" / "harness-artifact" / "result.json")

    def test_activation_is_unarmed_and_credentials_are_not_exported(self):
        activation = self.read(self.build(), "local/activation.example.json")
        self.assertIs(activation["enabled"], False)
        for key in ("nonce", "workflow_sha", "control_issue", "arm_comment_id", "arm_body_sha256", "issued_at", "expires_at"):
            self.assertIsNone(activation[key])
        self.assertIsNone(activation["dsh"]["credentials_path"])
        self.assertIsNone(activation["dsh"]["usage_authorization"])

    def test_web_prompts_bind_selected_profile_and_independent_conversations(self):
        bundle = self.build()
        for name in ("web-manager.txt", "web-report.txt"):
            text = (bundle / name).read_text(encoding="utf-8")
            self.assertTrue(text.startswith("草案：未创建网页任务"))
            self.assertIn(profile_digest(self.profile), text)
            self.assertIn('"nonce": null', text)
            self.assertIn("两个不同的网页对话", text)
            self.assertIn("<!-- harness-task:v1 -->", text)
            self.assertIn("<!-- harness-arm:v1 -->", text)
            self.assertIn("第二项不能从第一项PR续作", text)
        self.assertIn("<!-- harness-report:v1 -->",
                      (bundle / "web-report.txt").read_text(encoding="utf-8"))

    def test_existing_output_is_not_overwritten(self):
        output = self.root / "existing"
        output.mkdir()
        file = output / "user.txt"
        file.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "new directory"):
            build_bundle(self.profile, output, source_root=self.source)
        self.assertEqual(file.read_text(), "keep")

    def test_missing_code_fails_before_output_is_created(self):
        (self.source / CODE_FILES[-1]).unlink()
        with self.assertRaisesRegex(ValueError, "trusted controller"):
            self.build()
        self.assertFalse((self.root / "bundle").exists())

    def test_code_manifest_changes_when_any_controller_dependency_changes(self):
        first = controller_digest(self.source)
        path = self.source / CODE_FILES[0]
        path.write_text("changed", encoding="utf-8")
        self.assertNotEqual(first, controller_digest(self.source))
        self.assertEqual(code_manifest(self.source)[CODE_FILES[0]], hashlib.sha256(b"changed").hexdigest())

    def test_lf_crlf_and_utf8_bom_share_digest_and_deploy_canonical_text(self):
        name = CODE_FILES[0]
        path = self.source / name
        canonical = "# Unicode 中文\nsecond line\n".encode("utf-8")
        path.write_bytes(canonical)
        expected = controller_digest(self.source)
        path.write_bytes(b"\xef\xbb\xbf" + canonical.replace(b"\n", b"\r\n"))
        self.assertEqual(controller_digest(self.source), expected)
        bundle = self.build()
        copied = bundle / "repository/Tools/HarnessBridge" / name
        self.assertEqual(copied.read_bytes(), canonical)
        self.assertEqual(controller_digest(copied.parents[1]), expected)
        meta = self.read(bundle, "bundle-manifest.json")
        self.assertEqual(meta["code_sha256"][name], hashlib.sha256(canonical).hexdigest())
        path.write_bytes(canonical.replace("中文".encode("utf-8"), "改动".encode("utf-8")))
        self.assertNotEqual(controller_digest(self.source), expected)

    def test_invalid_utf8_source_is_refused_before_export(self):
        (self.source / CODE_FILES[0]).write_bytes(b"\xff\xfeinvalid")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            self.build()
        self.assertFalse((self.root / "bundle").exists())

    def test_readme_requires_shared_persistent_state_and_blocks_lost_state(self):
        text = (self.build() / "README.txt").read_text(encoding="utf-8")
        self.assertIn("所有Runner必须共用同一持久 HARNESS_CONTROLLER_HOME", text)
        self.assertIn("升级必须保留 .runtime", text)
        self.assertIn("状态丢失时必须先暂停激活并只读对账", text)
        self.assertIn("跨目录或跨机器唯一", text)


if __name__ == "__main__":
    unittest.main()
