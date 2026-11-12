"""Repair check evidence is selected by a bound mirror and a fixed manifest."""
import base64
from copy import deepcopy
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
import re
import threading
import unittest
from unittest.mock import Mock

from tests import test_unattended_dashboard_user_repair as fixtures
from unattended import dashboard, dashboard_evidence as evidence


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lS8AAAAASUVORK5CYII=")


class RepairCheckEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.UserRepairDashboardTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.base, self.task = self.f.base, self.f.task
        self.identity = "b" * 32
        self.directory = self.base.runtime / "tasks" / self.task["id"] / "full-repair-checks" / self.identity
        self.receipt = self.directory / "receipt.json"
        self.mirror = self.base.runtime / "workspace/Saved/Automation" / self.task["id"] / "full-repair-results.json"
        self.files = {}
        def file(relative, data):
            path = self.directory / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            self.files[relative] = path
            return {"relative_path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        records = [file("build/UnrealBuildTool.log", b"build output\n"), file("regression/stdout.log", b"x" * 70000 + b"\nACTUAL_REGRESSION_TAIL\n"),
                   file("visual/case-001/stdout.log", b"Local.First actual UE output\n"),
                   file("visual/case-002/Unreal.log", b"Local.Second actual UE output\n"),
                   file("visual/case-001/process.json", b'{"exit_code":0,"argv":["DO_NOT_EXPOSE"]}'),
                   file("package/package-evidence/loop-Slot97.zgs", b"ACTUAL_SLOT97")]
        images = [file("visual/case-001/images/first.png", PNG), file("visual/case-002/images/second.png", PNG),
                  file("package/package-evidence/plain.png", PNG)]
        self.row = {"version": 1, "id": self.identity, "task_id": self.task["id"], "authorization_id": self.f.identity,
            "request_sha256": self.task["user_repair"]["request_sha256"], "head_sha": "a" * 40, "scope": "user_repair_check",
            "engine_dir": str(self.base.root / "UE"), "workspace": str(self.base.runtime / "workspace"),
            "status": "failed", "passed": False, "started_at": self.base.now.isoformat(), "finished_at": self.base.now.isoformat(), "updated_at": self.base.now.isoformat(),
            "stages": {"build": {"passed": True}, "regression": {"passed": False}, "visual": {"passed": False,
                "cases": [{"name": "Local.First", "status": "PASS"}, {"name": "Local.Second", "status": "FAIL"}]}, "package": {"passed": True}},
            "evidence": records + images, "images": images}
        self.bind()

    def bind(self):
        self.base.write(self.receipt, self.row)
        self.base.write(self.mirror, {key: self.row[key] for key in
            ("version", "id", "task_id", "authorization_id", "request_sha256", "head_sha", "scope", "status", "passed", "updated_at")})
        mirror = json.loads(self.mirror.read_text())
        mirror.update(receipt_path=str(self.receipt), receipt_sha256=hashlib.sha256(self.receipt.read_bytes()).hexdigest())
        self.base.write(self.mirror, mirror)

    def detail(self, lane="development", node="code"):
        return self.f.detail(lane, node)

    def read(self, ref):
        return evidence.read_node_evidence(self.base.config_path, "development", "code", ref)

    def test_every_actual_local_image_and_bound_stage_log_is_readable_and_sources_stay_separate(self):
        detail = self.detail()
        images = [row for row in detail["artifacts"] if row["kind"] == "png"]
        self.assertEqual(len(images), 3)
        self.assertTrue(any("Local.First" in row["title"] for row in images))
        self.assertTrue(any("Local.Second" in row["title"] for row in images))
        for row in images: self.assertEqual(self.read(row["id"])[1], "image/png")
        log = next(row for row in detail["logs"] if "regression" in row["title"])
        self.assertIn(b"ACTUAL_REGRESSION_TAIL", self.read(log["id"])[0])
        self.assertLessEqual(len(self.read(log["id"])[0]), 65536)
        slot = next(row for row in detail["artifacts"] if row["kind"] == "binary")
        self.assertEqual(self.read(slot["id"]), (b"ACTUAL_SLOT97", "application/octet-stream", "loop-Slot97.zgs"))
        process = next(row for row in detail["artifacts"] if row["title"].endswith("process.json"))
        self.assertNotIn(b"DO_NOT_EXPOSE", self.read(process["id"])[0])
        self.row["scope"] = "user_repair_selftest"
        self.mirror = self.mirror.with_name("selftest-results.json"); self.bind()
        detail = self.detail()
        self.assertTrue(any("制作自测（不替代独立检查）" in row["title"] for row in detail["artifacts"]))

    def test_foreign_authorization_head_and_mirror_digest_never_expose_check_files(self):
        original = deepcopy(self.row)
        for key in ("task_id", "head_sha", "authorization_id", "request_sha256", "workspace", "scope", "digest"):
            with self.subTest(key=key):
                self.row = deepcopy(original)
                if key != "digest": self.row[key] = "FOREIGN"
                self.bind()
                if key == "digest":
                    mirror = json.loads(self.mirror.read_text()); mirror["receipt_sha256"] = "f" * 64; self.base.write(self.mirror, mirror)
                detail = self.detail()
                self.assertIn("待核实", detail["artifact_notice"])
                self.assertFalse(any(row["kind"] == "png" for row in detail["artifacts"]))
                self.assertTrue(any("事件摘要" in row["title"] for row in detail["logs"]))

    def test_outside_and_native_paths_are_not_exposed_and_changed_files_or_receipt_invalidate_refs(self):
        for relative in ("../outside.png", "visual/case-001/../../native/stdout.log", "native/analysis/stdout.log", "visual/elsewhere.png"):
            self.row["evidence"].append({"relative_path": relative, "sha256": "a" * 64, "size_bytes": 1})
        self.bind(); detail = self.detail()
        self.assertEqual(len([row for row in detail["artifacts"] if row["kind"] == "png"]), 3)
        log = next(row for row in detail["logs"] if "Local.First" in row["title"])
        path = self.files["visual/case-001/stdout.log"]; path.write_bytes(b"changed actual UE output".ljust(path.stat().st_size, b"!"))
        with self.assertRaisesRegex(ValueError, "SHA"): self.read(log["id"])
        image = next(row for row in detail["artifacts"] if row["kind"] == "png")
        self.row["status"] = "passed"; self.bind()
        with self.assertRaisesRegex(ValueError, "范围已变化"): self.read(image["id"])

    def test_running_selftest_exposes_only_existing_owned_logs_and_append_does_not_break_selection(self):
        self.row.update(scope="user_repair_selftest", status="running", passed=False, evidence=[], images=[],
                        operation=deepcopy(self.f.state["operation"]), inventory={"visual": {name: {"images": ["expected.png"]} for name in ("Local.First", "Local.Second", "Local.NotRun")}})
        self.row.pop("finished_at")
        self.mirror = self.mirror.with_name("selftest-results.json"); self.bind()
        detail = self.detail()
        self.assertFalse(any(row["kind"] == "png" for row in detail["artifacts"]))
        self.assertFalse(any("Local.NotRun" in row["title"] for row in detail["logs"]))
        log = next(row for row in detail["logs"] if "Local.First" in row["title"])
        self.assertIn("尚无终态SHA", log["title"])
        with self.files["visual/case-001/stdout.log"].open("ab") as stream: stream.write(b"APPENDED_CURRENT_OUTPUT\n")
        self.assertIn(b"APPENDED_CURRENT_OUTPUT", self.read(log["id"])[0])
        self.f.state["operation"]["started_at"] = "2026-11-02T11:01:00+00:00"; self.f.f.save()
        with self.assertRaisesRegex(ValueError, "范围已变化"): self.read(log["id"])

    def test_full_local_gallery_does_not_consume_the_current_provider_log_budget(self):
        records, images = [], []
        for index in range(1, 20):
            folder = self.directory / "visual" / f"case-{index:03d}"
            for suffix in ("stdout.log", "images/a.png", "images/b.png", "images/c.png", "images/d.png"):
                path = folder / suffix; path.parent.mkdir(parents=True, exist_ok=True)
                data = PNG if path.suffix == ".png" else b"Local actual log\n"; path.write_bytes(data)
                row = {"relative_path": path.relative_to(self.directory).as_posix(), "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
                records.append(row)
                if path.suffix == ".png": images.append(row)
        self.row.update(evidence=records, images=images)
        self.row["stages"]["visual"]["cases"] = [{"name": "Local." + str(index)} for index in range(1, 20)]
        self.bind(); detail = self.detail()
        self.assertEqual(len([row for row in detail["artifacts"] if row["kind"] == "png"]), 76)
        self.assertTrue(any("事件摘要" in row["title"] for row in detail["logs"]))

    def test_only_two_fixed_package_failure_artifacts_are_added_with_manifest_sha(self):
        for name, data in (("raw-package-summary.json", b'{"passed":false,"reason":"environment"}'), ("raw-package-uat.log", b"Actual formal package failure output\n")):
            path = self.directory / "package/package-evidence" / name
            path.write_bytes(data)
            self.row["evidence"].append({"relative_path": path.relative_to(self.directory).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)})
        self.bind(); detail = self.detail()
        raw = next(row for row in detail["logs"] if row["title"].endswith("raw-package-uat.log"))
        self.assertIn(b"Actual formal package failure", self.read(raw["id"])[0])
        summary = next(row for row in detail["artifacts"] if row["title"].endswith("raw-package-summary.json"))
        self.assertFalse(json.loads(self.read(summary["id"])[0])["passed"])
        self.assertIsNone(evidence._repair_check_member("package/package-evidence/another-script.py"))


@unittest.skipUnless(os.environ.get("UNATTENDED_TEST_BROWSER") == "1", "opt-in isolated headless browser")
class RepairCheckEvidenceBrowserTests(unittest.TestCase):
    def test_local_log_image_and_download_links_are_safe_on_mobile(self):
        from playwright.sync_api import expect, sync_playwright
        fixture = RepairCheckEvidenceTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        cache = Mock(); cache.get.side_effect = fixture.f.snapshot; cache.flow_detail.side_effect = fixture.detail
        cache.flow_evidence.side_effect = lambda lane, node, ref: fixture.read(ref)
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(cache, {}))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page(viewport={"width": 390, "height": 844}); writes, errors = [], []
                def read_only(route):
                    if route.request.method in {"GET", "HEAD"}: route.continue_()
                    else: writes.append(route.request.method); route.abort()
                page.route("**/*", read_only); page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/#development"); page.locator("#user-repair-detail").click()
                page.locator("#flow-detail-logs button", has_text="Local.Second").click()
                expect(page.locator("#flow-evidence-text")).to_contain_text("Local.Second actual UE output")
                page.locator("#flow-detail-artifacts button", has_text="first.png").click()
                expect(page.locator("#flow-evidence-image")).to_be_visible()
                self.assertTrue(page.locator("#flow-evidence-image").evaluate("img => img.complete && img.naturalWidth > 0"))
                page.locator("#flow-detail-artifacts button", has_text="loop-Slot97.zgs").click()
                expect(page.locator("#flow-evidence-download")).to_have_attribute("href", re.compile(r"^http://127\.0\.0\.1:\d+/api/flow/evidence\?"))
                self.assertTrue(page.locator("#flow-evidence-download").evaluate("link => new URL(link.href).origin === location.origin"))
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                self.assertFalse(writes); self.assertFalse(errors); browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__":
    unittest.main()
