import json
import tempfile
import unittest
from pathlib import Path

import celpy

from mu.cli import _runtime_base
from mu.expressions import CEL, ENGINE_KEY, engine_for, render
from mu.runtime import Flow, condition, params_for


class ExpressionTests(unittest.TestCase):
    def test_conformance(self):
        vectors = json.loads(
            (Path(__file__).parent / "fixtures/cel-v1.json").read_text()
        )
        for case in vectors["cases"]:
            with self.subTest(case=case["name"]):
                if case.get("error"):
                    with self.assertRaises(
                        (ValueError, TypeError, celpy.CELEvalError, celpy.CELParseError)
                    ):
                        render(case["template"], vectors["context"])
                else:
                    self.assertEqual(
                        render(case["template"], vectors["context"]), case["expected"]
                    )

    def test_engine_and_once(self):
        self.assertEqual(engine_for({}), CEL)
        for engine in ("cel-v2", "python-format-v1"):
            with self.assertRaises(ValueError):
                engine_for({"expression_engine": engine})
        context = {ENGINE_KEY: CEL}
        self.assertEqual(
            params_for({"a": {"type": "string"}}, {"a": "${{ 2 }}"}, context, "p"),
            {"a": "${{ 2 }}"},
        )
        with self.assertRaises(ValueError):
            condition({"operator": "cel", "expression": "${{ 1 }}"}, context, {})

    def test_cpu_flow_request_scopes_defaults_conditions_and_routines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = _runtime_base(root, root / "cache", "test")
            spec = {
                "expression_engine": CEL,
                "routines": {
                    "write": {
                        "params": {"v": {"type": "string"}},
                        "actions": [
                            {
                                "type": "write_json",
                                "path": '${{ session.working_dir + "/" + request.requestId + ".json" }}',
                                "content": {
                                    "v": "${{ params.v }}",
                                    "n": "${{ Request.Registry.N }}",
                                },
                            }
                        ],
                    }
                },
                "pipeline": {
                    "p": {
                        "params": {
                            "v": {
                                "type": "string",
                                "default": "${{ CreationSpace.Registry.Feel }}",
                            }
                        },
                        "context": {"local": "${{ Session.Registry.Tag }}"},
                        "nodes": [
                            {
                                "name": "n",
                                "precondition": {
                                    "operator": "cel",
                                    "expression": "${{ Request.Registry.N > 0 }}",
                                },
                                "run": [
                                    {
                                        "type": "execute_routine",
                                        "routine": "write",
                                        "args": {"v": "${{ params.v }}"},
                                    }
                                ],
                            }
                        ],
                    }
                },
            }
            flow = Flow(spec, "master_flow", base, [], root)
            requests = [
                (
                    "p",
                    {},
                    {
                        "requestId": str(i),
                        "registryContext": {
                            "CreationSpace": {"Feel": "${{ 1 }}"},
                            "Session": {"Tag": "x"},
                            "Request": {"N": i},
                        },
                    },
                )
                for i in [1, 2]
            ]
            self.assertFalse(flow.execute(requests))
            self.assertEqual(
                json.loads((root / "1.json").read_text()), {"v": "${{ 1 }}", "n": 1}
            )
            self.assertEqual(
                json.loads((root / "2.json").read_text()), {"v": "${{ 1 }}", "n": 2}
            )

    def test_workload_event_and_invalid_request_does_not_prepare(self):
        import sys

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = _runtime_base(root, root / "cache", "test")
            spec = {
                "expression_engine": CEL,
                "entries": {
                    "direct_flow": {
                        "initialization": [
                            {
                                "type": "write_json",
                                "path": str(root / "init.json"),
                                "content": "${{ entry.name }}",
                            }
                        ]
                    }
                },
                "events": [
                    {
                        "on": "pipeline_started",
                        "do": [
                            {
                                "type": "write_json",
                                "path": str(root / "event.json"),
                                "content": "${{ Request.Registry.N }}",
                            }
                        ],
                    }
                ],
                "workloads": {
                    "w": {
                        "params": {
                            "n": {"type": "number", "default": "${{ params.n }}"}
                        },
                        "target": sys.executable,
                        "units": 0,
                        "args": [
                            "-c",
                            'import sys; assert sys.argv[1] == "3"',
                            "${{ params.n }}",
                        ],
                    }
                },
                "pipeline": {
                    "p": {
                        "params": {"n": {"type": "number"}},
                        "prepare": [
                            {
                                "type": "write_json",
                                "path": str(root / "prepare.json"),
                                "content": True,
                            }
                        ],
                        "nodes": [
                            {
                                "name": "w",
                                "run": [{"type": "execute_workload", "workload": "w"}],
                            }
                        ],
                    }
                },
            }
            flow = Flow(spec, "direct_flow", base, [], root)
            flow.initialize()
            self.assertEqual(
                json.loads((root / "init.json").read_text()), "direct_flow"
            )
            self.assertTrue(
                flow.execute([("p", {}, {"registryContext": {"Request": {"N": 7}}})])
            )
            self.assertFalse((root / "prepare.json").exists())
            self.assertFalse(
                flow.execute(
                    [("p", {"n": 3}, {"registryContext": {"Request": {"N": 8}}})]
                )
            )
            self.assertEqual(json.loads((root / "event.json").read_text()), 8)

    def test_captured_artifact_registry_reference_is_plain_json_and_isolated(self):
        from mu.runtime import _request_scopes

        reference = {"artifactId": "canonical-artifact-id"}
        request = {"registryContext": {"Session": {"Asset": reference}}}
        context = {ENGINE_KEY: CEL}
        _request_scopes(context, request)
        self.assertEqual(
            render("${{ Session.Registry.Asset.artifactId }}", context),
            "canonical-artifact-id",
        )
        self.assertEqual(render("${{ Session.Registry.Asset }}", context), reference)
        context["Session"]["Registry"]["Asset"]["artifactId"] = "changed"
        self.assertEqual(reference["artifactId"], "canonical-artifact-id")

    def test_current_media_example_is_cel_without_executing_workloads(self):
        from copy import deepcopy

        from mu.expressions import validate_templates
        from mu.runtime import read_data, render_payload

        source = read_data(
            Path(__file__).parent.parent / "examples/song-with-cover/spec.yaml"
        )
        source["pipeline"]["song-with-cover-recure"] = deepcopy(
            source["pipeline"]["song-with-cover"]
        )
        self.assertEqual(engine_for(source), CEL)
        validate_templates(source)
        body = source["routines"]["report_node_status"]["actions"][0]["body"]
        rendered = render_payload(
            body,
            {
                ENGINE_KEY: CEL,
                "request": {"requestId": "r"},
                "node": {"name": "render"},
                "params": {"status": "running", "logs": ["ok"]},
            },
        )
        self.assertEqual(
            rendered,
            {
                "id": "r",
                "node_states": {"render": {"status": "running", "logs": ["ok"]}},
            },
        )

    def test_spec_syntax_budget_is_separate_from_runtime(self):
        from mu.expressions import validate_templates

        validate_templates([{"x": "${{ params.name }}"} for _ in range(300)])
        with self.assertRaises((ValueError, celpy.CELParseError)):
            validate_templates({"${{ params.name + }}": 1})
        with self.assertRaisesRegex(ValueError, "work limit"):
            validate_templates(["${{ 1 }}"] * 4097)
        with self.assertRaisesRegex(ValueError, "limit"):
            render(["${{ 1 }}"] * 193, {})

    def test_all_bundled_specs_use_cel_and_reject_removed_engine_before_actions(self):
        from mu.expressions import validate_templates
        from mu.runtime import read_data
        from unittest.mock import patch

        root = Path(__file__).parents[1]
        paths = list((root / "examples").rglob("spec*.yaml"))
        paths.append(root / ".github/skills/create-mu-spec/assets/minimal-spec.yaml")
        for path in paths:
            with self.subTest(spec=str(path)):
                spec = read_data(path)
                self.assertEqual(spec["expression_engine"], CEL)
                validate_templates(spec)
        with patch("mu.runtime.run_action") as action:
            with self.assertRaisesRegex(ValueError, "only cel-v1"):
                Flow({"expression_engine": "python-format-v1"}, "direct_flow", {}, [], Path("/tmp"))
            action.assert_not_called()
