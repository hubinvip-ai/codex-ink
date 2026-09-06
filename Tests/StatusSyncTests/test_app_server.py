from Tests.support import python_executable_header
import io
import json
import unittest
import os
import time
import threading
import tempfile
import sys
from pathlib import Path
from unittest import mock

from tools.codex_app_server import CodexAppServerClient, JsonlRpcConnection


class JsonlRpcConnectionTests(unittest.TestCase):
    def test_request_correlates_response_while_preserving_notifications(self):
        stdin = io.StringIO()
        stdout = io.StringIO(
            '\n'.join(
                [
                    json.dumps({"method": "thread/status/changed", "params": {"threadId": "thr_1"}}),
                    json.dumps({"id": 7, "result": {"data": [1, 2, 3]}}),
                ]
            )
            + '\n'
        )
        connection = JsonlRpcConnection(stdin=stdin, stdout=stdout)
        result = connection.request("thread/list", {"limit": 3}, request_id=7)
        self.assertEqual(result, {"data": [1, 2, 3]})
        self.assertEqual(connection.notifications[0]["method"], "thread/status/changed")
        sent = json.loads(stdin.getvalue())
        self.assertEqual(sent, {"method": "thread/list", "id": 7, "params": {"limit": 3}})

    def pipe_connection(self, timeout=.15):
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, 'r')
        self.addCleanup(stream.close)
        self.addCleanup(os.close, write_fd)
        return JsonlRpcConnection(stdin=io.StringIO(), stdout=stream, timeout=timeout), write_fd

    def test_real_pipe_coalesced_notification_and_response(self):
        connection, writer = self.pipe_connection()
        os.write(writer, b'{"method":"notice"}\n{"id":1,"result":{"ok":true}}\n')
        self.assertEqual(connection.request('test', {}, request_id=1), {'ok': True})
        self.assertEqual(len(connection.notifications), 1)

    def test_real_pipe_partial_line_times_out_without_waiting_for_newline(self):
        connection, writer = self.pipe_connection(.05)
        os.write(writer, b'{"id":1,')
        # Rescue a regressed blocking readline, but assert event ordering rather
        # than a sub-200ms wall-clock budget on a shared CI runner.
        newline_sent = threading.Event()
        def rescue():
            newline_sent.set()
            os.write(writer, b'"result":{}}\n')
        delayed = threading.Timer(2, rescue)
        delayed.start()
        try:
            with self.assertRaises(TimeoutError): connection.request('test', {}, request_id=1)
            self.assertFalse(newline_sent.is_set(), 'timeout waited for a newline')
        finally:
            delayed.cancel()
            delayed.join()

    def test_real_pipe_eof_differs_from_timeout_and_rejects_partial_line(self):
        for payload, expected in [(b'', 'closed before response'), (b'{"id":1', 'incomplete response')]:
            reader, writer = os.pipe()
            os.write(writer, payload)
            os.close(writer)
            with os.fdopen(reader, 'r') as stream:
                connection = JsonlRpcConnection(stdin=io.StringIO(), stdout=stream, timeout=.1)
                with self.assertRaisesRegex(RuntimeError, expected): connection.request('test', {}, request_id=1)

    def test_oversized_line_is_rejected_before_decoding(self):
        connection, writer = self.pipe_connection()
        connection._max_line_bytes = 64
        os.write(writer, b' ' * 65 + b'\n')
        with self.assertRaisesRegex(ValueError, 'line limit'): connection.request('test', {}, request_id=1)

    def test_real_pipe_split_utf8_response(self):
        connection, writer = self.pipe_connection()
        data = '{"id":1,"result":{"title":"任务"}}\n'.encode()
        split = data.index('任'.encode()) + 1
        os.write(writer, data[:split])
        delayed = threading.Timer(.02, lambda: os.write(writer, data[split:]))
        delayed.start()
        try: self.assertEqual(connection.request('test', {}, request_id=1), {'title':'任务'})
        finally: delayed.join()

    def test_error_response_raises_with_server_message(self):
        connection = JsonlRpcConnection(
            stdin=io.StringIO(),
            stdout=io.StringIO(json.dumps({"id": 2, "error": {"code": -1, "message": "bad request"}}) + '\n'),
        )
        with self.assertRaisesRegex(RuntimeError, "bad request"):
            connection.request("bad", {}, request_id=2)


class CodexAppServerClientTests(unittest.TestCase):
    def test_failed_initialize_closes_owned_process_and_pipes(self):
        with tempfile.TemporaryDirectory() as directory:
            executable=Path(directory)/'fake-codex'
            executable.write_text(python_executable_header() + 'import sys,time\nsys.stdin.readline()\nprint("not json",flush=True)\ntime.sleep(5)\n')
            executable.chmod(0o700)
            client=CodexAppServerClient(binary=executable,timeout=2)
            try:
                with self.assertRaises(ValueError): client.__enter__()
                self.assertIsNone(client.process)
                self.assertIsNone(client.connection)
            finally: client.close()

    def test_fetch_includes_real_account_and_plan(self):
        client = CodexAppServerClient()
        with mock.patch.object(
            client,
            "_request",
            side_effect=[
                {"data": []},
                {"rateLimits": {}},
                {"summary": {}, "dailyUsageBuckets": []},
                {"account": {"type": "chatgpt", "email": "demo@example.com", "planType": "pro"}},
            ],
        ) as request:
            snapshot = client.fetch()

        self.assertEqual(snapshot.account["account"]["planType"], "pro")
        self.assertEqual(request.call_args_list[-1], mock.call("account/read", {}))


if __name__ == "__main__":
    unittest.main()
