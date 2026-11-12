import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.bounded_dsh import GUARD, build_profile, normalize_events, run, validate_config
from lab_harness.dsh import DshError
from lab_harness.process import ProcessResult


class BoundedDshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / 'workspace'
        self.cwd.mkdir()
        install = self.root / 'desktop'
        (install / 'resources').mkdir(parents=True)
        (install / 'DeepSeek Harness.exe').touch()
        (install / 'resources/app.asar').touch()
        credential = self.root / 'empty-credentials.yaml'
        credential.write_text('{}', encoding='utf-8')
        self.config = {
            'verified_version': '0.2.1-rc.3', 'desktop_install': str(install),
            'credentials_path': str(credential),
            'usage_authorization': {'source': 'explicit_user_request',
                                    'scope': 'single_issue_smoke', 'user_text': 'test authorization'},
        }

    def fake_process(self, command, **kwargs):
        output = kwargs['output_dir']
        output.mkdir(parents=True, exist_ok=True)
        if command[-1] == '--version':
            stdout, stderr = output / 'stdout.log', output / 'stderr.log'
            stdout.write_text('0.2.1-rc.3\n', encoding='utf-8')
            stderr.write_text('', encoding='utf-8')
            return ProcessResult(0, False, False, stdout, stderr)
        report = {'protocol': 'bounded-dsh/v1', 'ready': True, 'visible_tool_count': 0,
                  'violations': [], 'request_count': 1, 'usage': {'inputTokens': 55, 'outputTokens': 12}}
        (output / 'guard-report.json').write_text(json.dumps(report), encoding='utf-8')
        events = [
            {'type': 'session', 'sessionId': 'session-fixture'},
            {'type': 'status', 'phase': 'turn_start', 'turn': 1},
            {'type': 'status', 'phase': 'turn_end', 'turn': 1, 'reason': {'kind': 'completed'}},
            {'type': 'final', 'text': 'def subtract(a, b):\n    return a - b\n'},
        ]
        stdout, stderr = output / 'stdout.log', output / 'stderr.log'
        stdout.write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
        stderr.write_text('', encoding='utf-8')
        return ProcessResult(0, False, False, stdout, stderr)

    def test_default_no_authorization_refuses_before_launch(self):
        self.config.pop('usage_authorization')
        with patch('lab_harness.bounded_dsh.run_process') as launch:
            with self.assertRaises(DshError):
                run('task', self.cwd, self.root / 'run', self.config)
            launch.assert_not_called()

    def test_prompt_and_timeout_bounds(self):
        with self.assertRaises(DshError):
            run('x' * 4097, self.cwd, self.root / 'run', self.config)
        for timeout in [0, -1, 121, True, '120']:
            with self.subTest(timeout=timeout), self.assertRaises(DshError):
                validate_config(self.config | {'timeout_seconds': timeout})

    def test_credentials_cannot_be_in_task_workspace(self):
        credential = self.cwd / 'credential.yaml'
        credential.write_text('{}', encoding='utf-8')
        with self.assertRaises(DshError):
            run('task', self.cwd, self.root / 'run', self.config | {'credentials_path': str(credential)})

    def test_env_file_refused(self):
        (self.cwd / '.env').write_text('SYNTHETIC=1', encoding='utf-8')
        with self.assertRaises(DshError):
            run('task', self.cwd, self.root / 'run', self.config)

    def test_fresh_profile_has_no_tools_other_consumers_or_user_bundles(self):
        profile = build_profile(self.root / 'home', credentials_path=Path(self.config['credentials_path']),
                                report_path=self.root / 'report.json', preflight_only=False)
        manifest = json.loads((profile / 'package.json').read_text())
        self.assertEqual(manifest['dsh']['profile']['bundles'], [])
        rows = json.loads((profile / 'cordis.patch.yml').read_text())[0]['insert']
        identities = {row['id'] for row in rows}
        self.assertFalse(any(i.startswith('tool-') for i in identities))
        self.assertTrue(identities.isdisjoint({'filesystem', 'subprocess', 'mcp-resources', 'llm-retry',
                                              'compaction-basic', 'session-title-llm', 'web', 'subagents'}))
        self.assertIn('boundedSmokePolicy', rows[-1]['inject'])
        with self.assertRaises(DshError):
            build_profile(self.root / 'home', credentials_path=Path(self.config['credentials_path']),
                          report_path=self.root / 'another.json', preflight_only=False)

    def test_success_returns_real_protocol_and_usage_fields(self):
        with patch('lab_harness.bounded_dsh.run_process', side_effect=self.fake_process):
            result = run('task', self.cwd, self.root / 'run', self.config)
        self.assertEqual(result['session_id'], 'session-fixture')
        self.assertEqual(result['request_count'], 1)
        self.assertEqual(result['usage']['outputTokens'], 12)
        self.assertIn('return a - b', result['final_text'])
        self.assertIsNone(result['cost_amount'])

    def test_missing_guard_report_refuses(self):
        def missing(*args, **kwargs):
            result = self.fake_process(*args, **kwargs)
            if args[0][-1] != '--version':
                (kwargs['output_dir'] / 'guard-report.json').unlink()
            return result
        with patch('lab_harness.bounded_dsh.run_process', side_effect=missing):
            with self.assertRaises(DshError):
                run('task', self.cwd, self.root / 'run', self.config)

    def test_policy_violation_or_second_request_refuses(self):
        for number, change in enumerate([{'request_count': 2}, {'violations': ['refused']}, {'visible_tool_count': 1}]):
            def modified(*args, **kwargs):
                result = self.fake_process(*args, **kwargs)
                if args[0][-1] == '--version':
                    return result
                path = kwargs['output_dir'] / 'guard-report.json'
                report = json.loads(path.read_text()) | change
                path.write_text(json.dumps(report), encoding='utf-8')
                return result
            with self.subTest(change=change), patch('lab_harness.bounded_dsh.run_process', side_effect=modified):
                with self.assertRaises(DshError):
                    run('task', self.cwd, self.root / f'run-{number}', self.config)

    def test_changed_installed_version_refuses_before_prompt_dispatch(self):
        def wrong_version(*args, **kwargs):
            result = self.fake_process(*args, **kwargs)
            result.stdout_path.write_text('different-version', encoding='utf-8')
            return result
        with patch('lab_harness.bounded_dsh.run_process', side_effect=wrong_version) as launch:
            with self.assertRaises(DshError):
                run('task', self.cwd, self.root / 'run', self.config)
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(launch.call_args.args[0][-1], '--version')

    def test_recorded_provider_metadata_is_normalized_without_changing_raw(self):
        fixture = Path(__file__).parent / 'fixtures/dsh_account_stdout.jsonl'
        raw = fixture.read_bytes()
        def recorded(*args, **kwargs):
            result = self.fake_process(*args, **kwargs)
            if args[0][-1] != '--version':
                result.stdout_path.write_bytes(raw)
            return result
        with patch('lab_harness.bounded_dsh.run_process', side_effect=recorded):
            result = run('task', self.cwd, self.root / 'run', self.config)
        self.assertEqual(Path(result['stdout_path']).read_bytes(), raw)
        self.assertEqual(result['provider_metadata_lines_removed'], 1)
        self.assertEqual(result['session_id'], 'session-recorded-fixture')
        self.assertIn('return left * right', result['final_text'])
        self.assertNotIn('[llm-deepseek-account]', Path(result['normalized_events_path']).read_text())
        process = json.loads(Path(result['process_result_path']).read_text())
        self.assertEqual(process['exit_code'], 0)
        self.assertIs(process['timed_out'], False)
        self.assertIs(process['output_limit_exceeded'], False)

    def test_unknown_non_json_preserves_process_evidence_and_fails(self):
        def corrupted(*args, **kwargs):
            result = self.fake_process(*args, **kwargs)
            if args[0][-1] != '--version':
                with result.stdout_path.open('a', encoding='utf-8') as stream:
                    stream.write('\nunknown warning to ignore\n')
            return result
        out = self.root / 'run'
        with patch('lab_harness.bounded_dsh.run_process', side_effect=corrupted):
            with self.assertRaisesRegex(DshError, 'Unknown non-JSON'):
                run('task', self.cwd, out, self.config)
        saved = json.loads((out / 'process-result.json').read_text())
        self.assertEqual(saved['exit_code'], 0)
        self.assertIn('unknown warning', (out / 'stdout.log').read_text())

    def test_metadata_allowlist_does_not_hide_other_origins_or_failures(self):
        session = '{"type":"session","sessionId":"fixture"}\n'
        for index, line in enumerate([
            '[llm-deepseek-account] model listing from https://evil.test/models loaded 2 model(s), skipped 0 row(s)',
            '[llm-deepseek-account] model listing from https://api.deepseek.com/models failed',
            '[llm-deepseek-account] model listing from https://api.deepseek.com/models loaded 2 model(s), skipped 0 row(s) EXTRA',
        ]):
            raw = self.root / f'unknown-{index}.log'
            raw.write_text(session + line, encoding='utf-8')
            with self.subTest(line=line), self.assertRaises(DshError):
                normalize_events(raw, self.root / f'events-{index}.jsonl')

    def test_metadata_after_final_is_not_ignored(self):
        raw = self.root / 'late.log'
        raw.write_text('{"type":"final","text":""}\n'
                       '[llm-deepseek-account] model listing from https://api.deepseek.com/models loaded 2 model(s), skipped 0 row(s)',
                       encoding='utf-8')
        with self.assertRaisesRegex(DshError, 'outside the active'):
            normalize_events(raw, self.root / 'normalized.jsonl')

    def test_secret_environment_not_inherited(self):
        def inspect(*args, **kwargs):
            self.assertNotIn('GITHUB_TOKEN', kwargs['environment'])
            self.assertNotIn('DEEPSEEK_BASE_URL', kwargs['environment'])
            self.assertEqual(kwargs['environment']['ELECTRON_RUN_AS_NODE'], '1')
            return self.fake_process(*args, **kwargs)
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'SYNTHETIC', 'DEEPSEEK_BASE_URL': 'https://invalid.test'}):
            with patch('lab_harness.bounded_dsh.run_process', side_effect=inspect):
                run('task', self.cwd, self.root / 'run', self.config)


class GuardJavascriptTests(unittest.TestCase):
    def test_request_guard_boundaries_in_real_node(self):
        exe = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/DeepSeek Harness/DeepSeek Harness.exe'
        if not exe.is_file():
            self.skipTest('Official desktop Node runtime is unavailable')
        script = f"""
import {{createPolicy}} from {json.dumps(GUARD.as_uri())};
import assert from 'node:assert/strict';
const config = {{provider:'deepseek-account', model:'deepseek-flash', maxRequests:1,
 maxTokens:512, maxInputBytes:8192, preflightOnly:false}};
const request = {{provider:'deepseek-account', model:'deepseek-flash', maxTokens:512,
 reasoningEffort:'off', tools:[], messages:[{{role:'user',content:[{{type:'text',text:'task'}}]}}]}};
const policy=createPolicy(config); policy.inspect(request);
assert.equal(policy.state.request_count,1);
assert.throws(()=>policy.inspect(request),/second model request/);
for(const change of [{{tools:[{{name:'pwsh'}}]}},{{maxTokens:513}},{{reasoningEffort:'high'}},
 {{provider:'another'}},{{model:'another'}},{{messages:[{{role:'user',content:[{{type:'image'}}]}}]}},
 {{messages:[{{role:'user',content:[{{type:'text',text:'x'.repeat(8193)}}]}}]}}]){{
 const p=createPolicy(config); assert.throws(()=>p.inspect({{...request,...change}}));
 assert.equal(p.state.request_count,0);
}}
const pre=createPolicy({{...config,preflightOnly:true}});
assert.throws(()=>pre.inspect(request),/preflight/);
const usage=createPolicy(config); usage.observe({{type:'usage',usage:{{inputTokens:10,outputTokens:5,unknown:'secret'}}}});
assert.deepEqual(usage.state.usage,{{inputTokens:10,outputTokens:5}});
console.log('guard boundaries passed');
"""
        environment = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
        result = subprocess.run([str(exe), '--input-type=module', '-e', script],
                                capture_output=True, text=True, env=environment, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('guard boundaries passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
