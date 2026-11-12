import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.portable import main
from lab_harness.project_profile import make_profile, profile_digest
from tests.portable_fixtures import fixture


class PortableCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile, self.activation, self.event, self.env, _ = fixture()
        self.profile_path = self.root / "profile.json"
        self.profile_path.write_text(json.dumps(self.profile), encoding="utf-8")

    def call(self, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(args), 0)
        return json.loads(output.getvalue())

    def test_inspect_and_dry_run_do_not_create_client_or_provider(self):
        with patch("lab_harness.github.GitHubClient", side_effect=AssertionError("network client forbidden")), \
             patch("lab_harness.bounded_dsh.run", side_effect=AssertionError("provider forbidden")):
            inspected = self.call(["inspect", "--profile", str(self.profile_path)])
            self.assertFalse(inspected["live_enabled"])
            self.assertEqual(inspected["profile_sha256"], profile_digest(self.profile))
            for command in ("generate", "publish"):
                args = [command, "--profile", str(self.profile_path)]
                args += (["--activation", str(self.root / "missing-private.json"), "--event", str(self.root / "missing-event.json")]
                         if command == "generate" else ["--artifact", str(self.root / "missing-artifact.json")])
                result = self.call(args)
                self.assertEqual(result["status"], "DRY_RUN")
                self.assertFalse(result["sent"])
                self.assertEqual(result["network_calls"], 0)

    def test_wrong_installed_code_identity_blocks_before_client_creation(self):
        activation = self.root / "activation.json"
        event = self.root / "event.json"
        activation.write_text(json.dumps(self.activation), encoding="utf-8")
        event.write_text(json.dumps(self.event), encoding="utf-8")
        with patch.dict("os.environ", self.env, clear=True), \
             patch("lab_harness.github.GitHubClient", side_effect=AssertionError("network client forbidden")), \
             self.assertRaisesRegex(ValueError, "differs"):
            main(["generate", "--profile", str(self.profile_path), "--activation", str(activation),
                  "--event", str(event), "--execute"])

    def test_profile_creation_uses_selected_config_and_never_arms(self):
        config = self.root / "config.local.json"
        config.write_text(json.dumps({"version": 1, "repository": "Other/beta", "default_branch": "develop",
                                      "trusted_actors": ["Other"], "source_repository": ""}), encoding="utf-8")
        destination = self.root / "new-profile.json"
        self.call(["profile", "--from-config", str(config), "--output", str(destination)])
        saved = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(saved, make_profile("Other/beta", "develop", "Other"))
        self.assertFalse((self.root / ".private").exists())
        with self.assertRaises(FileExistsError):
            main(["profile", "--from-config", str(config), "--output", str(destination)])

    def test_missing_configuration_cannot_silently_use_old_default_repository(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            main(["profile", "--from-config", str(self.root / "config.local.json"),
                  "--output", str(self.root / "new-profile.json")])
        self.assertFalse((self.root / "new-profile.json").exists())

    def test_nonofficial_provider_cannot_start_from_an_enabled_local_activation(self):
        activation = self.root / "activation.json"
        event = self.root / "event.json"
        self.activation["dsh"] = {
            "desktop_install": str(self.root / "unreviewed-program"),
            "credentials_path": str(self.root / "unreviewed-login"),
            "model": "deepseek-flash", "verified_version": "0.2.1-rc.3",
            "timeout_seconds": 120,
            "usage_authorization": {"source": "explicit_user_request", "scope": "single_issue_smoke",
                                    "user_text": "Synthetic offline test only"},
        }
        activation.write_text(json.dumps(self.activation), encoding="utf-8")
        event.write_text(json.dumps(self.event), encoding="utf-8")
        with patch("lab_harness.portable._identity"), \
             patch("lab_harness.github.GitHubClient", side_effect=AssertionError("network client forbidden")), \
             patch("lab_harness.bounded_dsh.run", side_effect=AssertionError("provider forbidden")), \
             self.assertRaisesRegex(ValueError, "verified official"):
            main(["generate", "--profile", str(self.profile_path), "--activation", str(activation),
                  "--event", str(event), "--execute"])

    def test_unsupported_transport_branch_is_rejected_when_creating_a_profile(self):
        for branch in ("开发", "release'quoted"):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                make_profile("Example/alpha", branch, "Example")

    def test_bundle_command_creates_only_disabled_review_files(self):
        with patch("lab_harness.github.GitHubClient", side_effect=AssertionError("network forbidden")):
            result = self.call(["bundle", "--profile", str(self.profile_path), "--output", str(self.root / "bundle")])
        self.assertFalse(result["deployed"])
        activation = json.loads((self.root / "bundle/local/activation.example.json").read_text(encoding="utf-8"))
        self.assertFalse(activation["enabled"])
        self.assertIsNone(activation["arm_comment_id"])
        self.assertFalse(list((self.root / "bundle/repository/.github/workflows").glob("*.yml")))


if __name__ == "__main__":
    unittest.main()
