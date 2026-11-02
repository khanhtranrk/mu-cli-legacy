import json
import tempfile
import unittest
from pathlib import Path

import celpy

from mu.cli import _runtime_base
from mu.expressions import CEL, ENGINE_KEY, engine_for, render
from mu.runtime import Flow, condition, params_for
from mu.spec_migration import migrate_spec


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
        self.assertEqual(engine_for({}), "python-format-v1")
        with self.assertRaises(ValueError):
            engine_for({"expression_engine": "cel-v2"})
        context = {ENGINE_KEY: CEL}
        self.assertEqual(
            params_for({"a": {"type": "string"}}, {"a": "${{ 2 }}"}, context, "p"),
            {"a": "${{ 2 }}"},
        )
        with self.assertRaises(ValueError):
            condition({"operator": "cel", "expression": "${{ 1 }}"}, context, {})

    def test_migration(self):
        source = {"context": {"x": "{session[output_dir]}/x", "n": "{params[n]}"}}
        report = migrate_spec(source)
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["converted"], 2)
        self.assertEqual(report["spec"]["context"]["n"], '${{ params["n"] }}')
        self.assertEqual(source["context"]["n"], "{params[n]}")
        self.assertTrue(migrate_spec({"name": "{params[n]} super"})["issues"])
        self.assertTrue(migrate_spec({"x": '{"a":1}'})["issues"])

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

    def test_cli_migration_never_overwrites_and_blocks_ambiguous_input(self):
        from typer.testing import CliRunner

        from mu.cli import app

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.yaml"
            target = root / "target.yaml"
            source.write_text(
                'context:\n  path: "{session[output_dir]}/x"\npipeline: {}\n'
            )
            original = source.read_bytes()
            runner = CliRunner()
            result = runner.invoke(
                app, ["spec", "migrate-cel", str(source), "--output", str(target)]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            candidate = target.read_bytes()
            self.assertEqual(source.read_bytes(), original)
            result = runner.invoke(
                app, ["spec", "migrate-cel", str(source), "--output", str(target)]
            )
            self.assertEqual(result.exit_code, 2)
            self.assertEqual(target.read_bytes(), candidate)
            source.write_text("x: '{\"literal\": 1}'\n")
            result = runner.invoke(
                app,
                [
                    "spec",
                    "migrate-cel",
                    str(source),
                    "--output",
                    str(root / "blocked.yaml"),
                ],
            )
            self.assertEqual(result.exit_code, 2)
            self.assertFalse((root / "blocked.yaml").exists())

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
