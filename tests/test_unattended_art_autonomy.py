"""Autonomous art recovery must keep the author and human acceptance separate."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from unattended import art_autonomy
from unattended.config import validate_art_autonomy_settings
from unattended.engine import Engine
from unattended.providers import ProviderError
from unattended.storage import lock, read_json, write_json
from tests import test_unattended_art_engine as fixtures

HEAD, SESSION = fixtures.HEAD, fixtures.SESSION


class ArtAutonomyTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ArtEngineTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.config['art_autonomy'] = {'enabled': True, 'max_cycles': 2, 'rework_window': 3}
        self.engine = self.f.engine
        self.plan = {'action': 'continue', 'diagnosis': 'Author diagnosis from evidence',
                     'plan': 'Author selected implementation, not a controller suggestion',
                     'reason': 'Existing authorized scope', 'required_approvals': [], 'questions': [],
                     'additional_tripo_credits': 0, 'head_sha': HEAD}
        self.f.providers.art_diagnose.return_value = self.plan

    def state(self):
        return read_json(self.engine.state_path)

    def trigger(self):
        self.f.checked()
        state = self.state()
        state['task']['reworks'] = 3
        self.engine._save(state)
        self.f.providers.review.return_value = {'decision': 'rework', 'summary': 'Repair needed',
                                              'feedback': 'Concrete review evidence', 'head_sha': HEAD}
        result = self.engine.step()
        self.assertEqual(result['phase'], 'ART_DIAGNOSE', result)
        return self.state()

    def test_limit_asks_actual_author_then_executes_exact_plan_in_original_session(self):
        before = self.trigger()
        self.f.providers.art_diagnose.assert_not_called()
        self.assertEqual(before['task']['reworks'], 3)
        result = self.f.restart().step()
        self.assertEqual(result['phase'], 'CODE', result)
        state = self.state()
        task = state['task']
        self.assertEqual(task['session_id'], SESSION)
        self.assertEqual(task['reworks'], 4)
        self.assertEqual(task['autonomous_rework_allowance'], 3)
        self.assertNotIn('manual_rework_allowance', task)
        self.assertIn(self.plan['plan'], task['feedback'])
        self.assertEqual(task['art_diagnosis_history'][0]['result'], self.plan)
        self.assertEqual(state['accepted'], [])
        self.assertEqual(state.get('art_pending', []), [])
        self.assertNotIn('art_history', state)
        args, kwargs = self.f.providers.art_diagnose.call_args
        self.assertEqual(kwargs['session_id'], SESSION)
        self.assertIn('调度器不提供候选美术方案', args[0])
        self.assertNotIn('重建屋顶', args[0])
        self.assertNotIn('private_state_dir', args[0])
        self.assertNotIn('key_file', args[0])
        receipt = read_json(Path(task['art_diagnosis_history'][0]['public_receipt']))
        self.assertEqual(receipt['execution_decision'], 'continue')

    def test_completed_diagnosis_is_recovered_without_second_claude_call(self):
        state = self.trigger()
        self.engine._new_operation(state, 'art_diagnose')
        self.f.providers.recover.return_value = deepcopy(self.plan)
        self.assertEqual(self.f.restart().step()['phase'], 'CODE')
        self.f.providers.art_diagnose.assert_not_called()
        self.assertEqual(self.state()['task']['session_id'], SESSION)
        self.assertEqual(len(self.state()['task']['art_diagnosis_history']), 1)

    def test_changed_user_boundary_waits_without_granting_execution_or_acceptance(self):
        self.trigger()
        self.plan.update(action='needs_user', required_approvals=['style_reference'], questions=['Change the approved style?'])
        result = self.engine.step()
        self.assertEqual(result['phase'], 'BLOCKED')
        self.assertEqual(self.state()['blocked_from'], 'ART_DIAGNOSE')
        self.assertIn('Change the approved style?', result['last_error'])
        self.assertEqual(self.state()['task']['reworks'], 3)
        self.assertEqual(self.state()['accepted'], [])
        self.f.providers.code.assert_called_once()

    def test_self_reported_within_scope_cannot_exceed_actual_local_budget(self):
        self.trigger()
        self.plan['additional_tripo_credits'] = 3000
        result = self.engine.step()
        self.assertEqual(result['phase'], 'BLOCKED')
        self.assertIn('预算', result['last_error'])
        self.assertEqual(self.state()['task']['art_diagnosis_history'][0]['result']['action'], 'continue')
        self.assertEqual(self.state()['task']['reworks'], 3)

    def test_changed_input_before_diagnosis_cannot_reuse_pending_source(self):
        self.trigger()
        package = self.f.evidence.public / 'review-package.json'
        package.write_bytes(package.read_bytes() + b'\n')
        result = self.engine.step()
        self.assertEqual(result['phase'], 'ART_DIAGNOSE')
        self.assertIn('来源已改变', result['last_error'])
        self.f.providers.art_diagnose.assert_not_called()

    def test_changed_evidence_during_diagnosis_never_grants_a_window(self):
        self.trigger()
        def changed(*args, **kwargs):
            package = self.f.evidence.public / 'review-package.json'
            package.write_bytes(package.read_bytes() + b'\n')
            return self.plan
        self.f.providers.art_diagnose.side_effect = changed
        result = self.engine.step()
        self.assertEqual(result['phase'], 'ART_DIAGNOSE')
        self.assertIn('不匹配', result['last_error'])
        self.assertNotIn('art_diagnosis_history', self.state()['task'])
        self.assertEqual(self.state()['task']['reworks'], 3)

    def test_diagnosis_quota_failure_waits_without_spending_failures_or_reworks(self):
        self.trigger()
        self.f.providers.art_diagnose.side_effect = ProviderError('Quota', transient=True,
            retry_reason='claude_subscription_limit', retry_after_seconds=3600)
        result = self.engine.step()
        self.assertEqual(result['status'], 'waiting_provider', result)
        state = self.state()
        self.assertEqual(state['failures'], 0)
        self.assertEqual(state['task']['reworks'], 3)
        self.assertEqual(state['task']['session_id'], SESSION)
        self.assertEqual(state['provider_retry']['phase'], 'ART_DIAGNOSE')

    def test_repeated_same_plan_and_artifacts_do_not_loop_indefinitely(self):
        self.trigger()
        self.assertEqual(self.engine.step()['phase'], 'CODE')
        self.assertEqual(self.engine.step()['phase'], 'ART_CHECK')
        self.assertEqual(self.engine.step()['phase'], 'ART_REVIEW')
        state = self.state()
        state['task']['reworks'] = 6
        self.engine._save(state)
        self.assertEqual(self.engine.step()['phase'], 'ART_DIAGNOSE')
        result = self.engine.step()
        self.assertEqual(result['phase'], 'BLOCKED')
        self.assertIn('同一产物', result['last_error'])
        self.assertEqual(len(self.state()['task']['art_diagnosis_history']), 2)

    def test_bounded_cycles_do_not_silently_expand_global_rework_limit(self):
        self.trigger()
        state = self.state()
        state['phase'] = 'ART_REVIEW'
        state['task'].pop('art_diagnosis')
        state['task']['art_diagnosis_history'] = [
            {'round': number, 'execution_decision': 'continue', 'art_autonomy_epoch': 0} for number in (1, 2)]
        self.engine._save(state)
        result = self.engine.step()
        self.assertEqual(result['phase'], 'BLOCKED')
        self.assertIn('自主诊断窗口已用完', result['last_error'])
        self.assertEqual(self.f.config['max_reworks'], 3)
        self.f.providers.art_diagnose.assert_not_called()

    def test_disabled_policy_preserves_existing_art_behavior(self):
        self.f.config['art_autonomy']['enabled'] = False
        self.f.checked()
        state = self.state()
        state['task']['reworks'] = 3
        self.engine._save(state)
        self.f.providers.review.return_value = {'decision': 'rework', 'summary': 'repair', 'feedback': '', 'head_sha': HEAD}
        result = self.engine.step()
        self.assertEqual(result['phase'], 'BLOCKED')
        self.assertIn('返工次数上限', result['last_error'])
        self.f.providers.art_diagnose.assert_not_called()

    def test_review_blocked_is_given_to_author_to_distinguish_external_dependencies(self):
        self.f.checked()
        self.f.providers.review.return_value = {'decision': 'blocked', 'summary': 'Dependency?', 'feedback': '', 'head_sha': HEAD}
        self.assertEqual(self.engine.step()['phase'], 'ART_DIAGNOSE')
        self.assertEqual(self.state()['task']['art_diagnosis']['trigger'], 'review_blocked')

    def test_policy_rejects_unknown_keys_booleans_and_unbounded_windows(self):
        valid = {'enabled': True, 'max_cycles': 2, 'rework_window': 3}
        self.assertEqual(validate_art_autonomy_settings(valid), valid)
        invalid = [{**valid, 'tools': '*'}, {**valid, 'enabled': 1}, {**valid, 'max_cycles': True},
                   {**valid, 'max_cycles': 0}, {**valid, 'rework_window': 100}, {'enabled': True}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_art_autonomy_settings(value)

    def waiting_user(self):
        self.trigger()
        self.plan.update(action='needs_user', required_approvals=['style_reference'], questions=['What is authorized?'])
        result = self.engine.step()
        self.assertEqual(result['phase'], 'BLOCKED', result)
        state = self.state()
        receipt = Path(state['task']['art_diagnosis']['receipt_path'])
        return state, receipt, art_autonomy.file_digest(receipt)

    def test_response_queues_exact_words_without_controller_lock_or_state_change(self):
        state, receipt, sha = self.waiting_user()
        before = self.engine.state_path.read_bytes()
        words = '  用户原话\n边界不变，请自行重新考虑。  '
        with lock(self.engine.runtime / 'controller.lock'), patch.object(self.engine, '_locks', side_effect=AssertionError):
            first = self.engine.respond_art_diagnosis(fixtures.TASK, sha, words)
            second = self.engine.respond_art_diagnosis(fixtures.TASK, sha, words)
        self.assertEqual(first['status'], 'queued')
        self.assertFalse(first['idempotent'])
        self.assertTrue(second['idempotent'])
        self.assertEqual(first['response_id'], second['response_id'])
        self.assertEqual(self.engine.state_path.read_bytes(), before)
        queue = read_json(self.engine.runtime / 'art-diagnosis-responses.json')
        self.assertEqual(len(queue['responses']), 1)
        self.assertEqual(queue['responses'][0]['feedback'], words)
        with self.assertRaises(ValueError):
            self.engine.respond_art_diagnosis(fixtures.TASK, sha, 'different answer')

    def test_answer_restarts_author_diagnosis_not_old_execution_and_preserves_original_session(self):
        before, receipt, sha = self.waiting_user()
        words = '继续在现有范围内独立提出办法。'
        submitted = self.engine.respond_art_diagnosis(fixtures.TASK, sha, words)
        state = self.state()
        self.assertTrue(self.engine.consume_art_diagnosis_response(state))
        task = self.state()['task']
        self.assertEqual(state['phase'], 'ART_DIAGNOSE')
        self.assertEqual(task['art_diagnosis']['round'], 2)
        self.assertEqual(task['art_autonomy_epoch'], 1)
        self.assertEqual(task['art_diagnosis']['trigger'], 'user_response')
        self.assertEqual(task['session_id'], SESSION)
        self.assertEqual(task['reworks'], before['task']['reworks'])
        self.assertNotIn('autonomous_rework_allowance', task)
        self.assertEqual(task['art_request']['config_sha256'], before['task']['art_request']['config_sha256'])
        self.assertIn(words, task['body'])
        self.assertEqual(task['art_request']['body'], task['body'])
        directory = self.engine.runtime / 'tasks' / fixtures.TASK
        self.assertEqual(read_json(directory / 'task.json')['body'], task['body'])
        self.assertIn(words, (directory / 'TASK.md').read_text(encoding='utf-8'))
        self.assertEqual(self.engine.art_queue()['requests'][0]['body'], task['body'])
        self.assertEqual(art_autonomy.file_digest(receipt), sha)
        self.assertEqual(self.engine.respond_art_diagnosis(fixtures.TASK, sha, words)['status'], 'consumed')
        self.plan.update(action='continue', required_approvals=[], questions=[])
        self.assertEqual(self.f.restart().step()['phase'], 'CODE')
        self.assertEqual(self.state()['task']['session_id'], SESSION)
        self.assertIn(words, self.f.providers.art_diagnose.call_args.args[0])
        self.assertEqual(self.state()['accepted'], [])
        self.assertEqual(self.state().get('art_pending', []), [])
        self.assertEqual(self.state()['task']['art_diagnosis_responses'][0]['id'], submitted['response_id'])

    def test_wrong_or_changed_response_source_is_rejected(self):
        _, receipt, sha = self.waiting_user()
        for task_id, digest in [('TASK-ART-OTHER', sha), (fixtures.TASK, 'b' * 64)]:
            with self.subTest(task=task_id, sha=digest), self.assertRaises(ValueError):
                self.engine.respond_art_diagnosis(task_id, digest, 'user answer')
        package = self.f.evidence.public / 'review-package.json'
        package.write_bytes(package.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, '来源已变化'):
            self.engine.respond_art_diagnosis(fixtures.TASK, sha, 'user answer')
        self.assertFalse((self.engine.runtime / 'art-diagnosis-responses.json').exists())

    def test_source_change_after_enqueue_rejects_without_state_mutation_or_call(self):
        _, _, sha = self.waiting_user()
        self.engine.respond_art_diagnosis(fixtures.TASK, sha, 'user answer')
        before = self.engine.state_path.read_bytes()
        package = self.f.evidence.public / 'review-package.json'
        package.write_bytes(package.read_bytes() + b'\n')
        self.assertTrue(self.engine.consume_art_diagnosis_response(self.state()))
        self.assertEqual(self.engine.state_path.read_bytes(), before)
        queue = read_json(self.engine.runtime / 'art-diagnosis-responses.json')
        self.assertEqual(queue['responses'][0]['status'], 'rejected')
        self.assertEqual(self.f.providers.art_diagnose.call_count, 1)

    def test_state_save_before_response_ack_recovers_without_duplicate_context_or_epoch(self):
        _, _, sha = self.waiting_user()
        words = 'Unique user answer'
        self.engine.respond_art_diagnosis(fixtures.TASK, sha, words)
        queue_path = self.engine.runtime / 'art-diagnosis-responses.json'
        original = art_autonomy.write_json
        def crash_on_ack(path, value):
            if Path(path) == queue_path and value['responses'][0]['status'] == 'consumed':
                raise SystemExit('crash after authoritative state save')
            return original(path, value)
        with patch.object(art_autonomy, 'write_json', side_effect=crash_on_ack), self.assertRaises(SystemExit):
            self.engine.consume_art_diagnosis_response(self.state())
        saved = self.state()
        self.assertEqual(saved['phase'], 'ART_DIAGNOSE')
        self.assertEqual(read_json(queue_path)['responses'][0]['status'], 'queued')
        self.assertTrue(self.f.restart().consume_art_diagnosis_response(saved))
        task = self.state()['task']
        self.assertEqual(task['body'].count(words), 1)
        self.assertEqual(task['art_autonomy_epoch'], 1)
        self.assertEqual(len(task['art_diagnosis_responses']), 1)
        self.assertEqual(task['art_diagnosis']['round'], 2)
        self.assertEqual(read_json(queue_path)['responses'][0]['status'], 'consumed')
        self.assertFalse(self.engine.consume_art_diagnosis_response(self.state()))
        self.assertEqual(self.f.providers.art_diagnose.call_count, 1)

    def test_pause_and_busy_inbox_leave_response_queued(self):
        _, _, sha = self.waiting_user()
        self.engine.respond_art_diagnosis(fixtures.TASK, sha, 'user answer')
        pause = self.engine.runtime / 'PAUSE'
        pause.write_text('pause', encoding='utf-8')
        self.assertFalse(self.engine.consume_art_diagnosis_response(self.state()))
        pause.unlink()
        with lock(self.engine.runtime / 'art-queue.lock'):
            self.assertFalse(self.engine.consume_art_diagnosis_response(self.state()))
        self.assertEqual(read_json(self.engine.runtime / 'art-diagnosis-responses.json')['responses'][0]['status'], 'queued')

    def test_waiting_diagnoses_do_not_spend_execution_cycles_and_new_epoch_resets_ceiling(self):
        self.f.config['art_autonomy']['max_cycles'] = 1
        _, _, sha = self.waiting_user()
        counter_probe = self.state()
        self.assertTrue(self.engine._begin_art_diagnosis(counter_probe, 'counter probe', trigger='test'))
        self.assertEqual(counter_probe['phase'], 'ART_DIAGNOSE')
        self.assertEqual(counter_probe['task'].get('art_autonomy_epoch', 0), 0)
        self.engine.respond_art_diagnosis(fixtures.TASK, sha, 'Keep existing boundaries')
        self.engine.consume_art_diagnosis_response(self.state())
        self.plan.update(action='continue', required_approvals=[], questions=[])
        self.assertEqual(self.engine.step()['phase'], 'CODE')
        task = self.state()['task']
        self.assertEqual([row['execution_decision'] for row in task['art_diagnosis_history']], ['waiting', 'continue'])
        self.assertEqual([row['round'] for row in task['art_diagnosis_history']], [1, 2])
        state = self.state()
        self.assertTrue(self.engine._begin_art_diagnosis(state, 'exhausted', trigger='test'))
        self.assertEqual(state['phase'], 'BLOCKED')
        state['task']['art_autonomy_epoch'] += 1
        self.assertTrue(self.engine._begin_art_diagnosis(state, 'fresh explicit response epoch', trigger='test'))
        self.assertEqual(state['phase'], 'ART_DIAGNOSE')
        self.assertEqual(state['task']['art_diagnosis']['round'], 3)

    def test_receipts_written_before_state_save_recover_exactly_once(self):
        self.trigger()
        original = art_autonomy.write_json
        saved_receipts = []
        def crash_after_public(path, value):
            original(path, value)
            if Path(path).parent.name == 'diagnoses':
                saved_receipts.extend([Path(value['receipt_path']), Path(path)])
                raise SystemExit('crash after receipts, before state save')
        with patch.object(art_autonomy, 'write_json', side_effect=crash_after_public), self.assertRaises(SystemExit):
            self.engine.step()
        self.assertNotIn('art_diagnosis_history', self.state()['task'])
        bytes_before = [path.read_bytes() for path in saved_receipts]
        self.f.providers.recover.return_value = deepcopy(self.plan)
        self.assertEqual(self.f.restart().step()['phase'], 'CODE')
        self.assertEqual([path.read_bytes() for path in saved_receipts], bytes_before)
        self.assertEqual(len(self.state()['task']['art_diagnosis_history']), 1)
        self.assertEqual(self.state()['task']['reworks'], 4)
        self.assertEqual(self.f.providers.art_diagnose.call_count, 1)


if __name__ == '__main__':
    unittest.main()
