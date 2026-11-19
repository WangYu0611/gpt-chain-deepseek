from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unattended import config
from unattended.secrets import read_secret, validate_api_base_url


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.path = self.root / "settings" / "project.json"
        self.path.parent.mkdir()
        self.value = {
            "version": 1, "project_path": "../project", "runtime_dir": "runtime",
            "goal": "实现明确目标并通过项目检查", "branch": "unattended/test-project",
            "schedule": {"weekday_start": "18:00", "weekday_end": "08:00"},
            "codex": {"command": ["codex.exe"]},
            "dsh": {"command": ["dsh.exe"], "home": "dsh-home", "patches": ["account.yml"]},
            "checks": [{"id": "unit", "argv": ["python.exe", "-m", "unittest"], "timeout_seconds": 60}],
        }

    def save(self, value=None):
        self.path.write_text(json.dumps(self.value if value is None else value, ensure_ascii=False), encoding="utf-8")
        return self.path

    def test_paths_resolve_against_config_parent(self):
        loaded = config.load_config(self.save())
        self.assertEqual(loaded["project_path"], str(self.project))
        self.assertEqual(loaded["runtime_dir"], str(self.path.parent / "runtime"))
        self.assertEqual(loaded["dsh"]["home"], str(self.path.parent / "dsh-home"))
        self.assertEqual(loaded["dsh"]["patches"], [str(self.path.parent / "account.yml")])
        self.assertEqual(loaded["config_path"], str(self.path))

    def test_shared_secrets_resolve_against_config_and_stay_outside_model_workspaces(self):
        shared = self.root / "private" / "secrets.local.json"
        shared.parent.mkdir()
        shared.write_text(json.dumps({"ANTHROPIC_API_KEY": "synthetic-anthropic-key"}), encoding="utf-8")
        self.value["secrets_file"] = "../private/secrets.local.json"
        loaded = config.load_config(self.save())
        self.assertEqual(loaded["secrets_file"], str(shared))
        self.assertEqual(read_secret("ANTHROPIC_API_KEY", config_path=self.path), "synthetic-anthropic-key")
        for location in (self.project / "secrets.local.json", self.path.parent / "runtime/workspace/secrets.local.json",
                         self.path.parent / "runtime/private/secrets.local.json"):
            self.value["secrets_file"] = str(location)
            with self.subTest(location=location), self.assertRaisesRegex(ValueError, "工作目录之外"):
                config.load_config(self.save())

    def test_large_source_config_routes_to_a_bounded_secrets_file(self):
        self.value.update(secrets_file="../secrets.local.json", goal="项目需求" * 30000)
        config.load_config(self.save())
        shared = self.root / "secrets.local.json"
        shared.write_text(json.dumps({"CODEX_API_KEY": "synthetic-codex-key"}), encoding="utf-8")
        self.assertEqual(read_secret("CODEX_API_KEY", config_path=self.path), "synthetic-codex-key")
        shared.write_text(json.dumps({"CODEX_API_KEY": "x" * 65537}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "本地密钥文件"):
            read_secret("CODEX_API_KEY", config_path=self.path)

    def test_secret_errors_hide_credentials(self):
        shared = self.root / "secrets.local.json"
        shared.write_text('{"CODEX_API_KEY": "synthetic-secret"', encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            read_secret("CODEX_API_KEY", secrets_path=shared)
        self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_api_endpoint_validation_excludes_embedded_secrets_and_controls(self):
        self.assertEqual(validate_api_base_url("https://api.example.test/v1/"), "https://api.example.test/v1")
        for value in ("http://api.example.test", "https://user:synthetic-secret@api.example.test",
                      "https://api.example.test?token=synthetic-secret", "https://api.example.test#fragment",
                      "https://api.example.test\n", "https://api.example.test:invalid", "https://"):
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                validate_api_base_url(value)
            self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_claude_auth_mode_is_explicit_and_validated(self):
        self.value.update(planning_provider="claude", claude={"command": ["claude.exe"], "auth_mode": "api_key"})
        self.assertEqual(config.load_config(self.save())["claude"]["auth_mode"], "api_key")
        self.value["claude"]["auth_mode"] = "other"
        with self.assertRaisesRegex(ValueError, "claude.auth_mode"):
            config.load_config(self.save())

    def test_explicit_models_and_reasoning_survive_loading(self):
        self.value["codex"].update(model="gpt-6-astra", reasoning_effort="high")
        self.value["dsh"].update(model="deepseek-flash", reasoning_effort="max")
        loaded = config.load_config(self.save())
        self.assertEqual(loaded["codex"]["reasoning_effort"], "high")
        self.assertEqual(loaded["dsh"]["reasoning_effort"], "max")

    def test_invalid_models_and_reasoning_fail_before_scheduling(self):
        for role, field, value in (("codex", "model", ""), ("dsh", "model", []),
                                   ("codex", "reasoning_effort", "maximum"),
                                   ("dsh", "reasoning_effort", "medium"),
                                   ("dsh", "reasoning_effort", None)):
            candidate = copy.deepcopy(self.value)
            candidate[role][field] = value
            with self.subTest(role=role, field=field, value=value), self.assertRaises(ValueError):
                config.load_config(self.save(candidate))

    def test_codex_subscription_fallback_requires_explicit_api_mode(self):
        self.value["codex"]["subscription_fallback"] = True
        with self.assertRaisesRegex(ValueError, "subscription_fallback"):
            config.load_config(self.save())
        self.value["codex"]["auth_mode"] = "api_key"
        self.assertTrue(config.load_config(self.save())["codex"]["subscription_fallback"])
        self.value["codex"]["subscription_fallback"] = "true"
        with self.assertRaisesRegex(ValueError, "subscription_fallback"):
            config.load_config(self.save())

    def test_project_runtime_may_not_overlap_in_either_direction(self):
        for runtime in (self.project, self.project / ".runtime", self.project.parent):
            value = copy.deepcopy(self.value)
            value["runtime_dir"] = str(runtime)
            with self.subTest(runtime=runtime), self.assertRaisesRegex(ValueError, "相互包含"):
                config.load_config(self.save(value))

    def test_goal_and_managed_branch_are_required(self):
        for key, value in (("goal", "  "), ("branch", "main"), ("version", 2)):
            candidate = dict(self.value, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                config.load_config(self.save(candidate))

    def test_checks_require_unique_identity_valid_argv_and_finite_timeout(self):
        invalid = (
            [{"id": "unit", "argv": []}],
            [{"id": "unit", "argv": ["python\0.exe"]}],
            [{"id": "unit", "argv": ["python.exe"], "timeout_seconds": True}],
            [{"id": "unit", "argv": ["python.exe"], "timeout_seconds": float("nan")}],
            [{"id": "same", "argv": ["python.exe"]}, {"id": "same", "argv": ["python.exe"]}],
        )
        for checks in invalid:
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                config.load_config(self.save(dict(self.value, checks=checks)))

    def test_close_policy_and_loop_limits_are_validated(self):
        for key, value in (("close_policy", "kill-whatever"), ("max_reworks", -1),
                           ("max_failures", 0), ("poll_seconds", 0), ("step_timeout_seconds", True),
                           ("push", "yes"), ("task_prefix", "../invalid")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                config.load_config(self.save(dict(self.value, **{key: value})))

    def test_schedule_validation_runs_when_loading_config(self):
        with self.assertRaisesRegex(ValueError, "Asia/Shanghai"):
            config.load_config(self.save(dict(self.value, schedule={"timezone": "UTC"})))

    def test_optional_publication_preserves_legacy_push_behavior(self):
        for push in (False, True):
            loaded = config.load_config(self.save(dict(self.value, push=push)))
            self.assertNotIn("publication", loaded)
            self.assertEqual(loaded["push"], push)

    def test_milestone_publication_keeps_complete_goal_and_push_false(self):
        publication = {"mode": "milestone", "milestone_id": "M2", "goal": "完成 M2 全部范围，补齐运行证据并通过阶段检查。"}
        loaded = config.load_config(self.save(dict(self.value, publication=publication, push=False)))
        self.assertEqual(loaded["publication"], publication)
        self.assertFalse(loaded["push"])
        publication["milestone_id"] = "M" + "2" * 59
        self.assertEqual(config.load_config(self.save(dict(self.value, publication=publication)))["publication"], publication)

    def test_milestone_publication_rejects_unknown_missing_and_invalid_fields(self):
        valid = {"mode": "milestone", "milestone_id": "M2", "goal": "完整阶段范围与完成条件"}
        invalid = [None, [], {}, {"mode": "milestone"}, dict(valid, unexpected=True),
                   dict(valid, mode="task"), dict(valid, mode=None), dict(valid, milestone_id=""),
                   dict(valid, milestone_id="M" * 61), dict(valid, milestone_id="../M2"),
                   dict(valid, milestone_id="-M2"), dict(valid, milestone_id="M2 with space"),
                   dict(valid, milestone_id=2), dict(valid, goal=" \n\t"), dict(valid, goal=[])]
        for publication in invalid:
            with self.subTest(publication=publication), self.assertRaises(ValueError):
                config.load_config(self.save(dict(self.value, publication=publication)))

    def test_init_does_not_overwrite_existing_config_or_invoke_git(self):
        self.path.write_text("keep existing config", encoding="utf-8")
        with patch.object(config.gitops, "head") as head:
            with self.assertRaisesRegex(ValueError, "已存在"):
                config.init_config(self.path, self.project, "目标")
        head.assert_not_called()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "keep existing config")

    def test_init_creates_disabled_push_configuration_and_private_calendar_copy(self):
        template = self.root / "tool" / "unattended" / "calendars" / "china-2026.json"
        template.parent.mkdir(parents=True)
        template.write_text(json.dumps({"years": [2026], "holidays": [], "workdays": [], "overrides": {}}), encoding="utf-8")
        with patch.object(config, "ROOT", self.root / "tool"), patch.object(config.gitops, "head", return_value="a" * 40):
            loaded = config.init_config(self.path, self.project, "  明确目标  ")
        self.assertEqual(loaded["goal"], "明确目标")
        self.assertFalse(loaded["push"])
        self.assertNotIn("publication", loaded)
        self.assertEqual(loaded["checks"], [])
        self.assertTrue(loaded["branch"].startswith("unattended/"))
        self.assertEqual(loaded["schedule"]["weekday_start"], "18:00")
        self.assertEqual(loaded["schedule"]["weekday_end"], "08:00")
        copied = Path(loaded["schedule"]["holidays_file"])
        self.assertEqual(copied.parent, self.path.parent)
        self.assertEqual(json.loads(copied.read_text(encoding="utf-8")), json.loads(template.read_text(encoding="utf-8")))

    def test_init_preserves_explicit_milestone_publication(self):
        template = self.root / "tool" / "unattended" / "calendars" / "china-2026.json"
        template.parent.mkdir(parents=True)
        template.write_text(json.dumps({"years": [2026], "holidays": [], "workdays": [], "overrides": {}}), encoding="utf-8")
        publication = {"mode": "milestone", "milestone_id": "M2", "goal": "按已确认 Roadmap 完成并验收 M2 全部范围；不进入 M3。"}
        with patch.object(config, "ROOT", self.root / "tool"), patch.object(config.gitops, "head", return_value="a" * 40):
            loaded = config.init_config(self.path, self.project, "项目总体需求", publication=publication)
        self.assertEqual(loaded["publication"], publication)
        self.assertFalse(loaded["push"])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["publication"], publication)

    def test_doctor_requires_independent_checks_and_never_runs_a_model(self):
        candidate = config.load_config(self.save())
        calendar_path = self.path.parent / "calendar.json"
        current_year = datetime.now(timezone.utc).year
        calendar_path.write_text(json.dumps({"years": [current_year], "holidays": [], "workdays": [], "overrides": {}}), encoding="utf-8")
        candidate["schedule"]["holidays_file"] = str(calendar_path)
        Path(candidate["dsh"]["home"]).mkdir()
        Path(candidate["dsh"]["patches"][0]).write_text("[]", encoding="utf-8")
        with patch.object(config.gitops, "head", return_value="a" * 40), \
             patch.object(config.gitops, "git", return_value="set-identity"), \
             patch.object(config.shutil, "which", return_value="native.exe"):
            ready = config.doctor(candidate)
            self.assertTrue(ready["ready"])
            self.assertEqual(ready["status"], "ready")
            calendar_path.write_text(json.dumps({"years": [current_year - 1], "holidays": [], "workdays": [], "overrides": {}}), encoding="utf-8")
            uncovered = config.doctor(candidate)
            self.assertFalse(uncovered["ready"])
            self.assertFalse(uncovered["checks"]["calendar"]["calendar_covered"])
            calendar_path.write_text(json.dumps({"years": [current_year], "holidays": [], "workdays": [], "overrides": {}}), encoding="utf-8")
            candidate["checks"] = []
            missing = config.doctor(candidate)
        self.assertFalse(missing["ready"])
        self.assertFalse(missing["checks"]["independent_checks_configured"])

    def test_doctor_reports_missing_cli_without_launching_it(self):
        candidate = config.load_config(self.save())
        candidate["codex"]["command"] = [str(self.root / "missing-codex.exe")]
        candidate["dsh"] = {"command": [str(self.root / "missing-dsh.exe")]}
        with patch.object(config.gitops, "head", return_value="a" * 40), \
             patch.object(config.gitops, "git", return_value="identity"), \
             patch.object(config.shutil, "which", return_value=None):
            result = config.doctor(candidate)
        self.assertFalse(result["ready"])
        self.assertFalse(result["checks"]["codex_available"])
        self.assertFalse(result["checks"]["dsh_available"])

    def test_doctor_checks_shared_claude_api_credentials_without_exposing_them(self):
        self.value.update(planning_provider="claude", coding_provider="claude",
                          claude={"command": ["claude.exe"], "auth_mode": "api_key"},
                          secrets_file="../secrets.local.json")
        candidate = config.load_config(self.save())
        shared = Path(candidate["secrets_file"])
        with patch.object(config.gitops, "head", return_value="a" * 40), \
             patch.object(config.gitops, "git", return_value="identity"), \
             patch.object(config.shutil, "which", return_value="native.exe"), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_BASE_URL": ""}):
            self.assertFalse(config.doctor(candidate)["checks"]["claude_api_key_configured"])
            for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
                with self.subTest(name=name):
                    shared.write_text(json.dumps({name: "synthetic-anthropic-key"}), encoding="utf-8")
                    result = config.doctor(candidate)
                    self.assertTrue(result["checks"]["claude_api_key_configured"])
                    self.assertNotIn("synthetic-anthropic-key", json.dumps(result))
            shared.write_text(json.dumps({"ANTHROPIC_AUTH_TOKEN": "synthetic-anthropic-key",
                                          "ANTHROPIC_BASE_URL": "https://host.invalid?token=private"}), encoding="utf-8")
            self.assertFalse(config.doctor(candidate)["checks"]["claude_api_key_configured"])


if __name__ == "__main__":
    unittest.main()
