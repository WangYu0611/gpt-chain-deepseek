from __future__ import annotations

import json
import unittest

from relay.protocol import ProtocolError, commit_sha, extract_block, parse_delivery, parse_handoff, repository_path


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.handoff = {"relay_id":"review-fixture", "reviewed_sha":self.sha,
                        "action":"execute", "task_id":"TASK-002",
                        "task_path":"Docs/Tasks/TASK-002.md", "published_sha":"b" * 40}

    def wrap(self, content):
        return "审阅结果。\n【交接】\n" + content + "\n【交接结束】"

    def test_pure_json_and_standard_fences(self):
        raw = json.dumps(self.handoff)
        for content in (raw, "```json\n" + raw + "\n```", "```\n" + raw + "\n```",
                        "```JSON\n" + raw + "\n```", "~~~json\n" + raw + "\n~~~"):
            with self.subTest(content=content[:20]):
                self.assertEqual(parse_handoff(self.wrap(content), "review-fixture", self.sha), self.handoff)

    def test_rendered_language_and_copy_labels_are_allowed_at_edges(self):
        raw = json.dumps(self.handoff)
        for prefix, suffix in (("json\nCopy code\n", ""), ("JSON\n复制代码\n", "\n已复制"),
                               ("JSON\n\nCopy code\n\n", "\n\nCopied"),
                               ("json Copy code\n", "\nCopied"), ("json复制代码\n", "\n复制"),
                               ("Copy\n", "\nCopy code")):
            with self.subTest(prefix=prefix):
                answer = self.wrap(prefix + raw + suffix)
                self.assertEqual(parse_handoff(answer, "review-fixture", self.sha), self.handoff)

    def test_display_labels_outside_fence_are_allowed(self):
        raw = json.dumps(self.handoff, ensure_ascii=False, indent=2)
        answer = self.wrap("JSON\nCopy code\n```json\n" + raw + "\n```\nCopied")
        self.assertEqual(parse_handoff(answer, "review-fixture", self.sha), self.handoff)

    def test_arbitrary_explanation_is_not_stripped(self):
        raw = json.dumps(self.handoff)
        for content in ("Here is the JSON:\n" + raw, raw + "\nPlease execute this task.",
                        "json\n请执行以下任务\n" + raw, raw + "\nCopy code and continue",
                        "javascript\n" + raw, "```python\n" + raw + "\n```"):
            with self.subTest(content=content[:40]), self.assertRaises(ProtocolError):
                parse_handoff(self.wrap(content), "review-fixture", self.sha)

    def test_multiple_objects_or_non_objects_are_rejected(self):
        for content in ("{} {}", "{}\nCopy code\n{}", "[{}]", '"json"', "null", "true"):
            with self.subTest(content=content), self.assertRaises(ProtocolError):
                extract_block(self.wrap(content), "【交接】", "【交接结束】")

    def test_unmatched_or_mixed_fences_are_rejected(self):
        raw = json.dumps(self.handoff)
        for content in ("```json\n" + raw, raw + "\n```", "~~~json\n" + raw + "\n```",
                        "```json\n" + raw + "\n```\nextra"):
            with self.subTest(content=content[:30]), self.assertRaises(ProtocolError):
                parse_handoff(self.wrap(content), "review-fixture", self.sha)

    def test_relay_id_and_reviewed_sha_remain_strict(self):
        for changes in ({"relay_id":"old-review"}, {"reviewed_sha":"b" * 40},
                        {"reviewed_sha":None}, {"reviewed_sha":42}, {"reviewed_sha":"aaaaaaa"}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                parse_handoff(self.wrap("json\nCopy code\n" + json.dumps(dict(self.handoff, **changes))),
                              "review-fixture", self.sha)
        upper = dict(self.handoff, reviewed_sha=self.sha.upper())
        self.assertEqual(parse_handoff(self.wrap(json.dumps(upper)), "review-fixture", self.sha)["relay_id"], "review-fixture")

    def test_task_paths_reject_escape_or_option_injection(self):
        for path in ("../outside.md", "Docs/../../outside.md", "/absolute.md", "C:/outside.md",
                     "Docs\\Tasks\\TASK.md", "-option", "Docs/./TASK.md", "Docs/../TASK.md"):
            with self.subTest(path=path), self.assertRaises(ProtocolError):
                parse_handoff(self.wrap(json.dumps(dict(self.handoff, task_path=path))),
                              "review-fixture", self.sha)
        self.assertEqual(repository_path("Docs/Tasks/任务书.md"), "Docs/Tasks/任务书.md")

    def test_published_sha_requires_full_hex_commit(self):
        for sha in ("main", "1234567", "z" * 40, None):
            with self.subTest(sha=sha), self.assertRaises(ProtocolError):
                parse_handoff(self.wrap(json.dumps(dict(self.handoff, published_sha=sha))),
                              "review-fixture", self.sha)
        self.assertEqual(commit_sha("B" * 64), "b" * 64)

    def test_delivery_accepts_same_display_wrappers_and_checks_task(self):
        delivery = {"task_id":"TASK-002", "commit_sha":self.sha,
                    "report_paths":["Docs/Reports/报告.md"], "screenshot_paths":["Docs/Evidence/截图.png"]}
        answer = "【交付】\njson\n复制代码\n" + json.dumps(delivery) + "\nCopied\n【交付结束】"
        self.assertEqual(parse_delivery(answer, "TASK-002"), delivery)
        with self.assertRaises(ProtocolError):
            parse_delivery(answer, "TASK-OTHER")
        delivery["report_paths"] = ["../secret.txt"]
        with self.assertRaises(ProtocolError):
            parse_delivery("【交付】" + json.dumps(delivery) + "【交付结束】", "TASK-002")


if __name__ == "__main__":
    unittest.main()
