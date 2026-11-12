"""Offline filesystem fixtures and injected processes; never GitHub, UE or DSH."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness import coding_runtime as runtime
from lab_harness.process import ProcessResult
from tests.coding_fixtures import make_profile


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
BASE = "a" * 40
TREE = "b" * 40
NONCE = "01234567-89ab-4cde-8f01-23456789abcd"
SUMMARY_KEYS = {"id", "status", "exit_code", "timed_out", "output_limit_exceeded",
                "stdout_sha256", "stderr_sha256", "duration_seconds", "reason"}


def git_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


class GitObjects:
    repository = "Example/alpha"

    def __init__(self, files=None):
        files = files or {"Source/Example/Widget.cpp": b"int value = 1;\n",
                          "Config/Default.ini": b"[Engine]\n", "Example.uproject": b"{}"}
        self.commit = {"sha": BASE, "tree": {"sha": TREE}}
        self.tree = {"sha": TREE, "truncated": False, "tree": []}
        self.blobs, self.calls = {}, []
        for path, data in files.items():
            sha = git_sha(data)
            self.tree["tree"].append({"path": path, "mode": "100644", "type": "blob", "size": len(data), "sha": sha})
            self.blobs[sha] = {"sha": sha, "size": len(data), "encoding": "base64",
                               "content": base64.b64encode(data).decode("ascii")}

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method != "GET":
            raise AssertionError("Read-only API required")
        if path == "/git/commits/" + BASE:
            return self.commit
        if path == "/git/trees/" + TREE:
            return self.tree
        if path.startswith("/git/blobs/"):
            return self.blobs[path.rsplit("/", 1)[1]]
        raise AssertionError("Unapproved endpoint")


class FakeProcess:
    def __init__(self, *, stdout=b"PASS", stderr=b"", exit_code=0, timed_out=False, overflow=False, error=None):
        self.stdout, self.stderr, self.exit_code = stdout, stderr, exit_code
        self.timed_out, self.overflow, self.error, self.calls = timed_out, overflow, error, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error:
            raise self.error
        out = kwargs["output_dir"]
        (out / "stdout.log").write_bytes(self.stdout)
        (out / "stderr.log").write_bytes(self.stderr)
        return ProcessResult(self.exit_code, self.timed_out, self.overflow, out / "stdout.log", out / "stderr.log")


class CodingRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = make_profile()
        self.activation = {"enabled": True, "repository": self.profile["repository"], "nonce": NONCE,
                           "expires_at": None}
        self.task = {"number": 1, "task_id": "TASK-1", "base_sha": BASE}

    def prepare(self, client=None, **kwargs):
        return runtime.prepare_workspace(self.profile, self.task, self.activation, self.root,
                                         client or GitObjects(), **kwargs)

    def checks(self, fake=None, *, changed=None, now=lambda: NOW):
        executable = self.root / "trusted-check.exe"
        executable.write_bytes(b"Offline fake executable; never launched")
        for spec in self.profile["checks"]:
            if spec["argv"][0] == "C:/tools/fake-check.exe":
                spec["argv"][0] = str(executable)
        workspace = self.root / "checks-workspace"
        workspace.mkdir(exist_ok=True)
        return runtime.run_checks(self.profile, self.task, self.activation, workspace,
                                  ["Source/Example/Widget.cpp"] if changed is None else changed,
                                  self.root / "checks-output", process=fake or FakeProcess(), now=now)

    def test_materializes_fixed_sha_not_local_modified_source_and_never_launches_process(self):
        source = self.root / "user-project"
        source.mkdir()
        (source / "untracked.txt").write_text("keep", encoding="utf-8")
        self.profile["source_repository"] = str(source)
        client = GitObjects()
        workspace = self.prepare(client, process=lambda *a, **kw: self.fail("No subprocess is necessary"))
        self.assertEqual((workspace / "Source/Example/Widget.cpp").read_bytes(), b"int value = 1;\n")
        self.assertEqual((source / "untracked.txt").read_text(), "keep")
        self.assertFalse((workspace / "untracked.txt").exists())
        self.assertEqual(client.calls[0], ("GET", "/git/commits/" + BASE, {}))
        self.assertEqual(client.calls[1][2], {"params": {"recursive": 1}})
        self.assertIn(".runtime", workspace.parts)
        self.assertTrue(str(workspace).isascii())

    def test_continuous_activation_accepts_later_task_but_refuses_reuse(self):
        self.task["number"] = 57
        workspace = self.prepare()
        self.assertEqual(workspace.parent.name, "57")
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_existing_engine_claim_directory_is_accepted_but_source_is_not_used(self):
        parent = self.root / ".runtime" / "coding" / runtime.repository_namespace(self.profile["repository"]) / NONCE / "1"
        parent.mkdir(parents=True)
        (parent / "claim.json").write_text("reserved", encoding="utf-8")
        self.assertTrue(self.prepare().is_dir())
        self.assertEqual((parent / "claim.json").read_text(), "reserved")
        self.profile["source_repository"] = str(self.root)
        self.task["number"] = 2
        client = GitObjects()
        with self.assertRaises(runtime.RuntimeBlocked):
            self.prepare(client)
        self.assertEqual(client.calls, [])

    def test_checks_refuse_source_or_output_inside_user_project(self):
        self.profile["source_repository"] = str(self.root / "checks-workspace")
        fake = FakeProcess()
        with self.assertRaises(runtime.RuntimeBlocked):
            self.checks(fake)
        self.assertEqual(fake.calls, [])
        self.profile["source_repository"] = str(self.root / "checks-output")
        with self.assertRaises(runtime.RuntimeBlocked):
            self.checks(fake)
        self.assertEqual(fake.calls, [])

    def test_invalid_bindings_stop_before_api(self):
        for key, value in (("base_sha", "main"), ("number", 0), ("number", True)):
            client = GitObjects()
            previous = self.task[key]
            self.task[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.prepare(client)
            self.assertEqual(client.calls, [])
            self.task[key] = previous
        self.activation["expires_at"] = "2000-01-01T00:00:00Z"
        client = GitObjects()
        with self.assertRaises(runtime.RuntimeBlocked):
            self.prepare(client)
        self.assertEqual(client.calls, [])

    def test_rejects_truncation_symlinks_gitlinks_traversal_case_and_oversize(self):
        mutations = [lambda c: c.tree.update(truncated=True),
                     lambda c: c.tree.update(sha="c" * 40),
                     lambda c: c.tree["tree"][0].update(mode="120000"),
                     lambda c: c.tree["tree"][0].update(type="commit", mode="160000"),
                     lambda c: c.tree["tree"][0].update(path="../escape.cpp"),
                     lambda c: c.tree["tree"][0].update(size=runtime.MAX_FILE_BYTES + 1),
                     lambda c: c.tree["tree"].append(dict(c.tree["tree"][0], path="source/example/widget.cpp"))]
        for index, mutate in enumerate(mutations):
            client = GitObjects()
            mutate(client)
            self.task["number"] = index + 1
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.prepare(client)
            self.assertFalse(any("/git/blobs/" in call[1] for call in client.calls))

    def test_rejects_missing_scope_file_count_and_total_size_before_blobs(self):
        for index, mutate in enumerate((lambda c: c.tree["tree"].pop(),
                lambda c: c.tree.update(tree=c.tree["tree"] * 400),
                lambda c: [item.update(size=50) for item in c.tree["tree"]])):
            client = GitObjects()
            mutate(client)
            self.task["number"] = index + 1
            with patch.object(runtime, "MAX_TOTAL_BYTES", 100):
                with self.subTest(index=index), self.assertRaises(ValueError):
                    self.prepare(client)
            self.assertFalse(any("/git/blobs/" in call[1] for call in client.calls))

    def test_blob_integrity_and_encoding_are_required(self):
        for index, change in enumerate(({"content": "not-base64"}, {"content": "eHh4"}, {"sha": "f" * 40}, {"encoding": "utf-8"})):
            client = GitObjects()
            first = next(iter(client.blobs.values()))
            first.update(change)
            self.task["number"] = index + 1
            with self.subTest(change=change), self.assertRaises(runtime.RuntimeBlocked):
                self.prepare(client)

    def lfs_fixture(self):
        data = b"real binary asset\0\xff"
        oid = hashlib.sha256(data).hexdigest()
        pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(data)}\n".encode("ascii")
        source = self.root / "source"
        source.mkdir()
        self.profile["source_repository"] = str(source)
        asset = source / "Content" / "Map.umap"
        asset.parent.mkdir()
        cache = source / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
        cache.parent.mkdir(parents=True)
        return data, pointer, asset, cache

    def test_lfs_uses_verified_cache_then_verified_working_file(self):
        data, pointer, asset, cache = self.lfs_fixture()
        asset.write_bytes(b"wrong content")
        cache.write_bytes(data)
        self.assertEqual(runtime.resolve_lfs(self.profile, "Content/Map.umap", pointer), data)
        cache.unlink()
        asset.write_bytes(data)
        self.assertEqual(runtime.resolve_lfs(self.profile, "Content/Map.umap", pointer), data)
        self.profile["materialize_paths"] = ["Content"]
        workspace = self.prepare(GitObjects({"Content/Map.umap": pointer}))
        self.assertEqual((workspace / "Content/Map.umap").read_bytes(), data)

    def test_lfs_missing_wrong_hash_wrong_size_or_link_never_counts_as_asset(self):
        data, pointer, asset, cache = self.lfs_fixture()
        for content in (None, b"wrong", b"x" * len(data)):
            if content is not None:
                asset.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(runtime.RuntimeBlocked):
                runtime.resolve_lfs(self.profile, "Content/Map.umap", pointer)
        asset.write_bytes(data)
        bad_size = pointer.replace(f"size {len(data)}".encode(), f"size {len(data) + 1}".encode())
        with self.assertRaises(runtime.RuntimeBlocked):
            runtime.resolve_lfs(self.profile, "Content/Map.umap", bad_size)
        os.link(asset, cache)
        with self.assertRaises(runtime.RuntimeBlocked):
            runtime.resolve_lfs(self.profile, "Content/Map.umap", pointer)

    def test_lfs_reparse_metadata_is_rejected(self):
        data, pointer, asset, cache = self.lfs_fixture()
        asset.write_bytes(data)
        original = Path.lstat
        class LinkedInfo:
            st_mode = 0o100644
            st_file_attributes = 0x400
        def lstat(path, *args, **kwargs):
            return LinkedInfo() if path == asset else original(path, *args, **kwargs)
        with patch.object(Path, "lstat", lstat), self.assertRaises(runtime.RuntimeBlocked):
            runtime.resolve_lfs(self.profile, "Content/Map.umap", pointer)

    def test_checks_use_exact_triggers_safe_environment_and_summary_only(self):
        fake = FakeProcess(stdout=b"PASS\nprivate local log")
        with patch.dict(os.environ, {"GH_TOKEN": "never-child", "DEEPSEEK_API_KEY": "never-child", "AWS_SECRET_ACCESS_KEY": "never-child"}):
            result = self.checks(fake)
        self.assertEqual(result[0]["status"], "PASS")
        self.assertEqual(set(result[0]), SUMMARY_KEYS)
        self.assertEqual(result[0]["stdout_sha256"], hashlib.sha256(fake.stdout).hexdigest())
        self.assertNotIn("private local log", str(result))
        argv, opts = fake.calls[0]
        self.assertEqual(argv[-1], self.task["task_id"])
        self.assertEqual(opts["timeout_seconds"], 120)
        self.assertEqual(opts["cwd"], self.root / "checks-workspace")
        self.assertFalse({"GH_TOKEN", "DEEPSEEK_API_KEY", "AWS_SECRET_ACCESS_KEY"} & opts["environment"].keys())
        self.assertNotIn("shell", opts)

    def test_inapplicable_check_is_not_required_and_unknown_path_refused(self):
        fake = FakeProcess()
        self.profile["checks"].append(dict(self.profile["checks"][0], id="report", paths=["Docs/Reports/{task_id}.md"]))
        result = self.checks(fake)
        self.assertEqual([row["status"] for row in result], ["PASS", "NOT_REQUIRED"])
        self.assertEqual(len(fake.calls), 1)
        with self.assertRaises(runtime.RuntimeBlocked):
            self.checks(changed=["Source/Example/Widget.cpp/evil"])

    def test_nonzero_missing_success_failure_timeout_overflow_stop_later_checks(self):
        cases = [dict(exit_code=1), dict(stdout=b""), dict(stdout=b"PASS FAILED"), dict(timed_out=True),
                 dict(overflow=True), dict(error=RuntimeError("secret must not leak"))]
        self.profile["checks"].append(dict(self.profile["checks"][0], id="second"))
        for index, args in enumerate(cases):
            fake = FakeProcess(**args)
            with self.subTest(args=args):
                # Distinct output roots represent distinct reserved tasks, never a retry.
                with tempfile.TemporaryDirectory(dir=self.root, prefix=f"run{index}-") as directory:
                    previous = self.root
                    self.root = Path(directory)
                    try:
                        for spec in self.profile["checks"]:
                            spec["argv"][0] = "C:/tools/fake-check.exe"
                        result = self.checks(fake)
                    finally:
                        self.root = previous
                self.assertEqual([r["status"] for r in result], ["FAIL", "NOT_RUN"])
                self.assertEqual(len(fake.calls), 1)
                self.assertNotIn("secret must not leak", str(result))

    def test_finite_deadline_clamps_timeout_and_expired_checks_do_not_run(self):
        self.activation["expires_at"] = (NOW + timedelta(seconds=7)).isoformat()
        fake = FakeProcess()
        self.assertEqual(self.checks(fake)[0]["status"], "PASS")
        self.assertEqual(fake.calls[0][1]["timeout_seconds"], 7)
        self.activation["expires_at"] = NOW.isoformat()
        fake = FakeProcess()
        self.assertEqual(self.checks(fake)[0]["status"], "NOT_RUN")
        self.assertEqual(fake.calls, [])

    def test_success_patterns_are_literal_not_regex(self):
        self.profile["checks"][0]["success_patterns"] = ["PASS.*"]
        self.assertEqual(self.checks(FakeProcess(stdout=b"PASS yes"))[0]["status"], "FAIL")

    def test_relative_executable_or_batch_never_runs(self):
        for index, command in enumerate(("check.exe", str(self.root / "check.bat"))):
            fake = FakeProcess()
            self.profile["checks"][0]["argv"][0] = command
            self.profile["checks"][0]["id"] = f"command{index}"
            self.assertEqual(self.checks(fake)[0]["status"], "FAIL")
            self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
