"""Claude authors the UE MCP config; only native connection/call evidence closes the follow-up."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from tests import test_unattended_claude_user_repair as fixture
from unattended import providers


class ClaudeUEFollowupTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ClaudeUserRepairTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        for relative in ("Plugins/Experimental/ModelContextProtocol", "Plugins/Experimental/Toolsets/AllToolsets"):
            (self.f.engine / relative).mkdir(parents=True)
        self.path = self.f.runtime / "user-followups" / ("f" * 32) / "request.json"
        source = deepcopy(self.f.state["operation"])
        source["output_dir"] = str(self.f.runtime / "runs" / ("code-" + "e" * 32))
        request = {"version": 1, "id": "f" * 32, "task_id": self.f.task_id, "repair_id": self.f.profile["id"],
                   "repair_request_sha256": self.f.profile["request_sha256"], "session_id": fixture.SESSION,
                   "source_operation": source, "source_head": source["head_sha"], "user_instruction": "Let Claude connect UE MCP itself.",
                   "authorized_at": "2026-11-08T13:00:00+00:00", "max_turns": 3}
        fixture.save(self.path, request)
        self.profile = {key: request[key] for key in ("version", "id", "task_id", "repair_id", "repair_request_sha256",
                                                     "session_id", "source_head", "max_turns")}
        self.profile.update(request_path=str(self.path), request_sha256=fixture.sha(self.path), status="active",
                            activated_at="2026-11-08T13:00:01+00:00")
        self.f.state["task"]["user_followup"] = deepcopy(self.profile)
        self.f.state["task"]["user_followup_attempts"] = []
        self.f.config["_user_followup_profile"] = deepcopy(self.profile)
        self.turn(1)

    def turn(self, number):
        self.f.out = self.f.runtime / "runs" / ("code-followup-" + str(number))
        self.f.state["operation"].update(output_dir=str(self.f.out), user_followup_id=self.profile["id"],
            user_followup_request_sha256=self.profile["request_sha256"], user_followup_turn=number)
        self.f.state["task"]["user_followup_attempts"].append({"id": self.profile["id"], "turn": number,
            "output_dir": str(self.f.out), "started_at": "2026-11-08T13:00:02+00:00"})
        self.f.persist_state()
        self.f.rows = fixture.stream()

    def config(self, url="http://127.0.0.1:55123/user-chosen-mcp", name="model_selected_ue"):
        value = {"mcpServers": {name: {"type": "http", "url": url}}}
        fixture.save(self.f.work / ".mcp.json", value)
        return value

    def native(self, *, connected=True, invoked=True, error=False, server="model_selected_ue", result=True):
        tool = "mcp__" + server + "__list_toolsets"
        for row in self.f.rows:
            if row.get("type") == "system" and row.get("subtype") == "init":
                row.update(mcp_servers=[{"name": server, "status": "connected" if connected else "failed"}], tools=[tool])
        if invoked:
            blocks = [fixture.event("assistant", message={"content": [{"type": "tool_use", "id": "toolu_local_1",
                "name": tool, "input": {"do_not_log": "private tool contents"}}]})]
            if result:
                blocks.append(fixture.event("user", message={"content": [{"type": "tool_result", "tool_use_id": "toolu_local_1",
                                "is_error": error, "content": "private server output"}]}))
            self.f.rows[2:2] = blocks

    def test_missing_config_turn_can_author_its_own_file_then_next_turn_loads_exact_bytes(self):
        original_capture = self.f.capture
        def capture(*args, **kwargs):
            self.config()  # Simulates the actual model's project edit inside its first turn.
            return original_capture(*args, **kwargs)
        self.f.launch.side_effect = capture
        first = self.f.invoke()
        self.assertFalse(first["user_followup"]["completed"])
        argv = self.f.launch.call_args.args[0]
        self.assertEqual(argv[argv.index("--mcp-config") + 1], '{"mcpServers":{}}')
        self.assertIn("mcp__*", argv[argv.index("--disallowedTools"):argv.index("--allowedTools")])
        self.turn(2); self.native(); self.f.launch.side_effect = self.f.capture
        second = self.f.invoke()
        self.assertTrue(second["user_followup"]["completed"])
        argv = self.f.launch.call_args.args[0]
        snapshot = Path(argv[argv.index("--mcp-config") + 1])
        self.assertEqual(snapshot.read_bytes(), (self.f.work / ".mcp.json").read_bytes())
        self.assertIn("mcp__model_selected_ue__*", argv[argv.index("--allowedTools"):])
        self.assertNotIn("mcp__*", argv[argv.index("--disallowedTools"):argv.index("--allowedTools")])
        self.assertTrue((self.f.out / "ue-mcp-evidence.json").is_file())
        raw = (self.f.out / "stdout.log").read_text()
        self.assertNotIn("private tool contents", raw); self.assertNotIn("private server output", raw)

    def test_only_connected_real_completed_tool_call_satisfies_followup(self):
        self.config()
        for index, flags in enumerate((dict(invoked=False), dict(connected=False), dict(error=True), dict(result=False),
                                       dict(server="foreign_server")), 1):
            # Separate fixture-owned output directories; profile is bounded by
            # the actual provider turn, not by the number of test alternatives.
            self.turn(1); self.f.out = self.f.runtime / "runs" / ("code-case-" + str(index))
            self.f.state["operation"]["output_dir"] = str(self.f.out)
            self.f.state["task"]["user_followup_attempts"][-1]["output_dir"] = str(self.f.out); self.f.persist_state()
            self.native(**flags)
            with self.subTest(flags=flags): self.assertFalse(self.f.invoke()["user_followup"]["completed"])

    def test_local_only_config_rejects_remote_credentials_stdio_and_multiple_servers_without_deadlocking_author(self):
        values = [{"mcpServers": {"ue": {"type": "http", "url": "http://example.com:1234/mcp"}}},
                  {"mcpServers": {"ue": {"type": "http", "url": "http://user:secret@127.0.0.1:1234/mcp"}}},
                  {"mcpServers": {"ue": {"type": "stdio", "command": "dangerous.exe"}}},
                  {"mcpServers": {"ue": {"type": "http", "url": "http://127.0.0.1:1234/mcp", "headers": {"Authorization": "secret"}}}},
                  {"mcpServers": {"one": {}, "two": {}}}]
        for index, value in enumerate(values):
            fixture.save(self.f.work / ".mcp.json", value)
            selection, argument = providers._claude_local_mcp_config(self.f.work, self.f.runtime / "scratch" / str(index))
            self.assertIsNone(selection["server_name"]); self.assertTrue(selection["validation_error"])
            self.assertNotIn("secret", selection["validation_error"])
            self.assertEqual(argument, '{"mcpServers":{}}')
        self.f.invoke()  # Invalid config still reaches the native author to fix it.
        self.f.launch.assert_called_once()

    def test_recovery_binds_loaded_snapshot_not_later_project_edits_and_cannot_forge_mcp_proof(self):
        self.config(); self.native(); result = self.f.invoke()
        self.config(url="http://localhost:55124/new-model-choice")
        self.assertEqual(self.f.provider.recover("code", self.f.out), result)
        sidecar = self.f.out / "ue-mcp-evidence.json"
        proof = json.loads(sidecar.read_text()); proof["completed"] = False; fixture.save(sidecar, proof)
        with self.assertRaisesRegex(providers.ProviderError, "sidecar"): self.f.provider.recover("code", self.f.out)
        fixture.save(sidecar, result["user_followup"])
        (self.f.out / "ue-mcp-config.json").write_text('{"mcpServers":{}}')
        with self.assertRaises(providers.ProviderError): self.f.provider.recover("code", self.f.out)

    def test_followup_requires_exact_profile_session_attempt_and_bound_three_turn_cap(self):
        original = deepcopy(self.f.state)
        for key, value in (("user_followup_turn", 4), ("user_followup_id", "0" * 32),
                           ("user_followup_request_sha256", "0" * 64), ("requested_session_id", fixture.SOURCE)):
            self.f.state = deepcopy(original); self.f.state["operation"][key] = value; self.f.persist_state()
            with self.subTest(key=key), self.assertRaises(providers.ProviderError): self.f.invoke()
        self.f.launch.assert_not_called()

    def test_extra_shell_permissions_are_exact_project_or_model_owned_entrypoints_and_source_is_readonly(self):
        self.f.invoke(); argv = self.f.launch.call_args.args[0]; prompt = self.f.launch.call_args.kwargs["stdin"].decode()
        allowed = argv[argv.index("--allowedTools") + 1:]
        bash = [item for item in allowed if item.startswith("Bash(")]
        self.assertFalse(any(item in {"Bash(*)", "Bash(powershell *)", "Bash(python *)"} for item in bash))
        self.assertTrue(any("ue-mcp/connect.py" in item and item.endswith(" *)") for item in bash))
        self.assertTrue(any("ue-mcp/connect.ps1" in item and "-File" in item for item in bash))
        self.assertTrue(any("UnrealEditor.exe" in item and "ZhuGuoCity.uproject" in item and item.endswith(" *)") for item in bash))
        self.assertFalse(any("Stop-Process" in item or "taskkill" in item for item in bash))
        permissions = json.loads(argv[argv.index("--settings") + 1])["permissions"]
        for path in (self.f.engine / "Plugins/Experimental/ModelContextProtocol", self.f.engine / "Plugins/Experimental/Toolsets/AllToolsets"):
            self.assertIn(str(path), permissions["additionalDirectories"])
        self.assertIn("KILL_ON_JOB_CLOSE", prompt); self.assertIn("不得关闭用户已有UE", prompt)
        self.assertIn("不是UE/MCP内部执行代码的文件系统沙箱", prompt)

    def complete_then_normal_code(self):
        self.config(); self.native(); result = self.f.invoke()
        receipt = {"id": self.profile["id"], "task_id": self.f.task_id, "request_sha256": self.profile["request_sha256"],
                   "operation": deepcopy(self.f.state["operation"]), "result": result}
        path = self.path.parent / "turn-1.json"; fixture.save(path, receipt)
        self.f.state["task"]["user_followup_results"] = [{"id": self.profile["id"], "turn": 1, "completed": True,
                                                        "receipt_path": str(path), "receipt_sha256": fixture.sha(path)}]
        self.profile["status"] = "completed"
        self.f.state["task"]["user_followup"] = deepcopy(self.profile)
        self.f.config["_user_followup_profile"] = deepcopy(self.profile)
        self.f.state["operation"].pop("user_followup_turn")
        self.f.out = self.f.runtime / "runs" / "code-normal-reuse"
        self.f.state["operation"]["output_dir"] = str(self.f.out); self.f.persist_state()
        self.f.rows = fixture.stream()

    def test_completed_followup_reuses_verified_same_server_without_new_turn_or_mandatory_call(self):
        self.complete_then_normal_code()
        before = deepcopy(self.f.state["task"]["user_followup_attempts"])
        result = self.f.invoke()
        argv = self.f.launch.call_args.args[0]
        self.assertIn("mcp__model_selected_ue__*", argv)
        self.assertFalse(result["user_followup"]["completed"])  # Ordinary CODE need not call MCP again.
        self.assertEqual(self.f.state["task"]["user_followup_attempts"], before)
        record = json.loads((self.f.out / "process.json").read_text())
        self.assertTrue(record["user_followup_reuse"]); self.assertIsNone(record["user_followup_turn"])
        self.assertEqual(self.f.provider.recover("code", self.f.out), result)

    def test_active_followup_scope_clarification_does_not_exempt_completed_normal_code(self):
        self.complete_then_normal_code()
        active_prompt = self.f.launch.call_args.kwargs["stdin"].decode()
        marker = "本追加接入回合的最终范围说明"
        self.assertIn(marker, active_prompt)
        self.assertGreater(active_prompt.index(marker), active_prompt.index("本追加回合允许无新提交"))
        self.assertIn("优先于前文普通修复CODE的交付和自测要求", active_prompt)
        self.assertIn("配置就绪后即可如实交接并结束本回合", active_prompt)
        self.assertIn("不必因仅有MCP配置提交而重跑全量M、原5核心、19项Local或正式PackageIntegration", active_prompt)
        self.assertIn("完整运行一次独立CHECK", active_prompt)
        self.f.invoke()
        normal_prompt = self.f.launch.call_args.kwargs["stdin"].decode()
        self.assertNotIn(marker, normal_prompt)
        self.assertNotIn("不在此回合重复原完整游戏修复", normal_prompt)
        self.assertIn("普通CODE仍须按原任务提交真实变更并通过全部独立检查", normal_prompt)

    def test_completed_followup_cannot_silently_change_endpoint_or_server(self):
        self.complete_then_normal_code()
        self.config(url="http://127.0.0.1:55222/different", name="another_ue")
        result = self.f.invoke(); argv = self.f.launch.call_args.args[0]
        self.assertEqual(argv[argv.index("--mcp-config") + 1], '{"mcpServers":{}}')
        self.assertNotIn("mcp__another_ue__*", argv)
        self.assertFalse(result["user_followup"]["completed"])
        self.assertIn("differs", result["user_followup"]["evidence"]["validation_error"])


if __name__ == "__main__":
    unittest.main()
