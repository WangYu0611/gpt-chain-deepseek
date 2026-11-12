"""Profile portability and fail-closed validation without model or network access."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from lab_harness.project_profile import (
    MAX_PROFILE_BYTES, load_profile, make_profile, profile_digest, repository_namespace,
    task_spec, validate_document, validate_profile,
)


class ProjectProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = make_profile("Example/first-project", "main", "Example")

    def document(self, profile=None, number=1):
        task = task_spec(profile or self.profile, number)
        lines = [task["validation"]["first_line"], ""]
        lines.extend(task["validation"]["required_headings"])
        lines.extend(["本次只验证文档任务交接及真实报告，不代表项目功能已经完成。" * 3, ""])
        if task["validation"]["bullet_count"] is not None:
            lines.extend("- 核对任务的真实执行记录以及相应文档内容。" for _ in range(task["validation"]["bullet_count"]))
        return "\n".join(lines).rstrip() + "\n"

    def test_one_executor_policy_supports_two_repositories_and_custom_tasks(self):
        other = make_profile("Example/second-project", "release/v1", "Another-user", source_repository="C:\\code\\second")
        for number in (1, 2):
            item = other["document_tasks"][number - 1]
            item["task_id"] = f"TASK-CUSTOM-{number:02d}"
            item["document_path"] = f"Documentation/Canary/{item['task_id']}.md"
            item["report_path"] = f"Documentation/Reports/{item['task_id']}.md"
            item["validation"]["first_line"] = "# " + item["task_id"]
            item["prompt"] = "写一份对应自定义任务的中文文档，保留配置中的章节与长度要求。"
        other["stop_task"]["task_id"] = "TASK-CUSTOM-03"
        for profile in (self.profile, other):
            for number in (1, 2):
                self.assertEqual(validate_document(profile, number, self.document(profile, number)), self.document(profile, number))
        self.assertNotEqual(self.profile["runner_label"], other["runner_label"])
        with self.assertRaises(ValueError):
            validate_document(other, 1, self.document(self.profile, 1))

    def test_repository_normalization_and_case_stable_namespace(self):
        self.assertEqual(repository_namespace("https://github.com/Example/Repo.git"), repository_namespace("example/repo"))
        self.assertNotEqual(repository_namespace("Example/one"), repository_namespace("Example/two"))
        self.assertEqual(make_profile("git@github.com:Example/Repo.git", "main", "Example")["repository"], "Example/Repo")

    def test_digest_is_canonical_and_changes_with_policy(self):
        reversed_profile = dict(reversed(list(self.profile.items())))
        reversed_profile["document_tasks"] = list(reversed(self.profile["document_tasks"]))
        normalized = validate_profile(self.profile)
        expected = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(profile_digest(reversed_profile), expected)
        changed = copy.deepcopy(self.profile)
        changed["document_tasks"][0]["validation"]["max_chars"] += 1
        self.assertNotEqual(profile_digest(changed), expected)

    def test_returned_profiles_and_specs_do_not_mutate_input(self):
        returned = validate_profile(self.profile)
        returned["document_tasks"][0]["validation"]["required_headings"].append("## Changed")
        spec = task_spec(self.profile, 1)
        spec["task_id"] = "TASK-CHANGED"
        self.assertEqual(len(self.profile["document_tasks"][0]["validation"]["required_headings"]), 2)
        self.assertNotEqual(self.profile["document_tasks"][0]["task_id"], spec["task_id"])

    def test_stop_task_and_boolean_numbers_never_execute(self):
        for number in (3, 0, -1, True, False, "1", None):
            with self.subTest(number=number), self.assertRaises(ValueError):
                task_spec(self.profile, number)
            with self.subTest(number=number), self.assertRaises(ValueError):
                validate_document(self.profile, number, self.document())

    def test_live_authorization_and_unknown_fields_are_rejected(self):
        for name, value in (("live_enabled", True), ("live_enabled", 0), ("nonce", "new"),
                            ("token", "synthetic"), ("capability", "arbitrary-code"),
                            ("runner_label", "self-hosted"), ("schema", "other/v1")):
            changed = copy.deepcopy(self.profile)
            changed[name] = value
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                validate_profile(changed)
        changed = copy.deepcopy(self.profile)
        changed["document_tasks"][0]["command"] = ["python", "anything.py"]
        with self.assertRaises(ValueError):
            validate_profile(changed)

    def test_branch_actor_source_and_repository_validation(self):
        for field, value in (("repository", "https://example.test/a/b"), ("default_branch", "main~1"),
                             ("default_branch", "main\n"), ("trusted_actor", "actor[bot]"),
                             ("source_repository", "relative/project"), ("source_repository", "C:\\code\\a\x00"),
                             ("source_repository", None)):
            changed = copy.deepcopy(self.profile)
            changed[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_profile(changed)

    def test_paths_reject_traversal_sensitive_unicode_and_windows_ambiguity(self):
        bad_paths = ["../file.md", "/file.md", "C:/file.md", "Docs\\file.md", "Docs//file.md",
                     "Docs/file.md/", "Docs/file.md:stream", ".github/file.md", "Docs/.git/file.md",
                     "Docs/lab_harness/file.md", "Docs/AGENTS.md", "Docs/CON.md", "Docs/NUL/file.md",
                     "Docs/dir./file.md", "Docs/dir /file.md", "Docs/文档.md", "Docs/fіle.md",
                     "Docs/file\u202e.md", "Docs/file.MD", "Docs/file.py", "Docs/.env.md"]
        for path in bad_paths:
            changed = copy.deepcopy(self.profile)
            changed["document_tasks"][0]["document_path"] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_profile(changed)

    def test_path_overlaps_and_duplicate_task_ids_are_rejected(self):
        for kind in ("same", "case", "ancestor", "id", "stop_id", "task_number", "extra_task", "stop_dispatch"):
            changed = copy.deepcopy(self.profile)
            if kind in {"same", "case", "ancestor"}:
                original = changed["document_tasks"][0]["document_path"]
                changed["document_tasks"][1]["report_path"] = original if kind == "same" else original.lower() if kind == "case" else original + "/child.md"
            elif kind == "id":
                changed["document_tasks"][1]["task_id"] = changed["document_tasks"][0]["task_id"]
            elif kind == "stop_id":
                changed["stop_task"]["task_id"] = changed["document_tasks"][0]["task_id"]
            elif kind == "task_number":
                changed["document_tasks"][0]["number"] = True
            elif kind == "extra_task":
                changed["document_tasks"].append(copy.deepcopy(changed["document_tasks"][0]))
            else:
                changed["stop_task"]["dispatch"] = True
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_profile(changed)

    def test_profile_text_and_validation_bounds(self):
        for field, value in (("min_chars", 79), ("min_chars", True), ("max_chars", 3001),
                             ("max_chars", 50), ("bullet_count", True), ("bullet_count", -1),
                             ("required_headings", []), ("required_headings", ["## 标题", "## 标题"]),
                             ("required_headings", ["## 标题\n隐藏"]), ("first_line", "# Wrong")):
            changed = copy.deepcopy(self.profile)
            changed["document_tasks"][0]["validation"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_profile(changed)
        for prompt in ("中" * 1366, "\x00bad", "bad\u200bhidden", ""):
            changed = copy.deepcopy(self.profile)
            changed["document_tasks"][0]["prompt"] = prompt
            with self.subTest(prompt=prompt[:10]), self.assertRaises(ValueError):
                validate_profile(changed)

    def test_load_bounded_json_bom_duplicate_keys_and_malformed_data(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps(self.profile, ensure_ascii=False), encoding="utf-8-sig")
            self.assertEqual(load_profile(path), self.profile)
            bad_inputs = [b"x" * (MAX_PROFILE_BYTES + 1), b"not json", b"\xff", b"[]",
                          b'{"schema":"a","schema":"b"}', b'{"x":{"a":1,"a":2}}',
                          b'{"x":NaN}', b"[" * 2000 + b"]" * 2000]
            for raw in bad_inputs:
                path.write_bytes(raw)
                with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                    load_profile(path)

    def test_document_normalizes_crlf_and_rejects_missing_duplicate_headings(self):
        document = self.document()
        self.assertEqual(validate_document(self.profile, 1, document.replace("\n", "\r\n")), document)
        heading = self.profile["document_tasks"][0]["validation"]["required_headings"][0]
        for bad in (document.replace(heading, "## Different"), document + heading + "\n",
                    document + heading + " ###\n", document + document.splitlines()[0] + "\n",
                    document.replace("# TASK-", "# BAD-", 1), document[:30], document + "x" * 2000):
            with self.subTest(bad=bad[:50]), self.assertRaises(ValueError):
                validate_document(self.profile, 1, bad)

    def test_document_rejects_html_links_fences_and_hidden_controls(self):
        suffixes = ["<b>text</b>", "```text```", "~~~python", "https://example.test", "//example.test",
                    "www.example.test", "someone@example.test", "[x](relative.md)", "[x][reference]",
                    "[reference]: destination", "\x00", "\t", "\r", "\u200b", "\u202e", "\u2028"]
        for suffix in suffixes:
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                validate_document(self.profile, 1, self.document() + suffix)

    def test_bullet_count_rejects_extra_missing_or_other_list_styles(self):
        document = self.document(number=2)
        self.assertEqual(validate_document(self.profile, 2, document), document)
        for bad in (document + "- extra\n", document.replace("- ", "", 1),
                    document.replace("- ", "* ", 1), document + "1. extra\n"):
            with self.subTest(bad=bad[-60:]), self.assertRaises(ValueError):
                validate_document(self.profile, 2, bad)


if __name__ == "__main__":
    unittest.main()
