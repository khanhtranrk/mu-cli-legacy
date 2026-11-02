"""Focused entry/preparation and HTTP batch regression checks; CPU-only."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import yaml
from typer.testing import CliRunner

from mu.cli import app, _runtime_base, _execute_flow
from mu.runtime import Flow


def write(path, content):
    return {"type": "write_json", "path": str(path), "content": content}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = _runtime_base(self.root, self.root / "system", "test")

    def flow(self, spec, entry="direct_flow"):
        return Flow(spec, entry, self.base, [], self.root)

    def test_lazy_prepare_once_and_context_isolation_across_batches(self):
        prepare = {"type": "execute_command", "target": "python", "args": ["-c",
                   "from pathlib import Path; p=Path('count'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"],
                   "save_state": "setup"}
        spec = {"pipeline": {"used": {"prepare": [prepare], "params": {"name": {"type": "string"}},
                "nodes": [{"name": "save", "before_run": [write("{session[working_dir]}/{params[name]}.json",
                    {"id": "{request[requestId]}", "ready": "{actions[setup][success]}"})]}]},
                "unused": {"prepare": [write(self.root / "unused", {})], "nodes": []}}}
        flow = self.flow(spec)
        flow.initialize()
        for name in ("a", "b"):
            self.assertFalse(flow.execute([("used", {"name": name}, {"requestId": name, "specSlug": "demo"})]))
            self.assertEqual(json.loads((self.root / f"{name}.json").read_text()), {"id": name, "ready": True})
        flow.finalize(False)
        self.assertEqual((self.root / "count").read_text(), "x")
        self.assertFalse((self.root / "unused").exists())
        self.assertNotIn("request", flow.prepared["used"])
        self.assertNotIn("actions", self.base)

    def test_prepare_failure_is_cached_and_other_pipeline_executes(self):
        spec = {"pipeline": {"bad": {"prepare": [{"type": "execute_command", "target": "missing-mu-test-tool"}], "nodes": []},
                             "good": {"nodes": [{"name": "ok", "before_run": [write(self.root / "ok", {})]}]}}}
        flow = self.flow(spec)
        self.assertTrue(flow.execute([("bad", {}, {"requestId": "bad"}), ("good", {}, {"requestId": "good"})]))
        self.assertTrue((self.root / "ok").exists())
        self.assertIn("bad", flow.prepare_errors)

    def test_invalid_request_does_not_prepare(self):
        flow = self.flow({"pipeline": {"demo": {"params": {"name": {"type": "string"}},
                    "prepare": [write(self.root / "prepared", {})], "nodes": []}}})
        self.assertTrue(flow.execute([("demo", {"name": 1}, {})]))
        self.assertFalse((self.root / "prepared").exists())

    def test_initialization_failure_runs_all_finalization_and_preserves_error(self):
        spec = {"entries": {"direct_flow": {
            "initialization": [{"type": "execute_command", "target": "missing-init-tool"}],
            "finalization": [{"type": "execute_command", "target": "missing-final-tool"},
                             write(self.root / "finished", "{entry[status]}")]}}}
        with self.assertRaisesRegex(ValueError, "missing-init-tool"):
            _execute_flow(self.flow(spec), lambda: self.fail("body must not run"))
        self.assertEqual(json.loads((self.root / "finished").read_text()), "failed")

    def test_interruption_stops_subprocess_and_runs_finalization(self):
        import os
        import signal
        import subprocess
        import sys
        import time
        ready = self.root / "ready"
        spec = {"entries": {"direct_flow": {"finalization": [write(self.root / "cleanup", "{entry[status]}")]}},
                "pipeline": {"demo": {"nodes": [{"name": "slow", "before_run": [{"type": "execute_command",
                "target": sys.executable, "args": ["-c", f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(30)"]}]}]}}}
        (self.root / "spec.yaml").write_text(yaml.safe_dump(spec))
        (self.root / "input.yaml").write_text("{}")
        child = subprocess.Popen([sys.executable, "-m", "mu.cli", "run", "--spec", str(self.root / "spec.yaml"),
                "--pipeline", f"demo,{self.root / 'input.yaml'}", "--working_dir", str(self.root), "--device", "cuda,0,1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            os.kill(child.pid, signal.SIGINT)
            stdout, stderr = child.communicate(timeout=5)
            self.assertNotEqual(child.returncode, 0, stdout + stderr)
            self.assertEqual(json.loads((self.root / "cleanup").read_text()), "failed")
        finally:
            if child.poll() is None:
                child.kill(); child.communicate()

    def test_flow_runs_without_integrated_status_reporting(self):
        flow = self.flow({"entries": {"direct_flow": {"finalization": [write(self.root / "cleanup", {})]}},
                          "pipeline": {"demo": {"nodes": []}}})
        _execute_flow(flow, lambda: flow.execute([("demo", {}, {"requestId": "one"})]))
        self.assertTrue((self.root / "cleanup").exists())

    def test_master_flow_real_http_batches_and_metadata(self):
        self.check_master_flow_session(use_environment=False)

    def test_master_flow_session_uses_environment_when_options_are_absent(self):
        self.check_master_flow_session(use_environment=True)

    def test_cel_master_flow_real_http_snapshot_handoff(self):
        self.check_master_flow_session(use_environment=False, cel=True)

    def check_master_flow_session(self, *, use_environment, cel=False):
        root = self.root
        def notify(phase):
            return {"type": "send_http_request", "method": "post",
                    "url": "{session[server]}/test/action",
                    "header": {"Authorization": "Bearer {session[token]}", "Content-Type": "application/json"},
                    "body": {"phase": phase, "server": "{session[server]}"}}

        spec = {"entries": {"master_flow": {"initialization": [write(root / "init", {}), notify("initialization")],
                "finalization": [write(root / "final", "{entry[status]}"), notify("finalization")]}},
                "pipeline": {"demo": {"prepare": [write(root / "prepared", {}), notify("prepare")],
                "params": {"name": {"type": "string"}}, "nodes": [{"name": "save",
                "before_run": [write("{session[working_dir]}/{params[name]}.json", "{request[requestId]}"), notify("node")]}]}}}
        if cel:
            from mu.spec_migration import migrate_spec
            report = migrate_spec(spec)
            self.assertFalse(report["issues"])
            spec = report["spec"]
            spec["pipeline"]["demo"]["nodes"][0]["before_run"].append(write(str(root / "registry.json"), "${{ Request.Registry.N }}"))
        events, selections = [], []
        batches = [[{"requestId": str(i), "specSlug": "demo", "pipelineName": "demo", "assignmentVersion": 1, "payload": {"name": str(i)}}] for i in (1, 2)] + [[]]
        if cel:
            for batch in batches:
                for item in batch:
                    item["registryContext"] = {"Request": {"N": int(item["requestId"])*10}}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.assert_auth()
                assert "cel-v1" in self.headers["X-Mu-Expression-Engines"]
                events.append(self.path)
                self.send_response(200); self.end_headers(); self.wfile.write(yaml.safe_dump(spec).encode())
            def do_POST(self):
                self.assert_auth()
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                events.append((self.path, body))
                response = {"ok": True}
                if self.path.endswith("claim"):
                    assert (root / "init").exists()
                    selections.append(body); response = batches.pop(0)
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(response).encode())
            def assert_auth(self):
                assert self.headers["Authorization"] == "Bearer test-token"
                assert self.headers["User-Agent"].startswith("mu/")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_port}"
        args = ["master-flow", "run", "demo", "--unit-id", "worker", "--refresh-loop", "--working_dir", str(root),
                "--by-pipeline", "demo", "--by-pipeline", "other"]
        if use_environment:
            environment = {"MU_MASTER_FLOW_SERVER": endpoint + "/", "MU_MASTER_FLOW_TOKEN": "test-token"}
        else:
            args += ["--server", endpoint + "/", "--token", "test-token"]
            environment = {"MU_MASTER_FLOW_SERVER": "http://invalid", "MU_MASTER_FLOW_TOKEN": "wrong"}
        with patch("mu.cli.detect_devices", return_value=[]):
            result = CliRunner().invoke(app, args, env=environment)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(selections), 3)
        self.assertTrue(all(s["expressionEngine"] == ("cel-v1" if cel else "python-format-v1") for s in selections))
        if cel: self.assertEqual(json.loads((root / "registry.json").read_text()),20)
        self.assertIn("No matching requests", result.output)
        self.assertIn("Unit worker: offline", result.output)
        self.assertTrue(all(s["byStatus"] == ["waiting"] and s["byPipeline"] == ["demo", "other"] for s in selections))
        self.assertFalse(any(
            event[0].endswith("/api/requests/report")
            for event in events if isinstance(event, tuple)
        ))
        self.assertEqual(json.loads((root / "2.json").read_text()), "2")
        self.assertEqual(json.loads((root / "final").read_text()), "success")
        self.assertEqual(events[0][1]["status"], "online")
        self.assertEqual(events[-1][1]["status"], "offline")
        actions = [body for path, body in (event for event in events if isinstance(event, tuple)) if path == "/test/action"]
        self.assertEqual([action["phase"] for action in actions], ["initialization", "prepare", "node", "node", "finalization"])
        self.assertTrue(all(action["server"] == endpoint for action in actions))
        self.assertNotIn("test-token", result.output)
        self.assertNotIn("wrong", result.output)


if __name__ == "__main__":
    unittest.main()
