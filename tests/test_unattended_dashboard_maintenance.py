"""A deferred reload explains its own pause without claiming a model restart."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_dashboard_art as fixtures
from unattended import dashboard_art, dashboard_flows
from unattended.dashboard_state import OWNER
from unattended.storage import write_json


class ArtMaintenanceProjectionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ArtDashboardTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.state, _ = self.f.production_state("blender")
        self.config = dashboard_art.worker_config(self.f.main)
        self.runtime = Path(self.config["runtime_dir"])
        self.run = self.runtime / "runs" / ("code-" + "a" * 32); self.run.mkdir(parents=True)
        self.session = "550e8400-e29b-41d4-a716-446655440000"
        self.state["task"].update(session_id=self.session, session_owner_task_id=self.state["task"]["id"])
        self.state["operation"].update(output_dir=str(self.run), coding_provider="claude")
        write_json(self.runtime / "state.json", self.state)
        (self.run / "stdout.log").write_text(json.dumps({"type": "system", "subtype": "init", "session_id": self.session,
                                                        "model": "claude-opus-5-5", "private": "NEVER_PROJECT_NATIVE"}) + "\n")
        self.identity = {"pid": 123, "process_created": 456, "image": "owned-pythonw.exe"}
        self.request_id = "b" * 32
        self.pause = self.runtime / "PAUSE"; self.pause.write_bytes(("art-reload:" + self.request_id + "\n").encode())
        fact = self.pause.stat()
        self.request = {"version": 1, "id": self.request_id, "status": "waiting_boundary",
                        "config_file": str(Path(self.config["config_path"]).resolve()), "runtime_dir": str(self.runtime),
                        "config_sha256": hashlib.sha256(Path(self.config["config_path"]).read_bytes()).hexdigest(),
                        "source_identity": self.identity,
                        "pause": {"mode": "owned", "token": self.pause.read_text(), "fingerprint": {
                            "sha256": hashlib.sha256(self.pause.read_bytes()).hexdigest(), "mtime_ns": fact.st_mtime_ns, "size": fact.st_size}}}
        write_json(self.runtime / "art-reload-request.json", {"version": 1, "id": self.request_id})
        self.save_request()
        write_json(self.runtime / "background-health.json", {"owner": OWNER, "config": self.request["config_file"],
                   **self.identity, "status": "running", "heartbeat_epoch": self.f.now.timestamp()})
        self.inspect = patch("unattended.dashboard_state.process_identity", return_value=self.identity)
        self.inspect.start(); self.addCleanup(self.inspect.stop)

    def save_request(self):
        write_json(self.runtime / "reload-history" / (self.request_id + ".json"), self.request)

    def project(self):
        return dashboard_art.collect_art_snapshot(self.f.main, self.f.now)

    def test_owned_pause_explains_current_call_and_preserves_the_next_step_gate_without_writes(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.root.rglob("*") if p.is_file()}
        value = self.project()
        self.assertTrue(value["paused"])
        self.assertEqual(value["status"], "paused")
        self.assertTrue(value["maintenance"]["current_call_active"])
        self.assertIn("当前调用后自动切换", value["label"])
        flow = dashboard_flows.collect_flows(self.f.main_path, {"art": value})["art"]
        current = next(row for row in flow["nodes"] if row["current"])
        self.assertEqual(current["state"], "paused")
        self.assertIn("当前调用会自然结束", current["summary"])
        self.assertNotIn("NEVER_PROJECT_NATIVE", json.dumps(value))
        self.assertNotIn(self.request_id, json.dumps(value))
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.f.root.rglob("*") if p.is_file()})

    def test_stopping_starting_or_finished_call_never_claims_the_model_is_active(self):
        for stage in ("stopping", "starting"):
            self.request["status"] = stage; self.save_request()
            value = self.project()
            self.assertFalse(value["maintenance"]["current_call_active"])
            self.assertIn("等待后台接续", value["maintenance"]["summary"])
        self.request["status"] = "waiting_boundary"; self.save_request()
        write_json(self.run / "process.json", {"kind": "code", "exit_code": 0})
        self.assertFalse(self.project()["maintenance"]["current_call_active"])

    def test_foreign_binding_process_or_pause_cannot_be_misrepresented_as_owned_maintenance(self):
        baseline = deepcopy(self.request)
        for variant in ("config", "runtime", "config_sha", "source", "replacement", "user_pause", "fingerprint"):
            with self.subTest(variant=variant):
                self.request = deepcopy(baseline)
                if variant == "config": self.request["config_file"] += ".foreign"
                elif variant == "runtime": self.request["runtime_dir"] += "-foreign"
                elif variant == "config_sha": self.request["config_sha256"] = "e" * 64
                elif variant == "source": self.request["source_identity"]["process_created"] += 1
                elif variant == "replacement": self.request["replacement_identity"] = dict(self.request["source_identity"])
                elif variant == "user_pause": self.request["pause"]["mode"] = "user"
                else: self.request["pause"]["fingerprint"]["mtime_ns"] += 1
                self.save_request()
                self.assertIsNone(self.project()["maintenance"])

    def test_native_opening_is_required_for_the_current_call_message(self):
        (self.run / "stdout.log").write_text('{"type":"assistant","text":"not an opening"}\n')
        value = self.project()["maintenance"]
        self.assertFalse(value["current_call_active"])
        self.assertIn("等待安全边界", value["summary"])


if __name__ == "__main__":
    unittest.main()
