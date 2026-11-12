"""Offline protocol checks; no network, provider or real authorization used."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from lab_harness.serial_smoke import (
    ARM_MARKER, EXPERIMENT, REPORT_MARKER, TASK_MARKER,
    generate_task, task_snapshot, validate_document,
)
from lab_harness.smoke_policy import ACTOR, REPOSITORY, digest


DOCS = {
    1: '# TASK_M0_1\n\n## 项目目标\n验证 GPT 连续派工与 DSH 执行，完成两份文档并提交报告。\n\n## 完成标准\n前两个任务完成后，GPT 自动发布第三个任务。\n',
    2: '# TASK_M0_2\n\n## 待办清单\n- 核对第一份项目简介和任务报告内容。\n- 编写第二份文档并提交真实执行报告。\n- 接收验收结果，确认第三个任务自动发布。\n',
}
BASE = 'a' * 40
HEAD = 'b' * 40


class FakeClient:
    def __init__(self, issue, arm):
        self.issue, self.arm = issue, arm
        self.prs = []
        self.previous = {
            'user': {'login': 'github-actions[bot]'},
            'head': {'sha': HEAD, 'repo': {'full_name': REPOSITORY}},
            'body': REPORT_MARKER + json.dumps({
                'experiment': EXPERIMENT, 'nonce': 'offline-nonce',
                'task_number': 1, 'checks': 'PASS', 'head_sha': HEAD,
            }),
        }

    def get_issue(self, number):
        assert number == self.issue['number']
        return copy.deepcopy(self.issue)

    def list_comments(self, number):
        assert number == 5
        return [copy.deepcopy(self.arm)]

    def get_ref(self, branch):
        assert branch == 'main'
        return {'object': {'sha': BASE}}

    def get_pr(self, number):
        assert number == 8
        return copy.deepcopy(self.previous)

    def _list(self, path, params):
        assert path == '/pulls'
        return copy.deepcopy(self.prs)


def fixture(number=1):
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
    arm_record = dict(experiment=EXPERIMENT, authorized=True, nonce='offline-nonce',
                      workflow_sha=BASE, expires_at=expiry, max_model_requests=2)
    arm_body = ARM_MARKER + json.dumps(arm_record)
    arm = dict(id=901, body=arm_body, user={'login': ACTOR},
               created_at='2026-10-28T00:00:00Z', updated_at='2026-10-28T00:00:00Z')
    config = dict(enabled=True, experiment=EXPERIMENT, nonce='offline-nonce',
                  workflow_sha=BASE, expires_at=expiry, arm_comment_id=901,
                  arm_body_sha256=digest(arm_body), dsh={'fixture_only': True})
    task = dict(experiment=EXPERIMENT, number=number, nonce='offline-nonce',
                control_issue=5, dispatch=number != 3,
                previous_pr=8 if number > 1 else None,
                previous_head=HEAD if number > 1 else None)
    issue = dict(number=7 + number, title=f'TASK_M0_{number}', state='open',
                 user={'login': ACTOR}, body=TASK_MARKER + json.dumps(task))
    event = dict(action='opened', issue=copy.deepcopy(issue), sender={'login': ACTOR},
                 repository={'full_name': REPOSITORY, 'private': True})
    env = dict(GITHUB_ACTIONS='true', GITHUB_REPOSITORY=REPOSITORY,
               GITHUB_EVENT_NAME='issues', GITHUB_ACTOR=ACTOR,
               GITHUB_RUN_ATTEMPT='1', GITHUB_WORKFLOW_SHA=BASE, GITHUB_RUN_ID='900000')
    return config, event, env, FakeClient(issue, arm)


class SerialSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def generator(self, number=1):
        return Mock(return_value=dict(final_text=DOCS[number], exit_code=0, request_count=1,
                                      session_id='OFFLINE-fixture', model='deepseek-flash',
                                      version='0.2.1-rc.3', usage={}))

    def test_both_document_tasks_produce_receipts(self):
        for number in (1, 2):
            with self.subTest(number=number):
                config, event, env, client = fixture(number)
                generate = self.generator(number)
                result, path = generate_task(config, event, env, root=self.root,
                                             client=client, generate=generate)
                self.assertEqual(result['document'], DOCS[number])
                self.assertEqual(result['task_id'], f'TASK_M0_{number}')
                self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['model_requests'], 1)
                generate.assert_called_once()

    def test_task_three_never_calls_model(self):
        config, event, env, client = fixture(3)
        generate = self.generator()
        with self.assertRaises(ValueError):
            generate_task(config, event, env, root=self.root, client=client, generate=generate)
        generate.assert_not_called()

    def test_false_predecessor_is_rejected_before_billing(self):
        for field in ('actor', 'head', 'checks'):
            config, event, env, client = fixture(2)
            if field == 'actor':
                client.previous['user']['login'] = ACTOR
            elif field == 'head':
                client.previous['head']['sha'] = 'c' * 40
            else:
                client.previous['body'] = client.previous['body'].replace('PASS', 'FAIL')
            generate = self.generator(2)
            with self.subTest(field=field), self.assertRaises(ValueError):
                generate_task(config, event, env, root=self.root, client=client, generate=generate)
            generate.assert_not_called()

    def test_changed_issue_hash_is_rejected(self):
        _, _, _, client = fixture()
        before = digest(client.issue['body'])
        client.issue['body'] += '\nEdited task instructions'
        with self.assertRaisesRegex(ValueError, 'Task changed'):
            task_snapshot(client, client.issue['number'], 'offline-nonce', expected_body_sha=before)

    def test_mid_generation_issue_change_prevents_result_publication(self):
        config, event, env, client = fixture()
        result = self.generator().return_value
        def change_issue(*args):
            client.issue['body'] += '\nchanged while generating'
            return result
        with self.assertRaisesRegex(ValueError, 'Task changed'):
            generate_task(config, event, env, root=self.root, client=client, generate=change_issue)
        self.assertFalse(list(self.root.rglob('result.json')))

    def test_durable_claim_prevents_repeat_billing(self):
        config, event, env, client = fixture()
        generate = self.generator()
        generate_task(config, event, env, root=self.root, client=client, generate=generate)
        with self.assertRaises(FileExistsError):
            generate_task(config, event, env, root=self.root, client=client, generate=generate)
        generate.assert_called_once()

    def test_existing_report_and_rerun_prevent_billing(self):
        for kind in ('report', 'rerun'):
            config, event, env, client = fixture()
            if kind == 'report':
                client.prs = [{'number': 8}]
            else:
                env['GITHUB_RUN_ATTEMPT'] = '2'
            generate = self.generator()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                generate_task(config, event, env, root=self.root, client=client, generate=generate)
            generate.assert_not_called()

    def test_document_structure_and_scope_are_checked(self):
        for number, text in [(1, DOCS[1].replace('## 完成标准', '## 缺少小节')),
                             (2, DOCS[2] + '- 多余第四项\n'),
                             (1, DOCS[1] + 'https://example.test'),
                             (1, '```\n' + DOCS[1] + '```'),
                             (3, DOCS[1])]:
            with self.subTest(number=number, text=text), self.assertRaises(ValueError):
                validate_document(text, number)


if __name__ == '__main__':
    unittest.main()
