from Tests.support import python_executable_header
import errno
import io
import os
import subprocess
import sys
import shutil
import json
import socket
import signal
import threading
import tempfile
import unittest
from pathlib import Path

from PIL import Image

try:
    from tools.sync_adapters import run_bounded, CLISender, CodexRenderer
    from tools.reliable_sync import SyncFailure, frame_bytes
except ImportError:
    run_bounded = None


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(run_bounded, 'bounded adapters are not implemented')

    def test_timeout_is_bounded(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=0.1)

    def test_runs_without_shell_and_passes_binary_input(self):
        result = run_bounded([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'], timeout=5, content=b'not a shell; $HOME')
        self.assertEqual(result.stdout, b'not a shell; $HOME')
        self.assertEqual(result.returncode, 0)

    def test_sender_errors_are_classified_without_leaking_raw_output(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'sender'
            for code, expected in ((3, 'permission_denied'), (4, 'bluetooth_off'), (2, 'send_failed')):
                binary.write_text('#!/bin/sh\necho "ERROR 蓝牙不可用，状态值' + str(code) + ' secret"\nexit 2\n')
                binary.chmod(0o700)
                sender = CLISender(binary, 'device')
                with self.assertRaises(SyncFailure) as raised:
                    sender(Path(directory) / 'frame.png')
                self.assertEqual(raised.exception.code, expected)
                self.assertNotIn('secret', str(raised.exception))

    def test_sender_does_not_pass_mutable_quantized_output(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'sender'
            binary.write_text('#!/bin/sh\nfor arg in "$@"; do\n if [ "$arg" = "--quantized-output" ]; then exit 9; fi\ndone\necho "WRITE_COMPLETE packets=129 bytes=30511"\n')
            binary.chmod(0o700)
            CLISender(binary, 'device')(Path(directory) / 'frame.png')

    def test_zero_exit_without_write_completion_is_not_delivery(self):
        with self.assertRaises(SyncFailure):
            CLISender(Path('/usr/bin/true'), 'device')(Path('/unused.png'))

    def test_sender_stays_in_supervisors_group_and_has_a_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory)/'sender'
            binary.write_text(python_executable_header() + 'import os, sys\n'
                              'if os.getpgrp() != os.getppid() or not os.isatty(1): sys.exit(9)\n'
                              'print("WRITE_COMPLETE packets=129 bytes=30511")\n')
            binary.chmod(0o700)
            CLISender(binary, 'device')(Path(directory)/'frame.png')

    def test_explicit_relative_sender_is_not_a_path_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory)/'relative-sender'
            binary.write_text('#!/bin/sh\necho "WRITE_COMPLETE packets=129 bytes=30511"\n')
            binary.chmod(0o700)
            previous = Path.cwd()
            try:
                os.chdir(directory)
                CLISender(Path('./relative-sender'), 'device')(Path(directory)/'frame.png')
            finally:
                os.chdir(previous)

    def test_explicit_relative_codex_runs_read_only_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory)/'relative-codex'
            shutil.copyfile(Path(__file__).parent/'fixtures/fake_codex.py', binary)
            binary.chmod(0o700)
            previous = Path.cwd()
            try:
                os.chdir(directory)
                image = CodexRenderer(Path('./relative-codex'))({'version':1,'threads':{}})
            finally:
                os.chdir(previous)
            self.assertEqual(image.size, (400,300))
            self.assertLessEqual(len(image.getcolors()), 3)

    def test_png_metadata_does_not_change_pixel_digest(self):
        from PIL.PngImagePlugin import PngInfo
        image = Image.new('RGB', (400,300), (0,0,0))
        metadata = PngInfo()
        metadata.add_text('unrelated', 'different encoding')
        buffer = io.BytesIO()
        image.save(buffer, format='PNG', pnginfo=metadata, compress_level=0)
        decoded = Image.open(io.BytesIO(buffer.getvalue()))
        self.assertEqual(frame_bytes(image)[0], frame_bytes(decoded)[0])
        self.assertNotEqual(frame_bytes(image)[1], buffer.getvalue())

    def test_supervisor_signal_exit_terminates_live_sender_before_returning(self):
        self.assert_transport_stops_leaf(timeout=False)

    def test_transport_group_timeout_terminates_live_sender(self):
        self.assert_transport_stops_leaf(timeout=True)

    def assert_transport_stops_leaf(self, *, timeout):
        from tools.sync_journal import SyncJournal
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root/'sender'
            binary.write_text(python_executable_header()+ '''import json, os, socket, sys
from pathlib import Path
frame = Path(sys.argv[sys.argv.index('--input') + 1])
client = socket.socket(socket.AF_UNIX)
client.connect(str(frame.parent/'control.sock'))
client.sendall((json.dumps({'supervisor':os.getppid()}) + '\\n').encode())
while True:
    command = client.recv(16)
    if not command or command == b'x': break
    client.sendall(b'alive')
''')
            binary.chmod(0o700)
            server = socket.socket(socket.AF_UNIX)
            server.settimeout(5)
            server.bind(str(root/'control.sock'))
            server.listen()
            results = []
            done = threading.Event()
            def run():
                try:
                    with SyncJournal(root).sender_lock() as descriptor:
                        if timeout:
                            supervisor = Path(__file__).resolve().parents[2]/'tools/owned_sender.py'
                            run_bounded([sys.executable, str(supervisor), str(binary), str(root/'frame.png'), 'device', str(descriptor)],
                                        timeout=2, pass_fds=(descriptor,))
                        else:
                            CLISender(binary, 'device')(root/'frame.png', descriptor)
                except subprocess.TimeoutExpired:
                    results.append('send_timeout')
                except SyncFailure as error:
                    results.append(error.code)
                finally:
                    done.set()
            thread = threading.Thread(target=run)
            thread.start()
            connection = None
            try:
                connection, _ = server.accept()
                connection.settimeout(5)
                record = json.loads(connection.recv(256))
                if not timeout:
                    os.kill(record['supervisor'], signal.SIGKILL)
                self.assertTrue(done.wait(5))
                try:
                    connection.sendall(b'ping')
                    answer = connection.recv(16)
                except OSError as error:
                    # Darwin can report ENOTCONN after the peer has exited.
                    # Timeouts and unrelated socket errors must still fail.
                    if error.errno not in (errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN):
                        raise
                    answer = b''
                self.assertEqual(answer, b'', 'old leaf remains alive after adapter returned failure')
                self.assertEqual(results, ['send_timeout' if timeout else 'send_failed'])
            finally:
                if connection:
                    try:
                        connection.sendall(b'x')
                    except OSError:
                        pass
                    connection.close()
                server.close()
                thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
