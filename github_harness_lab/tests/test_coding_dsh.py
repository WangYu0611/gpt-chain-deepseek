"""Offline code-provider checks; only the pure guard runs in official Node."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lab_harness.coding_dsh import (
    GUARD, MAX_INPUT_BYTES, MAX_OUTPUT_TOKENS, MAX_PROMPT_BYTES, PROFILE,
    build_profile, run, validate_config,
)
from lab_harness.dsh import DshError
from lab_harness.process import ProcessResult, safe_environment


class CodingDshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        install = self.root / "synthetic-desktop"
        (install / "resources").mkdir(parents=True)
        (install / "DeepSeek Harness.exe").touch()
        (install / "resources/app.asar").touch()
        self.credential = self.root / "synthetic-empty-account.yaml"
        self.credential.write_text("{}", encoding="utf-8")
        self.config = {"verified_version": "0.2.1-rc.3", "desktop_install": str(install),
            "credentials_path": str(self.credential), "usage_authorization": {
                "source": "explicit_user_request", "scope": "continuous_project_code", "user_text": "offline fixture"}}
        self.guard = {"protocol": "bounded-code-dsh/v1", "ready": True, "preflight_only": False,
            "preflight_validated": False, "provider": "deepseek-account", "model": "deepseek-flash",
            "request_count": 1, "max_requests": 1, "max_tokens": 8192, "max_input_bytes": 524288,
            "visible_tool_count": 0, "tool_calls_blocked": 0, "violations": [],
            "usage": {"inputTokens": 5000, "outputTokens": 700}, "finish_kind": "stop"}
        self.events = [{"type": "session", "sessionId": "code-session-fixture"},
            {"type": "status", "phase": "turn_start", "turn": 1},
            {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
            {"type": "final", "text": '{"schema":"harness-code-change/v1"}'}]

    def process(self, command, **kwargs):
        output = kwargs["output_dir"]
        output.mkdir(parents=True, exist_ok=True)
        stdout, stderr = output / "stdout.log", output / "stderr.log"
        stderr.write_text("", encoding="utf-8")
        if command[-1] == "--version":
            stdout.write_text("0.2.1-rc.3\n", encoding="utf-8")
        else:
            self.assertEqual(command[-2:], ["--profile", PROFILE])
            self.assertEqual(kwargs["environment"]["ELECTRON_RUN_AS_NODE"], "1")
            self.assertEqual(kwargs["cwd"], self.cwd)
            (output / "guard-report.json").write_text(json.dumps(self.guard), encoding="utf-8")
            stdout.write_text("\n".join(json.dumps(event) for event in self.events), encoding="utf-8")
        return ProcessResult(0, False, False, stdout, stderr)

    def execute(self, name="run", prompt="offline task", **kwargs):
        with patch("lab_harness.coding_dsh.run_process", side_effect=self.process):
            return run(prompt, self.cwd, self.root / name, self.config, **kwargs)

    def test_current_scope_and_new_prompt_timeout_bounds(self):
        self.assertEqual((MAX_PROMPT_BYTES, MAX_INPUT_BYTES, MAX_OUTPUT_TOKENS), (196608, 524288, 8192))
        for scope in (None, "single_issue_smoke", "another"):
            config = copy.deepcopy(self.config)
            config["usage_authorization"]["scope"] = scope
            with self.subTest(scope=scope), patch("lab_harness.coding_dsh.run_process") as process:
                with self.assertRaises(DshError):
                    run("task", self.cwd, self.root / "no-dispatch", config)
                process.assert_not_called()
        for timeout in (0, -1, 301, True, "300", float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(DshError):
                validate_config(self.config | {"timeout_seconds": timeout})
        for timeout in (1, 120, 300):
            validate_config(self.config | {"timeout_seconds": timeout})
        for index, prompt in enumerate(("x" * (MAX_PROMPT_BYTES + 1), "中" * (MAX_PROMPT_BYTES // 3 + 1), "\ud800", "")):
            with self.subTest(index=index), patch("lab_harness.coding_dsh.run_process") as process:
                with self.assertRaises(DshError):
                    run(prompt, self.cwd, self.root / "oversized", self.config)
                process.assert_not_called()
        result = self.execute(prompt="x" * MAX_PROMPT_BYTES)
        self.assertEqual(result["mode"], "bounded-tool-free-code-headless")
        self.assertEqual(result["request_count"], 1)

    def test_profile_has_new_guard_dependency_and_no_tools_retries_or_extra_model_consumers(self):
        home = self.root / "home"
        profile = build_profile(home, credentials_path=self.credential,
                                report_path=self.root / "report.json", preflight_only=False)
        manifest = json.loads((profile / "package.json").read_text())
        self.assertEqual(manifest["dsh"]["profile"]["bundles"], [])
        rows = json.loads((profile / "cordis.patch.yml").read_text())[0]["insert"]
        identities = {row["id"] for row in rows}
        self.assertEqual(len(identities), len(rows))
        self.assertFalse(any(name.startswith("tool-") for name in identities))
        self.assertTrue(identities.isdisjoint({"filesystem", "subprocess", "llm-retry", "subagents", "web",
                                               "session-title-llm", "compaction-basic", "mcp-resources"}))
        guard = next(row for row in rows if row["id"] == "bounded-code-policy")
        self.assertEqual(guard["name"], GUARD.as_uri())
        self.assertEqual(guard["config"]["maxTokens"], 8192)
        self.assertEqual(guard["config"]["maxInputBytes"], 524288)
        self.assertEqual(guard["config"]["maxRequests"], 1)
        self.assertEqual(rows[-1]["inject"], ["boundedCodePolicy"])
        with self.assertRaises(DshError):
            build_profile(home, credentials_path=self.credential, report_path=self.root / "other.json", preflight_only=False)

    def test_success_keeps_supervisor_evidence_and_does_not_read_credentials(self):
        original_read = Path.read_text
        def guarded_read(path, *args, **kwargs):
            self.assertNotEqual(path, self.credential)
            return original_read(path, *args, **kwargs)
        with patch.object(Path, "read_text", guarded_read):
            result = self.execute()
        self.assertEqual(result["session_id"], "code-session-fixture")
        self.assertEqual(result["final_text"], self.events[-1]["text"])
        self.assertEqual(result["usage"]["outputTokens"], 700)
        self.assertIsNone(result["cost_amount"])
        self.assertEqual(json.loads(Path(result["process_result_path"]).read_text())["exit_code"], 0)

    def test_old_or_malformed_guard_evidence_is_rejected(self):
        original = copy.deepcopy(self.guard)
        bad = [{"protocol": "bounded-dsh/v1"}, {"ready": False}, {"request_count": True},
               {"visible_tool_count": False}, {"request_count": 2}, {"max_requests": 2},
               {"max_tokens": 512}, {"max_input_bytes": 8192}, {"tool_calls_blocked": 1},
               {"violations": ["denied"]}, {"provider": "other"}, {"preflight_only": True}]
        for index, change in enumerate(bad):
            self.guard = original | change
            with self.subTest(change=change), self.assertRaises(DshError):
                self.execute(f"bad-{index}")
        self.guard = original
        for index, raw in enumerate(("[]", '{"request_count":1,"request_count":1}', "{}", "x" * 32769)):
            def corrupt(*args, **kwargs):
                result = self.process(*args, **kwargs)
                if args[0][-1] != "--version":
                    (kwargs["output_dir"] / "guard-report.json").write_text(raw, encoding="utf-8")
                return result
            with self.subTest(raw=index), patch("lab_harness.coding_dsh.run_process", side_effect=corrupt), self.assertRaises(DshError):
                run("task", self.cwd, self.root / f"malformed-{index}", self.config)

    def test_preflight_requires_zero_requests_and_proven_guard_rejection(self):
        self.config.pop("usage_authorization")
        self.guard.update(preflight_only=True, preflight_validated=True, request_count=0)
        result = self.execute(preflight_only=True)
        self.assertEqual(result["preflight"], "PASS")
        self.assertIs(result["model_called"], False)
        self.guard["request_count"] = 1
        with self.assertRaises(DshError):
            self.execute("bad-preflight", preflight_only=True)

    def test_version_change_and_timeout_fail_with_process_evidence(self):
        def wrong_version(*args, **kwargs):
            result = self.process(*args, **kwargs)
            result.stdout_path.write_text("different version", encoding="utf-8")
            return result
        with patch("lab_harness.coding_dsh.run_process", side_effect=wrong_version) as process, self.assertRaises(DshError):
            run("task", self.cwd, self.root / "wrong-version", self.config)
        self.assertEqual(process.call_count, 1)
        def timed_out(*args, **kwargs):
            result = self.process(*args, **kwargs)
            if args[0][-1] != "--version":
                return ProcessResult(-1, True, False, result.stdout_path, result.stderr_path)
            return result
        with patch("lab_harness.coding_dsh.run_process", side_effect=timed_out), self.assertRaises(DshError):
            run("task", self.cwd, self.root / "timeout", self.config)
        facts = json.loads((self.root / "timeout/process-result.json").read_text())
        self.assertIs(facts["timed_out"], True)

    def test_tool_events_and_unknown_stdout_are_rejected(self):
        self.events.insert(2, {"type": "tool_call", "name": "pwsh"})
        with self.assertRaises(DshError):
            self.execute()
        self.events.pop(2)
        def unknown(*args, **kwargs):
            result = self.process(*args, **kwargs)
            if args[0][-1] != "--version":
                with result.stdout_path.open("a", encoding="utf-8") as stream:
                    stream.write("\nunknown output\n")
            return result
        with patch("lab_harness.coding_dsh.run_process", side_effect=unknown), self.assertRaises(DshError):
            run("task", self.cwd, self.root / "unknown", self.config)
        self.assertTrue((self.root / "unknown/process-result.json").is_file())

    def test_local_environment_and_credential_boundaries(self):
        def inspect(*args, **kwargs):
            self.assertNotIn("GITHUB_TOKEN", kwargs["environment"])
            self.assertNotIn("DEEPSEEK_API_KEY", kwargs["environment"])
            self.assertNotIn("DEEPSEEK_BASE_URL", kwargs["environment"])
            return self.process(*args, **kwargs)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "SYNTHETIC", "DEEPSEEK_API_KEY": "SYNTHETIC", "DEEPSEEK_BASE_URL": "https://invalid.test"}):
            with patch("lab_harness.coding_dsh.run_process", side_effect=inspect):
                run("task", self.cwd, self.root / "environment", self.config)
        local = self.cwd / "synthetic-account.yaml"
        local.write_text("{}", encoding="utf-8")
        with patch("lab_harness.coding_dsh.run_process") as process:
            with self.assertRaises(DshError):
                run("task", self.cwd, self.root / "local-account", self.config | {"credentials_path": str(local)})
            (self.cwd / ".env").write_text("SYNTHETIC=1", encoding="utf-8")
            with self.assertRaises(DshError):
                run("task", self.cwd, self.root / "env-file", self.config)
            process.assert_not_called()


class CodingGuardJavascriptTests(unittest.TestCase):
    def test_pure_guard_limits_and_public_hooks_in_official_node(self):
        executable = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/DeepSeek Harness/DeepSeek Harness.exe"
        if not executable.is_file():
            self.skipTest("Official desktop Node runtime is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "guard.json"
            script = f"""
import {{createPolicy, apply}} from {json.dumps(GUARD.as_uri())};
import assert from 'node:assert/strict';
import {{readFileSync}} from 'node:fs';
globalThis.fetch=()=>{{throw Error('Network is forbidden in this offline test')}};
const config={{provider:'deepseek-account',model:'deepseek-flash',maxRequests:1,
 maxTokens:8192,maxInputBytes:524288,preflightOnly:false,reportPath:{json.dumps(str(report))}}};
const messages=text=>[{{role:'user',content:[{{type:'text',text}}]}}];
const request={{provider:'deepseek-account',model:'deepseek-flash',maxTokens:8192,
 reasoningEffort:'off',tools:[],messages:messages('code task')}};
const overhead=Buffer.byteLength(JSON.stringify(messages('')),'utf8');
const exact=createPolicy(config);
exact.inspect({{...request,messages:messages('x'.repeat(524288-overhead))}});
assert.equal(exact.state.observed_input_bytes,524288);
assert.equal(exact.state.protocol,'bounded-code-dsh/v1');
assert.throws(()=>exact.inspect(request),/second model request/);
for(const delta of [{{maxTokens:512}},{{maxInputBytes:8192}},{{maxRequests:2}}])
 assert.throws(()=>createPolicy({{...config,...delta}}));
for(const delta of [{{tools:[{{name:'pwsh'}}]}},{{maxTokens:8193}},{{reasoningEffort:'high'}},
 {{provider:'other'}},{{model:'other'}},{{messages:[{{role:'assistant',content:[{{type:'text',text:'resume'}}]}}]}},
 {{messages:[{{role:'user',content:[{{type:'image'}}]}}]}},
 {{messages:messages('x'.repeat(524289-overhead))}}]){{
 const policy=createPolicy(config);assert.throws(()=>policy.inspect({{...request,...delta}}));
 assert.equal(policy.state.request_count,0);
}}
const pre=createPolicy({{...config,preflightOnly:true}});
assert.throws(()=>pre.inspect(request),/preflight/);
const hooks=new Map();let guard,service,mask,nextCalls=0;
const ctx={{tools:{{guard:fn=>{{guard=fn}},schemas:()=>[]}},
 on:(name,fn)=>hooks.set(name,fn),provide:(name,value)=>{{service={{name,value}}}}}};
apply(ctx,config);
assert.equal(service.name,'boundedCodePolicy');
assert.equal(service.value.protocol,'bounded-code-dsh/v1');
const agent={{ctx:{{tools:{{restrict:value=>{{mask=value}}}}}}}};
hooks.get('agent/created')({{agent}});assert.deepEqual(mask,{{allow:[]}});
await hooks.get('agent/pre-step')({{agent}},async()=>{{nextCalls++}});
const pinned=await hooks.get('agent/request')({{}},async()=>({{provider:'other',maxTokens:9}}));
assert.equal(pinned.maxTokens,8192);assert.equal(pinned.provider,'deepseek-account');
const stream=hooks.get('llm/stream')(request,async function*(){{
 nextCalls++;yield {{type:'usage',usage:{{inputTokens:123,outputTokens:45,unknown:'discard'}}}};
 yield {{type:'finish',reason:{{kind:'stop'}}}};
}});
assert.equal(JSON.parse(readFileSync(config.reportPath,'utf8')).request_count,1);
for await(const _ of stream){{}}
assert.throws(()=>hooks.get('llm/stream')(request,()=>{{throw Error('must never reach provider')}}),/second/);
const saved=JSON.parse(readFileSync(config.reportPath,'utf8'));
assert.deepEqual(saved.usage,{{inputTokens:123,outputTokens:45}});assert.equal(saved.finish_kind,'stop');
assert.match(guard(),/no tool execution/);
const denied=JSON.parse(readFileSync(config.reportPath,'utf8'));assert.equal(denied.tool_calls_blocked,1);
const preHooks=new Map();let preNext=0;
apply({{tools:{{guard:()=>{{}},schemas:()=>[]}},on:(name,fn)=>preHooks.set(name,fn),provide:()=>{{}}}},
 {{...config,preflightOnly:true}});
assert.deepEqual(await preHooks.get('agent/pre-step')({{agent}},async()=>{{preNext++}}),{{kind:'reject'}});
assert.equal(preNext,0);
const preSaved=JSON.parse(readFileSync(config.reportPath,'utf8'));
assert.equal(preSaved.request_count,0);assert.equal(preSaved.preflight_validated,true);
console.log('code guard offline checks passed');
"""
            environment = safe_environment()
            environment["ELECTRON_RUN_AS_NODE"] = "1"
            result = subprocess.run([str(executable), "--input-type=module", "-e", script],
                                    capture_output=True, text=True, env=environment, cwd=directory, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("code guard offline checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
