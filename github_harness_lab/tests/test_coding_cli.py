"""CLI tests with temporary local state and blocked/injected external boundaries."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness import coding
from lab_harness.coding_contract import PUBLIC_BINDING, profile_digest
from lab_harness.project_profile import repository_namespace
from tests.coding_fixtures import artifact_fixture, fixture, make_profile


class CodingCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-coding-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile, self.activation, self.event, self.env, _ = fixture()
        self.profile_path = self.save("profile.json", self.profile)
        self.activation_path = self.save("activation.json", self.activation)
        self.event_path = self.save("event.json", self.event)
        self.artifact = artifact_fixture(self.profile, self.activation, self.event, self.env)
        self.artifact_path = self.save("artifact.json", self.artifact)
        self.env.update(EXPECTED_PROFILE_SHA256=profile_digest(self.profile),
                        EXPECTED_CONTROLLER_SHA256=self.activation["controller_sha256"],
                        USERPROFILE=str(self.root / "fake-user"), LOCALAPPDATA=str(self.root / "fake-local-appdata"))

    def save(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def args(self, command, *, execute=False):
        args = [command, "--profile", str(self.profile_path)]
        if command == "generate":
            args += ["--activation", str(self.activation_path), "--event", str(self.event_path)]
        elif command == "publish":
            args += ["--artifact", str(self.artifact_path)]
        if execute:
            args.append("--execute")
        return args

    def call(self, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(coding.main(args), 0)
        return json.loads(output.getvalue())

    def state(self, profile=None):
        return self.root / ".runtime/coding" / repository_namespace((profile or self.profile)["repository"])

    def test_inspect_and_dryrun_do_not_read_activation_artifact_or_create_client(self):
        with patch("lab_harness.coding.read_object", side_effect=AssertionError("Private/event/artifact read forbidden")), \
             patch("lab_harness.github.GitHubClient", side_effect=AssertionError("Network client forbidden")), \
             patch("lab_harness.coding_dsh.run", side_effect=AssertionError("Provider forbidden")), \
             patch("lab_harness.coding._local_provider", side_effect=AssertionError("Provider inspection forbidden")):
            inspected = self.call(self.args("inspect"))
            self.assertEqual(inspected["status"], "LOCAL_PROFILE_ONLY")
            self.assertEqual(inspected["profile_sha256"], profile_digest(self.profile))
            self.assertFalse(inspected["deployed"])
            self.assertEqual(inspected["limits"]["max_tasks"], None)
            for command in ("generate", "publish"):
                # Nonexistent inputs show that dryrun neither reads nor requires them.
                args = self.args(command)
                args[-1] = str(self.root / "absent.json")
                result = self.call(args)
                self.assertEqual(result["status"], "DRY_RUN")
                self.assertEqual(result["operation"], command)
                self.assertFalse(result["sent"])
                self.assertEqual((result["network_calls"], result["model_calls"]), (0, 0))

    def test_identity_mismatch_fails_before_provider_or_network_client(self):
        for kind in ("profile", "expected-controller", "installed-controller", "activation-profile"):
            env, activation = copy.deepcopy(self.env), copy.deepcopy(self.activation)
            code_hash = activation["controller_sha256"]
            if kind == "profile": env["EXPECTED_PROFILE_SHA256"] = "f" * 64
            elif kind == "expected-controller": env["EXPECTED_CONTROLLER_SHA256"] = "f" * 64
            elif kind == "installed-controller": code_hash = "f" * 64
            else: activation["profile_sha256"] = "f" * 64
            self.save("activation.json", activation)
            with self.subTest(kind=kind), patch.dict("os.environ", env, clear=True), \
                 patch("lab_harness.coding_bundle.controller_digest", return_value=code_hash), \
                 patch("lab_harness.github.GitHubClient", side_effect=AssertionError("Network forbidden")), \
                 patch("lab_harness.coding._local_provider", side_effect=AssertionError("Provider inspection forbidden")), \
                 self.assertRaises(ValueError):
                coding.main(self.args("generate", execute=True))

    def test_publish_identity_is_also_checked_before_client_creation(self):
        with patch.dict("os.environ", self.env, clear=True), \
             patch("lab_harness.coding_bundle.controller_digest", return_value="f" * 64), \
             patch("lab_harness.github.GitHubClient", side_effect=AssertionError("Network forbidden")), \
             self.assertRaisesRegex(ValueError, "differs"):
            coding.main(self.args("publish", execute=True))

    def test_status_and_stop_are_local_repository_scoped_and_idempotent(self):
        with patch("lab_harness.coding.__file__", str(self.root / "lab_harness/coding.py")), \
             patch("lab_harness.coding.read_object", side_effect=AssertionError("Private data read forbidden")), \
             patch("lab_harness.github.GitHubClient", side_effect=AssertionError("Network forbidden")), \
             patch("lab_harness.coding_dsh.run", side_effect=AssertionError("Provider forbidden")):
            initial = self.call(self.args("status"))
            self.assertFalse(initial["local_stop"])
            self.assertFalse(initial["lease_present"])
            self.assertEqual(initial["claims"], 0)
            self.assertFalse(initial["remote_status_checked"])
            self.assertFalse(self.state().exists())
            for _ in range(2):
                stopped = self.call(self.args("stop"))
                self.assertEqual(stopped["status"], "LOCAL_GENERATION_STOPPED")
                self.assertEqual(Path(stopped["path"]), self.state() / "STOP")
                self.assertIn("control Issue", stopped["note"])
            for number in (1, 3):
                folder = self.state() / self.activation["nonce"] / str(number)
                folder.mkdir(parents=True)
                (folder / "claim.json").write_text("{}", encoding="utf-8")
            (self.state() / "lease.json").write_text("{}", encoding="utf-8")
            status = self.call(self.args("status"))
            self.assertTrue(status["local_stop"])
            self.assertTrue(status["lease_present"])
            self.assertEqual(status["claims"], 2)
            other_profile = make_profile("Other/beta", actor="Other")
            other = self.save("other-profile.json", other_profile)
            independent = self.call(["status", "--profile", str(other)])
            self.assertFalse(independent["local_stop"])
            self.assertEqual(independent["claims"], 0)
            self.assertFalse(self.state(other_profile).exists())

    def test_generation_gate_allows_only_reads_and_rechecks_local_stop_and_activation(self):
        output = self.root / "result.json"
        github_output = self.root / "github-output.txt"
        env = self.env | {"GITHUB_OUTPUT": str(github_output)}
        with patch("lab_harness.coding.__file__", str(self.root / "lab_harness/coding.py")), \
             patch.dict("os.environ", env, clear=True), \
             patch("lab_harness.coding_bundle.controller_digest", return_value=self.activation["controller_sha256"]), \
             patch("lab_harness.coding._local_provider") as provider_config, \
             patch("lab_harness.github.GitHubClient") as client_constructor, \
             patch("lab_harness.coding_engine.generate_task", return_value=(self.artifact, output)) as engine, \
             patch("lab_harness.coding_dsh.run") as provider:
            generated = self.call(self.args("generate", execute=True))
            self.assertEqual(generated["status"], "GENERATED")
            self.assertEqual(generated["model_requests"], 1)
            provider.assert_not_called()
            provider_config.assert_called_once_with(self.activation)
            self.assertIs(engine.call_args.kwargs["generate"], provider)
            self.assertEqual(engine.call_args.kwargs["root"], self.root)
            self.assertEqual(client_constructor.call_args.kwargs["token_env"], "GH_HARNESS_TOKEN")
            gate = client_constructor.call_args.kwargs["authorization_gate"]
            self.assertTrue(gate("github-read"))
            self.assertFalse(gate("github-write"))
            changed = copy.deepcopy(self.activation)
            changed["dsh"]["changed"] = True
            self.save("activation.json", changed)
            with self.assertRaisesRegex(ValueError, "changed"):
                gate("github-read")
            self.save("activation.json", self.activation)
            self.state().mkdir(parents=True)
            (self.state() / "STOP").write_text("stop", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "stopped"):
                gate("github-read")
        self.assertEqual(github_output.read_text(encoding="utf-8"), "artifact_path=" + str(output.resolve()) + "\n")

    def test_publish_reconstructs_only_public_activation_without_loading_private_provider(self):
        journal = self.root / "journal"
        with patch("lab_harness.coding.__file__", str(self.root / "lab_harness/coding.py")), \
             patch.dict("os.environ", self.env, clear=True), \
             patch("lab_harness.coding_bundle.controller_digest", return_value=self.activation["controller_sha256"]), \
             patch("lab_harness.coding._local_provider", side_effect=AssertionError("Provider inspection forbidden")), \
             patch("lab_harness.github.GitHubClient") as client_constructor, \
             patch("lab_harness.coding_publisher.publish_task", return_value={"status": "OFFLINE_PUBLISHED"}) as publisher:
            result = self.call(self.args("publish", execute=True) + ["--journal-root", str(journal)])
            self.assertEqual(result["status"], "OFFLINE_PUBLISHED")
            actual_activation = publisher.call_args.args[2]
            self.assertEqual(set(actual_activation), set(PUBLIC_BINDING) | {"schema", "enabled"})
            self.assertNotIn("dsh", actual_activation)
            self.assertEqual(actual_activation["expires_at"], None)
            self.assertEqual(publisher.call_args.kwargs["journal_root"], journal)
            self.assertEqual(client_constructor.call_args.args[0], self.profile["repository"])

    def test_generation_rejects_non_ascii_controller_before_provider_or_network(self):
        with patch("lab_harness.coding.__file__", str(self.root / "非ASCII/lab_harness/coding.py")), \
             patch.dict("os.environ", self.env, clear=True), \
             patch("lab_harness.coding_bundle.controller_digest", return_value=self.activation["controller_sha256"]), \
             patch("lab_harness.coding._local_provider", side_effect=AssertionError("Provider inspection forbidden")), \
             patch("lab_harness.github.GitHubClient", side_effect=AssertionError("Network forbidden")), \
             self.assertRaisesRegex(ValueError, "ASCII"):
            coding.main(self.args("generate", execute=True))

    def test_unreviewed_desktop_installation_cannot_start(self):
        dsh = {"desktop_install": str(self.root / "unreviewed-program"),
               "credentials_path": str(self.root / "unreviewed-login"), "verified_version": "0.2.1-rc.3",
               "model": "deepseek-flash", "timeout_seconds": 120,
               "usage_authorization": {"source": "explicit_user_request", "scope": "continuous_project_code",
                                       "user_text": "Synthetic offline fixture only"}}
        self.save("activation.json", self.activation | {"dsh": dsh})
        with patch("lab_harness.coding.__file__", str(self.root / "lab_harness/coding.py")), \
             patch.dict("os.environ", self.env, clear=True), \
             patch("lab_harness.coding_bundle.controller_digest", return_value=self.activation["controller_sha256"]), \
             patch("lab_harness.github.GitHubClient", side_effect=AssertionError("Network forbidden")), \
             patch("lab_harness.coding_dsh.run", side_effect=AssertionError("Provider forbidden")), \
             self.assertRaisesRegex(ValueError, "verified official"):
            coding.main(self.args("generate", execute=True))


if __name__ == "__main__":
    unittest.main()
