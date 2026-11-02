"""Type-preserving templates and numeric workload arguments; no network or GPU."""
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import yaml

from mu.pipelines import _render
from mu.runtime import params_for, run_action, RuntimeError


class RenderTests(unittest.TestCase):
    def test_whole_placeholders_preserve_types_and_copy_containers(self):
        values = {"width": 1280, "ratio": 1.5, "enabled": False, "empty": None,
                  "text": "001", "object": {"items": [1]}, "list": [True, 2]}
        for name, expected in values.items():
            with self.subTest(name=name):
                actual = _render("{params[" + name + "]}", {"params": values})
                self.assertEqual(actual, expected)
                self.assertIs(type(actual), type(expected))
                if isinstance(expected, (dict, list)):
                    self.assertIsNot(actual, expected)
        copied = _render("{params[object]}", {"params": values})
        copied["items"].append(3)
        self.assertEqual(values["object"], {"items": [1]})

    def test_nested_templates_and_composed_strings(self):
        context = {"params": {"width": 1280, "enabled": True}, "alias": "{params[width]}"}
        self.assertEqual(_render({"args": ["{alias}", "width={alias}", "flag={params[enabled]}"]}, context),
                         {"args": [1280, "width=1280", "flag=True"]})
        self.assertEqual(_render("{missing[key]}", context), "{missing[key]}")
        self.assertEqual(_render("{cycle}", {"cycle": "{cycle}"}), "{cycle}")

    def test_masterflow_resolved_registry_payload_is_plain_typed_input(self):
        # MasterFlow resolves CEL before claim; the worker receives ordinary JSON.
        payload = {"title": "Song: calm night", "width": 1024, "enabled": False,
                   "items": [1, "master"], "meta": {"mood": "calm"}}
        types = {"title": "string", "width": "number", "enabled": "boolean",
                 "items": "array", "meta": "object"}
        schema = {key: {"type": kind} for key, kind in types.items()}
        result = params_for(schema, payload, {"session": {"working_dir": "/tmp"}}, "pipeline main")
        self.assertEqual(result, payload)
        self.assertIs(type(result["width"]), int)
        self.assertIs(type(result["enabled"]), bool)
        # No Registry engine or ancestor access is introduced in this runtime.
        with self.assertRaises(RuntimeError):
            params_for({"width": {"type": "number"}},
                       {"width": "${{ Request.Registry.Width }}"}, {}, "pipeline main")

    def test_song_cover_numeric_params_reach_subprocess_as_strings(self):
        spec = yaml.safe_load((Path(__file__).parents[1] / "examples/song-with-cover/spec.yaml").read_text())
        node = next(n for n in spec["pipeline"]["song-with-cover"]["nodes"] if n["name"] == "generate-cover")
        action = {k: v for k, v in node["run"][0].items() if k not in {"skip_if", "postcondition"}}
        context = {"params": {"name": "demo", "cover_prompt": "cover", "cover_with": 1280, "cover_height": 768},
                   "session": {"output_dir": "/tmp/mu-render"},
                   "sys": {"cache_dir": "/tmp/cache", "models": {
                       "qwen-image-2.1-Q4_K_M": "diffusion", "vae/qwen_image_2.1_vae_bf16": "vae",
                       "Qwen3-VL-8B-Instruct-UD-Q4_K_XL": "llm"}}}
        with patch("mu.runtime._run_process", return_value=subprocess.CompletedProcess([], 0, "", "")) as process:
            run_action(action, context, {}, Path("/tmp"), spec=spec)
        argv = process.call_args.args[0]
        self.assertEqual(argv[argv.index("-W") + 1], "1280")
        self.assertEqual(argv[argv.index("-H") + 1], "768")
        self.assertTrue(all(isinstance(arg, str) for arg in argv))
        self.assertEqual(context["params"]["cover_with"], 1280)
        for invalid in ("1280", True):
            with self.assertRaises(RuntimeError):
                params_for({"width": {"type": "number"}}, {"width": invalid}, {}, "test")
