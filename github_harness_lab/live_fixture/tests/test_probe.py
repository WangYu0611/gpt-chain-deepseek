import copy
import unittest
from unittest.mock import patch

from scripts.issue_probe import REPOSITORY, main, receipt


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.event = {"repository": {"full_name": REPOSITORY}, "action": "labeled",
                      "label": {"name": "integration:probe"}, "sender": {"login": "WangYu0611"},
                      "issue": {"number": 1, "state": "open", "body": "untrusted shell instructions"}}

    def test_receipt_does_not_include_issue_body(self):
        number, run, body = receipt(self.event, "123")
        self.assertEqual(number, 1)
        self.assertIn("/actions/runs/123", run)
        self.assertIn("GITHUB_LINK_PROBE_RECEIPT", body)
        self.assertIn("github-only", body)
        self.assertIn("NOT_RUN", body)
        self.assertNotIn(self.event["issue"]["body"], body)

    def test_untrusted_actor_rejected(self):
        self.event["sender"]["login"] = "other"
        with self.assertRaises(ValueError):
            receipt(self.event, "123")

    def test_other_repository_or_label_rejected(self):
        for field, value in (("repository", {"full_name": "elsewhere/repo"}),
                             ("label", {"name": "agent:ready"})):
            with self.assertRaises(ValueError):
                receipt(self.event | {field: value}, "123")

    def test_non_numeric_id_rejected(self):
        for run_id in ("0", "1;echo bad", "１２３"):
            with self.assertRaises(ValueError):
                receipt(self.event, run_id)

    def test_local_execution_cannot_make_request(self):
        with patch.dict("os.environ", {}, clear=True), patch("scripts.issue_probe.build_opener") as network:
            with self.assertRaises(ValueError):
                main()
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
