import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

try:
    from tools.sync_journal import SyncJournal, JournalError
except ImportError:
    SyncJournal = None
    JournalError = RuntimeError


def request_many(directory, barrier):
    journal = SyncJournal(Path(directory))
    barrier.wait(timeout=10)
    for _ in range(25):
        journal.request(100)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SyncJournal, 'durable journal is not implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = SyncJournal(self.root)

    def test_acknowledges_only_attempted_revision(self):
        self.journal.request(100, force=True)
        self.journal.request(101, force=True)
        self.journal.acknowledge(1, 'a' * 64, 102, sent=True)
        state = SyncJournal(self.root).read()
        self.assertEqual(state['requested_revision'], 2)
        self.assertEqual(state['acknowledged_revision'], 1)
        self.assertEqual(state['force_revision'], 2)
        self.assertEqual(state['last_sent_at'], 102)
        self.assertIsNotNone(self.journal.begin(102))

    def test_continuous_events_do_not_postpone_debounce(self):
        for time in (100, 101, 102, 104):
            self.journal.request(time)
        self.assertIsNone(self.journal.begin(104))
        self.assertEqual(self.journal.begin(105)['requested_revision'], 4)

    def test_deduplication_does_not_claim_new_send(self):
        self.journal.request(100, force=True)
        self.journal.acknowledge(1, 'a' * 64, 101, sent=True)
        self.journal.request(110)
        self.journal.acknowledge(2, 'a' * 64, 115, sent=False)
        state = self.journal.read()
        self.assertEqual(state['last_sent_at'], 101)
        self.assertEqual(state['acknowledged_revision'], 2)
        self.assertIsNone(state['pending_since'])

    def test_failed_attempt_stays_pending_with_capped_backoff(self):
        self.journal.request(100, force=True)
        for count, (now, retry) in enumerate(((100,105), (105,120), (120,150), (150,210), (210,270)), 1):
            self.journal.fail('send_failed', now)
            self.journal.request(now + 1)
            state = self.journal.read()
            self.assertEqual(state['retry_at'], retry)
            self.assertEqual(state['failure_count'], count)
            self.assertEqual(state['acknowledged_revision'], 0)
            self.assertIsNone(self.journal.begin(retry - 1))
            self.assertIsNotNone(self.journal.begin(retry))

    def test_permanent_failure_requires_explicit_retry(self):
        self.journal.request(100, force=True)
        self.journal.fail('permission_denied', 101, permanent=True)
        self.journal.request(200, force=True)
        self.assertIsNone(self.journal.begin(1000))
        self.journal.retry(1001)
        self.assertIsNotNone(self.journal.begin(1001))

    def test_observed_metadata_is_reconciled_once(self):
        self.journal.observe('b' * 64, 100)
        self.journal.observe('b' * 64, 101)
        self.journal.observe('c' * 64, 102)
        self.assertEqual(self.journal.read()['requested_revision'], 2)
        self.assertEqual(self.journal.read()['pending_since'], 100)

    def test_invalid_state_and_future_version_are_preserved(self):
        self.journal.request(100)
        good = self.journal.read()
        for content in ('broken', json.dumps({**good, 'version':2}), json.dumps({**good, 'acknowledged_revision':9}), json.dumps({**good, 'failure_count':True})):
            self.journal.path.write_text(content)
            with self.assertRaises(JournalError):
                self.journal.request(110)
            self.assertEqual(self.journal.path.read_text(), content)

    def test_malformed_error_and_huge_timestamp_fail_closed(self):
        self.journal.request(100)
        good = self.journal.read()
        for field, value in (('last_error_code', []), ('pending_since', 10 ** 400)):
            self.journal.path.write_text(json.dumps({**good, field:value}))
            with self.assertRaises(JournalError):
                self.journal.read()

    def test_out_of_order_or_future_ack_is_rejected(self):
        self.journal.request(100)
        with self.assertRaises(JournalError):
            self.journal.acknowledge(2, 'a' * 64, 105, sent=True)
        self.journal.acknowledge(1, 'a' * 64, 105, sent=True)
        with self.assertRaises(JournalError):
            self.journal.acknowledge(0, 'b' * 64, 106, sent=True)
        self.assertEqual(self.journal.read()['last_sent_frame_hash'], 'a' * 64)

    def test_multiprocess_registration_never_loses_a_request(self):
        context = multiprocessing.get_context('spawn')
        barrier = context.Barrier(3)
        processes = [context.Process(target=request_many, args=(str(self.root), barrier)) for _ in range(2)]
        for process in processes:
            process.start()
        barrier.wait(timeout=10)
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(self.journal.read()['requested_revision'], 50)

    def test_sender_ownership_is_exclusive_and_released(self):
        with self.journal.sender_lock() as owner:
            self.assertTrue(owner)
            with SyncJournal(self.root).sender_lock() as other:
                self.assertFalse(other)
        with self.journal.sender_lock() as next_owner:
            self.assertTrue(next_owner)


if __name__ == '__main__':
    unittest.main()
