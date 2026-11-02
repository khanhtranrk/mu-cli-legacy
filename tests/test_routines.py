"""Routine boundaries, file metadata and dynamic data keys."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from mu.runtime import Flow, RuntimeError, run_action, validate_routines


class RoutineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = {'params': {'path': 'input.bin'}, 'node': {'name': 'demo'},
                        'actions': {'existing': {'value': 1}}}
        (self.root / 'input.bin').write_bytes(b'hello')
        self.spec = {'routines': {'inspect': {
            'params': {'path': {'type': 'string'}},
            'actions': [{'type': 'inspect_file', 'target': '${{ params.path }}', 'save_state': 'file'}],
            'returns': '${{ actions.file }}'}}}

    def call(self, action, **kwargs):
        run_action(action, self.context, {}, self.root, spec=self.spec, **kwargs)

    def test_metadata_and_context_isolation(self):
        self.call({'type': 'execute_routine', 'routine': 'inspect', 'args': {'path': '${{ params.path }}'}, 'save_state': 'result'})
        output = self.context['actions']['result']['output']
        self.assertEqual(output['sha256'], hashlib.sha256(b'hello').hexdigest())
        self.assertEqual(output['size'], 5)
        self.assertEqual(output['path'], str(self.root / 'input.bin'))
        self.assertNotIn('file', self.context['actions'])
        self.assertEqual(self.context['params'], {'path': 'input.bin'})
        self.assertEqual(self.context['actions']['existing'], {'value': 1})

    def test_nested_calls_and_failures(self):
        self.spec['routines']['wrapper'] = {'params': {}, 'actions': [
            {'type': 'execute_routine', 'routine': 'inspect', 'args': {'path': 'input.bin'}, 'save_state': 'nested'}],
            'returns': '${{ actions.nested.output.size }}'}
        self.call({'type': 'execute_routine', 'routine': 'wrapper', 'save_state': 'result'})
        self.assertEqual(self.context['actions']['result']['output'], 5)
        for args in ({}, {'path': 1}, {'path': 'input.bin', 'extra': 1}, {'path': 'missing'}):
            self.call({'type': 'execute_routine', 'routine': 'inspect', 'args': args, 'save_state': 'result', 'ignore_error': True})
            self.assertFalse(self.context['actions']['result']['success'])
            self.assertNotIn('output', self.context['actions']['result'])

    def test_cycles_and_hidden_workloads_rejected(self):
        self.spec['routines']['inspect']['actions'] = [{'type': 'execute_routine', 'routine': 'inspect'}]
        with self.assertRaisesRegex(RuntimeError, 'cycle'):
            validate_routines(self.spec)
        self.spec['routines']['inspect']['actions'] = [{'type': 'execute_workload', 'workload': 'gpu'}]
        with self.assertRaisesRegex(RuntimeError, 'execute_workload'):
            validate_routines(self.spec)

    def test_typed_arrays_and_dynamic_keys(self):
        self.spec['routines']['report'] = {'params': {'logs': {'type': 'array', 'default': []}}, 'actions': [
            {'type': 'write_json', 'path': str(self.root / 'report.json'),
             'content': {'${{ node.name }}': {'logs': '${{ params.logs }}'}}}]}
        self.call({'type': 'execute_routine', 'routine': 'report', 'args': {'logs': ['done']}})
        self.assertEqual(json.loads((self.root / 'report.json').read_text()), {'demo': {'logs': ['done']}})
        with self.assertRaisesRegex(RuntimeError, 'array'):
            self.call({'type': 'execute_routine', 'routine': 'report', 'args': {'logs': 'bad'}})
        for content in ({'${{ node.name }}': 1, 'demo': 2}, {'${{ missing }}': 1}):
            with self.assertRaises(RuntimeError):
                self.call({'type': 'write_json', 'path': str(self.root / 'bad.json'), 'content': content})
        self.assertFalse((self.root / 'bad.json').exists())

    def test_entry_and_finally_support(self):
        action = {'type': 'execute_routine', 'routine': 'inspect', 'args': {'path': 'input.bin'}}
        self.spec['entries'] = {'direct_flow': {'initialization': [action], 'finalization': [action]}}
        flow = Flow(self.spec, 'direct_flow', self.context, [], self.root)
        flow.initialize()
        flow.finalize(False)

    def test_prepare_and_failed_node_finally(self):
        self.spec['routines']['marker'] = {'params': {'path': {'type': 'string'}}, 'actions': [
            {'type': 'write_json', 'path': '${{ params.path }}', 'content': {'done': True}}]}
        def marker(name):
            return {'type': 'execute_routine', 'routine': 'marker', 'args': {'path': str(self.root / name)}}
        self.spec['pipeline'] = {'demo': {'params': {}, 'prepare': [marker('prepared.json')], 'nodes': [
            {'name': 'failed', 'run': [{'type': 'inspect_file', 'target': 'missing'}], 'finally': [marker('finally.json')]}]}}
        base = {'sys': {'cache_dir': str(self.root), 'models': {}}, 'session': {'working_dir': str(self.root)}}
        flow = Flow(self.spec, 'direct_flow', base, [], self.root)
        self.assertTrue(flow.execute([('demo', {}, {})]))
        self.assertTrue((self.root / 'prepared.json').is_file())
        self.assertTrue((self.root / 'finally.json').is_file())
