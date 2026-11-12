"""Adversarial local checks for trust, scope, secrets and the closed live gate."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness import gitops
from lab_harness.policy import PolicyError, canonical_digest, check_changes, parse_event, require_live, safe_path
from lab_harness.worker import Worker


def task_fixture():
    return {
        "task_id": "TASK-1", "allowed_paths": ["src"],
        "forbidden_paths": ["src/private"], "requirements": ["Implement arithmetic"],
        "acceptance": [{"id": "A1", "criterion": "returns the expected number"}],
        "evidence": [{"id": "E1", "requirement": "actual test exit code"}],
        "test_ids": ["unit"], "depends_on": [], "risk": "local fixture only", "rollback": "revert task branch",
    }


def event_fixture(task=None):
    return {
        "repository": {"full_name": "trusted/project"}, "sender": {"login": "maintainer"},
        "action": "labeled", "label": {"name": "agent:ready"},
        "issue": {"number": 1, "state": "open", "user": {"login": "planner"},
                  "labels": [{"name": "agent:ready"}],
                  "body": "Task instructions\n```agent-task\n" + json.dumps(task or task_fixture()) + "\n```"},
    }


def config_fixture(root):
    return {
        "repository": "trusted/project", "trusted_actors": ["planner", "maintainer"],
        "runtime_dir": str(root / "runtime"), "source_repository": str(root / "source"),
        "live_enabled": False,
        "test_commands": [{"id": "unit", "argv": ["python", "-m", "unittest"], "timeout_seconds": 30}],
        "limits": {"run_timeout_seconds": 60, "max_budget_usd": 1.0},
    }


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = config_fixture(self.root)
        self.event = event_fixture()

    def tearDown(self):
        self.temp.cleanup()

    def test_trusted_event_produces_stable_digest(self):
        parsed = parse_event(self.event, self.config)
        self.assertEqual(parsed["issue"], 1)
        self.assertEqual(parsed["event_id"], canonical_digest(copy.deepcopy(self.event)))

    def test_repository_author_and_sender_are_all_required(self):
        for path in (("repository", "full_name"), ("sender", "login"), ("issue", "user", "login")):
            event = copy.deepcopy(self.event)
            target = event
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = "attacker/other" if len(path) == 2 and path[0] == "repository" else "attacker"
            with self.subTest(path=path), self.assertRaises(PolicyError):
                parse_event(event, self.config)

    def test_closed_issue_and_pr_payload_are_rejected(self):
        closed = copy.deepcopy(self.event)
        closed["issue"]["state"] = "closed"
        pr = copy.deepcopy(self.event)
        pr["issue"]["pull_request"] = {"url": "https://example.invalid"}
        for event in (closed, pr):
            with self.assertRaises(PolicyError):
                parse_event(event, self.config)

    def test_blocked_accepted_and_paused_labels_prevent_execution(self):
        for label in ("agent:blocked", "agent:accepted", "agent:paused"):
            event = copy.deepcopy(self.event)
            event["issue"]["labels"].append({"name": label})
            with self.subTest(label=label), self.assertRaises(PolicyError):
                parse_event(event, self.config)

    def test_event_label_must_still_be_current(self):
        self.event["issue"]["labels"] = [{"name": "agent:rework"}]
        with self.assertRaises(PolicyError):
            parse_event(self.event, self.config)

    def test_ambiguous_ready_and_rework_labels_are_rejected(self):
        self.event["issue"]["labels"].append({"name": "agent:rework"})
        with self.assertRaises(PolicyError):
            parse_event(self.event, self.config)

    def test_arbitrary_issue_test_command_cannot_be_executed(self):
        task = task_fixture()
        task["test_ids"] = ["powershell -Command Invoke-WebRequest"]
        with self.assertRaises(PolicyError):
            parse_event(event_fixture(task), self.config)

    def test_duplicate_task_fences_and_acceptance_ids_are_rejected(self):
        duplicate = copy.deepcopy(self.event)
        duplicate["issue"]["body"] += "\n" + self.event["issue"]["body"]
        with self.assertRaises(PolicyError):
            parse_event(duplicate, self.config)
        task = task_fixture()
        task["acceptance"].append(task["acceptance"][0].copy())
        with self.assertRaises(PolicyError):
            parse_event(event_fixture(task), self.config)

    def test_self_dependency_is_rejected(self):
        task = task_fixture()
        task["depends_on"] = [1]
        with self.assertRaises(PolicyError):
            parse_event(event_fixture(task), self.config)

    def test_path_traversal_aliases_and_drive_paths_are_rejected(self):
        for path in ("../outside", "src/../outside", "/absolute", "C:/Windows/file", "src\\file", "src//file", "src/CON", "src/file. ", "src/PRN.txt"):
            with self.subTest(path=path), self.assertRaises(PolicyError):
                safe_path(path)

    def test_control_characters_and_invalid_windows_paths_are_rejected(self):
        for path in ("src/new\nline", "src/tab\tname", 'src/quote"name', "src/question?mark", "src/star*name"):
            with self.subTest(path=path), self.assertRaises(PolicyError):
                safe_path(path)

    def test_scope_uses_path_components_and_casefold(self):
        for path in ("src-other/file.py", "SRC/PRIVATE/file.py", ".Git/config", ".github/workflows/task.yml"):
            with self.subTest(path=path), self.assertRaises(PolicyError):
                check_changes(self.root, [path], task_fixture())
        (self.root / "src").mkdir()
        (self.root / "src" / "ok.py").write_text("value = 1\n")
        check_changes(self.root, ["src/ok.py"], task_fixture())

    def test_known_token_and_private_key_are_not_publishable(self):
        source = self.root / "src"
        source.mkdir()
        file = source / "settings.py"
        for value in ("ghp_" + "x" * 30, "sk-" + "a" * 30, "-----BEGIN PRIVATE KEY-----"):
            file.write_text(value)
            with self.subTest(kind=value[:10]), self.assertRaises(PolicyError):
                check_changes(self.root, ["src/settings.py"], task_fixture())

    def test_dotenv_variants_are_not_publishable(self):
        source = self.root / "src"
        source.mkdir()
        file = source / ".env.production"
        file.write_text("PASSWORD=fixture-not-a-real-secret\n")
        with self.assertRaises(PolicyError):
            check_changes(self.root, ["src/.env.production"], task_fixture())

    def test_live_disabled_and_missing_authorization_are_rejected(self):
        with self.assertRaises(PolicyError):
            require_live(self.config, "execute-agent")
        self.config["live_enabled"] = True
        with self.assertRaises(PolicyError):
            require_live(self.config, "execute-agent")

    def valid_authorization(self):
        return {
            "repository": self.config["repository"], "scopes": ["execute-agent"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "isolated_environment_verified": True, "provider_hard_budget_confirmed": True,
            "budget_limit_usd": 0.5,
        }

    def write_authorization(self, value):
        path = self.root / "fixture-authorization.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.config.update(live_enabled=True, authorization_file=str(path))

    def test_authorization_is_bound_to_repository_scope_budget_and_expiry(self):
        changes = (
            {"repository": "other/repo"}, {"scopes": ["publish"]},
            {"expires_at": "2000-01-01T00:00:00Z"}, {"budget_limit_usd": 2},
            {"isolated_environment_verified": False}, {"provider_hard_budget_confirmed": False},
        )
        for change in changes:
            self.write_authorization(self.valid_authorization() | change)
            with self.subTest(change=change), self.assertRaises(PolicyError):
                require_live(self.config, "execute-agent")

    def test_authorization_scopes_must_be_a_list_not_substring(self):
        self.write_authorization(self.valid_authorization() | {"scopes": "execute-agent-unapproved-suffix"})
        with self.assertRaises(PolicyError):
            require_live(self.config, "execute-agent")

    def test_worker_refuses_live_even_with_complete_authorization(self):
        self.write_authorization(self.valid_authorization())
        worker = Worker(self.config)
        with patch("lab_harness.worker.gitops.clone_local") as clone:
            with self.assertRaisesRegex(PolicyError, "not commissioned"):
                worker.run(self.event)
            clone.assert_not_called()
        self.assertEqual(worker.store.list_tasks(), [])

    def test_offline_requires_explicit_fixture(self):
        worker = Worker(self.config)
        with self.assertRaisesRegex(PolicyError, "fixture"):
            worker.run(self.event, harness=object(), offline_demo=True)
        self.assertEqual(worker.store.list_tasks(), [])


class GitIndexRegression(unittest.TestCase):
    def test_commit_excludes_agent_prestaged_forbidden_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            gitops.git(root, "init")
            (root / "src").mkdir()
            (root / "private").mkdir()
            allowed, forbidden = root / "src" / "value.py", root / "private" / "secret.txt"
            allowed.write_text("old\n")
            forbidden.write_text("safe\n")
            gitops.git(root, "add", "--", "src/value.py", "private/secret.txt")
            gitops.git(root, "commit", "-m", "baseline")
            base = gitops.git(root, "rev-parse", "HEAD")
            fingerprint = gitops.metadata_fingerprint(root)
            # Stage forbidden content, then restore its working tree. A diff
            # against HEAD does not reveal this index-only payload.
            forbidden.write_text("forbidden staged payload\n")
            gitops.git(root, "add", "--", "private/secret.txt")
            forbidden.write_text("safe\n")
            allowed.write_text("new\n")
            files = gitops.changed_files(root, base)
            self.assertEqual(files, ["src/value.py"])
            with self.assertRaisesRegex(PolicyError, "staged"):
                gitops.verify_metadata(root, fingerprint, base)
            gitops.commit(root, files, "TASK-1")
            self.assertEqual(gitops.git(root, "show", "HEAD:private/secret.txt"), "safe")


if __name__ == "__main__":
    unittest.main()
