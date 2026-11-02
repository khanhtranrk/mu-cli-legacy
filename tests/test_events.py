"""Lifecycle hook regressions; no network, GPU or model downloads."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from mu import runtime as rt


def hook(name, **extra):
    return {"on": name, "do": [{"type": "write_json", "path": "unused", **extra}]}


class EventTests(unittest.TestCase):
    def execute(self, spec, requests=None):
        seen = []
        original = rt.run_action
        def action(item, context, *args, **kwargs):
            if "event" in context:
                self.assertNotIn("private", context["actions"])
                seen.append((deepcopy(context["event"]), deepcopy(context.get("request")), item.get("tag")))
                context["actions"]["private"] = True
                if item.get("break_hook"):
                    raise ValueError("hook unavailable")
                return
            return original(item, context, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch.object(rt, "run_action", side_effect=action):
            flow = rt.Flow(spec, "direct_flow", {"sys": {"cache_dir": directory}}, [], Path(directory))
            failed = flow.execute(requests or [("p", {}, {"requestId": "r"})])
        return failed, seen

    def test_final_results_skip_ignore_finally_and_blocked(self):
        spec = {"events": [hook(n) for n in rt.EVENT_NAMES], "pipeline": {"p": {"nodes": [
            {"name": "bad", "finally": [{"type": "inspect_file", "target": "/mu-missing-finally"}]},
            {"name": "blocked", "depends_on": ["bad"]},
            {"name": "ignored", "ignore_error": True, "finally": [{"type": "inspect_file", "target": "/mu-missing-finally"}]},
            {"name": "skip", "skip_if": {"operator": "folder_exists", "path": "/tmp"}},
        ]}}}
        failed, seen = self.execute(spec)
        self.assertTrue(failed)
        ended = {e["node"]["name"]: e for e, _, _ in seen if e["name"] == "node_finished"}
        self.assertEqual(len(ended), 4)
        self.assertEqual(ended["bad"]["node"]["status"], "failed")
        self.assertTrue(ended["blocked"]["node"]["blocked"])
        self.assertTrue(ended["ignored"]["node"]["ignored_error"])
        self.assertEqual(ended["ignored"]["node"]["status"], "success")
        self.assertTrue(ended["skip"]["node"]["skipped"])
        self.assertFalse(any(e["name"] == "node_started" and e["node"]["name"] == "blocked" for e, _, _ in seen))
        self.assertEqual(seen[-1][0]["name"], "pipeline_finished")
        self.assertEqual(seen[-1][0]["pipeline"]["status"], "failed")
        self.assertTrue(all(e["pipeline"]["name"] == "p" for e, _, _ in seen))

    def test_scope_order_hook_failure_and_context_isolation(self):
        seen = []
        spec = {"events": [hook("node_finished", tag="root", break_hook=True)], "pipeline": {"p": {
            "events": [hook("node_finished", tag="pipeline")],
            "nodes": [{"name": "a", "events": [hook("node_finished", tag="node")]}]}}}
        failed, seen = self.execute(spec)
        self.assertFalse(failed)
        self.assertEqual([tag for _, _, tag in seen], ["root", "pipeline", "node"])
        # Actual routines and actions are executed without generating nested lifecycle events.
        with tempfile.TemporaryDirectory() as directory:
            spec = {"routines": {"r": {"actions": [{"type": "write_json", "path": str(Path(directory)/"out"), "content": "${{ event.name }}"}]}},
                    "events": [{"on": "node_finished", "do": [{"type": "execute_routine", "routine": "r"}]}],
                    "pipeline": {"p": {"nodes": [{"name": "a"}]}}}
            rt.execute_pipelines(spec, [("p", {})], {}, [], Path(directory))
            self.assertEqual((Path(directory)/"out").read_text().strip(), '"node_finished"')

    def test_preparation_parameter_failure_empty_and_multiple_requests(self):
        for definition in ({"params": {"x": {"type": "string"}}},
                           {"prepare": [{"type": "inspect_file", "target": "/mu-missing"}]}, {}):
            spec = {"events": [hook("pipeline_started"), hook("pipeline_finished")], "pipeline": {"p": definition}}
            failed, seen = self.execute(spec, [("p", {}, {"requestId": "one"}), ("p", {}, {"requestId": "two"})])
            self.assertEqual(len(seen), 4)
            self.assertEqual(sorted(r["requestId"] for e, r, _ in seen if e["name"] == "pipeline_finished"), ["one", "two"])
            self.assertEqual(failed, bool(definition))

    def test_request_finishes_without_waiting_for_other_request(self):
        completed = threading.Event()
        spec = {"events": [hook("pipeline_finished")], "pipeline": {"p": {"params": {"slow": {"type": "boolean"}}, "nodes": [
            {"name": "n", "run": [{"type": "execute_command", "target": "unused"}]}]}}}
        def action(item, context, *args, **kwargs):
            if "event" in context:
                if not context["params"]["slow"]:
                    completed.set()
            elif context["params"]["slow"]:
                self.assertTrue(completed.wait(2))
        with tempfile.TemporaryDirectory() as directory, patch.object(rt, "run_action", side_effect=action):
            flow = rt.Flow(spec, "direct_flow", {"sys": {"cache_dir": directory}}, [], Path(directory))
            self.assertFalse(flow.execute([("p", {"slow": True}, {}), ("p", {"slow": False}, {})]))

    def test_invalid_hooks_rejected_before_execution(self):
        for hooks in ({}, [{"on": []}], [{"on": "unknown", "do": []}],
                      [{"on": "node_finished", "do": [{"type": "execute_workload"}]}],
                      [{"on": "node_finished", "do": [{"type": "execute_routine", "routine": "missing"}]}]):
            with self.assertRaises(rt.RuntimeError):
                rt.Flow({"events": hooks}, "direct_flow", {}, [], Path("/tmp"))

    def test_scheduler_example_reports_once_with_spec_status_mapping(self):
        import yaml
        spec = yaml.safe_load((Path(__file__).parents[1]/"examples/scheduler-smoke/spec.yaml").read_text())
        reports = []
        original = rt.run_action
        def action(item, context, *args, **kwargs):
            if item["type"] == "send_http_request":
                values = rt._render(item, context)
                if values["url"].endswith('/api/requests/report'):
                    reports.append(rt.render_payload(item["body"], context))
                if "save_state" in item:
                    context.setdefault("actions", {})[item["save_state"]] = {"success": True, "body": {
                        "uploadUrl": "https://storage.invalid/upload", "uploadId": "receipt", "method": "put", "headers": {"Content-Type": "text/plain"}}}
                return
            return original(item, context, *args, **kwargs)
        def workload(spec, name, supplied, context, device, workdir):
            if name == "create-text-artifact":
                path = Path(supplied["output_path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("safe test")
        with tempfile.TemporaryDirectory() as directory, patch.object(rt, "run_action", side_effect=action), patch.object(rt, "execute_workload", side_effect=workload):
            flow = rt.Flow(spec, "master_flow", {"sys": {"cache_dir": directory}, "session": {
                "working_dir": directory, "server": "https://server.invalid", "token": "test-placeholder"}}, [], Path(directory))
            self.assertFalse(flow.execute([("scheduler-smoke", {}, {"requestId": "r"})]))
        self.assertEqual([r['status'] for r in reports if 'status' in r], ['processing', 'succeeded'])
        for node in spec['pipeline']['scheduler-smoke']['nodes']:
            self.assertEqual([r['node_states'][node['name']]['status'] for r in reports if node['name'] in r.get('node_states', {})], ['running', 'succeeded'])

    def test_yaml_on_key_is_literal_in_local_and_remote_specs(self):
        from mu.spec_yaml import load_spec_yaml
        from mu.master_flow import MasterFlowClient
        source = "events:\n  - on: pipeline_finished\n    do: []\nflag: true\nvalue: on\n"
        parsed = load_spec_yaml(source)
        self.assertEqual(parsed["events"][0]["on"], "pipeline_finished")
        self.assertIs(parsed["flag"], True)
        self.assertIs(parsed["value"], True)  # Existing value semantics preserved.
        with patch.object(MasterFlowClient, "_call", return_value=source):
            self.assertEqual(MasterFlowClient("https://example.invalid", "test").spec("/spec"), parsed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"spec.yaml"
            path.write_text(source)
            self.assertEqual(rt.read_data(path), parsed)
