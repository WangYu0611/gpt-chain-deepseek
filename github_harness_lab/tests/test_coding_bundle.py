"""Offline deployment drafts and isolated fake CLI script launch checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.coding_bundle import (CODE_FILES, CONTROLLER_PATH, PROFILE_FILE, WORKFLOW_PATH,
                                      build_bundle, code_manifest, controller_digest)
from lab_harness.coding_contract import profile_digest
from lab_harness.portable_bundle import CODE_FILES as DOCUMENT_CODE_FILES
from tests.coding_fixtures import make_profile


class CodingBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="coding-bundle-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        for name in CODE_FILES:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("# Trusted fixture " + name + "\n").encode("utf-8"))
        (self.source / ".private").mkdir()
        (self.source / ".private/activation.json").write_text("PRIVATE-NOT-EXPORTED", encoding="utf-8")
        (self.source / ".env").write_text("PRIVATE-NOT-EXPORTED", encoding="utf-8")
        self.profile = make_profile()

    def build(self, name="bundle", profile=None):
        return build_bundle(self.profile if profile is None else profile, self.root / name, source_root=self.source)

    @staticmethod
    def read(bundle, path):
        return json.loads((bundle / path).read_text(encoding="utf-8"))

    def workflow(self):
        return self.read(self.build(), "repository/" + WORKFLOW_PATH + ".disabled")

    def test_exact_dependency_whitelist_with_code_capability_no_secrets(self):
        self.assertEqual(len(CODE_FILES), len(set(CODE_FILES)))
        self.assertTrue(set(DOCUMENT_CODE_FILES) < set(CODE_FILES))
        self.assertIn("live_tools/code_request_guard.mjs", CODE_FILES)
        for name in ("code_changes", "coding_contract", "coding_runtime", "coding_dsh", "coding_engine",
                     "coding_publisher", "coding", "coding_bundle"):
            self.assertIn("lab_harness/" + name + ".py", CODE_FILES)
        with patch("subprocess.Popen", side_effect=AssertionError("No process launch")), \
                patch("socket.create_connection", side_effect=AssertionError("No network")):
            bundle = self.build()
        controller = bundle / "repository" / CONTROLLER_PATH
        actual = {p.relative_to(controller).as_posix() for p in controller.rglob("*") if p.is_file()}
        self.assertEqual(actual, set(CODE_FILES) | {PROFILE_FILE})
        meta = self.read(bundle, "bundle-manifest.json")
        self.assertEqual(meta["controller_sha256"], controller_digest(controller))
        self.assertEqual(meta["code_sha256"], code_manifest(self.source))
        self.assertEqual(meta["status"], "REVIEW_ONLY_NOT_DEPLOYED")
        self.assertEqual(meta["live_validation"], "NOT_RUN")
        self.assertFalse(meta["workflow_enabled"])
        self.assertEqual(meta["model_requests"], 0)
        for path in bundle.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"PRIVATE-NOT-EXPORTED", path.read_bytes())

    def test_disabled_workflow_private_repo_exact_owner_marker_no_fixed_issue(self):
        workflow = self.workflow()
        self.assertEqual(workflow["permissions"], {})
        self.assertEqual(workflow["on"], {"issues": {"types": ["opened"]}})
        generate = workflow["jobs"]["generate"]
        self.assertIn("vars.HARNESS_CODE_ENABLED == 'true'", generate["if"])
        self.assertIn("github.repository == 'Example/alpha'", generate["if"])
        self.assertIn("github.event.sender.login == 'Example'", generate["if"])
        self.assertIn("github.event.issue.user.login == 'Example'", generate["if"])
        self.assertIn("github.event.repository.private == true", generate["if"])
        self.assertIn("<!-- harness-code-task:v1 -->", generate["if"])
        self.assertNotIn("issue.number ==", generate["if"])
        self.assertNotIn("issue.title ==", generate["if"])
        self.assertEqual(generate["timeout-minutes"], 120)
        self.assertEqual(generate["runs-on"], ["self-hosted", "Windows", "X64", self.profile["runner_label"]])
        self.assertIn("github.repository", workflow["concurrency"]["group"])
        self.assertIn("github.event.issue.number", workflow["concurrency"]["group"])
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        self.assertFalse(list((self.root / "bundle/repository/.github/workflows").glob("*.yml")))

    def test_generation_read_only_and_publication_pinned_to_same_run_artifact(self):
        workflow = self.workflow()
        generate, publish = workflow["jobs"]["generate"], workflow["jobs"]["publish"]
        self.assertEqual(generate["permissions"], {"contents": "read", "issues": "read", "pull-requests": "read", "actions": "read"})
        self.assertNotIn("actions/checkout", json.dumps(generate))
        self.assertEqual(publish["permissions"], {"contents": "write", "issues": "read", "pull-requests": "write", "actions": "read"})
        self.assertEqual(generate["steps"][1]["with"]["path"], "${{ steps.controller.outputs.artifact_path }}")
        self.assertEqual(publish["steps"][0]["with"]["ref"], "${{ github.workflow_sha }}")
        self.assertFalse(publish["steps"][0]["with"]["persist-credentials"])
        self.assertEqual(publish["steps"][1]["with"]["artifact-ids"], "${{ needs.generate.outputs.artifact-id }}")
        self.assertNotIn("name", publish["steps"][1]["with"])
        self.assertEqual(publish["steps"][-1]["working-directory"], CONTROLLER_PATH)
        for job in workflow["jobs"].values():
            self.assertNotIn("name", job)  # CLI verifies real jobs named generate/publish.
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"@([0-9a-f]{40})$")

    def test_generated_scripts_have_no_untrusted_interpolation_and_literal_hashes(self):
        workflow = self.workflow()
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "run" in step:
                    compile(step["run"], "<workflow>", "exec")
                    self.assertNotIn("${{", step["run"])
                    self.assertNotIn("github.event.issue.body", step["run"])
                    self.assertEqual(step["env"]["EXPECTED_PROFILE_SHA256"], profile_digest(self.profile))
                    self.assertEqual(step["env"]["EXPECTED_CONTROLLER_SHA256"], controller_digest(self.source))
        script = workflow["jobs"]["generate"]["steps"][0]["run"]
        self.assertIn("Path(sys.executable).resolve()", script)
        self.assertIn("str(home).isascii()", script)
        self.assertIn("HARNESS_ACTIVATION_FILE", script)

    def _execute_fake_script(self, script, command):
        workdir = self.root / (command + "-controller")
        package = workdir / "lab_harness"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "coding.py").write_text(
            "import json\ndef main(argv):\n    print(json.dumps(argv))\n    return 0\n", encoding="utf-8")
        activation = self.root / "disabled-activation.json"
        activation.write_text("{}", encoding="utf-8")
        outside = self.root / (command + "-runner-temp")
        outside.mkdir()
        entry = outside / "script.py"
        entry.write_text(script, encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "SystemRoot", "WINDIR", "TEMP", "TMP"}}
        env.update(PYTHONNOUSERSITE="1", PYTHONUTF8="1", GITHUB_WORKSPACE=str(self.root / "checkout"),
                   HARNESS_CONTROLLER_HOME=str(workdir), HARNESS_CONTROLLER_PYTHON=sys.executable,
                   HARNESS_ACTIVATION_FILE=str(activation), HARNESS_ISSUE="901",
                   GITHUB_EVENT_PATH=str(self.root / "event.json"))
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(entry)], cwd=workdir, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_publish_temporary_script_imports_cwd_with_clean_pythonpath(self):
        script = self.workflow()["jobs"]["publish"]["steps"][-1]["run"]
        argv = self._execute_fake_script(script, "publish")
        self.assertEqual(argv[:3], ["publish", "--profile", PROFILE_FILE])
        self.assertEqual(Path(argv[4]), self.root / "checkout/harness-code-artifact/result.json")
        self.assertEqual(argv[-1], "--execute")

    def test_generate_temporary_script_imports_persistent_home_and_exact_config(self):
        script = self.workflow()["jobs"]["generate"]["steps"][0]["run"]
        argv = self._execute_fake_script(script, "generate")
        self.assertEqual(argv[0], "generate")
        self.assertEqual(Path(argv[2]), self.root / "generate-controller" / PROFILE_FILE)
        self.assertEqual(Path(argv[4]), self.root / "disabled-activation.json")
        self.assertEqual(Path(argv[6]), self.root / "event.json")
        self.assertEqual(argv[-1], "--execute")

    def test_real_packaged_controller_imports_and_inspects_without_live_actions(self):
        source = Path(__file__).resolve().parent.parent
        bundle = build_bundle(self.profile, self.root / "real-source-bundle", source_root=source)
        controller = bundle / "repository" / CONTROLLER_PATH
        entry = self.root / "offline-inspect.py"
        entry.write_text(
            "import importlib,sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path.cwd()))\n"
            "for name in ('code_changes','coding_runtime','coding_dsh','coding_engine','coding_publisher','coding_bundle'):\n"
            "    importlib.import_module('lab_harness.'+name)\n"
            "from lab_harness.coding import main\nraise SystemExit(main(['inspect','--profile','code-profile.json']))\n",
            encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "SystemRoot", "WINDIR", "TEMP", "TMP"}}
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(entry)], cwd=controller, env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual(info["status"], "LOCAL_PROFILE_ONLY")
        self.assertEqual(info["network_calls"], 0)
        self.assertEqual(info["model_calls"], 0)

    def test_activation_is_inert_with_new_null_bindings_and_no_expiry(self):
        value = self.read(self.build(), "local/activation.example.json")
        self.assertFalse(value["enabled"])
        for key in ("workflow_sha", "controller_sha256", "control_issue", "arm_comment_id", "arm_body_sha256",
                    "nonce", "issued_at", "expires_at", "max_tasks", "max_model_requests"):
            self.assertIsNone(value[key])
        for key in ("desktop_install", "credentials_path", "usage_authorization"):
            self.assertIsNone(value["dsh"][key])

    def test_prompts_use_separate_new_bindings_continuous_protocol_and_prior_review(self):
        self.profile["context_paths"].append("Docs/Reports/TASK-M2-21.md")
        bundle = self.build()
        manager = (bundle / "web-manager.txt").read_text(encoding="utf-8")
        report = (bundle / "web-report.txt").read_text(encoding="utf-8")
        for text in (manager, report):
            self.assertTrue(text.startswith("草案：未创建网页任务"))
            self.assertIn(profile_digest(self.profile), text)
            self.assertIn("两个不同的网页对话", text)
            self.assertIn('"nonce": null', text)
            self.assertIn("<!-- harness-code-task:v1 -->", text)
            self.assertIn("<!-- harness-code-arm:v1 -->", text)
            self.assertIn("没有预设总时长或正常任务数上限", text)
            self.assertIn("同一问题连续两次FAIL暂停", text)
            self.assertIn("base_sha=前一PR", text)
            self.assertNotIn("stop_task", text)
        self.assertIn("优先独立复核已有待验收报告：TASK-M2-21", manager)
        self.assertIn("每小时", manager)
        self.assertIn("可依据仍未关闭的缺陷发布有限诊断、补证、测试或代码审查任务", manager)
        self.assertIn("不预设根因、不重做已完成的分类功能", manager)
        self.assertIn("只有要宣布视觉修复通过时，才必须取得真实渲染与相关输入证据", manager)
        self.assertNotIn("若UI闪动根因或视觉行为没有真实证据，标BLOCKED", manager)
        self.assertIn("<!-- harness-code-report:v1 -->", report)
        self.assertIn("github-actions[bot]", report)
        self.assertIn("visual_acceptance=NOT_RUN", report)
        self.assertIn("没有固定第三项停止标记", report)

    def test_same_engine_different_repository_only_changes_profile_and_guards(self):
        first = self.build("first")
        second = self.build("second", make_profile("Other/project", "trunk", "Other"))
        a, b = (self.read(path, "bundle-manifest.json") for path in (first, second))
        self.assertEqual(a["code_sha256"], b["code_sha256"])
        self.assertEqual(a["controller_sha256"], b["controller_sha256"])
        self.assertNotEqual(a["profile_sha256"], b["profile_sha256"])
        a, b = (self.read(path, "repository/" + WORKFLOW_PATH + ".disabled") for path in (first, second))
        self.assertNotEqual(a["jobs"]["generate"]["if"], b["jobs"]["generate"]["if"])

    def test_crlf_bom_canonicalize_to_lf_and_meaningful_change_alters_digest(self):
        path = self.source / CODE_FILES[0]
        raw = "# 中文 fixture\nsecond line\n".encode("utf-8")
        path.write_bytes(raw)
        expected = controller_digest(self.source)
        path.write_bytes(b"\xef\xbb\xbf" + raw.replace(b"\n", b"\r\n"))
        self.assertEqual(controller_digest(self.source), expected)
        bundle = self.build()
        self.assertEqual((bundle / "repository" / CONTROLLER_PATH / CODE_FILES[0]).read_bytes(), raw)
        self.assertEqual(self.read(bundle, "bundle-manifest.json")["normalization"], "utf8-lf")
        path.write_bytes(raw + b"# changed\n")
        self.assertNotEqual(controller_digest(self.source), expected)

    def test_missing_or_invalid_source_does_not_create_bundle(self):
        path = self.source / CODE_FILES[-1]
        path.write_bytes(b"\xffinvalid")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            self.build()
        self.assertFalse((self.root / "bundle").exists())
        path.unlink()
        with self.assertRaisesRegex(ValueError, "trusted controller"):
            self.build()
        self.assertFalse((self.root / "bundle").exists())

    def test_no_overwrite_and_readme_states_real_operational_limits(self):
        bundle = self.build()
        with self.assertRaisesRegex(ValueError, "new directory"):
            self.build()
        text = (bundle / "README.txt").read_text(encoding="utf-8")
        for expected in ("同一持久ASCII HARNESS_CONTROLLER_HOME", "状态丢失", "禁止重新激活", "不是跨目录或跨机器",
                         "run_attempt>1拒绝", "没有自动跟随merge", "用户手动", "原始模型/构建日志留本地",
                         "真实模型请求、UE构建、网页触发与连续闭环实测均NOT_RUN", "输入192 KiB", "输出8192 token"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
