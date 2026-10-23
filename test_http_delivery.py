"""Response loss must never rewrite the result of an already executed action."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from launcher import Handler, MenuError, Server


class DeliveryTests(unittest.TestCase):
    def exercise(self, *, at_headers, business_error=None):
        with tempfile.TemporaryDirectory() as folder:
            saved = Path(folder) / 'committed.json'
            calls, replies, writes = [], [], []
            finished = threading.Event()

            class FaultWriter:
                def __init__(self, stream, owner):
                    self.stream, self.owner, self.count = stream, owner, 0

                def write(self, content):
                    self.count += 1
                    writes.append(self.count)
                    if at_headers or self.count > 1:
                        raise BrokenPipeError('synthetic browser disconnect')
                    return self.stream.write(content)

                def __getattr__(self, name):
                    return getattr(self.stream, name)

            class InterruptedHandler(Handler):
                def setup(self):
                    super().setup()
                    self.wfile = FaultWriter(self.wfile, self)

                def reply(self, code, body, mime='application/json; charset=utf-8'):
                    replies.append(code)
                    return super().reply(code, body, mime)

                def do_POST(self):
                    try:
                        super().do_POST()
                    finally:
                        finished.set()

            def scheme(name, selection, delete):
                calls.append(name)
                if business_error is not None:
                    raise business_error
                saved.write_text(json.dumps({'name': name, 'selection': selection}), encoding='utf-8')
                return {'message': 'committed'}

            server = Server(('127.0.0.1', 0), InterruptedHandler)
            server.token = 'delivery-test'
            server.manager = SimpleNamespace(scheme=scheme)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                client.request('POST', '/api/scheme', json.dumps({'name': 'fixture', 'selection': []}),
                               {'Authorization': 'Bearer delivery-test', 'Content-Type': 'application/json'})
                if at_headers:
                    with self.assertRaises(http.client.RemoteDisconnected):
                        client.getresponse()
                else:
                    response = client.getresponse()
                    self.assertEqual(response.status, 400 if business_error is not None else 200)
                    with self.assertRaises(http.client.IncompleteRead):
                        response.read()
                self.assertTrue(finished.wait(2), 'request finalizer did not run')
                status = server.operations.snapshot()
                self.assertIsNone(status['current'])
                self.assertEqual(calls, ['fixture'])
                self.assertEqual(replies, [400 if business_error is not None else 200])
                self.assertEqual(len(writes), 1 if at_headers else 2)
                self.assertFalse(server.mutation_lock.locked())
                if business_error is None:
                    self.assertEqual(json.loads(saved.read_text(encoding='utf-8'))['name'], 'fixture')
                    self.assertEqual(status['last']['status'], 'succeeded')
                    self.assertIsNone(status['last']['error'])
                else:
                    self.assertFalse(saved.exists())
                    self.assertEqual(status['last']['status'], 'failed')
                    self.assertEqual(status['last']['error']['code'], business_error.code)
                    self.assertEqual(status['last']['error']['error'], str(business_error))
                    self.assertEqual(status['last']['error']['details'], business_error.details)
            finally:
                client.close()
                server.shutdown()
                worker.join(2)
                server.server_close()

    def test_success_survives_disconnect_before_response_headers(self):
        self.exercise(at_headers=True)

    def test_success_survives_disconnect_during_response_body(self):
        self.exercise(at_headers=False)

    def test_business_error_survives_disconnect_before_response_headers(self):
        self.exercise(at_headers=True, business_error=MenuError('fixture conflict', code='fixture_conflict',
                                                              details={'path': 'fixture.txt'}))

    def test_business_error_survives_disconnect_during_response_body(self):
        self.exercise(at_headers=False, business_error=MenuError('fixture conflict', code='fixture_conflict',
                                                               details={'path': 'fixture.txt'}))


if __name__ == '__main__':
    unittest.main()
