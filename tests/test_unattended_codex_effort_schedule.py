from datetime import datetime
import unittest

from unattended.config import codex_review_transport, codex_role_settings


class CodexEffortScheduleTests(unittest.TestCase):
    def test_sunday_changes_every_role_and_keeps_high_after_sunday(self):
        config = {"codex": {
            "coding_model": "gpt-6.2-sol", "coding_reasoning_effort": "ultra",
            "model": "gpt-6.2-astra", "reasoning_effort": "ultra",
            "effort_change_at": "2026-11-22T00:00:00+08:00", "effort_after_change": "high",
            "review_transport": "codex", "review_transport_after_effort_change": "responses_api",
        }}
        for value, effort in (("2026-11-21T23:59:59+08:00", "ultra"),
                              ("2026-11-21T16:00:00+00:00", "high"),
                              ("2026-11-23T11:00:00+08:00", "high")):
            now = datetime.fromisoformat(value)
            self.assertEqual(codex_review_transport(config, now=now), "codex" if effort == "ultra" else "responses_api")
            for role, model in (("code", "gpt-6.2-sol"), ("plan", "gpt-6.2-astra"),
                                ("review", "gpt-6.2-astra")):
                with self.subTest(now=now, role=role):
                    self.assertEqual(codex_role_settings(config, role, now=now), (model, effort))
