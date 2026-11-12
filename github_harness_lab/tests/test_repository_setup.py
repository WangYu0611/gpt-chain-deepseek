import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.repository_setup import (MAX_CONFIG_BYTES, RepositorySettings, build_setup_request,
                                          check_environment, export_setup_bundle, load_settings,
                                          normalize_repository, save_settings, validate_settings)


class RepositorySetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def read_saved(self):
        return json.loads((self.root / "config.local.json").read_text(encoding="utf-8"))

    def test_supported_repository_inputs_are_canonicalized(self):
        for value in ("Example/repo", "  Example/repo  ", "https://github.com/Example/repo",
                      "https://github.com/Example/repo/", "https://github.com/Example/repo.git/",
                      "git@github.com:Example/repo.git"):
            with self.subTest(value=value):
                self.assertEqual(normalize_repository(value), "Example/repo")

    def test_credential_urls_external_hosts_and_nested_paths_are_rejected(self):
        values = ["https://user:secret@github.com/a/b", "https://github.com.evil.test/a/b",
                  "http://github.com/a/b", "https://github.com:443/a/b", "https://example.com/a/b",
                  "https://github.com/a/b/issues/1", "https://github.com/a/b?token=secret",
                  "https://github.com/a/b#readme", "https://github.com/a/b?", "a/b\n", "a/b\x00", "a/b\x85",
                  "a/../b", "a/..", "../b", "a/b\\c", "git@evil.test:a/b.git", "a//b",
                  "https://github.com/a/%2e%2e", "https://github.com//a/b", "-owner/repo"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_repository(value)

    def test_settings_reject_invalid_branch_actor_source_and_goal(self):
        invalid = [dict(default_branch="main.lock"), dict(default_branch="a..b"), dict(default_branch="a@{b"),
                   dict(default_branch="main~1"), dict(default_branch="a/.hidden"), dict(default_branch="a//b"),
                   dict(trusted_actor=""), dict(trusted_actor="user[bot]"), dict(source_repository="relative"),
                   dict(source_repository=str(self.root / "missing")), dict(project_goal="x" * 6001)]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_settings(RepositorySettings(**change))
        good = validate_settings(RepositorySettings(default_branch="release/v1", source_repository=str(self.root), project_goal="目标\n第二行"))
        self.assertEqual(good.source_repository, str(self.root.resolve()))

    def test_save_is_disabled_preserves_advanced_config_and_does_not_copy_authorization(self):
        original = {"version": 1, "repository": "Old/project", "trusted_actors": ["Old"], "live_enabled": True,
                    "runtime_dir": "old-runtime", "authorization_file": "old-auth.json",
                    "limits": {"run_timeout_seconds": 75, "max_budget_usd": 9},
                    "dsh": {"secret_for_test": "never-export-this"}, "custom": {"keep": [1, 2]}}
        (self.root / "config.local.json").write_text(json.dumps(original), encoding="utf-8-sig")
        (self.root / "old-auth.json").write_text("do not read or copy", encoding="utf-8")
        result = save_settings(self.root, RepositorySettings(repository="New/project", trusted_actor="New"))
        saved = self.read_saved()
        self.assertEqual(result, self.root / "config.local.json")
        self.assertFalse(saved["live_enabled"])
        self.assertEqual(saved["dsh"], original["dsh"])
        self.assertEqual(saved["limits"], original["limits"])
        self.assertEqual(saved["custom"], original["custom"])
        self.assertEqual(saved["setup"]["status"], "needs_deployment")
        self.assertNotEqual(saved["authorization_file"], original["authorization_file"])
        self.assertFalse((self.root / ".private").exists())
        self.assertFalse((self.root / ".runtime").exists())
        self.assertEqual((self.root / "old-auth.json").read_text(), "do not read or copy")

    def test_repository_state_paths_are_separate_and_case_stable(self):
        paths = []
        for repo in ("First/project", "Second/project", "FIRST/PROJECT"):
            save_settings(self.root, RepositorySettings(repository=repo))
            data = self.read_saved()
            paths.append((data["runtime_dir"], data["authorization_file"]))
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual(paths[0], paths[2])
        expected = hashlib.sha256(b"first/project").hexdigest()[:12]
        self.assertEqual(paths[0][0], ".runtime/repositories/" + expected)

    def test_save_roundtrip_and_generic_loader_compatibility(self):
        from lab_harness.worker import load_config
        settings = RepositorySettings(repository="User/work", trusted_actor="User", project_goal="中文目标")
        self.assertEqual(load_settings(self.root), RepositorySettings())
        path = save_settings(self.root, settings)
        self.assertEqual(load_settings(self.root), settings)
        config = load_config(path)
        self.assertFalse(config["live_enabled"])
        self.assertEqual(config["repository"], "User/work")

    def test_malformed_or_oversized_configuration_is_never_overwritten(self):
        path = self.root / "config.local.json"
        malformed = [b"{bad", b"[]", b'{"version":2}', b'{"version":1,"setup":false}',
                     b'{"version":1,"trusted_actors":"x"}', b'{"version":1,"trusted_actors":null}',
                     b'{"version":1,"version":1}',
                     b'{"version":1,"limits":{"max_budget_usd":NaN}}', b" " * (MAX_CONFIG_BYTES + 1)]
        for content in malformed:
            path.write_bytes(content)
            with self.subTest(size=len(content)):
                with self.assertRaises(ValueError):
                    load_settings(self.root)
                with self.assertRaises(ValueError):
                    save_settings(self.root, RepositorySettings())
                self.assertEqual(path.read_bytes(), content)
                self.assertFalse(list(self.root.glob("*.tmp")))

    def test_atomic_replace_failure_keeps_previous_config(self):
        save_settings(self.root, RepositorySettings())
        path = self.root / "config.local.json"
        before = path.read_bytes()
        with patch("lab_harness.repository_setup.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                save_settings(self.root, RepositorySettings(repository="New/repo"))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_export_has_only_whitelisted_snapshot_and_disabled_generic_bundle(self):
        save_settings(self.root, RepositorySettings())
        config = self.read_saved()
        config.update(secrets={"token": "DO_NOT_EXPORT"}, dsh={"credentials": "DO_NOT_EXPORT"},
                      authorization={"approved": True, "marker": "DO_NOT_EXPORT"})
        config["setup"]["private_token"] = "DO_NOT_EXPORT"
        (self.root / "config.local.json").write_text(json.dumps(config), encoding="utf-8")
        settings = RepositorySettings(repository="User/repo", trusted_actor="User", project_goal="写一份文档")
        folder = export_setup_bundle(self.root, settings)
        self.assertEqual({p.name for p in folder.iterdir()}, {"config.snapshot.json", "request.txt", "README.txt", "generic"})
        snapshot = json.loads((folder / "config.snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(set(snapshot), {"version", "repository", "default_branch", "trusted_actors", "source_repository", "live_enabled", "setup"})
        for file in (p for p in folder.rglob("*") if p.is_file()):
            self.assertNotIn("DO_NOT_EXPORT", file.read_text(encoding="utf-8"))
        activation = json.loads((folder / "generic/local/activation.example.json").read_text(encoding="utf-8"))
        self.assertFalse(activation["enabled"])
        self.assertIsNone(activation["nonce"])
        self.assertTrue((folder / "generic/repository/.github/workflows/harness-documents.yml.disabled").is_file())
        self.assertFalse(list((folder / "generic/repository/.github/workflows").glob("*.yml")))
        self.assertFalse(snapshot["live_enabled"])
        self.assertIn("两个独立网页对话", (folder / "request.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.root / ".private").exists())
        self.assertNotEqual(folder, export_setup_bundle(self.root, settings))

    def test_environment_is_read_only_and_checks_credential_existence_only(self):
        fake_home = self.root / "user"
        credential = fake_home / ".dsh" / ".credentials.yaml"
        credential.parent.mkdir(parents=True)
        credential.write_text("SECRET_NOT_TO_READ", encoding="utf-8")
        local = self.root / "local"
        harness = local / "Programs" / "DeepSeek Harness"
        (harness / "resources").mkdir(parents=True)
        (harness / "DeepSeek Harness.exe").touch()
        (harness / "resources" / "app.asar").touch()
        before = {p.relative_to(self.root) for p in self.root.rglob("*")}
        with patch("pathlib.Path.home", return_value=fake_home), patch.dict("os.environ", {"LOCALAPPDATA": str(local)}), \
                patch("pathlib.Path.open", side_effect=AssertionError("must not read files")), \
                patch("subprocess.Popen", side_effect=AssertionError("no processes")), \
                patch("socket.create_connection", side_effect=AssertionError("no network")):
            checks = check_environment(self.root, RepositorySettings())
        self.assertEqual({p.relative_to(self.root) for p in self.root.rglob("*")}, before)
        self.assertTrue(all(item["status"] in {"ok", "warn", "info"} for item in checks))
        self.assertEqual(next(x for x in checks if x["name"] == "官方 DeepSeek Harness")["status"], "ok")
        self.assertEqual(next(x for x in checks if x["name"] == "Harness 登录记录")["status"], "ok")
        self.assertNotIn("SECRET_NOT_TO_READ", json.dumps(checks))

    def test_save_and_export_never_start_process_or_network(self):
        with patch("subprocess.Popen", side_effect=AssertionError("no processes")), \
                patch("socket.create_connection", side_effect=AssertionError("no network")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            save_settings(self.root, RepositorySettings())
            folder = export_setup_bundle(self.root, RepositorySettings())
        self.assertTrue(folder.is_dir())
        self.assertEqual({p.name for p in self.root.iterdir()}, {"config.local.json", ".runtime"})

    def test_setup_request_contains_data_and_explicit_no_sending_boundary(self):
        request = build_setup_request(RepositorySettings(repository="User/repo", trusted_actor="User", project_goal="目标\n第二行"), self.root / "config.local.json")
        self.assertIn('"repository": "User/repo"', request)
        self.assertIn("等我明确批准", request)
        self.assertIn("不要创建或编辑 Issue/PR", request)
        self.assertIn("不要复用测试仓库的固定 Issue 编号", request)


if __name__ == "__main__":
    unittest.main()
