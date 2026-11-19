"""Shared SG routing must reach every client and override inherited bypass rules."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.request import Request, build_opener

from github_harness_lab.lab_harness.process import ProcessResult
from tests.test_unattended_art_tripo import Response
from tests.test_unattended_providers import CODEX_EVENTS, DSH_EVENTS, PLAN, write_events
from unattended import network, notifications
from unattended.art_tripo import TripoClient
from unattended.providers import AgentProviders, _claude_environment


class NetworkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "worker" / "config.json"
        self.config.parent.mkdir()
        self.central = self.root / "central" / "secrets.local.json"
        self.central.parent.mkdir()
        self.route = self.central.with_name(network.NETWORK_NAME)
        self.url = "http://127.0.0.1:17893"
        self.route.write_text(json.dumps({"proxy_url": self.url, "region": "SG"}), encoding="utf-8")
        self.config.write_text(json.dumps({"secrets_file": "../central/secrets.local.json"}), encoding="utf-8")
        self.central.write_text(json.dumps({"ANTHROPIC_API_KEY": "anthropic-test-credential", "TRIPO_API_KEY": "tripo-test-credential"}),
                                encoding="utf-8")

    def test_central_route_applies_to_upper_and_lowercase_environment_and_handler(self):
        environment = network.proxy_environment(config_path=self.config)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            self.assertEqual(environment[name], self.url)
        self.assertEqual(environment["NO_PROXY"], "localhost,127.0.0.1,::1")
        self.assertEqual(environment["no_proxy"], environment["NO_PROXY"])
        self.assertEqual(environment["NODE_USE_ENV_PROXY"], "1")
        handler = network.proxy_handler(config_path=self.config)
        self.assertEqual(handler.proxies, {"http": self.url, "https": self.url})
        claude = _claude_environment({"auth_mode": "api_key"}, config_path=self.config)
        self.assertEqual(claude["HTTPS_PROXY"], self.url)
        self.assertEqual(claude["ANTHROPIC_API_KEY"], "anthropic-test-credential")

    def test_explicit_missing_or_invalid_network_configuration_never_becomes_direct(self):
        self.config.write_text(json.dumps({"network_file": "missing.json"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            network.proxy_handler(config_path=self.config)
        for value in ({}, {"proxy_url": self.url, "region": "US"},
                      {"proxy_url": "http://user:password@127.0.0.1:17893", "region": "SG"},
                      {"proxy_url": "socks5://127.0.0.1:17893", "region": "SG"}):
            self.route.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(ValueError):
                network.proxy_environment(network_file=self.route)
        self.route.unlink()
        self.assertEqual(network.proxy_environment(secrets_file=self.central), {})

    def test_large_source_configuration_is_supported_while_route_file_remains_bounded(self):
        self.config.write_text(json.dumps({"secrets_file": "../central/secrets.local.json",
                                           "instructions": "x" * (1024 * 1024)}), encoding="utf-8")
        self.assertGreater(self.config.stat().st_size, 1024 * 1024)
        self.assertEqual(network.proxy_environment(config_path=self.config)["HTTPS_PROXY"], self.url)
        self.route.write_text(json.dumps({"proxy_url": self.url, "region": "SG"}) + " " * (1024 * 1024),
                              encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unreadable or invalid"):
            network.proxy_handler(config_path=self.config)

    def test_configured_proxy_ignores_external_bypass_but_preserves_loopback(self):
        handler = network.proxy_handler(network_file=self.route)
        with patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
            request = Request("https://api.example.test/responses")
            handler.proxy_open(request, self.url, "https")
            self.assertEqual(request.host, "127.0.0.1:17893")
            self.assertEqual(request._tunnel_host, "api.example.test")
            local = Request("http://127.0.0.1:8080/health")
            handler.proxy_open(local, self.url, "http")
            self.assertEqual(local.host, "127.0.0.1:8080")

    def test_local_proxy_receives_external_http_request_even_when_environment_bypasses_everything(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"proxied")
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.route.write_text(json.dumps({"proxy_url": f"http://127.0.0.1:{server.server_port}", "region": "SG"}),
                                  encoding="utf-8")
            handler = network.proxy_handler(network_file=self.route)
            with patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
                with build_opener(handler).open("http://external.invalid/evidence", timeout=2) as response:
                    self.assertEqual(response.read(), b"proxied")
            self.assertEqual(received, ["http://external.invalid/evidence"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_codex_and_dsh_normal_entry_points_receive_the_shared_route(self):
        provider = AgentProviders({"config_path": str(self.config), "network_file": str(self.route),
                                   "codex": {"command": ["codex.exe"]}, "dsh": {"command": ["dsh.exe"]}})
        environments = []
        def capture(argv, **kwargs):
            environments.append(kwargs["environment"])
            out = kwargs["output_dir"]
            write_events(out / "stdout.log", DSH_EVENTS if argv[0] == "dsh.exe" else CODEX_EVENTS)
            (out / "stderr.log").write_text("", encoding="utf-8")
            if "-o" in argv:
                Path(argv[argv.index("-o") + 1]).write_text(json.dumps(PLAN), encoding="utf-8")
            return ProcessResult(0, False, False, out / "stdout.log", out / "stderr.log")
        with patch("unattended.providers.run_process", side_effect=capture):
            provider.plan("Plan", self.root, self.root / "plan", 30)
            provider.code("Code", self.root, self.root / "code", 30)
        self.assertEqual(len(environments), 2)
        for environment in environments:
            self.assertEqual(environment["HTTPS_PROXY"], self.url)
            self.assertEqual(environment["NO_PROXY"], network.NO_PROXY)

    def test_feishu_and_tripo_api_and_download_use_the_same_proxy_handler(self):
        opener = MagicMock()
        response = MagicMock()
        response.status, response.read.return_value = 200, b'{"code":0}'
        opener.open.return_value.__enter__.return_value = response
        with patch.object(notifications.request, "build_opener", return_value=opener) as factory:
            self.assertEqual(notifications._post("https://open.feishu.cn/open-apis/bot/v2/hook/abcdefghijklmnop", {},
                                                  config_path=self.config), (True, None))
            self.assertEqual(factory.call_args.args[1].proxies["https"], self.url)
        client = TripoClient(self.root / "batch", self.central, network_file=self.route)
        tripo = MagicMock()
        tripo.open.return_value = Response(b'{"code":0}')
        with patch("unattended.art_tripo.urllib.request.build_opener", return_value=tripo) as factory:
            self.assertEqual(client._api_json("GET", "/task/test"), {"code": 0})
            self.assertEqual(factory.call_args.args[0].proxies["https"], self.url)
            tripo.open.return_value = Response(b"model-data", url="https://cdn.example/model.glb")
            client._download_one("https://cdn.example/model.glb", self.root / "model.glb")
            self.assertEqual(factory.call_args.args[0].proxies["https"], self.url)


if __name__ == "__main__":
    unittest.main()
