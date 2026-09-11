from Tests.support import python_executable_header
import json
import multiprocessing
import os
import socket
import sys
import time
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image
from tools.codex_status_hook import update_state_file
from tools.sync_journal import SyncJournal

try:
    from tools.reliable_sync import ReliableWorker, SyncFailure
except ImportError:
    ReliableWorker = None
    SyncFailure = RuntimeError


def render_state(state):
    status = state['threads'].get('thread', {}).get('status', 'queued')
    color = (0,0,0) if status == 'running' else (198,40,40)
    return Image.new('RGB', (400,300), color)


def crashable_consumer(directory, started):
    root = Path(directory)
    journal = SyncJournal(root)
    def waiting_sender(path, owner_fd):
        started.set()
        threading.Event().wait(30)
    worker = ReliableWorker(journal, root / 'status.json', render_state, waiting_sender, clock=lambda:105)
    worker.tick()


def subprocess_consumer(directory):
    from tools.sync_adapters import CLISender
    root = Path(directory)
    worker = ReliableWorker(SyncJournal(root), root/'status.json', render_state,
                            CLISender(root/'slow-sender', 'device'), clock=lambda:105)
    worker.tick()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ReliableWorker, 'reliable consumer is not implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_file = self.root / 'status.json'
        self.journal = SyncJournal(self.root)
        self.now = 100
        self.sent = []
        self.event('UserPromptSubmit')
        self.worker = ReliableWorker(self.journal, self.state_file, render_state, self.send, clock=lambda:self.now)

    def event(self, name):
        update_state_file(self.state_file, {'session_id':'thread', 'turn_id':'turn', 'cwd':'/project', 'hook_event_name':name}, self.now)

    def send(self, frame, owner_fd=None):
        with Image.open(frame) as image:
            self.sent.append(image.getpixel((0,0)))

    def start(self):
        self.worker.tick()
        self.now += 30
        return self.worker.tick()

    def test_merge_waits_thirty_seconds(self):
        self.worker.tick()
        self.now = 129
        self.assertEqual(self.worker.tick()['status'], 'pending')
        self.assertEqual(self.sent, [])
        self.now = 130
        self.assertEqual(self.worker.tick()['status'], 'sent')

    def test_cooldown_survives_restart_and_sends_latest(self):
        self.journal.request(self.now, force=True)
        self.worker.tick()
        self.now = 101
        self.event('Stop')
        self.worker.tick()
        restarted = ReliableWorker(self.journal, self.state_file, render_state, self.send, clock=lambda:self.now)
        self.now = 279
        self.assertEqual(restarted.tick()['status'], 'pending')
        self.assertEqual(len(self.sent), 1)
        self.now = 280
        self.assertEqual(restarted.tick()['status'], 'sent')
        self.assertEqual(self.sent, [(0,0,0), (198,40,40)])

    def test_timestamp_only_pixel_changes_skip_but_force_sends(self):
        def render(state):
            image = render_state(state)
            image.putpixel((0, 0), (0,0,0) if self.now == 100 else (198,40,40))
            image.info['codex_content_hash'] = 'a' * 64
            return image
        self.worker.render = render
        self.journal.request(self.now, force=True)
        self.worker.tick()
        self.now = 300
        self.journal.request(260)
        self.assertEqual(self.worker.tick()['status'], 'unchanged')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.journal.read()['last_sent_at'], 100)
        self.journal.request(self.now, force=True)
        self.assertEqual(self.worker.tick()['status'], 'sent')
        self.assertEqual(len(self.sent), 2)

    def test_failed_semantic_frame_is_not_cached(self):
        def render(state):
            image = render_state(state)
            image.info['codex_content_hash'] = 'a' * 64
            return image
        self.worker.render = render
        def failed(frame, owner_fd):
            raise SyncFailure('send_failed')
        self.worker.send = failed
        self.journal.request(self.now, force=True)
        self.assertEqual(self.worker.tick()['status'], 'retrying')
        self.worker.send = self.send
        self.now += 5
        self.assertEqual(self.worker.tick()['status'], 'sent')
        self.assertEqual(len(self.sent), 1)

    def test_preview_does_not_replace_sent_content_identity(self):
        def render(state):
            image = render_state(state)
            image.info['codex_content_hash'] = 'a' * 64 if self.now == 100 else 'b' * 64
            return image
        self.worker.render = render
        self.journal.request(self.now, force=True)
        self.worker.tick()
        self.now = 101
        self.event('Stop')
        self.worker.tick(no_push=True)
        self.worker.tick()
        self.now = 280
        self.assertEqual(self.worker.tick()['status'], 'sent')
        self.assertEqual(self.sent, [(0,0,0), (198,40,40)])

    def test_periodic_poll_waits_ten_minutes(self):
        self.journal.request(self.now, force=True)
        self.worker.tick()
        before = self.journal.read()['requested_revision']
        self.now = 699
        self.assertEqual(self.worker.tick()['status'], 'idle')
        self.assertEqual(self.journal.read()['requested_revision'], before)
        self.now = 700
        self.assertEqual(self.worker.tick()['status'], 'pending')

    def test_stop_arriving_during_send_is_delivered_next(self):
        def sender(frame, owner_fd):
            self.send(frame)
            if len(self.sent) == 1:
                self.event('Stop')
                self.journal.request(self.now)
        self.worker.send = sender
        self.start()
        state = self.journal.read()
        self.assertGreater(state['requested_revision'], state['acknowledged_revision'])
        self.worker.tick()
        self.now += 180
        self.worker.tick()
        self.assertEqual(self.sent, [(0,0,0), (198,40,40)])
        self.assertEqual(self.journal.read()['requested_revision'], self.journal.read()['acknowledged_revision'])

    def test_many_events_during_send_coalesce_to_final_state(self):
        def sender(frame, owner_fd):
            self.send(frame)
            if len(self.sent) == 1:
                for event in ('Stop', 'UserPromptSubmit', 'SessionEnd'):
                    self.event(event)
                    self.journal.request(self.now)
        self.worker.send = sender
        self.start()
        self.worker.tick()
        self.now += 180
        self.worker.tick()
        self.assertEqual(self.sent, [(0,0,0), (198,40,40)])

    def test_state_written_without_registration_is_recovered(self):
        self.start()
        self.event('Stop')
        restarted = ReliableWorker(self.journal, self.state_file, render_state, self.send, clock=lambda:self.now)
        restarted.tick()
        self.now += 180
        restarted.tick()
        self.assertEqual(self.sent, [(0,0,0), (198,40,40)])

    def test_new_request_after_idle_check_is_not_lost(self):
        self.start()
        self.assertEqual(self.worker.tick()['status'], 'idle')
        self.event('Stop')
        self.journal.request(self.now, force=True)
        self.worker.tick()
        self.assertEqual(self.sent[-1], (198,40,40))

    def test_snapshot_is_read_after_claiming_revision(self):
        self.worker.tick()
        self.now += 30
        original = self.journal.begin
        def begin(now):
            ticket = original(now)
            self.event('Stop')
            self.journal.request(now, force=True)
            return ticket
        self.journal.begin = begin
        self.worker.tick()
        self.assertEqual(self.sent, [(198,40,40)])
        self.assertGreater(self.journal.read()['requested_revision'], self.journal.read()['acknowledged_revision'])

    def test_failed_send_keeps_pending_and_retries_latest(self):
        def failed(frame, owner_fd):
            raise SyncFailure('send_failed')
        self.worker.send = failed
        self.assertEqual(self.start()['status'], 'retrying')
        self.assertEqual(self.journal.read()['acknowledged_revision'], 0)
        self.assertIsNone(self.journal.read()['last_sent_at'])
        self.event('Stop')
        self.worker.send = self.send
        self.now += 4
        self.worker.tick()
        self.assertEqual(self.sent, [])
        self.now += 1
        self.worker.tick()
        self.assertEqual(self.sent, [(198,40,40)])
        self.assertEqual(self.journal.read()['failure_count'], 0)

    def test_changing_public_preview_cannot_change_sending_frame(self):
        def sender(frame, owner_fd):
            original = frame.read_bytes()
            (self.root / 'preview.png').write_bytes(b'other preview')
            self.assertNotEqual(frame, self.root / 'preview.png')
            self.assertEqual(frame.read_bytes(), original)
            self.send(frame)
        self.worker.send = sender
        self.start()
        self.assertEqual(self.sent, [(0,0,0)])
        self.assertEqual(list((self.root / 'frames').glob('*.png')), [])

    def test_duplicate_pixels_skip_send_without_advancing_sent_time(self):
        self.start()
        self.now += 10
        self.journal.request(self.now)
        self.now += 180
        self.assertEqual(self.worker.tick()['status'], 'unchanged')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.journal.read()['last_sent_at'], 130)

    def test_force_survives_later_ordinary_request(self):
        self.start()
        self.journal.request(self.now, force=True)
        self.journal.request(self.now)
        self.worker.tick()
        self.assertEqual(len(self.sent), 2)

    def test_preview_does_not_acknowledge_or_send_pending_request(self):
        self.journal.request(self.now, force=True)
        before = self.journal.read()
        self.assertEqual(self.worker.tick(no_push=True)['status'], 'preview')
        self.assertEqual(self.journal.read(), before)
        self.assertEqual(self.sent, [])
        self.assertTrue((self.root / 'preview.png').is_file())

    def test_preview_is_available_while_consumer_owns_sender_lock(self):
        self.journal.request(self.now, force=True)
        before = self.journal.read()
        with self.journal.sender_lock():
            result = self.worker.tick(no_push=True)
        self.assertEqual(result['status'], 'preview')
        self.assertEqual(self.journal.read(), before)
        self.assertEqual(self.sent, [])

    def test_restarting_consumer_retries_repaired_permanent_error_once(self):
        self.journal.request(self.now, force=True)
        self.journal.fail('bluetooth_off', self.now, permanent=True)
        stopping = threading.Event()
        def report(result):
            stopping.set()
        self.worker.run(stopping, report)
        self.assertEqual(self.sent, [(0,0,0)])
        self.assertEqual(self.journal.read()['failure_count'], 0)

    def test_idle_consumer_picks_up_new_event_without_restarting(self):
        stopping, idle, delivered = threading.Event(), threading.Event(), threading.Event()
        failures = []
        self.journal.request(self.now, force=True)
        def report(result):
            if result['status'] == 'idle':
                idle.set()
            if len(self.sent) == 2:
                delivered.set()
                stopping.set()
        def run():
            try:
                self.worker.run(stopping, report)
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(idle.wait(3))
            self.event('Stop')
            self.journal.request(self.now, force=True)
            self.assertTrue(delivered.wait(7), 'idle consumer did not wake')
        finally:
            stopping.set()
            thread.join(timeout=3)
        self.assertEqual(failures, [])
        self.assertEqual(self.sent, [(0,0,0), (198,40,40)])

    def test_competing_worker_cannot_send_while_owner_active(self):
        with self.journal.sender_lock():
            self.journal.request(self.now, force=True)
            self.assertEqual(self.worker.tick()['status'], 'busy')
        self.assertEqual(self.sent, [])
        self.worker.tick()
        self.assertEqual(len(self.sent), 1)

    def test_corrupt_source_is_not_rendered_as_an_empty_dashboard(self):
        self.state_file.write_text('broken')
        self.worker.tick()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.journal.read()['last_error_code'], 'source_state_invalid')
        self.assertEqual(self.state_file.read_text(), 'broken')

    def test_malformed_status_is_reported_without_a_crash(self):
        self.state_file.write_text('{"version":1,"threads":{"a":{"session_id":"a","updated_at":1,"status":[]}}}')
        result = self.worker.tick()
        self.assertEqual(result, {'status':'blocked', 'error_code':'source_state_invalid'})
        self.assertEqual(self.sent, [])

    def test_off_palette_frame_is_not_sent(self):
        self.worker.render = lambda state:Image.new('RGB', (400,300), (127,127,127))
        self.assertEqual(self.start()['status'], 'blocked')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.journal.read()['last_error_code'], 'invalid_frame')

    def test_periodic_request_refreshes_without_hook_event(self):
        self.start()
        previous = self.journal.read()['requested_revision']
        self.now = 700
        self.worker.tick()
        self.assertGreater(self.journal.read()['requested_revision'], previous)
        self.now += 30
        self.worker.tick()
        self.assertEqual(self.journal.read()['requested_revision'], self.journal.read()['acknowledged_revision'])

    def test_killed_consumer_preserves_pending_and_releases_os_lock(self):
        self.journal.request(100, force=True)
        context = multiprocessing.get_context('spawn')
        started = context.Event()
        process = context.Process(target=crashable_consumer, args=(str(self.root), started))
        process.start()
        try:
            self.assertTrue(started.wait(timeout=10))
        finally:
            process.terminate()
            process.join(timeout=5)
        self.assertEqual(self.journal.read()['acknowledged_revision'], 0)
        self.assertIsNone(self.journal.read()['last_sent_at'])
        self.worker.tick()
        self.assertEqual(len(self.sent), 1)

    def test_run_holds_ownership_during_idle_and_recovers_new_event(self):
        stopping = threading.Event()
        watchdog = threading.Timer(5, stopping.set)
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        # Start with an explicit force request to avoid real-time debounce.
        self.journal.request(self.now, force=True)
        seen = []
        def report(result):
            seen.append(result['status'])
            if result['status'] == 'sent' and len(self.sent) == 1:
                self.assertEqual(self.worker.tick()['status'], 'busy')
                self.event('Stop')
                self.journal.request(self.now, force=True)
            if result['status'] == 'sent' and len(self.sent) == 2:
                stopping.set()
        self.worker.run(stopping, report)
        self.assertEqual(self.sent, [(0,0,0), (198,40,40)])
        self.assertEqual(seen, ['sent', 'sent'])

    def test_actual_sender_keeps_lock_after_consumer_is_killed(self):
        binary = self.root/'slow-sender'
        binary.write_text(python_executable_header() + '''import socket, sys
from pathlib import Path
frame = Path(sys.argv[sys.argv.index('--input') + 1])
client = socket.socket(socket.AF_UNIX)
client.connect(str(frame.parent.parent/'control.sock'))
client.sendall(b'ready')
client.recv(1)
client.close()
print('WRITE_COMPLETE packets=129 bytes=30511')
''')
        binary.chmod(0o700)
        self.journal.request(100, force=True)
        server = socket.socket(socket.AF_UNIX)
        server.settimeout(10)
        server.bind(str(self.root/'control.sock'))
        server.listen()
        self.addCleanup(server.close)
        process = multiprocessing.get_context('spawn').Process(target=subprocess_consumer, args=(str(self.root),))
        process.start()
        connection = None
        try:
            connection, _ = server.accept()
            connection.settimeout(5)
            self.assertEqual(connection.recv(5), b'ready')
            process.kill()
            process.join(timeout=5)
            # Real transport is still blocked on our socket, not a fake callback.
            with self.journal.sender_lock() as owner:
                self.assertFalse(owner, 'orphaned transport released ownership too early')
        finally:
            if connection:
                connection.sendall(b'x')
                connection.close()
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        deadline = time.monotonic() + 5
        available = False
        while time.monotonic() < deadline:
            with self.journal.sender_lock() as owner:
                if owner:
                    available = True
                    break
            threading.Event().wait(.02)
        self.assertTrue(available, 'transport lock not released after completion')
        self.assertEqual(self.journal.read()['acknowledged_revision'], 0)


if __name__ == '__main__':
    unittest.main()
