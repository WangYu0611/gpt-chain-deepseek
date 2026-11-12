"""State-write sharing conflicts resume from durable intent without model replay."""
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_recovery as recovery
from unattended import engine as engine_module
from unattended import gitops


def sharing_error():
    error = PermissionError(13, 'test transient sharing conflict')
    error.winerror = 5
    return error


class StorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = recovery.RecoveryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_locked_intent_save_raises_without_spending_failures_or_starting_provider(self):
        before = self.f.plan()
        original = engine_module.write_json
        def locked_state(path, value):
            if Path(path).name == 'state.json':
                raise sharing_error()
            return original(path, value)
        with patch.object(engine_module, 'write_json', side_effect=locked_state):
            with self.assertRaises(PermissionError):
                self.f.restart().step()
        self.assertEqual(self.f.load(), before)
        self.assertFalse(self.f.providers.code_calls)
        self.assertEqual(self.f.restart().step()['phase'], 'CHECK')
        self.assertEqual(len(self.f.providers.code_calls), 1)
        self.assertEqual(self.f.load()['failures'], 0)

    def test_locked_final_save_after_commit_recovers_git_without_second_provider_call(self):
        self.f.plan()
        original = engine_module.write_json
        def locked_after_code(path, value):
            if (Path(path).name == 'state.json' and self.f.providers.code_calls and
                    value['phase'] == 'CHECK'):
                raise sharing_error()
            return original(path, value)
        with patch.object(engine_module, 'write_json', side_effect=locked_after_code):
            with self.assertRaises(PermissionError):
                self.f.restart().step()
        state = self.f.load()
        committed = gitops.head(self.f.work)
        self.assertEqual(state['phase'], 'CODE')
        self.assertEqual(state['failures'], 0)
        self.assertIsNotNone(state['operation'])
        self.assertNotEqual(committed, state['task']['code_base'])
        self.assertEqual(len(self.f.providers.code_calls), 1)
        self.assertEqual(self.f.restart().step()['phase'], 'CHECK')
        self.assertEqual(gitops.head(self.f.work), committed)
        self.assertEqual(len(self.f.providers.code_calls), 1)
        self.assertEqual(self.f.load()['failures'], 0)


if __name__ == '__main__':
    unittest.main()
