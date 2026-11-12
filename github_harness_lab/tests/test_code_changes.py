"""Only synthetic text and temporary directories; no network or model calls."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.code_changes import (
    ApplyChangeSetError, MAX_CHANGES, MAX_CONTENT_BYTES, MAX_JSON_BYTES,
    MAX_ORIGINAL_BYTES, MAX_TOTAL_CONTENT_BYTES, SCHEMA,
    apply_change_set, validate_change_set,
)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CodeChangesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.originals = {"Source/main.cpp": "int value = 1;\r\n"}
        self.allowed = ["Source/main.cpp", "Source/新增.cpp"]
        self.data = {"schema": SCHEMA, "repository": "Example/Project",
                     "base_sha": "a" * 40, "task_id": "TASK-M2-22", "changes": [
            {"path": "Source/main.cpp", "operation": "modify",
             "before_sha256": sha(self.originals["Source/main.cpp"]), "content": "int value = 2;\r\n"},
            {"path": "Source/新增.cpp", "operation": "add", "before_sha256": None,
             "content": "// 普通 UTF-8 源码\nconst char* text = \"<span>ok</span>\";\n"},
        ]}
        for path, text in self.originals.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))

    def validate(self, data=None, originals=None, allowed=None):
        return validate_change_set(json.dumps(self.data if data is None else data, ensure_ascii=False),
            repository="Example/Project", base_sha="a" * 40, task_id="TASK-M2-22",
            originals=self.originals if originals is None else originals,
            allowed_paths=self.allowed if allowed is None else allowed)

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_add_modify_preserve_exact_utf8_and_line_endings(self):
        validated = self.validate()
        self.assertEqual(dict(validated), self.data)
        self.assertEqual(apply_change_set(self.root, validated, originals=self.originals), self.allowed)
        self.assertEqual(self.snapshot(), {change["path"]: change["content"].encode("utf-8")
                                          for change in self.data["changes"]})

    def test_replace_large_file_uses_sequential_unique_fragments(self):
        original = "prefix\n" + "x" * (MAX_CONTENT_BYTES + 20) + "\nint value = 1;\n"
        originals = {"Source/main.cpp": original}
        (self.root / "Source/main.cpp").write_text(original, encoding="utf-8", newline="")
        data = copy.deepcopy(self.data)
        data["changes"] = [{"path": "Source/main.cpp", "operation": "replace", "before_sha256": sha(original),
                            "edits": [{"old": "value = 1", "new": "value = 2"},
                                      {"old": "value = 2", "new": "value = 3"}]}]
        validated = self.validate(data, originals)
        self.assertEqual(apply_change_set(self.root, validated, originals=originals), ["Source/main.cpp"])
        self.assertEqual((self.root / "Source/main.cpp").read_bytes(), original.replace("value = 1", "value = 3").encode())

    def test_replace_rejects_missing_ambiguous_empty_or_noop_edits(self):
        original = "same same\n"
        originals = {"Source/main.cpp": original}
        for edits in ([{"old": "missing", "new": "x"}], [{"old": "same", "new": "x"}],
                      [{"old": "", "new": "x"}], [], [{"old": original, "new": original}],
                      [{"old": original, "new": "new", "extra": True}]):
            data = copy.deepcopy(self.data)
            data["changes"] = [{"path": "Source/main.cpp", "operation": "replace",
                                "before_sha256": sha(original), "edits": edits}]
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                self.validate(data, originals)

    def test_binding_and_original_hash_are_exact(self):
        for field, value in (("schema", "other/v1"), ("repository", "Other/Project"),
                             ("base_sha", "b" * 40), ("task_id", "TASK-M2-23")):
            data = copy.deepcopy(self.data)
            data[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(data)
        for value in (None, "f" * 64, sha("int value = 1;\n"), []):
            data = copy.deepcopy(self.data)
            data["changes"][0]["before_sha256"] = value
            with self.subTest(hash=value), self.assertRaises(ValueError):
                self.validate(data)

    def test_stale_second_file_fails_before_first_file_is_changed(self):
        validated = self.validate()
        (self.root / "Source/新增.cpp").write_text("unexpected", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            apply_change_set(self.root, validated, originals=self.originals)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.root.rglob(".harness-code-*")))

    def test_stale_missing_directory_and_binary_originals_fail_without_writes(self):
        for mode in ("stale", "missing", "directory", "binary"):
            with self.subTest(mode=mode):
                path = self.root / "Source/main.cpp"
                if path.is_file():
                    path.unlink()
                if path.is_dir():
                    path.rmdir()
                if mode == "stale":
                    path.write_bytes(b"different")
                elif mode == "directory":
                    path.mkdir()
                elif mode == "binary":
                    path.write_bytes(b"\x00\xff")
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    apply_change_set(self.root, self.validate(), originals=self.originals)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.root / "Source/新增.cpp").exists())

    def test_paths_and_protected_locations_are_rejected(self):
        bad = ["../escape.py", "/root.py", "C:/file.py", "C:file.py", "dir\\file.py", "dir//file.py",
               "dir/./file.py", "dir/file.py/", "file.py:stream", "dir/CON.txt", "dir/NUL", "LPT1/file.py",
               "dir./file.py", "dir /file.py", "file\n.py", "file\u202e.py", "PROGRA~1/a.py",
               "Source/*.cpp", ".git/config", ".github/workflows/a.yml", "Tools/HarnessBridge/run.py",
               "a/AGENTS.md", "a/.env.local", "a/.npmrc", "a/credentials.json", "a/secrets.yaml",
               "a/config.local.json", "a/id_rsa", "a/server.key", "a/账户凭据.json"]
        for path in bad:
            data = copy.deepcopy(self.data)
            data["changes"] = [{"path": path, "operation": "add", "before_sha256": None, "content": "safe"}]
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate(data, allowed=[path])
        data = copy.deepcopy(self.data)
        data["changes"][1]["path"] = "Source/unapproved.cpp"
        with self.assertRaises(ValueError):
            self.validate(data)

    def test_case_duplicates_and_file_directory_collisions_are_rejected(self):
        for allowed in (["Source/main.cpp", "source/MAIN.cpp"], ["Source/main.cpp", "source/other.cpp"],
                        ["Source/main.cpp", "Source/main.cpp/child.py"], ["Source/main.cpp"] * 2):
            with self.subTest(allowed=allowed), self.assertRaises(ValueError):
                self.validate(allowed=allowed)
        data = copy.deepcopy(self.data)
        data["changes"].append(copy.deepcopy(data["changes"][0]))
        with self.assertRaises(ValueError):
            self.validate(data)
        data = copy.deepcopy(self.data)
        data["changes"] = [{"path": "Source/MAIN.cpp", "operation": "add", "before_sha256": None, "content": "x"}]
        with self.assertRaises(ValueError):
            self.validate(data, allowed=["Source/MAIN.cpp"])

    def test_existing_directory_case_conflict_is_rejected(self):
        data = copy.deepcopy(self.data)
        data["changes"] = [{"path": "source/other.cpp", "operation": "add", "before_sha256": None, "content": "x"}]
        validated = self.validate(data, originals={}, allowed=["source/other.cpp"])
        before = self.snapshot()
        with self.assertRaises(ValueError):
            apply_change_set(self.root, validated, originals={})
        self.assertEqual(self.snapshot(), before)

    def test_unknown_fields_duplicate_json_keys_and_wrong_types_are_rejected(self):
        raw = json.dumps(self.data)
        invalid = [raw.replace('"schema":', '"schema": "duplicate", "schema":', 1),
                   raw.replace('"path":', '"path": "duplicate", "path":', 1),
                   raw.replace('"changes":', '"extra": 1, "changes":', 1),
                   raw.replace('"operation": "modify"', '"operation": "modify", "extra": 1', 1),
                   raw.replace('"operation": "modify"', '"operation": []', 1),
                   raw.replace('"operation": "modify"', '"operation": "delete"', 1),
                   raw.replace('"before_sha256": null', '"before_sha256": NaN', 1), "[]", "null", "{}​"]
        for value in invalid:
            with self.subTest(raw=value[:70]), self.assertRaises(ValueError):
                validate_change_set(value, repository="Example/Project", base_sha="a" * 40,
                                    task_id="TASK-M2-22", originals=self.originals, allowed_paths=self.allowed)

    def test_empty_changes_and_text_count_byte_limits(self):
        for changes in ([], self.data["changes"] * (MAX_CHANGES // 2 + 1)):
            data = copy.deepcopy(self.data)
            data["changes"] = changes
            with self.assertRaises(ValueError):
                self.validate(data)
        for content in ("x" * (MAX_CONTENT_BYTES + 1), "中" * (MAX_CONTENT_BYTES // 3 + 1), "bad\x00text", "\ud800"):
            data = copy.deepcopy(self.data)
            data["changes"][1]["content"] = content
            with self.assertRaises(ValueError):
                self.validate(data)
        with self.assertRaises(ValueError):
            validate_change_set(" " * (MAX_JSON_BYTES + 1), repository="Example/Project", base_sha="a" * 40,
                                task_id="TASK-M2-22", originals=self.originals, allowed_paths=self.allowed)
        oversized = {"Source/main.cpp": "x" * (MAX_ORIGINAL_BYTES + 1)}
        with self.assertRaises(ValueError):
            self.validate(originals=oversized)
        paths = [f"Source/new{i}.py" for i in range(MAX_TOTAL_CONTENT_BYTES // MAX_CONTENT_BYTES + 1)]
        data = copy.deepcopy(self.data)
        data["changes"] = [{"path": path, "operation": "add", "before_sha256": None,
                            "content": "x" * MAX_CONTENT_BYTES} for path in paths]
        with self.assertRaises(ValueError):
            self.validate(data, originals={}, allowed=paths)

    def test_receipt_tampering_and_serialized_receipt_require_revalidation(self):
        for mutation in ("content", "path", "plain"):
            validated = self.validate()
            if mutation == "plain":
                validated = json.loads(json.dumps(validated))
            else:
                validated["changes"][0][mutation] = "changed"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                apply_change_set(self.root, validated, originals=self.originals)
        self.assertEqual(self.snapshot(), {path: text.encode() for path, text in self.originals.items()})

    def test_new_directories_are_created_only_for_a_valid_change_set(self):
        data = copy.deepcopy(self.data)
        path = "新目录/nested/new.py"
        data["changes"] = [{"path": path, "operation": "add", "before_sha256": None, "content": "print('text only')\n"}]
        self.assertEqual(apply_change_set(self.root, self.validate(data, originals={}, allowed=[path]), originals={}), [path])
        self.assertEqual((self.root / path).read_text(encoding="utf-8"), "print('text only')\n")

    def test_hardlink_is_rejected_before_any_replacement(self):
        link = self.root / "Source/alias.cpp"
        try:
            os.link(self.root / "Source/main.cpp", link)
        except OSError as error:
            self.skipTest(f"Filesystem does not support hard links: {error}")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            apply_change_set(self.root, self.validate(), originals=self.originals)
        self.assertEqual(self.snapshot(), before)

    def test_symlink_target_and_workspace_are_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Symlink permission unavailable: {error}")
        path = "linked/new.py"
        data = copy.deepcopy(self.data)
        data["changes"] = [{"path": path, "operation": "add", "before_sha256": None, "content": "x"}]
        with self.assertRaises(ValueError):
            apply_change_set(self.root, self.validate(data, originals={}, allowed=[path]), originals={})
        with self.assertRaises(ValueError):
            apply_change_set(link, self.validate(), originals=self.originals)
        self.assertEqual(list(outside.iterdir()), [])

    def test_windows_reparse_attribute_is_rejected_without_following_it(self):
        # Windows junctions expose this bit even when S_ISLNK is false.
        from types import SimpleNamespace
        from lab_harness.code_changes import _safe_stat
        normal = (self.root / "Source/main.cpp").lstat()
        fake = SimpleNamespace(st_mode=normal.st_mode, st_nlink=1, st_file_attributes=0x400)
        with patch.object(Path, "lstat", return_value=fake), self.assertRaises(ValueError):
            _safe_stat(self.root / "Source/main.cpp")

    def test_partial_io_failure_reports_applied_paths_and_never_claims_transaction(self):
        replace = os.replace
        calls = []
        def fail_second(source, target):
            calls.append(target)
            if len(calls) == 2:
                raise OSError("synthetic write failure")
            return replace(source, target)
        with patch("lab_harness.code_changes.os.replace", side_effect=fail_second):
            with self.assertRaises(ApplyChangeSetError) as caught:
                apply_change_set(self.root, self.validate(), originals=self.originals)
        self.assertEqual(caught.exception.applied_paths, ("Source/main.cpp",))
        self.assertEqual(caught.exception.failed_path, "Source/新增.cpp")
        self.assertEqual((self.root / "Source/main.cpp").read_bytes(), b"int value = 2;\r\n")
        self.assertFalse((self.root / "Source/新增.cpp").exists())
        self.assertFalse(list(self.root.rglob(".harness-code-*")))


if __name__ == "__main__":
    unittest.main()
