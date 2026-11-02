"""Binary HTTP transfers: raw bytes, atomic replacement and failure preservation."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mu.runtime import run_action


class HttpFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = bytes(range(256)) * 8193
        payload = self.payload
        self.uploads = []
        uploads = self.uploads

        test_case = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                uploads.append((self.rfile.read(int(self.headers['Content-Length'])), dict(self.headers)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'storageId': 'file'}).encode())

            do_PUT = do_POST

            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302); self.send_header('Location', test_case.redirect_target); self.end_headers(); return
                if self.path == '/error':
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"Unauthorized"}')
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload[:100] if self.path == '/truncated' else payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.context = {}

    def action(self, **values):
        run_action({'type': 'send_http_request', 'url': self.url, 'save_state': 'transfer', **values},
                   self.context, {}, self.root)
        return self.context['actions']['transfer']

    def test_binary_upload_and_download(self):
        (self.root / 'source.bin').write_bytes(self.payload)
        result = self.action(method='post', body_file='source.bin', timeout=120,
                             header={'Content-Length': '1', 'Transfer-Encoding': 'chunked'})
        self.assertEqual(self.uploads[0][0], self.payload)
        self.assertEqual(self.uploads[0][1]['Content-Length'], str(len(self.payload)))
        self.assertNotIn('Transfer-Encoding', self.uploads[0][1])
        self.assertEqual(self.uploads[0][1]['Content-Type'], 'application/octet-stream')
        self.assertEqual(result['body'], {'storageId': 'file'})
        result = self.action(response_file='nested/download.bin')
        self.assertEqual((self.root / 'nested/download.bin').read_bytes(), self.payload)
        self.assertIsNone(result['body'])
        self.assertEqual(result['size'], len(self.payload))
        self.assertEqual(result['response_file'], str(self.root / 'nested/download.bin'))
        self.assertFalse(list(self.root.glob('nested/.mu-download-*')))

    def test_failed_download_preserves_existing_file(self):
        destination = self.root / 'keep.bin'
        destination.write_bytes(b'existing')
        for path in ['/error', '/truncated']:
            with self.assertRaises(ValueError):
                self.action(url=self.url + path, response_file='keep.bin')
            self.assertEqual(destination.read_bytes(), b'existing')
            self.assertFalse(list(self.root.glob('.mu-download-*')))
            self.assertFalse(self.context['actions']['transfer']['success'])
        self.action(url=self.url + '/error', response_file='keep.bin', ignore_error=True)
        self.assertEqual(self.context['actions']['transfer']['body'], {'error': 'Unauthorized'})
        self.assertEqual(destination.read_bytes(), b'existing')

    def test_invalid_declarations_and_json_compatibility(self):
        for values in [{'body': {}, 'body_file': 'source.bin'}, {'timeout': 0}, {'timeout': True},
                       {'timeout': float('nan')}, {'body_file': ''}, {'response_file': False}]:
            with self.assertRaises(ValueError):
                self.action(**values)
        self.assertEqual(self.uploads, [])
        result = self.action(method='post', body={'message': 'hello'})
        self.assertEqual(json.loads(self.uploads[0][0]), {'message': 'hello'})
        self.assertEqual(result['body'], {'storageId': 'file'})

    def test_assignment_header_and_cross_origin_credential_stripping(self):
        self.context = {'session': {'server': self.url}, 'request': {'assignmentVersion': 7}}
        self.action(method='post', body={'status': 'processing'}, header={'Authorization': 'Bearer worker'})
        self.assertEqual(self.uploads[-1][1]['X-Mu-Assignment-Version'], '7')
        captured = []
        class Storage(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                captured.append(dict(self.headers)); self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
            def do_PUT(self):
                captured.append(dict(self.headers)); self.rfile.read(int(self.headers['Content-Length'])); self.send_response(204); self.end_headers()
        external = ThreadingHTTPServer(('127.0.0.1', 0), Storage)
        threading.Thread(target=external.serve_forever, daemon=True).start()
        self.addCleanup(external.server_close); self.addCleanup(external.shutdown)
        target = f'http://127.0.0.1:{external.server_port}'
        self.redirect_target = target
        self.action(url=self.url+'/redirect', header={'Authorization': 'Bearer worker', 'Cookie': 'secret'})
        self.assertNotIn('Authorization', captured[-1]); self.assertNotIn('Cookie', captured[-1])
        self.assertNotIn('X-Mu-Assignment-Version', captured[-1])
        (self.root/'file').write_bytes(b'bytes')
        self.context['actions']['issue'] = {'body': {'url': target, 'method': 'PUT', 'headers': {'Content-Type': 'text/plain'}}}
        result = self.action(url='${{ actions.issue.body.url }}', method='${{ actions.issue.body.method }}', header='${{ actions.issue.body.headers }}', body_file='file')
        self.assertEqual(result['status_code'], 204); self.assertIsNone(result['body'])
        self.assertNotIn('Authorization', captured[-1]); self.assertNotIn('X-Mu-Assignment-Version', captured[-1])
        self.assertEqual(captured[-1]['Content-Length'], '5')


if __name__ == '__main__':
    unittest.main()
